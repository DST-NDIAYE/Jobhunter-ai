"""Tests unitaires et d'intégration pour les fonctionnalités 3 et 4 :
- Fonctionnalité 3 : Hyper-personnalisation des lettres de motivation (Modern / Classic / Impact)
- Fonctionnalité 4 : Planificateur quotidien du pilote automatique matinal
"""

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import Settings
from app.database import init_db, add_job, get_jobs
from app.application_generator import template_texts, generate_preparation
from app.scheduler import load_scheduler_config, save_scheduler_config, compute_next_run
from app.main import app


class FeatureTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.db_path = Path(self.dir.name) / "jobs.db"
        self.cv_path = Path(self.dir.name) / "cv.pdf"
        self.cv_path.write_bytes(b"%PDF-1.4 Data Scientist Python PyTorch SQL NLP LLM Docker FastAPI")

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

        config_patch = patch("app.scheduler.CONFIG_PATH", Path(self.dir.name) / "scheduler_config.json")
        config_patch.start()
        self.addCleanup(config_patch.stop)

        self.cv_text_mock = patch("app.application_generator.extract_cv_text", return_value="Serigne Bassirou NDIAYE\nData Scientist\nPython PyTorch SQL Machine Learning NLP FastAPI Docker")
        self.cv_text_mock.start()
        self.addCleanup(self.cv_text_mock.stop)

        self.cv_match_mock = patch("app.api.extract_cv_text", return_value="Serigne Bassirou NDIAYE\nData Scientist\nPython PyTorch SQL Machine Learning NLP FastAPI Docker")
        self.cv_match_mock.start()
        self.addCleanup(self.cv_match_mock.stop)

        self.client = TestClient(app)

    # --- Feature 3 : Hyper-personnalisation ---
    def test_feature_3_tone_variations(self):
        job = {
            "title": "Lead Data Scientist",
            "company": "TechInnov",
            "description": "Recherche expert Python, Machine Learning et FastAPI pour industrialiser des modèles LLM et RAG.",
            "source": "france_travail",
            "skills": [{"libelle": "Python"}, {"libelle": "FastAPI"}],
        }
        cv_text = "Serigne Bassirou NDIAYE\nData Scientist\nPython PyTorch SQL Machine Learning NLP FastAPI Docker"
        analysis = {
            "common_skills": ["Python", "Machine Learning", "FastAPI"],
            "offer_skills": ["Python", "FastAPI"],
            "missing_skills": [],
            "cv_evidence": ["Python PyTorch SQL Machine Learning"],
        }

        # 1. Modern tone
        msg_modern, let_modern = template_texts(job=job, cv_text=cv_text, analysis=analysis, settings=self.settings, tone="modern")
        self.assertIn("Adepte de solutions robustes et modernes", msg_modern)
        self.assertIn("Lead Data Scientist", let_modern)
        self.assertIn("Python", let_modern)

        # 2. Classic tone
        msg_classic, let_classic = template_texts(job=job, cv_text=cv_text, analysis=analysis, settings=self.settings, tone="classic")
        self.assertIn("Cordialement", msg_classic)
        self.assertIn("Madame, Monsieur", let_classic)

        # 3. Impact tone
        msg_impact, let_impact = template_texts(job=job, cv_text=cv_text, analysis=analysis, settings=self.settings, tone="impact")
        self.assertIn("Orienté résultats et retour sur investissement", msg_impact)
        self.assertIn("contribution mesurable", let_impact)

    def test_feature_3_generate_letter_endpoint(self):
        add_job({
            "external_id": "test-ft-letter",
            "source": "france_travail",
            "title": "Ingénieur IA & NLP",
            "company": "DataCorp",
            "description": "Développement de modèles NLP et Python",
        }, db_path=self.db_path)

        res = self.client.post("/api/letter/generate", json={
            "source": "france_travail",
            "external_id": "test-ft-letter",
            "tone": "impact",
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["ok"])
        self.assertIn("preparation", data)
        self.assertEqual(data["preparation"]["context"]["tone"], "impact")
        self.assertIn("résultats", data["preparation"]["message"].lower())

    # --- Feature 4 : Planificateur quotidien ---
    def test_feature_4_scheduler_logic_and_api(self):
        # 1. Prochaine exécution
        next_iso = compute_next_run("08:30")
        self.assertIn("T08:30:00", next_iso)
        next_dt = datetime.fromisoformat(next_iso)
        self.assertGreaterEqual(next_dt, datetime.now())

        # 2. API GET /api/scheduler
        res = self.client.get("/api/scheduler")
        self.assertEqual(res.status_code, 200)
        config = res.json()
        self.assertIn("enabled", config)
        self.assertIn("time", config)

        # 3. API POST /api/scheduler (modification)
        update_res = self.client.post("/api/scheduler", json={
            "enabled": True,
            "time": "09:15",
            "max_applications": 8,
            "tone": "modern",
        })
        self.assertEqual(update_res.status_code, 200)
        updated = update_res.json()
        self.assertTrue(updated["enabled"])
        self.assertEqual(updated["time"], "09:15")
        self.assertEqual(updated["max_applications"], 8)
        self.assertIn("T09:15:00", updated["next_run"])


if __name__ == "__main__":
    unittest.main()
