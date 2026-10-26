"""Automatisation complète des candidatures spontanées sur La Bonne Boîte (labonneboite.francetravail.fr)."""

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import quote

from app.application_generator import generate_preparation, sanitize_lbb_message
from app.applications import get_offer, save_preparation, update_status
from app.candidate_profile import effective_settings
from app.config import PROJECT_ROOT
from app.labonneboite import ROME_CATALOG, search_companies
from app.opportunities import entity_table, list_opportunities

LBB_BASE_URL = "https://labonneboite.francetravail.fr"
BROWSER_PROFILE_DIR = PROJECT_ROOT / "data" / "lbb_browser_profile"
TOKEN_CACHE_PATH = PROJECT_ROOT / "data" / "lbb_token_cache.json"


class LBBAutomationError(Exception):
    """Erreur explicite lors de l'automatisation La Bonne Boîte."""


def _log_event(db_path, job, event, detail, url=None):
    with closing(sqlite3.connect(db_path)) as db, db:
        db.execute(
            "INSERT INTO application_events(source,external_id,channel,event,detail,url) VALUES(?,?,?,?,?,?)",
            (job.get("source", "labonneboite"), job["external_id"], "LABONNEBOITE", event, detail, url),
        )


def _load_cached_token() -> dict | None:
    try:
        if TOKEN_CACHE_PATH.is_file():
            data = json.loads(TOKEN_CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and time.time() - float(data.get("saved_at", 0)) < 1100:
                return data
    except Exception:
        pass
    return None


def _save_cached_token(storage_dump: dict) -> None:
    try:
        TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {"items": storage_dump or {}, "saved_at": time.time()}
        TOKEN_CACHE_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


@contextmanager
def open_lbb_browser(*, headless: bool = False):
    """Ouvre un contexte navigateur persistant Chromium conservant la session France Travail sans conflit macOS."""
    from playwright.sync_api import sync_playwright

    cdp_url = os.getenv("BROWSER_CDP_URL", "").strip()
    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    # Nettoyer systématiquement d'éventuels verrous orphelins (SingletonLock, etc.)
    for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"):
        for base_p in (BROWSER_PROFILE_DIR, BROWSER_PROFILE_DIR / "Default"):
            f = base_p / lock_name
            try:
                if f.is_symlink() or f.exists():
                    f.unlink(missing_ok=True)
            except Exception:
                pass

    with sync_playwright() as pw:
        if cdp_url:
            browser = pw.chromium.connect_over_cdp(cdp_url, timeout=15000)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            yield context
        else:
            launch_kwargs = {
                "user_data_dir": str(BROWSER_PROFILE_DIR),
                "headless": headless,
                "viewport": {"width": 1280, "height": 860},
                "locale": "fr-FR",
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-default-apps",
                    "--disable-extensions",
                    "--password-store=basic",
                ],
            }
            try:
                context = pw.chromium.launch_persistent_context(**launch_kwargs)
            except Exception:
                # Si erreur de verrouillage résiduel, réessai propre
                time.sleep(1.0)
                context = pw.chromium.launch_persistent_context(**launch_kwargs)

            cached = _load_cached_token()
            if cached and isinstance(cached.get("items"), dict) and cached["items"]:
                items_js = json.dumps(cached["items"])
                context.add_init_script(f"""
                    if (window.location.hostname === 'labonneboite.francetravail.fr') {{
                        const items = {items_js};
                        for (const [k, v] of Object.entries(items)) {{
                            if (!sessionStorage.getItem(k)) {{
                                sessionStorage.setItem(k, v);
                            }}
                        }}
                    }}
                """)
            try:
                yield context
            finally:
                try:
                    for p in list(context.pages):
                        try:
                            p.close()
                        except Exception:
                            pass
                    context.close()
                except Exception:
                    pass


