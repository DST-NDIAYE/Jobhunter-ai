"""Candidatures spontanées : API La Bonne Boîte simulée, lot du jour, modèle de message, historique, réglages, routes.

Aucun appel réseau : réponses simulées (MockTransport) ou entreprises fictives du mode test ; base et modèle temporaires.
"""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
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
        self.modele = self.folder / "modele.txt"
        self.enterContext(patch.object(sp, "MODELE_PATH", self.modele))
        profile = self.folder / "search_profile.json"
        profile.write_text(json.dumps({"metiers_spontanees": [{"rome": "M1405", "libelle": "Data scientist"},
                                                              {"rome": "M1811", "libelle": "Data engineer"}]}))
        self.enterContext(patch.object(sp, "SEARCH_PROFILE", profile))
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

    def legacy(self, siret, status=None, sent=False, event=None):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO opportunities (source, external_id, siret, company_name, application_status, "
                       "application_sent_at) VALUES ('labonneboite', ?, ?, 'Ancienne', ?, ?)",
                       (siret, siret, status, "2026-09-01T10:00:00+00:00" if sent else None))
            if event:
                db.execute("INSERT INTO application_events (source, external_id, channel, event) "
                           "VALUES ('labonneboite', ?, 'LABONNEBOITE', ?)", (siret, event))


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


class LotTests(Base):
    def test_fake_batch_round_robin_form_only_and_deduplicated(self):
        with patch.dict(os.environ, {"JOBHUNTER_FAKE_FT": "1"}):
            result = sp.constituer_lot(self.settings, now=PARIS_MIDI)
        lot = result["lot"]
        self.assertTrue(result["nouveau"])
        self.assertEqual([e["siret"] for e in lot], ["00000000000101", "00000000000301", "00000000000102",
                                                     "00000000000302", "00000000000104", "00000000000303"])
        self.assertNotIn("00000000000103", [e["siret"] for e in lot])  # formulaire inactif
        self.assertTrue(all(e["lot_du"] == "2026-10-31" and e["statut"] == "À envoyer" for e in lot))
        self.assertEqual(lot[0]["lot_zone"], "France entière")

    def test_at_most_ten_and_same_day_returns_the_same_batch_without_calling_the_api(self):
        def handler(request):
            if request.method == "POST":
                return self.token(request)
            rome = request.url.params["rome"]
            return httpx.Response(200, json={"items": [item(f"{rome[1:]}{i:010d}", f"E{rome}{i}", potential=90 - i)
                                                        for i in range(8)]})
        client = self.client(handler)
        lot = sp.constituer_lot(self.settings, now=PARIS_MIDI, client=client, pause=0)["lot"]
        self.assertEqual(len(lot), 10)
        self.assertEqual([e["rome"] for e in lot[:4]], ["M1405", "M1811", "M1405", "M1811"])  # un métier après l'autre
        calls = len(self.requests)
        again = sp.constituer_lot(self.settings, now=PARIS_MIDI + timedelta(hours=8), client=client, pause=0)
        self.assertFalse(again["nouveau"])
        self.assertEqual([e["siret"] for e in again["lot"]], [e["siret"] for e in lot])
        self.assertEqual(len(self.requests), calls)

    def test_never_proposes_contacted_or_already_proposed_companies(self):
        self.legacy("10000000000001", status="Candidature envoyée", sent=True)
        self.legacy("10000000000002", status="Échec", sent=True)
        self.legacy("10000000000003", event="ERROR")
        self.legacy("10000000000004", event="SENT")
        self.legacy("10000000000005", status=None)  # simple résultat de recherche : jamais contactée
        sirets = [f"1000000000000{i}" for i in range(1, 8)]

        def handler(request):
            if request.method == "POST":
                return self.token(request)
            if request.url.params["rome"] != "M1405":
                return httpx.Response(200, json={"items": []})
            return httpx.Response(200, json={"items": [item(s, f"E{s[-1]}") for s in sirets]})
        yesterday = sp.constituer_lot(self.settings, now=PARIS_MIDI - timedelta(days=1), client=self.client(handler), pause=0)
        self.assertEqual([e["siret"] for e in yesterday["lot"]], sirets[4:])
        self.assertEqual(sp.anciennes_contactees(self.db), 4)
        today = sp.constituer_lot(self.settings, now=PARIS_MIDI, client=self.client(handler), pause=0)
        self.assertEqual(today["lot"], [])  # tout est déjà proposé ou contacté : aucun lot créé
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM spontanees WHERE lot_du='2026-10-31'").fetchone()[0], 0)

    def test_city_is_resolved_to_an_insee_code_with_a_radius(self):
        def handler(request):
            if request.url.host == "geo.api.gouv.fr":
                return httpx.Response(200, json=[{"nom": "Lyon", "code": "69123"}])
            if request.method == "POST":
                return self.token(request)
            return httpx.Response(200, json={"items": [item("12345678901234", "ACME")]})
        lot = sp.constituer_lot(self.settings, ville="Lyon", now=PARIS_MIDI, client=self.client(handler), pause=0)["lot"]
        self.assertEqual(lot[0]["lot_zone"], "Lyon et alentours (30 km)")
        searches = [r for r in self.requests if r.url.path.endswith("/recherche")]
        self.assertEqual({(r.url.params["citycode"], r.url.params["distance"]) for r in searches}, {("69123", "30")})

    def test_missing_form_flag_is_an_explicit_error_and_records_nothing(self):
        def handler(request):
            if request.method == "POST":
                return self.token(request)
            raw = item("12345678901234")
            raw.pop("email")
            return httpx.Response(200, json={"items": [raw]})
        with self.assertRaisesRegex(sp.SpontaneesError, "champ « email » absent"):
            sp.constituer_lot(self.settings, now=PARIS_MIDI, client=self.client(handler), pause=0)
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM spontanees").fetchone()[0], 0)

    def test_next_page_is_read_only_when_needed(self):
        def handler(request):
            if request.method == "POST":
                return self.token(request)
            page, rome = int(request.url.params["page"]), request.url.params["rome"]
            if page == 1:  # page pleine, mais sans formulaire
                return httpx.Response(200, json={"items": [item(f"{rome[1:]}{i:010d}", email="no") for i in range(100)]})
            return httpx.Response(200, json={"items": [item(f"{rome[1:]}9{i:09d}", f"P2-{i}") for i in range(3)]})
        lot = sp.constituer_lot(self.settings, now=PARIS_MIDI, client=self.client(handler), pause=0)["lot"]
        self.assertEqual(len(lot), 6)
        self.assertEqual(sorted({r.url.params["page"] for r in self.requests if r.method == "GET"}), ["1", "2"])


