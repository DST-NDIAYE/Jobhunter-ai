"""Routes REST de JobHunter AI : état, recherche France Travail, suivi des candidatures.

Aucune route n'envoie de candidature ni d'e-mail, et aucune ne renvoie de coordonnées ni de secret.
"""

from datetime import datetime, timezone, timedelta
import sqlite3
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.application_channels import extract_contact_emails
from app.applications import APPLICATION_STATUSES, get_offer, update_status
from app.config import get_settings
from app.cv_tailor import MASTER_PATH, CVTailorError, load_master
from app.database import add_job, get_jobs
from app.france_travail import search_jobs
from app.llm import resolve_provider
from app.matching import calculate_match_score, cv_text_from_master
from app.opportunities import list_opportunities

api_router = APIRouter(prefix="/api")


class StatusUpdateRequest(BaseModel):
    status: str
    personal_notes: str | None = None


def _master_status() -> dict[str, Any]:
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
    letter_model = settings.letter_model or settings.llm_model
    return {
        "ok": True,
        **_master_status(),
        "ft_configured": bool(settings.france_travail_client_id and settings.france_travail_client_secret),
        "llm_provider": provider or "local",
        "llm_configured": bool(provider),
        "llm_model": model,
        "llm_fallback_model": settings.llm_fallback_model,
        "letter_model": letter_model if provider else "",
        "gmail_configured": bool(settings.gmail_app_password or settings.gmail_token_file.is_file()),
    }


@api_router.get("/stats")
def get_tracking_stats():
    settings = get_settings()
    db_path = settings.database_path

    # Récupérer toutes les opportunités et offres
    jobs = get_jobs(db_path=db_path)
    opps = list_opportunities(db_path=db_path)
    all_items = jobs + opps

    sent_items = [
        item for item in all_items
        if item.get("application_status") == "Candidature envoyée" or item.get("application_sent_at")
    ]
    interview_items = [
        item for item in all_items
        if item.get("application_status") == "Entretien"
    ]
    pipeline_items = [
        item for item in all_items
        if item.get("application_status") in ("À postuler", "À étudier", "Relancée", "En cours", "Candidature préparée")
    ]
    refused_items = [
        item for item in all_items
        if item.get("application_status") == "Refus"
    ]

    # Relances +7 jours
    now = datetime.now(timezone.utc)
    to_remind = []
    for item in sent_items:
        sent_at_str = item.get("application_sent_at") or item.get("status_updated_at")
        if sent_at_str:
            try:
                sent_at = datetime.fromisoformat(sent_at_str.replace("Z", "+00:00"))
                if now - sent_at > timedelta(days=7):
                    to_remind.append(item)
            except Exception:
                pass

    return {
        "sent_count": len(sent_items),
        "interview_count": len(interview_items),
        "remind_count": len(to_remind),
        "pipeline_count": len(pipeline_items),
        "refused_count": len(refused_items),
        "total_tracked": len(all_items),
    }


@api_router.get("/applications")
def list_tracked_applications(
    status: str | None = None,
    channel: str | None = None,
    search: str | None = None,
):
    settings = get_settings()
    db_path = settings.database_path

    jobs = get_jobs(db_path=db_path)
    opps = list_opportunities(db_path=db_path)
    all_items = jobs + opps

    # Filtre par statut
    if status and status != "all":
        if status == "sent":
            all_items = [i for i in all_items if i.get("application_status") == "Candidature envoyée" or i.get("application_sent_at")]
        elif status == "interview":
            all_items = [i for i in all_items if i.get("application_status") == "Entretien"]
        elif status == "remind":
            now = datetime.now(timezone.utc)
            remind_list = []
            for item in all_items:
                sent_at_str = item.get("application_sent_at") or item.get("status_updated_at")
                if sent_at_str:
                    try:
                        sent_at = datetime.fromisoformat(sent_at_str.replace("Z", "+00:00"))
                        if now - sent_at > timedelta(days=7) and item.get("application_status") != "Entretien" and item.get("application_status") != "Refus":
                            remind_list.append(item)
                    except Exception:
                        pass
            all_items = remind_list
        elif status == "pipeline":
            all_items = [i for i in all_items if i.get("application_status") in ("À postuler", "À étudier", "Relancée", "En cours", "Candidature préparée")]
        else:
            all_items = [i for i in all_items if i.get("application_status") == status]

    # Filtre par canal
    if channel and channel != "all":
        all_items = [i for i in all_items if i.get("application_channel") == channel or i.get("source") == channel]

    # Recherche textuelle (titre, entreprise, ville)
    if search:
        s = search.lower().strip()
        all_items = [
            i for i in all_items
            if s in (i.get("title") or "").lower()
            or s in (i.get("company") or "").lower()
            or s in (i.get("location") or "").lower()
        ]

    # Tri antéchronologique
    all_items.sort(
        key=lambda x: x.get("application_sent_at") or x.get("status_updated_at") or x.get("first_seen_at") or "",
        reverse=True,
    )
    return all_items


@api_router.patch("/applications/{source}/{external_id}")
def update_application_status_and_notes(
    source: str,
    external_id: str,
    req: StatusUpdateRequest,
):
    settings = get_settings()
    db_path = settings.database_path

    if req.status not in APPLICATION_STATUSES:
        raise HTTPException(status_code=400, detail=f"Statut inconnu : {req.status}")

    # Mise à jour du statut
    update_status([(source, external_id)], req.status, db_path=db_path)

    # Mise à jour des notes si fournies
    if req.personal_notes is not None:
        table = "opportunities" if source == "labonneboite" else "jobs"
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                f"UPDATE {table} SET personal_notes = ? WHERE external_id = ? AND source = ?",
                (req.personal_notes, external_id, source),
            )

    updated = get_offer(external_id, source=source, db_path=db_path)
    return {"ok": True, "item": updated}


@api_router.get("/jobs/search")
def search_offers(keyword: str = "Data Scientist", location: str | None = None, contract: str | None = None,
                  limit: int = 20):
    """Offres France Travail enregistrées en base et classées par score de correspondance avec le CV maître."""
    settings = get_settings()
    jobs = search_jobs(keyword=keyword, location=location or None,
                       contract_type=contract if contract and contract != "Tous" else None, limit=limit)
    cv_text = cv_text_from_master()
    scored = []
    for job in jobs:
        add_job(job, db_path=settings.database_path)
        emails = extract_contact_emails(f"{job.get('title', '')} {job.get('description', '')}")
        scored.append({**job, "match_score": calculate_match_score(cv_text, job.get("description", "")) if cv_text else None,
                       "contact_emails": emails, "contact_email": emails[0] if emails else (job.get("contact_email") or "")})
    scored.sort(key=lambda x: x.get("match_score") or 0, reverse=True)
    return scored[:limit]


@api_router.get("/gmail/status")
def get_gmail_status():
    """Gmail configuré ou non (aucun envoi ni brouillon n'est possible depuis l'application)."""
    settings = get_settings()
    return {"configured": bool(settings.gmail_app_password or settings.gmail_token_file.is_file()),
            "method": "app_password" if settings.gmail_app_password else ("oauth" if settings.gmail_token_file.is_file() else None)}
