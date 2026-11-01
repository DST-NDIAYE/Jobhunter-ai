"""Routes REST de JobHunter AI : état, recherche France Travail, préparation d'une candidature, suivi.

Aucune route n'envoie de candidature ni d'e-mail (candidatures spontanées : voir api_spontanees). Seuls les
fichiers générés pour le candidat (CV, lettre) contiennent ses coordonnées ; aucune route ne renvoie de secret.
"""

import json
import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.application_channels import extract_contact_emails, safe_url
from app.applications import (APPLICATION_STATUSES, SPONTANEE, get_offer, save_notes, tracked_applications,
                              update_status)
from app.config import PROJECT_ROOT, get_settings
from app.cover_letter import LetterError, build_letter_files, generate_letter, letter_parts, letter_text
from app.cv_pdf import OUTPUT_DIR, build_cv_pdf, write_preview
from app.cv_tailor import CACHE_DIR, MASTER_PATH, CVTailorError, load_master, offer_key, tailor_cv
from app.database import add_job
from app.france_travail import search_jobs
from app.llm import resolve_provider
from app.matching import calculate_match_score, cv_text_from_master
from app.spontanees import changer_statut, marquer_envoyee
from app.test_mode import fake_ft_enabled, fake_search_jobs

api_router = APIRouter(prefix="/api")

SEARCH_PROFILE = PROJECT_ROOT / "search_profile.json"
MANUAL_SOURCE = "manuel"
# Statuts qu'une nouvelle préparation peut remplacer par « Candidature préparée » (jamais un envoi ni une issue).
STATUTS_AVANT_PREPARATION = (None, "Nouvelle", "À étudier", "À postuler", "Échec", "Prête à envoyer", "Intervention requise")


class StatusUpdateRequest(BaseModel):
    status: str
    personal_notes: str | None = None


class PrepareRequest(BaseModel):
    offer_text: str
    url: str | None = None
    source: str | None = None
    external_id: str | None = None


def _llm_error(message: str) -> HTTPException:
    """Message clair et code adapté : quota atteint, modèle absent, entrée invalide, service indisponible."""
    if "Quota" in message:
        return HTTPException(status_code=429, detail=message)
    if message.startswith("Aucun LLM configuré") or "interdit pour la lettre" in message:
        return HTTPException(status_code=503, detail=message)
    if message.startswith("Texte de l'offre"):
        return HTTPException(status_code=400, detail=message)
    if message.startswith(("CV maître", "Phrases d'objectif", "Identifiants en double", "Compétences en double")):
        return HTTPException(status_code=500, detail=f"CV maître invalide : {message}")
    return HTTPException(status_code=502, detail=message)


def _master_status() -> dict:
    try:
        load_master(MASTER_PATH)
        return {"master_ready": True, "master_error": None}
    except CVTailorError as exc:
        return {"master_ready": False, "master_error": str(exc)}


@api_router.get("/status")
def get_system_status():
    """État des services, sans aucune coordonnée ni secret."""
    settings = get_settings()
    provider, model = resolve_provider(settings, "auto")
    return {
        "ok": True,
        **_master_status(),
        "ft_configured": fake_ft_enabled() or bool(settings.france_travail_client_id and settings.france_travail_client_secret),
        "ft_mode_test": fake_ft_enabled(),
        "llm_provider": provider or "local",
        "llm_configured": bool(provider),
        "llm_model": model,
        "llm_fallback_model": settings.llm_fallback_model if provider != "fake" else "",
        "letter_model": (model if provider == "fake" else (settings.letter_model or settings.llm_model)) if provider else "",
        "gmail_configured": bool(settings.gmail_app_password or settings.gmail_token_file.is_file()),
    }


