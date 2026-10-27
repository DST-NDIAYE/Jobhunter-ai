"""API REST : base, CV maître, sorties et cache temporaires ; faux modèle de langage ; aucun appel réseau."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import Settings
from app.cover_letter import LetterError
from app.cv_tailor import CVTailorError
from app.database import get_jobs, init_db
from app.france_travail import FranceTravailError
from app.main import app
from app.test_mode import fake_search_jobs

FIXTURE = Path(__file__).parent / "fixtures" / "cv_master_fictif.yaml"
OFFRE = ("Data Scientist (H/F)\nEntreprise : Exemplia Analytics\nContrat : CDI\n\n"
         "Industrialisation de modèles de classification. Python, Scikit-learn, Docker, Kubernetes.")


class APITestCase(unittest.TestCase):
    def setUp(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.db_path = folder / "jobs.db"
        self.output = folder / "output"
        init_db(self.db_path)
        self.settings = Settings(database_path=self.db_path, france_travail_client_id="id",
                                 france_travail_client_secret="secret-ne-pas-afficher")
        profile = folder / "search_profile.json"
        profile.write_text(json.dumps({"preferred_titles": ["Data Scientist", "ML Engineer"],
                                       "future_excluded_title_terms": ["Senior", "Lead"]}))
        for target, value in (("app.config.get_settings", self.settings), ("app.api.get_settings", self.settings)):
            self.enterContext(patch(target, return_value=value))
        for target, value in (("app.api.MASTER_PATH", FIXTURE), ("app.api.OUTPUT_DIR", self.output),
                              ("app.api.CACHE_DIR", folder / "cache"), ("app.api.SEARCH_PROFILE", profile)):
            self.enterContext(patch(target, value))
        self.enterContext(patch.dict(os.environ, {"JOBHUNTER_FAKE_LLM": "1"}))
        self.client = TestClient(app)

    def add_opportunity(self, company, status, sent_days_ago):
        sent = (datetime.now(timezone.utc) - timedelta(days=sent_days_ago)).isoformat() if sent_days_ago is not None else None
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("INSERT INTO opportunities (source, external_id, siret, company_name, job_keyword, "
                       "application_status, application_sent_at, status_updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       ("labonneboite", company, company, company, "Data engineer", status, sent, sent))


class BaseTests(APITestCase):
    def test_root_and_docs(self):
        self.assertEqual(self.client.get("/", headers={"accept": "text/html"}).status_code, 200)
        self.assertEqual(self.client.get("/").json()["docs"], "/docs")
        self.assertEqual(self.client.get("/docs").status_code, 200)

    def test_root_never_404_whatever_the_launch_folder(self):
        browser = {"accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
        with tempfile.TemporaryDirectory() as elsewhere:
            previous = os.getcwd()
            os.chdir(elsewhere)
            try:
                for path, headers in (("/", browser), ("/", {"accept": "*/*"}), ("/", {}), ("/app", {}),
                                      ("/index.html", browser), ("/favicon.ico", {})):
                    with self.subTest(path=path, headers=headers):
                        self.assertNotEqual(self.client.get(path, headers=headers).status_code, 404)
                page = self.client.get("/", headers=browser)
                self.assertEqual((page.status_code, page.headers["content-type"]), (200, "text/html; charset=utf-8"))
                self.assertIn("JobHunter", page.text)
            finally:
                os.chdir(previous)
        with patch("app.main.INDEX", Path("/nulle/part/index.html")):
            missing = self.client.get("/", headers=browser)
        self.assertEqual(missing.status_code, 503)
        self.assertIn("Interface introuvable", missing.text)

    def test_frontend_has_no_contact_secret_or_send_button(self):
        html = (Path(__file__).parent.parent / "frontend" / "index.html").read_text()
        for absent in ("@gmail", "07 58", "NDIAYE", "Serigne", "Envoyer", "Gemini 1.5", "PRO V2.5", "Postuler en 1 clic"):
            self.assertNotIn(absent, html)
        self.assertEqual(html.count('target="_blank"'), html.count('rel="noopener noreferrer"'))

    def test_frontend_loads_no_external_resource(self):
        html = (Path(__file__).parent.parent / "frontend" / "index.html").read_text()
        self.assertNotRegex(html, r"""(?i)(src|href)\s*=\s*["']?(https?:)?//""")
        self.assertNotRegex(html, r"(?i)@import|url\(\s*['\"]?(https?:)?//|cdn\.")
        vue = self.client.get("/frontend/vendor/vue-3.4.38.global.prod.js")
        self.assertEqual(vue.status_code, 200)
        self.assertIn("vue v3.4.38", vue.text[:200])

    def test_status_has_no_contact_or_secret_and_shows_the_real_model(self):
        data = self.client.get("/api/status").json()
        self.assertTrue(data["ok"] and data["master_ready"] and data["ft_configured"])
        self.assertEqual((data["llm_provider"], data["llm_model"], data["letter_model"]),
                         ("fake", "faux-modele-test", "faux-modele-test"))
        for key in ("candidate_name", "candidate_email", "candidate_phone", "gmail_user", "cv_path"):
            self.assertNotIn(key, data)
        self.assertNotIn("secret-ne-pas-afficher", json.dumps(data))

    def test_removed_routes_are_gone(self):
        for method, path in (("post", "/api/apply/single"), ("post", "/api/gmail/dispatch"), ("post", "/api/gmail/test"),
                             ("post", "/api/leads/hunt"), ("post", "/api/letter/generate"), ("post", "/api/autopilot/run"),
                             ("get", "/api/scheduler"), ("get", "/api/profile"), ("get", "/api/stats"),
                             ("get", "/jobs"), ("get", "/jobs/match")):
            with self.subTest(path=path):
                self.assertIn(getattr(self.client, method)(path).status_code, (404, 405))

    def test_gmail_status_only(self):
        self.assertEqual(self.client.get("/api/gmail/status").json(), {"configured": False, "method": None})