class MessageTests(Base):
    COMPANY = {"entreprise": "Exemplia", "ville": "Lyon", "secteur": "Programmation informatique"}

    def test_template_is_created_from_the_example_then_rendered(self):
        self.assertFalse(self.modele.exists())
        modele = sp.charger_modele()
        self.assertTrue(self.modele.exists())
        self.assertEqual(modele, sp.MODELE_EXEMPLE.read_text(encoding="utf-8"))
        message = sp.message_pour(modele, self.COMPANY)
        self.assertEqual((message["problemes"], message["retires"]), ([], []))
        self.assertIn("Votre entreprise, Exemplia (Lyon)", message["texte"])
        self.assertIn("« Programmation informatique »", message["texte"])
        self.modele.write_text("Modifié pour {entreprise}.", encoding="utf-8")
        self.assertEqual(sp.message_pour(sp.charger_modele(), self.COMPANY)["texte"], "Modifié pour Exemplia.")

    def test_unknown_fields_and_braces_are_reported(self):
        self.assertIn("{poste}", sp.problemes_modele("Bonjour {entreprise}, poste {poste}")[0])
        self.assertIn("Accolade", sp.problemes_modele("Bonjour {entreprise")[0])
        self.assertTrue(sp.message_pour("Bonjour {0}", self.COMPANY)["problemes"])
        self.assertEqual(sp.problemes_modele("   "), ["Le modèle de message est vide."])

    def test_text_is_cleaned_exactly_like_the_site_field(self):
        texte, retires = sp.nettoyer_message("Bonjour – “Data” 🙂 IA 100 % #1\r\n\r\n\r\n\r\nFin…  ")
        self.assertEqual(texte, 'Bonjour - "Data"  IA 100  1\n\nFin...')
        self.assertEqual(retires, ["#", "%", "🙂"])

    def test_blocking_problems(self):
        missing = sp.message_pour("{entreprise} {secteur}", {**self.COMPANY, "secteur": ""})
        self.assertIn("{secteur}", missing["problemes"][0])
        self.assertIn("lien", sp.message_pour("Voir https://exemple.org", self.COMPANY)["problemes"][0])
        self.assertIn("trop long", sp.message_pour("a" * 1501, self.COMPANY)["problemes"][0])
        self.assertEqual(sp.message_pour("a" * 1500, self.COMPANY)["problemes"], [])