def search_defaults() -> dict:
    """Valeurs par défaut de la recherche, lues dans search_profile.json."""
    try:
        profile = json.loads(SEARCH_PROFILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        profile = {}
    titles = [t for t in profile.get("preferred_titles") or [] if isinstance(t, str) and t.strip()]
    excluded = [t for t in profile.get("future_excluded_title_terms") or [] if isinstance(t, str) and t.strip()]
    return {"titres": titles or ["Data Scientist"], "mots_exclus": excluded, "lieu": "France entière"}


@api_router.get("/search/defaults")
def get_search_defaults():
    return search_defaults()


def _excluded(title: str, terms: list[str]) -> bool:
    return any(re.search(rf"(?i)(?<!\w){re.escape(term)}(?!\w)", title or "") for term in terms)


@api_router.get("/jobs/search")
def search_offers(keyword: str | None = None, ville: str | None = None, contrat: str | None = None,
                  limit: int = 20, masquer: bool = True):
    """Offres France Travail (France entière, ou une ville), classées par score avec le CV maître."""
    settings = get_settings()
    defaults = search_defaults()
    keyword = (keyword or "").strip() or defaults["titres"][0]
    search = fake_search_jobs if fake_ft_enabled() else search_jobs
    jobs = search(keyword=keyword, location=(ville or "").strip() or None,
                  contract_type=contrat if contrat and contrat != "Tous" else None, limit=limit)
    terms = defaults["mots_exclus"] if masquer else []
    visible = [job for job in jobs if not _excluded(job.get("title"), terms)]
    cv_text = cv_text_from_master(MASTER_PATH)
    scored = []
    for job in visible:
        add_job(job, db_path=settings.database_path)
        emails = extract_contact_emails(f"{job.get('title', '')} {job.get('description', '')}")
        scored.append({**job, "contact_email": emails[0] if emails else (job.get("contact_email") or ""),
                       "match_score": calculate_match_score(cv_text, job.get("description", "")) if cv_text else None})
    scored.sort(key=lambda x: x.get("match_score") or 0, reverse=True)
    return {"offres": scored[:limit], "masquees": len(jobs) - len(visible), "mots_exclus": terms, "mot_cle": keyword}


def _record_offer(req: PrepareRequest, cv: dict, url: str | None, db_path) -> tuple[str, str]:
    """Enregistre l'offre préparée pour le suivi ; une offre collée reçoit un identifiant stable."""
    if req.source and req.external_id:
        source, external_id = req.source, req.external_id
    else:
        source, external_id = MANUAL_SOURCE, "M-" + offer_key(req.offer_text)[:12]
    if source == "labonneboite":
        raise HTTPException(status_code=400, detail="Les opportunités La Bonne Boîte sont en lecture seule.")
    lines = [line.strip() for line in req.offer_text.splitlines() if line.strip()]
    add_job({"external_id": external_id, "source": source, "title": cv["analyse"]["intitule"] or (lines[0][:200] if lines else ""),
             "company": cv["analyse"]["entreprise"] or None, "contract_type": cv["analyse"]["type_contrat"],
             "description": req.offer_text, "url": url}, db_path=db_path)
    current = get_offer(external_id, source=source, db_path=db_path)
    if current and current.get("application_status") in STATUTS_AVANT_PREPARATION:
        update_status([(source, external_id)], "Candidature préparée", db_path=db_path)
    return source, external_id


def _file_links(name: str) -> dict:
    return {"fichier": name, "url": f"/api/files/{name}", "telechargement": f"/api/files/{name}?download=1"}


def _offer_text(req: PrepareRequest) -> str:
    text = (req.offer_text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Collez le texte de l'offre avant de préparer la candidature.")
    return text


def _tailor(text: str, settings) -> tuple[dict, dict]:
    try:
        return tailor_cv(text, master_path=MASTER_PATH, settings=settings, cache_dir=CACHE_DIR), load_master(MASTER_PATH)
    except CVTailorError as exc:
        raise _llm_error(str(exc)) from None


def _letter(cv: dict, text: str, master: dict, settings, regenerate: bool = False) -> dict:
    """Lettre reprise du cache si elle existe (sauf regenerate), vérifiée, puis PDF et texte."""
    result = generate_letter(cv, text, master_path=MASTER_PATH, settings=settings, cache_dir=CACHE_DIR,
                             regenerate=regenerate)
    files = build_letter_files(result, cv, master, output_dir=OUTPUT_DIR)
    parts = letter_parts(result, cv, master)
    return {"statut": result["statut"], "a_corriger": result["a_corriger"], "mots": result["mots"],
            "depuis_cache": result["depuis_cache"], "texte": letter_text({**parts, "statut": "finale"}),
            **_file_links(files["pdf"].name)}


@api_router.post("/prepare")
def prepare_application(req: PrepareRequest):
    """CV et lettre adaptés à l'offre, rapport, et enregistrement dans le suivi. Rien n'est envoyé."""
    settings = get_settings()
    text = _offer_text(req)
    url = None
    if req.url and req.url.strip():
        url = safe_url(req.url)
        if not url:
            raise HTTPException(status_code=400, detail="Le lien de l'offre doit commencer par http:// ou https://.")
    cv, master = _tailor(text, settings)
    try:
        cv_files = build_cv_pdf(cv, master, output_dir=OUTPUT_DIR)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    preview = write_preview(cv_files["path"])
    source, external_id = _record_offer(req.model_copy(update={"offer_text": text}), cv, url, settings.database_path)

    lettre, lettre_erreur = None, None
    try:
        lettre = _letter(cv, text, master, settings)  # jamais régénérée si elle est en cache
    except LetterError as exc:
        lettre_erreur = _llm_error(str(exc)).detail

    coverage = cv_files["mots_cles"]
    return {
        "offre": {"source": source, "external_id": external_id, "titre": cv["analyse"]["intitule"],
                  "entreprise": cv["analyse"]["entreprise"], "contrat": cv["analyse"]["type_contrat"], "url": url},
        "rapport": {"points_forts": coverage["presents"], "manques": coverage["absents"],
                    "informations": cv["rapport"]["informations"]},
        "cv": {**_file_links(cv_files["path"].name), "puces_retirees": cv_files["puces_retirees"],
               # Paramètre de version : l'aperçu change de contenu sous le même nom à chaque préparation.
               "apercu": f"/api/files/{preview.name}?v={int(preview.stat().st_mtime_ns)}",
               "police_pt": cv_files["police_corps_pt"]},
        "lettre": lettre,
        "lettre_erreur": lettre_erreur,
        "statut": (get_offer(external_id, source=source, db_path=settings.database_path) or {}).get("application_status"),
    }


@api_router.post("/prepare/lettre")
def regenerate_letter(req: PrepareRequest):
    """Nouvelle rédaction de la lettre pour la même offre (appel au modèle), qui remplace celle du cache."""
    settings = get_settings()
    text = _offer_text(req)
    cv, master = _tailor(text, settings)
    try:
        return {"lettre": _letter(cv, text, master, settings, regenerate=True)}
    except LetterError as exc:
        raise _llm_error(str(exc)) from None


@api_router.get("/files/{name}")
def get_generated_file(name: str, download: bool = False):
    """CV, aperçu ou lettre générés, uniquement depuis le dossier de sortie."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.(pdf|txt|png)", name) or ".." in name:
        raise HTTPException(status_code=400, detail="Nom de fichier invalide.")
    path = OUTPUT_DIR / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Fichier introuvable : préparez à nouveau la candidature.")
    media = {"pdf": "application/pdf", "png": "image/png"}.get(name.rsplit(".", 1)[1], "text/plain; charset=utf-8")
    return FileResponse(path, media_type=media, filename=name if download else None,
                        content_disposition_type="attachment" if download else "inline")


@api_router.get("/statuses")
def get_statuses():
    return list(APPLICATION_STATUSES)


@api_router.get("/applications")
def list_applications():
    """Candidatures réelles (offres ayant un statut) et opportunités La Bonne Boîte en lecture seule."""
    return tracked_applications(get_settings().database_path)


def _editable(source: str, external_id: str, db_path) -> dict:
    if source == "labonneboite":
        raise HTTPException(status_code=403, detail="Les opportunités La Bonne Boîte sont en lecture seule.")
    offer = get_offer(external_id, source=source, db_path=db_path)
    if offer is None:
        raise HTTPException(status_code=404, detail="Candidature introuvable.")
    return offer


def _tracked(db_path, source: str, external_id: str) -> dict | None:
    return next((r for r in tracked_applications(db_path) if r["source"] == source and r["external_id"] == external_id),
                None)


@api_router.patch("/applications/{source}/{external_id}")
def update_application(source: str, external_id: str, req: StatusUpdateRequest):
    db_path = get_settings().database_path
    if source == SPONTANEE:  # suite d'une candidature spontanée envoyée (entretien, refus…)
        changer_statut(db_path, external_id, req.status)
        return {"ok": True, "item": _tracked(db_path, source, external_id)}
    _editable(source, external_id, db_path)
    if req.status not in APPLICATION_STATUSES:
        raise HTTPException(status_code=400, detail=f"Statut inconnu : {req.status}")
    update_status([(source, external_id)], req.status, db_path=db_path)
    if req.personal_notes is not None:
        save_notes(external_id, req.personal_notes, source=source, db_path=db_path)
    return {"ok": True, "item": _tracked(db_path, source, external_id)}


@api_router.post("/applications/{source}/{external_id}/postule")
def mark_as_applied(source: str, external_id: str):
    """Marque la candidature comme envoyée (par le candidat lui-même) : date du jour et relance à 7 jours."""
    db_path = get_settings().database_path
    if source == SPONTANEE:
        marquer_envoyee(db_path, external_id, par="vous")
        return {"ok": True, "item": _tracked(db_path, source, external_id)}
    _editable(source, external_id, db_path)
    update_status([(source, external_id)], "Candidature envoyée", db_path=db_path)
    return {"ok": True, "item": _tracked(db_path, source, external_id)}


@api_router.get("/gmail/status")
def get_gmail_status():
    """Gmail configuré ou non (aucun envoi ni brouillon n'est possible depuis l'application)."""
    settings = get_settings()
    return {"configured": bool(settings.gmail_app_password or settings.gmail_token_file.is_file()),
            "method": "app_password" if settings.gmail_app_password else ("oauth" if settings.gmail_token_file.is_file() else None)}
