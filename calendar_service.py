"""
Google Calendar API wrapper.

Creates a 30-minute appointment event on the Demo Clinic calendar.

AUTH TRADEOFF (PONYTAIL: picked the simpler option):
  SERVICE ACCOUNT (what this uses) vs OAUTH (user tokens).
  - Service account: one JSON key file, no browser flow, no refresh-token
    bookkeeping, works headless. Downside: the service account must be
    explicitly shared onto the calendar you want to write to.
  - OAuth: acts as the actual clinic owner, no sharing step. Downside: needs
    a browser-consent flow the first time and a refresh-token store.

For a 45-second sales demo, the service account wins: the clinic owner just
shares one calendar with one email address and the demo keeps working.
Credentials path comes from GOOGLE_CREDENTIALS_PATH env var.
"""

import datetime
import os

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar"]
CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary")


def _service():
    """Build an authenticated Calendar v3 service from the service account."""
    credentials = Credentials.from_service_account_file(
        os.environ["GOOGLE_CREDENTIALS_PATH"],
        scopes=SCOPES,
    )
    return build("calendar", "v3", credentials=credentials, cache_discovery=False)


def create_event(
    summary: str,
    start: datetime.datetime,
    duration_minutes: int = 30,
) -> dict:
    """
    Create a calendar event and return the event resource dict.

    `start` is a timezone-aware datetime; we convert it to the RFC3339 string
    the API expects. The event is created in the clinic's calendar (CALENDAR_ID,
    default "primary" — override with GOOGLE_CALENDAR_ID if the clinic uses a
    dedicated calendar).
    """
    end = start + datetime.timedelta(minutes=duration_minutes)

    event = {
        "summary": summary,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
    }

    service = _service()
    created = (
        service.events()
        .insert(calendarId=CALENDAR_ID, body=event, sendNotifications=False)
        .execute()
    )
    return created


def slot_is_available(start: datetime.datetime) -> bool:
    """
    Check whether `start` conflicts with an existing event on the clinic
    calendar, using the Calendar free/busy query.

    PONYTAIL: this is a real API call, not a stub. It is kept because a demo
    that books the same slot twice looks broken; the cost is one extra
    call per booking, which is negligible.
    """
    end = start + datetime.timedelta(minutes=30)
    service = _service()
    body = {
        "items": [{"id": CALENDAR_ID}],
        "timeMin": start.isoformat(),
        "timeMax": end.isoformat(),
    }
    result = service.freebusy().query(body=body).execute()
    busy = result.get("calendars", {}).get(CALENDAR_ID, {}).get("busy", [])
    return len(busy) == 0