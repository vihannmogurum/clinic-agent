"""
Demo Clinic WhatsApp Patient Intake & Appointment Agent.

FastAPI entrypoint and Meta WhatsApp Cloud API webhook handler.
"""

from dotenv import load_dotenv
load_dotenv(override=True)

import datetime
import asyncio
import logging
import os
import pathlib
import re

from fastapi import FastAPI, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
from datetime import timezone, timedelta

import uvicorn
import uuid
import calendar_service
import llm
import sheets_service
import state
import whatsapp
import appointment_store


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("clinic_agent")

app = FastAPI(title="Demo Clinic WhatsApp Agent")

# CORS middleware for web widget embedding
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")

IST = timezone(timedelta(hours=5, minutes=30))
UNANSWERED_LOG = pathlib.Path(__file__).parent / "data" / "unanswered_questions.log"
META_WA_VERIFY_TOKEN = os.getenv("META_WA_VERIFY_TOKEN", "clinic_verify_secret")

# Booking states. IDLE is represented by None in the persisted state.
IDLE = None
AWAITING_NAME = "name"
AWAITING_SERVICE = "service"
AWAITING_DATETIME = "slot"
CONFIRMED = "confirmed"
AWAITING_CANCEL_CONFIRMATION = "cancel_confirmation"
AWAITING_CANCEL_SELECTION = "cancel_selection"
AWAITING_RESCHEDULE_DATETIME = "reschedule_datetime"
AWAITING_RESCHEDULE_SELECTION = "reschedule_selection"
_WEBHOOK_LOCKS: dict[str, asyncio.Lock] = {}


@app.on_event("startup")
async def validate_google_sheets_configuration() -> None:
    try:
        appointment_store._config()
    except RuntimeError as error:
        logger.warning("Google Sheets is not ready: %s", error)


def _send_whatsapp_reply(to_phone: str, text: str) -> Response:
    logger.info("Attempting outbound WhatsApp message to %s: %r", to_phone, text)
    if not whatsapp.send_whatsapp_message(to_phone, text):
        logger.error("Failed to send WhatsApp message to %s", to_phone)
    return Response(content="OK", status_code=200)


class SlotResolutionError(RuntimeError):
    """Raised when the LLM cannot resolve an appointment time."""


def _parse_slot_datetime(slot_str: str) -> datetime.datetime:
    """Resolve an appointment time through the LLM and return an IST datetime."""
    try:
        resolved_slot = llm.parse_slot_with_llm(slot_str)
        dt = datetime.datetime.fromisoformat(resolved_slot)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=IST)
        return dt.astimezone(IST)
    except Exception as e:
        logger.warning("LLM slot parsing failed for '%s': %s", slot_str, e)
        raise SlotResolutionError(
            "The appointment time could not be resolved by the LLM."
        ) from e


def _parse_slot_with_session_date(slot_str: str, patient: dict) -> datetime.datetime:
    """Resolve a slot while retaining a previously rejected explicit date."""
    parsed = _parse_slot_datetime(slot_str)
    has_date_reference = bool(re.search(
        r"\b(today|tomorrow|day after tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday|\d{4}[-/]\d{1,2}[-/]\d{1,2})\b",
        slot_str.lower(),
    ))
    candidate_date = patient.get("candidate_date")
    if candidate_date and not has_date_reference:
        parsed = parsed.replace(
            year=int(candidate_date[:4]),
            month=int(candidate_date[5:7]),
            day=int(candidate_date[8:10]),
        )
    return parsed


def _remember_candidate_date(patient: dict, slot_dt: datetime.datetime) -> None:
    patient["candidate_date"] = slot_dt.date().isoformat()


def _format_slot(slot_dt: datetime.datetime) -> str:
    return slot_dt.strftime("%A, %B %d, %Y at %I:%M %p")


def _format_slot_for_sheet(slot_dt: datetime.datetime) -> str:
    return slot_dt.strftime("%d-%m-%y %I:%M %p")


