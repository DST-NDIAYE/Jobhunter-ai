"""Message MIME local : un destinataire, le texte enregistré et le CV configuré."""

import base64
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path
import re

import pymupdf


def validate_email(value):
    value = (value or "").strip()
    if len(value) > 254 or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}", value):
        raise ValueError("Renseignez une adresse email de recruteur valide, sans nom ni plusieurs destinataires.")
    local, domain = value.rsplit("@", 1)
    if len(local) > 64 or local.startswith(".") or local.endswith(".") or ".." in value or any(
        label.startswith("-") or label.endswith("-") for label in domain.split(".")
    ):
        raise ValueError("L'adresse email du recruteur n'est pas valide.")
    return value


def default_subject(job):
    title = " ".join((job.get("title") or "poste proposé").split())
    from app.candidate_profile import load_profile
    profile = load_profile()
    name = " ".join(filter(None, [profile["first_name"], profile["last_name"]]))
    return f"Candidature – {title[:200]}" + (" – " + name if name else "")


def validate_subject(value):
    value = (value or "").strip()
    if not value or len(value) > 300 or any(ord(c) < 32 for c in value):
        raise ValueError("L'objet doit contenir 1 à 300 caractères, sur une seule ligne.")
    return value


def read_cv(path):
    path = Path(path)
    if path.suffix.lower() != ".pdf":
        raise ValueError("Le CV configuré doit être un fichier PDF.")
    try:
        with path.open("rb") as file:
            data = file.read(15 * 1024 * 1024 + 1)
        if len(data) > 15 * 1024 * 1024:
            raise ValueError("Le CV dépasse la limite de 15 Mo pour cette intégration.")
        if not data.startswith(b"%PDF-"):
            raise ValueError("Le fichier du CV n'est pas un PDF valide.")
        with pymupdf.open(stream=data, filetype="pdf") as pdf:
            if pdf.needs_pass or pdf.page_count < 1:
                raise ValueError("Le CV PDF est vide ou protégé par un mot de passe.")
    except OSError:
        raise ValueError("Le CV est absent ou illisible. Vérifiez CV_PATH dans .env.") from None
    except (pymupdf.FileDataError, pymupdf.EmptyFileError):
        raise ValueError("Le fichier du CV n'est pas un PDF lisible.") from None
    return path.name, data


def extract_contact_emails(text: str) -> list[str]:
    """Extrait les adresses email valides d'un texte d'annonce."""
    if not text:
        return []
    pattern = r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"
    matches = re.findall(pattern, text)
    excluded_domains = ("example.com", "exemple.fr", "francetravail.fr", "pole-emploi.fr")
    results = []
    for m in matches:
        m = m.strip(".,;:()<>[]\"' ")
        try:
            val = validate_email(m)
            domain = val.split("@")[-1].lower()
            if not any(ex in domain for ex in excluded_domains) and val not in results:
                results.append(val)
        except ValueError:
            continue
    return results


def build_email_object(*, recipient, subject, body, cv_path, sender) -> EmailMessage:
    if not isinstance(body, str) or not body.strip():
        raise ValueError("Préparez et enregistrez d'abord votre message de candidature.")
    message = EmailMessage(policy=SMTP)
    message["To"] = validate_email(recipient)
    message["From"] = validate_email(sender)
    message["Subject"] = validate_subject(subject)
    message.set_content(body)
    filename, data = read_cv(cv_path)
    message.add_attachment(data, maintype="application", subtype="pdf", filename=filename)
    return message


def build_message(*, recipient, subject, body, cv_path, sender):
    msg = build_email_object(recipient=recipient, subject=subject, body=body, cv_path=cv_path, sender=sender)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")

