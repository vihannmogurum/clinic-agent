"""Live Google Sheets repository for clinic reference data and appointments."""

import datetime as dt
import logging
import os
import re
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

load_dotenv(override=True)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
REFERENCE_TABS = ("Doctors", "Services_and_Pricing", "Hospital_Hours", "Admission_and_Beds")
APPOINTMENTS_TAB = "Appointments"
REFERENCE_TTL_SECONDS = 60
HEADERS = {
    "Doctors": ["doctor_id", "name", "specialty", "qualifications", "working_days", "shift_start", "shift_end", "slot_duration_mins", "consultation_fee", "status"],
    "Services_and_Pricing": ["service_id", "service_name", "aliases", "category", "default_doctor_id", "duration_mins", "price", "prep_instructions", "availability"],
    "Hospital_Hours": ["day_of_week", "is_open", "open_time", "close_time", "emergency_24x7"],
    "Admission_and_Beds": ["category", "total_capacity", "occupied", "available", "charge_per_day", "notes"],
    "Appointments": ["appointment_id", "phone_number", "patient_name", "service_name", "doctor_id", "doctor_name", "appointment_date", "start_time", "end_time", "fee_quoted", "status", "created_at", "updated_at", "calendar_event_id"],
}

_reference_cache: dict[str, tuple[float, list[dict]]] = {}
logger = logging.getLogger("clinic_agent.appointment_store")


def normalize_phone(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


def _number(value: object, default: int = 0) -> int:
    cleaned = re.sub(r"[^\d.]", "", str(value or ""))
    return int(float(cleaned)) if cleaned else default


def _config() -> tuple[str, str]:
    sheet_id = os.getenv("GOOGLE_SHEET_ID") or os.getenv("SHEET_ID")
    credentials_file = os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or os.getenv("GOOGLE_SHEETS_CREDENTIALS_FILE") or os.getenv("GOOGLE_CREDENTIALS_PATH")
    if not sheet_id:
        raise RuntimeError("Google Sheets is not configured: set GOOGLE_SHEET_ID in .env.")
    if not credentials_file:
        raise RuntimeError("Google Sheets credentials are not configured: set GOOGLE_APPLICATION_CREDENTIALS in .env.")
    path = Path(credentials_file)
    if not path.is_absolute():
        path = Path(__file__).parent / path
    if not path.is_file():
        raise RuntimeError(f"Google Sheets credentials file is missing: {path}")
    return sheet_id, str(path)


def _sheets_service():
    sheet_id, credentials_file = _config()
    credentials = Credentials.from_service_account_file(credentials_file, scopes=SCOPES)
    return build("sheets", "v4", credentials=credentials, cache_discovery=False), sheet_id


def _rows(tab: str, *, cached: bool) -> list[dict]:
    now = time.monotonic()
    if cached and tab in _reference_cache and now - _reference_cache[tab][0] < REFERENCE_TTL_SECONDS:
        return _reference_cache[tab][1]
    service, sheet_id = _sheets_service()
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{tab}'!A:Z"
    ).execute()
    values = result.get("values", [])
    if not values:
        return []
    headers = [str(value).strip() for value in values[0]]
    rows = [dict(zip(headers, row + [""] * (len(headers) - len(row)))) for row in values[1:]]
    if cached:
        _reference_cache[tab] = (now, rows)
    return rows


def _clear_cache() -> None:
    _reference_cache.clear()


def _time(value: str) -> dt.time:
    return dt.datetime.strptime(value.strip(), "%H:%M").time()


def _lookup_service(service_name: str) -> dict | None:
    needle = service_name.strip().lower()
    for row in _rows("Services_and_Pricing", cached=True):
        names = [row.get("service_name", ""), row.get("aliases", "")]
        if any(needle == part.strip().lower() or needle in part.strip().lower()
               for value in names for part in value.split(",")):
            return row
    return None


