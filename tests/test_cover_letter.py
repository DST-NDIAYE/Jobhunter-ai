"""Lettre de motivation : LLM simulé, offre et CV maître fictifs, aucun appel réseau."""

import copy
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pymupdf

from app import cover_letter, cv_pdf, cv_tailor, llm
from app.config import Settings
from app.cover_letter import POLITESSE, Lettre, LetterError
from app.cv_tailor import OffreAnalyse, Selection

FIXTURE = Path(__file__).parent / "fixtures" / "cv_master_fictif.yaml"
OFFRE = ("Exemplia Analytics (entreprise fictive) recrute un Data Scientist en CDI pour industrialiser ses modèles. "
         "Compétences : Python, Scikit-learn, Kubernetes, conteneurisation. Un plus : LangChain.")


def make_cv(**analysis):
    master = cv_tailor.load_master(FIXTURE)
    analyse = OffreAnalyse.model_validate({
        "intitule": "Data Scientist (H/F)", "entreprise": "Exemplia Analytics", "type_contrat": "CDI",
        "competences_requises": ["Python", "Scikit-learn", "Kubernetes", "conteneurisation"],
        "competences_souhaitees": ["LangChain"], "mots_cles": [], "experience_min_annees": None,
        "niveau_experience": "junior", "langue": "fr", **analysis})
    selection = Selection.model_validate({"accroche": "ml", "projets": []})
    return {"analyse": analyse.model_dump(), **cv_tailor.assemble(selection, analyse, master, OFFRE)}, master


def ph(texte, *preuves):
    return {"texte": texte, "preuves": list(preuves)}


# Lettre fictive valide (~255 mots politesse comprise), construite uniquement sur le CV maître fictif.
LETTER = {
    "accroche": {"phrases": [
        ph("Exemplia Analytics recrute un Data Scientist pour industrialiser ses modèles et les mettre au service de "
           "ses équipes métier."),
        ph("Cette mission correspond précisément au travail que je souhaite poursuivre après mon alternance de Data "
           "Scientist.", "exp-fictive-ds"),
        ph("Je voudrais y apporter une approche rigoureuse, concrète et attentive aux besoins réels des utilisateurs.")]},
    "missions": [
        {"phrases": [
            ph("Sur la classification de tickets, j'ai nettoyé les données puis entraîné des modèles de classification "
               "avec Python et Scikit-learn.", "acc-classif-01", "acc-classif-02"),
            ph("J'ai ensuite contribué à la mise en production du pipeline selon les pratiques MLOps, ce qui m'a appris "
               "à relier la qualité des données à la tenue réelle d'un modèle.", "acc-classif-03"),
            ph("Ce travail demandait de garder une trace claire de chaque choix afin que l'équipe puisse reprendre le "
               "sujet sans difficulté.")]},
        {"phrases": [
            ph("J'ai également conçu un assistant de recherche documentaire avec LangChain et FAISS, de l'indexation "
               "vectorielle au prompt de réponse.", "acc-rag-01", "acc-rag-02"),
            ph("Ce travail rejoint votre intérêt pour les approches génératives et impose d'évaluer les réponses avec "
               "rigueur avant toute diffusion auprès des utilisateurs.")]}],
    "motivation": {"phrases": [
        ph("Votre offre met l'accent sur l'industrialisation des modèles et l'accompagnement des équipes produit, deux "
           "sujets sur lesquels je souhaite travailler dans la durée."),
        ph("J'aimerais aussi approfondir les sujets d'orchestration, comme Kubernetes, aux côtés de vos équipes."),
        ph("Rejoindre une équipe qui structure ses usages de la donnée me permettrait de mettre en pratique ce que j'ai "
           "appris en alternance, au contact de profils expérimentés.", "exp-fictive-ds"),
        ph("J'attache enfin de l'importance à expliquer clairement les résultats aux interlocuteurs non techniques, pour "
           "que les modèles servent réellement les décisions des équipes.")]},
}


def letter_with(section, index, *phrases):
    """Copie de LETTER où des phrases sont ajoutées au paragraphe donné."""
    data = copy.deepcopy(LETTER)
    target = data[section][index] if section == "missions" else data[section]
    target["phrases"].extend(phrases)
    return data


def issues(data, **analysis):
    cv, master = make_cv(**analysis)
    return cover_letter.validate_letter(Lettre.model_validate(data), master, OffreAnalyse.model_validate(cv["analyse"]))