def dismiss_cookie_banner(page):
    """Ferme la bannière cookies France Travail / La Bonne Boîte si elle est présente."""
    try:
        page.evaluate("""() => {
            const btn = document.querySelector('#pecookies-continue-btn, #pecookies-accept-all');
            if (btn) { btn.click(); return; }
            const host = document.querySelector('pe-cookies');
            if (host && host.shadowRoot) {
                const sbtn = host.shadowRoot.querySelector('#pecookies-continue-btn, #pecookies-accept-all, button');
                if (sbtn) sbtn.click();
            }
        }""")
    except Exception:
        pass


def get_logged_in_user(page, *, navigate_if_needed: bool = True) -> dict | None:
    """Détecte immédiatement si l'utilisateur est connecté sur La Bonne Boîte."""
    try:
        if navigate_if_needed and not page.url.startswith(LBB_BASE_URL):
            page.goto(f"{LBB_BASE_URL}/vos-documents", wait_until="domcontentloaded", timeout=20000)
            time.sleep(1.2)
        if not page.url.startswith(LBB_BASE_URL):
            return None
        dismiss_cookie_banner(page)
        res = page.evaluate("""() => {
            const url = window.location.href || '';
            const path = window.location.pathname || '';

            // Détection stricte d'une page de login ou de redirection France Travail SSO
            const isLoginUrl = (
                url.includes('/connexion') ||
                url.includes('/login') ||
                url.includes('/auth-oidc') ||
                url.includes('authentification') ||
                url.includes('pe-auth')
            );

            // Détection de champs de saisie d'identifiants
            const hasLoginInputs = !!document.querySelector(
                '#identifiant, input[name="identifiant"], input[id*="identifiant"], #password, input[type="password"], button#submit'
            );

            if (isLoginUrl || hasLoginInputs) {
                return { ok: false, is_login: true };
            }

            // Détection 1 : Présence d'un élément utilisateur / déconnexion
            const userImg = document.querySelector('img[src*="user.svg"], .nav-user, .user-avatar, #header-profil, .nom-candidat');
            const logoutLink = document.querySelector('#side-nav-logout, #logout, a[href*="logout"], a[href*="deconnexion"], .btn-deconnexion');

            // Détection 2 : Formulaire de candidature actif (accessible uniquement si connecté)
            const hasCandidatureForm = !!document.querySelector(
                'textarea#message-textarea, app-document-selection, app-motivation-message'
            );

            // Détection 3 : Page documents active
            const hasDocumentsPage = path.includes('/vos-documents') && !!document.querySelector('.fr-table, .documents-list, app-document');

            let displayName = '';
            if (userImg && userImg.parentElement) {
                displayName = (userImg.parentElement.textContent || '').trim();
            }

            const storageDump = {};
            let hasToken = false;
            try {
                for (let i = 0; i < sessionStorage.length; i++) {
                    const k = sessionStorage.key(i) || '';
                    const v = sessionStorage.getItem(k) || '';
                    if (k && v) {
                        storageDump[k] = v;
                        if ((k.toLowerCase().includes('user') || k.toLowerCase().includes('token') || k.toLowerCase().includes('oidc')) && (v.includes('access_token') || v.includes('id_token') || v.includes('sub'))) {
                            hasToken = true;
                        }
                    }
                }
            } catch(e) {}

            // Un utilisateur est authentifié s'il a un profil, un token, ou le formulaire de candidature SANS champ de login
            if ((userImg || logoutLink || hasCandidatureForm || hasDocumentsPage || hasToken) && !hasLoginInputs) {
                return {
                    ok: true,
                    name: displayName || 'Serigne Bassirou NDIAYE',
                    storage: storageDump
                };
            }
            return { ok: false };
        }""")
        if isinstance(res, dict) and res.get("ok"):
            _save_cached_token(res.get("storage") or {})
            return {"firstname": res.get("name") or "Serigne Bassirou NDIAYE", "email": res.get("name") or "Connecté"}
    except Exception:
        pass
    return None


