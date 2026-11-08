"""Campagne La Bonne Boîte : cibles, nom et message, nombre à envoyer, alertes, statuts, historique, export, routes.

Aucun appel réseau : API simulée (MockTransport) ou entreprises fictives du mode test ; base et modèles temporaires.
"""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx

from app import campagne as cp
from app.config import Settings
from app.database import init_db
from app.main import app
from app import spontanees as sp

MIDI = datetime(2026, 11, 5, 11, 0, tzinfo=timezone.utc)  # 12 h à Paris
MODELE = ("Madame, Monsieur,\n\nJe vous propose ma candidature spontanée chez {entreprise}. 🙂 100% #motivé\n\n"
          "Camille EXEMPLE\n")


def item(siret, name="ENTREPRISE", email="yes", potential=50.0, postcode="69001", office=""):
    return {"siret": siret, "company_name": name, "office_name": office, "city": "LYON", "postcode": postcode,
            "naf": "6201Z", "naf_label": "Programmation informatique", "hiring_potential": potential, "email": email}


class Base(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.db = self.folder / "jobs.db"
        init_db(self.db)
        self.settings = Settings(database_path=self.db, france_travail_client_id="id",
                                 france_travail_client_secret="secret-ne-pas-afficher")
        self.modele = self.folder / "modele.txt"
        self.modele.write_text(MODELE, encoding="utf-8")
        self.enterContext(patch.object(cp, "MODELE_A", self.modele))
        self.enterContext(patch.object(cp, "MODELE_B", self.folder / "modele_B.txt"))
        self.enterContext(patch.dict(os.environ, {"JOBHUNTER_FAKE_FT": "1", "JOBHUNTER_LBB_SITE": ""}))

    def ligne(self, siret, statut=cp.A_FAIRE, **champs):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO campagne (siret, entreprise, rome, metier, phase, statut, departement, potentiel, "
                       "envoyee_le, tentee_le, maj_le, version) VALUES (?, ?, 'M1811', 'Data engineer', ?, ?, ?, ?, ?, ?, ?, ?)",
                       (siret, champs.get("entreprise", f"ENTREPRISE {siret[-3:]}"), champs.get("phase", "A"), statut,
                        champs.get("departement", "75"), champs.get("potentiel", 50.0), champs.get("envoyee_le"),
                        champs.get("tentee_le"), champs.get("maj_le", champs.get("envoyee_le")), champs.get("version")))
        return siret


class NomEtMessageTests(Base):
    def test_company_names_are_cleaned_or_replaced_by_vous(self):
        for brut, attendu in (("UBER EATS FRANCE SAS", "Uber Eats France"), ("SWORD SA", "Sword"),
                              ("MAGELLAN CONSULTING", "Magellan Consulting"), ("BNP PARIBAS", "BNP Paribas"),
                              ("KPMG S.A.S.", "KPMG"), ("IBM FRANCE", "IBM France"), ("SARL DUPONT ET FILS", "Dupont et Fils"),
                              ("PASAPAS-KPF", "Pasapas-KPF"), ("SOCIETE GENERALE (SA)", "Societe Generale"),
                              ("RATP DEV", "RATP Dev"), ("DATA TECH SAS", "Data Tech"), ("L'OREAL", "L'Oreal"),
                              ("Exemplia Analytics", "Exemplia Analytics"), ("", "vous"), (None, "vous"), ("SAS", "vous"),
                              ("LOGICIELS 2000", "vous"), ("A" * 51, "vous"), ("LH & TECH", "vous"),
                              ("AKKA I&S", "vous"), ("ECONOCOM SERVICES & SOLUTIONS", "vous")):
            with self.subTest(brut=brut):
                self.assertEqual(cp.nom_pour_message(brut), attendu)

    def test_message_cleaning_length_and_single_field(self):
        message = cp.preparer_message(MODELE, "LIVRAISON EXEMPLE FRANCE SAS")
        self.assertEqual(message["problemes"], [])
        self.assertIn("chez Livraison Exemple France.", message["texte"])
        self.assertNotIn("🙂", message["texte"])
        self.assertNotIn("%", message["texte"])
        self.assertNotIn("#", message["texte"])
        self.assertEqual(sorted(message["retires"]), ["#", "%", "🙂"])
        self.assertEqual(message["longueur"], len(message["texte"]))
        self.assertIn("chez vous.", cp.preparer_message(MODELE, "LOGICIELS 2000")["texte"])
        self.assertIn("{ville}", cp.preparer_message("Chez {entreprise} à {ville}", "X")["problemes"][0])
        self.assertIn("absent", cp.preparer_message("Bonjour", "X")["problemes"][0])
        self.assertIn("trop long", cp.preparer_message("{entreprise} " + "a" * 1500, "X")["problemes"][0])
        self.assertEqual(cp.preparer_message("{entreprise} " + "a" * 1480, "Acme")["problemes"], [])
        self.assertIn("lien", cp.preparer_message("{entreprise} https://exemple.org", "X")["problemes"][0])
        self.assertIn("Écrivez « et »", cp.preparer_message("{entreprise}, spécialiste R&D", "X")["problemes"][0])
        self.assertEqual(cp.problemes_modele(None)[0][:22], "Modèle de message abse")

    def test_versions_a_and_b_alternate_only_when_b_exists(self):
        self.assertEqual(cp.charger_modeles(), {"A": MODELE})
        self.assertEqual(cp.version_suivante(self.db, cp.charger_modeles()), "A")
        (self.folder / "modele_B.txt").write_text("Version B pour {entreprise}.", encoding="utf-8")
        versions = cp.charger_modeles()
        self.assertEqual(cp.version_suivante(self.db, versions), "A")
        self.ligne("00000000000901", cp.ENVOYEE, version="A", envoyee_le=MIDI.isoformat())
        self.assertEqual(cp.version_suivante(self.db, versions), "B")
        self.ligne("00000000000902", cp.ENVOYEE, version="B", envoyee_le=MIDI.isoformat())
        self.assertEqual(cp.version_suivante(self.db, versions), "A")
        (self.folder / "modele_B.txt").write_text("Version B sans champ.", encoding="utf-8")
        self.assertEqual(cp.version_suivante(self.db, cp.charger_modeles()), "A")  # B inutilisable : jamais choisie
        apercu = cp.apercu(self.db)
        self.assertEqual(sorted(apercu["versions"]), ["A", "B"])
        self.assertTrue(apercu["version_b"])


class RythmeTests(Base):
    def test_confirmed_sends_of_the_day_use_the_paris_date(self):
        self.ligne("00000000000811", cp.ENVOYEE, envoyee_le="2026-11-04T22:30:00+00:00")  # 23 h 30 à Paris, la veille
        self.ligne("00000000000812", cp.ENVOYEE, envoyee_le="2026-11-04T23:30:00+00:00")  # 0 h 30 le 5
        self.ligne("00000000000813", cp.INCERTAINE, tentee_le="2026-11-05T08:00:00+00:00")  # non confirmée : pas comptée
        self.assertEqual(cp.envois_du_jour(self.db, MIDI), 1)

    def test_a_new_alert_after_a_lifted_one_has_its_own_reason(self):
        cp.poser_alerte(self.db, "page_inconnue", "Ancienne raison", MIDI)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE campagne_jours SET alerte = NULL, alerte_raison = 'Levée' WHERE jour = ?", (cp.jour(MIDI),))
        cp.poser_alerte(self.db, "incertain", "Envoi incertain pour X", MIDI)
        alerte = cp.alerte_du_jour(self.db, MIDI)
        self.assertEqual((alerte["type"], alerte["raison"]), ("incertain", "Envoi incertain pour X"))

    def test_no_email_refusal_and_search_address(self):
        self.assertTrue(cp.sans_email_apres_envoi("Page non trouvée\nThe office for this application does not have an email"))
        self.assertFalse(cp.sans_email_apres_envoi("Page non trouvée"))
        self.ligne("00000000000701")
        cp.marquer(self.db, "00000000000701", cp.EN_COURS)
        cp.marquer(self.db, "00000000000701", cp.TENTEE)
        self.assertEqual(cp.marquer(self.db, "00000000000701", cp.IGNOREE, motif="sans_email")["statut"], cp.IGNOREE)
        url = cp.url_recherche({"rome": "M1811", "departement": "75"}, 2)
        self.assertTrue(url.endswith("/recherche?rome=M1811&department_number=75&bbox=&distance=10"
                                     "&sort_by=romes.hiring_potential&sort_direction=desc&page=2"), url)

    def test_alert_blocks_the_day_and_keeps_the_first_reason(self):
        self.assertIsNone(cp.alerte_du_jour(self.db, MIDI))
        cp.poser_alerte(self.db, "captcha", "Captcha affiché", MIDI)
        cp.poser_alerte(self.db, "erreurs", "Autre raison", MIDI)
        alerte = cp.alerte_du_jour(self.db, MIDI)
        self.assertEqual((alerte["type"], alerte["raison"], alerte["bloque_essai"]), ("captcha", "Captcha affiché", True))
        self.assertIsNone(cp.alerte_du_jour(self.db, MIDI + timedelta(days=1)))

    def test_settings_defaults_and_validation(self):
        self.assertEqual(cp.lire_reglages(self.db), {"objectif": 50, "phase_a": True, "phase_b": False, "nom_cv": ""})
        saved = cp.ecrire_reglages(self.db, {"objectif": 1000, "nom_cv": "  CV data "})
        self.assertEqual((saved["objectif"], saved["nom_cv"]), (1000, "CV data"))
        for bad in ({"objectif": 1001}, {"objectif": 0}, {"objectif": True}, {"objectif": "5"}, {"plafond": 20},
                    {"montee": False}, {"pause_entreprise": [20, 60]}, {"heure_debut": 8},
                    {"phase_a": False, "phase_b": False}, {"nom_cv": "x" * 121}, {"inconnu": 1}):
            with self.subTest(bad=bad), self.assertRaises(cp.CampagneError):
                cp.ecrire_reglages(self.db, bad)
        with closing(sqlite3.connect(self.db)) as db, db:  # anciens réglages (plafond, pauses…) ignorés ; valeur abîmée
            db.execute("UPDATE reglages SET valeur = '{\"objectif\": \"beaucoup\", \"montee\": true, \"plafond\": 20}' "
                       "WHERE cle = 'campagne.reglages'")
        self.assertEqual(cp.lire_reglages(self.db), {"objectif": 50, "phase_a": True, "phase_b": False, "nom_cv": ""})


class CiblesTests(Base):
    CENTRES = (("75056", "Paris"), ("93008", "Bobigny"))
    VILLES = {"69": (("69123", "Lyon"),), "2A": (("2A004", "Ajaccio"),)}

    def api(self, pages, *, page_size=2, refus=None, panne=None):
        """Client La Bonne Boîte simulé : pages[(rome, citycode)] ; refus(rome, code) → 400 ; panne(rome, code) → 500.

        Comme la vraie API : une recherche sans ville est refusée.
        """
        appels = []

        def handle(request):
            if request.url.path.endswith("access_token"):
                return httpx.Response(200, json={"access_token": "jeton", "expires_in": 3600})
            rome, page = request.url.params["rome"], int(request.url.params["page"])
            code = request.url.params.get("citycode")
            appels.append((rome, code, page))
            if not code or (refus and refus(rome, code)):
                return httpx.Response(400, json={"message": "critères refusés"})
            if panne and panne(rome, code):
                return httpx.Response(500, json={"message": "erreur"})
            items = pages.get((rome, code), [])
            return httpx.Response(200, json={"items": items[page - 1] if page <= len(items) else []})
        http = httpx.Client(transport=httpx.MockTransport(handle))
        self.addCleanup(http.close)
        self.enterContext(patch.object(cp, "PAGE_API", page_size))
        self.enterContext(patch.dict(os.environ, {"JOBHUNTER_FAKE_FT": ""}))
        self.enterContext(patch.object(cp, "PHASES", {"A": (("Test", (("M1811", "Data engineer"), ("M1805", "Dév"))),),
                                                      "B": (("Test", (("M1204", "Contrôleur de gestion"),)),)}))
        self.enterContext(patch.object(cp, "CENTRES_IDF", self.CENTRES))
        self.enterContext(patch.object(cp, "VILLES", self.VILLES))
        self.enterContext(patch.object(cp, "PETITES_VILLES", {}))
        return sp.LaBonneBoiteClient(self.settings, client=http), appels

    def test_ile_de_france_first_then_main_cities_all_pages_dedup_exclusions_and_order(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO opportunities (source, external_id, siret, company_name) VALUES "
                       "('labonneboite', 'x', '00000000000004', 'Ancienne')")
            db.execute("INSERT INTO application_events (source, external_id, channel, event) VALUES "
                       "('labonneboite', '00000000000005', 'LABONNEBOITE', 'ERROR')")
            db.execute("INSERT INTO spontanees (siret, entreprise, rome, lot_du) VALUES ('00000000000006', 'Lot', 'M1405', '2026-10-31')")
        client, appels = self.api({
            ("M1811", "75056"): [[item("00000000000002", potential=90, postcode="75011"),
                                  item("00000000000001", potential=60, postcode="69001")]],  # hors IDF : ignorée ici
            ("M1805", "93008"): [[item("00000000000007", potential=95, postcode="93200")]],
            ("M1811", "69123"): [[item("00000000000001", potential=60, postcode="69001"), item("00000000000002", postcode="75011")],
                                 [item("00000000000003", email="no"), item("00000000000004")],
                                 [item("00000000000005")]],
            ("M1805", "2A004"): [[item("00000000000006"), item("00000000000008", postcode="20090")]],
            ("M1204", "75056"): [[item("00000000000009", postcode="75002", potential=99)]]})
        pauses = []
        state = cp.inventaire(self.settings, ["A"], client=client, pause=0.6, sleep=pauses.append)
        self.assertEqual(state["etat"], "termine")
        self.assertNotIn(None, [a[1] for a in appels])  # jamais de recherche sans ville (refusée par l'API)
        ordre_villes = list(dict.fromkeys(a[1] for a in appels))
        self.assertEqual(ordre_villes, ["75056", "93008", "69123", "2A004"])  # Île-de-France, puis département par département
        self.assertEqual([a for a in appels if a[1] == "69123"], [("M1811", "69123", 1), ("M1811", "69123", 2),
                                                                   ("M1811", "69123", 3), ("M1805", "69123", 1)])
        self.assertEqual(pauses, [0.6] * (len(appels) - 1))  # 2 appels par seconde au plus
        self.assertEqual(state["prevues"], 8)  # 4 villes × 2 métiers
        ordre = [e["siret"][-1] for e in cp.prochaines(self.db, ["A"], limite=10)]
        self.assertEqual(ordre, ["2", "7", "1", "8"])  # Paris, Seine-Saint-Denis, puis département par département
        self.assertEqual(cp.entreprise_par_siret(self.db, "00000000000008")["departement"], "2A")
        plan = cp.plan(self.db)
        self.assertEqual((plan["phases"]["A"]["eligibles"], plan["phases"]["B"]["eligibles"]), (4, 0))
        self.assertEqual(plan["deja_en_base"], 3)
        avant = len(appels)
        cp.inventaire(self.settings, ["A", "B"], client=client, pause=0, sleep=pauses.append)
        self.assertEqual({a[0] for a in appels[avant:]}, {"M1204"})  # A déjà fini : seule la phase B est lue
        self.assertEqual(cp.prochaines(self.db, ["A", "B"], limite=10)[-1]["siret"], "00000000000009")  # B après A

    def test_refused_code_or_city_is_skipped_noted_and_inventory_goes_on(self):
        client, appels = self.api({("M1811", "69123"): [[item("00000000000001", postcode="69001")]]},
                                  refus=lambda rome, code: rome == "M1805" or code == "2A004")
        state = cp.inventaire(self.settings, ["A"], client=client, pause=0)
        self.assertEqual(state["etat"], "termine")
        self.assertEqual(state["refus"], ["Code métier refusé et sauté : M1805 (refusé autour de Paris, accepté pour "
                                          "d'autres métiers)", "Ville refusée et sautée : Ajaccio (2A, code 2A004)"])
        self.assertEqual(len([a for a in appels if a[0] == "M1805"]), 1)  # code refusé : plus jamais demandé
        self.assertEqual([a for a in appels if a[1] == "2A004"], [("M1811", "2A004", 1)])
        self.assertEqual([e["siret"] for e in cp.prochaines(self.db, ["A"])], ["00000000000001"])

    def test_a_passing_server_error_is_retried_after_10_s_then_1_min(self):
        restants = [2]  # deux erreurs 500, puis l'API répond

        def panne(rome, code):
            if code == "69123" and restants[0]:
                restants[0] -= 1
                return True
            return False
        client, appels = self.api({("M1811", "69123"): [[item("00000000000001", postcode="69001")]]}, panne=panne)
        attentes = []
        state = cp.inventaire(self.settings, ["A"], client=client, pause=0, sleep=attentes.append)
        self.assertEqual(state["etat"], "termine")
        self.assertEqual([a for a in attentes if a], [10, 60])
        self.assertEqual(state["reessais"], 2)
        self.assertEqual([a for a in appels if a[:2] == ("M1811", "69123")], [("M1811", "69123", 1)] * 3)
        self.assertEqual([e["siret"] for e in cp.prochaines(self.db, ["A"])], ["00000000000001"])

    def test_site_search_drops_the_leading_zero_of_the_department(self):
        """Le 07/11 : « 01 » ne trouve rien sur le site, « 1 » trouve les entreprises de l'Ain."""
        for dep, attendu in (("01", "1"), ("03", "3"), ("09", "9"), ("75", "75"), ("2A", "2A"), ("971", "971"),
                             ("", "")):
            self.assertEqual(cp.departement_site(dep), attendu, dep)
        url = cp.url_recherche({"rome": "M1855", "departement": "01"})
        self.assertIn("department_number=1&", url)
        self.assertIn("department_number=75&", cp.url_recherche({"rome": "M1855", "departement": "75"}))
        self.assertEqual(cp.MOTIFS_IGNOREE["introuvable"], "Introuvable sur le site")

    def test_other_errors_stop_with_a_clear_message_and_the_next_run_resumes(self):
        en_panne = [True]
        client, appels = self.api({("M1811", "69123"): [[item("00000000000001", postcode="69001")]],
                                   ("M1811", "75056"): [[item("00000000000002", postcode="75011")]]},
                                  panne=lambda rome, code: code == "69123" and en_panne[0])
        attentes = []
        with self.assertRaises(cp.CampagneError) as ctx:
            cp.inventaire(self.settings, ["A"], client=client, pause=0, sleep=attentes.append)
        self.assertEqual([a for a in attentes if a], [10, 60])  # deux nouveaux essais avant d'abandonner
        self.assertIn("(après 3 essais)", str(ctx.exception))
        self.assertTrue(str(ctx.exception).startswith("Inventaire interrompu autour de Lyon (69) : "), str(ctx.exception))
        self.assertIn("relancez l'inventaire pour reprendre", str(ctx.exception))
        self.assertEqual(cp.etat_inventaire(self.db)["etat"], "erreur")
        self.assertEqual([e["siret"] for e in cp.prochaines(self.db, ["A"])], ["00000000000002"])  # gardée
        en_panne[0], avant = False, len(appels)
        state = cp.inventaire(self.settings, ["A"], client=client, pause=0, sleep=attentes.append)
        self.assertEqual(state["etat"], "termine")
        self.assertNotIn("75056", {a[1] for a in appels[avant:]})  # l'Île-de-France n'est pas relue
        self.assertEqual(sorted(e["siret"] for e in cp.prochaines(self.db, ["A"], limite=10)),
                         ["00000000000001", "00000000000002"])

    def test_missing_form_flag_stops(self):
        sans_flag = {k: v for k, v in item("00000000000001").items() if k != "email"}
        client, _ = self.api({("M1811", "75056"): [[sans_flag]], ("M1805", "75056"): [[sans_flag]]})
        with self.assertRaisesRegex(cp.CampagneError, "champ « email » absent"):
            cp.inventaire(self.settings, ["A"], client=client, pause=0)
        self.assertEqual(cp.etat_inventaire(self.db)["etat"], "erreur")
        self.assertEqual(cp.plan(self.db)["phases"]["A"]["eligibles"], 0)

    def test_every_department_has_main_cities_with_valid_codes(self):
        from app.villes_campagne import PETITES_VILLES, VILLES
        self.assertEqual(set(VILLES), set(cp.DEPARTEMENTS) - set(cp.IDF))
        self.assertLessEqual(set(PETITES_VILLES), set(cp.DEPARTEMENTS) - set(cp.IDF))
        for table in (VILLES, PETITES_VILLES):
            for dep, villes in table.items():
                with self.subTest(dep=dep):
                    self.assertTrue(villes)
                    for code, nom in villes:
                        self.assertRegex(code, r"^(\d{5}|2[AB]\d{3})$")
                        self.assertTrue(code.startswith(dep if len(dep) == 3 else dep[:2]))
                        self.assertTrue(nom)
        codes = [c for _, _, c, _ in cp._zones()]
        self.assertEqual(len(codes), len(set(codes)))  # aucune ville en double
        self.assertEqual(sum(map(len, PETITES_VILLES.values())), 139)
        self.assertEqual(cp.recherches_prevues(["A"]), (14 + 232 + 139) * 29)

    def test_new_inventory_redoes_every_search_and_only_adds_new_companies(self):
        pages = {("M1811", "75056"): [[item("00000000000001", postcode="75011")]]}
        client, appels = self.api(pages)
        with self.assertRaises(cp.CampagneError) as ctx:  # jamais avant la fin d'un premier inventaire
            cp.lancer_inventaire(self.settings, ["A"], nouveau=True)
        self.assertEqual(ctx.exception.status_code, 409)
        premier = cp.inventaire(self.settings, ["A"], client=client, pause=0)
        self.assertEqual((premier["ajoutees"], cp.plan(self.db)["inventaire"]["restantes"]), (1, 0))
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE campagne SET statut = 'envoyee', motif = 'gardé' WHERE siret = '00000000000001'")
        pages[("M1811", "75056")] = [[item("00000000000001", postcode="75011"), item("00000000000002", postcode="75012")]]
        avant = len(appels)
        state = cp.inventaire(self.settings, ["A"], client=client, pause=0, nouveau=True)
        self.assertEqual(state["etat"], "termine")
        self.assertEqual({a[:2] for a in appels[avant:]}, {a[:2] for a in appels[:avant]})  # toutes refaites
        self.assertEqual((state["ajoutees"], state["precedent"]["fin"]), (1, premier["fin"]))
        e = cp.entreprise_par_siret(self.db, "00000000000001")
        self.assertEqual((e["statut"], e["motif"]), ("envoyee", "gardé"))  # jamais reprise ni modifiée
        self.assertEqual([x["siret"] for x in cp.prochaines(self.db, ["A"], limite=10)], ["00000000000002"])
        inv = cp.plan(self.db)["inventaire"]
        self.assertNotIn("fait", inv)  # la liste détaillée des recherches n'est pas envoyée à l'écran
        self.assertEqual((inv["faites"], inv["restantes"]), (8, 0))

    def test_added_towns_only_add_their_own_searches(self):
        client, appels = self.api({("M1811", "69264"): [[item("00000000000003", postcode="69400")]]})
        cp.inventaire(self.settings, ["A"], client=client, pause=0)
        avant = len(appels)
        with patch.object(cp, "PETITES_VILLES", {"69": (("69264", "Villefranche-sur-Saône"),)}):
            self.assertEqual(cp.plan(self.db)["inventaire"]["restantes"], 2)  # 1 ville × 2 métiers
            state = cp.inventaire(self.settings, ["A"], client=client, pause=0)
            self.assertEqual(cp.plan(self.db)["inventaire"]["restantes"], 0)
        self.assertEqual(appels[avant:], [("M1811", "69264", 1), ("M1805", "69264", 1)])  # rien d'autre n'est refait
        self.assertEqual(state["ajoutees"], 1)
        self.assertEqual([x["siret"] for x in cp.prochaines(self.db, ["A"])], ["00000000000003"])

    def test_fake_inventory_and_manual_company(self):
        cp.inventaire(self.settings, ["A", "B"])
        a = cp.prochaines(self.db, ["A"], limite=50)
        self.assertEqual(len({e["siret"] for e in a}), len(a))
        self.assertNotIn("00000000000505", {e["siret"] for e in a})  # sans e-mail : jamais ciblée
        self.assertEqual(a[0]["departement"], "75")
        self.assertEqual(a[0]["nom_message"], "Livraison Exemple France")
        manuelle = cp.ajouter_manuelle(self.db, "123 456 789 01234", "GRAPSTOR", "M1811", "75")
        self.assertEqual((manuelle["siret"], manuelle["source"], manuelle["phase"], manuelle["departement"]),
                         ("12345678901234", "manuel", "A", "75"))
        for args in (("123", "X", "M1811", "75"), ("12345678901235", "X", "Z9999", "75"),
                     ("12345678901235", " ", "M1811", "75"), ("12345678901235", "X", "M1811", ""),
                     ("12345678901235", "X", "M1811", "99")):
            with self.subTest(args=args), self.assertRaises(cp.CampagneError):
                cp.ajouter_manuelle(self.db, *args)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO spontanees (siret, entreprise, rome, lot_du) VALUES ('00000000000999', 'L', 'M1405', 'x')")
        with self.assertRaisesRegex(cp.CampagneError, "déjà dans votre base"):
            cp.ajouter_manuelle(self.db, "00000000000999", "L", "M1811", "75")

    def test_departments(self):
        for cp_, dep in (("75011", "75"), ("20090", "2A"), ("20200", "2B"), ("97411", "974"), ("06000", "06"), ("", ""), ("ABCDE", "")):
            self.assertEqual(cp.departement(cp_), dep)
        self.assertEqual(cp.DEPARTEMENTS[:8], cp.IDF)
        self.assertEqual(len(cp.DEPARTEMENTS), len(set(cp.DEPARTEMENTS)))


class StatutsEtHistoriqueTests(Base):
    def test_transitions_and_crash_recovery_never_retry(self):
        for n in (1, 2, 3):
            self.ligne(f"0000000000010{n}")
        cp.marquer(self.db, "00000000000101", cp.EN_COURS)
        cp.marquer(self.db, "00000000000102", cp.EN_COURS)
        cp.marquer(self.db, "00000000000102", cp.TENTEE, version="A", tentee_le=MIDI.isoformat())
        with self.assertRaises(cp.CampagneError):
            cp.marquer(self.db, "00000000000102", cp.A_FAIRE)  # une entreprise tentée n'est jamais remise à faire
        with self.assertRaises(cp.CampagneError):
            cp.marquer(self.db, "00000000000103", cp.ENVOYEE)
        self.assertEqual(sorted(cp.reprise_apres_plantage(self.db)), ["00000000000101", "00000000000102"])
        self.assertEqual(cp.entreprise_par_siret(self.db, "00000000000101")["statut"], cp.ERREUR)
        self.assertEqual(cp.entreprise_par_siret(self.db, "00000000000102")["statut"], cp.INCERTAINE)
        self.assertEqual([e["siret"] for e in cp.prochaines(self.db, ["A"], limite=10)], ["00000000000103"])
        envoyee = cp.resoudre_incertaine(self.db, "00000000000102", cp.ENVOYEE)
        self.assertEqual((envoyee["statut"], envoyee["envoyee_le"]), (cp.ENVOYEE, MIDI.isoformat()))
        with self.assertRaises(cp.CampagneError):
            cp.resoudre_incertaine(self.db, "00000000000102", cp.ERREUR)

    def test_history_filters_pages_replies_stats_and_csv(self):
        for n in range(60):
            self.ligne(f"00000000002{n:03d}", cp.ENVOYEE, envoyee_le=(MIDI - timedelta(minutes=n)).isoformat(),
                       version="AB"[n % 2], phase="AB"[n >= 50], entreprise=f"ENTREPRISE {n:03d}")
        self.ligne("00000000003000", cp.IGNOREE)
        self.ligne("00000000003001", cp.A_FAIRE)
        self.ligne("00000000003002", cp.ENVOYEE, envoyee_le=MIDI.isoformat(), entreprise="=HYPERLIEN(\"x\")")
        page1 = cp.historique(self.db)
        self.assertEqual((page1["total"], page1["pages"], len(page1["items"])), (62, 2, 50))  # « à faire » exclues
        self.assertEqual(len(cp.historique(self.db, page=2)["items"]), 12)
        self.assertEqual(cp.historique(self.db, phase="B")["total"], 10)
        self.assertEqual(cp.historique(self.db, statut=cp.IGNOREE)["total"], 1)
        self.assertEqual(cp.historique(self.db, recherche="ENTREPRISE 01")["total"], 10)
        with self.assertRaises(cp.CampagneError):
            cp.historique(self.db, statut="nimporte")
        cp.noter_reponse(self.db, "00000000002000", "positive")
        cp.noter_reponse(self.db, "00000000002001", "refus")
        with self.assertRaises(cp.CampagneError):
            cp.noter_reponse(self.db, "00000000003000", "positive")  # ignorée : pas de réponse possible
        with self.assertRaises(cp.CampagneError):
            cp.noter_reponse(self.db, "00000000002000", "peut-être")
        self.assertEqual(cp.historique(self.db, reponse="positive")["total"], 1)
        self.assertEqual(cp.historique(self.db, reponse="aucune", statut=cp.ENVOYEE)["total"], 59)
        stats = {s["version"]: s for s in cp.stats_versions(self.db)}
        self.assertEqual((stats["A"]["envoyees"], stats["A"]["positive"], stats["B"]["refus"], stats["B"]["reponses"]),
                         (30, 1, 1, 1))
        csv_ = cp.export_csv(self.db)
        self.assertTrue(csv_.startswith("﻿siret;entreprise;"))
        self.assertEqual(len(csv_.strip().splitlines()), 63)
        self.assertIn(""";"'=HYPERLIEN""", csv_)  # aucune formule dans le tableur
        self.assertNotIn(";=HYPERLIEN", csv_)
        self.assertIn(";Envoyée;", csv_)
        self.assertIn(";Positive;", csv_)
        self.assertEqual(len(cp.export_csv(self.db, phase="B").strip().splitlines()), 11)

    def test_counters_of_the_day_and_total(self):
        hier = (MIDI - timedelta(days=1)).isoformat()
        self.ligne("00000000000301", cp.ENVOYEE, envoyee_le=MIDI.isoformat())
        self.ligne("00000000000302", cp.ENVOYEE, envoyee_le=hier)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO campagne (siret, entreprise, rome, phase, statut, motif, maj_le) VALUES "
                       "('00000000000303', 'X', 'M1811', 'A', 'ignoree', 'sans_email', ?)", (MIDI.isoformat(),))
        self.ligne("00000000000304", cp.A_FAIRE, phase="B")
        c = cp.compteurs(self.db, MIDI)
        self.assertEqual((c["jour"]["envoyees"], c["total"]["envoyees"], c["jour"]["ignorees"]["sans_email"],
                          c["restantes"]), (1, 2, 1, {"A": 0, "B": 1}))

    def test_first_run_steps(self):
        passage = cp.lire_passage(self.db)
        self.assertEqual(cp.etape_passage(passage), 1)
        for changes, etape in (({"metiers_valides": True}, 2), ({"essai": {"siret": "x"}}, 3),
                               ({"regle": {"bouton": "B"}}, 3), ({"regle_validee": True}, 4), ({"cinq_valide": True}, 5)):
            passage = cp.ecrire_passage(self.db, **changes)
            self.assertEqual(cp.etape_passage(passage), etape)
        self.assertEqual(cp.regle_confirmation(self.db), {"bouton": "B"})

    def test_only_the_confirmation_page_proves_a_send(self):
        regle = {"bouton": "Envoyer la candidature", "url_motif": "/candidature/envoyee",
                 "texte": "Votre candidature a bien été envoyée"}
        confirmation = "Candidature envoyée\nFélicitation, votre candidature est envoyée !\nVotre candidature a bien été envoyée le 5 nov. 2026."
        synthese = "Synthèse de votre candidature spontanée\nDate d'envoi : 05/11/2026 18:55\nVotre candidature a bien été envoyée"
        self.assertTrue(cp.est_confirmation(regle, "/candidature/envoyee", confirmation))
        self.assertFalse(cp.est_confirmation(regle, "/candidature/recapitulatif/5354238", synthese))
        self.assertFalse(cp.est_confirmation(regle, "/candidature/envoyee", "Synthèse de votre candidature"))
        self.assertFalse(cp.est_confirmation(None, "/candidature/envoyee", confirmation))
        for mauvaise in ({**regle, "url_motif": "/candidature/recapitulatif/{id}"}, {**regle, "texte": "Date d'envoi"}):
            with self.subTest(regle=mauvaise):
                self.assertIn("pas une preuve", cp.probleme_regle(mauvaise))
                self.assertFalse(cp.est_confirmation(mauvaise, "/candidature/recapitulatif/1", synthese))
        self.assertIsNone(cp.probleme_regle(regle))

    def test_sent_by_hand_counts_today_and_is_never_retried(self):
        self.ligne("00000000000601", cp.BROUILLON)
        self.ligne("00000000000602", cp.ENVOYEE, envoyee_le=MIDI.isoformat())
        e = cp.envoyee_a_la_main(self.db, "00000000000601", envoyee_le=MIDI.isoformat())
        self.assertEqual((e["statut"], e["motif"], e["envoyee_le"]), (cp.ENVOYEE, cp.MOTIF_MAIN, MIDI.isoformat()))
        self.assertEqual(cp.compteurs(self.db, MIDI)["jour"]["envoyees"], 2)
        self.assertEqual(cp.envois_du_jour(self.db, MIDI), 2)
        self.assertEqual(cp.prochaines(self.db, ["A"]), [])
        for siret in ("00000000000601", "00000000000602"):
            with self.subTest(siret=siret), self.assertRaises(cp.CampagneError):
                cp.envoyee_a_la_main(self.db, siret)
        self.assertEqual(cp.historique(self.db, statut=cp.ENVOYEE)["items"][0]["motif"], cp.MOTIF_MAIN)

    def test_company_name_check(self):
        self.assertTrue(cp.meme_entreprise(["Exemplia Analytics", "EXEMPLIA ANALYTICS SAS"], "EXEMPLIA ANALYTICS SAS"))
        self.assertTrue(cp.meme_entreprise(["Uber Eats", ""], "UBER EATS FRANCE SAS"))
        self.assertTrue(cp.meme_entreprise(["GROUPON FRANCE"], " GROUPON FRANCE "))
        self.assertFalse(cp.meme_entreprise(["GRAPSTOR"], "SWORD SA"))
        self.assertFalse(cp.meme_entreprise(["GRAPSTOR"], ""))


