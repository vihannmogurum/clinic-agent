"""
In-memory conversation state for the Demo Clinic WhatsApp agent.

STATE is a plain dict keyed by the patient's WhatsApp phone number (the value
of Twilio's `From` field with the leading "whatsapp:" stripped).

Each value is a dict:
    consent:  bool        -- has the patient replied YES to the consent prompt?
    intent:   str | None  -- last classified intent
    booking:  dict | None -- collected booking fields while we gather them
    step:     str | None  -- which booking field we are currently waiting for
"""

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
        },
    )


def reset(phone: str) -> None:
    """Clear or reset state for a given phone number."""
    if phone in STATE:
        del STATE[phone]