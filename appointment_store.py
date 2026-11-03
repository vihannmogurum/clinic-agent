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
REFERENCE_TTL_SECONDS = 30
HEADERS = {
    "Doctors": ["doctor_id", "name", "specialty", "qualifications", "working_days", "shift_start", "shift_end", "slot_duration_mins", "consultation_fee", "status"],
    "Services_and_Pricing": ["service_id", "service_name", "aliases", "category", "default_doctor_id", "duration_mins", "price", "prep_instructions", "availability"],
    "Hospital_Hours": ["day_of_week", "is_open", "open_time", "close_time", "emergency_24x7"],
    "Admission_and_Beds": ["category", "total_capacity", "occupied", "available", "charge_per_day", "notes"],
    "Appointments": ["appointment_id", "phone_number", "patient_name", "service_name", "doctor_id", "doctor_name", "appointment_date", "start_time", "end_time", "fee_quoted", "status", "created_at", "updated_at", "calendar_event_id"],
}

_reference_cache: dict[str, tuple[float, list[dict]]] = {}
logger = logging.getLogger("clinic_agent.appointment_store")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


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
    value = str(value or "").strip()
    for pattern in ("%H:%M", "%I:%M %p", "%I %p"):
        try:
            return dt.datetime.strptime(value.upper(), pattern).time()
        except ValueError:
            continue
    raise ValueError(f"Invalid sheet time: {value!r}")


def normalize_doc_name(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"^dr\.?\s*", "", value.strip(), flags=re.IGNORECASE)).lower()


def _active_doctors() -> list[dict]:
    return [
        row for row in _rows("Doctors", cached=True)
        if row.get("status", "").strip().upper() == "ACTIVE"
    ]


def _lookup_doctor(value: str) -> dict | None:
    needle = normalize_doc_name(value)
    for row in _active_doctors():
        if normalize_doc_name(row.get("name", "")) == needle:
            return row
    return None


def _doctor_from_text(value: str) -> dict | None:
    needle = normalize_doc_name(value)
    for row in _active_doctors():
        candidate = normalize_doc_name(row.get("name", ""))
        if candidate and candidate in needle:
            return row
    return None


def _lookup_service(service_name: str) -> dict | None:
    needle = service_name.strip().lower()
    for row in _rows("Services_and_Pricing", cached=True):
        names = [row.get("service_name", ""), row.get("aliases", "")]
        if any(needle == part.strip().lower() or needle in part.strip().lower()
               for value in names for part in value.split(",")):
            return row
    return None


def resolve_service_and_doctor(service_text: str) -> tuple[dict | None, dict | None]:
    doctor = _doctor_from_text(service_text)
    service = _lookup_service(service_text)
    if service is None:
        for row in _rows("Services_and_Pricing", cached=True):
            names = [row.get("service_name", ""), row.get("aliases", "")]
            if any(part.strip().lower() in service_text.lower()
                   for value in names for part in value.split(",") if part.strip()):
                service = row
                break
    if doctor is None and service:
        default_id = service.get("default_doctor_id", "").strip()
        if default_id:
            doctor = next(
                (row for row in _active_doctors() if row.get("doctor_id", "").strip() == default_id),
                None,
            )
        if doctor is None and "consult" in service.get("category", "").lower():
            specialty = service.get("category", "").strip().lower()
            doctor = next(
                (row for row in _active_doctors()
                 if specialty in row.get("specialty", "").strip().lower()
                 or row.get("specialty", "").strip().lower() in specialty),
                None,
            )
    return service, doctor


def _doctor_works(doctor: dict, requested_dt: dt.datetime, duration: int) -> bool:
    day = requested_dt.strftime("%A").lower()
    working_days = [item.strip().lower() for item in doctor.get("working_days", "").split(",")]
    return (
        day in working_days
        and requested_dt.time() >= _time(doctor.get("shift_start", "00:00"))
        and (requested_dt + dt.timedelta(minutes=duration)).time() <= _time(doctor.get("shift_end", "23:59"))
    )


def _doctor_duration(service: dict, doctor: dict | None) -> int:
    if doctor:
        raw_duration = str(doctor.get("slot_duration_mins", "")).strip()
        duration = int(float(raw_duration)) if re.fullmatch(r"\d+(?:\.\d+)?", raw_duration) else 0
        if duration > 0:
            return duration
    raw_duration = str(service.get("duration_mins", "")).strip()
    duration = int(float(raw_duration)) if re.fullmatch(r"\d+(?:\.\d+)?", raw_duration) else 0
    return duration if duration > 0 else 15


