"""Session existante facultative via CDP ; sinon ouverture dans le navigateur usuel."""

import os
import re
from urllib.parse import urlsplit
import webbrowser

from app.application_channels import safe_url
from app.email.message_builder import read_cv


def prefill_page(page, profile, message, cv_path):
    body = page.locator("body").inner_text(timeout=5000)
    if page.locator('input[type="password"]:visible').count() or re.search(
        r"captcha|verify you are human|connexion requise|connectez-vous|authentifiez-vous|access denied", body, re.I
    ):
        return "Connexion ou protection détectée : intervention manuelle requise. Aucun champ rempli."
    forms = page.locator("form:visible")
    if forms.count() != 1:
        return "Formulaire absent ou ambigu : ouvrez le formulaire de candidature puis intervenez manuellement."
    form = forms.first
    if not re.search(r"candidature|curriculum|\bcv\b|resume|apply|cover letter", form.inner_text(), re.I):
        return "Ce formulaire n'est pas identifié comme une candidature. Aucun champ rempli."
    mappings = {"first_name": r"^(prénom|prenom|first name)\s*\*?$", "last_name": r"^(nom|nom de famille|last name|surname)\s*\*?$",
                "email": r"^(e-?mail|adresse e-?mail|email address)\s*\*?$",
                "phone": r"^(téléphone|telephone|phone|phone number)\s*\*?$",
                "city": r"^(ville|city)\s*\*?$"}
    buttons = form.get_by_role("button", name=re.compile(r"candidater|postuler|envoyer|apply|submit", re.I))
    # Reconnaissance seulement : aucun clic de soumission.
    if buttons.count() != 1:
        return "Bouton de candidature absent ou ambigu : intervention manuelle requise."
    filled = []
    for key, pattern in mappings.items():
        field = form.get_by_label(re.compile(pattern, re.I))
        if profile.get(key) and field.count() == 1 and field.is_visible() and field.is_enabled() and not field.input_value():
            field.fill(profile[key], timeout=2000)
            filled.append(key)
    field = form.get_by_label(re.compile(r"^(message|message de candidature|cover letter)\s*\*?$", re.I))
    if message and field.count() == 1 and field.is_visible() and field.is_enabled() and not field.input_value():
        field.fill(message, timeout=2000); filled.append("message")
    upload = form.locator('input[type="file"]')
    if upload.count() == 1 and ("pdf" in (upload.get_attribute("accept") or "").lower() or
                              re.search(r"cv|resume|curriculum", upload.get_attribute("name") or "", re.I)):
        read_cv(cv_path)
        upload.set_input_files(str(cv_path), timeout=5000); filled.append("CV")
    return ("Champs préremplis : " + ", ".join(filled) + ". Relisez et confirmez vous-même dans le navigateur."
            if filled else "Formulaire complexe ou profil incomplet. Complétez la candidature manuellement.")


def prepare_browser_application(url, profile, message, cv_path, *, dry_run=False):
    if not safe_url(url):
        raise ValueError("Aucune URL de candidature valide.")
    if dry_run:
        return {"status": "MANUAL_REVIEW_REQUIRED", "url": url, "message": "Simulation : navigateur non ouvert, aucun fichier transmis."}
    from app.public_contacts import validate_public_target
    validate_public_target(url)
    cdp = os.getenv("BROWSER_CDP_URL", "").strip()
    if not cdp:
        opened = webbrowser.open(url, new=2)
        return {"status": "MANUAL_REVIEW_REQUIRED", "url": url,
                "message": "Page ouverte dans votre navigateur habituel. Connexion, remplissage et validation à effectuer manuellement."
                if opened else "Ouvrez le lien de candidature et complétez le formulaire manuellement."}
    parsed = urlsplit(cdp)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost") or parsed.username or parsed.password:
        raise ValueError("BROWSER_CDP_URL doit désigner une session locale existante.")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(cdp, timeout=5000, no_defaults=True)
            if not browser.contexts:
                raise ValueError("Aucune session navigateur utilisable.")
            page = browser.contexts[0].new_page()
            page.set_default_timeout(5000)
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            if urlsplit(page.url).hostname != urlsplit(url).hostname:
                detail = "Redirection vers un autre site ou une connexion : intervention requise avant tout remplissage."
            else:
                detail = prefill_page(page, profile, message, cv_path)
            return {"status": "MANUAL_REVIEW_REQUIRED", "url": safe_url(page.url) or url, "message": detail}
    except Exception:
        return {"status": "MANUAL_REVIEW_REQUIRED", "url": url,
                "message": "Préremplissage indisponible ou formulaire non pris en charge. Ouvrez le lien et intervenez manuellement."}
