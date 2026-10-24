"""
Demo Clinic WhatsApp Patient Intake & Appointment Agent.

FastAPI entrypoint and Twilio WhatsApp webhook handler.
"""

from dotenv import load_dotenv
load_dotenv()

import datetime
import logging
import os
import pathlib

from fastapi import FastAPI, Request, Response, Body, Depends
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


def _twiml(message: str) -> Response:
    """Return a TwiML XML response containing the given message."""
    # Escape XML special characters
    escaped = (
        message.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )
    xml = f"<Response><Message>{escaped}</Message></Response>"
    return Response(content=xml, media_type="application/xml")

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


def _format_slot(slot_dt: datetime.datetime) -> str:
    return slot_dt.strftime("%A, %B %d, %Y at %I:%M %p")


def _format_slot_for_sheet(slot_dt: datetime.datetime) -> str:
    return slot_dt.strftime("%d-%m-%y %I:%M %p")


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
                                calendar_service, sheets_service, whatsapp, logger, _twiml):
    """Complete the booking when all slots are filled."""
    name = patient["booking"]["name"]
    service = patient["booking"]["service"]
    slot_str = patient["booking"]["slot"]
    phone = from_phone.replace("whatsapp:", "").strip()

    try:
        slot_dt = _parse_slot_datetime(slot_str)
    except SlotResolutionError:
        patient["step"] = "slot"
        return _twiml(
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

    # Append to Sheets
    try:
        sheets_service.append_row([
            now_iso,
            name,
            phone,
            service,
            formatted_slot,
            "confirmed" if not booking_error else "pending_manual_review",
        ])
    except Exception as e:
        logger.error("Sheets API failed: %s", e)
        booking_error = True

    # Alert clinic owner (fire and forget - still async)
    try:
        whatsapp.send_owner_alert(patient_name=name, service=service, slot=formatted_slot)
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
    patient["step"] = None
    patient["intent"] = None
    return _twiml(reply)


@app.get("/")
async def health_check():
    return {"status": "ok", "clinic": "Demo Clinic"}


@app.post("/webhook/whatsapp")
async def whatsapp_webhook(request: Request):
    """
    Twilio WhatsApp incoming message webhook handler.
    Returns TwiML with the agent's reply inline (synchronous).
    """
    form_data = await request.form()
    parsed = whatsapp.parse_incoming(dict(form_data))
    body = parsed["body"]
    from_phone = parsed["from_phone"]

    if not from_phone:
        return _twiml("")

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
        return _twiml(reply)

    # 2. In-progress booking flow
    step = patient.get("step")
    if step:
        if step == "name":
            patient["booking"]["name"] = body.strip()
            patient["step"] = "service"
            pname = patient["booking"]["name"]
            reply = (
                f"Thanks, {pname}! What service or test would you like to book? "
                "(e.g., General Consultation, Complete Blood Count, Abdominal Ultrasound)"
            )
            return _twiml(reply)

        elif step == "service":
            patient["booking"]["service"] = body.strip()
            patient["step"] = "slot"
            reply = (
                "Got it! What date and time would you prefer for your appointment? "
                "(e.g., Tomorrow at 10 AM, 2026-10-20 11:00 AM)"
            )
            return _twiml(reply)

        elif step == "slot":
            try:
                slot_dt = _parse_slot_datetime(body.strip())
            except SlotResolutionError:
                return _twiml(
                    "I couldn't resolve that date and time. "
                    "Please provide it again, for example: tomorrow at 10 AM."
                )
            formatted_slot = _format_slot(slot_dt)
            patient["booking"]["slot"] = formatted_slot
            name = patient["booking"]["name"]
            service = patient["booking"]["service"]
            phone = from_phone.replace("whatsapp:", "").strip()

            now_iso = datetime.datetime.now(IST).isoformat()

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

            # Append to Sheets
            try:
                sheets_service.append_row([
                    now_iso,
                    name,
                    phone,
                    service,
                    formatted_slot,
                    "confirmed" if not booking_error else "pending_manual_review",
                ])
            except Exception as e:
                logger.error("Sheets API failed: %s", e)
                booking_error = True

            # Alert clinic owner (fire and forget - still async)
            try:
                whatsapp.send_owner_alert(patient_name=name, service=service, slot=formatted_slot)
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
            patient["step"] = None
            patient["intent"] = None
            return _twiml(reply)

    # 3. Intent routing
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

        return _twiml(reply)

    elif intent == "book_appointment":
        # Try to extract slots from the message in a single turn
        extracted = llm.extract_booking_slots(body)

        patient["booking"] = {
            "phone": from_phone,
            "name": extracted.get("name"),
            "service": extracted.get("service"),
            "slot": extracted.get("slot"),
        }

        # Check which slots are missing
        missing = []
        if not patient["booking"]["name"]:
            missing.append("name")
        if not patient["booking"]["service"]:
            missing.append("service")
        if not patient["booking"]["slot"]:
            missing.append("slot")

        if not missing:
            # All slots present - book directly
            now_iso = datetime.datetime.now(IST).isoformat()
            return _handle_booking_completion(patient, from_phone, IST, now_iso, calendar_service, sheets_service, whatsapp, logger, _twiml)

        # Some slots missing - continue step-by-step flow
        patient["step"] = missing[0]

        # If name is provided, skip to next missing
        if patient["booking"]["name"] and not patient["booking"]["service"]:
            patient["step"] = "service"
        if patient["booking"]["name"] and patient["booking"]["service"] and not patient["booking"]["slot"]:
            patient["step"] = "slot"

        # Generate appropriate prompt for the first missing slot
        if patient["step"] == "name":
            reply = "I would be glad to help you book an appointment! First, could you please provide your full name?"
        elif patient["step"] == "service":
            pname = patient["booking"]["name"]
            reply = f"Thanks, {pname}! What service or test would you like to book? (e.g., General Consultation, Complete Blood Count, Abdominal Ultrasound)"
        else:  # slot
            reply = "Got it! What date and time would you prefer for your appointment? (e.g., Tomorrow at 10 AM, 2026-10-20 11:00 AM)"

        return _twiml(reply)

    else:
        reply = "Are you looking to book an appointment or do you have a question about our services?"
        return _twiml(reply)


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
    if step:
        if step == "name":
            patient["booking"]["name"] = message.strip()
            patient["step"] = "service"
            pname = patient["booking"]["name"]
            reply = (
                f"Thanks, {pname}! What service or test would you like to book? "
                "(e.g., General Consultation, Complete Blood Count, Abdominal Ultrasound)"
            )
            return ChatResponse(reply=reply, booked=False)

        elif step == "service":
            patient["booking"]["service"] = message.strip()
            patient["step"] = "slot"
            reply = (
                "Got it! What date and time would you prefer for your appointment? "
                "(e.g., Tomorrow at 10 AM, 2026-10-20 11:00 AM)"
            )
            return ChatResponse(reply=reply, booked=False)

        elif step == "slot":
            try:
                slot_dt = _parse_slot_datetime(message.strip())
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

            # Append to Sheets
            try:
                sheets_service.append_row([
                    now_iso,
                    name,
                    phone_clean,
                    service,
                    sheet_slot,
                    "confirmed" if not booking_error else "pending_manual_review",
                ])
            except Exception as e:
                logger.error("Sheets API failed: %s", e)
                booking_error = True

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
            patient["step"] = None
            patient["intent"] = None
            return ChatResponse(reply=reply, booked=not booking_error)

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
        # Try to extract slots from the message
        extracted = llm.extract_booking_slots(message)

        patient["booking"] = {
            "phone": f"web:{from_phone}",
            "name": extracted.get("name"),
            "service": extracted.get("service"),
            "slot": extracted.get("slot"),
        }

        # Check which slots are missing
        missing = []
        if not patient["booking"]["name"]:
            missing.append("name")
        if not patient["booking"]["service"]:
            missing.append("service")
        if not patient["booking"]["slot"]:
            missing.append("slot")

        if not missing:
            # All slots present - book directly
            now_iso = datetime.datetime.now(IST).isoformat()
            phone_clean = from_phone.replace("web:", "").strip()

            try:
                slot_dt = _parse_slot_datetime(patient["booking"]["slot"])
            except SlotResolutionError:
                patient["step"] = "slot"
                return ChatResponse(
                    reply=(
                        "I couldn't resolve that date and time. "
                        "Please provide it again, for example: tomorrow at 10 AM."
                    ),
                    booked=False,
                )
            formatted_slot = _format_slot(slot_dt)
            patient["booking"]["slot"] = formatted_slot

            booking_error = False
            sheet_slot = _format_slot_for_sheet(slot_dt)

            # Create Calendar event
            try:
                calendar_service.create_event(
                    summary=f"{patient['booking']['service']} - {patient['booking']['name']}",
                    start=slot_dt,
                )
            except Exception as e:
                logger.error("Calendar API failed: %s", e)
                booking_error = True

            # Append to Sheets
            try:
                sheets_service.append_row([
                    datetime.datetime.now(IST).isoformat(),
                    patient["booking"]["name"],
                    phone_clean,
                    patient["booking"]["service"],
                    sheet_slot,
                    "confirmed" if not booking_error else "pending_manual_review",
                ])
            except Exception as e:
                logger.error("Sheets API failed: %s", e)
                booking_error = True

            # Reply to patient
            if not booking_error:
                reply = (
                    f"Your appointment for {patient['booking']['service']} has been confirmed for {formatted_slot}!\n"
                    f"Name: {patient['booking']['name']}\n"
                    "We look forward to seeing you at Demo Clinic."
                )
            else:
                reply = (
                    f"Thank you, {patient['booking']['name']}. We received your booking request "
                    f"for {patient['booking']['service']} on {formatted_slot}. "
                    "Our scheduling system encountered a minor delay, but our team has been notified "
                    "and will contact you directly to confirm your slot!"
                )

            patient["booking"] = None
            patient["step"] = None
            patient["intent"] = None
            return ChatResponse(reply=reply, booked=not booking_error)

        # Some slots missing - continue step-by-step flow
        patient["step"] = missing[0]

        if patient["booking"]["name"] and not patient["booking"]["service"]:
            patient["step"] = "service"
        if patient["booking"]["name"] and patient["booking"]["service"] and not patient["booking"]["slot"]:
            patient["step"] = "slot"

        if patient["step"] == "name":
            reply = "I would be glad to help you book an appointment! First, could you please provide your full name?"
        elif patient["step"] == "service":
            pname = patient["booking"]["name"]
            reply = f"Thanks, {pname}! What service or test would you like to book? (e.g., General Consultation, Complete Blood Count, Abdominal Ultrasound)"
        else:  # slot
            reply = "Got it! What date and time would you prefer for your appointment? (e.g., Tomorrow at 10 AM, 2026-10-20 11:00 AM)"

        return ChatResponse(reply=reply, booked=False)

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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
