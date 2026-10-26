"""Tests locaux : aucun vrai CV, identifiant ou appel réseau n'est nécessaire."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pymupdf

from app.matching import CVError, calculate_match_score, extract_cv_text


class ExtractCVTextTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "cv.pdf"

    def write_pdf(self, text="", encrypted=False):
        with pymupdf.open() as document:
            page = document.new_page()
            if text:
                page.insert_text((72, 72), text)
            options = {}
            if encrypted:
                options = {
                    "encryption": pymupdf.PDF_ENCRYPT_AES_256,
                    "owner_pw": "test-owner",
                    "user_pw": "test-reader",
                }
            document.save(self.path, **options)

    def test_extracts_and_cleans_multiple_pages_and_paragraphs(self):
        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_text((72, 72), "  Data   Scientist  ")
            page.insert_text((72, 150), "Python    SQL")
            document.new_page().insert_text((72, 72), "Machine Learning")
            document.save(self.path)

        text = extract_cv_text(self.path)

        self.assertEqual(text, "Data Scientist\n\nPython SQL\n\nMachine Learning")
        self.assertEqual(extract_cv_text(str(self.path)), text)

    def test_missing_file_has_actionable_error(self):
        with self.assertRaisesRegex(CVError, "introuvable"):
            extract_cv_text(self.path)

    def test_invalid_and_zero_byte_pdf_have_actionable_error(self):
        for content in (b"", b"Ce fichier ne contient pas de PDF."):
            with self.subTest(content=content):
                self.path.write_bytes(content)
                with self.assertRaisesRegex(CVError, "PDF valide"):
                    extract_cv_text(self.path)

    def test_unreadable_pdf_has_actionable_error(self):
        self.write_pdf("Python")
        with patch("app.matching.pymupdf.open", side_effect=PermissionError):
            with self.assertRaisesRegex(CVError, "droits de lecture"):
                extract_cv_text(self.path)

    def test_password_protected_pdf_has_actionable_error(self):
        self.write_pdf("Python", encrypted=True)
        with self.assertRaisesRegex(CVError, "mot de passe"):
            extract_cv_text(self.path)

    def test_empty_pdf_reports_ocr_limitation(self):
        self.write_pdf()
        with self.assertRaisesRegex(CVError, "aucun texte extractible.*OCR"):
            extract_cv_text(self.path)

    def test_image_only_pdf_reports_ocr_limitation(self):
        with pymupdf.open() as document:
            page = document.new_page()
            pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 10, 10), False)
            pixmap.clear_with(255)
            page.insert_image(pymupdf.Rect(72, 72, 172, 172), pixmap=pixmap)
            document.save(self.path)
        with self.assertRaisesRegex(CVError, "aucun texte extractible.*OCR"):
            extract_cv_text(self.path)


class MatchScoreTests(unittest.TestCase):
    def test_identical_text_scores_one_hundred(self):
        text = "Python SQL Data Scientist machine learning"
        self.assertEqual(calculate_match_score(text, text), 100.0)

    def test_disjoint_text_scores_zero(self):
        self.assertEqual(calculate_match_score("Python SQL", "Boulanger pâtissier"), 0.0)

    def test_empty_and_punctuation_only_inputs_score_zero(self):
        for cv_text, description in (
            ("", "Python"),
            (" \n ", "Python"),
            ("Python", ""),
            ("Python", None),
            ("!!!", "???"),
            ("!!!", "Python"),
            ("Python", "..."),
        ):
            with self.subTest(cv_text=cv_text, description=description):
                self.assertEqual(calculate_match_score(cv_text, description), 0.0)

    def test_accents_and_case_are_normalized(self):
        self.assertEqual(
            calculate_match_score("MODÉLISATION données", "modelisation DONNEES"),
            100.0,
        )

    def test_word_order_does_not_change_score(self):
        self.assertEqual(
            calculate_match_score("Python SQL statistiques", "statistiques Python SQL"),
            100.0,
        )

    def test_more_relevant_description_has_higher_score(self):
        cv_text = "Python SQL statistiques apprentissage automatique"
        close_score = calculate_match_score(cv_text, "Python SQL apprentissage automatique")
        partial_score = calculate_match_score(cv_text, "Python développement applications web")
        unrelated_score = calculate_match_score(cv_text, "Boulanger pâtissier")

        self.assertGreater(close_score, partial_score)
        self.assertGreater(partial_score, unrelated_score)
        self.assertLessEqual(close_score, 100.0)
        self.assertGreaterEqual(unrelated_score, 0.0)
        self.assertEqual(partial_score, round(partial_score, 2))


if __name__ == "__main__":
    unittest.main()
