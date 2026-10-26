"""Automatisation complète des candidatures aux offres d'emploi France Travail (candidat.francetravail.fr)."""

from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import quote, urlsplit

from app.application_generator import generate_preparation, sanitize_lbb_message
from app.applications import get_offer, save_preparation
from app.browser_automation.engine import prefill_page
from app.candidate_profile import effective_settings, load_profile
from app.config import PROJECT_ROOT
from app.lbb_automation import (
    BROWSER_PROFILE_DIR,
    dismiss_cookie_banner,
    open_lbb_browser,
    _log_event,
)
from app.opportunities import entity_table

FT_CANDIDAT_BASE = "https://candidat.francetravail.fr"


class FTAutomationError(Exception):
    """Erreur explicite lors de l'automatisation France Travail."""


class FTOfferExpiredError(FTAutomationError):
    """L'offre France Travail n'est plus en ligne ou a été pourvue."""


def ensure_ft_authenticated(page, *, login_timeout_sec: int = 240, on_status=None, target_url: str | None = None) -> dict:
    """Vérifie si la session France Travail candidat est active ; attend la connexion sinon."""
    initial_url = target_url or f"{FT_CANDIDAT_BASE}/espacecandidat/"
    try:
        if not (page.url or "").startswith(FT_CANDIDAT_BASE):
            page.goto(initial_url, wait_until="domcontentloaded", timeout=25000)
            time.sleep(1.8)
    except Exception:
        pass

    dismiss_cookie_banner(page)

    def is_logged_in(p) -> dict | None:
        try:
            url = p.url or ""
            if "/connexion" in url or "/authentification" in url or "/auth-oidc" in url:
                return None
            res = p.evaluate("""() => {
                const logout = document.querySelector('a[href*="deconnexion"], a[href*="logout"], #header-profil, .btn-deconnexion, .nom-candidat');
                const userText = (document.body && document.body.innerText) || '';
                const hasEspace = userText.includes('Mon espace') || userText.includes('Mes candidatures') || userText.includes('Mon profil');
                let name = '';
                if (logout) {
                    name = (logout.textContent || '').trim();
                }
                if (logout || hasEspace) {
                    return { ok: true, name: name || 'Connecté' };
                }
                return { ok: false };
            }""")
            if isinstance(res, dict) and res.get("ok"):
                return {"name": res.get("name") or "Candidat"}
        except Exception:
            pass
        return None

    user = is_logged_in(page)
    if user:
        return user

    if on_status:
        on_status("Connexion à votre espace France Travail en cours dans la fenêtre Google Chrome…")

    deadline = time.time() + login_timeout_sec
    while time.time() < deadline:
        time.sleep(1.2)
        try:
            for p in page.context.pages:
                if is_logged_in(p):
                    return {"name": "Connecté"}
        except Exception:
            pass

    raise FTAutomationError(
        "Délai de connexion France Travail dépassé. Connectez-vous à votre espace candidat dans Chrome puis réessayez."
    )


