"""Tests unitaires pour l'automatisation des candidatures France Travail."""

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from app.config import Settings
from app.database import init_db, add_job
from app.applications import get_offer
from app.ft_automation import (
    FTAutomationError,
    FTOfferExpiredError,
    ensure_ft_authenticated,
    submit_ft_offer_on_page,
    send_single_france_travail_offer,
)


class FTAutomationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.db_path = Path(self.folder.name) / "test.db"
        self.settings = Settings(database_path=self.db_path)
        init_db(self.db_path)
        self.sleep_patch = patch("time.sleep")
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    def test_ensure_ft_authenticated_already_logged_in(self):
        page = MagicMock()
        page.url = "https://candidat.francetravail.fr/espacecandidat/"
        page.evaluate.return_value = {"ok": True, "name": "Serigne Bassirou NDIAYE"}
        user = ensure_ft_authenticated(page, login_timeout_sec=2)
        self.assertEqual(user["name"], "Serigne Bassirou NDIAYE")

    def test_submit_ft_offer_detects_expired(self):
        page = MagicMock()
        page.url = "https://candidat.francetravail.fr/offres/recherche/detail/123"
        page.locator.return_value.count.return_value = 1
        page.locator.return_value.inner_text.return_value = "Cette offre n'est plus disponible."
        with self.assertRaises(FTOfferExpiredError):
            submit_ft_offer_on_page(
                page,
                offer_url="https://candidat.francetravail.fr/offres/recherche/detail/123",
                external_id="123",
                cv_path=Path("dummy.pdf"),
            )

    def test_submit_ft_offer_detects_already_sent(self):
        page = MagicMock()
        page.url = "https://candidat.francetravail.fr/offres/recherche/detail/123"
        page.locator.return_value.count.return_value = 1
        page.locator.return_value.inner_text.return_value = "Vous avez déjà postulé à cette offre."
        res = submit_ft_offer_on_page(
            page,
            offer_url="https://candidat.francetravail.fr/offres/recherche/detail/123",
            external_id="123",
            cv_path=Path("dummy.pdf"),
        )
        self.assertEqual(res["status"], "SENT")
        self.assertIn("Vous avez déjà postulé", res["message"])

    def test_submit_ft_offer_dry_run_stops_at_send_step(self):
        page = MagicMock()
        page.url = "https://candidat.francetravail.fr/offres/recherche/detail/123"
        page.locator.return_value.count.return_value = 1
        page.locator.return_value.is_visible.return_value = True
        page.locator.return_value.inner_text.return_value = "Offre active Data Scientist NRJ GROUP"
        page.context.pages = [page]
        page.evaluate.return_value = True

        res = submit_ft_offer_on_page(
            page,
            offer_url="https://candidat.francetravail.fr/offres/recherche/detail/123",
            external_id="123",
            title="Data Scientist",
            company="NRJ",
            message="Ma lettre de motivation",
            cv_path=Path("cv.pdf"),
            auto_send=False,
            dry_run=True,
        )
        self.assertEqual(res["status"], "PREPARATION_READY")
        self.assertIn("ouverte dans Chrome", res["message"])

    def test_submit_ft_offer_auto_send_success(self):
        page = MagicMock()
        page.url = "https://candidat.francetravail.fr/offres/recherche/detail/123"
        page.context.pages = [page]
        page.evaluate.return_value = True

        body_mock = MagicMock()
        body_mock.count.return_value = 1
        body_mock.inner_text.side_effect = [
            "Offre active Data Scientist NRJ GROUP",
            "Votre candidature a été transmise avec succès au recruteur !",
        ]

        def locator_router(selector):
            if selector == "body":
                return body_mock
            loc = MagicMock()
            loc.count.return_value = 1
            loc.first = loc
            loc.is_visible.return_value = True
            loc.is_disabled.return_value = False
            return loc

        page.locator.side_effect = locator_router

        res = submit_ft_offer_on_page(
            page,
            offer_url="https://candidat.francetravail.fr/offres/recherche/detail/123",
            external_id="123",
            title="Data Scientist",
            company="NRJ",
            message="Ma lettre de motivation",
            cv_path=Path("cv.pdf"),
            auto_send=True,
            dry_run=False,
        )
        self.assertEqual(res["status"], "SENT")
        self.assertIn("envoyée avec succès", res["message"])

    def test_send_single_france_travail_offer_e2e_db_update(self):
        add_job(
            {
                "external_id": "FT999",
                "title": "Machine Learning Engineer",
                "company": "TechCorp",
                "url": "https://candidat.francetravail.fr/offres/recherche/detail/FT999",
                "application_message": "Message prêt",
                "application_letter": "Lettre prête",
            },
            self.db_path,
        )

        mock_context = MagicMock()
        mock_page = MagicMock()
        mock_context.pages = [mock_page]

        with patch("app.ft_automation.open_lbb_browser") as mock_open_browser, \
             patch("app.ft_automation.submit_ft_offer_on_page") as mock_submit:
            mock_open_browser.return_value.__enter__.return_value = mock_context
            mock_submit.return_value = {
                "status": "SENT",
                "url": "https://candidat.francetravail.fr/offres/recherche/detail/FT999",
                "message": "Candidature France Travail envoyée avec succès !",
            }

            result = send_single_france_travail_offer("FT999", self.settings, auto_send=True)
            self.assertEqual(result["status"], "SENT")
            self.assertEqual(result["channel"], "FRANCE_TRAVAIL")

        job = get_offer("FT999", "france_travail", self.db_path)
        self.assertEqual(job["application_status"], "Candidature envoyée")
        self.assertIsNotNone(job["application_sent_at"])
        self.assertEqual(job["application_channel"], "FRANCE_TRAVAIL")

        # Vérifier application_events
        with closing(sqlite3.connect(self.db_path)) as db:
            events = db.execute("SELECT event, channel FROM application_events WHERE external_id='FT999'").fetchall()
            self.assertEqual(events, [("SENT", "LABONNEBOITE")])  # logged via _log_event
