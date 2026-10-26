"""Sélection de CV : LLM simulé, offre et CV maître fictifs, aucun appel réseau."""

import json
from pathlib import Path
import unittest
from unittest.mock import patch

from app.config import PROJECT_ROOT, Settings
from app import cv_tailor, llm
from app.cv_tailor import CVTailorError, OffreAnalyse, Selection

FIXTURE = Path(__file__).parent / "fixtures" / "cv_master_fictif.yaml"

# Offre inventée : entreprise, poste et contenu fictifs.
OFFRE_FICTIVE = """Exemplia Analytics (entreprise fictive) recrute un Data Scientist en CDD de 12 mois.
Missions : classification de documents, mise en production de modèles, veille R&D.
Compétences requises : Python, Scikit-learn, TensorFlow, Kubernetes.
Un plus : SQL, Git. Une connaissance du C++ est appréciée.
Profil : 2 ans d'expérience minimum."""


def analyse(**overrides):
    data = {
        "intitule": "Data Scientist", "entreprise": "Exemplia Analytics", "type_contrat": "CDD",
        "competences_requises": ["Python", "Scikit-learn", "TensorFlow", "Kubernetes"],
        "competences_souhaitees": ["SQL", "Git"], "mots_cles": ["classification", "production"],
        "experience_min_annees": 2, "niveau_experience": "junior", "langue": "fr",
    }
    data.update(overrides)
    return data


def selection(**overrides):
    data = {
        "accroche": "ml",
        "projets": [{"projet": "prj-fictif-classif", "accomplissements": ["acc-classif-02", "acc-classif-03"]},
                    {"projet": "prj-fictif-dashboard", "accomplissements": ["acc-dash-01"]}],
        "competences": ["Python", "Scikit-learn", "Pandas"],
        "competences_notions": ["TensorFlow", "SQL"],
    }
    data.update(overrides)
    return data


