"""
FreeLLMAPI / OpenAI-compatible LLM client wrapper.
"""

import json
import os
from openai import OpenAI

MODEL = os.environ.get("FREE_LLM_MODEL", "auto")

FAQ_PATH = os.path.join(os.path.dirname(__file__), "data", "clinic_faq.md")

with open(FAQ_PATH, "r", encoding="utf-8") as _f:
    FAQ_TEXT = _f.read()


def _client() -> OpenAI:
    return OpenAI(
        api_key=os.environ["FREE_LLM_API_KEY"],
        base_url=os.environ.get("FREE_LLM_BASE_URL", "https://api.freellmapi.com/v1"),
    )


def _chat(messages: list, *, json_mode: bool = False, temperature: float = 0.0) -> str:
    kwargs = {
        "model": MODEL,
        "messages": messages,
        "temperature": temperature,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    completion = _client().chat.completions.create(**kwargs)
    return completion.choices[0].message.content


def classify_intent(message: str) -> str:
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
    raw = _chat(
        [{"role": "system", "content": system}, {"role": "user", "content": message}],
        json_mode=True,
    )
    try:
        data = json.loads(raw)
        intent = data.get("intent", "unclear")
    except (json.JSONDecodeError, AttributeError, KeyError):
        intent = "unclear"

    return intent if intent in ("faq_question", "book_appointment", "unclear") else "unclear"


def extract_booking_slots(message: str) -> dict:
    """
    Extract booking information from a message in a single turn.
    Returns a dict with any found slots: name, service, slot (datetime string).
    Missing slots will be None.
    """
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
    raw = _chat(
        [{"role": "system", "content": system}, {"role": "user", "content": message}],
        json_mode=True,
    )
    try:
        data = json.loads(raw)
        # Filter out null values
        return {k: v for k, v in data.items() if v is not None}
    except (json.JSONDecodeError, AttributeError, KeyError):
        return {}


def answer_faq(question: str) -> str:
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
    return _chat(
        [{"role": "system", "content": system}, {"role": "user", "content": question}]
    )

