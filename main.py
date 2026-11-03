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
AWAITING_CANCEL_CONFIRMATION = "cancel_confirmation"
AWAITING_CANCEL_SELECTION = "cancel_selection"
AWAITING_RESCHEDULE_DATETIME = "reschedule_datetime"
AWAITING_RESCHEDULE_SELECTION = "reschedule_selection"
MANAGING_APPOINTMENT = "managing_appointment"
SELECTING_APPOINTMENT_TO_MANAGE = "selecting_appointment_to_manage"
AWAITING_PEER_DOCTOR_CONFIRMATION = "peer_doctor_confirmation"
AWAITING_CONSENT = "consent"
_WEBHOOK_LOCKS: dict[str, asyncio.Lock] = {}
_PURGE_TASK: asyncio.Task | None = None


@app.on_event("startup")
async def validate_google_sheets_configuration() -> None:
    global _PURGE_TASK
    try:
        appointment_store._config()
    except RuntimeError as error:
        logger.warning("Google Sheets is not ready: %s", error)
    _PURGE_TASK = asyncio.create_task(_appointment_purge_loop())


@app.on_event("shutdown")
async def stop_appointment_purge_loop() -> None:
    global _PURGE_TASK
    if _PURGE_TASK is None:
        return
    _PURGE_TASK.cancel()
    try:
        await _PURGE_TASK
    except asyncio.CancelledError:
        pass
    finally:
        _PURGE_TASK = None


async def _appointment_purge_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(appointment_store.purge_expired_appointments)
        except Exception as error:
            logger.error("Appointment purge failed: %s", error)
        await asyncio.sleep(60)


def _send_whatsapp_reply(to_phone: str, text: str) -> Response:
    logger.info("Attempting outbound WhatsApp message to %s: %r", to_phone, text)
    if not whatsapp.send_whatsapp_message(to_phone, text):
        logger.error("Failed to send WhatsApp message to %s", to_phone)
    return Response(content="OK", status_code=200)


def _send_whatsapp_buttons(to_phone: str, text: str, buttons: list[dict]) -> Response:
    if not whatsapp.send_whatsapp_buttons(to_phone, text, buttons):
        logger.error("Failed to send WhatsApp interactive buttons to %s", to_phone)
    return Response(content="OK", status_code=200)


def _send_whatsapp_list(
    to_phone: str, text: str, label: str, section: str, rows: list[dict],
) -> Response:
    if not whatsapp.send_whatsapp_list(to_phone, text, label, section, rows):
        logger.error("Failed to send WhatsApp interactive list to %s", to_phone)
    return Response(content="OK", status_code=200)


def _send_followup(to_phone: str, body: str, buttons: list[dict]) -> Response:
    return _send_whatsapp_buttons(to_phone, body, buttons)


def _followup_buttons(*, include_manage: bool = True) -> list[dict]:
    buttons = [
        {"id": "MENU_BOOK", "title": "📅 Book Another"},
    ]
    if include_manage:
        buttons.append({"id": "MENU_MANAGE", "title": "📋 My Appointments"})
    buttons.append({"id": "MENU_DONE", "title": "✅ No, Thank You"})
    return buttons


def _pricing_followup(to_phone: str) -> Response:
    answer = appointment_store.answer_reference_question("services pricing hours")
    body = (answer or "I couldn't find the clinic's live pricing right now.") + (
        "\n\nIs there anything else I can help you with?"
    )
    return _send_followup(to_phone, body[:1024], _followup_buttons())


def _reception_menu(to_phone: str) -> Response:
    return _send_whatsapp_buttons(
        to_phone,
        "Welcome to Demo Clinic! How can we help you today?",
        [
            {"id": "MENU_BOOK", "title": "📅 Book Appointment"},
            {"id": "MENU_PRICING", "title": "💰 Services & Fees"},
            {"id": "MENU_MANAGE", "title": "📋 My Appointments"},
        ],
    )


def _is_greeting(message: str) -> bool:
    return message.strip().lower() in {"hi", "hello", "hey", "menu", "start"}


def _is_manage_appointments(message: str) -> bool:
    normalized = re.sub(r"\s+", " ", message.strip().lower())
    return normalized in {
        "my appointments",
        "check appointment",
        "check appointments",
        "view appointments",
        "upcoming appointments",
    }


def _service_menu(to_phone: str) -> Response:
    try:
        menu_rows = appointment_store.interactive_service_rows()
    except Exception as error:
        logger.error("Unable to load services for interactive menu: %s", error)
        return _send_whatsapp_reply(to_phone, "I couldn't load services right now. Please type the service you need.")
    if not menu_rows:
        return _send_whatsapp_reply(to_phone, "Please type the service or test you would like to book.")
    return _send_whatsapp_list(to_phone, "Please select a service:", "Select Service", "Services", menu_rows)


def _date_menu(
    to_phone: str, service_id: str, doctor_id: str | None = None,
) -> Response:
    rows = appointment_store.interactive_open_dates(
        service_id, datetime.datetime.now(IST), limit=7, doctor_id=doctor_id,
    )
    rows.append({
        "id": "DATE_CUSTOM",
        "title": "Pick Another Date",
        "description": "Type a date such as 15 Nov",
    })
    return _send_whatsapp_list(
        to_phone,
        "Tap a day below, or type any date (e.g., '15 Nov'):",
        "Select Date",
        "Available Dates",
        rows,
    )


