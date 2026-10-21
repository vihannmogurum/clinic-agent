Create a lightweight, zero-dependency embeddable web chat widget and a `/chat` API endpoint for our clinic booking agent.

### Core Objectives:
1. **Preserve Existing Functionality:**
   - Do NOT break or modify the existing `/webhook/whatsapp` logic, Google Calendar IST timezone handling, or Google Sheets intake logging.
   - Refactor the core intent/booking brain into a shared helper if needed so both Twilio and the Web UI share the exact same booking pipeline.

2. **Add `/chat` API Endpoint in `main.py`:**
   - Method: `POST /chat`
   - Input JSON:
     ```json
     {
       "message": "string",
       "patient_name": "string (optional)",
       "phone": "string (optional)",
       "session_id": "string (optional)"
     }
     ```
   - Output JSON:
     ```json
     {
       "reply": "string",
       "booked": true/false
     }
     ```
   - Connect this directly to our slot extraction, Google Calendar sync, and Google Sheets logging.
   - Add CORS middleware so the widget can be embedded on any external domain.

3. **Serve Static Files:**
   - Mount FastAPI static files directory (`/static`).

4. **Build `static/widget.js` (Embeddable Script for Track A Clinics):**
   - Pure vanilla JavaScript (no npm, zero external libraries).
   - Injects a floating chat launcher bubble in the bottom-right corner.
   - Toggles an elegant chat window on click.
   - Manages chat history, input field, and a typing/loading indicator while awaiting the `/chat` response.
   - Uses scoped styles (inline or shadow DOM) so it does not collide with host website CSS.

5. **Build Two HTML Pages:**
   - `static/index.html`: A mock clinic website (e.g., "City Health Clinic") that embeds `<script src="/static/widget.js"></script>` to demo Track A.
   - `static/book.html`: A clean, full-screen mobile chat interface accessed directly via `/book` for Track B clinics (clinics without a website that need a direct link for their Google Maps profile or Instagram bio).

Keep it functional and minimal first. Do not over-engineer CSS frameworks; we will polish UI aesthetics in a follow-up step.