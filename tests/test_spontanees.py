"""La Bonne Boîte : client de l'API simulée, normalisation, nettoyage du message, anciennes candidatures spontanées.

Aucun appel réseau : réponses simulées (MockTransport) ; base temporaire.
"""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

from fastapi.testclient import TestClient
import httpx

from app.config import Settings
from app.database import init_db
from app.france_travail import FranceTravailConfigurationError
from app.main import app
from app import spontanees as sp

PARIS_MIDI = datetime(2026, 10, 31, 11, 0, tzinfo=timezone.utc)


def item(siret, name="Entreprise", email="yes", potential=50.0, city="LYON", naf_label="Programmation informatique",
         office=""):
    return {"siret": siret, "company_name": name, "office_name": office, "city": city, "postcode": "69001",
            "naf": "6201Z", "naf_label": naf_label, "hiring_potential": potential, "email": email}


class Base(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.db = self.folder / "jobs.db"
        init_db(self.db)
        self.settings = Settings(database_path=self.db, france_travail_client_id="id",
                                 france_travail_client_secret="secret-ne-pas-afficher")
        self.enterContext(patch.dict(os.environ, {"JOBHUNTER_FAKE_FT": "", "JOBHUNTER_LBB_SITE": ""}))
        self.requests = []

    def client(self, handler):
        def handle(request):
            self.requests.append(request)
            return handler(request)
        http = httpx.Client(transport=httpx.MockTransport(handle))
        self.addCleanup(http.close)
        return sp.LaBonneBoiteClient(self.settings, client=http)

    @staticmethod
    def token(request):
        return httpx.Response(200, json={"access_token": "jeton", "expires_in": 3600})

    def ancienne(self, siret="00000000000101", entreprise="Exemplia Analytics"):
        """Une candidature de l'ancien lot du jour (table conservée, plus alimentée)."""
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO spontanees (siret, entreprise, rome, metier, lot_du) VALUES (?, ?, 'M1405', "
                       "'Data scientist', '2026-10-31')", (siret, entreprise))
        return siret


class ClientTests(Base):
    def test_scope_fallback_then_search_parameters(self):
        def handler(request):
            if request.url.path.endswith("access_token"):
                scope = parse_qs(request.content.decode())["scope"][0]
                if scope == "api_labonneboitev2 search office":
                    return httpx.Response(400, json={"error": "invalid_scope"})
                return self.token(request)
            return httpx.Response(200, json={"hits": 1, "items": [item("12345678901234", "ACME")]})
        items = self.client(handler).recherche("M1405", citycode="69123", page=2)
        self.assertEqual(items[0]["siret"], "12345678901234")
        first, second, search = self.requests
        self.assertEqual([parse_qs(r.content.decode())["scope"][0] for r in (first, second)],
                         ["api_labonneboitev2 search office", "api_labonneboitev2"])
        self.assertEqual(search.url.path, "/partenaire/labonneboite/v2/recherche")
        self.assertEqual(dict(search.url.params), {"rome": "M1405", "page": "2", "page_size": "100",
                                                   "citycode": "69123", "distance": "30"})
        self.assertEqual(search.headers["authorization"], "Bearer jeton")

    def test_scope_refused_explains_what_to_activate(self):
        client = self.client(lambda r: httpx.Response(400, json={"error": "invalid_scope"}))
        with self.assertRaises(FranceTravailConfigurationError) as ctx:
            client.recherche("M1405")
        self.assertIn("La Bonne Boîte v2", str(ctx.exception))
        self.assertIn("francetravail.io", str(ctx.exception))
        self.assertNotIn("secret-ne-pas-afficher", str(ctx.exception))
        self.assertEqual(len(self.requests), 2)  # les deux scopes essayés, aucune recherche

    def test_bad_credentials_forbidden_and_invalid_response(self):
        with self.assertRaises(FranceTravailConfigurationError) as ctx:
            self.client(lambda r: httpx.Response(401, json={"error": "invalid_client"})).recherche("M1405")
        self.assertIn("FRANCE_TRAVAIL_CLIENT_ID", str(ctx.exception))
        forbidden = self.client(lambda r: self.token(r) if r.method == "POST" else httpx.Response(403))
        with self.assertRaises(FranceTravailConfigurationError):
            forbidden.recherche("M1405")
        broken = self.client(lambda r: self.token(r) if r.method == "POST" else httpx.Response(200, json={"items": "x"}))
        with self.assertRaisesRegex(Exception, "Réponse La Bonne Boîte invalide"):
            broken.recherche("M1405")


class NormalisationTests(unittest.TestCase):
    def test_city_names_are_readable(self):
        for raw, expected in (("PARIS", "Paris"), ("VILLENEUVE-D'ASCQ", "Villeneuve-d'Ascq"), ("SAINT-DENIS", "Saint-Denis"),
                              ("L'ISLE-ADAM", "L'Isle-Adam"), ("MARSEILLE 2E ARRONDISSEMENT", "Marseille 2e arrondissement"),
                              ("LE MANS", "Le Mans"), ("Aix-en-Provence", "Aix-en-Provence"), ("", "")):
            self.assertEqual(sp.joli_nom(raw), expected)

    def test_company_form_flag_and_identity(self):
        company = sp.normalize_company(item("12345678901234", "ACME SAS", office="Acme"), "M1405", "Data scientist")
        self.assertEqual((company["entreprise"], company["ville"], company["formulaire"]), ("Acme", "Lyon", True))
        self.assertFalse(sp.normalize_company(item("12345678901234", email="no"), "M1405", "x")["formulaire"])
        self.assertIsNone(sp.normalize_company(item("123"), "M1405", "x"))
        self.assertIsNone(sp.normalize_company(item("12345678901234", name=""), "M1405", "x"))


class NettoyageTests(unittest.TestCase):
    def test_text_is_cleaned_exactly_like_the_site_field(self):
        texte, retires = sp.nettoyer_message("Bonjour – “Data” 🙂 IA 100 % #1\r\n\r\n\r\n\r\nFin…  ")
        self.assertEqual(texte, 'Bonjour - "Data"  IA 100  1\n\nFin...')
        self.assertEqual(retires, ["#", "%", "🙂"])

    def test_site_override_is_local_only(self):
        with patch.dict(os.environ, {"JOBHUNTER_LBB_SITE": "http://127.0.0.1:8766/"}):
            self.assertEqual(sp.url_entreprise("12345678901234"), "http://127.0.0.1:8766/entreprise/12345678901234")
        with patch.dict(os.environ, {"JOBHUNTER_LBB_SITE": "https://exemple.org"}), self.assertRaises(sp.SpontaneesError):
            sp.site_lbb()


class AnciennesTests(Base):
    def test_mark_sent_keeps_the_first_date_and_advises_a_follow_up_after_ten_days(self):
        siret = self.ancienne()
        sent = sp.marquer_envoyee(self.db, siret, message="Texte", now=PARIS_MIDI)
        self.assertEqual((sent["statut"], sent["envoyee_par"], sent["message_envoye"]), ("Candidature envoyée", "vous", "Texte"))
        self.assertEqual(sent["relance_le"], "2026-11-10T11:00:00+00:00")
        sp.marquer_envoyee(self.db, siret, now=PARIS_MIDI + timedelta(days=2))
        later = sp.entreprise(self.db, siret, now=PARIS_MIDI + timedelta(days=10, minutes=1))
        self.assertEqual(later["envoyee_le"], "2026-10-31T11:00:00+00:00")
        self.assertTrue(later["relance_conseillee"])
        interview = sp.changer_statut(self.db, siret, "Entretien")
        self.assertEqual((interview["relance_le"], interview["relance_conseillee"]), (None, False))
        with self.assertRaises(sp.SpontaneesError):
            sp.changer_statut(self.db, siret, "Nouvelle")
        with self.assertRaises(sp.SpontaneesError):
            sp.marquer_envoyee(self.db, "99999999999999")

    def test_old_spontaneous_applications_stay_in_tracking(self):
        for target in ("app.config.get_settings", "app.api.get_settings"):
            self.enterContext(patch(target, return_value=self.settings))
        http = TestClient(app)
        siret = self.ancienne()
        sp.marquer_envoyee(self.db, siret)
        tracked = [a for a in http.get("/api/applications").json() if a["source"] == "spontanee"]
        self.assertEqual([(a["entreprise"], a["statut"]) for a in tracked], [("Exemplia Analytics", "Candidature envoyée")])
        res = http.patch(f"/api/applications/spontanee/{siret}", json={"status": "Entretien"})
        self.assertEqual(res.json()["item"]["statut"], "Entretien")
        self.assertEqual(http.patch(f"/api/applications/spontanee/{siret}", json={"status": "Nouvelle"}).status_code, 400)
        self.assertEqual(http.get("/api/spontanees").status_code, 404)  # lot du jour remplacé par la campagne


if __name__ == "__main__":
    unittest.main()