def submit_ft_offer_on_page(
    page,
    *,
    offer_url: str,
    external_id: str,
    title: str = "",
    company: str = "",
    message: str = "",
    cv_path: Path,
    auto_send: bool = True,
    dry_run: bool = False,
    on_status=None,
) -> dict:
    """Ouvre l'offre France Travail, clique sur Postuler, sélectionne le CV, colle la lettre et va jusqu'à l'envoi."""
    clean_id = str(external_id or "").strip("'\" \t\n")
    if not offer_url:
        offer_url = f"{FT_CANDIDAT_BASE}/offres/recherche/detail/{clean_id}"

    if on_status:
        on_status(f"Chargement de l'offre France Travail {clean_id}…")

    page.goto(offer_url, wait_until="domcontentloaded", timeout=25000)
    time.sleep(1.5)
    dismiss_cookie_banner(page)

    # 1. Détection offre expirée ou retirée
    body_text = page.locator("body").inner_text(timeout=5000) if page.locator("body").count() else ""
    if re.search(r"cette offre n'est plus disponible|offre expirée|offre pourvue|introuvable", body_text, re.I):
        raise FTOfferExpiredError(f"L'offre France Travail {clean_id} n'est plus disponible ou a été pourvue.")

    # 2. Détection candidature déjà envoyée
    if re.search(r"vous avez déjà postulé|candidature déjà transmise", body_text, re.I):
        return {
            "status": "SENT",
            "url": page.url,
            "message": f"Vous avez déjà postulé à cette offre sur France Travail ({clean_id}).",
        }

    # 3. Localiser le bouton « Postuler » sur la page France Travail
    postuler_selectors = [
        "#modal-postuler",
        "a[data-target='#modal-postuler']",
        "button[data-target='#modal-postuler']",
        "a.btn-primary:has-text('Postuler')",
        "button.btn-primary:has-text('Postuler')",
        "a:has-text('Postuler')",
        "button:has-text('Postuler')",
        "a:has-text('Postulez')",
        "button:has-text('Postulez')",
        ".btn-postuler",
        "#btn-postuler",
    ]

    postuler_btn = None
    for sel in postuler_selectors:
        loc = page.locator(sel)
        if loc.count() > 0 and loc.first.is_visible():
            postuler_btn = loc.first
            break

    if not postuler_btn:
        # Vérifier si l'offre demande un contact direct par courriel
        for candidate in re.findall(r"[\w.!#$%&'*+/=?^`{|}~-]+@[\w.-]+\.[A-Za-z]{2,}", body_text):
            if not any(ign in candidate.lower() for ign in ("noreply", "no-reply", "francetravail", "pole-emploi")):
                return {
                    "status": "MANUAL_REVIEW_REQUIRED",
                    "url": page.url,
                    "message": f"Cette offre demande un contact direct par courriel ({candidate}). Utilisez le canal Email.",
                }
        raise FTAutomationError(
            f"Bouton « Postuler » introuvable sur cette offre France Travail ({clean_id}). Vérifiez si l'offre est toujours ouverte."
        )

    if on_status:
        on_status("Clic sur « Postuler »…")

    postuler_btn.scroll_into_view_if_needed()
    time.sleep(0.5)

    # Récupérer l'URL initiale avant clic pour détecter redirection externe
    initial_url = page.url
    postuler_btn.click()
    time.sleep(2.0)
    dismiss_cookie_banner(page)

    # 4. Si redirection vers une authentification France Travail SSO
    current_url = page.url or ""
    if "/connexion" in current_url or "/authentification" in current_url or "/auth-oidc" in current_url:
        ensure_ft_authenticated(page, login_timeout_sec=180, on_status=on_status, target_url=current_url)
        # Retour sur l'offre après authentification
        page.goto(offer_url, wait_until="domcontentloaded", timeout=25000)
        time.sleep(1.5)
        dismiss_cookie_banner(page)
        # Re-clic sur postuler
        for sel in postuler_selectors:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.click()
                time.sleep(2.0)
                break

    # 5. Détecter si redirection vers un site partenaire ou ATS externe
    all_pages = page.context.pages
    active_page = page
    if len(all_pages) > 1:
        for p in all_pages:
            if p != page and urlsplit(p.url).hostname != urlsplit(FT_CANDIDAT_BASE).hostname:
                active_page = p
                break

    if urlsplit(active_page.url).hostname not in ("candidat.francetravail.fr", "www.francetravail.fr", "francetravail.fr"):
        # C'est un site partenaire ou carrière de l'entreprise
        prof = load_profile()
        detail = prefill_page(active_page, prof, message, cv_path)
        return {
            "status": "PREPARATION_READY",
            "url": active_page.url,
            "message": f"Redirigé vers le site du recruteur/partenaire ({urlsplit(active_page.url).hostname}). {detail}",
        }

    # 6. Candidature intégrée France Travail : modal ou formulaire
    clean_msg = sanitize_lbb_message(message, max_len=1480)
    cv_stem = Path(cv_path).stem

    if on_status:
        on_status("Sélection de votre CV et remplissage de la lettre personnalisée…")

    # A) Sélection du CV enregistré ou téléversement
    cv_selected = page.evaluate(
        """(targetStem) => {
            // Radio buttons ou checkboxes de sélection de CV
            const inputs = Array.from(document.querySelectorAll(
                'input[type="radio"][name*="cv" i], input[type="radio"][id*="cv" i], ' +
                'input[type="checkbox"][id*="cv" i], .modal input[type="radio"]'
            ));
            if (inputs.length > 0) {
                let chosen = inputs[0];
                for (const inp of inputs) {
                    const container = inp.closest('label, div, li') || inp.parentElement;
                    const text = (container && container.textContent) || '';
                    if (text.includes(targetStem) || text.includes('Serigne') || text.includes('NDIAYE')) {
                        chosen = inp;
                        break;
                    }
                }
                chosen.checked = true;
                chosen.click();
                chosen.dispatchEvent(new Event('input', { bubbles: true }));
                chosen.dispatchEvent(new Event('change', { bubbles: true }));
                return true;
            }
            return false;
        }""",
        cv_stem,
    )

    if not cv_selected:
        file_input = page.locator("input[type='file']")
        if file_input.count() > 0:
            try:
                file_input.first.set_input_files(str(cv_path))
                time.sleep(1.5)
            except Exception:
                pass

    # B) Remplissage de la lettre de motivation
    textarea = page.locator(
        "textarea#message, textarea#lettre, textarea[name*='message' i], textarea[name*='lettre' i], "
        ".modal textarea, textarea"
    )
    if textarea.count() > 0 and textarea.first.is_visible():
        try:
            textarea.first.click()
            textarea.first.fill(clean_msg)
        except Exception:
            pass
        page.evaluate(
            """(val) => {
                const ta = document.querySelector("textarea#message, textarea#lettre, textarea[name*='message' i], .modal textarea, textarea");
                if (ta) {
                    ta.value = val;
                    ta.dispatchEvent(new Event('input', { bubbles: true }));
                    ta.dispatchEvent(new Event('change', { bubbles: true }));
                    ta.dispatchEvent(new Event('blur', { bubbles: true }));
                }
            }""",
            clean_msg,
        )

    # C) Validation des cases de consentement si présentes
    page.evaluate("""() => {
        const cbs = document.querySelectorAll(
            '#modal-postuler input[type="checkbox"]:not(:checked), .modal input[type="checkbox"]:not(:checked)'
        );
        for (const cb of cbs) {
            cb.checked = true;
            cb.dispatchEvent(new Event('change', { bubbles: true }));
        }
    }""")
    time.sleep(0.8)

    # 7. Mode test ou vérification (arrêter juste avant l'envoi pour que l'utilisateur relise)
    if dry_run or not auto_send:
        return {
            "status": "PREPARATION_READY",
            "url": page.url,
            "message": f"Offre France Travail {clean_id} ouverte dans Chrome avec votre CV coché et la lettre rédigée. Vous pouvez relire et valider d'un clic !",
        }

    # 8. Envoi automatique jusqu'à la confirmation
    if on_status:
        on_status("Validation et envoi de la candidature…")

    send_btn = page.locator(
        "button:has-text('Valider ma candidature'), button:has-text('Envoyer ma candidature'), "
        "button:has-text('Confirmer l\\'envoi'), button:has-text('Envoyer'), "
        "button[type='submit']:has-text('Valider'), button[type='submit']:has-text('Envoyer'), "
        "#btn-valider-postuler, .modal button.btn-primary"
    )

    if send_btn.count() > 0 and not send_btn.first.is_disabled():
        send_btn.first.scroll_into_view_if_needed()
        time.sleep(0.4)
        send_btn.first.click()
        time.sleep(2.0)

    # Vérifier si confirmation intermédiaire
    confirm_btn = page.locator(
        "button:has-text('Confirmer'), button:has-text('Oui, envoyer'), button:has-text('Finaliser')"
    )
    if confirm_btn.count() > 0 and confirm_btn.first.is_visible() and not confirm_btn.first.is_disabled():
        confirm_btn.first.click()
        time.sleep(2.0)

    # 9. Détection de la confirmation de transmission
    body_after = page.locator("body").inner_text(timeout=5000) if page.locator("body").count() else ""
    is_sent = bool(re.search(
        r"candidature\s+(?:a\s+)?(?:bien\s+)?(?:été\s+)?(?:transmise|envoyée|enregistrée|prise\s+en\s+compte)|"
        r"candidature\s+(?:transmise|envoyée)|"
        r"votre\s+message\s+a\s+été\s+envoyé|"
        r"merci\s+pour\s+votre\s+candidature",
        body_after,
        re.I,
    ))

    if is_sent:
        return {
            "status": "SENT",
            "url": page.url,
            "message": f"Candidature France Travail envoyée avec succès pour « {title or clean_id} » ({company or ''}) !",
        }

    return {
        "status": "PREPARATION_READY",
        "url": page.url,
        "message": f"Offre ouverte et formulaire rempli dans Chrome. Finalisez l'envoi en cliquant sur Valider.",
    }


