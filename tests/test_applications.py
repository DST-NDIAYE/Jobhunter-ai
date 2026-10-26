"""Suivi manuel et migration additive sans réseau."""

from contextlib import closing
from pathlib import Path
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from app import applications as store
from app.config import PROJECT_ROOT, Settings
from app.database import add_job, get_jobs, init_db


def offer(external_id="1", source="france_travail"):
    return {"external_id": external_id, "source": source, "title": "Data Scientist",
            "company": "Entreprise test", "location": "Lyon", "description": "Python SQL",
            "publication_date": "2026-09-01", "keywords": ["Data"],
            "skills": [{"libelle": "Python"}], "url": "https://example.org/job/1"}


class ApplicationStorageTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "jobs.db"
        init_db(self.path)
        add_job(offer(), self.path)

    def update(self, status, **kwargs):
        return store.update_status([("france_travail", "1")], status, db_path=self.path, **kwargs)

    def test_new_offers_have_no_application(self):
        row = store.get_offer("1", db_path=self.path)
        self.assertIsNone(row["application_status"])
        self.assertEqual(row["personal_notes"], "")
        self.assertEqual(store.get_applications(db_path=self.path), [])
        self.assertEqual(store.application_stats(self.path)["total_offers"], 1)

    def test_all_statuses_persist_and_filter(self):
        for status in store.APPLICATION_STATUSES:
            self.update(status)
            rows = store.get_applications([status], self.path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["application_status"], status)
            self.assertTrue(rows[0]["status_updated_at"])
        self.assertEqual(store.get_applications(["À étudier"], self.path), [])

    def test_reapplying_status_preserves_timestamp_and_changing_status_updates_it(self):
        self.update("À étudier")
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE jobs SET status_updated_at='2000-01-01T00:00:00Z'")
        self.update("À étudier")
        self.assertEqual(store.get_offer("1", db_path=self.path)["status_updated_at"], "2000-01-01T00:00:00Z")
        self.update("À postuler")
        self.assertNotEqual(store.get_offer("1", db_path=self.path)["status_updated_at"], "2000-01-01T00:00:00Z")

    def test_prepared_and_manually_sent_dates_survive_later_status_changes(self):
        self.update("Candidature préparée")
        prepared = store.get_offer("1", db_path=self.path)
        self.assertTrue(prepared["application_prepared_at"])
        self.assertIsNone(prepared["application_sent_at"])
        self.update("Candidature envoyée")
        sent = store.get_offer("1", db_path=self.path)
        self.assertTrue(sent["application_sent_at"])
        self.update("Entretien")
        self.update("Candidature préparée")
        current = store.get_offer("1", db_path=self.path)
        self.assertEqual(current["application_prepared_at"], prepared["application_prepared_at"])
        self.assertEqual(current["application_sent_at"], sent["application_sent_at"])

    def test_notes_persist_without_creating_application_or_changing_status_date(self):
        text = "Questions : salaire ?\nContact à rappeler. '; DROP TABLE jobs; --"
        store.save_notes("1", text, db_path=self.path)
        self.assertEqual(store.get_offer("1", db_path=self.path)["personal_notes"], text)
        self.assertEqual(store.get_applications(db_path=self.path), [])
        self.update("À étudier")
        before = store.get_offer("1", db_path=self.path)["status_updated_at"]
        store.save_notes("1", "", db_path=self.path)
        after = store.get_offer("1", db_path=self.path)
        self.assertEqual(after["status_updated_at"], before)
        self.assertEqual(after["personal_notes"], "")

    def test_rerunning_search_preserves_tracking_and_notes(self):
        self.update("Candidature préparée", snapshots={("france_travail", "1"): {"match_score": 87.5, "cv_name": "cv.pdf"}})
        store.save_notes("1", "À contacter", db_path=self.path)
        before = store.get_offer("1", db_path=self.path)
        self.assertFalse(add_job({**offer(), "title": "Modifié", "application_status": "Refus"}, self.path))
        after = store.get_offer("1", db_path=self.path)
        for field in ("application_status", "status_updated_at", "personal_notes", "application_prepared_at", "application_match_score", "application_cv_name"):
            self.assertEqual(after[field], before[field])
        self.assertEqual(len(get_jobs(self.path)), 1)

    def test_batch_updates_are_atomic_and_deduplicate_selection(self):
        add_job(offer("2"), self.path)
        identities = [("france_travail", "1"), ("france_travail", "2"), ("france_travail", "1")]
        self.assertEqual(store.update_status(identities, "À postuler", db_path=self.path), 2)
        with self.assertRaises(ValueError):
            store.update_status([("france_travail", "1"), ("france_travail", "absent")], "Refus", db_path=self.path)
        self.assertEqual(len(store.get_applications(["À postuler"], self.path)), 2)

    def test_source_is_part_of_identity(self):
        add_job(offer(source="other"), self.path)
        self.update("Archivée")
        self.assertIsNone(store.get_offer("1", source="other", db_path=self.path)["application_status"])

    def test_tracking_survives_a_fresh_python_process(self):
        self.update("Candidature préparée", snapshots={
            ("france_travail", "1"): {"match_score": 87.5, "cv_name": "cv.pdf"},
        })
        store.save_notes("1", "À rappeler mardi\nQuestion sur le poste", db_path=self.path)
        expected = store.get_offer("1", db_path=self.path)
        result = subprocess.run(
            [sys.executable, "-c", "import json, sys; from app.database import init_db; "
             "from app.applications import get_offer; init_db(sys.argv[1]); "
             "print(json.dumps(get_offer('1', db_path=sys.argv[1])))", str(self.path)],
            cwd=PROJECT_ROOT, capture_output=True, text=True, check=True, timeout=15,
        )
        self.assertEqual(json.loads(result.stdout), expected)

    def test_invalid_status_score_and_notes_are_rejected(self):
        with self.assertRaises(ValueError):
            self.update("inconnu")
        with self.assertRaises(ValueError):
            store.get_applications(["inconnu"], self.path)
        for score in (-1, 101, float("nan"), float("inf"), True):
            with self.assertRaises(ValueError):
                self.update("À étudier", snapshots={("france_travail", "1"): {"match_score": score}})
        with self.assertRaises(ValueError):
            store.save_notes("1", "x" * 20001, db_path=self.path)
        self.assertIsNone(store.get_offer("1", db_path=self.path)["application_status"])

    def test_dashboard_counts_all_offers_and_current_statuses(self):
        self.update("À étudier")
        add_job(offer("2"), self.path)
        add_job(offer("3"), self.path)
        store.update_status([("france_travail", "2")], "Refus", db_path=self.path)
        stats = store.application_stats(self.path)
        self.assertEqual((stats["total_offers"], stats["À étudier"], stats["Refus"], stats["Candidature envoyée"]), (3, 1, 1, 0))

    def test_migration_preserves_existing_offers_and_is_idempotent(self):
        columns = ("application_status", "personal_notes", "status_updated_at", "application_prepared_at",
                   "application_sent_at", "application_match_score", "application_cv_name")
        original = get_jobs(self.path)[0]
        with closing(sqlite3.connect(self.path)) as connection, connection:
            for column in columns:
                connection.execute(f"ALTER TABLE jobs DROP COLUMN {column}")
        init_db(self.path)
        init_db(self.path)
        migrated = get_jobs(self.path)[0]
        for field in original.keys() - set(columns):
            self.assertEqual(migrated[field], original[field])
        self.assertIsNone(migrated["application_status"])


if __name__ == "__main__":
    unittest.main()
