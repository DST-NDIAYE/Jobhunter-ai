"""CV PDF : CV maître fictif, rendu HTML vérifié sans navigateur, puis un vrai rendu Chromium."""

from pathlib import Path
import re
import tempfile
import unittest

import pymupdf

from app import cv_pdf, cv_tailor
from app.cv_tailor import OffreAnalyse, Selection

FIXTURE = Path(__file__).parent / "fixtures" / "cv_master_fictif.yaml"


def make_cv(notions=("TensorFlow",), master=None, requises=("Python", "TensorFlow")):
    master = master or cv_tailor.load_master(FIXTURE)
    analyse = OffreAnalyse.model_validate({
        "intitule": "Data Scientist (H/F)", "entreprise": "Exemplia Analytics", "type_contrat": "CDI",
        "competences_requises": list(requises), "competences_souhaitees": [], "mots_cles": [],
        "experience_min_annees": None, "niveau_experience": "junior", "langue": "fr"})
    selection = Selection.model_validate({
        "accroche": "ml",
        "projets": [{"projet": "prj-fictif-rag", "accomplissements": ["acc-rag-02", "acc-rag-01"]},
                    {"projet": "prj-fictif-classif", "accomplissements": ["acc-classif-02", "acc-classif-01"]},
                    {"projet": "prj-fictif-dashboard", "accomplissements": ["acc-dash-01"]}],
        "competences": ["Python", "FAISS", "Pandas"], "competences_notions": list(notions)})
    offer = "Offre fictive. Compétences : " + ", ".join(requises)
    return {"analyse": analyse.model_dump(), **cv_tailor.assemble(selection, analyse, master, offer)}, master


def fake_renderer(fits):
    """Faux convertisseur : une page si fits(html) est vrai, sinon deux."""
    def render(html):
        document = pymupdf.open()
        for _ in range(1 if fits(html) else 2):
            document.new_page()
        return document.tobytes()
    return render


def at_most(n):
    return fake_renderer(lambda html: html.count("<li>") <= n)


class HtmlTests(unittest.TestCase):
    def setUp(self):
        self.cv, self.master = make_cv()
        self.html = cv_pdf.render_html(self.cv, self.master)

    def test_sober_single_column_without_images_or_tables(self):
        for forbidden in ("<img", "<table", "<svg", "background-image", "column-count", "grid"):
            self.assertNotIn(forbidden, self.html)
        sizes = [float(s) for s in re.findall(r"font-size:\s*([\d.]+)pt", self.html)]
        self.assertEqual(cv_pdf.BODY_FONT_PT, 10)
        self.assertGreaterEqual(min(sizes), 10)

    def test_content_comes_from_master_and_selection(self):
        for expected in ("camille.exemple@example.com", "06 00 11 22 33", "linkedin.com/in/camille-exemple-fictif",
                         "Data Scientist junior orientée Machine Learning. Je recherche un poste en CDI.",
                         "Conception du prompt de réponse.", "Master fictif de Science des Données",
                         "Notions :</b> TensorFlow", "Technologies : Python, LangChain, FAISS"):
            self.assertIn(expected, self.html)
        # Accomplissement non sélectionné : absent.
        self.assertNotIn("Mise en production du pipeline", self.html)
        # Ordre de priorité du LLM conservé dans chaque projet.
        self.assertLess(self.html.index("Conception du prompt"), self.html.index("Indexation vectorielle"))

    def test_notions_line_only_when_selected(self):
        cv, master = make_cv(notions=())
        self.assertNotIn("Notions", cv_pdf.render_html(cv, master))

    def test_compact_mode_keeps_only_most_recent_subjects(self):
        self.assertIn("Algèbre", self.html)
        compact = cv_pdf.render_html(self.cv, self.master, compact=True)
        self.assertNotIn("Algèbre", compact)
        self.assertIn("Deep Learning, Séries temporelles", compact)
        self.assertIn("Licence fictive de Mathématiques", compact)

    def test_master_text_is_escaped(self):
        master = cv_tailor.load_master(FIXTURE)
        master["accroches"]["ml"] = "<script>alert(1)</script>"
        cv, master = make_cv(master=master)
        html = cv_pdf.render_html(cv, master)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_filename(self):
        self.assertEqual(cv_pdf.pdf_filename(self.cv, self.master), "CV_EXEMPLE_Exemplia-Analytics_Data-Scientist.pdf")