def errors(data, **analysis):
    found = issues(data, **analysis)
    return found["bloquants"] + found["a_revoir"]


def level(data, fragment, **analysis):
    """Niveau (« bloquants » ou « a_revoir ») du premier écart qui contient fragment."""
    found = issues(data, **analysis)
    return next((lvl for lvl in ("bloquants", "a_revoir") if any(fragment in e for e in found[lvl])), None)


def has(errs, fragment):
    return any(fragment in e for e in errs)


class SequenceProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def generate(self, *, messages, model, api_key, timeout, schema):
        self.calls.append(messages)
        return schema.model_validate(self.responses.pop(0))


class GenerationTests(unittest.TestCase):
    def generate(self, *responses, settings=None):
        self.provider = SequenceProvider(responses)
        cv, _ = make_cv()
        with patch.dict(llm.PROVIDERS, {"fake": self.provider}), \
                patch("app.cover_letter.resolve_provider", return_value=("fake", "modele-test")):
            return cover_letter.generate_letter(cv, OFFRE, master_path=FIXTURE,
                                                settings=settings or Settings(llm_api_key="cle"))

    def test_valid_letter_is_accepted_in_one_call(self):
        self.assertEqual(errors(LETTER), [])
        result = self.generate(LETTER)
        self.assertEqual(result["tentatives"], 1)
        self.assertEqual((result["statut"], result["a_corriger"]), ("finale", []))
        self.assertTrue(250 <= result["mots"] <= 300)

    def test_no_personal_data_sent_to_llm(self):
        self.generate(LETTER)
        sent = json.dumps(self.provider.calls, ensure_ascii=False)
        for value in ("Camille", "EXEMPLE", "camille.exemple@example.com", "06 00 11 22 33", "0600112233",
                      "Villefictive-sur-Test", "linkedin", "github.com", "**"):
            self.assertNotIn(value, sent)
        payload = json.loads(self.provider.calls[0][1]["content"])
        self.assertEqual(payload["competences_absentes"], ["Kubernetes"])
        self.assertIn("politesse", self.provider.calls[0][0]["content"])

    def test_invalid_letter_gets_one_correction_with_the_errors(self):
        bad = letter_with("missions", 0, ph("J'ai aussi travaillé sur le cas acc-inconnu.", "acc-invente-99"))
        result = self.generate(bad, LETTER)
        self.assertEqual(result["tentatives"], 2)
        second = json.loads(self.provider.calls[1][1]["content"])
        self.assertTrue(any("acc-invente-99" in c for c in second["corrections_a_appliquer"]))

    def test_blocking_issue_after_correction_is_refused(self):
        bad = letter_with("missions", 0, ph("J'ai aussi travaillé sur ce cas.", "acc-invente-99"))
        with self.assertRaisesRegex(LetterError, "points bloquants.*acc-invente-99"):
            self.generate(bad, bad)
        self.assertEqual(len(self.provider.calls), 2)

    def test_style_issue_after_correction_gives_a_draft_with_its_points(self):
        jargon = letter_with("motivation", None, ph("Ce poste est une opportunité intéressante."))
        result = self.generate(jargon, jargon)
        self.assertEqual(result["statut"], "brouillon")
        self.assertEqual(result["a_corriger"], ["formule creuse ou jargon interdit : « opportunité »"])
        second = json.loads(self.provider.calls[1][1]["content"])
        self.assertIn("formule creuse ou jargon interdit : « opportunité »", second["corrections_a_appliquer"])

    def test_letter_uses_its_own_model_and_never_a_lite_one(self):
        seen = []
        with patch("app.cover_letter.resolve_provider", side_effect=lambda s, mode: seen.append(
                (s.llm_model, s.llm_fallback_model)) or ("fake", s.llm_model)), \
                patch.dict(llm.PROVIDERS, {"fake": SequenceProvider([LETTER])}):
            cv, _ = make_cv()
            cover_letter.generate_letter(cv, OFFRE, master_path=FIXTURE, settings=Settings(
                llm_api_key="cle", llm_model="gemini-x-flash-lite", llm_fallback_model="gemini-y-flash-lite",
                letter_model="gemini-x-flash", letter_fallback_model=""))
        # LETTER_MODEL pour la lettre ; le secours « lite » hérité de l'analyse est écarté.
        self.assertEqual(seen, [("gemini-x-flash", "")])
        with self.assertRaisesRegex(LetterError, "« lite », interdit pour la lettre"):
            self.generate(LETTER, settings=Settings(llm_api_key="cle", llm_model="gemini-x-flash-lite"))

    def test_no_llm_configured(self):
        cv, _ = make_cv()
        with self.assertRaisesRegex(LetterError, "Aucun LLM"):
            cover_letter.generate_letter(cv, OFFRE, master_path=FIXTURE, settings=Settings(llm_provider="local"))


