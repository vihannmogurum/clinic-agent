"""
In-memory conversation state for the Demo Clinic WhatsApp agent.

STATE is a plain dict keyed by the patient's WhatsApp phone number from Meta's
webhook payload.

Each value is a dict:
    consent:  bool        -- has the patient replied YES to the consent prompt?
    intent:   str | None  -- last classified intent
    booking:  dict | None -- collected booking fields while we gather them
    step:     str | None  -- name, service, slot, or None (idle)
"""

from collections import deque


STATE: dict[str, dict] = {}


def get(phone: str) -> dict:
    """Return the state for a phone number, creating an empty entry if new."""
    return STATE.setdefault(
        phone,
        {
            "consent": False,
            "intent": None,
            "booking": None,
            "step": None,
            "deferred_intent_text": None,
            "candidate_date": None,
            "processed_message_ids": deque(maxlen=100),
        },
    )


def mark_message_processed(phone: str, message_id: str | None) -> bool:
    """Return False when a Meta message ID has already been handled."""
    if not message_id:
        return True
    patient = get(phone)
    processed_ids = patient["processed_message_ids"]
    if message_id in processed_ids:
        return False
    processed_ids.append(message_id)
    return True


def reset(phone: str) -> None:
    """Clear or reset state for a given phone number."""
    if phone in STATE:
        del STATE[phone]