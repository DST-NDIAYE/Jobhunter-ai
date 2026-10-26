"""Détection pure, sans réseau ni adresse inventée."""

from urllib.parse import urlsplit, unquote
from app.email.message_builder import validate_email

ATS_HOSTS = ("myworkdayjobs.com", "greenhouse.io", "lever.co", "smartrecruiters.com",
             "teamtailor.com", "recruitee.com", "workable.com", "icims.com", "successfactors.com")
LABELS = {"EMAIL": "Email", "LABONNEBOITE": "La Bonne Boîte", "FRANCE_TRAVAIL": "France Travail",
          "COMPANY_SITE": "Site carrière", "ATS": "ATS", "MANUAL": "Manuel", "UNKNOWN": "À déterminer"}


def safe_url(value):
    if not isinstance(value, str):
        return None
    try:
        p = urlsplit(value.strip())
        if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password or any(ord(c) < 32 for c in value):
            return None
        return value.strip()
    except ValueError:
        return None


def public_email(value):
    try:
        if isinstance(value, str) and value.lower().startswith("mailto:"):
            value = unquote(value[7:].split("?", 1)[0])
        return validate_email(value)
    except (ValueError, TypeError, AttributeError):
        return ""


def url_channel(url, fallback="MANUAL"):
    host = (urlsplit(url).hostname or "").lower()
    if host == "labonneboite.francetravail.fr":
        return "LABONNEBOITE"
    if host == "francetravail.fr" or host.endswith(".francetravail.fr"):
        return "FRANCE_TRAVAIL"
    if any(host == name or host.endswith("." + name) for name in ATS_HOSTS):
        return "ATS"
    return fallback


def detect_application_channel(item):
    email = public_email(item.get("contact_email"))
    if item.get("contact_email_manual"):
        if email:
            return {"channel": "EMAIL", "email": email, "url": None, "reason": "Adresse renseignée manuellement"}
    explicit = safe_url(item.get("application_url"))
    if explicit:
        return {"channel": url_channel(explicit, "COMPANY_SITE"), "email": email, "url": explicit,
                "reason": "URL de candidature fournie"}
    if email:
        return {"channel": "EMAIL", "email": email, "url": None, "reason": "Adresse publiée ou renseignée"}
    origin = safe_url(item.get("source_url") or item.get("url"))
    if origin and url_channel(origin) in ("LABONNEBOITE", "FRANCE_TRAVAIL", "ATS"):
        return {"channel": url_channel(origin), "email": "", "url": origin, "reason": "Lien fourni par la source"}
    website = safe_url(item.get("website"))
    if website:
        return {"channel": url_channel(website, "COMPANY_SITE"), "email": "", "url": website, "reason": "Site officiel fourni"}
    if origin:
        return {"channel": "MANUAL", "email": "", "url": origin, "reason": "Page source à vérifier"}
    return {"channel": "UNKNOWN", "email": "", "url": None, "reason": "Aucune coordonnée exploitable"}