class EvidenceTests(unittest.TestCase):
    def test_claim_without_evidence_is_refused(self):
        errs = errors(letter_with("missions", 1, ph("J'ai aussi présenté ces résultats en comité.")))
        self.assertTrue(has(errs, "affirmation sans preuve"))

    def test_each_bullet_used_must_be_cited(self):
        claim = ph("J'ai aussi construit un tableau de bord et automatisé un rapport hebdomadaire.", "acc-rag-01")
        errs = errors(letter_with("missions", 1, claim))
        self.assertTrue(has(errs, "s'appuie sur acc-dash-01, absent de ses preuves"))
        self.assertTrue(has(errs, "s'appuie sur acc-dash-02, absent de ses preuves"))
        self.assertTrue(has(errs, "« acc-rag-01 » cité sans rapport"))

    def test_sentences_about_the_company_are_not_matched_to_bullets(self):
        company = ph("Exemplia Analytics construit un tableau de bord et automatise un rapport hebdomadaire.")
        self.assertFalse(has(errors(letter_with("accroche", None, company)), "absent de ses preuves"))

    def test_words_explained_by_cited_bullets_do_not_trigger_other_bullets(self):
        # « hebdomadaire » existe dans acc-classif-04 et acc-dash-02 ; la phrase cite acc-classif-04.
        claim = ph("J'ai assuré le suivi hebdomadaire des performances du modèle.", "acc-classif-04")
        self.assertFalse(has(errors(letter_with("missions", 0, claim)), "acc-dash-02"))

    def test_redundant_certification_bullet_does_not_count_as_a_project(self):
        cert = ph("J'ai obtenu 2 certifications de l'Organisme Fictif en Machine Learning.", "acc-dash-03")
        self.assertFalse(has(errors(letter_with("missions", 1, cert)), "projets"))

    def test_past_participle_matches_the_bullet_noun(self):
        # « formé » renvoie à « Formation… » : la puce doit être citée.
        claim = ph("J'ai aussi été formé et j'ai obtenu des certifications fictives.")
        self.assertTrue(has(errors(letter_with("missions", 1, claim)), "acc-dash-03, absent de ses preuves"))


class SkillTests(unittest.TestCase):
    def test_number_absent_from_master_is_refused_and_present_one_accepted(self):
        self.assertTrue(has(errors(letter_with("missions", 0, ph("J'ai atteint 95 % de précision.", "acc-classif-02"))),
                            "chiffre absent du CV : 95"))
        self.assertFalse(has(errors(letter_with("missions", 1, ph("J'ai obtenu 2 certifications fictives.", "acc-dash-03"))),
                             "chiffre absent"))

    def test_tool_absent_from_master_claimed_as_acquired_is_refused(self):
        self.assertTrue(has(errors(letter_with("missions", 0, ph("J'ai déployé ces modèles avec Kubernetes.", "acc-classif-03"))),
                            "« Kubernetes »"))

    def test_displayed_skill_is_never_a_notion_or_a_gap(self):
        self.assertTrue(has(errors(letter_with("missions", 1, ph("J'ai aussi des bases en SQL."))),
                            "« SQL » figure sur le CV"))
        self.assertTrue(has(errors(letter_with("missions", 1, ph("Je n'ai pas encore beaucoup pratiqué Git."))),
                            "« Git » figure sur le CV"))

    def test_knowledge_bases_are_not_a_gap(self):
        claim = ph("J'ai structuré deux bases de connaissances avec LangChain.", "acc-rag-01")
        self.assertFalse(has(errors(letter_with("missions", 1, claim)), "figure sur le CV"))

    def test_master_notion_is_presented_as_such(self):
        self.assertTrue(has(errors(letter_with("missions", 1, ph("Je maîtrise Docker au quotidien."))),
                            "« Docker » est une notion du Master"))
        self.assertFalse(has(errors(letter_with("missions", 1, ph("J'ai vu les notions de Docker en Master.",
                                                                    "frm-fictif-master"))), "Docker"))

    def test_origin_of_a_skill_must_be_proven(self):
        self.assertTrue(has(errors(letter_with("missions", 1, ph("Durant ma formation, j'ai utilisé LangChain.",
                                                                    "frm-fictif-master"))),
                            "origine « formation » non prouvée par le CV pour « LangChain »"))
        self.assertFalse(has(errors(letter_with("missions", 1, ph("En alternance, j'ai utilisé LangChain.",
                                                                    "acc-rag-01"))), "origine"))

    def test_absent_skills_get_one_general_sentence_at_most(self):
        two = letter_with("motivation", None, ph("Je souhaite aussi découvrir de nouveaux outils."))
        self.assertTrue(has(errors(two), "au plus une phrase sur l'envie d'approfondir"))
        listed = copy.deepcopy(LETTER)
        listed["motivation"]["phrases"][1] = ph("J'aimerais approfondir Kubernetes et Spark aux côtés de vos équipes.")
        self.assertTrue(has(errors(listed, competences_requises=["Python", "Kubernetes", "Spark"]),
                            "énumérées une à une"))

    def test_certification_is_mentioned_once_when_the_offer_asks_its_topic(self):
        self.assertTrue(has(errors(LETTER, mots_cles=["Machine Learning"]), "mentionner la certification"))
        with_cert = letter_with("missions", 1, ph("J'ai obtenu 2 certifications de l'Organisme Fictif en Machine Learning.",
                                                  "acc-dash-03"))
        self.assertFalse(has(errors(with_cert, mots_cles=["Machine Learning"]), "certification"))


