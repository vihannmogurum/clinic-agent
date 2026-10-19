"""
Google Sheets API wrapper.

Appends one row to the "Patient Intake Log" spreadsheet so the clinic has a
record of every booking: timestamp, name, phone, service, slot, status.

Same service-account auth as calendar_service.py (see the tradeoff comment
there). The spreadsheet ID comes from the SHEET_ID env var, which the clinic
owner copies from the URL of their sheet: .../d/<SHEET_ID>/edit
"""

import os

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _service():
    """Build an authenticated Sheets v4 service from the service account."""
    credentials = Credentials.from_service_account_file(
        os.environ["GOOGLE_CREDENTIALS_PATH"],
        scopes=SCOPES,
    )
    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def append_row(values: list) -> dict:
    """
    Append `values` as a new row at the bottom of the Patient Intake Log sheet.

    `values` is a plain list, e.g. ["2026-10-18T10:30:00+05:30", "Ravi",
    "9876543210", "Consultation", "2026-10-20 11:00", "confirmed"].
    """
    sheet_id = os.environ["SHEET_ID"]
    service = _service()
    result = (
        service.spreadsheets()
        .values()
        .append(
            spreadsheetId=sheet_id,
            range="A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [values]},
        )
        .execute()
    )
    return result