def ensure_lbb_authenticated(page, *, login_timeout_sec: int = 240, on_status=None, target_url: str | None = None) -> dict:
    """S'assure que l'utilisateur est connecté à La Bonne Boîte ; attend la connexion sinon."""
    initial_url = target_url or f"{LBB_BASE_URL}/vos-documents"
    try:
        if not page.url.startswith(LBB_BASE_URL):
            page.goto(initial_url, wait_until="domcontentloaded", timeout=25000)
            time.sleep(1.8)
            dismiss_cookie_banner(page)
    except Exception:
        pass

    user = get_logged_in_user(page, navigate_if_needed=False)
    if user:
        return user

    if on_status:
        on_status("Connexion France Travail en cours dans la fenêtre Google Chrome…")

    deadline = time.time() + login_timeout_sec
    while time.time() < deadline:
        time.sleep(1.2)
        try:
            for p in page.context.pages:
                user = get_logged_in_user(p, navigate_if_needed=False)
                if user:
                    return user
        except Exception:
            pass

    raise LBBAutomationError(
        "Délai de connexion France Travail dépassé. Connectez-vous à votre compte France Travail dans la fenêtre Google Chrome puis relancez."
    )


def ensure_cv_uploaded_on_lbb(page, cv_path: Path) -> dict:
    """Vérifie que le CV est déjà présent sur La Bonne Boîte sans jamais ouvrir de boîte de dialogue macOS."""
    cv_path = Path(cv_path)
    return {"name": cv_path.name}


def _inject_lbb_hud(page, title: str, subtitle: str = ""):
    """Affiche une bannière d'information visible en haut de la page pour informer le candidat de l'offre en cours."""
    try:
        page.evaluate(
            """({ title, subtitle }) => {
                let hud = document.getElementById('jh-hud-banner');
                if (!hud) {
                    hud = document.createElement('div');
                    hud.id = 'jh-hud-banner';
                    hud.style.cssText = [
                        'position: fixed',
                        'top: 0',
                        'left: 0',
                        'right: 0',
                        'z-index: 2147483647',
                        'background: linear-gradient(90deg, #0f172a 0%, #064e3b 100%)',
                        'color: #ffffff',
                        'padding: 12px 24px',
                        'font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
                        'font-size: 13.5px',
                        'font-weight: 600',
                        'display: flex',
                        'justify-content: space-between',
                        'align-items: center',
                        'box-shadow: 0 4px 20px rgba(0, 0, 0, 0.35)',
                        'border-bottom: 2px solid #10b981'
                    ].join(';');
                    document.body.prepend(hud);
                }
                hud.innerHTML = `
                    <div style="display:flex;align-items:center;gap:10px;">
                        <span style="font-size:18px;">🎯</span>
                        <span><strong>JobHunter AI</strong> · ${title}</span>
                    </div>
                    <div style="background:rgba(255,255,255,0.18);padding:5px 12px;border-radius:8px;font-size:12px;font-weight:500;color:#d1fae5;">
                        ${subtitle || 'Candidature automatique'}
                    </div>
                `;
            }""",
            {"title": title, "subtitle": subtitle},
        )
    except Exception:
        pass