class LevelTests(unittest.TestCase):
    def test_unproven_facts_personal_data_and_politeness_are_blocking(self):
        cases = {
            "chiffre absent du CV": letter_with("missions", 0, ph("J'ai atteint 95 % de précision.", "acc-classif-02")),
            "outil ou compétence absent": letter_with("missions", 0, ph("J'ai déployé ces modèles avec Kubernetes.",
                                                                        "acc-classif-03")),
            "origine « formation »": letter_with("missions", 1, ph("Durant ma formation, j'ai utilisé LangChain.",
                                                                   "frm-fictif-master")),
            "affirmation sans preuve": letter_with("missions", 1, ph("J'ai aussi présenté ces résultats en comité.")),
            "formule de politesse écrite par le LLM": letter_with("motivation", None,
                                                                  ph("Veuillez agréer mes salutations distinguées.")),
            "donnée personnelle": letter_with("motivation", None, ph("Écrivez-moi à camille.exemple@example.com.")),
        }
        for fragment, data in cases.items():
            with self.subTest(fragment=fragment):
                self.assertEqual(level(data, fragment), "bloquants")

    def test_length_jargon_and_style_are_to_review(self):
        short = copy.deepcopy(LETTER)
        short["motivation"]["phrases"] = short["motivation"]["phrases"][:1]
        cases = {
            "longueur de": short,
            "formule creuse ou jargon": letter_with("motivation", None, ph("Une opportunité à saisir.")),
            "figure sur le CV": letter_with("missions", 1, ph("J'ai aussi des bases en SQL.")),
            "au plus une phrase": letter_with("motivation", None, ph("Je souhaite aussi découvrir de nouveaux outils.")),
        }
        for fragment, data in cases.items():
            with self.subTest(fragment=fragment):
                self.assertEqual(level(data, fragment), "a_revoir")


