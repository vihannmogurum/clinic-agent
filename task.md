The webhook at `/webhook/whatsapp` still returned:
"Are you looking to book an appointment or do you have a question about our services?"
when processing:
"I want to book a General Consultation for tomorrow at 3 PM, name is Vihann" from "whatsapp:+918328635331".

Please:
1. Inspect the session state / in-memory store for "whatsapp:+918328635331" and check if an old conversational state is hijacking the router.
2. Trace the exact execution path inside `/webhook/whatsapp` to see why this message hit the `unclear` branch instead of executing `llm.extract_booking_slots()` or the booking workflow.
3. Add explicit debug logs:
   `[DEBUG] Phone: <phone> | State: <state> | Message: <msg> | Intent: <intent>`
4. Fix the routing so that whenever "book", "consultation", or "appointment" is detected—or when slots are present—it extracts the slots, executes the booking, updates Google Calendar and Google Sheets, and updates state accordingly.
5. Provide a way or route/command to reset or clear session state for a phone number.
6. Verify the fix by executing this exact PowerShell simulation against the running server and confirm the returned TwiML XML contains the booking confirmation:

$body = @{
    From        = "whatsapp:+918328635331"
    To          = "whatsapp:+17372508034"
    Body        = "I want to book a General Consultation for tomorrow at 3 PM, name is Vihann"
    ProfileName = "Vihann"
}

$reply = Invoke-RestMethod -Uri "http://127.0.0.1:8000/webhook/whatsapp" `
  -Method Post `
  -ContentType "application/x-www-form-urlencoded" `
  -Body $body

$reply.Response.Message