def _service_by_id(service_id: str) -> dict | None:
    needle = str(service_id or "").strip()
    for index, row in enumerate(_rows("Services_and_Pricing", cached=True)):
        if str(row.get("service_id") or index).strip() == needle:
            return row
    return None


def _service_is_enabled(service: dict) -> bool:
    value = str(service.get("availability", service.get("status", service.get("active", "")))).strip().upper()
    return value not in {"DISABLED", "INACTIVE", "NO", "FALSE", "CLOSED"}


def interactive_service_rows() -> list[dict]:
    """Return sanitized service menu records directly from the live sheet."""
    rows = []
    for index, service in enumerate(_rows("Services_and_Pricing", cached=True)):
        name = str(service.get("service_name", "")).strip()
        if not name or not _service_is_enabled(service):
            continue
        service_id = str(service.get("service_id") or index).strip()
        price = re.sub(r"[₹,\s]", "", str(service.get("price_inr", service.get("price", ""))))
        duration = str(service.get("duration_mins") or "15").strip()
        category = str(service.get("category", "")).strip()
        description = f"₹{price} • {duration} mins"
        if category:
            description += f" • {category}"
        rows.append({
            "id": f"SVC_{service_id}",
            "title": name[:24],
            "description": description[:72],
        })
        if len(rows) == 10:
            break
    return rows


def interactive_service(service_id: str) -> dict | None:
    """Resolve an interactive service ID and retain all live booking metadata."""
    service = _service_by_id(service_id)
    if service and str(service.get("service_name", "")).strip() and _service_is_enabled(service):
        return service
    return None


def interactive_service_for_name(service_name: str) -> dict | None:
    """Resolve a typed service to the live row used by interactive booking."""
    service, _ = resolve_service_and_doctor(service_name)
    if service and str(service.get("service_name", "")).strip() and _service_is_enabled(service):
        return service
    return None


def _eligible_doctors(service: dict) -> list[dict]:
    active = _active_doctors()
    default_id = str(service.get("default_doctor_id", "")).strip()
    default = next((doctor for doctor in active if doctor.get("doctor_id", "").strip() == default_id), None)
    category = str(service.get("category", "")).strip().lower()
    if not category:
        matched = active
    else:
        matched = [
            doctor for doctor in active
            if category in str(doctor.get("specialty", "")).strip().lower()
            or str(doctor.get("specialty", "")).strip().lower() in category
        ]
    if default:
        return [default] + [doctor for doctor in matched if doctor.get("doctor_id") != default_id]
    return matched


def _hospital_hours(day: dt.date) -> dict | None:
    return next(
        (row for row in _rows("Hospital_Hours", cached=True)
         if str(row.get("day_of_week", "")).strip().lower() == day.strftime("%A").lower()
         and str(row.get("is_open", "")).strip().upper() in {"YES", "OPEN", "TRUE"}),
        None,
    )


def interactive_open_slots(
    service_id: str, day: dt.date, doctor_id: str | None = None, limit: int = 10,
) -> list[dict]:
    """Compute open nominal intervals from live hours, shifts, and appointments."""
    service = interactive_service(service_id)
    if not service:
        return []
    hours = _hospital_hours(day)
    if not hours:
        return []
    try:
        clinic_start = _time(hours.get("open_time", "00:00"))
        clinic_end = _time(hours.get("close_time", "23:59"))
    except ValueError:
        logger.error("Invalid hospital hours for %s", day)
        return []
    doctors = _eligible_doctors(service)
    if doctor_id:
        doctors = [doctor for doctor in doctors if doctor.get("doctor_id", "").strip() == doctor_id]
    if not doctors:
        doctors = [None]
    result = []
    duration = _doctor_duration(service, doctors[0])
    cursor = dt.datetime.combine(day, clinic_start)
    close_dt = dt.datetime.combine(day, clinic_end)
    while cursor + dt.timedelta(minutes=duration) <= close_dt and len(result) < limit:
        availability = None
        for candidate_doctor in doctors:
            candidate_id = candidate_doctor.get("doctor_id") if candidate_doctor else None
            candidate = check_slot_availability(
                str(service.get("service_name", "")), cursor,
                doctor_id=candidate_id, cleanup=False,
            )
            if candidate.get("available"):
                availability = candidate
                break
        if availability is None:
            availability = {"available": False}
        if availability.get("available"):
            doctor = availability.get("doctor")
            doctor_name = doctor.get("name", "") if doctor else ""
            fee = availability.get("fee", _number(service.get("price_inr", service.get("price"))))
            result.append({
                "id": f"TIME_{cursor.strftime('%H:%M')}",
                "title": cursor.strftime("%I:%M %p").lstrip("0"),
                "description": (
                    f"Dr. {doctor_name} • ₹{fee}" if doctor_name
                    else f"₹{fee}"
                ),
                "datetime": cursor,
                "doctor_id": doctor.get("doctor_id") if doctor else None,
            })
        cursor += dt.timedelta(minutes=duration)
    return result


