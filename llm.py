"""
FreeLLMAPI / OpenAI-compatible LLM client wrapper with robust fallbacks.
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone
from openai import OpenAI

from dotenv import load_dotenv
load_dotenv(override=True)

import logging

# Configure basic logging if not already configured
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MODEL = os.environ.get("FREE_LLM_MODEL", "auto")
IST = timezone(timedelta(hours=5, minutes=30))

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
BOOKING_ACTION_WORDS = [
    "book",
    "appointment",
    "schedule",
    "make",
    "get",
    "need an appointment",
    "want an appointment",
]

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

def _llm_base_url() -> str:
    base_url = (os.environ.get("FREE_LLM_BASE_URL") or "").strip().strip("'\"")
    if not base_url:
        base_url = "https://generativelanguage.googleapis.com/v1beta/openai/"
    if base_url.rstrip("/") == "http://localhost:3001":
        logger.warning("FREE_LLM_BASE_URL points to localhost:3001; using the FreeLLMAPI endpoint")
        base_url = "https://api.freellmapi.com/v1"
    return base_url


def _client() -> OpenAI:
    return OpenAI(
        api_key=os.environ["FREE_LLM_API_KEY"],
        base_url=_llm_base_url(),
    )


def _chat(messages: list, *, json_mode: bool = False, temperature: float = 0.0) -> str:
    kwargs = {
        "model": os.environ.get("FREE_LLM_MODEL", "auto"),
        "messages": messages,
        "temperature": temperature,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    client = _client()
    try:
        completion = client.chat.completions.create(**kwargs)
        return completion.choices[0].message.content
    except Exception as e:
        logger.error("LLM request failed at %s: %s", client.base_url, e)
        raise


def parse_slot_with_llm(user_message: str) -> str:
    """Resolve a user's appointment time to an ISO 8601 local datetime."""
    now = datetime.now(IST)
    tomorrow = now + timedelta(days=1)
    day_after_tomorrow = now + timedelta(days=2)
    current_time = now.strftime("%Y-%m-%d %H:%M:%S %Z")
    context = (
        f"Current timestamp: {current_time}\n"
        f"Current weekday: {now.strftime('%A')}\n"
        f"Tomorrow's date: {tomorrow.strftime('%Y-%m-%d')} "
        f"({tomorrow.strftime('%A')})\n"
        f"Day after tomorrow's date: {day_after_tomorrow.strftime('%Y-%m-%d')} "
        f"({day_after_tomorrow.strftime('%A')})\n\n"
    )
    system = (
        context +
        "Convert the user's requested appointment time into exactly one ISO 8601 "
        "local datetime string: YYYY-MM-DDTHH:MM:SS.\n"
        "Resolve relative dates using the date context above. For example, the "
        "word 'tomorrow' MUST use Tomorrow's date, never the current date. "
        "Resolve 'next Monday' to the next calendar Monday after the current date. "
        "Interpret AM/PM exactly as written: 10 AM means 10:00:00, while 10 PM "
        "means 22:00:00. If no explicit time is specified, use 10:00:00.\n"
        "Return ONLY the ISO datetime string. Do not include markdown, timezone "
        "suffixes, explanation, or any other text."
    )
    try:
        candidate = _chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user_message}],
            temperature=0.0,
        ).strip().strip("`")
    except Exception as e:
        logger.error("parse_slot_with_llm failed at %s: %s", _llm_base_url(), e)
        raise
    datetime.strptime(candidate, "%Y-%m-%dT%H:%M:%S")

    verification_system = (
        context +
        "You are the final appointment-time verifier. Compare the user's original "
        "request with the candidate ISO datetime. Correct the candidate if it has "
        "the wrong relative date or AM/PM value. In particular, 'tomorrow at 10 AM' "
        "must resolve to Tomorrow's date at 10:00:00, never today's date and never "
        "22:00:00. Return ONLY the corrected ISO datetime string in the exact "
        "format YYYY-MM-DDTHH:MM:SS."
    )
    try:
        verified = _chat(
            [
                {"role": "system", "content": verification_system},
                {
                    "role": "user",
                    "content": f"Original request: {user_message}\nCandidate: {candidate}",
                },
            ],
            temperature=0.0,
        ).strip().strip("`")
    except Exception as e:
        logger.error("parse_slot_with_llm verification failed at %s: %s", _llm_base_url(), e)
        raise
    datetime.strptime(verified, "%Y-%m-%dT%H:%M:%S")
    return verified


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
        except Exception as e:
            logger.error("classify_intent attempt %d failed at %s: %s", attempt + 1, _llm_base_url(), e)
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
    # Extract stable fields locally, then let the LLM handle appointment dates.
    regex_result = _regex_extract_slots(message)
    if "slot" in regex_result:
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
            extracted = {k: v for k, v in data.items() if v is not None}
            return {**regex_result, **extracted}
        except Exception as e:
            logger.error("extract_booking_slots attempt %d failed at %s: %s", attempt + 1, _llm_base_url(), e)
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