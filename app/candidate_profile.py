"""Profil candidat local, modifiable et sans données personnelles inventées."""

from dataclasses import replace
import json
import os
import tempfile
from app.config import PROJECT_ROOT, project_path

FIELDS = ("first_name", "last_name", "email", "phone", "city", "linkedin_url", "portfolio_url", "cv_path")
PATH = PROJECT_ROOT / "candidate_profile.json"


def load_profile():
    result = dict.fromkeys(FIELDS, "")
    try:
        data = json.loads(PATH.read_text())
        if isinstance(data, dict):
            result.update({k: str(data[k] or "") for k in FIELDS if k in data})
    except (OSError, ValueError):
        pass
    return result


def effective_settings(settings):
    path = load_profile()["cv_path"]
    return replace(settings, cv_path=project_path(path)) if path else settings


def save_profile(data):
    from app.application_channels import safe_url, public_email
    values = {key: str(data.get(key) or "").strip() for key in FIELDS}
    if any(len(value) > 1000 for value in values.values()):
        raise ValueError("Un champ du profil est trop long.")
    if values["email"] and not public_email(values["email"]):
        raise ValueError("Adresse email candidat invalide.")
    if any(values[k] and not safe_url(values[k]) for k in ("linkedin_url", "portfolio_url")):
        raise ValueError("URL de profil invalide.")
    if values["cv_path"]:
        from app.email.message_builder import read_cv
        read_cv(project_path(values["cv_path"]))
    with tempfile.NamedTemporaryFile(mode="w", dir=PATH.parent, encoding="utf-8", delete=False) as file:
        os.chmod(file.name, 0o600)
        json.dump(values, file, ensure_ascii=False, indent=2)
    os.replace(file.name, PATH)
