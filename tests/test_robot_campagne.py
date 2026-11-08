"""Robot de la campagne contre le faux site La Bonne Boîte : navigateur sans fenêtre, temps accéléré, mode test.

Chaque test lance le vrai processus du robot (comme le serveur), avec une base, des modèles et des dossiers temporaires.
Aucune requête ne quitte la machine (proxy inexistant, requêtes hors du faux site bloquées et journalisées).
"""

from contextlib import closing
import json
import os
from pathlib import Path
import re
import signal
import socket
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from app import campagne as cp
from app import robot_campagne as robot
from app.config import Settings
from app.database import init_db
from tests.faux_site_lbb import EMAIL, PRENOM, demarrer

REGLE = {"bouton": "Envoyer la candidature", "url_motif": "/candidature/envoyee", "texte": "Candidature envoyée"}
MODELE_A = "Madame, Monsieur,\n\nVersion A : candidature spontanée chez {entreprise}. 🙂 100% #data\n\nCamille EXEMPLE\n"
MODELE_B = "Bonjour,\n\nVersion B : candidature spontanée chez {entreprise}.\n\nCamille EXEMPLE\n"


def _port_libre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class RobotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = f"http://127.0.0.1:{_port_libre()}"
        cls.server = demarrer(int(cls.site.rsplit(":", 1)[1]))

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True

    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.db = self.folder / "jobs.db"
        init_db(self.db)
        self.settings = Settings(database_path=self.db)
        (self.folder / "modele.txt").write_text(MODELE_A, encoding="utf-8")
        self.enterContext(patch.dict(os.environ, {
            "JOBHUNTER_FAKE_FT": "1", "JOBHUNTER_LBB_SITE": self.site, "JOBHUNTER_ROBOT_INVISIBLE": "1",
            "JOBHUNTER_ROBOT_ACCELERE": "1000", "JOBHUNTER_MODELE_SPONTANE": str(self.folder / "modele.txt"),
            "HTTPS_PROXY": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9", "NO_PROXY": "127.0.0.1,localhost"}))
        self.enterContext(patch.object(cp, "MODELE_A", self.folder / "modele.txt"))
        self.enterContext(patch.object(cp, "MODELE_B", self.folder / "modele_B.txt"))
        self.enterContext(patch.object(robot, "ROBOT_DIR", self.folder / "robot"))
        self.faux("/__test__/reinitialiser")
        self.faux("/__test__/documents", noms=["CV_Camille_EXEMPLE.pdf", "Lettre_type.docx", "CV_Camille_EXEMPLE_Anglais.pdf"])
        cp.inventaire(self.settings, ["A", "B"])
        cp.ecrire_reglages(self.db, {"nom_cv": "CV_Camille_EXEMPLE"})
        self.ordre = [e["siret"] for e in cp.prochaines(self.db, ["A"], limite=100)]
        self.addCleanup(self.attendre_processus)

    @staticmethod
    def attendre_processus():
        for proc in robot._PROCESSUS.values():
            try:
                proc.wait(timeout=15)
            except Exception:  # noqa: BLE001
                proc.kill()
        robot._PROCESSUS.clear()

    # Outils

    def faux(self, path, **data):
        return httpx.post(self.site + path, json=data or None, timeout=10).json()

    def candidatures(self):
        return [c for c in httpx.get(self.site + "/__test__/etat", timeout=10).json()["candidatures"] if not c.get("ancienne")]

    def statut(self, siret):
        return cp.entreprise_par_siret(self.db, siret)

    def passage_fait(self):
        cp.ecrire_passage(self.db, metiers_valides=True, essai={"siret": "x"}, regle=REGLE, regle_validee=True,
                          cinq={"envoyees": 5}, cinq_valide=True)

    def lancer(self, mode, siret=None, pendant=None, connecte=True, nombre=None):
        if connecte:
            self.faux("/__test__/connexion")
        robot.lancer_robot(self.settings, mode, siret, nombre)
        fin = time.monotonic() + 120
        while time.monotonic() < fin:
            etat = robot.etat_robot()
            if pendant:
                pendant(etat)
            if not etat["actif"]:
                return etat
            time.sleep(0.2)
        robot.arreter_robot()
        self.fail("Le robot ne s'est pas arrêté à temps.")

    def assert_sans_coordonnees(self, etat):
        run = robot.ROBOT_DIR / etat["id"]
        textes = "\n".join(etat["journal"]) + (run / "etat.json").read_text(encoding="utf-8")
        for secret in (EMAIL, PRENOM, "Prénom", "coordonnées"):  # « Vos coordonnées » n'est jamais lu
            self.assertNotIn(secret, textes)

    # Premier passage

    def test_trial_waits_for_login_reaches_summary_lists_buttons_and_never_sends(self):
        cp.ecrire_passage(self.db, metiers_valides=True)
        siret, vu = self.ordre[0], []

        def pendant(etat):
            if etat.get("attente") == "connexion" and not vu:
                time.sleep(1)  # le robot attend : il ne se connecte jamais lui-même
                vu.append(httpx.get(self.site + "/__test__/connecte").json()["connecte"])
                self.faux("/__test__/connexion")
        etat = self.lancer("essai", siret, pendant, connecte=False)
        self.assertEqual(vu, [False])
        self.assertEqual(etat["statut"], "termine", etat["journal"])
        self.assertIn("rien n'a été envoyé", etat["resume"])
        journal = "\n".join(etat["journal"])
        self.assertIn("Requête bloquée vers example.org", journal)
        self.assertIn("CV « CV_Camille_EXEMPLE (document PDF) » coché, seul.", journal)
        self.assertIn("Envoyer la candidature", etat["boutons"])
        self.assertFalse([b for b in etat["boutons"] if PRENOM in b])  # en-tête exclu : votre nom y figure
        self.assertTrue(etat["captures"][-1]["nom"].endswith("-synthese.png"))
        (candidature,) = self.candidatures()
        self.assertEqual((candidature["siret"], candidature["etat"]), (siret, "draft"))  # brouillon, jamais envoyé
        self.assertEqual(candidature["documents"], ["CV_Camille_EXEMPLE.pdf"])  # un seul document : le CV désigné
        self.assertEqual(candidature["message"], cp.preparer_message(MODELE_A, self.statut(siret)["entreprise"])["texte"])
        self.assertNotIn("Bonjour,\n\n   Votre entreprise", candidature["message"])  # message par défaut remplacé
        self.assertNotRegex(candidature["message"], "[🙂%#]")
        e = self.statut(siret)
        self.assertEqual((e["statut"], e["candidature_id"], e["version"]), (cp.BROUILLON, str(candidature["id"]), "A"))
        essai = cp.lire_passage(self.db)["essai"]
        self.assertEqual((essai["siret"], essai["ecarts"]), (siret, []))
        self.assert_sans_coordonnees(etat)

    def test_a_send_made_by_hand_after_the_trial_is_recorded_once(self):
        regle = {**REGLE, "texte": "Votre candidature a bien été envoyée"}
        cp.ecrire_passage(self.db, metiers_valides=True, regle=regle, regle_validee=True)
        self.enterContext(patch.dict(os.environ, {"JOBHUNTER_ROBOT_FENETRE": "8"}))
        siret, clic = self.ordre[0], []

        def pendant(etat):
            if etat.get("fenetre_ouverte") and not clic:  # vous envoyez vous-même dans la fenêtre restée ouverte
                clic.append(True)
                self.faux("/__test__/clic", action="Envoyer la candidature")
            if clic and self.statut(siret)["statut"] == cp.ENVOYEE:
                robot.arreter_robot()
        etat = self.lancer("essai", siret, pendant)
        self.assertEqual(etat["statut"], "termine", etat["journal"])
        e = self.statut(siret)
        self.assertEqual((e["statut"], e["motif"]), (cp.ENVOYEE, cp.MOTIF_MAIN))
        self.assertIn("envoyée à la main", "\n".join(robot.etat_robot()["journal"]))
        self.assertEqual([c["etat"] for c in self.candidatures()], ["sent"])
        self.assertEqual(cp.compteurs(self.db)["jour"]["envoyees"], 1)
        self.assertEqual(cp.prochaines(self.db, ["A"])[0]["siret"], self.ordre[1])  # jamais retentée

    def test_company_card_is_checked_before_the_form_even_for_manual_companies(self):
        cp.ecrire_passage(self.db, metiers_valides=True)
        cp.ajouter_manuelle(self.db, "00000000000505", "GRAPHE IMAGINAIRE", "M1805", "75")  # fiche : sans e-mail
        etat = self.lancer("essai", "00000000000505")
        self.assertEqual(etat["statut"], "termine", etat["journal"])
        e = self.statut("00000000000505")
        self.assertEqual((e["statut"], e["motif"]), (cp.IGNOREE, "sans_email"))
        cp.ajouter_manuelle(self.db, "12345678901234", "INCONNUE DU SITE", "M1811", "75")
        etat = self.lancer("essai", "12345678901234")
        self.assertIn("fiche introuvable", "\n".join(etat["journal"]))
        e = self.statut("12345678901234")
        self.assertEqual((e["statut"], e["motif"]), (cp.IGNOREE, "introuvable"))  # passée, jamais une erreur
        self.faux("/__test__/scenario", siret=self.ordre[0], type="deja_texte")
        self.lancer("essai", self.ordre[0])
        self.assertEqual(self.statut(self.ordre[0])["motif"], "deja_candidate")
        self.assertEqual(self.candidatures(), [])  # aucun formulaire validé, rien envoyé

    def test_no_email_refusal_after_the_click_is_ignored_without_alert(self):
        self.passage_fait()
        cp.ecrire_reglages(self.db, {"objectif": 2})
        self.faux("/__test__/scenario", siret=self.ordre[1], type="sans_email_envoi")
        etat = self.lancer("campagne")
        self.assertEqual((etat["statut"], etat["alerte"]), ("termine", None), etat["journal"])
        e = self.statut(self.ordre[1])
        self.assertEqual((e["statut"], e["motif"]), (cp.IGNOREE, "sans_email"))
        self.assertIsNone(cp.alerte_du_jour(self.db))
        envoyees = [c["siret"] for c in self.candidatures() if c["etat"] == "sent"]
        self.assertEqual(envoyees, [self.ordre[0], self.ordre[2]])  # le robot est passé à la suivante
        self.assertIn("rien n'est parti", "\n".join(etat["journal"]))

    def test_card_is_found_on_later_result_pages_after_a_homonym(self):
        """Comme le 06/11 : 10 résultats par page, entreprises sans e-mail mieux classées, pagination sans lien."""
        self.passage_fait()
        cp.ecrire_reglages(self.db, {"objectif": 3})
        self.faux("/__test__/remplissage", nombre=25)  # vos entreprises M1811 de Paris passent en page 3
        etat = self.lancer("campagne")
        self.assertEqual((etat["statut"], etat["alerte"]), ("termine", None), etat["journal"])
        envoyees = [c["siret"] for c in self.candidatures() if c["etat"] == "sent"]
        self.assertEqual(envoyees, self.ordre[:3])
        self.assertNotIn("79999999999999", envoyees)  # l'homonyme (autre SIRET) n'est jamais choisi
        self.assertNotIn("introuvable", "\n".join(etat["journal"]))

    def absentes(self, potentiels: dict[str, float]) -> None:
        """Entreprises de l'inventaire absentes du site (Paris, M1811)."""
        with closing(sqlite3.connect(self.db)) as db, db:
            for siret, potentiel in potentiels.items():
                db.execute("INSERT INTO campagne (siret, entreprise, raison_sociale, rome, metier, phase, departement, "
                           "potentiel) VALUES (?, ?, ?, 'M1811', 'Data engineer', 'A', '75', ?)",
                           (siret, f"ABSENTE {siret[-2:]}", f"ABSENTE {siret[-2:]}", potentiel))

    def test_companies_missing_from_the_site_are_ignored_and_the_run_goes_on(self):
        """Comme le 07/11 (OKTEO, WIZIO, RECYCLEA) : une fiche introuvable est passée, sans erreur ni alerte du jour."""
        self.passage_fait()
        absentes = {"80000000000001": 96, "80000000000002": 94, "80000000000003": 91.5, "80000000000004": 89,
                    "80000000000005": 85}  # intercalées entre les 4 premières entreprises trouvées (Paris, M1811…)
        self.absentes(absentes)
        etat = self.lancer("campagne", nombre=4)
        self.assertEqual((etat["statut"], etat["alerte"]), ("termine", None), etat["journal"])
        self.assertIsNone(cp.alerte_du_jour(self.db))
        self.assertEqual((etat["compteurs"]["erreurs"], etat["compteurs"]["ignorees"]), (0, 5))
        for siret in absentes:
            e = self.statut(siret)
            self.assertEqual((e["statut"], e["motif"]), (cp.IGNOREE, "introuvable"))
        self.assertEqual(cp.compteurs(self.db)["jour"]["ignorees"]["introuvable"], 5)
        envoyees = [c["siret"] for c in self.candidatures() if c["etat"] == "sent"]
        self.assertEqual(envoyees, self.ordre[:4])  # le robot a continué après chaque fiche introuvable
        self.assertEqual(cp.prochaines(self.db, ["A"])[0]["siret"], self.ordre[4])
        journal = "\n".join(etat["journal"])
        self.assertIn("annoncée(s)", journal)
        self.assertIn("nom absent de toutes ces pages, aucune page rechargée", journal)
        # Une page de confirmation restée à l'écran n'est jamais attribuée à une autre entreprise.
        self.assertNotIn("envoyée à la main", journal)
        # Capture seulement si une page a été lue pour l'entreprise ; ensuite, la mémoire des pages suffit.
        self.assertEqual(len([c for c in etat["captures"] if "fiche-introuvable" in c["nom"]]), 1)

    def test_ten_missing_companies_in_a_row_stay_to_do_and_the_robot_pauses_then_goes_on(self):
        self.passage_fait()
        absentes = [f"800000000001{n:02d}" for n in range(10)]
        self.absentes({siret: 199 - n for n, siret in enumerate(absentes)})  # devant toutes les autres
        etat = self.lancer("campagne", nombre=1)
        self.assertEqual((etat["statut"], etat["alerte"]), ("termine", None), etat["journal"])
        journal = "\n".join(etat["journal"])
        self.assertIn("10 fiches introuvables de suite : la recherche du site est peut-être en panne", journal)
        self.assertIn("Pause de 10 min : 10 fiches introuvables de suite", journal)
        self.assertEqual({self.statut(s)["statut"] for s in absentes}, {cp.A_FAIRE})  # rien de perdu
        self.assertEqual([c["siret"] for c in self.candidatures()], [self.ordre[0]])
        self.assertIsNone(cp.alerte_du_jour(self.db))

    def test_a_late_document_list_is_awaited(self):
        """Comme le 07/11 (AMILTONE) : la liste de vos documents arrive après le reste du formulaire."""
        self.passage_fait()
        self.faux("/__test__/scenario", siret=self.ordre[0], type="documents_lents")
        etat = self.lancer("campagne", nombre=1)
        self.assertEqual((etat["statut"], etat["alerte"]), ("termine", None), etat["journal"])
        (candidature,) = self.candidatures()
        self.assertEqual((candidature["siret"], candidature["etat"]), (self.ordre[0], "sent"))
        self.assertEqual(candidature["documents"], ["CV_Camille_EXEMPLE.pdf"])
        self.assertNotIn("rechargé", "\n".join(etat["journal"]))

    def test_an_empty_document_list_reloads_the_form_once(self):
        self.passage_fait()
        # La 2e : la 1re ouvre déjà son formulaire une fois pendant la connexion.
        self.faux("/__test__/scenario", siret=self.ordre[1], type="documents_absents")
        etat = self.lancer("campagne", nombre=2)
        self.assertEqual((etat["statut"], etat["alerte"]), ("termine", None), etat["journal"])
        journal = "\n".join(etat["journal"])
        self.assertEqual(journal.count("formulaire rechargé une fois"), 1, journal)
        self.assertEqual([(c["siret"], c["etat"]) for c in self.candidatures()],
                         [(self.ordre[0], "sent"), (self.ordre[1], "sent")])

    def test_no_document_at_all_sets_the_company_aside_and_keeps_it_to_do(self):
        self.passage_fait()
        self.faux("/__test__/documents", noms=[])
        with closing(sqlite3.connect(self.db)) as db, db:  # deux entreprises à faire seulement
            db.execute("UPDATE campagne SET statut = 'ignoree', motif = 'deja_candidate' WHERE statut = 'a_faire' "
                       "AND siret NOT IN (?, ?)", self.ordre[:2])
        etat = self.lancer("campagne", nombre=5)
        self.assertEqual((etat["statut"], etat["alerte"]), ("termine", None), etat["journal"])
        self.assertIn("celles mises de côté pendant cette exécution seront reprises la prochaine fois", etat["resume"])
        journal = "\n".join(etat["journal"])
        self.assertEqual(journal.count("la liste de vos documents reste vide"), 2)
        self.assertIsNone(cp.alerte_du_jour(self.db))
        for siret in self.ordre[:2]:  # rien n'a été créé sur le site : reprises plus tard, sans échec compté
            e = self.statut(siret)
            self.assertEqual((e["statut"], e["motif"]), (cp.A_FAIRE, None))
        self.assertEqual(len([c for c in etat["captures"] if c["legende"].endswith("étape 1/2 sans vos documents")]), 2)
        self.assertEqual(self.candidatures(), [])

    def test_a_department_with_a_leading_zero_is_searched_without_it(self):
        """Comme le 07/11 : « 01 » ne trouve rien sur le site, « 1 » trouve les entreprises de l'Ain."""
        self.passage_fait()
        with closing(sqlite3.connect(self.db)) as db, db:  # seule l'entreprise de l'Ain reste à faire
            db.execute("UPDATE campagne SET statut = 'ignoree', motif = 'deja_candidate' "
                       "WHERE statut = 'a_faire' AND siret != '00000000000701'")
        etat = self.lancer("campagne", nombre=1)
        self.assertEqual((etat["statut"], etat["alerte"]), ("termine", None), etat["journal"])
        (candidature,) = self.candidatures()
        self.assertEqual((candidature["siret"], candidature["etat"]), ("00000000000701", "sent"))
        recherches = httpx.get(self.site + "/__test__/etat", timeout=10).json()["recherches"]
        self.assertIn(["M1811", "1", 1], recherches)
        self.assertNotIn("01", [r[1] for r in recherches])

    def test_a_brief_detour_through_the_login_page_is_not_a_lost_session(self):
        self.passage_fait()
        cp.ecrire_reglages(self.db, {"objectif": 1})
        self.faux("/__test__/connexion")
        self.faux("/__test__/eclair")  # rafraîchissement de session pendant la recherche de la fiche
        etat = self.lancer("campagne")
        self.assertEqual((etat["statut"], etat["alerte"]), ("termine", None), etat["journal"])
        self.assertNotIn("Session France Travail perdue", "\n".join(etat["journal"]))
        self.assertFalse(httpx.get(self.site + "/__test__/etat").json()["eclair"])  # le détour a bien eu lieu
        self.assertEqual(self.statut(self.ordre[0])["statut"], cp.ENVOYEE)

    def test_server_error_after_the_click_is_uncertain_and_the_control_waits_for_the_list(self):
        self.passage_fait()
        self.faux("/__test__/scenario", siret=self.ordre[1], type="erreur_serveur")
        etat = self.lancer("campagne", nombre=2)
        self.assertEqual((etat["statut"], etat["alerte"]), ("termine", None), etat["journal"])  # le robot continue
        e = self.statut(self.ordre[1])
        self.assertEqual(e["statut"], cp.INCERTAINE)  # jamais de nouvel essai
        self.assertTrue(e["motif"].startswith("erreur serveur du site après le clic (/errors/500)"), e["motif"])
        self.assertEqual([c["siret"] for c in self.candidatures() if c["etat"] == "sent"], [self.ordre[0], self.ordre[2]])
        journal = "\n".join(etat["journal"])
        self.assertIn("apparaît dans « Gérer vos candidatures »", journal)  # liste chargée après coup : attendue
        self.assertNotIn("Contrôle impossible", journal)

    def test_persistent_504_pauses_and_retries_then_stops_without_alert_after_two_hours(self):
        """Comme le 06/11 à 12 h 33, mais le site reste en panne : 3 essais par page, une pause toutes les 2 erreurs
        5xx (10, 20, puis 30 min), et arrêt sans alerte après 5 pauses sans succès. Rien n'est perdu."""
        self.passage_fait()
        self.faux("/__test__/connexion")
        self.faux("/__test__/panne", chemin="/recherche", code=504, nombre=999)
        etat = self.lancer("campagne", nombre=50)
        self.assertEqual((etat["statut"], etat["alerte"]), ("arrete", None), etat["journal"])
        self.assertIn("Toujours des échecs après 5 pauses", etat["resume"])
        self.assertIsNone(cp.alerte_du_jour(self.db))
        journal = "\n".join(etat["journal"])
        self.assertEqual(journal.count("nouvel essai dans 30 s"), 12)  # 12 entreprises, 2 nouveaux essais chacune
        self.assertEqual(journal.count("Pause de 10 min : 2 erreurs 5xx de suite"), 1)
        self.assertEqual(journal.count("Pause de 20 min"), 1)
        self.assertEqual(journal.count("Pause de 30 min"), 3)
        lignes = etat["journal"]
        pause = next(i for i, l in enumerate(lignes) if "Pause de 10 min" in l)
        erreurs = [i for i, l in enumerate(lignes) if "ERREUR" in l and "HTTP 504" in l]
        self.assertLess(erreurs[1], pause)  # la pause suit la 2e erreur…
        self.assertLess(pause, erreurs[2])  # …et précède la 3e
        self.assertIn("HTTP 504 Gateway Timeout sur /recherche (page), réponse vide", journal)
        self.assertNotIn("?rome=", journal)  # adresses sans leurs paramètres
        for siret in self.ordre[:12]:  # panne du site : reprises plus tard, sans échec compté contre elles
            e = self.statut(siret)
            self.assertEqual((e["statut"], e["motif"]), (cp.A_FAIRE, None))
        self.assertIn("Pages chargées sur La Bonne Boîte pendant cette exécution :", journal)
        self.assertEqual(self.candidatures(), [])

    def test_a_transient_504_on_a_result_page_is_retried_without_error(self):
        self.passage_fait()
        cp.ecrire_reglages(self.db, {"objectif": 1})
        self.faux("/__test__/connexion")
        self.faux("/__test__/panne", chemin="/recherche", code=504, nombre=2)  # 2 échecs, puis le site répond
        etat = self.lancer("campagne")
        self.assertEqual((etat["statut"], etat["alerte"]), ("termine", None), etat["journal"])
        journal = "\n".join(etat["journal"])
        self.assertIn("nouvel essai dans 30 s", journal)
        self.assertIn("nouvel essai dans 2 min", journal)
        self.assertEqual(etat["compteurs"]["erreurs"], 0)
        self.assertEqual(self.statut(self.ordre[0])["statut"], cp.ENVOYEE)

    def test_result_pages_are_remembered_within_and_across_runs(self):
        """Pages lues retenues par métier et département : plus de relecture depuis la page 1 à chaque entreprise."""
        self.passage_fait()
        cp.ecrire_reglages(self.db, {"objectif": 1})
        self.faux("/__test__/remplissage", nombre=25, homonyme=False)  # entreprises M1811 de Paris en page 3
        self.lancer("campagne")
        pages = lambda: [r[2] for r in httpx.get(self.site + "/__test__/etat").json()["recherches"] if r[:2] == ["M1811", "75"]]
        self.assertEqual(pages(), [1, 2, 3])  # 1re entreprise : lecture jusqu'à sa page
        etat = self.lancer("campagne", nombre=2)  # nouvelle exécution le même jour : 2 de plus
        self.assertEqual(etat["statut"], "termine", etat["journal"])
        self.assertEqual(pages(), [1, 2, 3, 3])  # 2e entreprise M1811 : droit à la page 3, rien d'autre
        self.assertEqual([c["siret"] for c in self.candidatures() if c["etat"] == "sent"], self.ordre[:3])

    def test_a_server_error_page_with_content_names_its_code_without_personal_data(self):
        self.passage_fait()
        self.faux("/__test__/connexion")
        self.faux("/__test__/panne", chemin="/recherche", code=503, nombre=6,
                  corps="<h1>Service Unavailable</h1><p>Contact : camille.exemple@example.test, 06 12 34 56 78</p>")
        etat = self.lancer("campagne", nombre=1)  # 2 entreprises en erreur (3 essais chacune), pause, puis reprise
        self.assertEqual((etat["statut"], etat["alerte"]), ("termine", None), etat["journal"])
        journal = "\n".join(etat["journal"])
        self.assertIn("HTTP 503 Service Unavailable sur /recherche (page), réponse : « Service Unavailable Contact : "
                      "[e-mail masqué], [chiffres masqués] »", journal)
        self.assertIn("Pause de 10 min : 2 erreurs 5xx de suite", journal)
        self.assertNotIn("example.test", journal + etat["resume"])
        self.assertNotIn("page non reconnue", journal.lower())
        self.assertEqual([c["siret"] for c in self.candidatures()], [self.ordre[2]])

    def test_an_empty_429_pauses_then_the_robot_goes_on(self):
        self.passage_fait()
        self.faux("/__test__/connexion")
        self.faux("/__test__/panne", chemin="/recherche", code=429, nombre=1)
        etat = self.lancer("campagne", nombre=1)
        self.assertEqual((etat["statut"], etat["alerte"]), ("termine", None), etat["journal"])
        journal = "\n".join(etat["journal"])
        self.assertIn("code 429", journal)
        self.assertIn("Pause de 15 min : La Bonne Boîte demande de ralentir (code 429)", journal)
        e = self.statut(self.ordre[0])
        self.assertEqual((e["statut"], e["motif"]), (cp.A_FAIRE, None))  # reprise plus tard
        self.assertEqual([c["siret"] for c in self.candidatures()], [self.ordre[1]])
        self.assertIsNone(cp.alerte_du_jour(self.db))

    def test_a_name_with_ampersand_uses_vous_and_passes_the_summary_check(self):
        """Comme LH & TECH le 06/11 : le site modifie le « & » dans la lettre ; avec « vous », plus d'écart."""
        self.passage_fait()
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE campagne SET potentiel = 500 WHERE siret = '00000000000601'")  # traitée en premier
        etat = self.lancer("campagne", nombre=1)
        self.assertEqual((etat["statut"], etat["alerte"]), ("termine", None), etat["journal"])
        (candidature,) = self.candidatures()
        self.assertEqual((candidature["siret"], candidature["etat"]), ("00000000000601", "sent"))
        self.assertIn("chez vous.", candidature["message"])
        self.assertNotIn("&", candidature["message"])

    def test_a_summary_mismatch_is_always_captured_and_the_robot_goes_on(self):
        self.passage_fait()
        self.faux("/__test__/scenario", siret=self.ordre[0], type="lettre_modifiee")
        etat = self.lancer("campagne", nombre=1)
        self.assertEqual((etat["statut"], etat["alerte"]), ("termine", None), etat["journal"])
        ligne = next(l for l in etat["journal"] if "ÉCHEC" in l)
        self.assertIn("(texte de la lettre)", ligne)
        self.assertNotIn("None", ligne)
        self.assertRegex(ligne, r"capture \d+-[a-z0-9-]+-synthese\.png")
        e = self.statut(self.ordre[0])
        self.assertEqual(e["statut"], cp.ERREUR)  # jamais reprise : un nouvel essai referait le même écart
        self.assertTrue(e["candidature_id"])
        self.assertEqual([(c["siret"], c["etat"]) for c in self.candidatures()],
                         [(self.ordre[0], "draft"), (self.ordre[1], "sent")])  # rien d'envoyé pour la première

    def test_calibration_observes_your_click_and_proposes_the_rule(self):
        cp.ecrire_passage(self.db, metiers_valides=True)
        siret = self.ordre[0]
        self.lancer("essai", siret)

        def pendant(etat):
            if etat.get("attente") == "clic":
                self.faux("/__test__/clic", action="Envoyer la candidature")  # vous cliquez dans la fenêtre
        etat = self.lancer("calibrage", siret, pendant)
        self.assertEqual(etat["statut"], "termine", etat["journal"])
        passage = cp.lire_passage(self.db)
        self.assertEqual({k: passage["regle"][k] for k in REGLE}, REGLE)
        self.assertFalse(passage["regle_validee"])
        self.assertEqual(self.statut(siret)["statut"], cp.INCERTAINE)  # envoyée par vous, à confirmer par la règle
        self.assertEqual([c["etat"] for c in self.candidatures()], ["sent"])  # le brouillon de l'essai, envoyé une fois
        with self.assertRaisesRegex(cp.CampagneError, "valider les cinq envois|faire le calibrage"):
            robot.lancer_robot(self.settings, "campagne")

    # Envois

    def test_campaign_sends_the_chosen_number_with_versions_a_b_and_ignores_then_stops(self):
        self.passage_fait()
        (self.folder / "modele_B.txt").write_text(MODELE_B, encoding="utf-8")
        self.faux("/__test__/scenario", siret=self.ordre[1], type="deja_texte")
        self.faux("/__test__/scenario", siret=self.ordre[2], type="deja_envoyee")
        etat = self.lancer("campagne", nombre=4)
        self.assertEqual(etat["statut"], "termine", etat["journal"])
        self.assertIn("Objectif atteint : 4 candidature(s) envoyée(s).", etat["resume"])
        self.assertEqual(cp.lire_reglages(self.db)["objectif"], 4)  # nombre gardé pour le prochain lancement
        envoyees = self.candidatures()
        self.assertEqual([c["siret"] for c in envoyees], [self.ordre[0]] + self.ordre[3:6])
        self.assertTrue(all(c["etat"] == "sent" for c in envoyees))
        self.assertEqual([re.search("Version (.)", c["message"])[1] for c in envoyees], ["A", "B", "A", "B"])
        for siret in (self.ordre[1], self.ordre[2]):
            e = self.statut(siret)
            self.assertEqual((e["statut"], e["motif"]), (cp.IGNOREE, "deja_candidate"))
        e = self.statut(self.ordre[0])
        self.assertEqual((e["statut"], e["version"]), (cp.ENVOYEE, "A"))
        self.assertTrue(e["envoyee_le"] and e["tentee_le"] and e["candidature_id"])
        self.assertEqual(cp.envois_du_jour(self.db), 4)
        self.assertNotIn("Pause de", "\n".join(etat["journal"]))  # aucune pause entre les entreprises
        self.assertEqual(etat["captures"], [])  # en campagne, captures des erreurs seulement
        self.assertEqual({s["version"]: s["envoyees"] for s in cp.stats_versions(self.db)}, {"A": 2, "B": 2})
        self.assertRegex("\n".join(etat["journal"]), r"ENVOYÉE .*\[\d+ page\(s\) chargée\(s\)\]")
        self.assert_sans_coordonnees(etat)

    def test_five_sends_in_front_of_you_then_stop(self):
        cp.ecrire_passage(self.db, metiers_valides=True, essai={"siret": "x"}, regle=REGLE, regle_validee=True)
        etat = self.lancer("cinq")
        self.assertEqual(etat["statut"], "termine", etat["journal"])
        self.assertEqual(len(self.candidatures()), 5)
        self.assertEqual(cp.lire_passage(self.db)["cinq"]["envoyees"], 5)
        self.assertIn("Cinq envois faits", etat["resume"])

    def test_no_pause_between_companies_and_a_number_above_the_companies_left(self):
        self.passage_fait()
        etat = self.lancer("campagne", nombre=1000)  # plus que d'entreprises à faire : il s'arrête quand il n'en reste plus
        self.assertEqual(etat["statut"], "termine", etat["journal"])
        self.assertIn("Plus aucune entreprise à faire", etat["resume"])
        self.assertEqual(cp.prochaines(self.db, ["A"]), [])
        self.assertNotIn("Pause", "\n".join(etat["journal"]))
        with self.assertRaisesRegex(cp.CampagneError, "nombre de candidatures à envoyer"):
            robot.lancer_robot(self.settings, "campagne", nombre=0)

    # Alertes

    def test_an_unsolved_captcha_stops_and_blocks_the_rest_of_the_day(self):
        self.passage_fait()
        self.faux("/__test__/scenario", siret=self.ordre[1], type="captcha")
        etat = self.lancer("campagne")
        self.assertEqual((etat["statut"], etat["alerte"]), ("arrete", "captcha"), etat["journal"])
        self.assertIn("ne résout ni ne contourne jamais", etat["resume"])
        self.assertTrue(etat["captures"][-1]["nom"].endswith("-captcha.png"))
        self.assertIn("Résolvez-le vous-même dans la fenêtre du robot", "\n".join(etat["journal"]))
        self.assertEqual([c["siret"] for c in self.candidatures()], [self.ordre[0]])
        self.assertEqual(self.statut(self.ordre[1])["statut"], cp.A_FAIRE)  # rien de créé : reprise plus tard
        self.assertEqual(cp.alerte_du_jour(self.db)["type"], "captcha")
        for mode, siret in (("campagne", None), ("essai", self.ordre[2])):
            with self.subTest(mode=mode), self.assertRaisesRegex(cp.CampagneError, "Aucune reprise avant demain"):
                robot.lancer_robot(self.settings, mode, siret)

    def test_a_captcha_you_solve_lets_the_robot_go_on(self):
        self.passage_fait()
        self.faux("/__test__/scenario", siret=self.ordre[1], type="captcha")
        vu = []

        def pendant(etat):
            if etat.get("attente") == "captcha" and not vu:
                vu.append(True)
                self.faux("/__test__/captcha_resolu")  # vous le résolvez vous-même dans la fenêtre
        etat = self.lancer("campagne", nombre=2, pendant=pendant)
        self.assertEqual(vu, [True])
        self.assertEqual((etat["statut"], etat["alerte"]), ("termine", None), etat["journal"])
        self.assertIn("captcha résolu par vous", "\n".join(etat["journal"]))
        self.assertEqual([c["siret"] for c in self.candidatures()], [self.ordre[0], self.ordre[2]])
        e = self.statut(self.ordre[1])
        self.assertEqual((e["statut"], e["motif"]), (cp.A_FAIRE, None))  # reprise plus tard, sans échec compté
        self.assertIsNone(cp.alerte_du_jour(self.db))

    def test_a_captcha_on_the_summary_keeps_the_draft(self):
        self.passage_fait()
        self.faux("/__test__/scenario", siret=self.ordre[0], type="captcha_synthese")
        etat = self.lancer("campagne")
        self.assertEqual((etat["statut"], etat["alerte"]), ("arrete", "captcha"), etat["journal"])
        e = self.statut(self.ordre[0])
        self.assertEqual(e["statut"], cp.BROUILLON)  # reprise depuis sa Synthèse, jamais un 2e brouillon
        self.assertTrue(e["candidature_id"])
        self.assertEqual([c["etat"] for c in self.candidatures()], ["draft"])

    def test_a_limit_message_stops_for_the_day(self):
        self.passage_fait()
        self.faux("/__test__/scenario", siret=self.ordre[1], type="blocage")
        etat = self.lancer("campagne")
        self.assertEqual((etat["statut"], etat["alerte"]), ("arrete", "blocage"), etat["journal"])
        self.assertEqual(self.statut(self.ordre[0])["statut"], cp.ENVOYEE)
        self.assertEqual(self.statut(self.ordre[1])["statut"], cp.A_FAIRE)  # rien de créé : reprise un autre jour
        self.assertEqual(cp.alerte_du_jour(self.db)["type"], "blocage")

    def test_an_unknown_page_and_an_uncertain_send_are_set_aside_and_the_run_goes_on(self):
        self.passage_fait()
        self.faux("/__test__/scenario", siret=self.ordre[1], type="inconnue")
        self.faux("/__test__/scenario", siret=self.ordre[2], type="incertain")
        etat = self.lancer("campagne", nombre=3)
        self.assertEqual((etat["statut"], etat["alerte"]), ("termine", None), etat["journal"])
        self.assertIsNone(cp.alerte_du_jour(self.db))
        e = self.statut(self.ordre[1])
        self.assertEqual(e["statut"], cp.A_FAIRE)  # reprise une fois, lors d'une prochaine exécution
        self.assertTrue(e["motif"].startswith(robot.PREMIER_ECHEC + "page non reconnue"), e["motif"])
        self.assertEqual(self.statut(self.ordre[2])["statut"], cp.INCERTAINE)  # jamais de nouvel essai
        envoyees = [c["siret"] for c in self.candidatures() if c["etat"] == "sent"]
        self.assertEqual(envoyees, [self.ordre[0], self.ordre[2], self.ordre[3], self.ordre[4]])
        journal = "\n".join(etat["journal"])
        self.assertIn("apparaît dans « Gérer vos candidatures »", journal)  # contrôle de fin d'exécution
        self.assertEqual(journal.count("ÉCHEC"), 2)

    def test_continue_without_effect_is_clicked_again_as_for_viveris(self):
        """Comme VIVERIS le 07/11 : après « Continuer », le formulaire reste affiché. 2e clic ; sinon, mise de côté."""
        self.passage_fait()
        self.faux("/__test__/scenario", siret=self.ordre[0], type="continuer_sans_effet")
        self.faux("/__test__/scenario", siret=self.ordre[1], type="continuer_jamais")
        etat = self.lancer("campagne", nombre=2)
        self.assertEqual((etat["statut"], etat["alerte"]), ("termine", None), etat["journal"])
        journal = "\n".join(etat["journal"])
        self.assertEqual(journal.count("nouveau clic sur « Continuer »"), 2)
        self.assertEqual(self.statut(self.ordre[0])["statut"], cp.ENVOYEE)
        e = self.statut(self.ordre[1])
        self.assertEqual(e["statut"], cp.A_FAIRE)
        self.assertIn("après deux clics sur « Continuer »", e["motif"])
        self.assertEqual([c["siret"] for c in self.candidatures() if c["etat"] == "sent"], [self.ordre[0], self.ordre[2]])

    def test_three_failures_in_a_row_pause_then_go_on_and_a_second_failure_is_final(self):
        self.passage_fait()
        for siret in self.ordre[:3]:
            self.faux("/__test__/scenario", siret=siret, type="bouton_inactif")
        etat = self.lancer("campagne", nombre=1)
        self.assertEqual((etat["statut"], etat["alerte"]), ("termine", None), etat["journal"])
        self.assertIn("Pause de 10 min : 3 échecs de suite", "\n".join(etat["journal"]))
        self.assertEqual([self.statut(s)["statut"] for s in self.ordre[:3]], [cp.A_FAIRE] * 3)  # reprises une fois
        self.assertEqual([c["siret"] for c in self.candidatures()], [self.ordre[3]])
        etat = self.lancer("campagne", nombre=1)  # même panne pour ces 3 entreprises : cette fois, « erreur »
        self.assertEqual((etat["statut"], etat["alerte"]), ("termine", None), etat["journal"])
        self.assertEqual([self.statut(s)["statut"] for s in self.ordre[:3]], [cp.ERREUR] * 3)
        self.assertEqual([c["siret"] for c in self.candidatures()], [self.ordre[3], self.ordre[4]])
        self.assertIsNone(cp.alerte_du_jour(self.db))

    # Session, plantage, pause, arrêt, CV

    def test_lost_session_waits_for_your_reconnection_then_resumes(self):
        self.passage_fait()
        cp.ecrire_reglages(self.db, {"objectif": 3})
        self.faux("/__test__/scenario", siret=self.ordre[1], type="session")
        vu = []

        def pendant(etat):
            if etat.get("attente") == "reconnexion" and not vu:
                vu.append(True)
                time.sleep(1)
                self.faux("/__test__/connexion")  # vous vous reconnectez dans la fenêtre
        etat = self.lancer("campagne", pendant=pendant)
        self.assertEqual(vu, [True])
        self.assertEqual(etat["statut"], "termine", etat["journal"])
        e = self.statut(self.ordre[1])
        self.assertEqual((e["statut"], e["motif"]), (cp.A_FAIRE, None))  # rien de créé : reprise plus tard
        self.assertEqual([c["siret"] for c in self.candidatures()], [self.ordre[0], self.ordre[2], self.ordre[3]])

    def test_crash_never_retries_a_company_and_restart_continues(self):
        self.passage_fait()
        cp.ecrire_reglages(self.db, {"objectif": 3})
        self.faux("/__test__/scenario", siret=self.ordre[1], type="bouton_inactif")  # « Continuer » ne répond pas
        tue, nom = [], self.statut(self.ordre[1])["entreprise"]

        def pendant(etat):
            if not tue and etat.get("entreprise") == nom and "2/2" in (etat.get("etape") or ""):
                pid = json.loads((robot.ROBOT_DIR / etat["id"] / "etat.json").read_text())["pid"]
                os.killpg(pid, signal.SIGKILL)  # plantage brutal : robot et navigateur
                tue.append(pid)
        etat = self.lancer("campagne", pendant=pendant)
        self.assertTrue(tue)
        self.assertEqual(etat["statut"], "erreur")
        robot._PROCESSUS.clear()
        etat = self.lancer("campagne", nombre=2)  # la reprise après plantage passe l'entreprise interrompue en erreur
        self.assertEqual(etat["statut"], "termine", etat["journal"])
        self.assertEqual(self.statut(self.ordre[1])["statut"], cp.ERREUR)
        envoyees = [c["siret"] for c in self.candidatures() if c["etat"] == "sent"]
        self.assertEqual(envoyees, [self.ordre[0], self.ordre[2], self.ordre[3]])
        self.assertEqual(len(envoyees), len(set(envoyees)))  # aucun doublon

    def test_pause_resume_and_stop(self):
        self.passage_fait()
        cp.ecrire_reglages(self.db, {"objectif": 50})
        etapes = []

        def pendant(etat):
            compteurs = etat.get("compteurs") or {}
            if compteurs.get("envoyees") == 1 and not etapes:
                robot.pause_robot(True)
                etapes.append("pause")
            elif etat.get("statut") == "en_pause" and etapes == ["pause"]:
                time.sleep(1)
                envoyees = len(self.candidatures())
                etapes.append(envoyees)
                robot.pause_robot(False)
            elif compteurs.get("envoyees") == 2 and len(etapes) == 2:
                robot.arreter_robot()
                etapes.append("arret")
        etat = self.lancer("campagne", pendant=pendant)
        self.assertEqual(etapes[0], "pause")
        self.assertEqual(etat["statut"], "arrete")
        self.assertIsNone(etat["alerte"])
        self.assertIn("Arrêt demandé", etat["resume"])
        self.assertIsNone(cp.alerte_du_jour(self.db))  # un arrêt demandé n'est pas une alerte
        self.assertIn(len(self.candidatures()), (2, 3))
        statuts = {self.statut(s)["statut"] for s in self.ordre}
        self.assertNotIn(cp.EN_COURS, statuts)
        self.assertNotIn(cp.ERREUR, statuts)  # un arrêt demandé n'est pas une erreur (brouillon ou « à faire »)

    def test_blocked_login_page_is_named_clearly_without_alert(self):
        cp.ecrire_passage(self.db, metiers_valides=True)
        self.faux("/__test__/connexion_externe")  # la connexion part vers une adresse bloquée en mode test
        etat = self.lancer("essai", self.ordre[0], connecte=False)
        self.assertEqual((etat["statut"], etat["alerte"]), ("arrete", None), etat["journal"])
        self.assertIn("Page de connexion France Travail bloquée : "
                      "https://authentification-candidat.pole-emploi.fr/connexion/oauth2/authorize", etat["resume"])
        self.assertNotIn("jeton-fictif", "\n".join(etat["journal"]))  # jamais les paramètres de connexion
        self.assertNotIn("non reconnue", etat["resume"])
        self.assertIsNone(cp.alerte_du_jour(self.db))
        self.assertEqual(self.statut(self.ordre[0])["statut"], cp.A_FAIRE)
        self.assertEqual(self.candidatures(), [])

    def test_ambiguous_or_missing_cv_stops_without_alert_and_frees_the_company(self):
        self.passage_fait()
        cp.ecrire_reglages(self.db, {"nom_cv": "CV"})  # deux documents contiennent « CV »
        etat = self.lancer("campagne")
        self.assertEqual((etat["statut"], etat["alerte"]), ("arrete", None))
        self.assertIn("2 documents correspondent au CV « CV »", etat["resume"])
        self.assertEqual(self.statut(self.ordre[0])["statut"], cp.A_FAIRE)  # rien créé : rendue « à faire »
        self.assertEqual(self.candidatures(), [])


class JournalHttpTests(unittest.TestCase):
    """Texte écrit au journal pour une réponse en erreur (sans navigateur)."""

    @staticmethod
    def detail(code, texte, corps, retry_after=None):
        class Reponse:
            def text(self):
                if isinstance(corps, Exception):
                    raise corps
                return corps
        robot_ = SimpleNamespace(erreur_http={"code": code, "texte": texte, "chemin": "/recherche", "type": "page",
                                              "retry_after": retry_after, "reponse": Reponse()})
        return robot.Robot.detail_http(robot_)

    def test_empty_504_as_on_november_6(self):
        self.assertEqual(self.detail(504, "Gateway Timeout", ""), "HTTP 504 Gateway Timeout sur /recherche (page), réponse vide")

    def test_first_words_without_personal_data_and_retry_after(self):
        detail = self.detail(429, "Too Many Requests", "<html><body><h1>Trop de requêtes</h1> écrivez à "
                             "camille.exemple@example.test ou au 01 23 45 67 89</body></html>", retry_after="120")
        self.assertEqual(detail, "HTTP 429 Too Many Requests sur /recherche (page), réponse : « Trop de requêtes "
                                 "écrivez à [e-mail masqué] ou au [chiffres masqués] », Retry-After : 120")
        self.assertEqual(self.detail(502, "", RuntimeError("indisponible")), "HTTP 502 sur /recherche (page), réponse illisible")

    def test_window_closes_one_minute_after_a_campaign_ten_after_a_trial(self):
        self.assertEqual((robot.delai_fenetre("campagne"), robot.delai_fenetre("cinq")), (60, 60))
        self.assertEqual((robot.delai_fenetre("essai"), robot.delai_fenetre("calibrage")), (600, 600))

    def test_addresses_lose_their_parameters(self):
        self.assertEqual(robot.sans_donnees("net::ERR_HTTP_RESPONSE_CODE_FAILURE at https://labonneboite.francetravail.fr/"
                                            "recherche?rome=M1811&department_number=75&bbox="),
                         "net::ERR_HTTP_RESPONSE_CODE_FAILURE at https://labonneboite.francetravail.fr/recherche")


class SitesAutorisesTests(unittest.TestCase):
    """Liste des sites que le navigateur du robot peut joindre (vrai site, hors mode test)."""

    SITE = "labonneboite.francetravail.fr"

    def test_france_travail_and_the_exact_login_address_are_allowed(self):
        for hote in (self.SITE, "authentification-candidat.pole-emploi.fr", "authentification-candidat.francetravail.fr",
                     "francetravail.fr", "api.francetravail.io"):
            with self.subTest(hote=hote):
                self.assertTrue(robot.hote_autorise(hote, self.SITE))
        self.assertTrue(robot.est_connexion("authentification-candidat.pole-emploi.fr", "/connexion/oauth2/authorize"))

    def test_any_other_site_stays_blocked(self):
        for hote in ("cdn.tagcommander.com", "pole-emploi.fr", "www.pole-emploi.fr", "faux-authentification-candidat.pole-emploi.fr",
                     "evilfrancetravail.fr", "francetravail.fr.exemple.org", "example.org", ""):
            with self.subTest(hote=hote):
                self.assertFalse(robot.hote_autorise(hote, self.SITE))

    def test_name_resolution_is_limited_to_allowed_sites(self):
        regles = robot.regles_dns(self.SITE)
        self.assertTrue(regles.startswith("MAP * ~NOTFOUND, "))
        for hote in (self.SITE, "authentification-candidat.pole-emploi.fr", "*.francetravail.fr", "*.francetravail.io"):
            self.assertIn(f"EXCLUDE {hote}", regles)
        self.assertNotIn("tagcommander", regles)
        self.assertNotIn("EXCLUDE *.pole-emploi.fr", regles)

    def test_test_mode_allows_only_the_local_fake_site(self):
        self.assertTrue(robot.hote_autorise("127.0.0.1", "127.0.0.1", test=True))
        self.assertFalse(robot.hote_autorise("authentification-candidat.pole-emploi.fr", "127.0.0.1", test=True))


if __name__ == "__main__":
    unittest.main()
