"""Tests pour l'API REST JobHunter AI (routes front-end)."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import Settings
from app.database import add_job, get_jobs, init_db
from app.france_travail import FranceTravailError
from app.main import app
from app.matching import cv_text_from_master

FIXTURE = Path(__file__).parent / "fixtures" / "cv_master_fictif.yaml"


class APIRestTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.db_path = Path(self.dir.name) / "jobs.db"

        init_db(self.db_path)
        self.settings = Settings(
            database_path=self.db_path,
            france_travail_client_id="dummy_id",
            france_travail_client_secret="dummy_secret",
        )
        for target in ("app.config.get_settings", "app.api.get_settings"):
            p = patch(target, return_value=self.settings)
            p.start()
            self.addCleanup(p.stop)

        self.client = TestClient(app)

    def test_root_and_docs(self):
        self.assertEqual(self.client.get("/", headers={"accept": "text/html"}).status_code, 200)
        self.assertEqual(self.client.get("/").json()["docs"], "/docs")
        self.assertEqual(self.client.get("/docs").status_code, 200)

    def test_status_endpoint_has_no_contact_or_secret(self):
        with patch("app.api.MASTER_PATH", FIXTURE):
            data = self.client.get("/api/status").json()
        self.assertTrue(data["ok"] and data["master_ready"] and data["ft_configured"])
        self.assertEqual(data["llm_provider"], "local")
        for key in ("candidate_name", "candidate_email", "candidate_phone", "gmail_user", "cv_path"):
            self.assertNotIn(key, data)
        self.assertNotIn("dummy_secret", str(data))

    def test_removed_routes_are_gone(self):
        for method, path in (("post", "/api/apply/single"), ("post", "/api/gmail/dispatch"), ("post", "/api/gmail/test"),
                             ("post", "/api/leads/hunt"), ("post", "/api/letter/generate"), ("post", "/api/autopilot/run"),
                             ("get", "/api/scheduler"), ("get", "/api/profile"), ("get", "/jobs"), ("get", "/jobs/match")):
            with self.subTest(path=path):
                self.assertIn(getattr(self.client, method)(path).status_code, (404, 405))

    @patch("app.api.search_jobs")
    def test_search_is_scored_with_the_master_and_saved(self, search):
        search.return_value = [
            {"external_id": "1", "source": "france_travail", "title": "Boulanger", "description": "Pain et viennoiseries"},
            {"external_id": "2", "source": "france_travail", "title": "Data Scientist",
             "description": "Python Scikit-learn classification LangChain FAISS. Contact : rh@exemplia.fr"}]
        with patch("app.api.cv_text_from_master", return_value=cv_text_from_master(FIXTURE)):
            jobs = self.client.get("/api/jobs/search", params={"keyword": "Data", "limit": 5}).json()
        self.assertEqual([j["external_id"] for j in jobs], ["2", "1"])
        self.assertGreater(jobs[0]["match_score"], jobs[1]["match_score"])
        self.assertEqual(jobs[0]["contact_email"], "rh@exemplia.fr")
        self.assertEqual(len(get_jobs(self.db_path)), 2)

    @patch("app.api.search_jobs", side_effect=FranceTravailError("France Travail indisponible.", status_code=502))
    def test_search_error_is_a_clear_json_response(self, search):
        res = self.client.get("/api/jobs/search")
        self.assertEqual(res.status_code, 502)
        self.assertEqual(res.json()["detail"], "France Travail indisponible.")

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

    def test_gmail_status_only(self):
        self.assertEqual(self.client.get("/api/gmail/status").json(), {"configured": False, "method": None})
