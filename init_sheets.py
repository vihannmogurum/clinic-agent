"""Create and verify the clinic's Google Sheets worksheet tabs."""

from dotenv import load_dotenv
import appointment_store

load_dotenv(override=True)


def _column_name(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def main() -> None:
    service, sheet_id = appointment_store._sheets_service()
    metadata = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    existing = {sheet["properties"]["title"] for sheet in metadata.get("sheets", [])}
    requests = [
        {"addSheet": {"properties": {"title": tab}}}
        for tab in appointment_store.HEADERS
        if tab not in existing
    ]
    if requests:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id, body={"requests": requests}
        ).execute()

    for tab, headers in appointment_store.HEADERS.items():
        values = service.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=f"'{tab}'!A:Z"
        ).execute().get("values", [])
        if not values:
            service.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range=f"'{tab}'!A1:{_column_name(len(headers))}1",
                valueInputOption="RAW",
                body={"values": [headers]},
            ).execute()
        else:
            existing_headers = [str(value).strip() for value in values[0]]
            missing_headers = [header for header in headers if header not in existing_headers]
            if missing_headers:
                updated_headers = existing_headers + missing_headers
                end_column = _column_name(len(updated_headers))
                service.spreadsheets().values().update(
                    spreadsheetId=sheet_id,
                    range=f"'{tab}'!A1:{end_column}1",
                    valueInputOption="RAW",
                    body={"values": [updated_headers]},
                ).execute()
    print(f"Google Sheets connection verified: {sheet_id}")
    print("Worksheet tabs ready: " + ", ".join(appointment_store.HEADERS))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        raise SystemExit(f"Google Sheets setup failed: {error}")
