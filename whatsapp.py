"""
Twilio WhatsApp client wrapper.

Handles both directions:
- INCOMING: parse the form-encoded POST body that Twilio's WhatsApp Sandbox
  sends to our webhook (we only need the `Body`, `From`, and `MessageSid`
  fields).
- OUTGOING: send a reply message back to the same number the message came
  from, plus a separate alert message to the clinic owner.

PONYTAIL: incoming webhook signature validation (X-Twilio-Signature) is
intentionally skipped for this prototype. It is one extra dependency and one
extra place for a misconfigured auth token to break the demo. Re-add it before
production: validate the signature against TWILIO_AUTH_TOKEN and reject
unsigned requests.
"""

import os

from twilio.rest import Client

# Twilio WhatsApp Sandbox uses "+1 415 526 8675" as the sandbox number; the
# `from_` value we send with must match the sandbox number Twilio assigned us.
# Read it from env so the same code works after the sandbox number is issued.
# Use os.getenv to avoid KeyError at import time if .env not loaded yet.
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155268675")


def parse_incoming(form_data: dict) -> dict:
    """
    Extract the fields we care about from a Twilio webhook POST body.

    Twilio sends everything as form-encoded strings, so `Body` may arrive as
    a list if the same key is repeated (Twilio does this for media). We take
    the first value.
    """
    body = form_data.get("Body")
    if isinstance(body, list):
        body = body[0] if body else ""

    return {
        "body": (body or "").strip(),
        "from_phone": (form_data.get("From") or "").strip(),
        "message_sid": (form_data.get("MessageSid") or "").strip(),
    }


def _client() -> Client:
    """Build a Twilio client from the account SID and auth token in env."""
    return Client(
        os.environ["TWILIO_ACCOUNT_SID"],
        os.environ["TWILIO_AUTH_TOKEN"],
    )


def send_message(to: str, text: str) -> str:
    """
    Send a WhatsApp message to `to` (e.g. "whatsapp:+919876543210").

    Returns the Twilio message SID. Raises on failure — callers are expected
    to catch and fall back gracefully.
    """
    message = _client().messages.create(
        from_=TWILIO_WHATSAPP_NUMBER,
        to=to,
        body=text,
    )
    return message.sid


def send_owner_alert(patient_name: str, service: str, slot: str) -> str:
    """
    Send the clinic owner a short WhatsApp alert about a new booking.

    `OWNER_WHATSAPP_NUMBER` is read from env so it can change without a redeploy.
    """
    owner_number = os.environ["OWNER_WHATSAPP_NUMBER"]
    text = (
        f"New appointment booked at Demo Clinic.\n"
        f"Patient: {patient_name}\n"
        f"Service: {service}\n"
        f"Slot: {slot}"
    )
    return send_message(owner_number, text)