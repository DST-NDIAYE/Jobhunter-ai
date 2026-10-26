"""Tests pour les endpoints Gmail (brouillon, envoi et détection d'emails)."""

import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from app.main import app
from app.email.message_builder import extract_contact_emails
from app.config import Settings
from pathlib import Path
import tempfile
from app.database import init_db, add_job


class TestGmailFeatures(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_extract_contact_emails(self):
        text = "Pour postuler, envoyez votre CV à rh.recrutement@supertech.fr ou à jobs@startup.io !"
        emails = extract_contact_emails(text)
        self.assertIn("rh.recrutement@supertech.fr", emails)
        self.assertIn("jobs@startup.io", emails)

    def test_gmail_status_endpoint(self):
        resp = self.client.get("/api/gmail/status")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("configured", data)
        self.assertIn("user", data)
        self.assertIn("method", data)

    def test_gmail_dispatch_draft(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            init_db(db_path)
            cv_file = Path(tmpdir) / "cv.pdf"
            cv_file.write_bytes(b"%PDF-1.4\n%EOF")

            s = Settings(
                database_path=db_path,
                cv_path=cv_file,
                gmail_app_password="test-password-16c",
                gmail_user="test@gmail.com",
            )
            add_job({"external_id": "test-job-1", "source": "france_travail", "title": "Data Scientist"}, db_path)

            with patch("app.api.get_settings", return_value=s), \
                 patch("app.email.app_password_client.AppPasswordGmail.create_draft", return_value="draft-test-123"), \
                 patch("app.email.message_builder.read_cv", return_value=("cv.pdf", b"%PDF-1.4")):
                resp = self.client.post("/api/gmail/dispatch", json={
                    "external_id": "test-job-1",
                    "source": "france_travail",
                    "recipient_email": "recruteur@entreprise.com",
                    "subject": "Candidature Data Scientist",
                    "body": "Bonjour, voici ma candidature.",
                    "action": "draft"
                })
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertTrue(data["ok"])
                self.assertEqual(data["action"], "draft")
                self.assertIn("Brouillon créé avec succès", data["message"])


if __name__ == "__main__":
    unittest.main()
