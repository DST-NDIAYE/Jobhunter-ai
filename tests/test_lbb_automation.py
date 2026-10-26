import unittest
from unittest.mock import MagicMock
from pathlib import Path

from app.lbb_automation import (
    sanitize_lbb_message,
    get_logged_in_user,
    submit_lbb_application_on_page,
    LBBAutomationError,
)


class TestLBBAutomation(unittest.TestCase):
    def test_sanitize_lbb_message(self):
        msg = "Bonjour 🙂👍 ! Voici ma candidature pour le poste de Data Engineer."
        cleaned = sanitize_lbb_message(msg)
        self.assertNotIn("🙂", cleaned)
        self.assertNotIn("👍", cleaned)
        self.assertIn("Bonjour", cleaned)
        self.assertIn("Data Engineer", cleaned)

    def test_get_logged_in_user_false_on_login_inputs(self):
        page = MagicMock()
        page.url = "https://labonneboite.francetravail.fr/candidature/nouvelle/12345678901234?job=M1811"
        # Mock evaluate returning ok: false because hasLoginInputs was true
        page.evaluate.return_value = {"ok": False, "is_login": True}
        user = get_logged_in_user(page, navigate_if_needed=False)
        self.assertIsNone(user)

    def test_get_logged_in_user_true_when_authenticated(self):
        page = MagicMock()
        page.url = "https://labonneboite.francetravail.fr/candidature/nouvelle/12345678901234?job=M1811"
        page.evaluate.return_value = {"ok": True, "name": "Serigne Bassirou NDIAYE"}
        user = get_logged_in_user(page, navigate_if_needed=False)
        self.assertIsNotNone(user)
        self.assertEqual(user["firstname"], "Serigne Bassirou NDIAYE")

    def test_invalid_siret_raises_error(self):
        page = MagicMock()
        with self.assertRaises(LBBAutomationError):
            submit_lbb_application_on_page(
                page,
                siret="invalid_siret",
                rome="M1811",
                job_label="Data engineer",
                message="Bonjour",
                cv_path=Path("CV_Serigne.pdf"),
            )

    def test_submit_dry_run_success(self):
        page = MagicMock()
        page.url = "https://labonneboite.francetravail.fr/candidature/nouvelle/38103128500590?job=M1811"
        page.locator.return_value.count.return_value = 1
        page.evaluate.return_value = {"ok": True, "name": "Serigne Bassirou NDIAYE"}

        res = submit_lbb_application_on_page(
            page,
            siret="38103128500590",
            rome="M1811",
            job_label="Data engineer",
            message="Bonjour, je souhaite postuler.",
            cv_path=Path("CV_Serigne.pdf"),
            dry_run=True,
        )
        self.assertEqual(res["status"], "PREPARATION_READY")
        self.assertIn("rempli avec votre CV", res["message"])

    def test_submit_auto_send_success_when_sent_url(self):
        page = MagicMock()
        page.url = "https://labonneboite.francetravail.fr/candidature/envoyee/38103128500590"
        page.locator.return_value.count.return_value = 1
        page.evaluate.return_value = {"ok": True, "name": "Serigne Bassirou NDIAYE"}

        res = submit_lbb_application_on_page(
            page,
            siret="38103128500590",
            rome="M1811",
            job_label="Data engineer",
            message="Bonjour, je souhaite postuler.",
            cv_path=Path("CV_Serigne.pdf"),
            dry_run=False,
        )
        self.assertEqual(res["status"], "SENT")
        self.assertIn("envoyée", res["message"])


if __name__ == "__main__":
    unittest.main()