class StyleTests(unittest.TestCase):
    def test_jargon_company_name_politeness_and_length(self):
        jargon = letter_with("motivation", None, ph("Ce poste est une opportunité de valoriser les données."))
        errs = errors(jargon)
        self.assertTrue(has(errs, "« valoriser »") and has(errs, "« opportunité »"))
        anonymous = copy.deepcopy(LETTER)
        anonymous["accroche"]["phrases"][0] = ph("Votre équipe recrute un Data Scientist pour industrialiser ses modèles.")
        self.assertTrue(has(errors(anonymous), "nommer l'entreprise"))
        polite = letter_with("motivation", None, ph("Veuillez agréer mes salutations distinguées."))
        self.assertTrue(has(errors(polite), "formule de politesse écrite par le LLM"))
        short = copy.deepcopy(LETTER)
        short["motivation"]["phrases"] = short["motivation"]["phrases"][:1]
        self.assertTrue(has(errors(short), "longueur de"))

    def test_structure(self):
        one_project = copy.deepcopy(LETTER)
        one_project["missions"][1]["phrases"][0]["preuves"] = ["acc-classif-04"]
        self.assertTrue(has(errors(one_project), "2 à 3 projets"))
        single = copy.deepcopy(LETTER)
        single["missions"] = single["missions"][:1]
        self.assertTrue(has(errors(single), "exactement 2 paragraphes"))


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.cv, self.master = make_cv()
        self.result = {"lettre": LETTER}
        self.parts = cover_letter.letter_parts(self.result, self.cv, self.master, today=date(2026, 10, 27))

    def test_header_politeness_and_signature_come_from_code(self):
        text = cover_letter.letter_text(self.parts)
        lines = text.splitlines()
        self.assertEqual(lines[0], "Camille EXEMPLE")
        for expected in ("camille.exemple@example.com", "06 00 11 22 33", "Villefictive-sur-Test, le 27 octobre 2026",
                         "Objet : candidature au poste de Data Scientist (H/F) – CDI", "Madame, Monsieur,"):
            self.assertIn(expected, text)
        self.assertEqual(self.parts["corps"][-1], POLITESSE)
        self.assertEqual(lines[-1], "Camille EXEMPLE")

    def test_files_are_written_with_a_fake_renderer(self):
        def one_page(html):
            document = pymupdf.open()
            document.new_page()
            return document.tobytes()

        with tempfile.TemporaryDirectory() as folder:
            files = cover_letter.build_letter_files(self.result, self.cv, self.master, folder, render=one_page,
                                                    today=date(2026, 10, 27))
            self.assertEqual(files["pdf"].name, "LM_EXEMPLE_Exemplia-Analytics_Data-Scientist.pdf")
            self.assertTrue(files["texte"].read_text().startswith("Camille EXEMPLE"))

    def test_draft_is_clearly_marked_and_replaces_the_final_version(self):
        def one_page(html):
            document = pymupdf.open()
            document.new_page()
            return document.tobytes()

        draft = {"lettre": LETTER, "statut": "brouillon", "a_corriger": ["longueur de 240 mots"]}
        with tempfile.TemporaryDirectory() as folder:
            final = cover_letter.build_letter_files(self.result, self.cv, self.master, folder, render=one_page)
            files = cover_letter.build_letter_files(draft, self.cv, self.master, folder, render=one_page)
            self.assertEqual(files["pdf"].name, "LM_EXEMPLE_Exemplia-Analytics_Data-Scientist_BROUILLON.pdf")
            self.assertFalse(final["pdf"].exists())  # l'ancienne version finale ne peut plus être prise pour la bonne
            text = files["texte"].read_text()
            self.assertTrue(text.startswith("=== BROUILLON À CORRIGER – NE PAS ENVOYER ==="))
            self.assertIn("- longueur de 240 mots", text)
        parts = cover_letter.letter_parts(draft, self.cv, self.master)
        self.assertIn("BROUILLON À CORRIGER", cover_letter.letter_html(parts))
        self.assertNotIn("BROUILLON", cover_letter.letter_html(self.parts))

    def test_text_is_escaped_in_html(self):
        parts = dict(self.parts, corps=["<script>x</script>"])
        self.assertNotIn("<script>", cover_letter.letter_html(parts))


def chromium_available():
    try:
        with cv_pdf.ChromiumRenderer():
            return True
    except Exception:
        return False


@unittest.skipUnless(chromium_available(), "Chromium (Playwright) non installé")
class ChromiumLetterTests(unittest.TestCase):
    def test_real_letter_pdf_is_one_page_and_selectable(self):
        cv, master = make_cv()
        with tempfile.TemporaryDirectory() as folder:
            files = cover_letter.build_letter_files({"lettre": LETTER}, cv, master, folder, today=date(2026, 10, 27))
            with pymupdf.open(files["pdf"]) as document:
                self.assertEqual(document.page_count, 1)
                self.assertIn("Je vous prie d'agréer", document[0].get_text())
                sizes = [s["size"] for b in document[0].get_text("dict")["blocks"] for l in b.get("lines", [])
                         for s in l["spans"]]
                self.assertGreaterEqual(min(sizes), 9.9)


if __name__ == "__main__":
    unittest.main()