def interactive_open_dates(
    service_id: str, now: dt.datetime, limit: int = 7,
    doctor_id: str | None = None,
) -> list[dict]:
    """Return the next sheet-valid dates with at least one open slot."""
    service = interactive_service(service_id)
    if not service:
        return []
    dates = []
    day = now.date()
    for _ in range(14):
        hours = _hospital_hours(day)
        if hours and interactive_open_slots(service_id, day, doctor_id=doctor_id, limit=1):
            dates.append({
                "id": f"DATE_{day.isoformat()}",
                "title": day.strftime("%a, %d %b")[:24],
                "description": f"Hours: {hours.get('open_time', '')} - {hours.get('close_time', '')}"[:72],
            })
            if len(dates) == limit:
                break
        day += dt.timedelta(days=1)
    return dates


def _available_peer(service: dict, primary: dict, requested_dt: dt.datetime,
                    exclude_appointment_id: str | None) -> dict | None:
    specialty = primary.get("specialty", "").strip().lower()
    if not specialty:
        return None
    hours = next(
        (row for row in _rows("Hospital_Hours", cached=True)
         if row.get("day_of_week", "").strip().lower() == requested_dt.strftime("%A").lower()),
        None,
    )
    for peer in _active_doctors():
        if peer.get("doctor_id") == primary.get("doctor_id"):
            continue
        if peer.get("specialty", "").strip().lower() != specialty:
            continue
        duration = _doctor_duration(service, peer)
        if _doctor_works(peer, requested_dt, duration):
            if (
                not hours
                or hours.get("is_open", "").strip().upper() != "YES"
                or requested_dt.time() < _time(hours.get("open_time", "00:00"))
                or (requested_dt + dt.timedelta(minutes=duration)).time() > _time(hours.get("close_time", "23:59"))
            ):
                continue
            if not _doctor_has_collision(peer.get("doctor_id", ""), requested_dt, duration, exclude_appointment_id):
                return peer
    return None


def _doctor_has_collision(doctor_id: str, requested_dt: dt.datetime, duration: int,
                          exclude_appointment_id: str | None = None) -> bool:
    end_dt = requested_dt + dt.timedelta(minutes=duration)
    for row in _rows(APPOINTMENTS_TAB, cached=False):
        if row.get("status", "").strip().upper() != "CONFIRMED":
            continue
        if row.get("appointment_date") != requested_dt.date().isoformat():
            continue
        if row.get("appointment_id") == exclude_appointment_id or row.get("doctor_id") != doctor_id:
            continue
        if requested_dt.strftime("%H:%M") < row.get("end_time", "00:00") and row.get("start_time", "99:99") < end_dt.strftime("%H:%M"):
            return True
    return False


