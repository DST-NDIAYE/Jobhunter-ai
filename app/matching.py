"""Lecture locale du CV et similarité lexicale simple, sans service externe."""

from pathlib import Path
import re

import pymupdf
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity


class CVError(Exception):
    """Le CV ne peut pas être lu ou ne contient pas de texte exploitable."""


def _clean_text(text: str) -> str:
    """Nettoie les espaces en conservant les lignes et les paragraphes."""
    lines = [re.sub(r"[^\S\n]+", " ", line).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def extract_cv_text(path: str | Path) -> str:
    """Extrait le texte d'un PDF local. Les PDF scannés nécessitent un OCR externe."""
    cv_path = Path(path).expanduser()
    try:
        if not cv_path.is_file():
            raise CVError(
                "Le CV PDF est introuvable. Vérifiez le fichier et le chemin configuré."
            )
        with pymupdf.open(cv_path) as document:
            if not document.is_pdf:
                raise CVError("Le fichier du CV doit être un PDF valide.")
            if document.needs_pass:
                raise CVError(
                    "Le CV PDF est protégé par un mot de passe. "
                    "Utilisez une copie non protégée."
                )
            paragraphs = []
            for page in document:
                # Les blocs de type 0 contiennent du texte, les autres des images.
                for block in page.get_text("blocks", sort=True):
                    if block[6] == 0:
                        paragraphs.append(block[4])
            text = _clean_text("\n\n".join(paragraphs))
    except (OSError, RuntimeError, ValueError) as exc:
        raise CVError(
            "Impossible de lire le CV PDF. Vérifiez les droits de lecture "
            "et que le fichier est un PDF valide."
        ) from exc

    if not text:
        raise CVError(
            "Le CV PDF ne contient aucun texte extractible. Il est peut-être "
            "vide ou scanné ; l'OCR n'est pas inclus dans cette V1."
        )
    return text


def calculate_match_score(cv_text: str, job_description: str | None) -> float:
    """Renvoie une similarité TF-IDF/cosinus entre 0 et 100, pas une probabilité."""
    if not cv_text.strip() or not job_description or not job_description.strip():
        return 0.0

    vectorizer = TfidfVectorizer(strip_accents="unicode", lowercase=True)
    analyze = vectorizer.build_analyzer()
    if not analyze(cv_text) or not analyze(job_description):
        return 0.0

    vectors = vectorizer.fit_transform([cv_text, job_description])
    similarity = float(cosine_similarity(vectors[0], vectors[1])[0, 0])
    return round(max(0.0, min(100.0, similarity * 100)), 2)