def _appointment_doctor(appointment: dict) -> tuple[str, str]:
    name = appointment.get("doctor_name", "").strip()
    specialty = ""
    if name:
        doctor = appointment_store._lookup_doctor(name)
        specialty = (doctor or {}).get("specialty", "")
    return name, specialty


def _manage_appointment_prompt(to_phone: str, patient: dict, appointment: dict) -> Response:
    appointment_id = appointment["appointment_id"]
    patient["selected_appointment_id"] = appointment_id
    patient["step"] = MANAGING_APPOINTMENT
    doctor_name, specialty = _appointment_doctor(appointment)
    doctor_text = (
        f"{_doctor_display_name(doctor_name)} ({specialty})"
        if doctor_name else "the clinic"
    )
    return _send_whatsapp_buttons(
        to_phone,
        "📋 Your Upcoming Appointment:\n\n"
        f"🩺 Service: {appointment.get('service_name', '')}\n"
        f"👨‍⚕️ Doctor: {doctor_text}\n"
        f"📅 Date: {appointment.get('appointment_date', '')}\n"
        f"🕒 Time: {appointment.get('start_time', '')}\n"
        f"💰 Fee: ₹{appointment.get('fee_quoted', '')}\n\n"
        "What would you like to do with this booking?",
        [
            {"id": f"ACT_RESCHED_{appointment_id}", "title": "🔄 Reschedule"},
            {"id": f"ACT_CANCEL_{appointment_id}", "title": "❌ Cancel Booking"},
            {"id": "MENU_DONE", "title": "✅ Done / Thanks"},
        ],
    )


def _manage_appointments(to_phone: str, patient: dict) -> Response:
    appointments = appointment_store.upcoming(to_phone)
    if not appointments:
        patient["step"] = IDLE
        return _send_whatsapp_buttons(
            to_phone,
            "You don't have any upcoming appointments scheduled with Demo Clinic right now.",
            [
                {"id": "MENU_BOOK", "title": "📅 Book Appointment"},
                {"id": "MENU_PRICING", "title": "💰 Services & Fees"},
            ],
        )
    if len(appointments) == 1:
        return _manage_appointment_prompt(to_phone, patient, appointments[0])
    patient["step"] = SELECTING_APPOINTMENT_TO_MANAGE
    patient["appointment_ids"] = [row["appointment_id"] for row in appointments]
    return _send_whatsapp_list(
        to_phone,
        "📋 Select an upcoming appointment to manage:",
        "📋 Select Booking",
        "Upcoming Appointments",
        [
            {
                "id": f"SEL_APPT_{row['appointment_id']}",
                "title": row.get("service_name", "")[:24],
                "description": (
                    f"{row.get('appointment_date', '')} at {row.get('start_time', '')} "
                    f"• Dr. {row.get('doctor_name', '')}"
                )[:72],
            }
            for row in appointments
        ],
    )


def _time_menu(
    to_phone: str, service_id: str, date_text: str, doctor_id: str | None = None,
) -> Response:
    try:
        day = datetime.date.fromisoformat(date_text)
    except ValueError:
        return _send_whatsapp_reply(to_phone, "Please type a valid date.")
    rows = appointment_store.interactive_open_slots(
        service_id, day, doctor_id=doctor_id, limit=10,
    )
    if not rows:
        return _send_whatsapp_reply(to_phone, "There are no open slots on that date. Please choose another date.")
    return _send_whatsapp_list(
        to_phone,
        "Choose an available time slot:",
        "Choose Time",
        "Open Slots",
        rows,
    )


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


def _is_consent_yes(message: str) -> bool:
    return message.strip().lower() in {"yes", "y", "agree", "ok", "sure"}


def _is_consent_no(message: str) -> bool:
    return message.strip().lower() in {"no", "stop", "cancel"}


def _is_trivial_deferred_text(message: str | None) -> bool:
    return not message or message.strip().lower() in {"hi", "hello", "hey", "yes", "start"}


def _hydrate_booking_from_message(patient: dict, phone: str, message: str) -> None:
    extracted = llm.extract_booking_slots(message)
    booking = patient.get("booking")
    if not isinstance(booking, dict):
        booking = {
            "phone": phone,
            "name": None,
            "service": None,
            "slot": None,
        }
        patient["booking"] = booking
    booking["phone"] = phone
    booking["name"] = extracted.get("name") or booking.get("name")
    booking["service"] = extracted.get("service") or booking.get("service")
    booking["slot"] = extracted.get("slot") or booking.get("slot")
    doctor = extracted.get("doctor")
    if doctor:
        booking["doctor_text"] = doctor
        resolved_doctor = appointment_store._lookup_doctor(doctor)
        if resolved_doctor:
            booking["doctor_id"] = resolved_doctor["doctor_id"]
    if booking.get("name") and booking.get("service") and booking.get("slot"):
        patient["step"] = AWAITING_DATETIME
    elif not booking.get("name"):
        patient["step"] = AWAITING_NAME
    elif not booking.get("service"):
        patient["step"] = AWAITING_SERVICE
    else:
        patient["step"] = AWAITING_DATETIME


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
    return message.strip().lower() in {"cancel", "cancel booking", "stop", "never mind", "reset"}


