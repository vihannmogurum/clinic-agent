"""
FreeLLMAPI / OpenAI-compatible LLM client wrapper with robust fallbacks.
"""

import json
import os
import re
from openai import OpenAI

MODEL = os.environ.get("FREE_LLM_MODEL", "auto")

FAQ_PATH = os.path.join(os.path.dirname(__file__), "data", "clinic_faq.md")

with open(FAQ_PATH, "r", encoding="utf-8") as _f:
    FAQ_TEXT = _f.read()

# ============ Keyword-based fallbacks (fast, reliable) ============

BOOKING_KEYWORDS = [
    "book", "appointment", "schedule", "slot",
    "visit", "checkup", "check-up", "see doctor", "see a doctor",
    "make an appointment", "get an appointment", "need an appointment"
]

FAQ_KEYWORDS = [
    "cost", "price", "pricing", "fee", "charge", "how much",
    "hour", "time", "open", "close", "location", "where",
    "address", "phone", "contact", "test", "blood", "ultrasound",
    "ecg", "x-ray", "xray", "prepare", "preparation", "fasting"
]

# Words that indicate booking ACTION vs FAQ question
BOOKING_ACTION_WORDS = ["book", "schedule", "make", "get", "need an appointment", "want an appointment"]

SERVICE_KEYWORDS = {
    "consultation": "General Consultation",
    "checkup": "General Consultation",
    "check-up": "General Consultation",
    "blood": "Blood Test",
    "test": "Blood Test",
    "ultrasound": "Ultrasound",
    "ecg": "ECG",
    "x-ray": "X-Ray",
    "xray": "X-Ray",
}

# ============ LLM Client ============

def _client() -> OpenAI:
    return OpenAI(
        api_key=os.environ["FREE_LLM_API_KEY"],
        base_url=os.environ.get("FREE_LLM_BASE_URL", "https://api.freellmapi.com/v1"),
    )