def _peer_failover(service: dict, primary: dict, requested_dt: dt.datetime,
                   exclude_appointment_id: str | None) -> dict | None:
    peer = _available_peer(service, primary, requested_dt, exclude_appointment_id)
    if not peer:
        return None
    duration = _doctor_duration(service, peer)
    return {
        "original_doctor": primary,
        "peer_doctor": peer,
        "peer_duration_mins": duration,
    }


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
    doctor_id: str | None = None,
    *,
    cleanup: bool = True,
) -> dict:
    if cleanup:
        purge_expired_appointments()
    service, resolved_doctor = resolve_service_and_doctor(service_name)
    if not service:
        available = [row.get("service_name", "") for row in _rows("Services_and_Pricing", cached=True) if row.get("service_name")]
        return {"available": False, "reason": "That service is not listed.", "suggestions": [], "available_services": available}
    if not _service_is_enabled(service):
        return {"available": False, "reason": f"{service['service_name']} is currently unavailable.", "suggestions": []}

    day = requested_dt.strftime("%A")
    hours = next((row for row in _rows("Hospital_Hours", cached=True)
                  if row.get("day_of_week", "").strip().lower() == day.lower()), None)
    explicit_doctor = _doctor_from_text(service_name)
    explicit_binding = explicit_doctor is not None or doctor_id is not None
    doctor = (
        next((row for row in _active_doctors() if row.get("doctor_id") == doctor_id), None)
        if doctor_id else resolved_doctor
    )
    duration = _doctor_duration(service, doctor)
    if doctor and not _doctor_works(doctor, requested_dt, duration):
        if explicit_binding:
            return {"available": False, "reason": "That time is outside the doctor's configured schedule.", "suggestions": []}
        peer = _available_peer(service, doctor, requested_dt, exclude_appointment_id)
        if peer:
            doctor = peer
            duration = _doctor_duration(service, doctor)
        else:
            return {"available": False, "reason": "No doctor in that specialty is working at that time.", "suggestions": []}
    end_dt = requested_dt + dt.timedelta(minutes=duration)
    if not hours or hours.get("is_open", "").strip().upper() != "YES":
        return {"available": False, "reason": f"The clinic is closed on {day}.", "suggestions": []}
    if requested_dt.time() < _time(hours["open_time"]) or end_dt.time() > _time(hours["close_time"]):
        return {"available": False, "reason": "That time is outside the clinic's configured hours.", "suggestions": []}

    selected_doctor_id = doctor.get("doctor_id", "").strip() if doctor else ""
    if doctor:
        if doctor.get("status", "").strip().upper() != "ACTIVE":
            return {"available": False, "reason": "The assigned doctor is not available.", "suggestions": []}
        if not _doctor_works(doctor, requested_dt, duration):
            return {"available": False, "reason": "That time is outside the doctor's configured schedule.", "suggestions": []}

    collisions = []
    for row in _rows(APPOINTMENTS_TAB, cached=False):
        if row.get("status", "").strip().upper() != "CONFIRMED":
            continue
        if row.get("appointment_date") != requested_dt.date().isoformat():
            continue
        if row.get("appointment_id") == exclude_appointment_id:
            continue
        if doctor and row.get("doctor_id") != selected_doctor_id:
            continue
        if not doctor and row.get("service_name", "").strip().lower() != service["service_name"].strip().lower():
            continue
        if row.get("start_time", "99:99") < end_dt.strftime("%H:%M") and row.get("end_time", "00:00") > requested_dt.strftime("%H:%M"):
            collisions.append(row)
    if collisions:
        failover = _peer_failover(service, doctor, requested_dt, exclude_appointment_id) if doctor else None
        if failover:
            peer = failover["peer_doctor"]
            peer_end = requested_dt + dt.timedelta(minutes=failover["peer_duration_mins"])
            if not explicit_binding:
                return {
                    "available": True,
                    "service": service,
                    "doctor": peer,
                    "fee": _number(peer.get("consultation_fee")),
                    "duration_mins": failover["peer_duration_mins"],
                    "prep_instructions": service.get("prep_instructions", ""),
                }
            return {
                "available": False,
                "reason": "That slot is already booked.",
                "suggestions": [],
                "failover": {
                    "original_doctor": doctor,
                    "peer_doctor": peer,
                    "peer_duration_mins": failover["peer_duration_mins"],
                    "peer_end": peer_end,
                },
            }
        suggestions = []
        candidate = end_dt
        for _ in range(12):
            result = check_slot_availability(
                service_name, candidate, exclude_appointment_id,
                doctor_id=doctor.get("doctor_id") if explicit_binding and doctor else None,
                cleanup=False,
            )
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
        spreadsheetId=sheet_id, range=f"'{APPOINTMENTS_TAB}'!A:N",
        valueInputOption="RAW", insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()


def _appointment_rows() -> list[dict]:
    return _rows(APPOINTMENTS_TAB, cached=False)