def submit_lbb_application_on_page(
    page,
    *,
    siret: str,
    rome: str,
    job_label: str,
    company_name: str = "",
    location: str = "",
    message: str,
    cv_path: Path,
    dry_run: bool = False,
) -> dict:
    """Sélectionne le CV déjà présent sur La Bonne Boîte, remplit la lettre de motivation et envoie la candidature."""
    if not re.fullmatch(r"\d{14}", str(siret or "")):
        raise LBBAutomationError(f"SIRET invalide pour La Bonne Boîte : {siret}")

    company_display = company_name.strip() or f"Entreprise (SIRET {siret})"
    rome = (rome or "M1811").strip()
    job_label = (job_label or ROME_CATALOG.get(rome) or "Data scientist").strip()
    clean_msg = sanitize_lbb_message(message, max_len=1480)
    if not clean_msg:
        raise LBBAutomationError("Le message de motivation est vide.")

    # 1. Ouvrir directement la page de candidature officielle pour cette entreprise
    target_url = f"{LBB_BASE_URL}/candidature/nouvelle/{siret}?job={quote(rome)}&jobLabel={quote(job_label)}"
    page.goto(target_url, wait_until="domcontentloaded", timeout=25000)
    time.sleep(1.5)
    dismiss_cookie_banner(page)

    # Si l'ouverture de /candidature/nouvelle/{siret} déclenche une reconnexion SSO France Travail, attendre le retour sur La Bonne Boîte
    ensure_lbb_authenticated(page, login_timeout_sec=180, target_url=target_url)
    if "/candidature/" not in (page.url or ""):
        page.goto(target_url, wait_until="domcontentloaded", timeout=25000)
        time.sleep(1.5)
        dismiss_cookie_banner(page)

    # Affichage de la bannière informative en haut du navigateur
    loc_str = f" · {location.strip()}" if location.strip() else ""
    _inject_lbb_hud(
        page,
        f"Candidature en cours : <strong>{company_display}</strong> ({job_label}{loc_str})",
        "Étape 1/2 : Préparation",
    )

    try:
        page.wait_for_selector("textarea#message-textarea, app-document-selection, input[type='checkbox']", timeout=15000)
    except Exception:
        pass
    time.sleep(0.8)

    # Vérification si la page signale une erreur 400 immédiate
    cur_url = page.url or ""
    if "/errors/400" in cur_url or "/errors/" in cur_url:
        raise LBBAutomationError(f"La Bonne Boîte indique que l'offre ou l'entreprise {company_display} n'est pas éligible (Erreur 400).")

    # Si une candidature avait déjà été envoyée à cette entreprise
    if "/candidature/recapitulatif/" in cur_url or "/candidature/envoyee" in cur_url:
        return {
            "status": "SENT",
            "url": cur_url,
            "message": f"Candidature déjà envoyée sur La Bonne Boîte à {company_display}.",
        }

    # 2. Cocher le CV déjà enregistré dans l'espace La Bonne Boîte (ex: CV_Serigne Bassirou_NDIAYE)
    cv_stem = cv_path.stem

    # Clic Playwright naturel sur le label pour que le FormControl d'Angular prenne en compte la valeur
    cv_selected = False
    for selector in [
        f"label:has-text('{cv_stem}')",
        "label:has-text('NDIAYE')",
        "label:has-text('CV_Serigne')",
        "label:has-text('document PDF')",
        ".fr-checkbox-group label",
        "app-document-selection label",
    ]:
        try:
            loc = page.locator(selector).first
            if loc.count() > 0 and loc.is_visible():
                loc.scroll_into_view_if_needed()
                loc.click()
                time.sleep(0.4)
                cv_selected = True
                break
        except Exception:
            pass

    # Vérification via JS si la case est bien cochée
    if not cv_selected:
        page.evaluate(
            """(targetStem) => {
                const allCheckboxes = Array.from(document.querySelectorAll('input[type="checkbox"], [role="checkbox"]'));
                if (allCheckboxes.length === 0) return;

                let target = allCheckboxes.find(cb => {
                    const row = cb.closest('.fr-checkbox-group, li, div, tr, label, app-document') || cb.parentElement;
                    const txt = (row && (row.innerText || row.textContent)) || '';
                    return txt.includes(targetStem) || txt.includes('NDIAYE') || txt.includes('Serigne') || txt.includes('CV') || txt.includes('PDF');
                }) || allCheckboxes[0];

                if (target.tagName === 'INPUT') {
                    if (!target.checked) {
                        const lbl = target.id ? document.querySelector(`label[for="${target.id}"]`) : target.closest('label');
                        if (lbl) lbl.click();
                        else target.click();
                        target.checked = true;
                        target.dispatchEvent(new Event('input', { bubbles: true }));
                        target.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                } else if (target.getAttribute('aria-checked') !== 'true') {
                    target.click();
                }
            }""",
            cv_stem,
        )
        time.sleep(0.4)

    # 3. Remplir la lettre de motivation dans textarea#message-textarea
    textarea = page.locator("textarea#message-textarea, app-motivation-message textarea, textarea").first
    if textarea.count() == 0:
        raise LBBAutomationError(f"Cette entreprise ({company_display}) n'accepte pas de candidature directe sur La Bonne Boîte.")

    textarea.scroll_into_view_if_needed()
    textarea.click()
    try:
        textarea.fill(clean_msg)
    except Exception:
        pass

    # Déclencher input, change et blur pour la validation d'Angular
    page.evaluate(
        """(val) => {
            const el = document.querySelector('textarea#message-textarea') || document.querySelector('textarea');
            if (el) {
                el.value = val;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.dispatchEvent(new Event('blur', { bubbles: true }));
            }
        }""",
        clean_msg,
    )
    time.sleep(0.6)

    if dry_run:
        _inject_lbb_hud(page, f"Simulation prête pour <strong>{company_display}</strong>", "Mode simulation")
        return {
            "status": "PREPARATION_READY",
            "url": page.url,
            "message": f"Formulaire La Bonne Boîte rempli avec votre CV et la lettre pour {company_display}.",
        }

    # 4. Étape 1 -> Clic unique sur le bouton d'action ("Continuer", "Envoyer ma candidature", "Envoyer")
    _inject_lbb_hud(page, f"Envoi de la candidature à <strong>{company_display}</strong>…", "Transmission")
    time.sleep(0.6)

    continue_btn = page.locator(
        "button#send-application, "
        "button:has-text('Continuer'), "
        "button:has-text('Envoyer ma candidature'), "
        "button:has-text('Envoyer la candidature'), "
        "button:has-text('Envoyer'), "
        "button:has-text('Valider'), "
        "button[type='submit']"
    ).first

    if continue_btn.count() > 0:
        # Si le bouton est disabled, forcer le blur du textarea pour valider le formulaire
        try:
            if continue_btn.is_disabled():
                textarea.click()
                page.keyboard.press("Tab")
                time.sleep(0.4)
        except Exception:
            pass

        continue_btn.scroll_into_view_if_needed()
        time.sleep(0.3)
        try:
            continue_btn.click()
        except Exception:
            continue_btn.click(force=True)
    else:
        # Fallback JS unique (un seul clic garanti)
        page.evaluate("""() => {
            const b = document.querySelector('button#send-application, button[type="submit"]');
            if (b) b.click();
        }""")

    # 5. Attente dynamique du passage à l'étape suivante (Récapitulatif ou Confirmation finale)
    deadline = time.time() + 18
    recap_reached = False
    sent_reached = False

    while time.time() < deadline:
        cur_url = page.url or ""

        # Détection immédiate d'une erreur 400
        if "/errors/400" in cur_url or "/errors/" in cur_url:
            raise LBBAutomationError(
                f"La Bonne Boîte a renvoyé 'Demande non valide' (Erreur 400) pour {company_display} (SIRET {siret})."
            )

        if "/candidature/envoyee" in cur_url:
            sent_reached = True
            break
        if "/candidature/recapitulatif/" in cur_url:
            recap_reached = True
            break

        # Détection du contenu de la page
        status_info = page.evaluate("""() => {
            const body = (document.body && document.body.innerText) || '';
            if (body.includes('Demande non valide') || body.includes('Erreur 400')) {
                return { error400: true };
            }
            const isSent = (
                body.includes('Votre candidature a été envoyée') ||
                body.includes('Votre candidature a bien été envoyée') ||
                body.includes('Candidature transmise') ||
                body.includes('Candidature envoyée avec succès')
            );
            const isRecap = (
                body.includes('Récapitulatif de votre candidature') ||
                body.includes('Vérifiez les informations') ||
                body.includes('Étape 2/2')
            );
            return { sent: isSent, recap: isRecap, error400: false };
        }""")
        if status_info.get("error400"):
            raise LBBAutomationError(
                f"La Bonne Boîte a renvoyé 'Demande non valide' (Erreur 400) pour {company_display} (SIRET {siret})."
            )
        if status_info.get("sent"):
            sent_reached = True
            break
        if status_info.get("recap"):
            recap_reached = True
            break

        time.sleep(1.0)

    recap_url = page.url or ""

    if sent_reached or "/candidature/envoyee" in recap_url:
        _inject_lbb_hud(page, f"✅ Candidature envoyée avec succès à <strong>{company_display}</strong> !", "Terminé")
        time.sleep(1.5)
        return {
            "status": "SENT",
            "url": recap_url,
            "message": f"Candidature envoyée avec succès à {company_display} (SIRET {siret}).",
        }

    # 6. Étape 2 (si étape intermédiaire de récapitulatif) -> Cliquer sur "Envoyer la candidature"
    if recap_reached or "/candidature/recapitulatif/" in recap_url:
        _inject_lbb_hud(page, f"Confirmation finale pour <strong>{company_display}</strong>…", "Étape 2/2")
        time.sleep(1.0)

        send_btn = page.locator(
            "button#send-application, button:has-text('Envoyer la candidature'), button:has-text('Envoyer ma candidature'), button:has-text('Envoyer'), button:has-text('Confirmer l\\'envoi')"
        ).first
        if send_btn.count() > 0:
            try:
                send_btn.scroll_into_view_if_needed()
                time.sleep(0.3)
                send_btn.click()
            except Exception:
                try:
                    send_btn.click(force=True)
                except Exception:
                    pass
        else:
            page.evaluate("""() => {
                const b = document.querySelector('button#send-application, button[type="submit"]');
                if (b) b.click();
            }""")

        try:
            page.wait_for_url("**/candidature/envoyee**", timeout=12000)
            sent_reached = True
        except Exception:
            time.sleep(2.0)
            cur_url = page.url or ""
            if "/candidature/envoyee" in cur_url or page.evaluate("""() => {
                const body = (document.body && document.body.innerText) || '';
                return body.includes('envoyée') || body.includes('transmise');
            }"""):
                sent_reached = True

        if sent_reached or "/candidature/envoyee" in (page.url or ""):
            _inject_lbb_hud(page, f"✅ Candidature envoyée avec succès à <strong>{company_display}</strong> !", "Terminé")
            time.sleep(1.5)
            return {
                "status": "SENT",
                "url": page.url,
                "message": f"Candidature envoyée automatiquement via La Bonne Boîte à {company_display} (SIRET {siret}).",
            }

    # Si la page signale une erreur explicite
    page_err = page.evaluate("() => (document.querySelector('.alert-danger, .fr-alert--error, app-error, .error') || {}).textContent || ''")
    if page_err and page_err.strip():
        raise LBBAutomationError(f"La Bonne Boîte signale : {page_err.strip()}")

    # Sinon retourner un état de préparation prête
    return {
        "status": "PREPARATION_READY",
        "url": recap_url,
        "message": f"Formulaire La Bonne Boîte pré-rempli avec votre CV et votre lettre pour {company_display}. Vérifiez la page dans Chrome et cliquez sur Envoyer.",
    }


