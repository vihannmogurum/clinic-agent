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

from fastapi import FastAPI, Request, Response
import uvicorn

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

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
UNANSWERED_LOG = pathlib.Path(__file__).parent / "data" / "unanswered_questions.log"

def _parse_slot_datetime(slot_str: str) -> datetime.datetime:
    """Parse a natural language or date string into a timezone-aware datetime."""
    try:
        from dateutil import parser
        dt = parser.parse(slot_str, fuzzy=True)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        return dt
    except Exception:
        now = datetime.datetime.now(IST)
        tomorrow = now + datetime.timedelta(days=1)
        # Default to 2 PM (14:00) if slot time cannot be parsed
        return tomorrow.replace(hour=14, minute=0, second=0, microsecond=0)


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

    slot_dt = _parse_slot_datetime(slot_str)

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
            slot_str,
            "confirmed" if not booking_error else "pending_manual_review",
        ])
    except Exception as e:
        logger.error("Sheets API failed: %s", e)
        booking_error = True

    # Alert clinic owner (fire and forget - still async)
    try:
        whatsapp.send_owner_alert(patient_name=name, service=service, slot=slot_str)
    except Exception as e:
        logger.error("Owner alert WhatsApp message failed: %s", e)

    # Reply to patient
    if not booking_error:
        reply = (
            f"Your appointment for {service} has been confirmed for {slot_str}!\n"
            f"Name: {name}\n"
            "We look forward to seeing you at Demo Clinic."
        )
    else:
        reply = (
            f"Thank you, {name}. We received your booking request for {service} on {slot_str}. "
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
            patient["booking"]["slot"] = body.strip()
            name = patient["booking"]["name"]
            service = patient["booking"]["service"]
            slot_str = patient["booking"]["slot"]
            phone = from_phone.replace("whatsapp:", "").strip()

            slot_dt = _parse_slot_datetime(slot_str)
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
                    slot_str,
                    "confirmed" if not booking_error else "pending_manual_review",
                ])
            except Exception as e:
                logger.error("Sheets API failed: %s", e)
                booking_error = True

            # Alert clinic owner (fire and forget - still async)
            try:
                whatsapp.send_owner_alert(patient_name=name, service=service, slot=slot_str)
            except Exception as e:
                logger.error("Owner alert WhatsApp message failed: %s", e)

            # Reply to patient
            if not booking_error:
                reply = (
                    f"Your appointment for {service} has been confirmed for {slot_str}!\n"
                    f"Name: {name}\n"
                    "We look forward to seeing you at Demo Clinic."
                )
            else:
                reply = (
                    f"Thank you, {name}. We received your booking request for {service} on {slot_str}. "
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
