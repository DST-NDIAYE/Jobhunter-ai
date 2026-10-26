"""Tests SQLite hors ligne : chaque test dispose d'un fichier temporaire."""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from app.database import add_job, get_jobs, init_db, job_exists


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db_path = Path(self.directory.name) / "data" / "jobs.db"
        init_db(self.db_path)

    def test_database_persists_and_initialization_preserves_jobs(self):
        self.assertTrue(add_job({"external_id": "123", "title": "Développeur"}, self.db_path))
        init_db(self.db_path)

        # Chaque fonction ouvre une nouvelle connexion au même fichier.
        self.assertTrue(job_exists("123", db_path=self.db_path))
        jobs = get_jobs(self.db_path)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["title"], "Développeur")
        self.assertIsInstance(jobs[0]["id"], int)
        self.assertEqual(jobs[0]["external_id"], "123")
        created_at = datetime.fromisoformat(jobs[0]["created_at"].replace("Z", "+00:00"))
        self.assertIsNotNone(created_at.tzinfo)

    def test_duplicate_does_not_overwrite_original_offer(self):
        self.assertTrue(add_job({"external_id": "ABC", "title": "Initial"}, self.db_path))
        self.assertFalse(add_job({"external_id": "ABC", "title": "Modifié"}, self.db_path))
        self.assertEqual([job["title"] for job in get_jobs(self.db_path)], ["Initial"])

    def test_same_external_id_in_another_source_is_distinct(self):
        add_job({"external_id": "ABC"}, self.db_path)
        self.assertFalse(job_exists("ABC", source="other", db_path=self.db_path))
        self.assertTrue(add_job({"external_id": "ABC", "source": "other"}, self.db_path))
        self.assertTrue(job_exists("ABC", source="other", db_path=self.db_path))
        self.assertEqual(len(get_jobs(self.db_path)), 2)

    def test_optional_fields_and_metadata_round_trip(self):
        offer = {
            "external_id": "FULL",
            "title": "Analyste données",
            "company": "Exemple",
            "location": "Paris",
            "contract_type": "CDI",
            "description": "SQL, Python et analyse.",
            "publication_date": "2026-01-10T10:00:00Z",
            "url": "https://example.com/offres/FULL",
            "salary": {"libelle": "Annuel de 40000.0 Euros à 45000.0 Euros", "commentaire": "Selon expérience"},
            "experience": "Débutant accepté",
            "skills": [{"code": "123", "libelle": "Analyser des données", "exigence": "S"}],
        }
        add_job(offer, self.db_path)
        stored = get_jobs(self.db_path)[0]
        for key, value in offer.items():
            self.assertEqual(stored[key], value)

        add_job({"external_id": "MINIMAL"}, self.db_path)
        minimal = get_jobs(self.db_path)[0]
        self.assertEqual(minimal["source"], "france_travail")
        for key in ("company", "salary", "experience", "url"):
            self.assertIsNone(minimal[key])
        self.assertEqual(minimal["skills"], [])

    def test_missing_or_blank_identity_is_rejected(self):
        for external_id in (None, "", "   ", 123):
            with self.subTest(external_id=external_id):
                with self.assertRaises(ValueError):
                    add_job({"external_id": external_id}, self.db_path)
        with self.assertRaises(ValueError):
            add_job({"id": "API-ID"}, self.db_path)
        self.assertEqual(get_jobs(self.db_path), [])

    def test_limit_offset_and_stable_order(self):
        for number in range(5):
            add_job({"external_id": str(number)}, self.db_path)
        page = get_jobs(self.db_path, limit=2, offset=1)
        self.assertEqual([job["external_id"] for job in page], ["3", "2"])
        self.assertEqual(get_jobs(self.db_path, offset=10), [])
        for kwargs in ({"limit": 0}, {"limit": -1}, {"limit": True}, {"offset": -1}, {"offset": 1.5}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    get_jobs(self.db_path, **kwargs)

    def test_identifier_is_a_sql_parameter(self):
        external_id = "x'); DROP TABLE jobs; --"
        self.assertTrue(add_job({"external_id": external_id}, self.db_path))
        self.assertTrue(job_exists(external_id, db_path=self.db_path))
        self.assertFalse(job_exists("missing", db_path=self.db_path))
        self.assertEqual(len(get_jobs(self.db_path)), 1)


if __name__ == "__main__":
    unittest.main()
