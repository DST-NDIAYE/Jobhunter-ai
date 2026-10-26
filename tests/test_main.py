"""Tests des routes, avec SQLite réel et services externes simulés."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import Settings
from app.database import get_jobs
from app.france_travail import FranceTravailError
from app.main import app
from app.matching import CVError


class APITests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.settings = Settings(
            database_path=Path(directory.name) / "jobs.db",
            cv_path=Path(directory.name) / "cv.pdf",
        )
        for target in ("app.config.get_settings", "app.main.get_settings"):
            patcher = patch(target, return_value=self.settings)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = self.enterContext(TestClient(app))

    def test_root_and_openapi_available_without_credentials(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/docs").status_code, 200)
        schema = self.client.get("/openapi.json").json()
        self.assertIn("/jobs/match", schema["paths"])
        parameters = schema["paths"]["/jobs"]["get"]["parameters"]
        self.assertEqual({param["name"] for param in parameters},
                         {"keyword", "location", "contract_type", "limit"})

    @patch("app.main.search_jobs")
    def test_search_passes_filters_and_persists_without_duplicates(self, search):
        search.return_value = [{"external_id": "A", "title": "Data Scientist"}]
        params = {"keyword": "Data Scientist", "location": "Paris", "contract_type": "CDI", "limit": 20}
        for _ in range(2):
            response = self.client.get("/jobs", params=params)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()[0]["external_id"], "A")
            self.assertIsNone(response.json()[0]["salary"])
        search.assert_called_with(**params)
        self.assertEqual(len(get_jobs(self.settings.database_path)), 1)

    @patch("app.main.search_jobs")
    def test_invalid_limit_does_not_call_external_service(self, search):
        for value in (0, 151, "invalid"):
            self.assertEqual(self.client.get("/jobs", params={"limit": value}).status_code, 422)
        search.assert_not_called()

    @patch("app.main.search_jobs", return_value=[])
    def test_empty_search_is_a_json_list(self, search):
        response = self.client.get("/jobs")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    @patch("app.main.extract_cv_text", return_value="python machine learning")
    @patch("app.main.search_jobs")
    def test_matching_scores_sorts_and_saves_jobs(self, search, extract):
        search.return_value = [
            {"external_id": "unrelated", "description": "cuisine boulangerie restaurant"},
            {"external_id": "relevant", "description": "python machine learning"},
            {"external_id": "no-description"},
        ]
        response = self.client.get("/jobs/match", params={"keyword": "Data Scientist"})
        self.assertEqual(response.status_code, 200)
        jobs = response.json()
        self.assertEqual(jobs[0]["external_id"], "relevant")
        self.assertEqual([job["match_score"] for job in jobs], [100.0, 0.0, 0.0])
        self.assertEqual(len(get_jobs(self.settings.database_path)), 3)
        extract.assert_called_once_with(self.settings.cv_path)
        self.assertNotIn("cv_text", response.text)

    @patch("app.main.search_jobs")
    @patch("app.main.extract_cv_text", side_effect=CVError("CV introuvable"))
    def test_cv_error_is_actionable_and_prevents_remote_call(self, extract, search):
        response = self.client.get("/jobs/match")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json(), {"detail": "CV introuvable"})
        search.assert_not_called()

    @patch("app.main.search_jobs", side_effect=FranceTravailError("Service indisponible", status_code=503))
    def test_remote_error_is_a_safe_json_response(self, search):
        response = self.client.get("/jobs")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "Service indisponible"})


if __name__ == "__main__":
    unittest.main()
