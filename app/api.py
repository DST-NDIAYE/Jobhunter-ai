"""Routes API REST pour le front-end moderne JobHunter AI."""

from datetime import datetime, timezone, timedelta
from pathlib import Path
import sqlite3
from typing import Any
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.config import get_settings
from app.candidate_profile import load_profile, save_profile
from app.database import get_jobs, add_job
from app.applications import get_applications, update_status, get_offer, APPLICATION_STATUSES
from app.opportunities import list_opportunities
from app.application_router import recent_events
from app.ft_automation import send_single_france_travail_offer
from app.lbb_automation import send_single_lbb_opportunity
from app.matching import extract_cv_text, calculate_match_score
from app.france_travail import search_jobs, FranceTravailError

api_router = APIRouter(prefix="/api")


class StatusUpdateRequest(BaseModel):
    status: str
    personal_notes: str | None = None


class ProfileUpdateRequest(BaseModel):
    candidate: dict[str, Any]
    target_criteria: dict[str, Any] | None = None
    lbb_settings: dict[str, Any] | None = None


class AutopilotRequest(BaseModel):
    target: str = "france_travail"  # "france_travail" ou "lbb"
    max_applications: int = 5
    auto_send: bool = False  # False = mode vérification
    tone: str = "modern"  # "modern", "classic", "impact"


class SingleApplyRequest(BaseModel):
    source: str
    external_id: str
    auto_send: bool = False
    tone: str = "modern"


class GenerateLetterRequest(BaseModel):
    source: str
    external_id: str
    tone: str = "modern"  # "modern", "classic", "impact"
    mode: str = "auto"
    instructions: str = ""


class LeadHuntRequest(BaseModel):
    company: str
    title: str = "Data Scientist"
    description: str = ""


