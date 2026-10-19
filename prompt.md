# Build: WhatsApp Patient Intake & Appointment Agent (Prototype)

## Context

I'm building a productized AI agent service for local clinics in Hyderabad. This is the **first prototype** — a working demo for a fictional "Demo Clinic" that I'll use to record a 45-second sales video and pitch to real clinics. Everything must run on free tiers. No paid services, no n8n, no visual workflow tools — pure Python so I understand and can debug every part of it.

## What to build

A FastAPI backend that receives WhatsApp messages (via Twilio's WhatsApp Sandbox) and runs this flow:

1. **Receive message** → Twilio webhook hits `POST /webhook/whatsapp`.
2. **Consent check** → If this is a new conversation (no prior message from this number), reply with a consent prompt before collecting any data: *"Hi! Before we proceed, please confirm you agree to share your details with Demo Clinic for appointment purposes. Reply YES to continue."* Do not proceed until they reply YES.
3. **Intent routing** → After consent, classify the incoming message as one of: `faq_question`, `book_appointment`, or `unclear`. Use a simple Groq API call with a system prompt for this classification — keep it cheap and fast.
4. **FAQ handling** → If `faq_question`, answer using ONLY the contents of `data/clinic_faq.md` (a local file holding Demo Clinic's services, pricing, hours, and prep instructions). The system prompt must instruct the model to never invent information not in that file, and if the answer isn't in the FAQ, reply: "Let me connect you with our team on that — they'll follow up shortly," and log the unanswered question to `data/unanswered_questions.log`.
5. **Appointment booking** → If `book_appointment`, run a short structured conversation to collect: patient name, phone number (from the WhatsApp sender ID), service requested, and preferred date/time. Once all four are collected:
   - Create a Google Calendar event (30-minute slot) using the Calendar API.
   - Append a row to a Google Sheet (`Patient Intake Log`) with: timestamp, name, phone, service, slot, status.
   - Reply to the patient with a confirmation message including the date/time.
   - Send a separate WhatsApp message to the clinic owner's number (from env var `OWNER_WHATSAPP_NUMBER`) with a short alert: new booking, patient name, service, slot.
6. **Unclear** → If the message doesn't fit either category, reply asking the patient to clarify (e.g. "Are you looking to book an appointment or do you have a question?").

## Tech constraints

- **Language:** Python 3.11+, FastAPI, async where it matters (webhook handler, API calls).
- **WhatsApp:** Twilio WhatsApp Sandbox (free tier). Use the `twilio` Python SDK to send messages; incoming messages arrive as form-encoded POST to the webhook per Twilio's spec.
- **LLM:** Groq API (`groq` Python SDK), model `llama-3.1-70b-versatile` or current equivalent free-tier model. Read the API key from env var `GROQ_API_KEY`.
- **Calendar:** Google Calendar API v3, service account or OAuth (pick whichever is simpler to set up locally — explain the tradeoff in a comment). Read credentials path from env var `GOOGLE_CREDENTIALS_PATH`.
- **Sheets:** Google Sheets API v4, same credential approach as Calendar. Read spreadsheet ID from env var `SHEET_ID`.
- **No database.** Conversation state (consent status, in-progress booking collection) can live in an in-memory dict keyed by phone number for this prototype — note clearly in a comment that this won't survive a server restart and is fine only for a demo.
- **Hosting target:** Will deploy later to Render.com free tier. Structure the app so it reads `PORT` from env and runs with uvicorn.

## Project structure to generate

```
clinic-agent/
├── main.py                  # FastAPI app, webhook route, request routing
├── llm.py                   # Groq client wrapper: intent classification + FAQ answering
├── calendar_service.py      # Google Calendar API wrapper (create event, check slot)
├── sheets_service.py        # Google Sheets API wrapper (append row)
├── whatsapp.py              # Twilio client wrapper (send message)
├── state.py                 # In-memory per-phone-number conversation state
├── data/
│   ├── clinic_faq.md         # Demo Clinic's services, pricing, hours, prep instructions (write realistic sample content)
│   └── unanswered_questions.log   # created at runtime, empty to start
├── .env.example              # all required env vars, no real values
├── requirements.txt
└── README.md                 # setup steps: Twilio sandbox join code, Google Cloud service account setup, Groq API key, how to run locally with ngrok for webhook testing
```

## Also do this

- Write a realistic `data/clinic_faq.md` for "Demo Clinic" — general physician + diagnostics, with made-up but plausible pricing (in ₹), hours (Mon–Sat, 9 AM–7 PM), and prep instructions for a blood test and an ultrasound (fasting requirements etc.) — this needs to look convincing in a sales demo.
- Add basic error handling: if Groq, Calendar, or Sheets API calls fail, log the error and have the agent reply with a graceful fallback message rather than crashing or leaving the patient hanging.
- Add a `tests/` folder with at least one test that mocks Twilio's incoming webhook payload and asserts the consent flow works, and one that asserts FAQ answers stay within the FAQ content (no hallucinated info).
- In the README, include the exact steps to get `ngrok` running and point Twilio's sandbox webhook at it, since that's how I'll test this locally before deploying.
- Keep the code readable and commented — I'm going to walk through this line by line to actually learn it, not just copy-paste it.

## Out of scope for this prototype

- No payment handling.
- No multi-clinic / multi-tenant support — hardcode for Demo Clinic only.
- No third-party clinic software integration (Practo etc.).
- No persistent database — in-memory state is fine for now.

## Acceptance check

I should be able to:
1. Run `uvicorn main:app --reload` locally.
2. Expose it via ngrok and point Twilio's sandbox webhook at `<ngrok-url>/webhook/whatsapp`.
3. Message the Twilio sandbox number from my phone, get the consent prompt, reply YES, ask a FAQ question and get a correct answer from `clinic_faq.md`, then book an appointment and see it land in Google Calendar and the Google Sheet, with a separate alert WhatsApp message arriving on the owner's number.