class SearchTests(APITestCase):
    def test_defaults_come_from_search_profile(self):
        self.assertEqual(self.client.get("/api/search/defaults").json(),
                         {"titres": ["Data Scientist", "ML Engineer"], "mots_exclus": ["Senior", "Lead"],
                          "lieu": "France entière"})

    def test_search_france_entiere_by_default_hides_excluded_titles_and_scores(self):
        with patch("app.api.search_jobs", side_effect=fake_search_jobs) as search:
            data = self.client.get("/api/jobs/search").json()
        self.assertEqual(search.call_args.kwargs["location"], None)  # France entière
        self.assertEqual(search.call_args.kwargs["keyword"], "Data Scientist")  # premier titre du profil
        self.assertEqual(data["masquees"], 1)  # « Lead Data Scientist »
        self.assertNotIn("DEMO-004", [o["external_id"] for o in data["offres"]])
        scores = [o["match_score"] for o in data["offres"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(data["offres"][-1]["external_id"], "DEMO-005")  # le boulanger en dernier
        self.assertEqual(len(get_jobs(self.db_path)), 4)

    def test_city_and_filter_toggle(self):
        with patch("app.api.search_jobs", side_effect=fake_search_jobs) as search:
            data = self.client.get("/api/jobs/search", params={"ville": "Lyon", "masquer": "false"}).json()
        self.assertEqual(search.call_args.kwargs["location"], "Lyon")
        self.assertEqual(data["masquees"], 0)

    def test_search_error_is_a_clear_json_response(self):
        with patch("app.api.search_jobs", side_effect=FranceTravailError("France Travail indisponible.", status_code=502)):
            res = self.client.get("/api/jobs/search")
        self.assertEqual((res.status_code, res.json()["detail"]), (502, "France Travail indisponible."))


class PrepareTests(APITestCase):
    def prepare(self, **body):
        return self.client.post("/api/prepare", json={"offer_text": OFFRE, **body})

    def test_full_flow_prepare_download_mark_applied_and_track(self):
        res = self.prepare(url="https://example.org/offres/1")
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        self.assertEqual((data["offre"]["entreprise"], data["offre"]["contrat"]), ("Exemplia Analytics", "CDI"))
        self.assertIn("Kubernetes", data["rapport"]["manques"])
        self.assertIn("Python", [p["mot_cle"] for p in data["rapport"]["points_forts"]])
        self.assertEqual(data["lettre"]["statut"], "finale")
        self.assertIn("Je vous prie d'agréer", data["lettre"]["texte"])
        self.assertEqual(data["statut"], "Candidature préparée")
        cv = self.client.get(data["cv"]["telechargement"])
        self.assertEqual((cv.status_code, cv.headers["content-type"]), (200, "application/pdf"))
        self.assertIn("attachment", cv.headers["content-disposition"])
        self.assertEqual(self.client.get(data["lettre"]["url"]).status_code, 200)
        preview = self.client.get(data["cv"]["apercu"])
        self.assertEqual((preview.status_code, preview.headers["content-type"]), (200, "image/png"))

        source, external_id = data["offre"]["source"], data["offre"]["external_id"]
        self.assertEqual(source, "manuel")
        applied = self.client.post(f"/api/applications/{source}/{external_id}/postule").json()["item"]
        self.assertEqual(applied["statut"], "Candidature envoyée")
        sent = datetime.fromisoformat(applied["date_envoi"])
        self.assertEqual(datetime.fromisoformat(applied["relance_le"]).date(), (sent + timedelta(days=7)).date())
        self.assertFalse(applied["relance_conseillee"])
        tracked = self.client.get("/api/applications").json()
        self.assertEqual([(t["entreprise"], t["statut"]) for t in tracked], [("Exemplia Analytics", "Candidature envoyée")])

    def test_prepare_never_regenerates_a_cached_letter_but_regenerate_does(self):
        from app.test_mode import FakeLLM
        with patch.object(FakeLLM, "_lettre", side_effect=FakeLLM._lettre) as write:
            first = self.prepare().json()["lettre"]
            again = self.prepare().json()["lettre"]
            self.assertEqual(write.call_count, 1)
            self.assertEqual((first["depuis_cache"], again["depuis_cache"]), (False, True))
            self.assertEqual(again["texte"], first["texte"])
            res = self.client.post("/api/prepare/lettre", json={"offer_text": OFFRE})
            self.assertEqual(res.status_code, 200, res.text)
            self.assertEqual(write.call_count, 2)
            self.assertFalse(res.json()["lettre"]["depuis_cache"])
            self.assertEqual(self.client.get(res.json()["lettre"]["url"]).status_code, 200)

    def test_regenerate_errors_are_clear(self):
        self.assertEqual(self.client.post("/api/prepare/lettre", json={"offer_text": " "}).status_code, 400)
        with patch("app.api.generate_letter", side_effect=LetterError("Quota atteint pour m1 : réessayez plus tard.")):
            res = self.client.post("/api/prepare/lettre", json={"offer_text": OFFRE})
        self.assertEqual(res.status_code, 429)

    def test_preparing_again_never_downgrades_a_sent_application(self):
        data = self.prepare().json()
        self.client.post(f"/api/applications/{data['offre']['source']}/{data['offre']['external_id']}/postule")
        self.assertEqual(self.prepare().json()["statut"], "Candidature envoyée")

    def test_france_travail_offer_keeps_its_identity(self):
        data = self.prepare(source="france_travail", external_id="DEMO-001").json()
        self.assertEqual((data["offre"]["source"], data["offre"]["external_id"]), ("france_travail", "DEMO-001"))

    def test_invalid_inputs(self):
        self.assertEqual(self.client.post("/api/prepare", json={"offer_text": "  "}).status_code, 400)
        self.assertEqual(self.prepare(url="javascript:alert(1)").status_code, 400)

    def test_quota_error_is_429_with_a_clear_message(self):
        with patch("app.api.tailor_cv", side_effect=CVTailorError("Quota atteint pour m1, m2 : réessayez plus tard.")):
            res = self.prepare()
        self.assertEqual(res.status_code, 429)
        self.assertIn("Quota atteint", res.json()["detail"])

    def test_letter_failure_keeps_the_cv(self):
        with patch("app.api.generate_letter", side_effect=LetterError("Lettre refusée, points bloquants : x.")):
            data = self.prepare().json()
        self.assertIsNone(data["lettre"])
        self.assertIn("points bloquants", data["lettre_erreur"])
        self.assertEqual(self.client.get(data["cv"]["url"]).status_code, 200)

    def test_files_route_only_serves_generated_files(self):
        for name in ("..%2F..%2F.env", ".env", "cv_master.yaml", "x.pdf.exe"):
            with self.subTest(name=name):
                self.assertIn(self.client.get(f"/api/files/{name}").status_code, (400, 404))


class TrackingTests(APITestCase):
    def test_lbb_is_read_only_and_relance_after_seven_days(self):
        self.add_opportunity("Ancienne SA", "Candidature envoyée", 10)
        self.add_opportunity("Récente SA", "Candidature envoyée", 2)
        self.add_opportunity("Echec SA", "Échec", 9)
        items = {i["entreprise"]: i for i in self.client.get("/api/applications").json()}
        self.assertTrue(items["Ancienne SA"]["relance_conseillee"])
        self.assertFalse(items["Récente SA"]["relance_conseillee"])
        self.assertIsNone(items["Echec SA"]["relance_le"])
        self.assertTrue(all(i["lecture_seule"] for i in items.values()))
        res = self.client.patch("/api/applications/labonneboite/Ancienne SA", json={"status": "Entretien"})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(self.client.post("/api/applications/labonneboite/Ancienne SA/postule").status_code, 403)

    def test_status_change_and_validation(self):
        data = self.client.post("/api/prepare", json={"offer_text": OFFRE}).json()
        path = f"/api/applications/{data['offre']['source']}/{data['offre']['external_id']}"
        self.assertEqual(self.client.patch(path, json={"status": "Relancée"}).status_code, 400)
        item = self.client.patch(path, json={"status": "Entretien"}).json()["item"]
        self.assertEqual(item["statut"], "Entretien")
        self.assertEqual(self.client.patch("/api/applications/manuel/inconnu", json={"status": "Refus"}).status_code, 404)
        self.assertIn("Candidature envoyée", self.client.get("/api/statuses").json())


if __name__ == "__main__":
    unittest.main()