def send_single_france_travail_offer(
    external_id: str,
    settings,
    *,
    auto_send: bool = True,
    dry_run: bool = False,
    headless: bool = False,
    tone: str = "modern",
    on_status=None,
) -> dict:
    """Envoie ou prépare une candidature unitaire France Travail et met à jour SQLite."""
    settings = effective_settings(settings)
    clean_id = str(external_id or "").strip("'\" \t\n")
    job = get_offer(clean_id, "france_travail", settings.database_path)
    if not job:
        # Tenter avec l'ID brut si quotes
        job = get_offer(external_id, "france_travail", settings.database_path)
    if not job:
        raise ValueError(f"Offre France Travail {clean_id} introuvable dans SQLite.")

    # Préparation automatique de la lettre si manquante ou si changement de ton demandé
    if not (job.get("application_message") or "").strip():
        draft = generate_preparation(job, settings, mode="auto", tone=tone)
        save_preparation(
            job["external_id"],
            draft["message"],
            draft["letter"],
            "france_travail",
            settings.database_path,
            generated=draft,
            ready=True,
        )
        job = get_offer(job["external_id"], "france_travail", settings.database_path)

    offer_url = job.get("application_url") or job.get("url") or f"{FT_CANDIDAT_BASE}/offres/recherche/detail/{clean_id}"
    message = job.get("application_letter") or job.get("application_message") or ""

    with open_lbb_browser(headless=headless) as context:
        page = context.pages[0] if context.pages else context.new_page()
        result = submit_ft_offer_on_page(
            page,
            offer_url=offer_url,
            external_id=clean_id,
            title=job.get("title") or "",
            company=job.get("company") or "",
            message=message,
            cv_path=settings.cv_path,
            auto_send=auto_send,
            dry_run=dry_run,
            on_status=on_status,
        )

    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    table = entity_table("france_travail")
    new_status = "Candidature envoyée" if result["status"] == "SENT" else "Prête à envoyer"
    with closing(sqlite3.connect(settings.database_path)) as db, db:
        db.execute(
            f"""UPDATE {table} SET
                application_status=?,
                status_updated_at=?,
                application_sent_at=CASE WHEN ?='Candidature envoyée' THEN COALESCE(application_sent_at, ?) ELSE application_sent_at END,
                application_attempted_at=?,
                application_error=NULL,
                application_channel='FRANCE_TRAVAIL'
                WHERE source='france_travail' AND external_id=?""",
            (new_status, now, new_status, now, now, job["external_id"]),
        )
    _log_event(settings.database_path, job, result["status"], result["message"], result.get("url"))
    return {**result, "channel": "FRANCE_TRAVAIL"}


