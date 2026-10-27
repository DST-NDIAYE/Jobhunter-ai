"""Adaptation du CV par l'ordre : LLM simulé, offre et CV maître fictifs, aucun appel réseau."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.config import PROJECT_ROOT, Settings
from app import cv_tailor, llm
from app.cv_tailor import CVTailorError, OffreAnalyse, Selection

FIXTURE = Path(__file__).parent / "fixtures" / "cv_master_fictif.yaml"

# Offre inventée : entreprise, poste et contenu fictifs.
OFFRE_FICTIVE = """Exemplia Analytics (entreprise fictive) recrute un Data Scientist en CDD de 12 mois.
Missions : classification de documents, mise en production de modèles, veille R&D.
Compétences requises : Python, Scikit-learn, TensorFlow, Kubernetes, conteneurisation, environnement cloud.
Un plus : SQL, Git. Une connaissance du C++ est appréciée.
Profil : 2 ans d'expérience minimum."""

ALL_ITEMS = {"prj-fictif-classif": ["acc-classif-01", "acc-classif-02", "acc-classif-03", "acc-classif-04",
                                    "acc-classif-05"],
             "prj-fictif-rag": ["acc-rag-01", "acc-rag-02"],
             "prj-fictif-dashboard": ["acc-dash-01", "acc-dash-02", "acc-dash-03"]}


def analyse(**overrides):
    data = {
        "intitule": "Data Scientist", "entreprise": "Exemplia Analytics", "type_contrat": "CDD",
        "competences_requises": ["Python", "Scikit-learn", "TensorFlow", "Kubernetes", "conteneurisation",
                                 "environnement cloud"],
        "competences_souhaitees": ["SQL", "Git"], "mots_cles": ["classification", "production"],
        "experience_min_annees": 2, "niveau_experience": "junior", "langue": "fr",
    }
    data.update(overrides)
    return data


