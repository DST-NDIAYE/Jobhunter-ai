"""Tests unitaires pour les fonctionnalités avancées : Chasseur de Décideurs LinkedIn."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient

from app.config import Settings
from app.database import init_db
from app.lead_hunter import build_recruiter_links, generate_lead_outreach
from app.main import app


class TestAdvancedFeatures(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.db_path = Path(self.dir.name) / "jobs.db"
        init_db(self.db_path)
        # Base temporaire et mode local : ni la vraie base ni un LLM distant ne sont sollicités.
        self.settings = Settings(database_path=self.db_path, cv_path=Path(self.dir.name) / "cv.pdf")
        for target in ("app.config.get_settings", "app.main.get_settings", "app.api.get_settings",
                       "app.lead_hunter.get_settings"):
            p = patch(target, return_value=self.settings)
            p.start()
            self.addCleanup(p.stop)

        self.client = TestClient(app)

    def test_build_recruiter_links(self):
        links = build_recruiter_links("Mistral AI", "Data Scientist")
        self.assertIn("tech_lead_url", links)
        self.assertIn("recruiter_url", links)
        self.assertIn("google_xray_url", links)
        self.assertIn("Mistral", links["tech_lead_url"])
        self.assertIn("linkedin.com", links["tech_lead_url"])

    def test_generate_lead_outreach_fallback(self):
        outreach = generate_lead_outreach("Alan", "Lead Data Scientist", "Alan recrute un Lead Data Scientist")
        self.assertIn("lead_connection_note", outreach)
        self.assertIn("lead_inmail", outreach)
        self.assertIn("recruiter_connection_note", outreach)
        self.assertIn("recruiter_inmail", outreach)
        # Vérifier que les notes respectent la limite stricte de 300 car. de LinkedIn
        self.assertTrue(len(outreach["lead_connection_note"]) <= 300)
        self.assertTrue(len(outreach["recruiter_connection_note"]) <= 300)

    def test_api_hunt_leads_route(self):
        resp = self.client.post("/api/leads/hunt", json={
            "company": "Datadog",
            "title": "Senior ML Engineer",
            "description": "Poste en observabilité et IA"
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("ok"))
        outreach = data.get("outreach", {})
        self.assertIn("tech_lead_url", outreach)
        self.assertIn("lead_connection_note", outreach)


if __name__ == "__main__":
    unittest.main()