def run_daily_ft_autopilot(
    settings,
    *,
    keywords: list[str] | None = None,
    location: str | None = "Île-de-France",
    max_applications: int = 10,
    mode: str = "auto",
    tone: str = "modern",
    dry_run: bool = False,
    headless: bool = False,
    on_progress=None,
) -> dict:
    """Exécute la campagne automatique sur les offres d'emploi France Travail."""
    settings = effective_settings(settings)
    if not keywords:
        keywords = ["Data Scientist", "AI Engineer", "Machine Learning Engineer", "Data Engineer", "Data Analyst"]

    def notify(stage: str, current: int = 0, total: int = 0, detail: str = ""):
        if on_progress:
            on_progress({"stage": stage, "current": current, "total": total, "detail": detail})

    # Récupérer les offres existantes non encore envoyées
    with closing(sqlite3.connect(settings.database_path)) as db:
        db.row_factory = sqlite3.Row
        existing = [
            dict(r)
            for r in db.execute(
                "SELECT * FROM jobs WHERE source='france_travail' AND (application_status IS NULL OR application_status NOT IN ('Candidature envoyée', 'Entretien', 'Refus', 'Archivée')) ORDER BY application_match_score DESC, id DESC"
            ).fetchall()
        ]

    # Si pas assez d'offres en base, en chercher de nouvelles
    if len(existing) < max_applications:
        notify("search", 0, max_applications, "Recherche de nouvelles offres d'emploi sur France Travail…")
        try:
            from app.global_search import search_global
            search_global(keywords, location=location, limit=max(30, max_applications * 3))
        except Exception:
            pass
        with closing(sqlite3.connect(settings.database_path)) as db:
            db.row_factory = sqlite3.Row
            existing = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM jobs WHERE source='france_travail' AND (application_status IS NULL OR application_status NOT IN ('Candidature envoyée', 'Entretien', 'Refus', 'Archivée')) ORDER BY application_match_score DESC, id DESC"
                ).fetchall()
            ]

    selected = existing[:max_applications]
    report = {
        "requested": max_applications,
        "found_eligible": len(existing),
        "selected_count": len(selected),
        "prepared_count": 0,
        "sent_count": 0,
        "failed_count": 0,
        "results": [],
    }

    if not selected:
        return report

    # 1. Préparation des lettres
    prepared_jobs = []
    for idx, job in enumerate(selected, start=1):
        clean_id = str(job["external_id"]).strip("'\" \t\n")
        notify(
            "prepare",
            idx,
            len(selected),
            f"Génération de la lettre pour {job.get('company') or clean_id} ({idx}/{len(selected)})…",
        )
        full_job = get_offer(job["external_id"], "france_travail", settings.database_path)
        if not full_job:
            continue
        if not (full_job.get("application_message") or "").strip():
            draft = generate_preparation(full_job, settings, mode=mode, tone=tone)
            save_preparation(
                full_job["external_id"],
                draft["message"],
                draft["letter"],
                "france_travail",
                settings.database_path,
                generated=draft,
                ready=True,
            )
            full_job = get_offer(full_job["external_id"], "france_travail", settings.database_path)
        prepared_jobs.append(full_job)
        report["prepared_count"] += 1

    # 2. Envoi via navigateur persistant Chrome
    notify("auth", 0, len(prepared_jobs), "Ouverture de France Travail dans Chrome…")
    with open_lbb_browser(headless=headless) as context:
        page = context.pages[0] if context.pages else context.new_page()

        for idx, job in enumerate(prepared_jobs, start=1):
            clean_id = str(job["external_id"]).strip("'\" \t\n")
            company_name = job.get("company") or clean_id
            title = job.get("title") or "Offre d'emploi"
            offer_url = job.get("application_url") or job.get("url") or f"{FT_CANDIDAT_BASE}/offres/recherche/detail/{clean_id}"
            message = job.get("application_letter") or job.get("application_message") or ""

            notify(
                "send",
                idx,
                len(prepared_jobs),
                f"{'Pré-remplissage' if dry_run else 'Envoi'} {idx}/{len(prepared_jobs)} : {company_name} — {title[:35]}…",
            )
            now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
            try:
                res = submit_ft_offer_on_page(
                    page,
                    offer_url=offer_url,
                    external_id=clean_id,
                    title=title,
                    company=company_name,
                    message=message,
                    cv_path=settings.cv_path,
                    auto_send=not dry_run,
                    dry_run=dry_run,
                )
                new_status = "Candidature envoyée" if res["status"] == "SENT" else "Prête à envoyer"
                with closing(sqlite3.connect(settings.database_path)) as db, db:
                    db.execute(
                        """UPDATE jobs SET
                            application_status=?,
                            status_updated_at=?,
                            application_sent_at=CASE WHEN ?='Candidature envoyée' THEN COALESCE(application_sent_at, ?) ELSE application_sent_at END,
                            application_attempted_at=?,
                            application_error=NULL,
                            application_channel='FRANCE_TRAVAIL'
                            WHERE source='france_travail' AND external_id=?""",
                        (new_status, now, new_status, now, now, job["external_id"]),
                    )
                _log_event(settings.database_path, job, res["status"], res["message"], res.get("url"))
                if res["status"] == "SENT":
                    report["sent_count"] += 1
                report["results"].append({
                    "external_id": clean_id,
                    "company_name": company_name,
                    "location": job.get("location"),
                    "job_label": title,
                    "status": res["status"],
                    "message": res["message"],
                })
                if not dry_run and idx < len(prepared_jobs):
                    time.sleep(2.0)
            except Exception as exc:
                err_msg = str(exc)
                report["failed_count"] += 1
                with closing(sqlite3.connect(settings.database_path)) as db, db:
                    db.execute(
                        """UPDATE jobs SET
                            application_status='Échec',
                            status_updated_at=?,
                            application_attempted_at=?,
                            application_error=?
                            WHERE source='france_travail' AND external_id=?""",
                        (now, now, err_msg, job["external_id"]),
                    )
                _log_event(settings.database_path, job, "ERROR", err_msg, offer_url)
                report["results"].append({
                    "external_id": clean_id,
                    "company_name": company_name,
                    "location": job.get("location"),
                    "job_label": title,
                    "status": "ERROR",
                    "message": err_msg,
                })

    notify("done", len(prepared_jobs), len(prepared_jobs), "Campagne France Travail terminée !")
    return report
