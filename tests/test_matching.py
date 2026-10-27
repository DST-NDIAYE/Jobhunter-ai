"""Tests locaux : CV maître fictif, aucun identifiant ni appel réseau."""

from pathlib import Path
import unittest

from app.matching import calculate_match_score, cv_text_from_master

FIXTURE = Path(__file__).parent / "fixtures" / "cv_master_fictif.yaml"


class MasterTextTests(unittest.TestCase):
    def test_text_comes_from_the_master_without_markup_or_contacts(self):
        text = cv_text_from_master(FIXTURE)
        for expected in ("Entraînement de modèles de classification.", "LangChain", "Docker", "AWS",
                         "Master fictif de Science des Données", "Data Scientist junior orientée Machine Learning."):
            self.assertIn(expected, text)
        for absent in ("**", "camille.exemple@example.com", "06 00 11 22 33", "Camille"):
            self.assertNotIn(absent, text)

    def test_missing_master_gives_no_text(self):
        self.assertEqual(cv_text_from_master(Path("/chemin/inexistant/cv_master.yaml")), "")

    def test_master_text_scores_a_related_offer_higher(self):
        text = cv_text_from_master(FIXTURE)
        related = calculate_match_score(text, "Data Scientist Python Scikit-learn classification LangChain FAISS")
        unrelated = calculate_match_score(text, "Boulanger pâtissier viennoiseries")
        self.assertGreater(related, unrelated)


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
