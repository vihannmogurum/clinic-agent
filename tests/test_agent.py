"""
Unit tests for Demo Clinic WhatsApp Agent.
- Tests consent flow via FastAPI TestClient
- Tests FAQ answering against clinic_faq.md grounding
"""

import unittest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

import main
import llm

client = TestClient(main.app)

class TestClinicAgent(unittest.TestCase):

    def setUp(self):
        # Clear in-memory state before each test
        main.state.STATE.clear()

    @patch("whatsapp.send_message")
    def test_consent_flow(self, mock_send):
        # 1. First message without consent -> receives consent prompt
        response = client.post("/webhook/whatsapp", data={
            "From": "whatsapp:+919876543210",
            "Body": "Hi, I want to know about pricing",
            "MessageSid": "SM12345"
        })
        self.assertEqual(response.status_code, 200)
        mock_send.assert_called_once()
        args, _ = mock_send.call_args
        self.assertIn("Before we proceed, please confirm", args[1])

        # 2. Reply YES -> receives welcome / help prompt
        mock_send.reset_mock()
        response = client.post("/webhook/whatsapp", data={
            "From": "whatsapp:+919876543210",
            "Body": "YES",
            "MessageSid": "SM12346"
        })
        self.assertEqual(response.status_code, 200)
        mock_send.assert_called_once()
        args, _ = mock_send.call_args
        self.assertIn("Thank you for confirming!", args[1])

    @patch("llm._chat")
    def test_faq_grounding_within_faq(self, mock_chat):
        # Mock Groq response to return exact pricing from faq
        mock_chat.return_value = "General Physician Consultation is ₹200."
        
        answer = llm.answer_faq("How much is a general consultation?")
        self.assertIn("₹200", answer)

    @patch(
        "llm._chat",
        return_value="NOT_IN_FAQ"
    )
    @patch("whatsapp.send_message")
    def test_faq_out_of_bounds_returns_fallback(self, mock_send, mock_chat):
        # When FAQ doesn't have the answer, agent returns NOT_IN_FAQ marker
        answer = llm.answer_faq("Do you perform open-heart surgery?")
        self.assertEqual(answer.strip(), "NOT_IN_FAQ")

if __name__ == "__main__":
    unittest.main()