def _is_confirmed_cancellation(message: str) -> bool:
    normalized = message.strip().lower()
    return "cancel" in normalized and ("appointment" in normalized or "booking" in normalized)


def _is_reschedule(message: str) -> bool:
    normalized = message.strip().lower()
    return any(term in normalized for term in ("reschedule", "change my appointment", "move my appointment"))


def _appointment_summary(appointment: dict) -> str:
    doctor = appointment.get("doctor_name")
    doctor_text = f" with {_doctor_display_name(doctor)}" if doctor else ""
    return (
        f"{appointment['service_name']}{doctor_text} on {appointment['appointment_date']} "
        f"at {appointment['start_time']} (Fee: ₹{appointment['fee_quoted']})"
    )


def _cancel_booking(patient: dict) -> str:
    patient["booking"] = None
    patient["step"] = IDLE
    patient["intent"] = None
    return "Your appointment booking has been cancelled. You can start again whenever you are ready."


def _reset_appointment_action(patient: dict) -> None:
    patient["booking"] = None
    patient.pop("draft_booking", None)
    patient["step"] = IDLE
    patient["intent"] = None
    patient.pop("appointment_ids", None)
    patient.pop("target_appointment_id", None)
    patient.pop("candidate_date", None)
    patient.pop("pending_failover", None)


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


def _doctor_display_name(name: str) -> str:
    return f"Dr. {re.sub(r'^dr\.?\s*', '', name.strip(), flags=re.IGNORECASE)}"


def _failover_prompt(patient: dict, slot_dt: datetime.datetime, availability: dict) -> str | None:
    proposal = availability.get("failover")
    if not proposal:
        return None
    original = proposal["original_doctor"]
    peer = proposal["peer_doctor"]
    patient["pending_failover"] = {
        "original_doctor_id": original["doctor_id"],
        "original_doctor_name": original["name"],
        "peer_doctor_id": peer["doctor_id"],
        "peer_doctor_name": peer["name"],
        "specialty": peer.get("specialty", ""),
        "fee_quoted": proposal["peer_doctor"].get("consultation_fee", ""),
        "date": slot_dt.date().isoformat(),
        "start_time": slot_dt.strftime("%H:%M"),
        "end_time": proposal["peer_end"].strftime("%H:%M"),
    }
    patient["step"] = AWAITING_PEER_DOCTOR_CONFIRMATION
    return (
        f"{_doctor_display_name(original['name'])} is booked at {slot_dt.strftime('%I:%M %p')}, "
        f"but {_doctor_display_name(peer['name'])} ({peer.get('specialty', 'specialist')}) "
        f"is available at {slot_dt.strftime('%I:%M %p')} "
        f"(Fee: ₹{proposal['peer_doctor'].get('consultation_fee', 'not listed')}). "
        f"Would you like to book with {_doctor_display_name(peer['name'])}, "
        "or see alternative times for the original doctor?"
    )


def _alternative_slots(service: str, slot_dt: datetime.datetime, doctor_id: str | None) -> list[str]:
    result = []
    duration = appointment_store.check_slot_availability(
        service, slot_dt, doctor_id=doctor_id
    ).get("duration_mins", 15)
    candidate = slot_dt + datetime.timedelta(minutes=duration)
    for _ in range(12):
        availability = appointment_store.check_slot_availability(service, candidate, doctor_id=doctor_id)
        if availability["available"]:
            result.append(_format_slot(candidate))
            if len(result) == 3:
                break
        candidate += datetime.timedelta(minutes=duration)
    return result


def _finish_booking(patient: dict, from_phone: str, slot_dt: datetime.datetime,
                    doctor_id: str | None = None, web: bool = False):
    name = patient["booking"]["name"]
    service = patient["booking"]["service"]
    phone = from_phone.replace("whatsapp:", "").strip()
    availability = appointment_store.check_slot_availability(
        service, slot_dt, doctor_id=doctor_id
    )
    if not availability["available"]:
        response = (
            f"{availability['reason']} Please provide another time."
            if not availability.get("failover")
            else "That doctor is no longer available at this time. Please choose another slot."
        )
        return ChatResponse(reply=response, booked=False) if web else _send_whatsapp_reply(from_phone, response)
    calendar_event = None
    booking_error = False
    try:
        calendar_event = calendar_service.create_event(
            summary=f"{service} - {name}",
            start=slot_dt,
            duration_minutes=availability["duration_mins"],
        )
        appointment_store.create_appointment(
            phone=phone,
            patient_name=name,
            service_name=service,
            requested_dt=slot_dt,
            calendar_event_id=(calendar_event or {}).get("id"),
            doctor_id=doctor_id,
        )
    except Exception as error:
        logger.error("Booking commit failed: %s", error)
        booking_error = True
    formatted_slot = _format_slot(slot_dt)
    try:
        whatsapp.send_owner_alert(
            patient_name=name, service=service, slot=formatted_slot, exclude_phone=from_phone
        )
    except Exception as error:
        logger.error("Owner alert WhatsApp message failed: %s", error)
    doctor = availability.get("doctor")
    reply = (
        "✅ Appointment Confirmed!\n\n"
        f"👤 Patient: {name}\n"
        f"🩺 Service: {service}\n"
        f"👨‍⚕️ Doctor: {_doctor_display_name(doctor['name']) if doctor else 'the clinic'}\n"
        f"📅 Date: {slot_dt.date().isoformat()} at {slot_dt.strftime('%I:%M %p')}\n"
        f"💰 Consultation Fee: ₹{availability['fee']}\n\n"
        "Is there anything else I can help you with today?"
        if not booking_error else
        f"Thank you, {name}. We received your booking request for {service} on {formatted_slot}, "
        "but our scheduling system encountered a minor delay."
    )
    _reset_appointment_action(patient)
    if web:
        return ChatResponse(reply=reply, booked=not booking_error)
    if booking_error:
        return _send_whatsapp_reply(from_phone, reply)
    return _send_followup(from_phone, reply, _followup_buttons())