class FakeProvider:
    """Renvoie les réponses prévues par schéma et garde les messages reçus."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def generate(self, *, messages, model, api_key, timeout, schema):
        self.calls.append(messages)
        return schema.model_validate(self.responses[schema.__name__])


class TailorTestCase(unittest.TestCase):
    def run_tailor(self, analysis=None, chosen=None, offer=OFFRE_FICTIVE):
        self.provider = FakeProvider({"OffreAnalyse": analysis or analyse(), "Selection": chosen or selection()})
        with patch.dict(llm.PROVIDERS, {"fake": self.provider}), \
                patch("app.cv_tailor.resolve_provider", return_value=("fake", "modele-test")):
            return cv_tailor.tailor_cv(offer, master_path=FIXTURE, settings=Settings(llm_api_key="cle-test"))


class SelectionTests(TailorTestCase):
    def test_valid_selection_copies_master_text_and_adapts_objective(self):
        result = self.run_tailor()
        self.assertEqual(result["accroche"]["texte"],
                         "Data Scientist junior orientée Machine Learning. Je recherche un poste en CDD.")
        first = result["experiences"][0]["projets"][0]
        self.assertEqual([a["id"] for a in first["accomplissements"]], ["acc-classif-02", "acc-classif-03"])
        self.assertEqual(first["accomplissements"][0]["texte"], "Entraînement de modèles de classification.")
        self.assertEqual([c["nom"] for c in result["competences"]], ["Python", "Scikit-learn", "Pandas"])
        self.assertEqual([c["nom"] for c in result["competences_notions"]], ["TensorFlow", "SQL"])
        # Une expérience sans projet retenu reste dans le parcours.
        self.assertEqual(len(result["experiences"]), 2)
        # Priorité globale du LLM, indépendante de l'ordre chronologique des expériences.
        self.assertEqual(result["ordre_accomplissements"], ["acc-classif-02", "acc-classif-03", "acc-dash-01"])

    def test_unknown_accomplishment_id_is_rejected(self):
        chosen = selection(projets=[{"projet": "prj-fictif-classif",
                                     "accomplissements": ["acc-classif-02", "acc-invente-99"]}])
        with self.assertRaisesRegex(CVTailorError, "accomplissement inconnu « acc-invente-99 »"):
            self.run_tailor(chosen=chosen)

    def test_accomplishment_from_another_project_is_rejected(self):
        chosen = selection(projets=[{"projet": "prj-fictif-classif", "accomplissements": ["acc-rag-01"]}])
        with self.assertRaisesRegex(CVTailorError, "n'appartient pas au projet"):
            self.run_tailor(chosen=chosen)

    def test_unknown_project_accroche_and_skill_are_rejected(self):
        chosen = selection(accroche="invente", competences=["Python", "Kubernetes"],
                           projets=[{"projet": "prj-invente", "accomplissements": ["acc-classif-01"]}])
        with self.assertRaises(CVTailorError) as ctx:
            self.run_tailor(chosen=chosen)
        message = str(ctx.exception)
        for expected in ("accroche inconnue", "projet inconnu « prj-invente »", "compétence inconnue « Kubernetes »"):
            self.assertIn(expected, message)

    def test_notions_skill_in_main_list_is_rejected(self):
        with self.assertRaisesRegex(CVTailorError, "« TensorFlow » est de niveau notions"):
            self.run_tailor(chosen=selection(competences=["Python", "TensorFlow"], competences_notions=[]))

    def test_notions_skill_not_requested_by_offer_is_rejected(self):
        with self.assertRaisesRegex(CVTailorError, "« R » \\(notions\\) n'est pas demandée"):
            # L'offre parle de « R&D », pas du langage R.
            self.run_tailor(chosen=selection(competences_notions=["R"]))
        with self.assertRaisesRegex(CVTailorError, "« C » \\(notions\\) n'est pas demandée"):
            # « C++ » n'est pas le langage C.
            self.run_tailor(chosen=selection(competences_notions=["C"]))

    def test_notions_variants_are_recognised_in_offer(self):
        result = self.run_tailor(chosen=selection(competences_notions=["Git/GitHub"]))
        self.assertEqual([c["nom"] for c in result["competences_notions"]], ["Git/GitHub"])

    def test_one_page_budget_and_duplicates_are_enforced(self):
        too_many = selection(projets=[{"projet": "prj-fictif-classif",
                                       "accomplissements": ["acc-classif-01", "acc-classif-01"]}])
        with self.assertRaisesRegex(CVTailorError, "accomplissement en double"):
            self.run_tailor(chosen=too_many)
        with patch("app.cv_tailor.MAX_ACCOMPLISSEMENTS", 2):
            with self.assertRaisesRegex(CVTailorError, "plus de 2 accomplissements"):
                self.run_tailor(chosen=selection())

    def test_selection_priorities_use_required_and_desired_skills(self):
        self.run_tailor(analysis=analyse(competences_souhaitees=["LangChain", "IA générative"]))
        system, user = self.provider.calls[1]
        for expected in ("competences_requises ET competences_souhaitees",
                         "Un projet qui prouve une compétence souhaitée doit figurer dans la sélection"):
            self.assertIn(expected, system["content"])
        sent = json.loads(user["content"])["analyse_offre"]
        self.assertEqual(sent["competences_souhaitees"], ["LangChain", "IA générative"])

    def test_skills_cited_by_offer_are_computed_from_text(self):
        result = self.run_tailor()
        # « R&D » et « C++ » ne citent ni R ni C ; « Git » cite Git/GitHub.
        self.assertEqual(sorted(result["competences_citees_par_offre"]),
                         ["Git/GitHub", "Python", "SQL", "Scikit-learn", "TensorFlow"])

    def test_one_page_limits(self):
        self.assertEqual((cv_tailor.MAX_ACCOMPLISSEMENTS, cv_tailor.MAX_PAR_PROJET), (12, 5))

    def test_llm_must_not_write_text(self):
        # Un champ libre en plus est refusé par le schéma strict.
        with self.assertRaises(ValueError):
            Selection.model_validate({**selection(), "texte_accroche": "Texte rédigé par le LLM"})


class ObjectiveTests(TailorTestCase):
    def test_objective_follows_contract(self):
        expected = {"CDI": "Je recherche un poste en CDI.", "CDD": "Je recherche un poste en CDD.",
                    "these_cifre": "Je recherche un contrat de thèse CIFRE.", "stage": "Je recherche un poste.",
                    "alternance": "Je recherche un poste.", "inconnu": "Je recherche un poste."}
        for contract, sentence in expected.items():
            with self.subTest(contract=contract):
                result = self.run_tailor(analysis=analyse(type_contrat=contract))
                self.assertTrue(result["accroche"]["texte"].endswith(sentence))
                self.assertTrue(result["accroche"]["texte"].startswith("Data Scientist junior orientée Machine"))


class ReportTests(TailorTestCase):
    def test_strengths_gaps_and_notions(self):
        report = self.run_tailor()["rapport"]
        self.assertEqual([p["demande"] for p in report["points_forts"]], ["Python", "Scikit-learn"])
        self.assertIn("prj-fictif-classif", report["points_forts"][1]["preuves"])
        self.assertEqual(report["manques"], ["Kubernetes"])
        self.assertEqual([n["competence"] for n in report["competences_a_niveau_notions"]],
                         ["TensorFlow", "SQL", "Git/GitHub"])
        self.assertEqual(report["informations"], [])

    def test_experience_requested_is_reported_from_text(self):
        offer = OFFRE_FICTIVE.replace("2 ans d'expérience minimum", "5 ans d'expérience minimum, profil senior")
        # Même si l'analyse du LLM sous-estime l'expérience, le texte de l'offre est vérifié en code.
        report = self.run_tailor(analysis=analyse(experience_min_annees=None), offer=offer)["rapport"]
        self.assertIn("Expérience mentionnée dans l'offre : au moins 5 ans.", report["informations"])

    def test_contract_and_language_are_reported(self):
        report = self.run_tailor(analysis=analyse(type_contrat="alternance", langue="en"))["rapport"]
        infos = report["informations"]
        self.assertTrue(any("Contrat de l'offre : alternance" in a for a in infos))
        self.assertTrue(any("analyse « alternance », texte de l'offre « CDD »" in a for a in infos))
        self.assertTrue(any("non rédigée en français" in a for a in infos))

    def test_gaps_senior_level_and_other_contract_never_block(self):
        offer = OFFRE_FICTIVE + "\nKubernetes, Spark et Scala exigés. 8 ans d'expérience. Stage de 6 mois."
        result = self.run_tailor(offer=offer, analysis=analyse(
            type_contrat="stage", niveau_experience="senior", experience_min_annees=8,
            competences_requises=["Kubernetes", "Spark", "Scala"]))
        # La sélection complète est produite ; les écarts sont de simples informations.
        self.assertEqual(len(result["experiences"][0]["projets"][0]["accomplissements"]), 2)
        self.assertEqual(result["rapport"]["manques"], ["Kubernetes", "Spark", "Scala"])
        self.assertEqual(len(result["rapport"]["informations"]), 3)
        for info in result["rapport"]["informations"]:
            for word in ("déconseill", "refus", "bloqu", "en dessous", "hors cible"):
                self.assertNotIn(word, info.lower())

    def test_years_detection(self):
        cases = {"3+ years of experience in ML": 3, "Expérience de 4 ans minimum": 4,
                 "5 ans d'expérience minimum": 5, "Débutant accepté": None, "Expérience : < 6 mois": None,
                 # Fourchettes : la borne basse.
                 "Vous justifiez de 3 à 5 ans d’expérience en data science": 3, "3-5 years of experience": 3,
                 "3 – 5 ans d'expérience": 3, "entre 2 et 4 ans d'expérience": 2,
                 # « Bac+2 » n'est pas le début d'une fourchette.
                 "Bac+2 5 ans d'expérience": 5}
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(cv_tailor.years_required_in_text(text), expected)

    def test_range_in_offer_reports_lower_bound(self):
        offer = OFFRE_FICTIVE.replace("2 ans d'expérience minimum", "3 à 5 ans d'expérience")
        report = self.run_tailor(analysis=analyse(experience_min_annees=None), offer=offer)["rapport"]
        self.assertIn("Expérience mentionnée dans l'offre : au moins 3 ans.", report["informations"])

    def test_project_proof_found_in_titles_texts_and_tags(self):
        report = self.run_tailor(analysis=analyse(
            competences_requises=["MLOps", "IA générative", "recherche documentaire", "Kubernetes"],
            competences_souhaitees=[]))["rapport"]
        strengths = {p["demande"]: p["preuves"] for p in report["points_forts"]}
        # MLOps : texte d'un accomplissement ; IA générative : tag ; recherche documentaire : titre.
        self.assertEqual(strengths, {"MLOps": ["prj-fictif-classif"], "IA générative": ["prj-fictif-rag"],
                                     "recherche documentaire": ["prj-fictif-rag"]})
        self.assertEqual(report["manques"], ["Kubernetes"])


class PrivacyTests(TailorTestCase):
    def test_no_personal_data_sent_to_llm(self):
        self.run_tailor()
        sent = json.dumps(self.provider.calls, ensure_ascii=False)
        for value in ("camille.exemple@example.com", "06 00 11 22 33", "0600112233", "Villefictive",
                      "Camille", "EXEMPLE", "Société Fictive", "Laboratoire Imaginaire", "linkedin", "github.com"):
            self.assertNotIn(value, sent)
        self.assertEqual(len(self.provider.calls), 2)
        # L'analyse de l'offre ne reçoit aucune donnée du CV.
        self.assertNotIn("acc-classif-01", json.dumps(self.provider.calls[0], ensure_ascii=False))

    def test_personal_data_in_catalogue_blocks_the_request(self):
        master = cv_tailor.load_master(FIXTURE)
        master["accroches"]["ml"] = "Contact : camille.exemple@example.com"
        with self.assertRaisesRegex(CVTailorError, "Donnée personnelle détectée"):
            cv_tailor.selection_messages(OffreAnalyse.model_validate(analyse()), master)


class InputTests(unittest.TestCase):
    def test_no_llm_configured_is_a_clear_error(self):
        with self.assertRaisesRegex(CVTailorError, "Aucun LLM configuré"):
            cv_tailor.tailor_cv(OFFRE_FICTIVE, master_path=FIXTURE, settings=Settings(llm_provider="local"))

    def test_empty_offer_and_broken_master_are_rejected(self):
        with self.assertRaisesRegex(CVTailorError, "vide"):
            cv_tailor.tailor_cv("  ", master_path=FIXTURE, settings=Settings())
        master = cv_tailor.load_master(FIXTURE)
        master["competences"]["langages"][0]["preuves"] = ["prj-inexistant"]
        with self.assertRaisesRegex(CVTailorError, "Preuves inconnues"):
            cv_tailor.check_master(master)

    @unittest.skipUnless((PROJECT_ROOT / "cv_master.yaml").is_file(), "CV maître personnel absent (ignoré par git)")
    def test_real_master_is_consistent(self):
        master = cv_tailor.load_master(PROJECT_ROOT / "cv_master.yaml")
        payload = json.dumps(cv_tailor.selection_payload(master), ensure_ascii=False)
        cv_tailor._check_no_personal_data(payload, master)


if __name__ == "__main__":
    unittest.main()
