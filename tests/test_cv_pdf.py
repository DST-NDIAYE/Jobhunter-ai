"""CV PDF : CV maître fictif, rendu HTML vérifié sans navigateur, puis un vrai rendu Chromium."""

from pathlib import Path
import re
import tempfile
import unittest

import pymupdf

from app import cv_pdf, cv_tailor
from app.cv_tailor import OffreAnalyse, Selection

FIXTURE = Path(__file__).parent / "fixtures" / "cv_master_fictif.yaml"
OFFER = "Offre fictive : Python, Docker, conteneurisation, Kubernetes."


def make_cv(master=None, projets=(), accroche="ml"):
    master = master or cv_tailor.load_master(FIXTURE)
    analyse = OffreAnalyse.model_validate({
        "intitule": "Data Scientist (H/F)", "entreprise": "Exemplia Analytics", "type_contrat": "CDI",
        "competences_requises": ["Python", "conteneurisation", "Kubernetes"], "competences_souhaitees": [],
        "mots_cles": [], "experience_min_annees": None, "niveau_experience": "junior", "langue": "fr"})
    selection = Selection.model_validate({"accroche": accroche, "projets": list(projets)})
    return {"analyse": analyse.model_dump(), **cv_tailor.assemble(selection, analyse, master, OFFER)}, master


def fake_renderer(fits):
    """Faux convertisseur : une page si fits(html) est vrai, sinon deux."""
    def render(html):
        document = pymupdf.open()
        for _ in range(1 if fits(html) else 2):
            document.new_page()
        return document.tobytes()
    return render


def css_values(html, prop, unit):
    return [float(v) for v in re.findall(rf"{prop}:\s*([\d.]+){unit}", html)]


class HtmlTests(unittest.TestCase):
    def setUp(self):
        self.cv, self.master = make_cv()
        self.html = cv_pdf.render_html(self.cv, self.master)

    def test_everything_is_displayed_by_default(self):
        for text in ("Nettoyage des données de tickets.", "Rédaction de la documentation du pipeline.",
                     "Formation fictive et obtention de 2 certifications.", "Algèbre, Probabilités.",
                     "<b>Qualités</b> : Rigueur fictive", "<b>Notions (Master)</b> : Docker, AWS",
                     "Certification Fictive A • Certification Fictive B", "<b>Français</b> (Natif)"):
            self.assertIn(text, self.html)
        self.assertEqual(self.html.count("<li>"), 10)

    def test_layout_follows_the_ats_cv(self):
        order = [self.html.index(f"<h2>{title}</h2>") for title in
                 ("Expériences professionnelles", "Formations", "Compétences",
                  "Certifications Organisme Fictif (2)", "Langues &amp; Qualités")]
        self.assertEqual(order, sorted(order))
        self.assertIn("Data Scientist (Alternance)", self.html)
        self.assertNotIn("(Stage)", self.html)
        self.assertIn('<span class="date">De sept. 2024 à sept. 2025 | Villefictive</span>', self.html)
        self.assertIn("LinkedIn : <a", self.html)
        self.assertIn("<b>Technologies :</b> Python, Scikit-learn, TF-IDF.", self.html)
        self.assertIn("Entraînement de <b>modèles de classification</b>.", self.html)

    def test_single_column_without_images_or_tables(self):
        for forbidden in ("<img", "<table", "<svg", "background-image", "column-count", "grid"):
            self.assertNotIn(forbidden, self.html)

    def test_master_text_is_escaped_before_bold(self):
        master = cv_tailor.load_master(FIXTURE)
        master["experiences"][0]["projets"][0]["accomplissements"][0]["texte"] = "<script>x</script> **gras**"
        cv, master = make_cv(master=master)
        html = cv_pdf.render_html(cv, master)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;x&lt;/script&gt; <b>gras</b>", html)

    def test_filename(self):
        self.assertEqual(cv_pdf.pdf_filename(self.cv, self.master), "CV_EXEMPLE_Exemplia-Analytics_Data-Scientist.pdf")