class HistoriqueEtReglagesTests(Base):
    def batch(self, now=PARIS_MIDI):
        with patch.dict(os.environ, {"JOBHUNTER_FAKE_FT": "1"}):
            return sp.constituer_lot(self.settings, now=now)["lot"]

    def test_mark_sent_keeps_the_first_date_and_advises_a_follow_up_after_ten_days(self):
        siret = self.batch()[0]["siret"]
        sent = sp.marquer_envoyee(self.db, siret, message="Texte", now=PARIS_MIDI)
        self.assertEqual((sent["statut"], sent["envoyee_par"], sent["message_envoye"]), ("Candidature envoyée", "vous", "Texte"))
        self.assertEqual(sent["relance_le"], "2026-11-10T11:00:00+00:00")
        sp.marquer_envoyee(self.db, siret, now=PARIS_MIDI + timedelta(days=2))
        later = sp.entreprise(self.db, siret, now=PARIS_MIDI + timedelta(days=10, minutes=1))
        self.assertEqual(later["envoyee_le"], "2026-10-31T11:00:00+00:00")
        self.assertTrue(later["relance_conseillee"])
        self.assertFalse(sp.entreprise(self.db, siret, now=PARIS_MIDI + timedelta(days=9))["relance_conseillee"])
        interview = sp.changer_statut(self.db, siret, "Entretien")
        self.assertEqual((interview["relance_le"], interview["relance_conseillee"]), (None, False))
        with self.assertRaises(sp.SpontaneesError):
            sp.changer_statut(self.db, siret, "Nouvelle")
        with self.assertRaises(sp.SpontaneesError):
            sp.marquer_envoyee(self.db, "99999999999999")

    def test_sends_of_the_day_use_the_paris_date(self):
        lot = self.batch()
        sp.marquer_envoyee(self.db, lot[0]["siret"], now=datetime(2026, 10, 30, 22, 30, tzinfo=timezone.utc))  # 23 h 30 à Paris
        sp.marquer_envoyee(self.db, lot[1]["siret"], now=datetime(2026, 10, 30, 23, 30, tzinfo=timezone.utc))  # 0 h 30 le 31
        self.assertEqual(sp.envois_du_jour(self.db, now=PARIS_MIDI), 1)

    def test_settings_default_to_no_automatic_sending_and_are_validated(self):
        self.assertEqual(sp.lire_reglages(self.db), {"envoi_automatique": False, "plafond_par_jour": 10, "nom_cv": ""})
        saved = sp.ecrire_reglages(self.db, {"envoi_automatique": True, "plafond_par_jour": 3, "nom_cv": "  CV data "})
        self.assertEqual(saved, {"envoi_automatique": True, "plafond_par_jour": 3, "nom_cv": "CV data"})
        self.assertEqual(sp.lire_reglages(self.db), saved)
        for bad in ({"plafond_par_jour": 11}, {"plafond_par_jour": 0}, {"plafond_par_jour": True},
                    {"plafond_par_jour": "5"}, {"envoi_automatique": "oui"}, {"nom_cv": "x" * 121}, {"inconnu": 1}):
            with self.subTest(bad=bad), self.assertRaises(sp.SpontaneesError):
                sp.ecrire_reglages(self.db, bad)
        with closing(sqlite3.connect(self.db)) as db, db:  # valeur abîmée : retour à la valeur sûre
            db.execute("UPDATE reglages SET valeur = '\"oui\"' WHERE cle = 'spontanees.envoi_automatique'")
        self.assertFalse(sp.lire_reglages(self.db)["envoi_automatique"])

    def test_site_override_is_local_only(self):
        with patch.dict(os.environ, {"JOBHUNTER_LBB_SITE": "http://127.0.0.1:8766/"}):
            self.assertEqual(sp.url_entreprise("12345678901234"), "http://127.0.0.1:8766/entreprise/12345678901234")
        with patch.dict(os.environ, {"JOBHUNTER_LBB_SITE": "https://exemple.org"}), self.assertRaises(sp.SpontaneesError):
            sp.site_lbb()
        self.assertEqual(sp.url_formulaire({"siret": "12345678901234", "rome": "M1889", "metier": "Ingénieur IA"}),
                         "https://labonneboite.francetravail.fr/candidature/nouvelle/12345678901234?job=M1889"
                         "&jobLabel=Ing%C3%A9nieur%20IA")