class RoutesTests(Base):
    def setUp(self):
        super().setUp()
        for target in ("app.config.get_settings", "app.api.get_settings", "app.api_campagne.get_settings"):
            self.enterContext(patch(target, return_value=self.settings))
        self.enterContext(patch("app.robot_campagne.ROBOT_DIR", self.folder / "robot"))
        self.http = TestClient(app)

    def test_screen_inventory_settings_and_first_run_gates(self):
        data = self.http.get("/api/campagne").json()
        self.assertEqual((data["etape"], data["envois_du_jour"], data["objectif_max"], data["reglages"]["phase_b"],
                          data["mode_test"]), (1, 0, 1000, False, True))
        self.assertEqual(data["message"]["versions"]["A"]["nom"], "Uber Eats France")
        self.assertTrue(self.http.get("/api/campagne/acces").json()["ok"])
        self.assertTrue(self.http.post("/api/campagne/metiers/verifier").json()["ok"])
        self.http.post("/api/campagne/inventaire")
        for _ in range(100):
            if self.http.get("/api/campagne").json()["plan"]["inventaire"]["etat"] == "termine":
                break
        self.assertGreater(self.http.get("/api/campagne").json()["plan"]["phases"]["A"]["eligibles"], 10)
        eligibles = self.http.get("/api/campagne").json()["plan"]["phases"]["A"]["eligibles"]
        self.assertEqual(self.http.post("/api/campagne/inventaire", json={"nouveau": True}).status_code, 200)
        for _ in range(100):
            inv = self.http.get("/api/campagne").json()["plan"]["inventaire"]
            if inv["etat"] == "termine":
                break
        self.assertEqual((inv["ajoutees"], inv["restantes"]), (0, 0))  # même liste : rien de nouveau, rien en double
        self.assertEqual(self.http.get("/api/campagne").json()["plan"]["phases"]["A"]["eligibles"], eligibles)
        res = self.http.post("/api/campagne/robot", json={"mode": "essai", "siret": "00000000000101"})
        self.assertEqual(res.status_code, 409)
        self.assertIn("valider la liste des métiers", res.json()["detail"])
        self.http.post("/api/campagne/metiers/valider")
        res = self.http.post("/api/campagne/robot", json={"mode": "essai", "siret": "00000000000101"})
        self.assertIn("nom du CV", res.json()["detail"])
        res = self.http.put("/api/campagne/reglages", json={"objectif": 2000})
        self.assertEqual((res.status_code, res.json()["detail"]),
                         (400, "Le nombre de candidatures à envoyer doit être un nombre entier entre 1 et 1000."))
        self.assertEqual(self.http.put("/api/campagne/reglages", json={"nom_cv": "CV_Camille_EXEMPLE"}).status_code, 200)
        for mode, attendu in (("calibrage", "faire l'essai"), ("cinq", "faire l'essai"), ("campagne", "faire l'essai")):
            with self.subTest(mode=mode):
                self.assertIn(attendu, self.http.post("/api/campagne/robot", json={"mode": mode}).json()["detail"])
        self.assertEqual(self.http.post("/api/campagne/robot", json={"mode": "turbo"}).status_code, 400)
        self.assertEqual(self.http.post("/api/campagne/robot", json={"mode": "essai"}).status_code, 400)
        self.assertEqual(self.http.post("/api/campagne/regle/valider", json={"bouton": "a", "url_motif": "/x", "texte": "t"}).status_code, 409)
        self.assertEqual(self.http.post("/api/campagne/cinq/valider").status_code, 409)
        cp.poser_alerte(self.db, "captcha", "Captcha affiché")
        res = self.http.post("/api/campagne/robot", json={"mode": "essai", "siret": "00000000000101"})
        self.assertIn("Aucune reprise avant demain", res.json()["detail"])

    def test_rule_validation_marks_the_calibration_send(self):
        self.ligne("00000000000401", cp.INCERTAINE, tentee_le=MIDI.isoformat())
        cp.ecrire_passage(self.db, calibrage={"siret": "00000000000401"},
                          regle={"bouton": "Envoyer la candidature", "url_motif": "/candidature/envoyee", "texte": "envoyée"})
        res = self.http.post("/api/campagne/regle/valider", json={"bouton": "Envoyer la candidature", "url_motif": "envoyee",
                                                                   "texte": "envoyée"})
        self.assertEqual(res.status_code, 400)
        res = self.http.post("/api/campagne/regle/valider", json={"bouton": "Envoyer la candidature",
                                                                   "url_motif": "/candidature/envoyee", "texte": "Candidature envoyée"})
        self.assertTrue(res.json()["regle_validee"])
        self.assertEqual(res.json()["regle"]["texte"], "Candidature envoyée")
        self.assertEqual(cp.entreprise_par_siret(self.db, "00000000000401")["statut"], cp.ENVOYEE)
        res = self.http.post("/api/campagne/regle/valider", json={"bouton": "Envoyer la candidature",
                             "url_motif": "/candidature/recapitulatif/{id}", "texte": "Date d'envoi"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("pas une preuve", res.json()["detail"])
        self.ligne("00000000000402", cp.BROUILLON)
        self.assertEqual(self.http.post("/api/campagne/00000000000402/main").json()["motif"], cp.MOTIF_MAIN)
        self.assertEqual(self.http.post("/api/campagne/00000000000402/main").status_code, 409)

    def test_history_export_replies_and_captures_routes(self):
        self.ligne("00000000000501", cp.ENVOYEE, envoyee_le=MIDI.isoformat(), version="A")
        self.ligne("00000000000502", cp.INCERTAINE, tentee_le=MIDI.isoformat())
        self.assertEqual(self.http.get("/api/campagne/historique?statut=envoyee").json()["total"], 1)
        self.assertEqual(self.http.get("/api/campagne/historique?statut=bidon").status_code, 400)
        self.assertEqual(self.http.post("/api/campagne/00000000000501/reponse", json={"reponse": "positive"}).json()["reponse"], "positive")
        self.assertEqual(self.http.post("/api/campagne/00000000000502/reponse", json={"reponse": "positive"}).status_code, 409)
        self.assertEqual(self.http.post("/api/campagne/00000000000502/statut", json={"statut": "a_faire"}).status_code, 400)
        self.assertEqual(self.http.post("/api/campagne/00000000000502/statut", json={"statut": "erreur"}).json()["statut"], "erreur")
        res = self.http.get("/api/campagne/export.csv?statut=envoyee")
        self.assertEqual(res.headers["content-type"], "text/csv; charset=utf-8")
        self.assertIn("attachment", res.headers["content-disposition"])
        self.assertEqual(len(res.text.strip().splitlines()), 2)
        robot = self.http.get("/api/campagne/robot").json()
        self.assertEqual((robot["actif"], robot["id"]), (False, None))
        self.assertEqual(self.http.post("/api/campagne/robot/pause").status_code, 409)
        self.assertEqual(self.http.post("/api/campagne/robot/passer").status_code, 409)
        self.assertFalse(self.http.post("/api/campagne/robot/arret").json()["actif"])
        for path in ("20261105-120000/..%2F..%2Fjobs.db", "20261105-120000/journal.txt", "x/01-a.png", "20261105-120000/01-a.png"):
            with self.subTest(path=path):
                self.assertIn(self.http.get(f"/api/campagne/robot/captures/{path}").status_code, (404, 400))
        self.assertEqual(self.http.post("/api/campagne/entreprises", json={"siret": "12345678901234", "entreprise": "GRAPSTOR",
                                                                            "rome": "M1811"}).status_code, 400)
        manuelle = self.http.post("/api/campagne/entreprises", json={"siret": "12345678901234", "entreprise": "GRAPSTOR",
                                                                      "rome": "M1811", "departement": "75"})
        self.assertEqual(manuelle.json()["nom_message"], "Grapstor")
        trouvees = self.http.get("/api/campagne/entreprises?recherche=grapstor").json()["items"]
        self.assertEqual([e["siret"] for e in trouvees], ["12345678901234"])
        self.assertEqual(self.http.get("/api/campagne/apercu?entreprise=SWORD%20SA").json()["versions"]["A"]["nom"], "Sword")


if __name__ == "__main__":
    unittest.main()
