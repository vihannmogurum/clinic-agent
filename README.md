# WhatsApp Patient Intake & Appointment Agent (Demo Clinic)

A productized AI agent service for local clinics in Hyderabad. Built with FastAPI, Twilio WhatsApp Sandbox, FreeLLMAPI (OpenAI-compatible), Google Calendar API v3, and Google Sheets API v4.

## Setup Steps

### 1. Install Dependencies
```bash
python -m venv venv
venv\Scripts\activate  # Windows
# source venv/bin/activate  # Linux/Mac
pip install -r requirements.txt
```

### 2. Configure Environment
```bash
cp .env.example .env
# Edit .env with your credentials:
# - Twilio: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_NUMBER
# - FreeLLMAPI: FREE_LLM_API_KEY, FREE_LLM_BASE_URL
# - Google: GOOGLE_CREDENTIALS_PATH, GOOGLE_CALENDAR_ID, SHEET_ID
# - Clinic: OWNER_WHATSAPP_NUMBER
```

### 3. Google Cloud Setup
1. Create a Google Cloud project
2. Enable **Google Calendar API** and **Google Sheets API**
3. Create a service account and download `credentials.json` to project root
4. Create a Google Sheet named "Patient Intake Log" and share with service account email
5. Create a Google Calendar named "Demo Clinic" and share with service account email
6. Copy the Sheet ID and Calendar ID to `.env`

### 4. Run the App
```bash
venv\Scripts\activate
uvicorn main:app --reload --port 8000
```

### 5. Test Locally
```bash
# Web chat demo
open http://127.0.0.1:8000/demo

# Direct booking page
open http://127.0.0.1:8000/book

# Test API
curl -X POST http://127.0.0.1:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "I want to book a General Consultation for today at 3 PM, name is John", "session_id": "test"}'
```

### 6. Twilio WhatsApp (Optional)
```bash
# Start ngrok tunnel
ngrok http 8000

# Configure Twilio Sandbox webhook:
# POST https://<ngrok-url>/webhook/whatsapp
```

## Project Structure
```
clinic-agent/
├── main.py                 # FastAPI app, webhook, /chat endpoint
├── llm.py                  # LLM client with robust fallbacks
├── calendar_service.py     # Google Calendar API wrapper
├── sheets_service.py       # Google Sheets API wrapper
├── whatsapp.py             # Twilio WhatsApp wrapper
├── state.py                # In-memory conversation state
├── requirements.txt
├── .env.example
├── data/
│   ├── clinic_faq.md       # Clinic FAQ (pricing, hours, prep)
│   └── unanswered_questions.log
├── static/
│   ├── widget.js           # Embeddable chat widget
│   ├── index.html          # Demo clinic website
│   └── book.html           # Full-screen booking page
└── tests/
    └── test_agent.py       # Unit tests
```

## Features
- **WhatsApp + Web chat** via `/webhook/whatsapp` and `/chat` endpoints
- **Single-turn booking**: "I want to book a General Consultation for today at 3 PM, name is John"
- **Multi-step booking**: Step-by-step name → service → slot collection
- **FAQ answering**: Pricing, hours, prep instructions from `data/clinic_faq.md`
- **Google Calendar** integration with IST timezone (correct 9 AM, 3 PM, etc.)
- **Google Sheets** intake logging
- **Robust LLM fallbacks**: Keywords → Regex → Local FAQ → LLM (no rate limit crashes)
- **Embeddable widget** (`/static/widget.js`) for any website
- **Direct booking page** (`/book`) for Instagram bio / Google Maps
- **Demo clinic site** (`/demo`) with embedded widget

## Tech Stack
- FastAPI + Uvicorn
- FreeLLMAPI (OpenAI-compatible) via `openai` SDK
- Google Calendar API v3 + Sheets API v4
- Twilio WhatsApp Sandbox
- Vanilla JS widget (zero dependencies)