def _peer_acceptance(message: str, peer_name: str) -> bool:
    normalized = message.strip().lower()
    return normalized in {"yes", "y", "ok", "okay", "sure", "book with doctor"} or (
        "book" in normalized and appointment_store.normalize_doc_name(peer_name) in
        appointment_store.normalize_doc_name(normalized)
    )


def _peer_decline(message: str) -> bool:
    normalized = message.strip().lower()
    return normalized in {"no", "n", "alternative", "alternative times", "other times"} or "original doctor" in normalized


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
    interactive_id = parsed.get("interactive_id")

    if not from_phone or not body:
        return Response(content="OK", status_code=200)

    lock = _WEBHOOK_LOCKS.setdefault(from_phone, asyncio.Lock())
    async with lock:
        if not state.mark_message_processed(from_phone, message_id):
            logger.info("Ignoring duplicate WhatsApp message %s from %s", message_id, from_phone)
            return Response(content="OK", status_code=200)
        return await _process_whatsapp_message(from_phone, body, interactive_id)


async def _process_whatsapp_message(
    from_phone: str, body: str, interactive_id: str | None = None,
) -> Response:
    """Process one unique inbound WhatsApp message."""
    patient = state.get(from_phone)
    if interactive_id == "CONSENT_YES":
        body = "yes"
    elif interactive_id == "CONSENT_NO":
        body = "no"

    # 1. Consent gate with deferred replay.
    if not patient["consent"] and patient.get("step") == AWAITING_CONSENT:
        if _is_consent_yes(body):
            patient["consent"] = True
            patient["step"] = IDLE
            deferred_text = patient.pop("deferred_intent_text", None)
            if not _is_trivial_deferred_text(deferred_text):
                return await _process_whatsapp_message(from_phone, deferred_text)
            return _reception_menu(from_phone)
        if _is_consent_no(body):
            patient.pop("deferred_intent_text", None)
            patient["step"] = IDLE
            return _send_whatsapp_reply(
                from_phone,
                "Understood. We cannot process appointment bookings without consent. "
                "Feel free to message us anytime if you change your mind.",
            )
        return _send_whatsapp_reply(
            from_phone,
            "Please reply YES to agree to share your details for appointment purposes, or NO to cancel.",
        )

    if not patient["consent"]:
        if appointment_store.has_appointment_history(from_phone):
            patient["consent"] = True
        else:
            patient["deferred_intent_text"] = body.strip()
            patient["step"] = AWAITING_CONSENT
            return _send_whatsapp_buttons(
                from_phone,
                "Hi! Before we proceed, please confirm you agree to share your details with Demo Clinic for appointment purposes.",
                [
                    {"id": "CONSENT_YES", "title": "I Agree"},
                    {"id": "CONSENT_NO", "title": "Cancel"},
                ],
            )

    # 2. In-progress booking flow
    step = patient.get("step")
    if interactive_id == "FAILOVER_ACCEPT":
        body = "yes"
    elif interactive_id == "FAILOVER_ALT_TIMES":
        body = "original doctor times"
    elif interactive_id == "GLOBAL_CANCEL":
        body = "cancel"
    if interactive_id == "MENU_BOOK":
        patient["booking"] = {
            "phone": from_phone,
            "name": None,
            "service": None,
            "slot": None,
            "guided": True,
        }
        patient["draft_booking"] = patient["booking"]
        patient["intent"] = "book_appointment"
        patient["step"] = AWAITING_SERVICE
        return _service_menu(from_phone)
    if interactive_id == "MENU_PRICING":
        return _pricing_followup(from_phone)
    if interactive_id == "MENU_MANAGE":
        return _manage_appointments(from_phone, patient)
    if interactive_id == "MENU_DONE":
        _reset_appointment_action(patient)
        return _send_whatsapp_reply(
            from_phone,
            "Thank you for choosing Demo Clinic! Whenever you need us again, "
            "just send a 'Hi'. Have a great day! 👋",
        )
    if interactive_id == "MENU_MAIN":
        _reset_appointment_action(patient)
        return _reception_menu(from_phone)
    if interactive_id and interactive_id.startswith("SEL_APPT_"):
        appointment = appointment_store.get_appointment(interactive_id[9:])
        if not appointment:
            return _manage_appointments(from_phone, patient)
        return _manage_appointment_prompt(from_phone, patient, appointment)
    if interactive_id and (
        interactive_id.startswith("ACT_CANCEL_")
        or interactive_id.startswith("ACT_RESCHED_")
    ):
        appointment_id = interactive_id.split("_", 2)[2]
        appointment = appointment_store.get_appointment(appointment_id)
        if not appointment:
            return _manage_appointments(from_phone, patient)
        patient["selected_appointment_id"] = appointment_id
        if interactive_id.startswith("ACT_CANCEL_"):
            patient["step"] = AWAITING_CANCEL_CONFIRMATION
            doctor_name, _ = _appointment_doctor(appointment)
            return _send_whatsapp_buttons(
                from_phone,
                f"Are you sure you want to cancel your {appointment.get('service_name', '')} "
                f"with {_doctor_display_name(doctor_name) if doctor_name else 'the clinic'} "
                f"on {appointment.get('appointment_date', '')} at {appointment.get('start_time', '')}?",
                [
                    {"id": f"CONFIRM_CANCEL_{appointment_id}", "title": "✅ Yes, Cancel It"},
                    {"id": "MENU_MAIN", "title": "🔙 No, Keep It"},
                ],
            )
        service = appointment_store.interactive_service_for_name(appointment["service_name"])
        if not service:
            return _send_whatsapp_reply(from_phone, "That service is no longer available for rescheduling.")
        patient["booking"] = {
            "phone": from_phone,
            "name": appointment.get("patient_name", ""),
            "service": appointment["service_name"],
            "service_id": service.get("service_id", ""),
            "doctor_id": appointment.get("doctor_id", ""),
            "doctor_name": appointment.get("doctor_name", ""),
            "price_inr": appointment.get("fee_quoted", ""),
            "default_doctor_id": service.get("default_doctor_id", ""),
            "rescheduling_from_appt_id": appointment_id,
            "guided": True,
        }
        patient["draft_booking"] = patient["booking"]
        patient["step"] = AWAITING_DATETIME
        return _date_menu(
            from_phone, str(service.get("service_id", "")),
            patient["booking"].get("doctor_id"),
        )
    if interactive_id and interactive_id.startswith("CONFIRM_CANCEL_"):
        appointment_id = interactive_id[len("CONFIRM_CANCEL_"):]
        appointment = appointment_store.get_appointment(appointment_id)
        if not appointment:
            return _manage_appointments(from_phone, patient)
        try:
            if appointment.get("calendar_event_id"):
                calendar_service.delete_event(appointment["calendar_event_id"])
            appointment_store.delete_appointment(appointment_id)
        except Exception as error:
            logger.error("Appointment cancellation failed: %s", error)
            return _send_whatsapp_reply(from_phone, "I couldn't cancel that appointment right now. Please try again.")
        _reset_appointment_action(patient)
        return _send_followup(
            from_phone,
            f"✅ Your appointment for {appointment.get('service_name', '')} on "
            f"{appointment.get('appointment_date', '')} at {appointment.get('start_time', '')} "
            "has been cancelled.\n\n"
            "Is there anything else I can help you with?",
            [
                {"id": "MENU_BOOK", "title": "📅 Book Appointment"},
                {"id": "MENU_MANAGE", "title": "📋 My Appointments"},
                {"id": "MENU_DONE", "title": "✅ No, Thank You"},
            ],
        )
    if interactive_id and interactive_id.startswith("SVC_") and step == AWAITING_SERVICE:
        service_id = interactive_id[4:]
        service = appointment_store.interactive_service(service_id)
        if service:
            booking = patient.setdefault("booking", {})
            booking.update({
                "service_id": service_id,
                "service": service.get("service_name", body),
                "price_inr": service.get("price_inr", service.get("price", "")),
                "duration_mins": service.get("duration_mins", "15"),
                "default_doctor_id": service.get("default_doctor_id", ""),
            })
            patient["draft_booking"] = booking
            patient["step"] = AWAITING_DATETIME
            return _date_menu(from_phone, service_id)
    if interactive_id and interactive_id.startswith("DATE_") and step == AWAITING_DATETIME:
        if interactive_id == "DATE_CUSTOM":
            return _send_whatsapp_reply(from_phone, "Please type your preferred date, for example 15 Nov.")
        date_text = interactive_id[5:]
        patient["candidate_date"] = date_text
        booking = patient.get("booking") or {}
        service_id = booking.get("service_id")
        if service_id:
            return _time_menu(
                from_phone, service_id, date_text, booking.get("doctor_id"),
            )
        return _send_whatsapp_reply(from_phone, "Please select a service first.")
    if interactive_id and interactive_id.startswith("TIME_") and step == AWAITING_DATETIME:
        booking = patient.get("booking") or {}
        service_id = booking.get("service_id")
        date_text = patient.get("candidate_date")
        if not service_id or not date_text:
            return _send_whatsapp_reply(from_phone, "Please select a service and date first.")
        slot = next(
            (row for row in appointment_store.interactive_open_slots(
                service_id, datetime.date.fromisoformat(date_text),
                doctor_id=booking.get("doctor_id"), limit=10,
            ) if row["id"] == interactive_id),
            None,
        )
        if not slot:
            return _send_whatsapp_reply(from_phone, "That slot is no longer available. Please choose another time.")
        slot_dt = slot["datetime"].replace(tzinfo=IST)
        booking["slot_dt"] = slot_dt.isoformat()
        booking["slot"] = _format_slot(slot_dt)
        booking["doctor_id"] = slot.get("doctor_id") or booking.get("default_doctor_id")
        if booking.get("rescheduling_from_appt_id"):
            appointment_id = booking["rescheduling_from_appt_id"]
            appointment = appointment_store.get_appointment(appointment_id)
            if not appointment:
                _reset_appointment_action(patient)
                return _send_whatsapp_reply(from_phone, "That appointment is no longer available to reschedule.")
            try:
                service_row = appointment_store.interactive_service(booking["service_id"]) or {}
                doctor_row = appointment_store._lookup_doctor(booking.get("doctor_name", ""))
                duration = appointment_store._doctor_duration(service_row, doctor_row)
                if appointment.get("calendar_event_id"):
                    calendar_service.update_event(
                        appointment["calendar_event_id"],
                        slot_dt,
                        duration,
                    )
                updated = appointment_store.reschedule(appointment_id, slot_dt)["appointment"]
            except Exception as error:
                logger.error("Interactive reschedule failed: %s", error)
                return _send_whatsapp_reply(from_phone, "That slot is no longer available. Please choose another time.")
            _reset_appointment_action(patient)
            return _send_followup(
                from_phone,
                f"✅ Appointment Updated!\n\n"
                f"🩺 Service: {updated['service_name']}\n"
                f"📅 Date: {updated['appointment_date']} at {updated['start_time']}\n"
                f"💰 Consultation Fee: ₹{updated['fee_quoted']}\n\n"
                "Is there anything else I can help you with today?",
                _followup_buttons(),
            )
        patient["step"] = AWAITING_NAME
        return _send_whatsapp_reply(
            from_phone,
            "Almost done! Please reply with the Patient's Full Name to confirm your booking.",
        )
    if step == AWAITING_PEER_DOCTOR_CONFIRMATION:
        if _is_cancellation(body):
            _reset_appointment_action(patient)
            return _send_whatsapp_reply(from_phone, "Your booking has been cancelled.")
        pending = patient.get("pending_failover")
        if not pending or not patient.get("booking"):
            _reset_appointment_action(patient)
            return _send_whatsapp_reply(from_phone, "That booking proposal has expired. Please start again.")
        if _peer_acceptance(body, pending["peer_doctor_name"]):
            slot_dt = datetime.datetime.fromisoformat(
                f"{pending['date']}T{pending['start_time']}"
            ).replace(tzinfo=IST)
            patient["booking"]["doctor_id"] = pending["peer_doctor_id"]
            return _finish_booking(patient, from_phone, slot_dt)
        if _peer_decline(body):
            patient["candidate_date"] = pending["date"]
            patient["step"] = AWAITING_DATETIME
            patient.pop("pending_failover", None)
            slots = _alternative_slots(
                patient["booking"]["service"],
                datetime.datetime.fromisoformat(f"{pending['date']}T{pending['start_time']}").replace(tzinfo=IST),
                pending["original_doctor_id"],
            )
            return _send_whatsapp_reply(
                from_phone,
                "Understood. Available alternatives with the original doctor: "
                + (", ".join(slots) if slots else "no nearby slots found")
                + ". Please choose a time.",
            )
        try:
            patient["candidate_date"] = pending["date"]
            slot_dt = _parse_slot_with_session_date(body.strip(), patient)
        except SlotResolutionError:
            return _send_whatsapp_reply(from_phone, "Please reply YES for the available doctor, NO for alternatives, or provide another time.")
        patient["booking"]["doctor_id"] = pending["original_doctor_id"]
        patient["step"] = AWAITING_DATETIME
        patient.pop("pending_failover", None)
        availability = appointment_store.check_slot_availability(
            patient["booking"]["service"], slot_dt, doctor_id=pending["original_doctor_id"]
        )
        if not availability["available"]:
            _remember_candidate_date(patient, slot_dt)
            return _send_whatsapp_reply(
                from_phone,
                f"{availability['reason']} "
                f"Available alternatives: {', '.join(_alternative_slots(patient['booking']['service'], slot_dt, pending['original_doctor_id']))}.",
            )
        return _finish_booking(patient, from_phone, slot_dt, pending["original_doctor_id"])
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
                return _send_followup(
                    from_phone,
                    f"Your appointment for {appointment['service_name']} on "
                    f"{appointment['appointment_date']} at {appointment['start_time']} "
                    f"with {_doctor_display_name(appointment['doctor_name']) if appointment.get('doctor_name') else 'the clinic'} "
                    "has been cancelled.\n\n"
                    "Is there anything else I can help you with?",
                    [
                        {"id": "MENU_BOOK", "title": "📅 Book Appointment"},
                        {"id": "MENU_MANAGE", "title": "📋 My Appointments"},
                        {"id": "MENU_DONE", "title": "✅ No, Thank You"},
                    ],
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
            return _send_followup(
                from_phone,
                f"✅ Appointment Updated!\n\n"
                f"🩺 Service: {appointment['service_name']}\n"
                f"👨‍⚕️ Doctor: {_doctor_display_name(appointment['doctor_name']) if appointment.get('doctor_name') else 'Not required'}\n"
                f"📅 Date: {appointment['appointment_date']} at {appointment['start_time']}\n"
                f"💰 Consultation Fee: ₹{appointment['fee_quoted']}\n\n"
                "Is there anything else I can help you with today?",
                _followup_buttons(),
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
    if not step and (_is_greeting(body) or _is_booking_trigger(body)):
        if _is_booking_trigger(body):
            patient["booking"] = {
                "phone": from_phone,
                "name": None,
                "service": None,
                "slot": None,
                "guided": True,
            }
            patient["draft_booking"] = patient["booking"]
            patient["intent"] = "book_appointment"
            patient["step"] = AWAITING_SERVICE
            return _service_menu(from_phone)
        return _reception_menu(from_phone)
    if not step and _is_manage_appointments(body):
        return _manage_appointments(from_phone, patient)
    if step:
        if step == AWAITING_NAME:
            if _is_booking_trigger(body):
                return _send_whatsapp_reply(
                    from_phone,
                    "Please provide your full name so I can continue the booking.",
                )
            patient["booking"]["name"] = body.strip()
            if patient["booking"].get("guided") and patient["booking"].get("slot_dt"):
                slot_dt = datetime.datetime.fromisoformat(patient["booking"]["slot_dt"])
                return _finish_booking(
                    patient, from_phone, slot_dt,
                    patient["booking"].get("doctor_id"),
                )
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
            service = appointment_store.interactive_service_for_name(body)
            if service:
                patient["booking"].update({
                    "service_id": service.get("service_id", ""),
                    "price_inr": service.get("price_inr", service.get("price", "")),
                    "duration_mins": service.get("duration_mins", "15"),
                    "default_doctor_id": service.get("default_doctor_id", ""),
                })
                return _date_menu(from_phone, str(service.get("service_id", "")))
            return _send_whatsapp_reply(from_phone, "That service is not available in the clinic's live list. Please select a service.")

        elif step == AWAITING_DATETIME:
            if (
                patient["booking"].get("service_id")
                and not re.search(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b", body, re.IGNORECASE)
            ):
                try:
                    date_candidate = _parse_slot_with_session_date(body.strip(), patient).date()
                    patient["candidate_date"] = date_candidate.isoformat()
                    return _time_menu(
                        from_phone,
                        patient["booking"]["service_id"],
                        patient["candidate_date"],
                    )
                except SlotResolutionError:
                    pass
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

            availability = appointment_store.check_slot_availability(
                service, slot_dt, doctor_id=patient["booking"].get("doctor_id")
            )
            if not availability["available"]:
                failover_reply = _failover_prompt(patient, slot_dt, availability)
                if failover_reply:
                    return _send_whatsapp_buttons(
                        from_phone,
                        failover_reply,
                        [
                            {"id": "FAILOVER_ACCEPT", "title": "Book Peer Doctor"},
                            {"id": "FAILOVER_ALT_TIMES", "title": "Original Doctor Times"},
                            {"id": "GLOBAL_CANCEL", "title": "Cancel"},
                        ],
                    )
                _remember_candidate_date(patient, slot_dt)
                suggestions = (
                    f" Next available slots: {', '.join(availability['suggestions'])}."
                    if availability["suggestions"] else ""
                )
                return _send_whatsapp_reply(
                    from_phone,
                    f"Sorry, {availability['reason']}{suggestions} Please provide another time.",
                )
            assigned_doctor = availability.get("doctor")
            patient["booking"]["doctor_id"] = assigned_doctor.get("doctor_id") if assigned_doctor else None

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
                        doctor_id=availability.get("doctor", {}).get("doctor_id") if availability.get("doctor") else None,
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

            if not booking_error:
                doctor_text = (
                    _doctor_display_name(assigned_doctor["name"])
                    if assigned_doctor else "the clinic"
                )
                reply = (
                    "✅ Appointment Confirmed!\n\n"
                    f"👤 Patient: {name}\n"
                    f"🩺 Service: {service}\n"
                    f"👨‍⚕️ Doctor: {doctor_text}\n"
                    f"📅 Date: {slot_dt.date().isoformat()} at {slot_dt.strftime('%I:%M %p')}\n"
                    f"💰 Consultation Fee: ₹{availability['fee']}\n\n"
                    "Is there anything else I can help you with today?"
                )
            else:
                reply = (
                    f"Thank you, {name}. We received your booking request for {service} on {formatted_slot}. "
                    "Our scheduling system encountered a minor delay, but our team has been notified "
                    "and will contact you directly to confirm your slot!"
                )

            _reset_appointment_action(patient)
            patient.pop("candidate_date", None)
            if booking_error:
                return _send_whatsapp_reply(from_phone, reply)
            return _send_followup(from_phone, reply, _followup_buttons())

    # 3. Answer live clinic-data questions before the static FAQ fallback.
    live_answer = appointment_store.answer_reference_question(body)
    if live_answer is not None:
        if not step and any(term in body.lower() for term in ("price", "pricing", "fee", "service", "hours", "open", "close")):
            return _send_followup(
                from_phone,
                live_answer + "\n\nIs there anything else I can help you with?",
                _followup_buttons(),
            )
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
        _hydrate_booking_from_message(patient, from_phone, body)
        if (
            not patient["booking"].get("name")
            and not patient["booking"].get("service")
            and not patient["booking"].get("slot")
        ):
            patient["booking"]["guided"] = True
            patient["draft_booking"] = patient["booking"]
            patient["step"] = AWAITING_SERVICE
            return _service_menu(from_phone)
        if patient["step"] == AWAITING_DATETIME and patient["booking"].get("slot"):
            return await _process_whatsapp_message(from_phone, body)
        if patient["step"] == AWAITING_NAME:
            return _send_whatsapp_reply(from_phone, "I would be glad to help you book an appointment! First, could you please provide your full name?")
        if patient["step"] == AWAITING_SERVICE:
            return _service_menu(from_phone)
        if patient["step"] == AWAITING_DATETIME and patient["booking"].get("service"):
            service = appointment_store.interactive_service_for_name(
                patient["booking"]["service"],
            )
            if service:
                patient["booking"].update({
                    "service_id": service.get("service_id", ""),
                    "price_inr": service.get("price_inr", service.get("price", "")),
                    "duration_mins": service.get("duration_mins", "15"),
                    "default_doctor_id": service.get("default_doctor_id", ""),
                })
                return _date_menu(from_phone, str(service.get("service_id", "")))
        return _send_whatsapp_reply(from_phone, "What date and time would you prefer for your appointment?")

    return _reception_menu(from_phone)


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
    if step == AWAITING_PEER_DOCTOR_CONFIRMATION:
        if _is_cancellation(message):
            _reset_appointment_action(patient)
            return ChatResponse(reply="Your booking has been cancelled.", booked=False)
        pending = patient.get("pending_failover")
        if not pending or not patient.get("booking"):
            _reset_appointment_action(patient)
            return ChatResponse(reply="That booking proposal has expired. Please start again.", booked=False)
        if _peer_acceptance(message, pending["peer_doctor_name"]):
            slot_dt = datetime.datetime.fromisoformat(
                f"{pending['date']}T{pending['start_time']}"
            ).replace(tzinfo=IST)
            patient["booking"]["doctor_id"] = pending["peer_doctor_id"]
            return _finish_booking(patient, from_phone, slot_dt, pending["peer_doctor_id"], web=True)
        if _peer_decline(message):
            patient["candidate_date"] = pending["date"]
            patient["step"] = AWAITING_DATETIME
            patient.pop("pending_failover", None)
            slots = _alternative_slots(
                patient["booking"]["service"],
                datetime.datetime.fromisoformat(f"{pending['date']}T{pending['start_time']}").replace(tzinfo=IST),
                pending["original_doctor_id"],
            )
            return ChatResponse(
                reply="Understood. Available alternatives with the original doctor: "
                + (", ".join(slots) if slots else "no nearby slots found")
                + ". Please choose a time.",
                booked=False,
            )
        try:
            patient["candidate_date"] = pending["date"]
            slot_dt = _parse_slot_with_session_date(message.strip(), patient)
        except SlotResolutionError:
            return ChatResponse(
                reply="Please reply YES for the available doctor, NO for alternatives, or provide another time.",
                booked=False,
            )
        patient["booking"]["doctor_id"] = pending["original_doctor_id"]
        patient["step"] = AWAITING_DATETIME
        patient.pop("pending_failover", None)
        availability = appointment_store.check_slot_availability(
            patient["booking"]["service"], slot_dt, doctor_id=pending["original_doctor_id"]
        )
        if not availability["available"]:
            _remember_candidate_date(patient, slot_dt)
            return ChatResponse(
                reply=f"{availability['reason']} Available alternatives: "
                f"{', '.join(_alternative_slots(patient['booking']['service'], slot_dt, pending['original_doctor_id']))}.",
                booked=False,
            )
        return _finish_booking(patient, from_phone, slot_dt, pending["original_doctor_id"], web=True)
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
                    f"{appointment['appointment_date']} at {appointment['start_time']} "
                    f"with {_doctor_display_name(appointment['doctor_name']) if appointment.get('doctor_name') else 'the clinic'} "
                    "has been cancelled.",
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
                f"Doctor: {_doctor_display_name(updated['doctor_name']) if updated.get('doctor_name') else 'Not required'}. "
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

            availability = appointment_store.check_slot_availability(
                service, slot_dt, doctor_id=patient["booking"].get("doctor_id")
            )
            if not availability["available"]:
                failover_reply = _failover_prompt(patient, slot_dt, availability)
                if failover_reply:
                    return ChatResponse(reply=failover_reply, booked=False)
                _remember_candidate_date(patient, slot_dt)
                suggestions = ", ".join(availability["suggestions"])
                return ChatResponse(
                    reply=f"{availability['reason']} "
                    f"{'Available alternatives: ' + suggestions + '. ' if suggestions else ''}"
                    "Please provide another time.",
                    booked=False,
                )
            assigned_doctor = availability.get("doctor")
            patient["booking"]["doctor_id"] = assigned_doctor.get("doctor_id") if assigned_doctor else None

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
                        doctor_id=assigned_doctor.get("doctor_id") if assigned_doctor else None,
                    )
                except Exception as e:
                    logger.error("Appointment ledger failed: %s", e)
                    booking_error = True

            # Reply to patient
            if not booking_error:
                doctor_line = (
                    f"Doctor: {_doctor_display_name(assigned_doctor['name'])} "
                    f"({assigned_doctor.get('specialty', 'specialist')})"
                    if assigned_doctor else "Clinician: Not required for this service"
                )
                reply = (
                    "✅ Appointment Confirmed!\n"
                    f"Patient: {name}\n"
                    f"{doctor_line}\n"
                    f"Service: {service}\n"
                    f"Slot: {formatted_slot}\n"
                    f"Fee: ₹{availability['fee']}\n\n"
                    "Please arrive 10 minutes prior to your appointment. "
                    "We look forward to seeing you at Demo Clinic!"
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
        reply = "Please choose an option from the menu, or type your question."
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