def _begin_booking(patient: dict, phone: str) -> str:
    """Start a booking without consuming the trigger message as a field."""
    patient["booking"] = {
        "phone": phone,
        "name": None,
        "service": None,
        "slot": None,
    }
    patient["step"] = AWAITING_NAME
    patient["intent"] = "book_appointment"
    return (
        "I would be glad to help you book an appointment! "
        "First, could you please provide your full name?"
    )


def _is_booking_trigger(message: str) -> bool:
    """Prevent a booking command from being stored as a patient's name."""
    normalized = re.sub(r"\s+", " ", message.strip().lower())
    return normalized in {
        "book",
        "book appointment",
        "book an appointment",
        "make an appointment",
        "schedule an appointment",
        "get an appointment",
        "need an appointment",
    }


def _is_cancellation(message: str) -> bool:
    return message.strip().lower() in {"cancel", "cancel booking", "stop", "never mind"}


def _is_confirmed_cancellation(message: str) -> bool:
    normalized = message.strip().lower()
    return "cancel" in normalized and ("appointment" in normalized or "booking" in normalized)


def _is_reschedule(message: str) -> bool:
    normalized = message.strip().lower()
    return any(term in normalized for term in ("reschedule", "change my appointment", "move my appointment"))


def _appointment_summary(appointment: dict) -> str:
    return (
        f"{appointment['service_name']} on {appointment['appointment_date']} "
        f"at {appointment['start_time']} (Fee: ₹{appointment['fee_quoted']})"
    )


def _cancel_booking(patient: dict) -> str:
    patient["booking"] = None
    patient["step"] = IDLE
    patient["intent"] = None
    return "Your appointment booking has been cancelled. You can start again whenever you are ready."


def _reset_appointment_action(patient: dict) -> None:
    patient["booking"] = None
    patient["step"] = IDLE
    patient["intent"] = None
    patient.pop("appointment_ids", None)
    patient.pop("target_appointment_id", None)
    patient.pop("candidate_date", None)


def _start_confirmed_cancellation(patient: dict, phone: str) -> str:
    appointments = appointment_store.upcoming(phone)
    if not appointments:
        return "I couldn't find any upcoming confirmed appointments under your phone number."
    if len(appointments) == 1:
        appointment = dict(appointments[0])
        patient["target_appointment_id"] = appointment["appointment_id"]
        patient["step"] = AWAITING_CANCEL_CONFIRMATION
        return (
            f"I found your appointment for {_appointment_summary(appointment)}. "
            "Reply YES to confirm cancellation, or NO to keep it."
        )
    patient["appointment_ids"] = [row["appointment_id"] for row in appointments]
    patient["step"] = AWAITING_CANCEL_SELECTION
    lines = ["I found multiple upcoming appointments. Reply with the number to cancel:"]
    lines.extend(f"{index}. {_appointment_summary(dict(row))}" for index, row in enumerate(appointments, 1))
    return "\n".join(lines)


def _start_reschedule(patient: dict, phone: str) -> str:
    appointments = appointment_store.upcoming(phone)
    if not appointments:
        return "I couldn't find any upcoming confirmed appointments to reschedule."
    if len(appointments) == 1:
        appointment = dict(appointments[0])
        patient["target_appointment_id"] = appointment["appointment_id"]
        patient["step"] = AWAITING_RESCHEDULE_DATETIME
        return (
            f"I found your appointment for {_appointment_summary(appointment)}. "
            "What new date and time would you prefer?"
        )
    patient["appointment_ids"] = [row["appointment_id"] for row in appointments]
    patient["step"] = AWAITING_RESCHEDULE_SELECTION
    lines = ["I found multiple upcoming appointments. Reply with the number to reschedule:"]
    lines.extend(f"{index}. {_appointment_summary(dict(row))}" for index, row in enumerate(appointments, 1))
    return "\n".join(lines)


def _log_unanswered_question(phone: str, question: str) -> None:
    """Append unanswered questions to data/unanswered_questions.log."""
    try:
        UNANSWERED_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(UNANSWERED_LOG, "a", encoding="utf-8") as f:
            now_str = datetime.datetime.now(IST).isoformat()
            f.write(f"[{now_str}] Phone: {phone} | Question: {question}" + chr(10))
    except Exception as e:
        logger.error("Failed to log unanswered question: %s", e)