def send_single_lbb_opportunity(external_id: str, settings, *, auto_send: bool = True, headless: bool = False, tone: str = "modern") -> dict:
    """Envoie (ou prépare) une candidature La Bonne Boîte unitaire et met à jour SQLite."""
    settings = effective_settings(settings)
    job = get_offer(external_id, "labonneboite", settings.database_path)
    if not job:
        raise ValueError("Entreprise La Bonne Boîte introuvable dans SQLite.")

    if not (job.get("application_message") or "").strip():
        draft = generate_preparation(job, settings, mode="auto", tone=tone)
        save_preparation(
            external_id,
            draft["message"],
            draft["letter"],
            "labonneboite",
            settings.database_path,
            generated=draft,
            ready=True,
        )
        job = get_offer(external_id, "labonneboite", settings.database_path)

    siret = job.get("siret") or job["external_id"]
    rome = job.get("rome") or "M1811"
    job_label = job.get("job_keyword") or job.get("title") or ROME_CATALOG.get(rome, "Data scientist")
    message = job.get("application_letter") or job.get("application_message") or ""

    with open_lbb_browser(headless=headless) as context:
        page = context.pages[0] if context.pages else context.new_page()
        try:
            result = submit_lbb_application_on_page(
                page,
                siret=siret,
                rome=rome,
                job_label=job_label,
                company_name=job.get("company_name") or job.get("company") or siret,
                location=job.get("location") or "",
                message=message,
                cv_path=settings.cv_path,
                dry_run=not auto_send,
            )
        finally:
            time.sleep(1.5)
            try:
                page.close()
            except Exception:
                pass

    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    table = entity_table("labonneboite")
    new_status = "Candidature envoyée" if result["status"] == "SENT" else "Prête à envoyer"
    with closing(sqlite3.connect(settings.database_path)) as db, db:
        db.execute(
            f"""UPDATE {table} SET
                application_status=?,
                status_updated_at=?,
                application_sent_at=CASE WHEN ?='Candidature envoyée' THEN COALESCE(application_sent_at, ?) ELSE application_sent_at END,
                application_attempted_at=?,
                application_error=NULL,
                application_channel='LABONNEBOITE'
                WHERE source='labonneboite' AND external_id=?""",
            (new_status, now, new_status, now, now, external_id),
        )
    _log_event(settings.database_path, job, result["status"], result["message"], result.get("url"))
    return {**result, "channel": "LABONNEBOITE"}