@api_router.get("/status")
def get_system_status():
    settings = get_settings()
    cv_exists = Path(settings.cv_path).is_file()
    ft_configured = bool(settings.france_travail_client_id and settings.france_travail_client_secret)
    cand = load_profile()
    llm_provider = settings.llm_provider or "local"
    llm_configured = bool(settings.llm_api_key and llm_provider.lower() not in ("", "local"))
    llm_model = settings.llm_model or ""

    from app.email.draft_service import get_gmail_status
    gmail_info = get_gmail_status(settings)

    return {
        "ok": True,
        "cv_ready": cv_exists,
        "cv_path": str(settings.cv_path),
        "cv_name": Path(settings.cv_path).name if cv_exists else None,
        "ft_configured": ft_configured,
        "llm_provider": llm_provider,
        "llm_configured": llm_configured,
        "llm_model": llm_model,
        "gmail_configured": gmail_info["configured"],
        "gmail_user": gmail_info["user"],
        "gmail_method": gmail_info["method"],
        "candidate_name": f"{cand.get('first_name', '')} {cand.get('last_name', '')}".strip() or "Candidat",
        "candidate_email": cand.get("email", ""),
        "candidate_phone": cand.get("phone", ""),
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


@api_router.get("/events")
def get_timeline_events(limit: int = 50):
    settings = get_settings()
    events = recent_events(settings.database_path, limit=limit)
    return events


@api_router.get("/jobs/search")
def search_offers(
    keyword: str = "Data Scientist",
    location: str | None = None,
    contract: str | None = None,
    source: str = "all",
    remote: str = "all",
    limit: int = 20,
):
    settings = get_settings()
    all_raw_jobs = []

    # 1. France Travail (si sélectionné)
    if source in ("all", "france_travail"):
        try:
            ft_jobs = search_jobs(
                keyword=keyword,
                location=location,
                contract_type=contract if contract != "Tous" else None,
                limit=limit,
            )
            all_raw_jobs.extend(ft_jobs)
        except Exception as exc:
            if source == "france_travail":
                raise HTTPException(status_code=502, detail=f"Erreur France Travail : {str(exc)}")

    # Calcul du match score
    scored_jobs = []
    cv_text = ""
    try:
        if Path(settings.cv_path).is_file():
            cv_text = extract_cv_text(settings.cv_path)
    except Exception:
        pass

    from app.email.message_builder import extract_contact_emails

    for job in all_raw_jobs:
        add_job(job, db_path=settings.database_path)
        score = calculate_match_score(cv_text, job.get("description", "")) if cv_text else None
        emails = extract_contact_emails(f"{job.get('title', '')} {job.get('description', '')}")
        contact_email = emails[0] if emails else (job.get("contact_email") or "")
        scored_jobs.append({**job, "match_score": score, "contact_emails": emails, "contact_email": contact_email})

    scored_jobs.sort(key=lambda x: (x.get("match_score") or 0), reverse=True)
    return scored_jobs[:limit]


@api_router.get("/profile")
def get_candidate_profile():
    return load_profile()


@api_router.post("/profile")
def update_candidate_profile(data: dict[str, Any]):
    current = load_profile()
    current.update(data)
    save_profile(current)
    return {"ok": True, "profile": current}


@api_router.post("/autopilot/run")
def trigger_autopilot(req: AutopilotRequest):
    raise HTTPException(status_code=410, detail="Autopilot d'envoi désactivé.")
    settings = get_settings()

    try:
        if req.target == "france_travail":
            from app.ft_automation import run_daily_ft_autopilot
            summary = run_daily_ft_autopilot(
                settings,
                max_applications=req.max_applications,
                dry_run=not req.auto_send,
                headless=False,
                tone=req.tone,
            )
        else:
            from app.lbb_automation import run_daily_autopilot
            summary = run_daily_autopilot(
                settings,
                max_applications=req.max_applications,
                dry_run=not req.auto_send,
                headless=False,
                tone=req.tone,
            )
        return {"ok": True, "summary": summary}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@api_router.post("/apply/single")
def apply_single(req: SingleApplyRequest):
    settings = get_settings()

    try:
        if req.source == "france_travail":
            from app.ft_automation import send_single_france_travail_offer
            res = send_single_france_travail_offer(
                req.external_id,
                settings,
                auto_send=req.auto_send,
                dry_run=not req.auto_send,
                headless=False,
                tone=req.tone,
            )
        else:
            from app.lbb_automation import send_single_lbb_opportunity
            res = send_single_lbb_opportunity(
                req.external_id,
                settings,
                auto_send=req.auto_send,
                headless=False,
                tone=req.tone,
            )
        return {"ok": True, "result": res}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@api_router.post("/letter/generate")
def generate_custom_letter(req: GenerateLetterRequest):
    settings = get_settings()
    job = get_offer(req.external_id, source=req.source, db_path=settings.database_path)
    if not job:
        raise HTTPException(status_code=404, detail=f"Offre {req.external_id} introuvable.")

    from app.application_generator import generate_preparation
    from app.applications import save_preparation

    draft = generate_preparation(
        job,
        settings,
        mode=req.mode,
        instructions=req.instructions,
        tone=req.tone,
    )
    save_preparation(
        req.external_id,
        draft["message"],
        draft["letter"],
        req.source,
        settings.database_path,
        generated=draft,
        ready=True,
    )
    return {"ok": True, "preparation": draft}


@api_router.get("/scheduler")
def get_scheduler():
    from app.scheduler import load_scheduler_config
    return load_scheduler_config()


@api_router.post("/scheduler")
def update_scheduler(data: dict[str, Any]):
    from app.scheduler import save_scheduler_config
    return save_scheduler_config(data)


@api_router.post("/scheduler/trigger")
async def trigger_scheduler_now():
    from app.scheduler import execute_scheduled_autopilot
    res = await execute_scheduled_autopilot()
    return {"ok": True, "result": res}


class GmailDispatchRequest(BaseModel):
    external_id: str
    source: str = "france_travail"
    recipient_email: str
    subject: str | None = None
    body: str | None = None
    action: str = "draft"  # "draft" ou "send"


@api_router.get("/gmail/status")
def get_gmail_status_route():
    from app.email.draft_service import get_gmail_status
    return get_gmail_status(get_settings())


@api_router.post("/gmail/dispatch")
def dispatch_gmail_route(req: GmailDispatchRequest):
    settings = get_settings()
    from app.email.draft_service import dispatch_gmail_application
    from app.applications import get_offer
    from app.email.message_builder import default_subject

    job = get_offer(req.external_id, req.source, settings.database_path)
    if not job:
        # Si non trouvé dans SQLite, fabriquer un job minimal
        job = {"external_id": req.external_id, "source": req.source, "title": "Offre d'emploi"}

    subject = (req.subject or "").strip() or job.get("email_subject") or default_subject(job)
    body = (req.body or "").strip() or job.get("application_letter") or job.get("application_message")
    if not body:
        raise HTTPException(
            status_code=400,
            detail="Le contenu du message de candidature est vide. Cliquez d'abord sur « Générer » pour préparer votre lettre.",
        )

    try:
        res = dispatch_gmail_application(
            external_id=req.external_id,
            source=req.source,
            recipient=req.recipient_email,
            subject=subject,
            body=body,
            settings=settings,
            action=req.action,
        )
        return res
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@api_router.post("/gmail/test")
def test_gmail_route():
    settings = get_settings()
    from app.email.draft_service import get_gmail_status
    st = get_gmail_status(settings)
    if not st["configured"]:
        raise HTTPException(
            status_code=400,
            detail="Gmail non configuré. Renseignez GMAIL_APP_PASSWORD dans .env (ou configurez Google OAuth).",
        )
    if settings.gmail_app_password:
        from app.email.app_password_client import AppPasswordGmail
        client = AppPasswordGmail(st["user"], settings.gmail_app_password, timeout=10)
        user = client.test_connection()
        return {"ok": True, "message": f"Connexion SMTP réussie pour {user} !"}
    elif settings.gmail_token_file.is_file():
        from app.email.gmail_client import GmailClient
        client = GmailClient(settings)
        user = client.connected_email()
        return {"ok": True, "message": f"Connexion OAuth réussie pour {user} !"}
    raise HTTPException(status_code=400, detail="Mode Gmail non reconnu.")


@api_router.post("/leads/hunt")
def hunt_decision_makers_and_recruiters(req: LeadHuntRequest):
    """Génère des requêtes et liens ciblés LinkedIn / X-Ray ainsi que des messages d'approche personnalisés."""
    from app.lead_hunter import generate_lead_outreach
    try:
        outreach = generate_lead_outreach(
            company=req.company,
            title=req.title,
            description=req.description,
        )
        return {"ok": True, "outreach": outreach}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Erreur chasseur de décideurs : {str(exc)}")