class ReductionTests(unittest.TestCase):
    def test_styles_never_go_below_limits(self):
        for font, _, margin in cv_pdf.STYLES:
            self.assertGreaterEqual(font, cv_pdf.MIN_FONT_PT)
            self.assertGreaterEqual(margin, cv_pdf.MIN_MARGIN_MM)
        cv, master = make_cv()
        tightest = cv_pdf.render_html(cv, master, {"style": len(cv_pdf.STYLES) - 1})
        self.assertGreaterEqual(min(css_values(tightest, "font-size", "pt")), 9)
        self.assertGreaterEqual(min(css_values(tightest, "margin", "mm")), 12)

    def test_nothing_is_removed_when_it_fits(self):
        cv, master = make_cv()
        _, _, details = cv_pdf.fit_one_page(cv, master, fake_renderer(lambda html: True))
        self.assertEqual((details["police_corps_pt"], details["marges_mm"]), (10.0, 15))
        self.assertFalse(details["qualites_retirees"] or details["matieres_anterieures_retirees"])
        self.assertEqual(details["puces_retirees"], [])

    def test_reductions_happen_in_order(self):
        cv, master = make_cv()
        seen = []

        def fits(html):
            seen.append(("Qualités</b>" in html, "Algèbre" in html, html.count("<li>")))
            return html.count("<li>") <= 8

        _, _, details = cv_pdf.fit_one_page(cv, master, fake_renderer(fits))
        styles = len(cv_pdf.STYLES)
        # a) chaque palier de style avec tout le contenu ;
        self.assertEqual(seen[:styles], [(True, True, 10)] * styles)
        # b) puis sans Qualités ; c) puis sans les matières des licences ; d) puis des puces en moins.
        self.assertEqual(seen[styles:], [(False, True, 10), (False, False, 10), (False, False, 9), (False, False, 8)])
        self.assertTrue(details["qualites_retirees"] and details["matieres_anterieures_retirees"])

    def test_least_relevant_bullets_go_first_and_three_per_project_remain(self):
        cv, master = make_cv()  # priorité : classif (5), rag (2), dashboard (3, dont le redondant en dernier)
        _, final, details = cv_pdf.fit_one_page(cv, master, fake_renderer(lambda html: html.count("<li>") <= 8))
        # Seul classif a plus de 3 puces : ses deux dernières partent, rag et dashboard restent entiers.
        self.assertEqual(details["puces_retirees"], ["acc-classif-05", "acc-classif-04"])
        self.assertIn("acc-dash-03", final["ordre_accomplissements"])
        self.assertIn("acc-rag-02", final["ordre_accomplissements"])

    def test_clear_error_when_three_bullets_per_project_do_not_fit(self):
        cv, master = make_cv()
        with self.assertRaisesRegex(ValueError, "3 puces par projet"):
            cv_pdf.fit_one_page(cv, master, fake_renderer(lambda html: html.count("<li>") <= 7))

    def test_projects_are_never_removed(self):
        cv, master = make_cv()
        _, final, _ = cv_pdf.fit_one_page(cv, master, fake_renderer(lambda html: html.count("<li>") <= 8))
        html = cv_pdf.render_html(final, master)
        for title in ("Classification de tickets fictifs", "Assistant RAG fictif", "Tableau de bord fictif"):
            self.assertIn(title, html)


def chromium_available():
    try:
        with cv_pdf.ChromiumRenderer():
            return True
    except Exception:
        return False


@unittest.skipUnless(chromium_available(), "Chromium (Playwright) non installé")
class ChromiumTests(unittest.TestCase):
    def test_full_cv_is_one_selectable_page_with_keyword_report(self):
        cv, master = make_cv()
        with tempfile.TemporaryDirectory() as folder:
            result = cv_pdf.build_cv_pdf(cv, master, output_dir=folder)
            with pymupdf.open(result["path"]) as document:
                self.assertEqual(document.page_count, 1)
                page = document[0]
                text = page.get_text()
                self.assertIn("Rédaction de la documentation du pipeline.", text)
                self.assertIn("De sept. 2024 à sept. 2025", text)
                # Puces en vrais caractères, lisibles par un ATS.
                self.assertEqual(text.count("•"), 10 + 1)  # 10 puces + 1 séparateur de certifications
                self.assertEqual(page.get_images(), [])
                sizes = [span["size"] for block in page.get_text("dict")["blocks"]
                         for line in block.get("lines", []) for span in line["spans"]]
                self.assertGreaterEqual(min(sizes), 8.9)
                # Aucun texte ne déborde dans la marge droite (il y serait rogné à l'impression).
                right_edge = page.rect.width - result["marges_mm"] / 25.4 * 72
                rightmost = max(span["bbox"][2] for block in page.get_text("dict")["blocks"]
                                for line in block.get("lines", []) for span in line["spans"])
                self.assertLessEqual(rightmost, right_edge + 0.5)
        self.assertEqual(result["puces_retirees"], [])
        present = {p["mot_cle"]: p["via"] for p in result["mots_cles"]["presents"]}
        self.assertEqual(present["conteneurisation"], "Docker")
        self.assertEqual(result["mots_cles"]["absents"], ["Kubernetes"])

    def test_overflowing_cv_is_trimmed_after_layout_qualities_and_subjects(self):
        master = cv_tailor.load_master(FIXTURE)
        long_text = "Accomplissement fictif très détaillé destiné à remplir la page. " * 15
        for item in master["experiences"][0]["projets"][0]["accomplissements"]:
            item["texte"] = long_text
        cv, master = make_cv(master=master)
        with tempfile.TemporaryDirectory() as folder:
            result = cv_pdf.build_cv_pdf(cv, master, output_dir=folder)
        self.assertEqual(result["pages"], 1)
        self.assertTrue(result["puces_retirees"])
        self.assertTrue(result["qualites_retirees"] and result["matieres_anterieures_retirees"])
        self.assertEqual(result["police_corps_pt"], cv_pdf.MIN_FONT_PT)
        self.assertTrue(all(i.startswith("acc-classif-") for i in result["puces_retirees"]))


if __name__ == "__main__":
    unittest.main()
