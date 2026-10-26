"""Vérifications minimales : MIME local et création Gmail entièrement simulée."""

import base64
from contextlib import closing
from email import policy
from email.parser import BytesParser
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pymupdf

from app.applications import get_offer
from app.config import Settings
from app.database import add_job, init_db
from app.email.draft_service import create_application_draft, save_email_details
from app.email.gmail_client import GmailError
from app.email.message_builder import build_message


class GmailDraftChecks(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        cv = root / "CV Exemple.pdf"
        with pymupdf.open() as document:
            document.new_page().insert_text((50, 50), "CV de test")
            document.save(cv)
        self.settings = Settings(database_path=root / "jobs.db", cv_path=cv,
                                 gmail_token_file=root / "absent.json")
        init_db(self.settings.database_path)
        add_job({"external_id": "gmail-test", "title": "Data Scientist"}, self.settings.database_path)
        # Préparation enregistrée factice, sans générateur ni réseau.
        import sqlite3
        with closing(sqlite3.connect(self.settings.database_path)) as db, db:
            db.execute("UPDATE jobs SET application_status='Prête à envoyer', application_message=?",
                       ("Bonjour,\nVoici ma candidature enregistrée.\nCordialement,",))
        job = get_offer("gmail-test", db_path=self.settings.database_path)
        save_email_details(job, "recruteur@example.com", "Candidature – Data Scientist", self.settings)

    def test_mime_contains_exact_saved_text_and_only_configured_pdf(self):
        raw = build_message(recipient="recruteur@example.com", sender="moi@example.com",
                            subject="Candidature – Data Scientist", body="Texte sauvegardé",
                            cv_path=self.settings.cv_path)
        message = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw))
        self.assertEqual(message["To"], "recruteur@example.com")
        self.assertEqual(message["Subject"], "Candidature – Data Scientist")
        self.assertEqual(message.get_body().get_content().rstrip("\r\n"), "Texte sauvegardé")
        files = list(message.iter_attachments())
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].get_filename(), self.settings.cv_path.name)
        self.assertEqual(files[0].get_content(), self.settings.cv_path.read_bytes())
        with self.assertRaises(ValueError):
            build_message(recipient="a@example.com\nBcc: b@example.com", sender="moi@example.com",
                          subject="Objet", body="Texte", cv_path=self.settings.cv_path)

    def test_mock_creation_persists_ids_and_prevents_duplicate(self):
        with patch("app.email.draft_service.GmailClient") as factory:
            client = factory.return_value
            client.connected_email.return_value = "moi@example.com"
            client.create_draft.return_value = ("draft-123", "message-456")
            self.assertEqual(create_application_draft("gmail-test", "france_travail", self.settings), "draft-123")
            with self.assertRaisesRegex(ValueError, "existe déjà"):
                create_application_draft("gmail-test", "france_travail", self.settings)
            client.create_draft.assert_called_once()
            raw = client.create_draft.call_args.args[0]
            message = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw))
            self.assertIn("candidature enregistrée", message.get_body().get_content())
        row = get_offer("gmail-test", db_path=self.settings.database_path)
        self.assertEqual(row["email_status"], "DRAFT_CREATED")
        self.assertEqual(row["gmail_draft_id"], "draft-123")
        self.assertEqual(row["gmail_message_id"], "message-456")
        self.assertEqual(row["application_status"], "Prête à envoyer")
        self.assertIsNone(row["application_sent_at"])
        self.assertIsNotNone(row["draft_created_at"])

    def test_network_uncertainty_blocks_silent_retry(self):
        with patch("app.email.draft_service.GmailClient") as factory:
            client = factory.return_value
            client.connected_email.return_value = "moi@example.com"
            client.create_draft.side_effect = GmailError("Réseau interrompu", uncertain=True)
            with self.assertRaisesRegex(GmailError, "incertain"):
                create_application_draft("gmail-test", "france_travail", self.settings)
            with self.assertRaisesRegex(ValueError, "incertain"):
                create_application_draft("gmail-test", "france_travail", self.settings)
            client.create_draft.assert_called_once()
        row = get_offer("gmail-test", db_path=self.settings.database_path)
        self.assertEqual(row["email_status"], "ERROR")
        self.assertEqual(row["gmail_draft_pending"], 1)

    def test_missing_message_or_cv_never_creates_draft(self):
        import sqlite3
        with patch("app.email.draft_service.GmailClient") as factory:
            client = factory.return_value
            client.connected_email.return_value = "moi@example.com"
            self.settings.cv_path.unlink()
            with self.assertRaisesRegex(GmailError, "CV est absent"):
                create_application_draft("gmail-test", "france_travail", self.settings)
            with closing(sqlite3.connect(self.settings.database_path)) as db, db:
                db.execute("UPDATE jobs SET application_message=''")
            with self.assertRaisesRegex(GmailError, "Préparez et enregistrez"):
                create_application_draft("gmail-test", "france_travail", self.settings)
            client.create_draft.assert_not_called()


if __name__ == "__main__":
    unittest.main()