def parse_sheet_datetime(date_str: str, time_str: str) -> dt.datetime:
    """Parse appointment date/time values as timezone-aware IST."""
    date_value = str(date_str or "").strip()
    parsed_date = dt.date.fromisoformat(date_value)
    parsed_time = _time(time_str)
    return dt.datetime.combine(parsed_date, parsed_time, tzinfo=IST)


def _appointment_end(row: dict) -> dt.datetime | None:
    try:
        return parse_sheet_datetime(row.get("appointment_date", ""), row.get("end_time", ""))
    except (TypeError, ValueError):
        try:
            start = parse_sheet_datetime(row.get("appointment_date", ""), row.get("start_time", ""))
            service = interactive_service_for_name(row.get("service_name", "")) or {}
            doctor = _lookup_doctor(row.get("doctor_name", "")) if row.get("doctor_name") else None
            return start + dt.timedelta(minutes=_doctor_duration(service, doctor))
        except (TypeError, ValueError):
            logger.warning("Unable to parse appointment end time for %s", row.get("appointment_id", "unknown"))
            return None


def purge_expired_appointments() -> int:
    """Delete expired and cancelled appointment rows bottom-up."""
    rows = _appointment_rows()
    now = dt.datetime.now(IST)
    expired_rows = []
    for sheet_row, row in enumerate(rows, start=2):
        end = _appointment_end(row)
        if row.get("status", "").strip().upper() == "CANCELLED" or (end and end <= now):
            expired_rows.append(sheet_row)
    if not expired_rows:
        return 0
    service, sheet_id = _sheets_service()
    metadata = service.spreadsheets().get(
        spreadsheetId=sheet_id,
        fields="sheets(properties(sheetId,title))",
    ).execute()
    sheet = next(
        sheet for sheet in metadata.get("sheets", [])
        if sheet.get("properties", {}).get("title") == APPOINTMENTS_TAB
    )
    sheet_id_value = sheet["properties"]["sheetId"]
    requests = [
        {
            "deleteDimension": {
                "range": {
                    "sheetId": sheet_id_value,
                    "dimension": "ROWS",
                    "startIndex": row - 1,
                    "endIndex": row,
                }
            }
        }
        for row in sorted(expired_rows, reverse=True)
    ]
    service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id, body={"requests": requests},
    ).execute()
    _clear_cache()
    return len(expired_rows)


def delete_appointment(appointment_id: str) -> dict:
    """Delete one appointment row from the live worksheet."""
    rows = _appointment_rows()
    row_index = next(
        (index for index, row in enumerate(rows, start=2)
         if row.get("appointment_id") == appointment_id),
        None,
    )
    if row_index is None:
        raise ValueError("Appointment not found.")
    service, sheet_id = _sheets_service()
    metadata = service.spreadsheets().get(
        spreadsheetId=sheet_id,
        fields="sheets(properties(sheetId,title))",
    ).execute()
    sheet = next(
        sheet for sheet in metadata.get("sheets", [])
        if sheet.get("properties", {}).get("title") == APPOINTMENTS_TAB
    )
    service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{
            "deleteDimension": {
                "range": {
                    "sheetId": sheet["properties"]["sheetId"],
                    "dimension": "ROWS",
                    "startIndex": row_index - 1,
                    "endIndex": row_index,
                }
            }
        }]},
    ).execute()
    _clear_cache()
    return next(row for index, row in enumerate(rows, start=2) if index == row_index)


def create_appointment(phone: str, patient_name: str, service_name: str,
                       requested_dt: dt.datetime, calendar_event_id: str | None = None,
                       doctor_id: str | None = None) -> dict:
    result = check_slot_availability(service_name, requested_dt, doctor_id=doctor_id)
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
    purge_expired_appointments()
    now = dt.datetime.now(IST)
    return [row for row in _appointment_rows()
            if normalize_phone(row.get("phone_number")) == normalize_phone(phone)
            and row.get("status", "").upper() == "CONFIRMED"
            and (_appointment_end(row) is not None and _appointment_end(row) > now)]


def has_appointment_history(phone: str) -> bool:
    purge_expired_appointments()
    normalized = normalize_phone(phone)
    return any(
        normalize_phone(row.get("phone_number")) == normalized
        for row in _appointment_rows()
    )


def get_appointment(appointment_id: str) -> dict | None:
    purge_expired_appointments()
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
        spreadsheetId=sheet_id, range=f"'{APPOINTMENTS_TAB}'!A{index}:N{index}",
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