def answer_reference_question(question: str) -> str | None:
    """Answer supported clinic-data questions from live reference tabs."""
    query = question.strip().lower()
    asks_beds = any(term in query for term in ("bed", "icu", "sicu", "room", "admission"))
    asks_doctors = any(term in query for term in ("doctor", "physician", "qualification", "specialist", "timing", "schedule"))
    asks_services = any(term in query for term in ("price", "pricing", "cost", "fee", "service", "test", "how much", "availability"))
    asks_hours = any(term in query for term in ("hours", "opening", "closing", "visiting", "when are you open", "when do you close"))
    if not any((asks_beds, asks_doctors, asks_services, asks_hours)):
        return None

    try:
        service_rows = _rows("Services_and_Pricing", cached=True)
        if not asks_services and any(
            part.strip().lower() in query
            for row in service_rows
            for value in (row.get("service_name", ""), row.get("aliases", ""))
            for part in value.split(",")
            if part.strip()
        ):
            asks_services = True
        sections = []
        if asks_beds:
            rows = _rows("Admission_and_Beds", cached=True)
            if rows:
                sections.append("Bed availability:\n" + "\n".join(
                    f"- {row.get('category', 'Unknown')}: {row.get('available', '0')} available "
                    f"(total {row.get('total_capacity', '0')}, occupied {row.get('occupied', '0')})"
                    + (f"; ₹{row['charge_per_day']}/day" if row.get("charge_per_day") else "")
                    for row in rows
                ))
        if asks_doctors:
            rows = _rows("Doctors", cached=True)
            if rows:
                sections.append("Doctors:\n" + "\n".join(
                    f"- {row.get('name', 'Unknown')} — {row.get('specialty', 'General')}; "
                    f"qualifications: {row.get('qualifications', 'not listed')}; "
                    f"hours: {row.get('working_days', 'not listed')} "
                    f"{row.get('shift_start', '')}-{row.get('shift_end', '')}; "
                    f"status: {row.get('status', 'not listed')}"
                    for row in rows
                ))
        if asks_services:
            if service_rows:
                sections.append("Services and pricing:\n" + "\n".join(
                    f"- {row.get('service_name', 'Unknown')}: ₹{row.get('price', 'not listed')} "
                    f"({row.get('availability', 'not listed').lower()})"
                    for row in service_rows
                ))
        if asks_hours:
            rows = _rows("Hospital_Hours", cached=True)
            if rows:
                sections.append("Hospital hours:\n" + "\n".join(
                    f"- {row.get('day_of_week', 'Unknown')}: "
                    f"{'open' if row.get('is_open', '').strip().upper() == 'YES' else 'closed'}"
                    + (f", {row.get('open_time', '')}-{row.get('close_time', '')}" if row.get("open_time") else "")
                    + (", emergency 24x7" if row.get("emergency_24x7", "").strip().upper() == "YES" else "")
                    for row in rows
                ))
        return "\n\n".join(sections) if sections else "I couldn't find that information in the clinic's live records."
    except Exception as error:
        logger.error("Live clinic-data lookup failed: %s", error)
        return "I couldn't access the clinic's live records right now. Please try again shortly."


def check_slot_availability(
    service_name: str,
    requested_dt: dt.datetime,
    exclude_appointment_id: str | None = None,
) -> dict:
    service = _lookup_service(service_name)
    if not service:
        available = [row.get("service_name", "") for row in _rows("Services_and_Pricing", cached=True) if row.get("service_name")]
        return {"available": False, "reason": "That service is not listed.", "suggestions": [], "available_services": available}
    if service.get("availability", "").strip().upper() != "AVAILABLE":
        return {"available": False, "reason": f"{service['service_name']} is currently unavailable.", "suggestions": []}

    day = requested_dt.strftime("%A")
    hours = next((row for row in _rows("Hospital_Hours", cached=True)
                  if row.get("day_of_week", "").strip().lower() == day.lower()), None)
    duration = _number(service.get("duration_mins"), 30)
    end_dt = requested_dt + dt.timedelta(minutes=duration)
    if not hours or hours.get("is_open", "").strip().upper() != "YES":
        return {"available": False, "reason": f"The clinic is closed on {day}.", "suggestions": []}
    if requested_dt.time() < _time(hours["open_time"]) or end_dt.time() > _time(hours["close_time"]):
        return {"available": False, "reason": "That time is outside the clinic's configured hours.", "suggestions": []}

    doctor = None
    doctor_id = service.get("default_doctor_id", "").strip()
    if doctor_id:
        doctor = next((row for row in _rows("Doctors", cached=True) if row.get("doctor_id") == doctor_id), None)
        if not doctor or doctor.get("status", "").strip().upper() != "ACTIVE":
            return {"available": False, "reason": "The assigned doctor is not available.", "suggestions": []}
        working_days = [item.strip().lower() for item in doctor.get("working_days", "").split(",")]
        if day.lower() not in working_days or requested_dt.time() < _time(doctor["shift_start"]) or end_dt.time() > _time(doctor["shift_end"]):
            return {"available": False, "reason": "That time is outside the doctor's configured schedule.", "suggestions": []}

    collisions = []
    for row in _rows(APPOINTMENTS_TAB, cached=False):
        if row.get("status", "").strip().upper() != "CONFIRMED":
            continue
        if row.get("appointment_date") != requested_dt.date().isoformat():
            continue
        if row.get("appointment_id") == exclude_appointment_id:
            continue
        if doctor and row.get("doctor_id") != doctor_id:
            continue
        if not doctor and row.get("service_name", "").strip().lower() != service["service_name"].strip().lower():
            continue
        if row.get("start_time", "99:99") < end_dt.strftime("%H:%M") and row.get("end_time", "00:00") > requested_dt.strftime("%H:%M"):
            collisions.append(row)
    if collisions:
        suggestions = []
        candidate = end_dt
        for _ in range(12):
            result = check_slot_availability(service_name, candidate, exclude_appointment_id)
            if result["available"]:
                suggestions.append(candidate.strftime("%I:%M %p"))
                if len(suggestions) == 3:
                    break
            candidate += dt.timedelta(minutes=duration)
        return {"available": False, "reason": "That slot is already booked.", "suggestions": suggestions}
    return {
        "available": True,
        "service": service,
        "doctor": doctor,
        "fee": _number(doctor.get("consultation_fee")) if doctor else _number(service.get("price")),
        "duration_mins": duration,
        "prep_instructions": service.get("prep_instructions", ""),
    }