def _handle_booking_completion(patient: dict, from_phone: str, IST, now_iso: str,
                                calendar_service, sheets_service, whatsapp, logger):
    """Complete the booking when all slots are filled."""
    name = patient["booking"]["name"]
    service = patient["booking"]["service"]
    slot_str = patient["booking"]["slot"]
    phone = from_phone.replace("whatsapp:", "").strip()

    try:
        slot_dt = _parse_slot_datetime(slot_str)
    except SlotResolutionError:
        patient["step"] = AWAITING_DATETIME
        return _send_whatsapp_reply(
            from_phone,
            "I couldn't resolve that date and time. "
            "Please provide it again, for example: tomorrow at 10 AM."
        )
    formatted_slot = _format_slot(slot_dt)

    booking_error = False

    # Create Calendar event
    try:
        calendar_service.create_event(
            summary=f"{service} - {name}",
            start=slot_dt,
        )
    except Exception as e:
        logger.error("Calendar API failed: %s", e)
        booking_error = True

    # Alert clinic owner (fire and forget - still async)
    try:
        whatsapp.send_owner_alert(
            patient_name=name,
            service=service,
            slot=formatted_slot,
            exclude_phone=from_phone,
        )
    except Exception as e:
        logger.error("Owner alert WhatsApp message failed: %s", e)

    # Reply to patient
    if not booking_error:
        reply = (
            f"Your appointment for {service} has been confirmed for {formatted_slot}!\n"
            f"Name: {name}\n"
            "We look forward to seeing you at Demo Clinic."
        )
    else:
        reply = (
            f"Thank you, {name}. We received your booking request for {service} on {formatted_slot}. "
            "Our scheduling system encountered a minor delay, but our team has been notified "
            "and will contact you directly to confirm your slot!"
        )

    patient["booking"] = None
    patient["step"] = IDLE
    patient["intent"] = None
    return _send_whatsapp_reply(from_phone, reply)


@app.get("/")
async def health_check():
    return {"status": "ok", "clinic": "Demo Clinic"}


