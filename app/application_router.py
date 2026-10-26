"""Routeur d'envoi de candidatures : envoi automatique La Bonne Boîte, email Gmail ou préparation navigateur."""

from contextlib import closing
from datetime import datetime, timezone, timedelta
import sqlite3

from app.application_channels import detect_application_channel
from app.application_generator import generate_preparation
from app.applications import get_offer, save_preparation
from app.opportunities import entity_table
from app.candidate_profile import load_profile, effective_settings
from app.browser_automation.engine import prepare_browser_application
from app.email.draft_service import create_application_draft, send_application_email
from app.email.gmail_client import GmailError
from app.lbb_automation import LBBAutomationError, send_single_lbb_opportunity


def _event(db, job, channel, event, detail, url=None):
    db.execute(
        "INSERT INTO application_events(source,external_id,channel,event,detail,url) VALUES(?,?,?,?,?,?)",
        (job["source"], job["external_id"], channel, event, detail, url),
    )


def refresh_public_contact(job, settings):
    """Consultation nécessaire seulement, cache durable 24 h, choix manuel prioritaire."""
    if job.get("contact_email") or job.get("contact_email_manual") or not job.get("website") or job.get("application_url"):
        return job
    now = datetime.now(timezone.utc)
    try:
        checked = datetime.fromisoformat(job.get("contact_checked_at") or "")
        if checked.tzinfo and now - checked < timedelta(hours=24):
            return job
    except ValueError:
        pass
    from app.public_contacts import find_public_contact
    detail, candidates = "", []
    try:
        candidates = find_public_contact(job["website"])
        detail = f"{len(candidates)} adresse(s) publique(s) trouvée(s)."
        if len(candidates) > 1:
            detail += " Plusieurs contacts : sélection manuelle nécessaire sur le site."
    except (ValueError, OSError):
        detail = "Contact public indisponible ; utilisez le canal officiel ou vérifiez le site."
    with closing(sqlite3.connect(settings.database_path)) as db, db:
        db.execute(
            f"UPDATE {entity_table(job['source'])} SET contact_checked_at=? WHERE source=? AND external_id=?",
            (now.isoformat(), job["source"], job["external_id"]),
        )
        if len(candidates) == 1:
            db.execute(
                f"""UPDATE {entity_table(job['source'])} SET contact_email=?,contact_email_source=?
                WHERE source=? AND external_id=? AND contact_email='' AND contact_email_manual=0""",
                (candidates[0]["email"], candidates[0]["source_url"], job["source"], job["external_id"]),
            )
        _event(db, job, "UNKNOWN", "CONTACT_LOOKUP", detail, job["website"])
    return get_offer(job["external_id"], job["source"], settings.database_path)


