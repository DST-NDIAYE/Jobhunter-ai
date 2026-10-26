"""Coordination SQLite/Gmail : pas de génération, pas d'envoi, pas de retry implicite."""

from contextlib import closing
from datetime import datetime, timezone
import logging
import sqlite3

from app.applications import get_offer
from app.opportunities import entity_table
from app.email.gmail_client import GmailClient, GmailError
from app.email.message_builder import build_message, default_subject, validate_email, validate_subject

LOGGER = logging.getLogger(__name__)
PENDING_MESSAGE = "La création précédente est en cours ou son résultat est incertain. Vérifiez les brouillons dans Gmail avant toute nouvelle tentative."


def email_status(job):
    if job.get("gmail_draft_id"):
        return "DRAFT_CREATED"
    if job.get("gmail_draft_pending") or job.get("send_error"):
        return "ERROR"
    if job.get("application_status") == "Prête à envoyer" and job.get("contact_email") and (job.get("application_message") or "").strip():
        return "READY"
    return "NOT_READY"


def save_email_details(job, recipient, subject, settings):
    source = job["source"]
    recipient = validate_email(recipient) if recipient.strip() else ""
    subject = validate_subject(subject)
    with closing(sqlite3.connect(settings.database_path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        current = get_offer(job["external_id"], job["source"], settings.database_path)
        if not current:
            raise ValueError("Candidature introuvable.")
        error = current.get("send_error") if current.get("gmail_draft_pending") else None
        status = email_status({**current, "contact_email": recipient, "send_error": error})
        db.execute(f"""UPDATE {entity_table(source)} SET contact_email=?, email_subject=?, application_channel=?, contact_email_manual=1, contact_email_source='manual',
                      email_status=?, send_error=? WHERE source=? AND external_id=?""",
                   (recipient, subject, "EMAIL" if recipient else "UNKNOWN", status, error, job["source"], job["external_id"]))


def _guard(job):
    if job is None:
        raise ValueError("Candidature introuvable.")
    if job.get("gmail_draft_id"):
        raise ValueError("Un brouillon Gmail existe déjà pour cette candidature.")
    if job.get("gmail_draft_pending"):
        raise ValueError(PENDING_MESSAGE)


def create_application_draft(external_id, source, settings):
    job = get_offer(external_id, source, settings.database_path)
    _guard(job)
    reserved = False
    remote_created = False
    try:
        client = GmailClient(settings)
        account = client.connected_email()
        if not (job.get("application_message") or "").strip():
            raise ValueError("Préparez et enregistrez d'abord votre message de candidature.")
        if job.get("application_status") != "Prête à envoyer":
            raise ValueError("Marquez d'abord cette candidature comme Prête à envoyer.")
        subject = job.get("email_subject") or default_subject(job)
        raw = build_message(recipient=job.get("contact_email"), subject=subject,
                            body=job["application_message"], cv_path=settings.cv_path, sender=account)
        # Réservation durable, y compris entre deux onglets/processus ou après un crash.
        with closing(sqlite3.connect(settings.database_path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            current = get_offer(external_id, source, settings.database_path)
            _guard(current)
            if any(current.get(k) != job.get(k) for k in
                   ("application_status", "application_message", "contact_email", "email_subject")):
                raise ValueError("La candidature a changé. Actualisez-la avant de créer le brouillon.")
            db.execute(f"""UPDATE {entity_table(source)} SET gmail_draft_pending=1, email_status='READY', send_error=NULL,
                          email_subject=? WHERE source=? AND external_id=?""", (subject, source, external_id))
        reserved = True
        draft_id, message_id = client.create_draft(raw)
        remote_created = True
        with closing(sqlite3.connect(settings.database_path)) as db, db:
            db.execute(f"""UPDATE {entity_table(source)} SET gmail_draft_id=?, gmail_message_id=?, draft_created_at=?,
                          gmail_account_email=?, email_status='DRAFT_CREATED', send_error=NULL,
                          gmail_draft_pending=0 WHERE source=? AND external_id=?""",
                       (draft_id, message_id, datetime.now(timezone.utc).isoformat(), account, source, external_id))
        return draft_id
    except (GmailError, ValueError, sqlite3.Error, OSError) as exc:
        uncertain = remote_created or (reserved and (not isinstance(exc, GmailError) or exc.uncertain))
        message = str(exc) if isinstance(exc, (GmailError, ValueError)) else "Impossible d'enregistrer le résultat du brouillon dans SQLite."
        if uncertain:
            message = PENDING_MESSAGE
        LOGGER.warning("Gmail draft: %s; reserved=%s; uncertain=%s", type(exc).__name__, reserved, uncertain)
        try:
            with closing(sqlite3.connect(settings.database_path)) as db, db:
                # Une opération concurrente réussie ne doit jamais être remplacée par une erreur.
                db.execute(f"""UPDATE {entity_table(source)} SET email_status='ERROR', send_error=?,
                              gmail_draft_pending=CASE WHEN ? THEN ? ELSE gmail_draft_pending END
                              WHERE source=? AND external_id=? AND gmail_draft_id IS NULL""",
                           (message, int(reserved), int(uncertain), source, external_id))
        except sqlite3.Error:
            LOGGER.warning("Gmail draft: SQLite result unavailable")
        raise GmailError(message, uncertain=uncertain) from None


def send_application_email(external_id, source, settings):
    """Envoie directement l'email de candidature avec le CV en pièce jointe via l'API Gmail."""
    job = get_offer(external_id, source, settings.database_path)
    if job is None:
        raise ValueError("Candidature introuvable.")
    if job.get("application_status") == "Candidature envoyée":
        raise ValueError("Cette candidature a déjà été envoyée.")
    client = GmailClient(settings)
    account = client.connected_email()
    if not (job.get("application_message") or "").strip():
        raise ValueError("Préparez et enregistrez d'abord votre message de candidature.")
    subject = job.get("email_subject") or default_subject(job)
    raw = build_message(
        recipient=job.get("contact_email"),
        subject=subject,
        body=job["application_message"],
        cv_path=settings.cv_path,
        sender=account,
    )
    message_id = client.send_message(raw)
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    with closing(sqlite3.connect(settings.database_path)) as db, db:
        db.execute(
            f"""UPDATE {entity_table(source)} SET
                gmail_message_id=?,
                gmail_account_email=?,
                email_status='SENT',
                application_status='Candidature envoyée',
                status_updated_at=?,
                application_sent_at=COALESCE(application_sent_at, ?),
                send_error=NULL,
                gmail_draft_pending=0
                WHERE source=? AND external_id=?""",
            (message_id, account, now, now, source, external_id),
        )
    return message_id


def get_gmail_status(settings):
    from app.candidate_profile import load_profile
    profile = load_profile()
    candidate_email = profile.get("email", "").strip()
    user = settings.gmail_user or candidate_email
    has_app_pwd = bool(settings.gmail_app_password)
    has_oauth_token = settings.gmail_token_file.is_file()
    has_oauth_secret = settings.gmail_client_secret_file.is_file()

    configured = has_app_pwd or has_oauth_token
    method = "app_password" if has_app_pwd else ("oauth" if has_oauth_token else ("oauth_ready" if has_oauth_secret else "none"))

    return {
        "configured": configured,
        "method": method,
        "user": user,
    }


def dispatch_gmail_application(*, external_id: str, source: str, recipient: str, subject: str, body: str, settings, action: str = "draft"):
    """Crée un brouillon ou envoie directement l'email de candidature avec le CV en pièce jointe."""
    from app.email.app_password_client import AppPasswordGmail
    from app.email.message_builder import build_email_object, build_message, validate_email, validate_subject
    from app.candidate_profile import load_profile

    recipient = validate_email(recipient)
    subject = validate_subject(subject)
    if not body or not body.strip():
        raise ValueError("Le corps du message de candidature ne peut pas être vide.")

    profile = load_profile()
    sender = settings.gmail_user or profile.get("email") or "candidat@gmail.com"

    # Construction du message MIME avec le CV PDF attaché
    email_obj = build_email_object(
        recipient=recipient,
        subject=subject,
        body=body,
        cv_path=settings.cv_path,
        sender=sender,
    )

    now = datetime.now(timezone.utc).isoformat()
    db_path = settings.database_path

    # Cas 1 : Mot de passe d'application Google (App Password)
    if settings.gmail_app_password:
        client = AppPasswordGmail(sender, settings.gmail_app_password, timeout=settings.http_timeout)
        if action == "draft":
            draft_id = client.create_draft(email_obj)
            with closing(sqlite3.connect(db_path)) as db, db:
                db.execute(
                    f"""UPDATE {entity_table(source)} SET
                        contact_email=?, email_subject=?, application_message=?,
                        gmail_draft_id=?, draft_created_at=?, gmail_account_email=?,
                        application_channel='EMAIL', email_status='DRAFT_CREATED',
                        status_updated_at=?, send_error=NULL
                        WHERE source=? AND external_id=?""",
                    (recipient, subject, body, draft_id, now, sender, now, source, external_id),
                )
            return {"ok": True, "action": "draft", "id": draft_id, "recipient": recipient, "message": "Brouillon créé avec succès dans votre boîte Gmail (avec CV attaché)."}
        else:
            msg_id = client.send_email(email_obj)
            with closing(sqlite3.connect(db_path)) as db, db:
                db.execute(
                    f"""UPDATE {entity_table(source)} SET
                        contact_email=?, email_subject=?, application_message=?,
                        gmail_message_id=?, application_sent_at=?, status_updated_at=?,
                        application_channel='EMAIL', email_status='SENT',
                        application_status='Candidature envoyée', send_error=NULL
                        WHERE source=? AND external_id=?""",
                    (recipient, subject, body, msg_id, now, now, source, external_id),
                )
            return {"ok": True, "action": "send", "id": msg_id, "recipient": recipient, "message": f"Candidature envoyée par email à {recipient} avec succès (CV attaché)."}

    # Cas 2 : OAuth officiel Google
    elif settings.gmail_token_file.is_file():
        client = GmailClient(settings)
        raw = build_message(recipient=recipient, subject=subject, body=body, cv_path=settings.cv_path, sender=sender)
        if action == "draft":
            draft_id, message_id = client.create_draft(raw)
            with closing(sqlite3.connect(db_path)) as db, db:
                db.execute(
                    f"""UPDATE {entity_table(source)} SET
                        contact_email=?, email_subject=?, application_message=?,
                        gmail_draft_id=?, gmail_message_id=?, draft_created_at=?,
                        gmail_account_email=?, application_channel='EMAIL',
                        email_status='DRAFT_CREATED', status_updated_at=?, send_error=NULL
                        WHERE source=? AND external_id=?""",
                    (recipient, subject, body, draft_id, message_id, now, sender, now, source, external_id),
                )
            return {"ok": True, "action": "draft", "id": draft_id, "recipient": recipient, "message": "Brouillon Gmail créé avec succès via l'API Google."}
        else:
            msg_id = client.send_message(raw)
            with closing(sqlite3.connect(db_path)) as db, db:
                db.execute(
                    f"""UPDATE {entity_table(source)} SET
                        contact_email=?, email_subject=?, application_message=?,
                        gmail_message_id=?, application_sent_at=?, status_updated_at=?,
                        application_channel='EMAIL', email_status='SENT',
                        application_status='Candidature envoyée', send_error=NULL
                        WHERE source=? AND external_id=?""",
                    (recipient, subject, body, msg_id, now, now, source, external_id),
                )
            return {"ok": True, "action": "send", "id": msg_id, "recipient": recipient, "message": f"Candidature envoyée par email à {recipient} via l'API Google."}
    else:
        raise ValueError("Aucun accès Gmail configuré. Ajoutez votre mot de passe d'application Google dans .env (GMAIL_APP_PASSWORD) ou dans l'onglet Paramètres.")