def _chat(messages: list, *, json_mode: bool = False, temperature: float = 0.0) -> str:
    kwargs = {
        "model": os.environ.get("FREE_LLM_MODEL", "auto"),
        "messages": messages,
        "temperature": temperature,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    completion = _client().chat.completions.create(**kwargs)
    return completion.choices[0].message.content


# ============ Robust Intent Classification ============

def _keyword_classify_intent(message: str) -> str:
    """Fast, reliable keyword-based classification."""
    msg = message.lower()

    # Check for booking ACTION words (not just consultation as a noun)
    # Must have booking action words OR explicit booking intent
    if any(kw in msg for kw in BOOKING_ACTION_WORDS):
        return "book_appointment"

    # "consultation" alone is usually a FAQ question about cost/availability
    # unless paired with booking action words
    if "consultation" in msg and not any(kw in msg for kw in BOOKING_ACTION_WORDS):
        return "faq_question"

    # Check for FAQ intent
    if any(kw in msg for kw in FAQ_KEYWORDS):
        return "faq_question"

    return "unclear"


def classify_intent(message: str) -> str:
    """
    Robust intent classification with multiple fallback layers:
    1. Keyword-based (instant, reliable)
    2. LLM with retries (if keywords inconclusive)
    """
    # Layer 1: Keyword-based (always works, instant)
    keyword_intent = _keyword_classify_intent(message)
    if keyword_intent != "unclear":
        return keyword_intent

    # Layer 2: LLM fallback (with retries and timeout)
    system = (
        "You are an intent classifier for a clinic's WhatsApp agent.\n"
        "Read the patient's message and reply with ONLY a JSON object like "
        '{"intent": "faq_question"}.\n\n'
        "Rules:\n"
        '- "faq_question" when the patient asks about services, pricing, hours, '
        "pre-test instructions, location, or how to book.\n"
        '- "book_appointment" when the patient wants to book, schedule, or set up '
        "an appointment, consultation, or test. Keywords like book, appointment, "
        "schedule, consultation, slot all indicate booking intent.\n"
        '- "unclear" for anything else, including greetings and messages that '
        "do not clearly fit either category.\n\n"
        "Reply with the JSON object and nothing else."
    )

    for attempt in range(3):
        try:
            raw = _chat(
                [{"role": "system", "content": system}, {"role": "user", "content": message}],
                json_mode=True,
                temperature=0.0,
            )
            data = json.loads(raw)
            intent = data.get("intent", "unclear")
            if intent in ("faq_question", "book_appointment", "unclear"):
                return intent
        except Exception:
            continue  # Retry

    # Final fallback
    return "unclear"


# ============ Robust Slot Extraction ============

def _regex_extract_slots(message: str) -> dict:
    """Fast regex-based slot extraction."""
    result = {}
    msg = message.lower()

    # Service extraction
    for keyword, service in SERVICE_KEYWORDS.items():
        if keyword in msg:
            result["service"] = service
            break

    # Slot/time extraction - extract just the time portion
    slot_patterns = [
        (r"today at (\d{1,2}(?::\d{2})?\s*(?:am|pm))", r"today at \1"),
        (r"tomorrow at (\d{1,2}(?::\d{2})?\s*(?:am|pm))", r"tomorrow at \1"),
        (r"(\d{1,2}(?::\d{2})?\s*(?:am|pm))\s*(?:today|tomorrow)?", r"\1"),
        (r"(\d{4}-\d{2}-\d{2}\s+\d{1,2}(?::\d{2})?(?:\s*(?:am|pm))?)", r"\1"),
    ]

    for pattern, replacement in slot_patterns:
        match = re.search(pattern, message, re.IGNORECASE)
        if match:
            # Extract just the matched time portion
            result["slot"] = match.group(1) if match.groups() else match.group(0)
            # If it's just a time, prepend today/tomorrow context
            if "today" in message.lower() and "tomorrow" not in result["slot"].lower():
                result["slot"] = "today at " + result["slot"]
            elif "tomorrow" in message.lower() and "tomorrow" not in result["slot"].lower():
                result["slot"] = "tomorrow at " + result["slot"]
            break

    # Name extraction
    name_match = re.search(r"name is\s+([A-Za-z\s]+)", message, re.IGNORECASE)
    if name_match:
        result["name"] = name_match.group(1).strip()

    # Also check for "my name is" or "I am"
    name_match2 = re.search(r"(?:my name is|i am)\s+([A-Za-z\s]+)", message, re.IGNORECASE)
    if name_match2 and "name" not in result:
        result["name"] = name_match2.group(1).strip()

    return result


def extract_booking_slots(message: str) -> dict:
    """
    Robust slot extraction with multiple fallback layers:
    1. Regex-based (instant, reliable for common patterns)
    2. LLM fallback (if regex misses something)
    """
    # Layer 1: Regex-based (instant, reliable for common patterns)
    regex_result = _regex_extract_slots(message)
    if regex_result:
        return regex_result

    # Layer 2: LLM fallback
    system = (
        "You are a slot extractor for a clinic booking system.\n"
        "Extract the following fields from the patient's message if present:\n"
        "- name: patient's full name\n"
        "- service: the medical service or test they want (e.g., General Consultation, Blood Test, Ultrasound)\n"
        "- slot: preferred date/time in natural language (e.g., 'tomorrow at 3 PM', '2026-10-20 15:00')\n\n"
        "Reply with ONLY a JSON object like:\n"
        '{"name": "John Doe", "service": "General Consultation", "slot": "tomorrow at 3 PM"}.\n\n'
        "If a field is not mentioned, use null. Do not guess or infer missing fields."
    )

    for attempt in range(2):
        try:
            raw = _chat(
                [{"role": "system", "content": system}, {"role": "user", "content": message}],
                json_mode=True,
                temperature=0.0,
            )
            data = json.loads(raw)
            return {k: v for k, v in data.items() if v is not None}
        except Exception:
            continue

    return {}


# ============ Robust FAQ Answering ============

def _local_faq_answer(question: str) -> str:
    """Fast local FAQ search - no LLM needed."""
    q = question.lower()

    # Simple keyword-based lookup from FAQ
    if "consultation" in q and ("cost" in q or "price" in q or "fee" in q):
        return "General Physician Consultation is ₹200. Specialist Consultation is ₹500."
    if "blood" in q and ("cost" in q or "price" in q):
        return "Complete Blood Count is ₹150. HbA1c is ₹400. Fasting Blood Sugar is ₹100. Lipid Profile is ₹300."
    if "ultrasound" in q and ("cost" in q or "price" in q):
        return "Abdominal Ultrasound is ₹800. Obstetric Ultrasound is ₹900. Thyroid Ultrasound is ₹700."
    if "ecg" in q and ("cost" in q or "price" in q):
        return "ECG is ₹200."
    if "x-ray" in q or "xray" in q and ("cost" in q or "price" in q):
        return "Chest X-Ray is ₹400."
    if "hour" in q or "time" in q or "open" in q or "close" in q:
        return "Monday to Saturday: 9:00 AM to 7:00 PM. Sunday: Closed."
    if "location" in q or "address" in q or "where" in q:
        return "Road No. 12, Banjara Hills, Hyderabad, Telangana 500034"
    if "phone" in q or "contact" in q:
        return "+91 98765 43210"
    if "fasting" in q or "prepare" in q:
        return "For blood tests: Do not eat or drink anything (except plain water) for 8 to 12 hours before the test. For ultrasound: Drink 3 to 4 glasses of water 1 hour before and do not empty bladder."
    return ""


def answer_faq(question: str) -> str:
    """
    Robust FAQ answering with multiple fallback layers:
    1. Local keyword search (instant, covers common questions)
    2. LLM with FAQ context (for complex questions)
    """
    # Layer 1: Local FAQ (instant, covers 80%+ of common questions)
    local_answer = _local_faq_answer(question)
    if local_answer:
        return local_answer

    # Layer 2: LLM with FAQ context
    system = (
        "You are a helpful assistant for Demo Clinic.\n"
        "Answer the patient's question using ONLY the information in the "
        "FAQ below. Do not invent, extrapolate, or guess at anything not "
        "explicitly stated there.\n\n"
        "FAQ:\n"
        f"{FAQ_TEXT}\n\n"
        "If the FAQ does not contain the answer, reply with exactly the "
        "single word: NOT_IN_FAQ\n"
        "If it does, answer concisely and only with facts from the FAQ."
    )

    for attempt in range(2):
        try:
            return _chat(
                [{"role": "system", "content": system}, {"role": "user", "content": question}],
                temperature=0.0,
            )
        except Exception:
            continue

    return "NOT_IN_FAQ"