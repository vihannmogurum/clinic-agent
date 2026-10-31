"""Meta WhatsApp Cloud API integration."""

import json
import os
from urllib import request

import logging
import urllib.error
from dotenv import load_dotenv

load_dotenv(override=True)

logger = logging.getLogger(__name__)

GRAPH_API_VERSION = "v21.0"


def parse_meta_incoming(payload: dict) -> dict:
    """Extract a text message from a Meta WhatsApp webhook payload."""
    try:
        value = payload["entry"][0]["changes"][0]["value"]
        messages = value.get("messages") or []
        if not messages:
            return {"from_phone": None, "body": None}

        message = messages[0]
        if message.get("type") != "text":
            return {"from_phone": None, "body": None}

        phone = (message.get("from") or "").strip()
        body = ((message.get("text") or {}).get("body") or "").strip()
        if not phone or not body:
            return {"from_phone": None, "body": None}
        return {"from_phone": phone, "body": body}
    except (KeyError, IndexError, TypeError, AttributeError):
        return {"from_phone": None, "body": None}


def send_whatsapp_message(to_phone: str, text: str) -> bool:
    """Send a text message through the Meta WhatsApp Cloud API."""
    if not text:
        logger.error("send_whatsapp_message aborted: text body is empty or None")
        return False

    token = (os.environ.get("META_WA_TOKEN") or "").strip().strip("'\"")
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    phone_number_id = (os.environ.get("META_WA_PHONE_NUMBER_ID") or "").strip().strip("'\"")
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "text",
        "text": {"body": text},
    }
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=15) as response:
            return 200 <= response.status < 300
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        logger.error(f"Meta Graph API error ({e.code}): {error_body}")
        return False
    except Exception as e:
        logger.error(f"Failed to send request: {e}")
        return False


def send_owner_alert(patient_name: str, service: str, slot: str) -> bool:
    """Send a booking alert to the clinic owner."""
    owner_phone = os.environ["OWNER_PHONE"]
    text = (
        "New appointment booked at Demo Clinic.\n"
        f"Patient: {patient_name}\n"
        f"Service: {service}\n"
        f"Slot: {slot}"
    )
    return send_whatsapp_message(owner_phone, text)
