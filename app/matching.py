"""Similarité lexicale simple entre le CV maître et une offre, sans service externe."""

from pathlib import Path

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from app.cv_tailor import MASTER_PATH, NOTIONS_CATEGORY, CVTailorError, load_master, master_text


def cv_text_from_master(master_path: Path = MASTER_PATH) -> str:
    """Tout le texte du CV maître (accroche par défaut, puces, compétences, notions, formations).

    Renvoie une chaîne vide si le master est absent ou invalide : les offres sont alors listées sans score.
    """
    try:
        master = load_master(master_path)
    except CVTailorError:
        return ""
    accroche = " ".join(" ".join(text.split()) for text in master["accroches"].values())
    notions = " ".join(s["nom"] for s in master["competences"].get(NOTIONS_CATEGORY) or [])
    return f"{master_text(master, accroche)}\n{notions}"


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