@app.get("/webhook/whatsapp")
async def verify_whatsapp_webhook(
    hub_mode: str | None = Query(None, alias="hub.mode"),
    hub_verify_token: str | None = Query(None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(None, alias="hub.challenge"),
):
    if (
        hub_mode == "subscribe"
        and hub_verify_token == META_WA_VERIFY_TOKEN
        and hub_challenge is not None
    ):
        return Response(content=hub_challenge, media_type="text/plain")
    return Response(content="Forbidden", status_code=403)


@app.post("/webhook/whatsapp")
async def whatsapp_webhook(request: Request):
    """Handle Meta WhatsApp Cloud API webhook events."""
    payload = await request.json()
    parsed = whatsapp.parse_meta_incoming(payload)
    body = parsed["body"]
    from_phone = parsed["from_phone"]
    message_id = parsed.get("message_id")

    if not from_phone or not body:
        return Response(content="OK", status_code=200)

    lock = _WEBHOOK_LOCKS.setdefault(from_phone, asyncio.Lock())
    async with lock:
        if not state.mark_message_processed(from_phone, message_id):
            logger.info("Ignoring duplicate WhatsApp message %s from %s", message_id, from_phone)
            return Response(content="OK", status_code=200)
        return await _process_whatsapp_message(from_phone, body)


async def _process_whatsapp_message(from_phone: str, body: str) -> Response:
    """Process one unique inbound WhatsApp message."""
    patient = state.get(from_phone)

    # 1. Consent check
    if not patient["consent"]:
        if body.strip().upper() == "YES":
            patient["consent"] = True
            reply = (
                "Thank you for confirming! Welcome to Demo Clinic.\n\n"
                "How can I help you today? You can ask about pricing, hours, or request to book an appointment."
            )
        else:
            reply = (
                "Hi! Before we proceed, please confirm you agree to share your "
                "details with Demo Clinic for appointment purposes. Reply YES to continue."
            )
        return _send_whatsapp_reply(from_phone, reply)

    # 2. In-progress booking flow
    step = patient.get("step")
    if step == AWAITING_CANCEL_CONFIRMATION:
        if body.strip().upper() == "YES":
            appointment_id = patient.get("target_appointment_id")
            appointment = appointment_store.get_appointment(appointment_id)
            if appointment:
                if appointment.get("calendar_event_id"):
                    try:
                        calendar_service.delete_event(appointment["calendar_event_id"])
                    except Exception as e:
                        logger.error("Calendar cancellation failed: %s", e)
                        return _send_whatsapp_reply(
                            from_phone,
                            "I couldn't cancel the calendar booking right now. "
                            "Your appointment is still scheduled; please try again.",
                        )
                appointment_store.set_status(appointment_id, "CANCELLED")
                _reset_appointment_action(patient)
                return _send_whatsapp_reply(
                    from_phone,
                    f"Your appointment for {appointment['service_name']} on "
                    f"{appointment['appointment_date']} at {appointment['start_time']} has been cancelled.",
                )
            _reset_appointment_action(patient)
            return _send_whatsapp_reply(from_phone, "I couldn't find that appointment.")
        if body.strip().upper() == "NO":
            _reset_appointment_action(patient)
            return _send_whatsapp_reply(from_phone, "Got it! Your appointment remains scheduled.")
        return _send_whatsapp_reply(from_phone, "Please reply YES to confirm cancellation, or NO to keep it.")
    if step == AWAITING_CANCEL_SELECTION:
        try:
            index = int(body.strip()) - 1
            appointment_id = patient["appointment_ids"][index]
            patient["target_appointment_id"] = appointment_id
            patient["step"] = AWAITING_CANCEL_CONFIRMATION
            appointment = dict(appointment_store.get_appointment(appointment_id))
            return _send_whatsapp_reply(
                from_phone,
                f"I found your appointment for {_appointment_summary(appointment)}. "
                "Reply YES to confirm cancellation, or NO to keep it.",
            )
        except (ValueError, IndexError, TypeError):
            return _send_whatsapp_reply(from_phone, "Please reply with one of the appointment numbers listed.")
    if step == AWAITING_RESCHEDULE_SELECTION:
        try:
            index = int(body.strip()) - 1
            patient["target_appointment_id"] = patient["appointment_ids"][index]
            patient["step"] = AWAITING_RESCHEDULE_DATETIME
            return _send_whatsapp_reply(from_phone, "What new date and time would you prefer?")
        except (ValueError, IndexError, TypeError):
            return _send_whatsapp_reply(from_phone, "Please reply with one of the appointment numbers listed.")
    if step == AWAITING_RESCHEDULE_DATETIME:
        try:
            requested_dt = _parse_slot_with_session_date(body.strip(), patient)
            appointment_id = patient["target_appointment_id"]
            current = appointment_store.get_appointment(appointment_id)
            availability = appointment_store.check_slot_availability(
                current["service_name"], requested_dt, appointment_id
            )
            if not availability["available"]:
                suggestions = ", ".join(availability["suggestions"])
                return _send_whatsapp_reply(
                    from_phone,
                    f"{availability['reason']} "
                    f"Available alternatives: {suggestions}. Please provide another time.",
                )
            calendar_updated = False
            old_start = datetime.datetime.fromisoformat(
                f"{current['appointment_date']}T{current['start_time']}"
            ).replace(tzinfo=IST)
            if current.get("calendar_event_id"):
                calendar_service.update_event(
                    current["calendar_event_id"],
                    requested_dt,
                    availability["duration_mins"],
                )
                calendar_updated = True
            try:
                result = appointment_store.reschedule(appointment_id, requested_dt)
            except Exception:
                if calendar_updated:
                    try:
                        calendar_service.update_event(
                            current["calendar_event_id"],
                            old_start,
                            availability["duration_mins"],
                        )
                    except Exception as rollback_error:
                        logger.error("Calendar reschedule rollback failed: %s", rollback_error)
                raise
            appointment = result["appointment"]
            _reset_appointment_action(patient)
            _remember_candidate_date(patient, requested_dt)
            return _send_whatsapp_reply(
                from_phone,
                f"Your appointment for {appointment['service_name']} has been rescheduled to "
                f"{appointment['appointment_date']} at {appointment['start_time']}. "
                f"Consultation/Service Fee: ₹{appointment['fee_quoted']}.",
            )
        except (SlotResolutionError, ValueError) as e:
            logger.warning("Rescheduling failed: %s", e)
            return _send_whatsapp_reply(
                from_phone,
                f"{e} Please provide another date and time, for example: tomorrow at 10 AM.",
            )
    if not step and _is_confirmed_cancellation(body):
        return _send_whatsapp_reply(from_phone, _start_confirmed_cancellation(patient, from_phone))
    if not step and _is_reschedule(body):
        return _send_whatsapp_reply(from_phone, _start_reschedule(patient, from_phone))
    if step and _is_cancellation(body):
        return _send_whatsapp_reply(from_phone, _cancel_booking(patient))
    if step:
        if step == AWAITING_NAME:
            if _is_booking_trigger(body):
                return _send_whatsapp_reply(
                    from_phone,
                    "Please provide your full name so I can continue the booking.",
                )
            patient["booking"]["name"] = body.strip()
            patient["step"] = AWAITING_SERVICE
            pname = patient["booking"]["name"]
            reply = (
                f"Thanks, {pname}! What service or test would you like to book? "
                "(e.g., General Consultation, Complete Blood Count, Abdominal Ultrasound)"
            )
            return _send_whatsapp_reply(from_phone, reply)

        elif step == AWAITING_SERVICE:
            patient["booking"]["service"] = body.strip()
            patient["step"] = AWAITING_DATETIME
            reply = (
                "Got it! What date and time would you prefer for your appointment? "
                "(e.g., Tomorrow at 10 AM, 2026-10-20 11:00 AM)"
            )
            return _send_whatsapp_reply(from_phone, reply)

        elif step == AWAITING_DATETIME:
            try:
                slot_dt = _parse_slot_with_session_date(body.strip(), patient)
            except SlotResolutionError:
                return _send_whatsapp_reply(
                    from_phone,
                    "I couldn't resolve that date and time. "
                    "Please provide it again, for example: tomorrow at 10 AM."
                )
            formatted_slot = _format_slot(slot_dt)
            patient["booking"]["slot"] = formatted_slot
            name = patient["booking"]["name"]
            service = patient["booking"]["service"]
            phone = from_phone.replace("whatsapp:", "").strip()

            now_iso = datetime.datetime.now(IST).isoformat()

            availability = appointment_store.check_slot_availability(service, slot_dt)
            if not availability["available"]:
                _remember_candidate_date(patient, slot_dt)
                suggestions = (
                    f" Next available slots: {', '.join(availability['suggestions'])}."
                    if availability["suggestions"] else ""
                )
                return _send_whatsapp_reply(
                    from_phone,
                    f"Sorry, {availability['reason']}{suggestions} Please provide another time.",
                )

            booking_error = False
            calendar_event = None

            # Create Calendar event
            try:
                calendar_event = calendar_service.create_event(
                    summary=f"{service} - {name}",
                    start=slot_dt,
                    duration_minutes=availability["duration_mins"],
                )
            except Exception as e:
                logger.error("Calendar API failed: %s", e)
                booking_error = True

            if not booking_error:
                try:
                    appointment_store.create_appointment(
                        phone=phone,
                        patient_name=name,
                        service_name=service,
                        requested_dt=slot_dt,
                        calendar_event_id=(calendar_event or {}).get("id"),
                    )
                except Exception as e:
                    logger.error("Appointment ledger failed: %s", e)
                    booking_error = True

            # Alert clinic owner (fire and forget - still async)
            try:
                whatsapp.send_owner_alert(
                    patient_name=name,
                    service=service,
                    slot=formatted_slot,
                    exclude_phone=from_phone,
                )
            except Exception as e:
                logger.error("Owner alert WhatsApp message failed: %s", e)

            # Reply to patient
            if not booking_error:
                reply = (
                    f"Your appointment for {service} has been confirmed for {formatted_slot}!\n"
                    f"Name: {name}\n"
                    f"Fee: ₹{availability['fee']}\n"
                    "We look forward to seeing you at Demo Clinic."
                )
            else:
                reply = (
                    f"Thank you, {name}. We received your booking request for {service} on {formatted_slot}. "
                    "Our scheduling system encountered a minor delay, but our team has been notified "
                    "and will contact you directly to confirm your slot!"
                )

            patient["booking"] = None
            patient["step"] = IDLE
            patient["intent"] = None
            patient.pop("candidate_date", None)
            return _send_whatsapp_reply(from_phone, reply)

    # 3. Answer live clinic-data questions before the static FAQ fallback.
    live_answer = appointment_store.answer_reference_question(body)
    if live_answer is not None:
        return _send_whatsapp_reply(from_phone, live_answer)

    # 4. Intent routing
    try:
        intent = llm.classify_intent(body)
    except Exception as e:
        logger.error("Groq intent classification failed: %s", e)
        intent = "unclear"

    patient["intent"] = intent

    if intent == "faq_question":
        try:
            answer = llm.answer_faq(body)
            if answer.strip() == "NOT_IN_FAQ":
                reply = "Let me connect you with our team on that — they will follow up shortly."
                _log_unanswered_question(from_phone, body)
            else:
                reply = answer
        except Exception as e:
            logger.error("Groq FAQ answering failed: %s", e)
            reply = "Let me connect you with our team on that — they will follow up shortly."
            _log_unanswered_question(from_phone, body)

        return _send_whatsapp_reply(from_phone, reply)

    elif intent == "book_appointment":
        return _send_whatsapp_reply(from_phone, _begin_booking(patient, from_phone))

    else:
        reply = "Are you looking to book an appointment or do you have a question about our services?"
        return _send_whatsapp_reply(from_phone, reply)


# ============ Web Chat API Endpoint ============

class ChatRequest(BaseModel):
    message: str
    patient_name: str | None = None
    phone: str | None = None
    session_id: str | None = None


class ChatResponse(BaseModel):
    reply: str
    booked: bool


async def process_chat_message(
    message: str,
    patient_name: str | None = None,
    phone: str | None = None,
    session_id: str | None = None,
) -> ChatResponse:
    """Core chat processing logic shared by web widget and WhatsApp."""
    from_phone = phone or session_id or str(uuid.uuid4())

    patient = state.get(f"web:{from_phone}")

    # Consent is implicit for web chat
    if not patient["consent"]:
        patient["consent"] = True

    # Check in-progress booking flow
    step = patient.get("step")
    if step == AWAITING_CANCEL_CONFIRMATION:
        if message.strip().upper() == "YES":
            appointment = appointment_store.get_appointment(patient.get("target_appointment_id"))
            if appointment:
                if appointment.get("calendar_event_id"):
                    try:
                        calendar_service.delete_event(appointment["calendar_event_id"])
                    except Exception as e:
                        logger.error("Calendar cancellation failed: %s", e)
                        return ChatResponse(
                            reply="I couldn't cancel the calendar booking right now. "
                            "Your appointment is still scheduled; please try again.",
                            booked=False,
                        )
                appointment_store.set_status(appointment["appointment_id"], "CANCELLED")
                _reset_appointment_action(patient)
                return ChatResponse(
                    reply=f"Your appointment for {appointment['service_name']} on "
                    f"{appointment['appointment_date']} at {appointment['start_time']} has been cancelled.",
                    booked=False,
                )
            _reset_appointment_action(patient)
            return ChatResponse(reply="I couldn't find that appointment.", booked=False)
        if message.strip().upper() == "NO":
            _reset_appointment_action(patient)
            return ChatResponse(reply="Got it! Your appointment remains scheduled.", booked=False)
        return ChatResponse(reply="Please reply YES to confirm cancellation, or NO to keep it.", booked=False)
    if step == AWAITING_RESCHEDULE_DATETIME:
        try:
            requested_dt = _parse_slot_with_session_date(message.strip(), patient)
            appointment_id = patient["target_appointment_id"]
            appointment = appointment_store.get_appointment(appointment_id)
            availability = appointment_store.check_slot_availability(
                appointment["service_name"], requested_dt, appointment_id
            )
            if not availability["available"]:
                return ChatResponse(
                    reply=f"{availability['reason']} Please provide another time.",
                    booked=False,
                )
            calendar_updated = False
            old_start = datetime.datetime.fromisoformat(
                f"{appointment['appointment_date']}T{appointment['start_time']}"
            ).replace(tzinfo=IST)
            if appointment.get("calendar_event_id"):
                calendar_service.update_event(
                    appointment["calendar_event_id"], requested_dt, availability["duration_mins"]
                )
                calendar_updated = True
            try:
                updated = appointment_store.reschedule(appointment_id, requested_dt)["appointment"]
            except Exception:
                if calendar_updated:
                    try:
                        calendar_service.update_event(
                            appointment["calendar_event_id"],
                            old_start,
                            availability["duration_mins"],
                        )
                    except Exception as rollback_error:
                        logger.error("Calendar reschedule rollback failed: %s", rollback_error)
                raise
            _reset_appointment_action(patient)
            return ChatResponse(
                reply=f"Your appointment for {updated['service_name']} has been rescheduled to "
                f"{updated['appointment_date']} at {updated['start_time']}. "
                f"Consultation/Service Fee: ₹{updated['fee_quoted']}.",
                booked=False,
            )
        except (SlotResolutionError, ValueError) as e:
            if "requested_dt" in locals():
                _remember_candidate_date(patient, requested_dt)
            return ChatResponse(reply=f"{e} Please provide another date and time.", booked=False)
    if not step and _is_confirmed_cancellation(message):
        return ChatResponse(reply=_start_confirmed_cancellation(patient, from_phone), booked=False)
    if not step and _is_reschedule(message):
        return ChatResponse(reply=_start_reschedule(patient, from_phone), booked=False)
    if step and _is_cancellation(message):
        return ChatResponse(reply=_cancel_booking(patient), booked=False)
    if step:
        if step == AWAITING_NAME:
            if _is_booking_trigger(message):
                return ChatResponse(
                    reply="Please provide your full name so I can continue the booking.",
                    booked=False,
                )
            patient["booking"]["name"] = message.strip()
            patient["step"] = AWAITING_SERVICE
            pname = patient["booking"]["name"]
            reply = (
                f"Thanks, {pname}! What service or test would you like to book? "
                "(e.g., General Consultation, Complete Blood Count, Abdominal Ultrasound)"
            )
            return ChatResponse(reply=reply, booked=False)

        elif step == AWAITING_SERVICE:
            patient["booking"]["service"] = message.strip()
            patient["step"] = AWAITING_DATETIME
            reply = (
                "Got it! What date and time would you prefer for your appointment? "
                "(e.g., Tomorrow at 10 AM, 2026-10-20 11:00 AM)"
            )
            return ChatResponse(reply=reply, booked=False)

        elif step == AWAITING_DATETIME:
            try:
                slot_dt = _parse_slot_with_session_date(message.strip(), patient)
            except SlotResolutionError:
                return ChatResponse(
                    reply=(
                        "I couldn't resolve that date and time. "
                        "Please provide it again, for example: tomorrow at 10 AM."
                    ),
                    booked=False,
                )
            formatted_slot = _format_slot(slot_dt)
            patient["booking"]["slot"] = formatted_slot
            name = patient["booking"]["name"]
            service = patient["booking"]["service"]
            phone_clean = from_phone.replace("whatsapp:", "").strip()

            now_iso = datetime.datetime.now(IST).isoformat()
            sheet_slot = _format_slot_for_sheet(slot_dt)

            availability = appointment_store.check_slot_availability(service, slot_dt)
            if not availability["available"]:
                _remember_candidate_date(patient, slot_dt)
                suggestions = ", ".join(availability["suggestions"])
                return ChatResponse(
                    reply=f"{availability['reason']} "
                    f"{'Available alternatives: ' + suggestions + '. ' if suggestions else ''}"
                    "Please provide another time.",
                    booked=False,
                )

            booking_error = False
            calendar_event = None

            # Create Calendar event
            try:
                calendar_event = calendar_service.create_event(
                    summary=f"{service} - {name}",
                    start=slot_dt,
                    duration_minutes=availability["duration_mins"],
                )
            except Exception as e:
                logger.error("Calendar API failed: %s", e)
                booking_error = True

            if not booking_error:
                try:
                    appointment_store.create_appointment(
                        phone=phone_clean,
                        patient_name=name,
                        service_name=service,
                        requested_dt=slot_dt,
                        calendar_event_id=(calendar_event or {}).get("id"),
                    )
                except Exception as e:
                    logger.error("Appointment ledger failed: %s", e)
                    booking_error = True

            # Reply to patient
            if not booking_error:
                reply = (
                    f"Your appointment for {service} has been confirmed for {formatted_slot}!\n"
                    f"Name: {name}\n"
                    f"Fee: ₹{availability['fee']}\n"
                    "We look forward to seeing you at Demo Clinic."
                )
            else:
                reply = (
                    f"Thank you, {name}. We received your booking request for {service} on {formatted_slot}. "
                    "Our scheduling system encountered a minor delay, but our team has been notified "
                    "and will contact you directly to confirm your slot!"
                )

            patient["booking"] = None
            patient["step"] = IDLE
            patient["intent"] = None
            patient.pop("candidate_date", None)
            return ChatResponse(reply=reply, booked=not booking_error)

    # Answer live clinic-data questions before the static FAQ fallback.
    live_answer = appointment_store.answer_reference_question(message)
    if live_answer is not None:
        return ChatResponse(reply=live_answer, booked=False)

    # Intent routing
    try:
        intent = llm.classify_intent(message)
    except Exception as e:
        logger.error("Intent classification failed: %s", e)
        intent = "unclear"

    if intent == "faq_question":
        try:
            answer = llm.answer_faq(message)
            if answer.strip() == "NOT_IN_FAQ":
                reply = "Let me connect you with our team on that — they will follow up shortly."
            else:
                reply = answer
        except Exception as e:
            logger.error("FAQ answering failed: %s", e)
            reply = "Let me connect you with our team on that — they will follow up shortly."
        return ChatResponse(reply=reply, booked=False)

    elif intent == "book_appointment":
        return ChatResponse(
            reply=_begin_booking(patient, f"web:{from_phone}"),
            booked=False,
        )

    else:
        reply = "Are you looking to book an appointment or do you have a question about our services?"
        return ChatResponse(reply=reply, booked=False)


@app.post("/chat", response_model=ChatResponse)
async def chat_endpoint(request: ChatRequest):
    """Web chat API endpoint for the web widget."""
    return await process_chat_message(
        message=request.message,
        patient_name=request.patient_name,
        phone=request.phone,
        session_id=request.session_id,
    )


# ============ Static File Routes ============

@app.get("/book", response_class=HTMLResponse)
async def book_page():
    """Direct booking page for Track B clinics."""
    return FileResponse("static/book.html")


@app.get("/demo", response_class=HTMLResponse)
async def demo_page():
    """Demo clinic website for Track A clinics."""
    return FileResponse("static/index.html")


@app.get("/privacy", response_class=HTMLResponse)
async def privacy_policy():
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Clinic Agent - Privacy Policy</title>
        <style>
            body { font-family: system-ui, -apple-system, sans-serif; line-height: 1.6; max-width: 760px; margin: 40px auto; padding: 0 20px; color: #1e293b; background: #f8fafc; }
            h1, h2 { color: #0f172a; }
            hr { border: 0; border-top: 1px solid #e2e8f0; margin: 24px 0; }
        </style>
    </head>
    <body>
        <h1>Privacy Policy</h1>
        <p><strong>Effective Date:</strong> October 2026</p>
        <p>This Clinic Appointment Booking Service facilitates automated appointment scheduling through our WhatsApp integration and web booking widgets.</p>
        <hr>
        <h2>1. Information We Collect</h2>
        <p>We only process data required to coordinate your clinic visit: patient name, contact number, requested appointment date/time, and basic consultation type.</p>
        <h2>2. How Data Is Used</h2>
        <p>Your details are strictly used to schedule calendar slots, generate clinic visit records, and deliver appointment confirmations or reminder messages. We do not sell, rent, or monetize personal information.</p>
        <h2>3. Data Retention & Deletion</h2>
        <p>Patients can request the immediate removal of their booking records and personal details at any time by contacting clinic administration or messaging our support line.</p>
    </body>
    </html>
    """


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