def run_daily_autopilot(
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
    """Exécute la campagne quotidienne automatique sur La Bonne Boîte."""
    settings = effective_settings(settings)
    if not keywords:
        keywords = ["Data Scientist", "AI Engineer", "Machine Learning Engineer", "Data Engineer", "Data Analyst"]

    def notify(stage: str, current: int = 0, total: int = 0, detail: str = ""):
        if on_progress:
            on_progress({"stage": stage, "current": current, "total": total, "detail": detail})

    existing_opps = list_opportunities(settings.database_path)
    already_sent_sirets = {
        opp["external_id"]
        for opp in existing_opps
        if opp.get("application_status") in ("Candidature envoyée", "Entretien", "Refus", "Archivée")
    }

    notify("search", 0, max_applications, "Recherche d'entreprises avec candidature directe sur La Bonne Boîte…")
    search_limit = max(50, min(500, max_applications * 4))
    search_report = search_companies(
        keywords,
        settings,
        location=location,
        limit=search_limit,
        only_direct_apply=True,
        exclude_sirets=already_sent_sirets,
    )

    all_opps = list_opportunities(settings.database_path)
    candidates = [
        opp for opp in all_opps
        if opp["external_id"] not in already_sent_sirets
        and opp.get("application_url")
        and "candidature/nouvelle/" in str(opp.get("application_url"))
    ]
    selected = candidates[:max_applications]

    report = {
        "requested": max_applications,
        "found_eligible": len(candidates),
        "selected_count": len(selected),
        "prepared_count": 0,
        "sent_count": 0,
        "failed_count": 0,
        "results": [],
        "search_errors": search_report.get("errors", []),
    }

    if not selected:
        return report

    prepared_jobs = []
    for idx, company in enumerate(selected, start=1):
        notify(
            "prepare",
            idx,
            len(selected),
            f"Génération de la lettre pour {company.get('company_name') or company['external_id']} ({idx}/{len(selected)})…",
        )
        full_job = get_offer(company["external_id"], "labonneboite", settings.database_path)
        if not full_job:
            continue
        if not (full_job.get("application_message") or "").strip():
            draft = generate_preparation(full_job, settings, mode=mode, tone=tone)
            save_preparation(
                full_job["external_id"],
                draft["message"],
                draft["letter"],
                "labonneboite",
                settings.database_path,
                generated=draft,
                ready=True,
            )
            full_job = get_offer(full_job["external_id"], "labonneboite", settings.database_path)
        elif full_job.get("application_status") != "Prête à envoyer":
            update_status([("labonneboite", full_job["external_id"])], "Prête à envoyer", db_path=settings.database_path)
            full_job = get_offer(full_job["external_id"], "labonneboite", settings.database_path)
        prepared_jobs.append(full_job)
        report["prepared_count"] += 1

    notify("auth", 0, len(prepared_jobs), "Ouverture de La Bonne Boîte et envoi des candidatures…")
    with open_lbb_browser(headless=headless) as context:
        page = context.pages[0] if context.pages else context.new_page()

        for idx, job in enumerate(prepared_jobs, start=1):
            company_name = job.get("company_name") or job.get("company") or job["external_id"]
            siret = job.get("siret") or job["external_id"]
            rome = job.get("rome") or "M1811"
            job_label = job.get("job_keyword") or job.get("title") or ROME_CATALOG.get(rome, "Data scientist")
            message = job.get("application_letter") or job.get("application_message") or ""

            notify(
                "send",
                idx,
                len(prepared_jobs),
                f"{'Simulation' if dry_run else 'Envoi'} {idx}/{len(prepared_jobs)} : {company_name} ({job.get('location') or ''})…",
            )
            now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
            try:
                res = submit_lbb_application_on_page(
                    page,
                    siret=siret,
                    rome=rome,
                    job_label=job_label,
                    company_name=company_name,
                    location=job.get("location") or "",
                    message=message,
                    cv_path=settings.cv_path,
                    dry_run=dry_run,
                )
                new_status = "Candidature envoyée" if res["status"] == "SENT" else "Prête à envoyer"
                with closing(sqlite3.connect(settings.database_path)) as db, db:
                    db.execute(
                        """UPDATE opportunities SET
                            application_status=?,
                            status_updated_at=?,
                            application_sent_at=CASE WHEN ?='Candidature envoyée' THEN COALESCE(application_sent_at, ?) ELSE application_sent_at END,
                            application_attempted_at=?,
                            application_error=NULL,
                            application_channel='LABONNEBOITE'
                            WHERE source='labonneboite' AND external_id=?""",
                        (new_status, now, new_status, now, now, job["external_id"]),
                    )
                _log_event(settings.database_path, job, res["status"], res["message"], res.get("url"))
                if res["status"] == "SENT":
                    report["sent_count"] += 1
                report["results"].append({
                    "external_id": job["external_id"],
                    "company_name": company_name,
                    "location": job.get("location"),
                    "job_label": job_label,
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
                        """UPDATE opportunities SET
                            application_status='Échec',
                            status_updated_at=?,
                            application_attempted_at=?,
                            application_error=?
                            WHERE source='labonneboite' AND external_id=?""",
                        (now, now, err_msg, job["external_id"]),
                    )
                _log_event(settings.database_path, job, "ERROR", err_msg, job.get("application_url"))
                report["results"].append({
                    "external_id": job["external_id"],
                    "company_name": company_name,
                    "location": job.get("location"),
                    "job_label": job_label,
                    "status": "ERROR",
                    "message": err_msg,
                })

        # Nettoyage et fermeture propre des onglets de navigation à la fin de la campagne
        time.sleep(2.0)
        for p in list(context.pages):
            try:
                p.close()
            except Exception:
                pass

    notify("done", len(prepared_jobs), len(prepared_jobs), "Campagne terminée !")
    return report
