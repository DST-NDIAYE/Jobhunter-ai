"""Robot assisté contre le faux site La Bonne Boîte : navigateur sans fenêtre, mode test, aucun accès au vrai site.

Chaque test lance le vrai processus du robot (comme le serveur), avec une base, un lot et des dossiers temporaires.
"""

from contextlib import closing
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx

from app import robot_spontanees as robot
from app import spontanees as sp
from app.config import Settings
from app.database import init_db
from tests.faux_site_lbb import demarrer


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
        self.enterContext(patch.dict(os.environ, {
            "JOBHUNTER_FAKE_FT": "1", "JOBHUNTER_LBB_SITE": self.site, "JOBHUNTER_ROBOT_INVISIBLE": "1",
            "HTTPS_PROXY": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9", "NO_PROXY": "127.0.0.1,localhost"}))
        self.enterContext(patch.object(sp, "MODELE_PATH", self.folder / "modele.txt"))
        self.enterContext(patch.object(robot, "ROBOT_DIR", self.folder / "robot"))
        self.faux("/__test__/reinitialiser")
        self.lot = sp.constituer_lot(self.settings)["lot"]
        self.sirets = [e["siret"] for e in self.lot]

    def faux(self, path, **data):
        return httpx.post(self.site + path, json=data or None, timeout=10).json()

    def candidatures(self):
        return httpx.get(self.site + "/__test__/etat", timeout=10).json()["candidatures"]

    def ligne(self, siret):
        with closing(sqlite3.connect(self.db)) as db:
            db.row_factory = sqlite3.Row
            return dict(db.execute("SELECT * FROM spontanees WHERE siret = ?", (siret,)).fetchone())

    def lancer(self, mode, connecte=True, pendant=None):
        if connecte:
            self.faux("/__test__/connexion")
        robot.lancer_robot(self.settings, mode)
        fin = time.monotonic() + 90
        while time.monotonic() < fin:
            etat = robot.etat_robot()
            if pendant:
                pendant(etat)
            if not etat["actif"]:
                return etat
            time.sleep(0.3)
        robot.arreter_robot()
        self.fail("Le robot ne s'est pas arrêté à temps.")

    def test_trial_waits_for_your_login_fills_one_form_and_never_sends(self):
        vu_connexion = []

        def pendant(etat):
            if etat.get("attente") == "connexion" and not vu_connexion:
                time.sleep(1.5)  # le robot attend : il ne se connecte jamais lui-même
                vu_connexion.append(httpx.get(self.site + "/__test__/connecte").json()["connecte"])
                self.faux("/__test__/connexion")
        etat = self.lancer("essai", connecte=False, pendant=pendant)
        self.assertEqual(vu_connexion, [False])
        self.assertEqual(etat["statut"], "termine", etat["journal"])
        self.assertIn("Rien n'a été envoyé", etat["resume"])
        self.assertEqual([c["nom"] for c in etat["captures"]],
                         ["01-exemplia-analytics-formulaire.png", "02-exemplia-analytics-cv.png",
                          "03-exemplia-analytics-message.png", "04-exemplia-analytics-pret.png"])
        journal = "\n".join(etat["journal"])
        for attendu in ("Connexion détectée", "CV « CV_Camille_EXEMPLE (document PDF) » coché", "message collé",
                        "le robot s'arrête avant l'envoi"):
            self.assertIn(attendu, journal)
        self.assertEqual(self.candidatures(), [])  # ni envoi ni brouillon sur le site
        ligne = self.ligne(self.sirets[0])
        self.assertEqual((ligne["statut"], ligne["envoyee_le"]), ("À envoyer", None))
        self.assertTrue(ligne["robot_note"].startswith("Essai du "))
        run = robot.ROBOT_DIR / etat["id"]
        fichiers = {p.name for p in run.iterdir()}
        self.assertEqual(fichiers - {c["nom"] for c in etat["captures"]},
                         {"travail.json", "etat.json", "journal.txt", "pid", "robot.log"})  # aucune session enregistrée
        captures = robot.chemin_capture(etat["id"], etat["captures"][0]["nom"])
        self.assertEqual(captures.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")

    def test_assisted_skip_your_own_send_then_captcha_stops_immediately(self):
        self.faux("/__test__/scenario", siret=self.sirets[2], type="captcha")
        actions, vues = ["passer", "clic"], set()

        def pendant(etat):
            if etat.get("attente") == "decision" and etat.get("entreprise") not in vues and actions:
                vues.add(etat["entreprise"])
                if actions.pop(0) == "passer":
                    robot.decider("passer")
                else:  # vous cliquez « Continuer » puis « Envoyer la candidature » dans la fenêtre du robot
                    self.faux("/__test__/clic", action="envoyer")
        etat = self.lancer("assiste", pendant=pendant)
        self.assertEqual(etat["statut"], "arrete", etat["journal"])
        self.assertIn("Captcha", etat["resume"])
        self.assertTrue(etat["captures"][-1]["nom"].endswith("-captcha.png"))
        self.assertEqual((etat["compteurs"]["passees"], etat["compteurs"]["envoyees"]), (1, 1))
        envoyees = self.candidatures()
        self.assertEqual([(c["siret"], c["etat"]) for c in envoyees], [(self.sirets[1], "sent")])
        self.assertEqual(envoyees[0]["documents"], ["CV_Camille_EXEMPLE (document PDF)"])
        self.assertEqual(envoyees[0]["message"], sp.message_pour(sp.charger_modele(), self.lot[1])["texte"])
        passee, envoyee, captcha = (self.ligne(s) for s in self.sirets[:3])
        self.assertEqual((passee["statut"], passee["robot_note"]), ("À envoyer", "Passée lors du robot assisté."))
        self.assertEqual((envoyee["statut"], envoyee["envoyee_par"]), ("Candidature envoyée", "vous"))
        self.assertEqual(captcha["statut"], "À envoyer")

    def test_unexpected_page_stops_with_a_capture(self):
        for siret in (self.sirets[0], self.sirets[1]):  # dès la connexion, puis en cours de lot
            with self.subTest(position=siret):
                self.faux("/__test__/reinitialiser")
                self.faux("/__test__/scenario", siret=siret, type="inattendue")
                vues = set()

                def pendant(etat):
                    if etat.get("attente") == "decision" and etat.get("entreprise") not in vues:
                        vues.add(etat["entreprise"])
                        robot.decider("passer")
                etat = self.lancer("assiste", pendant=pendant)
                self.assertEqual(etat["statut"], "arrete", etat["journal"])
                self.assertIn("Page inattendue", etat["resume"])
                self.assertIn("rien n'a été envoyé", etat["resume"])
                self.assertTrue(etat["captures"][-1]["nom"].endswith("page-inattendue.png"))
                self.assertEqual(self.candidatures(), [])

    def test_several_documents_need_the_cv_name_setting(self):
        self.faux("/__test__/documents", noms=["CV_Camille_EXEMPLE.pdf", "Lettre_type.docx", "CV_Camille_EXEMPLE_Anglais.pdf"])
        etat = self.lancer("essai")
        self.assertEqual(etat["statut"], "arrete")
        self.assertIn("Plusieurs documents", etat["resume"])
        self.assertIn("Lettre_type (document DOCX)", etat["resume"])
        sp.ecrire_reglages(self.db, {"nom_cv": "anglais"})
        etat = self.lancer("essai")
        self.assertEqual(etat["statut"], "termine", etat["journal"])
        self.assertIn("CV « CV_Camille_EXEMPLE_Anglais (document PDF) » coché", "\n".join(etat["journal"]))
        self.assertEqual(self.candidatures(), [])

    def test_automatic_sending_checks_the_summary_and_respects_the_daily_cap(self):
        sp.ecrire_reglages(self.db, {"envoi_automatique": True, "plafond_par_jour": 2})
        self.faux("/__test__/scenario", siret=self.sirets[1], type="deja_envoyee")
        etat = self.lancer("assiste")
        self.assertEqual(etat["statut"], "termine", etat["journal"])
        self.assertIn("Plafond du jour atteint (2 envois)", etat["resume"])
        envoyees = [c["siret"] for c in self.candidatures() if c["etat"] == "sent" and c["message"] != "Ancien message"]
        self.assertEqual(envoyees, [self.sirets[0], self.sirets[2]])
        self.assertEqual([self.ligne(s)["envoyee_par"] for s in envoyees], ["robot", "robot"])
        self.assertEqual(self.ligne(self.sirets[1])["statut"], sp.DEJA_CONTACTEE)
        self.assertEqual(self.ligne(self.sirets[3])["statut"], "À envoyer")
        self.assertIn("synthèse vérifiée", "\n".join(etat["journal"]))

    def test_stop_request_and_existing_draft(self):
        self.faux("/__test__/scenario", siret=self.sirets[0], type="brouillon")

        def pendant(etat):
            if etat.get("attente") == "decision":
                robot.arreter_robot()
        etat = self.lancer("assiste", pendant=pendant)
        self.assertEqual(etat["statut"], "arrete")
        self.assertIn("Arrêt demandé", etat["resume"])
        self.assertIn("brouillon existe déjà", self.ligne(self.sirets[0])["robot_note"])
        self.assertEqual([c for c in self.candidatures() if c["message"] != "Ancien message"], [])


if __name__ == "__main__":
    unittest.main()