def send_application(external_id, source, settings, *, dry_run=False, auto_send=True):
    settings = effective_settings(settings)
    job = get_offer(external_id, source, settings.database_path)
    if not job:
        raise ValueError("Candidature introuvable.")
    detected = detect_application_channel(job)
    if dry_run:
        return {**detected, "status": "SIMULATION", "message": "Simulation : aucun réseau, aucun brouillon créé, aucune transmission."}

    # Auto-préparation si le message/lettre n'a pas encore été généré !
    if not (job.get("application_message") or "").strip():
        draft = generate_preparation(job, settings, mode="auto")
        save_preparation(external_id, draft["message"], draft["letter"], source, settings.database_path, generated=draft, ready=True)
        job = get_offer(external_id, source, settings.database_path)

    job = refresh_public_contact(job, settings)
    detected = detect_application_channel(job)
    channel, url = detected["channel"], detected.get("url")
    now = datetime.now(timezone.utc).isoformat()
    table = entity_table(source)

    # Cas 1 : Candidature spontanée directe sur La Bonne Boîte
    has_direct_lbb = bool(
        job.get("application_url") and "candidature/nouvelle" in str(job.get("application_url"))
    )
    if (source == "labonneboite" or channel == "LABONNEBOITE") and has_direct_lbb:
        try:
            return send_single_lbb_opportunity(external_id, settings, auto_send=auto_send)
        except (LBBAutomationError, ValueError, OSError) as exc:
            msg = str(exc)
            with closing(sqlite3.connect(settings.database_path)) as db, db:
                db.execute(
                    f"UPDATE {table} SET application_status='Échec',status_updated_at=?,application_error=? WHERE source=? AND external_id=?",
                    (now, msg, source, external_id),
                )
                _event(db, job, "LABONNEBOITE", "ERROR", msg, url)
            return {"status": "ERROR", "channel": "LABONNEBOITE", "url": url, "message": msg}

    # Cas 2 : Offre d'emploi France Travail
    is_france_travail = (
        source == "france_travail"
        or channel == "FRANCE_TRAVAIL"
        or (url and "candidat.francetravail.fr" in url)
    )
    if is_france_travail and channel != "EMAIL":
        try:
            from app.ft_automation import send_single_france_travail_offer
            return send_single_france_travail_offer(external_id, settings, auto_send=auto_send, dry_run=dry_run)
        except Exception as exc:
            msg = str(exc)
            with closing(sqlite3.connect(settings.database_path)) as db, db:
                db.execute(
                    f"UPDATE {table} SET application_status='Échec',status_updated_at=?,application_error=? WHERE source=? AND external_id=?",
                    (now, msg, source, external_id),
                )
                _event(db, job, "FRANCE_TRAVAIL", "ERROR", msg, url)
            return {"status": "ERROR", "channel": "FRANCE_TRAVAIL", "url": url, "message": msg}

    with closing(sqlite3.connect(settings.database_path)) as db, db:
        db.execute(
            f"UPDATE {table} SET application_channel=?,application_attempted_at=?,application_error=NULL,application_url=COALESCE(?,application_url) WHERE source=? AND external_id=?",
            (channel, now, url, source, external_id),
        )
        _event(db, job, channel, "CHANNEL_DETECTED", detected["reason"], url)
        _event(db, job, channel, "ATTEMPT", "Traitement du canal de candidature.", url)

    try:
        if channel == "EMAIL":
            with closing(sqlite3.connect(settings.database_path)) as db, db:
                db.execute(f"UPDATE {table} SET application_status='Prête à envoyer' WHERE source=? AND external_id=?", (source, external_id))
            if not job.get("gmail_draft_id"):
                create_application_draft(external_id, source, settings)
            result = {
                "status": "DRAFT_CREATED",
                "url": "https://mail.google.com/mail/u/0/#drafts",
                "message": "Brouillon Gmail créé avec votre CV.",
            }
            status, error = "Intervention requise", None
        elif url:
            result = prepare_browser_application(url, load_profile(), job["application_message"], settings.cv_path)
            status, error = "Intervention requise", None
        else:
            result = {
                "status": "MANUAL_REVIEW_REQUIRED",
                "url": None,
                "message": "Aucun canal exploitable. Renseignez une adresse publiée dans la fiche ou consultez le site officiel.",
            }
            status, error = "Intervention requise", None
    except (ValueError, OSError, GmailError) as exc:
        result = {
            "status": "ERROR",
            "url": url,
            "message": str(exc) if isinstance(exc, (ValueError, GmailError)) else "Canal indisponible. Réessayez après vérification de la configuration.",
        }
        status, error = "Échec", result["message"]
    with closing(sqlite3.connect(settings.database_path)) as db, db:
        db.execute(
            f"""UPDATE {table} SET
                application_status=?,
                status_updated_at=?,
                application_sent_at=CASE WHEN ?='Candidature envoyée' THEN COALESCE(application_sent_at, ?) ELSE application_sent_at END,
                application_error=?
                WHERE source=? AND external_id=?""",
            (status, now, status, now, error, source, external_id),
        )
        _event(db, job, channel, result["status"], result["message"], result.get("url"))
    return {**result, "channel": channel}


def recent_events(db_path, limit=100):
    with closing(sqlite3.connect(db_path)) as db:
        db.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in db.execute(
                "SELECT created_at,source,external_id,channel,event,detail,url FROM application_events ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        ]