def _append(row: list[object]) -> None:
    service, sheet_id = _sheets_service()
    service.spreadsheets().values().append(
        spreadsheetId=sheet_id, range=f"'{APPOINTMENTS_TAB}'!A:M",
        valueInputOption="RAW", insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()


def _appointment_rows() -> list[dict]:
    return _rows(APPOINTMENTS_TAB, cached=False)


def create_appointment(phone: str, patient_name: str, service_name: str,
                       requested_dt: dt.datetime, calendar_event_id: str | None = None) -> dict:
    result = check_slot_availability(service_name, requested_dt)
    if not result["available"]:
        raise ValueError(result["reason"])
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    appointment_id = f"APT-{uuid.uuid4().hex[:12].upper()}"
    end = requested_dt + dt.timedelta(minutes=result["duration_mins"])
    _append([
        appointment_id, normalize_phone(phone), patient_name, result["service"]["service_name"],
        result["doctor"]["doctor_id"] if result["doctor"] else "",
        result["doctor"]["name"] if result["doctor"] else "",
        requested_dt.date().isoformat(), requested_dt.strftime("%H:%M"), end.strftime("%H:%M"),
        result["fee"], "CONFIRMED", now, now, calendar_event_id or "",
    ])
    return {"appointment_id": appointment_id, **result}


def upcoming(phone: str) -> list[dict]:
    today = dt.datetime.now().date().isoformat()
    return [row for row in _appointment_rows()
            if normalize_phone(row.get("phone_number")) == normalize_phone(phone)
            and row.get("status", "").upper() == "CONFIRMED"
            and row.get("appointment_date", "") >= today]


def get_appointment(appointment_id: str) -> dict | None:
    return next((row for row in _appointment_rows() if row.get("appointment_id") == appointment_id), None)


def _update(appointment_id: str, updates: dict) -> None:
    service, sheet_id = _sheets_service()
    rows = _appointment_rows()
    index = next((i for i, row in enumerate(rows, start=2) if row.get("appointment_id") == appointment_id), None)
    if index is None:
        raise ValueError("Appointment not found.")
    current = get_appointment(appointment_id) or {}
    current.update(updates)
    values = [current.get(header, "") for header in HEADERS[APPOINTMENTS_TAB]]
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id, range=f"'{APPOINTMENTS_TAB}'!A{index}:M{index}",
        valueInputOption="RAW", body={"values": [values]},
    ).execute()


def set_status(appointment_id: str, status: str) -> None:
    _update(appointment_id, {"status": status, "updated_at": dt.datetime.now(dt.timezone.utc).isoformat()})


def reschedule(appointment_id: str, requested_dt: dt.datetime) -> dict:
    appointment = get_appointment(appointment_id)
    if not appointment:
        raise ValueError("Appointment not found.")
    result = check_slot_availability(appointment["service_name"], requested_dt, appointment_id)
    if not result["available"]:
        raise ValueError(result["reason"])
    end = requested_dt + dt.timedelta(minutes=result["duration_mins"])
    _update(appointment_id, {
        "appointment_date": requested_dt.date().isoformat(),
        "start_time": requested_dt.strftime("%H:%M"),
        "end_time": end.strftime("%H:%M"),
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    })
    return {"appointment": get_appointment(appointment_id), **result}