def selection(**overrides):
    data = {
        "accroche": "ml",
        "projets": [{"projet": "prj-fictif-rag", "accomplissements": ["acc-rag-02", "acc-rag-01"]},
                    {"projet": "prj-fictif-classif", "accomplissements": ["acc-classif-02", "acc-classif-03"]}],
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
            return cv_tailor.tailor_cv(offer, master_path=FIXTURE, settings=Settings(llm_api_key="cle-test"),
                                       cache_dir=None)


def project_items(result):
    return {p["id"]: [a["id"] for a in p["accomplissements"]] for e in result["experiences"] for p in e["projets"]}


class OrderTests(TailorTestCase):
    def test_everything_in_the_master_is_kept(self):
        result = self.run_tailor()
        self.assertEqual({k: sorted(v) for k, v in project_items(result).items()}, ALL_ITEMS)
        self.assertEqual(len(result["ordre_accomplissements"]), 10)
        names = [n for group in result["competences"] for n in group["noms"]]
        master = cv_tailor.load_master(FIXTURE)
        self.assertEqual(sorted(names), sorted(cv_tailor._skills(master)))

    def test_llm_order_is_applied_and_omissions_are_completed_in_master_order(self):
        result = self.run_tailor()
        items = project_items(result)
        # Ordre du LLM d'abord, puis ce qu'il a oublié, dans l'ordre du master.
        self.assertEqual(items["prj-fictif-classif"],
                         ["acc-classif-02", "acc-classif-03", "acc-classif-01", "acc-classif-04", "acc-classif-05"])
        self.assertEqual(items["prj-fictif-rag"], ["acc-rag-02", "acc-rag-01"])
        # Projet absent de la réponse du LLM : ajouté, en dernier dans la priorité.
        self.assertEqual(result["ordre_accomplissements"][:2], ["acc-rag-02", "acc-rag-01"])
        self.assertEqual(result["ordre_accomplissements"][-3:], ["acc-dash-01", "acc-dash-02", "acc-dash-03"])

    def test_experiences_stay_chronological_and_projects_follow_llm_rank(self):
        result = self.run_tailor()
        self.assertEqual([e["id"] for e in result["experiences"]], ["exp-fictive-ds", "exp-fictive-stage"])
        self.assertEqual([p["id"] for p in result["experiences"][0]["projets"]], ["prj-fictif-rag", "prj-fictif-classif"])

    def test_text_is_copied_from_master_with_bold_markup(self):
        result = self.run_tailor()
        texts = {a["id"]: a["texte"] for e in result["experiences"] for p in e["projets"] for a in p["accomplissements"]}
        self.assertEqual(texts["acc-classif-02"], "Entraînement de **modèles de classification**.")

    def test_unknown_accomplishment_id_is_rejected(self):
        chosen = selection(projets=[{"projet": "prj-fictif-classif", "accomplissements": ["acc-classif-02", "acc-invente-99"]}])
        with self.assertRaisesRegex(CVTailorError, "accomplissement inconnu « acc-invente-99 »"):
            self.run_tailor(chosen=chosen)

    def test_wrong_project_duplicates_and_unknown_ids_are_rejected(self):
        chosen = selection(accroche="invente", projets=[
            {"projet": "prj-fictif-classif", "accomplissements": ["acc-rag-01", "acc-classif-01", "acc-classif-01"]},
            {"projet": "prj-invente", "accomplissements": []}])
        with self.assertRaises(CVTailorError) as ctx:
            self.run_tailor(chosen=chosen)
        for expected in ("accroche inconnue", "n'appartient pas au projet", "accomplissement en double",
                         "projet inconnu « prj-invente »"):
            self.assertIn(expected, str(ctx.exception))

    def test_llm_returns_only_an_order(self):
        self.assertEqual(set(Selection.model_fields), {"accroche", "projets"})
        with self.assertRaises(ValueError):
            Selection.model_validate({**selection(), "texte_accroche": "Texte rédigé par le LLM"})

    def test_redundant_accomplishment_is_moved_last(self):
        chosen = selection(projets=[{"projet": "prj-fictif-dashboard", "accomplissements": ["acc-dash-03", "acc-dash-01"]}])
        result = self.run_tailor(chosen=chosen)
        self.assertEqual(project_items(result)["prj-fictif-dashboard"], ["acc-dash-01", "acc-dash-02", "acc-dash-03"])
        self.assertEqual(result["ordre_accomplissements"][-1], "acc-dash-03")


class SkillOrderTests(TailorTestCase):
    def test_skills_cited_by_offer_come_first_then_level(self):
        groups = {g["categorie"]: g["noms"] for g in self.run_tailor()["competences"]}
        # Offre : Python, SQL ; « R&D » ne cite pas R et « C++ » ne cite pas C.
        self.assertEqual(groups["langages"], ["Python", "SQL", "R", "C"])
        self.assertEqual(groups["data_science"], ["Scikit-learn", "TensorFlow", "Pandas"])
        self.assertEqual(groups["outils"], ["Git/GitHub"])
        # Rubrique « Notions (Master) » toujours présente, même si l'offre ne cite rien.
        self.assertEqual(groups["notions_master"], ["Docker", "AWS"])

    def test_level_only_orders_when_offer_cites_nothing(self):
        master = cv_tailor.load_master(FIXTURE)
        groups = {g["categorie"]: g["noms"] for g in cv_tailor.order_skills(master, "")}
        self.assertEqual(groups["ia_generative"], ["LangChain", "FAISS", "expressions régulières (Regex)"])


class ObjectiveTests(TailorTestCase):
    def test_objective_follows_contract(self):
        expected = {"CDI": "Je recherche un poste en CDI.", "CDD": "Je recherche un poste en CDD.",
                    "these_cifre": "Je recherche un contrat de thèse CIFRE.", "stage": "Je recherche un poste.",
                    "alternance": "Je recherche un poste.", "inconnu": "Je recherche un poste."}
        for contract, sentence in expected.items():
            with self.subTest(contract=contract):
                result = self.run_tailor(analysis=analyse(type_contrat=contract))
                self.assertEqual(result["accroche"]["texte"], "Data Scientist junior orientée Machine Learning. " + sentence)

    def test_accroche_ending_with_comma_continues_the_sentence(self):
        result = self.run_tailor(chosen=selection(accroche="ats"), analysis=analyse(type_contrat="CDI"))
        self.assertEqual(result["accroche"]["texte"], "Data Scientist junior orientée données, je recherche un poste en CDI.")


class ReportTests(TailorTestCase):
    def test_keywords_present_and_absent_with_synonyms_and_notions(self):
        report = self.run_tailor()["rapport"]["mots_cles"]
        present = {p["mot_cle"]: (p["via"], p["notion"]) for p in report["presents"]}
        self.assertEqual(present["Python"], ("Python", False))
        self.assertEqual(present["conteneurisation"], ("Docker", True))
        self.assertEqual(present["environnement cloud"], ("AWS", True))
        # Git et TensorFlow sont affichés dans les rubriques principales : pas « via notions ».
        self.assertEqual(present["Git"], ("Git", False))
        self.assertEqual(present["TensorFlow"], ("TensorFlow", False))
        self.assertEqual(report["absents"], ["Kubernetes"])

    def test_keyword_coverage_reads_the_given_text_only(self):
        coverage = cv_tailor.keyword_coverage(OffreAnalyse.model_validate(analyse()), "Python et Docker")
        self.assertEqual([p["mot_cle"] for p in coverage["presents"]], ["Python", "conteneurisation"])

    def test_notion_flag_depends_only_on_where_the_term_is_displayed(self):
        offer = OffreAnalyse.model_validate(analyse(competences_requises=["Docker", "SQL"], competences_souhaitees=[],
                                                    mots_cles=[]))
        coverage = cv_tailor.keyword_coverage(offer, "Langages : Python, SQL", "Docker, AWS")
        self.assertEqual([(p["mot_cle"], p["notion"]) for p in coverage["presents"]], [("Docker", True), ("SQL", False)])
        # Le même terme cité aussi ailleurs (ex. dans une puce) n'est plus « via notions ».
        coverage = cv_tailor.keyword_coverage(offer, "Déploiement avec Docker. Langages : SQL", "Docker, AWS")
        self.assertEqual([p["notion"] for p in coverage["presents"]], [False, False])

    def test_communication_synonyms(self):
        offer = OffreAnalyse.model_validate(analyse(competences_requises=["Communication"], competences_souhaitees=[],
                                                    mots_cles=[]))
        for text, via in (("Animation d’ateliers de formation", "animation d’ateliers"),
                          ("Accompagnement et acculturation des équipes métier", "acculturation"),
                          ("Qualités : esprit d'équipe", "esprit d’équipe")):
            with self.subTest(text=text):
                coverage = cv_tailor.keyword_coverage(offer, text)
                self.assertEqual(coverage["presents"][0]["via"], via)
        self.assertEqual(cv_tailor.keyword_coverage(offer, "Python")["absents"], ["Communication"])

    def test_experience_requested_is_reported_from_text(self):
        offer = OFFRE_FICTIVE.replace("2 ans d'expérience minimum", "5 ans d'expérience minimum, profil senior")
        infos = self.run_tailor(analysis=analyse(experience_min_annees=None), offer=offer)["rapport"]["informations"]
        self.assertIn("Expérience mentionnée dans l'offre : au moins 5 ans.", infos)

    def test_range_in_offer_reports_lower_bound(self):
        offer = OFFRE_FICTIVE.replace("2 ans d'expérience minimum", "3 à 5 ans d'expérience")
        infos = self.run_tailor(analysis=analyse(experience_min_annees=None), offer=offer)["rapport"]["informations"]
        self.assertIn("Expérience mentionnée dans l'offre : au moins 3 ans.", infos)

    def test_contract_and_language_are_reported_never_blocking(self):
        offer = OFFRE_FICTIVE + "\nKubernetes, Spark et Scala exigés. 8 ans d'expérience."
        result = self.run_tailor(offer=offer, analysis=analyse(type_contrat="alternance", langue="en",
                                                              niveau_experience="senior", experience_min_annees=8))
        infos = result["rapport"]["informations"]
        self.assertTrue(any("Contrat de l'offre : alternance" in a for a in infos))
        self.assertTrue(any("analyse « alternance », texte de l'offre « CDD »" in a for a in infos))
        self.assertTrue(any("non rédigée en français" in a for a in infos))
        self.assertEqual(len(result["ordre_accomplissements"]), 10)
        for info in infos:
            for word in ("déconseill", "refus", "bloqu", "en dessous", "hors cible"):
                self.assertNotIn(word, info.lower())

    def test_years_detection(self):
        cases = {"3+ years of experience in ML": 3, "Expérience de 4 ans minimum": 4,
                 "5 ans d'expérience minimum": 5, "Débutant accepté": None, "Expérience : < 6 mois": None,
                 "Vous justifiez de 3 à 5 ans d’expérience en data science": 3, "3-5 years of experience": 3,
                 "3 – 5 ans d'expérience": 3, "entre 2 et 4 ans d'expérience": 2, "Bac+2 5 ans d'expérience": 5}
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(cv_tailor.years_required_in_text(text), expected)


class PrivacyTests(TailorTestCase):
    def test_no_personal_data_and_no_markup_sent_to_llm(self):
        self.run_tailor()
        sent = json.dumps(self.provider.calls, ensure_ascii=False)
        for value in ("camille.exemple@example.com", "06 00 11 22 33", "0600112233", "Villefictive",
                      "Camille", "EXEMPLE", "Société Fictive", "Laboratoire Imaginaire", "linkedin", "github.com", "**"):
            self.assertNotIn(value, sent)
        self.assertEqual(len(self.provider.calls), 2)
        self.assertNotIn("acc-classif-01", json.dumps(self.provider.calls[0], ensure_ascii=False))

    def test_selection_prompt_asks_for_an_order_using_required_and_desired_skills(self):
        self.run_tailor(analysis=analyse(competences_souhaitees=["LangChain", "IA générative"]))
        system, user = self.provider.calls[1]
        for expected in ("tu ne", "TOUS les projets", "competences_requises ET competences_souhaitees", "redondant_avec"):
            self.assertIn(expected, system["content"])
        payload = json.loads(user["content"])
        self.assertEqual(payload["analyse_offre"]["competences_souhaitees"], ["LangChain", "IA générative"])
        items = {a["id"]: a for p in payload["catalogue"]["projets"] for a in p["accomplissements"]}
        self.assertEqual(items["acc-dash-03"]["redondant_avec"], "certifications")

    def test_personal_data_in_catalogue_blocks_the_request(self):
        master = cv_tailor.load_master(FIXTURE)
        master["accroches"]["ml"] = "Contact : camille.exemple@example.com"
        with self.assertRaisesRegex(CVTailorError, "Donnée personnelle détectée"):
            cv_tailor.selection_messages(OffreAnalyse.model_validate(analyse()), master)


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.folder = self.enterContext(tempfile.TemporaryDirectory())
        self.provider = FakeProvider({"OffreAnalyse": analyse(), "Selection": selection()})

    def tailor(self, master_path=FIXTURE, settings=None):
        with patch.dict(llm.PROVIDERS, {"fake": self.provider}), \
                patch("app.cv_tailor.resolve_provider", return_value=("fake", "modele-test")):
            return cv_tailor.tailor_cv(OFFRE_FICTIVE, master_path=master_path, cache_dir=self.folder,
                                       settings=settings or Settings(llm_api_key="cle-test"))

    def test_same_offer_never_calls_the_llm_twice(self):
        first = self.tailor()
        self.assertEqual(len(self.provider.calls), 2)
        second = self.tailor()
        self.assertEqual(len(self.provider.calls), 2)  # analyse et sélection lues dans le cache
        self.assertEqual(first["ordre_accomplissements"], second["ordre_accomplissements"])
        key = hashlib.sha256(OFFRE_FICTIVE.strip().encode()).hexdigest()
        self.assertTrue((Path(self.folder) / "analyses" / f"{key}.json").is_file())

    def test_cache_works_without_any_llm(self):
        self.tailor()
        with patch("app.cv_tailor.resolve_provider", return_value=(None, "")):
            result = cv_tailor.tailor_cv(OFFRE_FICTIVE, master_path=FIXTURE, cache_dir=self.folder,
                                         settings=Settings(llm_provider="local"))
        self.assertEqual(result["analyse"]["entreprise"], "Exemplia Analytics")

    def test_master_change_recomputes_the_selection_only(self):
        self.tailor()
        changed = Path(self.folder) / "master.yaml"
        changed.write_text(FIXTURE.read_text().replace("Nettoyage des données de tickets.",
                                                        "Nettoyage soigné des données de tickets."))
        self.tailor(master_path=changed)
        self.assertEqual([c[0]["content"][:30] for c in self.provider.calls[2:]],
                         [cv_tailor.SELECTION_PROMPT[:30]])

    def test_unreadable_cache_is_recomputed(self):
        self.tailor()
        for file in Path(self.folder).rglob("*.json"):
            file.write_text("{pas du json")
        self.tailor()
        self.assertEqual(len(self.provider.calls), 4)


class InputTests(unittest.TestCase):
    def test_no_llm_configured_is_a_clear_error(self):
        with self.assertRaisesRegex(CVTailorError, "Aucun LLM configuré"):
            cv_tailor.tailor_cv(OFFRE_FICTIVE, master_path=FIXTURE, settings=Settings(llm_provider="local"),
                                cache_dir=None)

    def test_empty_offer_and_broken_master_are_rejected(self):
        with self.assertRaisesRegex(CVTailorError, "vide"):
            cv_tailor.tailor_cv("  ", master_path=FIXTURE, settings=Settings(), cache_dir=None)
        master = cv_tailor.load_master(FIXTURE)
        master["competences"]["langages"][0]["preuves"] = ["prj-inexistant"]
        with self.assertRaisesRegex(CVTailorError, "Preuves inconnues"):
            cv_tailor.check_master(master)

    def test_unclosed_bold_markup_and_bad_redundancy_target_are_rejected(self):
        master = cv_tailor.load_master(FIXTURE)
        master["experiences"][0]["projets"][0]["accomplissements"][0]["texte"] = "Texte **non fermé."
        with self.assertRaisesRegex(CVTailorError, "gras non fermé"):
            cv_tailor.check_master(master)
        master = cv_tailor.load_master(FIXTURE)
        master["experiences"][1]["projets"][0]["accomplissements"][2]["redondant_avec"] = "inexistante"
        with self.assertRaisesRegex(CVTailorError, "rubrique absente"):
            cv_tailor.check_master(master)

    @unittest.skipUnless((PROJECT_ROOT / "cv_master.yaml").is_file(), "CV maître personnel absent (ignoré par git)")
    def test_real_master_is_consistent(self):
        master = cv_tailor.load_master(PROJECT_ROOT / "cv_master.yaml")
        payload = json.dumps(cv_tailor.selection_payload(master), ensure_ascii=False)
        cv_tailor._check_no_personal_data(payload, master)
        self.assertNotIn("**", payload)
        self.assertIn("notions_master", master["competences"])


if __name__ == "__main__":
    unittest.main()