class SkillCoherenceTests(unittest.TestCase):
    def test_skill_without_displayed_proof_moves_to_notions_if_cited(self):
        cv, master = make_cv(requises=("Python", "Pandas"))
        cv = cv_pdf.without_accomplishments(cv, ["acc-dash-01"])  # seule preuve de Pandas
        shown = cv_pdf.displayed_skills(cv, master)
        self.assertEqual([s["nom"] for s in shown["principales"]], ["Python", "FAISS"])
        self.assertEqual(shown["passees_en_notions"], ["Pandas"])
        self.assertIn("Pandas", [s["nom"] for s in shown["notions"]])

    def test_skill_without_displayed_proof_is_hidden_if_not_cited(self):
        cv, master = make_cv(requises=("Python",))
        cv = cv_pdf.without_accomplishments(cv, ["acc-dash-01"])
        shown = cv_pdf.displayed_skills(cv, master)
        self.assertEqual(shown["masquees"], ["Pandas"])
        self.assertNotIn("Pandas", cv_pdf.render_html(cv, master))

    def test_displayed_education_counts_as_proof(self):
        master = cv_tailor.load_master(FIXTURE)
        master["competences"]["data_science"][0]["preuves"].append("frm-fictif-master")  # Pandas
        cv, master = make_cv(master=master, requises=("Python",))
        cv = cv_pdf.without_accomplishments(cv, ["acc-dash-01"])
        self.assertIn("Pandas", [s["nom"] for s in cv_pdf.displayed_skills(cv, master)["principales"]])


class FitOnePageTests(unittest.TestCase):
    def test_subjects_are_removed_before_any_accomplishment(self):
        cv, master = make_cv()
        _, _, details = cv_pdf.fit_one_page(cv, master, fake_renderer(lambda html: "Algèbre" not in html))
        self.assertTrue(details["matieres_formations_anterieures_retirees"])
        self.assertEqual(details["accomplissements_retires"], [])

    def test_trims_the_most_crowded_project_first_and_keeps_one_per_project(self):
        cv, master = make_cv()
        with tempfile.TemporaryDirectory() as folder:
            result = cv_pdf.build_cv_pdf(cv, master, output_dir=folder, render=at_most(3))
            self.assertEqual(result["pages"], 1)
            # rag et classif ont 2 accomplissements : à égalité, le moins prioritaire (classif) d'abord.
            self.assertEqual(result["accomplissements_retires"], ["acc-classif-01", "acc-rag-01"])
            self.assertEqual(result["accomplissements_affiches"], ["acc-rag-02", "acc-classif-02", "acc-dash-01"])
            self.assertEqual(result["projets_retires"], [])
            self.assertTrue(Path(result["path"]).is_file())

    def test_only_projects_unrelated_to_the_offer_are_removed(self):
        cv, master = make_cv(requises=("LangChain",))  # seul le projet RAG est lié à l'offre
        _, final, details = cv_pdf.fit_one_page(cv, master, at_most(1))
        self.assertEqual(details["projets_retires"], ["prj-fictif-dashboard", "prj-fictif-classif"])
        self.assertEqual(final["ordre_accomplissements"], ["acc-rag-02"])

    def test_projects_linked_to_the_offer_are_never_removed(self):
        cv, master = make_cv(requises=("Python",))  # les trois projets utilisent Python
        with self.assertRaisesRegex(ValueError, "dépasse une page"):
            cv_pdf.fit_one_page(cv, master, at_most(2))

    def test_font_size_is_never_reduced(self):
        cv, master = make_cv(requises=("LangChain",))
        sizes = []

        def render(html):
            sizes.extend(float(s) for s in re.findall(r"font-size:\s*([\d.]+)pt", html))
            return at_most(1)(html)

        cv_pdf.fit_one_page(cv, master, render)
        self.assertGreaterEqual(min(sizes), 10)


def chromium_available():
    try:
        with cv_pdf.ChromiumRenderer():
            return True
    except Exception:
        return False


@unittest.skipUnless(chromium_available(), "Chromium (Playwright) non installé")
class ChromiumTests(unittest.TestCase):
    def test_real_pdf_is_one_selectable_page(self):
        cv, master = make_cv()
        with tempfile.TemporaryDirectory() as folder:
            result = cv_pdf.build_cv_pdf(cv, master, output_dir=folder)
            with pymupdf.open(result["path"]) as document:
                self.assertEqual(document.page_count, 1)
                page = document[0]
                self.assertIn("Conception du prompt de réponse.", page.get_text())
                self.assertEqual(page.get_images(), [])
                self.assertAlmostEqual(page.rect.width, 595, delta=2)  # A4 en points
                body_sizes = [round(span["size"], 1) for block in page.get_text("dict")["blocks"]
                              for line in block.get("lines", []) for span in line["spans"]]
                self.assertGreaterEqual(min(body_sizes), 9.9)

    def test_overflowing_cv_is_trimmed_to_one_page(self):
        master = cv_tailor.load_master(FIXTURE)
        long_text = "Accomplissement fictif très détaillé destiné à remplir la page. " * 12
        for exp in master["experiences"]:
            for project in exp["projets"]:
                for item in project["accomplissements"]:
                    item["texte"] = long_text
        cv, master = make_cv(master=master, requises=("LangChain",))
        with tempfile.TemporaryDirectory() as folder:
            result = cv_pdf.build_cv_pdf(cv, master, output_dir=folder)
        self.assertEqual(result["pages"], 1)
        self.assertTrue(result["matieres_formations_anterieures_retirees"])
        self.assertEqual(result["accomplissements_retires"][0], "acc-classif-01")
        self.assertIn("acc-rag-02", result["accomplissements_affiches"])


if __name__ == "__main__":
    unittest.main()
