"""Tests pour l'API REST JobHunter AI (routes front-end)."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import Settings
from app.database import init_db, add_job
from app.main import app


class APIRestTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.db_path = Path(self.dir.name) / "jobs.db"
        self.cv_path = Path(self.dir.name) / "cv.pdf"
        self.cv_path.write_bytes(b"%PDF-1.4 test")

        init_db(self.db_path)
        self.settings = Settings(
            database_path=self.db_path,
            cv_path=self.cv_path,
            france_travail_client_id="dummy_id",
            france_travail_client_secret="dummy_secret",
        )
        for target in ("app.config.get_settings", "app.main.get_settings", "app.api.get_settings"):
            p = patch(target, return_value=self.settings)
            p.start()
            self.addCleanup(p.stop)

        self.client = TestClient(app)

    def test_status_endpoint(self):
        res = self.client.get("/api/status")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["cv_ready"])
        self.assertTrue(data["ft_configured"])

    def test_stats_and_applications_lifecycle(self):
        # 1. Add a dummy job
        add_job({
            "external_id": "test-123",
            "source": "france_travail",
            "title": "Data Scientist Senior",
            "company": "Tech Corp",
            "location": "Paris",
            "description": "Python ML",
        }, db_path=self.db_path)

        # 2. Check stats initial
        res = self.client.get("/api/stats")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["total_tracked"], 1)

        # 3. Update status to Entretien
        patch_res = self.client.patch(
            "/api/applications/france_travail/test-123",
            json={"status": "Entretien", "personal_notes": "Entretien RH prévu jeudi"},
        )
        self.assertEqual(patch_res.status_code, 200)
        self.assertTrue(patch_res.json()["ok"])

        # 4. Check stats updated
        stats_data = self.client.get("/api/stats").json()
        self.assertEqual(stats_data["interview_count"], 1)

        # 5. List applications
        apps = self.client.get("/api/applications").json()
        self.assertEqual(len(apps), 1)
        self.assertEqual(apps[0]["application_status"], "Entretien")
        self.assertEqual(apps[0]["personal_notes"], "Entretien RH prévu jeudi")

    def test_profile_endpoint(self):
        res = self.client.get("/api/profile")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("first_name", data)