class RoutesTests(Base):
    def setUp(self):
        super().setUp()
        for target in ("app.config.get_settings", "app.api.get_settings", "app.api_spontanees.get_settings"):
            self.enterContext(patch(target, return_value=self.settings))
        self.enterContext(patch("app.robot_spontanees.ROBOT_DIR", self.folder / "robot"))
        self.enterContext(patch.dict(os.environ, {"JOBHUNTER_FAKE_FT": "1"}))
        self.http = TestClient(app)

    def test_batch_message_mark_sent_history_and_tracking(self):
        empty = self.http.get("/api/spontanees").json()
        self.assertEqual((empty["lot"], empty["historique"], empty["reglages"]["envoi_automatique"]), ([], [], False))
        self.assertEqual(empty["modele"]["champs"], ["entreprise", "ville", "secteur"])
        data = self.http.post("/api/spontanees/lot", json={"ville": None}).json()
        self.assertTrue(data["nouveau"])
        first = data["lot"][0]
        self.assertEqual(first["entreprise"], "Exemplia Analytics")
        self.assertIn("Exemplia Analytics (Paris)", first["message"]["texte"])
        self.assertTrue(first["url_entreprise"].endswith("/entreprise/00000000000101"))
        self.assertEqual(self.http.post("/api/spontanees/lot", json={"ville": "Lyon"}).json()["nouveau"], False)
        sent = self.http.post("/api/spontanees/00000000000101/envoyee").json()["item"]
        self.assertEqual(sent["statut"], "Candidature envoyée")
        self.assertEqual(sent["message_envoye"], first["message"]["texte"])
        self.assertEqual((datetime.fromisoformat(sent["relance_le"]) - datetime.fromisoformat(sent["envoyee_le"])).days, 10)
        self.assertEqual(self.http.post("/api/spontanees/123/envoyee").status_code, 400)
        self.assertEqual(self.http.post("/api/spontanees/99999999999999/envoyee").status_code, 404)
        tracked = [a for a in self.http.get("/api/applications").json() if a["source"] == "spontanee"]
        self.assertEqual([(a["entreprise"], a["statut"]) for a in tracked], [("Exemplia Analytics", "Candidature envoyée")])
        self.assertEqual(tracked[0]["statuts"], ["Candidature envoyée", "Entretien", "Refus", "Archivée"])
        res = self.http.patch("/api/applications/spontanee/00000000000101", json={"status": "Entretien"})
        self.assertEqual(res.json()["item"]["statut"], "Entretien")
        self.assertEqual(self.http.patch("/api/applications/spontanee/00000000000101", json={"status": "Nouvelle"}).status_code, 400)
        history = self.http.get("/api/spontanees").json()["historique"]
        self.assertEqual(len(history), len(data["lot"]))

    def test_api_access_error_is_a_clear_message(self):
        with patch("app.api_spontanees.constituer_lot",
                   side_effect=FranceTravailConfigurationError(sp.LaBonneBoiteClient.SCOPE_REFUSED)):
            res = self.http.post("/api/spontanees/lot", json={})
        self.assertEqual(res.status_code, 503)
        self.assertIn("ajoutez l'API « La Bonne Boîte v2 »", res.json()["detail"])

    def test_settings_routes(self):
        self.assertEqual(self.http.get("/api/spontanees/reglages").json()["plafond_max"], 10)
        res = self.http.put("/api/spontanees/reglages", json={"plafond_par_jour": 12})
        self.assertEqual((res.status_code, res.json()["detail"]), (400, "Le plafond doit être un nombre entier entre 1 et 10 par jour."))
        self.assertEqual(self.http.put("/api/spontanees/reglages", json={"plafond_par_jour": ""}).status_code, 400)
        saved = self.http.put("/api/spontanees/reglages", json={"envoi_automatique": True, "nom_cv": "CV"}).json()
        self.assertEqual((saved["envoi_automatique"], saved["plafond_par_jour"], saved["nom_cv"]), (True, 10, "CV"))
        self.assertTrue(self.http.get("/api/spontanees/acces").json()["ok"])

    def test_robot_routes_refuse_clearly_and_serve_only_captures(self):
        self.assertEqual(self.http.get("/api/spontanees/robot").json(), {"actif": False, "id": None, "journal": [], "captures": []})
        res = self.http.post("/api/spontanees/robot", json={"mode": "essai"})
        self.assertEqual(res.status_code, 409)
        self.assertIn("constituez d'abord le lot du jour", res.json()["detail"])
        self.assertEqual(self.http.post("/api/spontanees/robot", json={"mode": "turbo"}).status_code, 400)
        self.assertEqual(self.http.post("/api/spontanees/robot/decision", json={"action": "envoyee"}).status_code, 409)
        self.assertEqual(self.http.post("/api/spontanees/robot/decision", json={"action": "tout"}).status_code, 400)
        self.assertFalse(self.http.post("/api/spontanees/robot/arret").json()["actif"])
        for path in ("20261031-120000/..%2F..%2Fjobs.db", "20261031-120000/journal.txt", "x/01-a.png", "20261031-120000/01-a.png"):
            with self.subTest(path=path):
                self.assertIn(self.http.get(f"/api/spontanees/robot/captures/{path}").status_code, (404, 400))
        sp.ecrire_reglages(self.db, {"plafond_par_jour": 1})
        self.http.post("/api/spontanees/lot", json={})
        self.http.post("/api/spontanees/00000000000101/envoyee")
        res = self.http.post("/api/spontanees/robot", json={"mode": "assiste"})
        self.assertEqual(res.status_code, 409)
        self.assertIn("Plafond du jour atteint", res.json()["detail"])


if __name__ == "__main__":
    unittest.main()
