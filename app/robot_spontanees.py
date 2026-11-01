"""Robot assisté des candidatures spontanées La Bonne Boîte : fenêtre visible, processus séparé du serveur.

- Vous vous connectez vous-même avec France Travail : le robot ne remplit jamais l'identifiant ni le mot de
  passe, et la session disparaît avec la fenêtre (navigateur neuf, rien n'est enregistré sur le disque).
- Pour chaque entreprise du lot du jour : formulaire du site, CV déjà téléversé coché, message collé, puis arrêt
  avant l'envoi. Vous relisez et envoyez vous-même ; le robot n'envoie que si l'envoi automatique est activé.
- Choix du CV (choix_cv) : si plusieurs documents de votre espace sont possibles, le robot ne devine pas ; il
  s'arrête sur le formulaire et vous demande lequel joindre.
- Au plus 10 entreprises par jour, pauses entre les actions, capture d'écran à chaque étape, arrêt immédiat en
  cas de captcha ou de page inattendue, journal lisible (journal.txt).

Le serveur prépare travail.json (entreprises, messages, réglages) puis lance :
    python -m app.robot_spontanees --dossier output/robot/<AAAAMMJJ-HHMMSS>
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import random
import re
import signal
import subprocess
import sys
import threading
import time
import unicodedata
from urllib.parse import urlsplit

from app.config import PROJECT_ROOT
from app.cv_pdf import OUTPUT_DIR
from app.spontanees import (A_ENVOYER, DEJA_CONTACTEE, SITE_LBB, SpontaneesError, charger_modele, envois_du_jour,
                            lire_reglages, lot_du_jour, marquer_envoyee, noter_robot, site_lbb)
from app.test_mode import fake_ft_enabled

ROBOT_DIR = OUTPUT_DIR / "robot"
RUN_ID = re.compile(r"\d{8}-\d{6}(?:-\d{1,2})?")
CAPTURE = re.compile(r"\d{2,3}-[a-z0-9-]{1,60}\.png")
ACTIONS = ("envoyee", "passer", "arreter")
CHOIX_CV = re.compile(r"cv:(\d{1,2})")  # « joindre le document n° N », quand le choix du CV est ambigu
_PROCESSUS: dict[str, subprocess.Popen] = {}


def _maintenant() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _lire(path: Path, defaut=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return defaut


def _ecrire(path: Path, data) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)  # jamais de fichier à moitié écrit pour le serveur


# --- Côté serveur : lancement, état, décisions, arrêt ------------------------------------------------------------

def _dernier() -> Path | None:
    runs = sorted(p for p in ROBOT_DIR.iterdir() if p.is_dir() and RUN_ID.fullmatch(p.name)) if ROBOT_DIR.is_dir() else []
    return runs[-1] if runs else None


def _pid(run: Path, etat: dict) -> int | None:
    try:
        pid = etat.get("pid") or int((run / "pid").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return pid if isinstance(pid, int) and pid > 0 else None


def _actif(run: Path, etat: dict) -> bool:
    """Processus du robot encore en vie (fenêtre ouverte comprise)."""
    if etat.get("sortie"):
        return False
    if (proc := _PROCESSUS.get(run.name)) is not None:
        return proc.poll() is None
    pid = _pid(run, etat)
    if pid is None:
        return False
    try:
        os.kill(pid, 0)  # lancé par une instance précédente du serveur (rechargement)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def etat_robot() -> dict:
    """Dernière exécution : étape, attente, journal et captures."""
    run = _dernier()
    if run is None:
        return {"actif": False, "id": None, "journal": [], "captures": []}
    etat = _lire(run / "etat.json", {}) or {}
    actif = _actif(run, etat)
    if not actif and etat.get("statut") == "en_cours":
        etat.update(statut="erreur", attente=None,
                    resume="Le robot s'est arrêté de façon inattendue : voir le journal ci-dessous.")
    try:
        journal = (run / "journal.txt").read_text(encoding="utf-8").splitlines()[-300:]
    except OSError:
        journal = []
    captures = [{"nom": c["nom"], "legende": c.get("legende") or "",
                 "url": f"/api/spontanees/robot/captures/{run.name}/{c['nom']}"}
                for c in etat.get("captures") or [] if isinstance(c, dict) and CAPTURE.fullmatch(str(c.get("nom")))]
    public = {k: etat.get(k) for k in ("mode", "envoi_automatique", "statut", "etape", "attente", "entreprise",
                                       "documents", "debut", "fin", "resume", "compteurs", "fenetre_ouverte")}
    return {**public, "actif": actif, "id": run.name, "journal": journal, "captures": captures}


def lancer_robot(settings, mode: str) -> dict:
    """Prépare le travail (lot du jour, messages, réglages) et lance le robot dans son propre processus."""
    if mode not in ("essai", "assiste"):
        raise SpontaneesError("Mode du robot inconnu : essai ou assisté.")
    run = _dernier()
    if run is not None and _actif(run, _lire(run / "etat.json", {}) or {}):
        raise SpontaneesError("Le robot est déjà lancé : arrêtez-le ou fermez sa fenêtre avant d'en relancer un.", 409)
    db_path, reglages = settings.database_path, lire_reglages(settings.database_path)
    lot = [e for e in lot_du_jour(db_path, modele=charger_modele()) if e["statut"] == A_ENVOYER and not e["envoyee_le"]]
    restant = reglages["plafond_par_jour"] - envois_du_jour(db_path)
    if not lot:
        raise SpontaneesError("Aucune entreprise à traiter : constituez d'abord le lot du jour "
                              "(ou toutes ses entreprises sont déjà envoyées).", 409)
    if mode == "essai":
        lot = [e for e in lot if not e["message"]["problemes"]][:1]
        if not lot:
            raise SpontaneesError("Aucun message utilisable pour l'essai : corrigez le modèle de message.", 409)
    elif restant <= 0:
        raise SpontaneesError(f"Plafond du jour atteint ({reglages['plafond_par_jour']} envois) : "
                              "le robot reprendra demain.", 409)
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = ROBOT_DIR / run_id
    for suffix in range(1, 100):
        if not folder.exists():
            break
        folder = ROBOT_DIR / f"{run_id}-{suffix}"
    folder.mkdir(parents=True)
    automatique = mode == "assiste" and reglages["envoi_automatique"]
    _ecrire(folder / "travail.json", {
        "mode": mode, "envoi_automatique": automatique, "plafond": reglages["plafond_par_jour"],
        "nom_cv": reglages["nom_cv"], "site": site_lbb(), "base": str(db_path),
        "entreprises": [{"siret": e["siret"], "entreprise": e["entreprise"], "ville": e["ville"],
                         "url_formulaire": e["url_formulaire"], "message": e["message"]["texte"],
                         "problemes": e["message"]["problemes"]} for e in lot]})
    _ecrire(folder / "etat.json", {"mode": mode, "envoi_automatique": automatique, "statut": "en_cours",
                                   "etape": "Démarrage du robot…", "debut": _maintenant(), "captures": []})
    with open(folder / "robot.log", "ab") as log:
        proc = subprocess.Popen([sys.executable, "-m", "app.robot_spontanees", "--dossier", str(folder)],
                                cwd=PROJECT_ROOT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    _PROCESSUS[folder.name] = proc
    (folder / "pid").write_text(str(proc.pid), encoding="utf-8")  # etat.json appartient désormais au robot
    return etat_robot()


def decider(action: str, document: int | None = None) -> dict:
    """Votre décision : formulaire envoyé par vous ou passé, ou bien le document à joindre (choix du CV ambigu)."""
    if action not in ("envoyee", "passer", "cv"):
        raise SpontaneesError("Décision inconnue : envoyée, passer ou cv.")
    run = _dernier()
    etat = (_lire(run / "etat.json", {}) or {}) if run else {}
    attente = "cv" if action == "cv" else "decision"
    if run is None or not _actif(run, etat) or etat.get("attente") != attente:
        raise SpontaneesError("Le robot n'attend pas de choix de CV en ce moment." if action == "cv" else
                              "Le robot n'attend pas de décision en ce moment.", 409)
    if action == "cv" and not (type(document) is int and 0 <= document < len(etat.get("documents") or [])):
        raise SpontaneesError("Document inconnu : choisissez l'un des documents proposés.")
    _ecrire(run / "decision.json", {"action": action, "document": document, "siret": etat.get("siret")})
    return etat_robot()


def arreter_robot() -> dict:
    """Arrêt demandé : le robot s'arrête à la prochaine vérification et ferme sa fenêtre (forcé après 20 s)."""
    run = _dernier()
    etat = (_lire(run / "etat.json", {}) or {}) if run else {}
    if run is not None and _actif(run, etat):
        (run / "arret").write_text(_maintenant(), encoding="utf-8")
        timer = threading.Timer(20, _forcer_arret, args=(run, _pid(run, etat)))
        timer.daemon = True
        timer.start()
    return etat_robot()


def _forcer_arret(run: Path, pid) -> None:
    if pid is not None and _actif(run, _lire(run / "etat.json", {}) or {}):
        try:
            os.killpg(pid, signal.SIGTERM)  # robot, pilote Playwright et navigateur
        except (ProcessLookupError, PermissionError):
            pass


def chemin_capture(run_id: str, nom: str) -> Path:
    path = ROBOT_DIR / run_id / nom
    if not RUN_ID.fullmatch(run_id) or not CAPTURE.fullmatch(nom) or not path.is_file():
        raise SpontaneesError("Capture introuvable.", 404)
    return path


# --- Côté robot : processus lancé par le serveur ----------------------------------------------------------------

def _action_valide(action) -> bool:
    return action in ACTIONS or bool(CHOIX_CV.fullmatch(str(action)))


class ArretRobot(Exception):
    """Arrêt du robot (captcha, page inattendue, demande, délai) : message lisible pour le journal."""


def _cle(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", (text or "").casefold())
    return " ".join(re.sub(r"[\W_]+", " ", "".join(c for c in decomposed if not unicodedata.combining(c))).split())


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", _cle(text)).strip("-")[:40] or "entreprise"


def _nom_document(nom: str) -> str:
    """« CV_Data (document PDF) » ou « CV_Data.pdf » → « CV_Data »."""
    return re.sub(r"\.[A-Za-z0-9]{2,4}$", "", re.sub(r"\s*\(document [^)]*\)\s*$", "", nom or "").strip())


def choix_cv(documents: list[dict], nom_cv: str = "", choisi: tuple | None = None) -> tuple[dict | None, str]:
    """Le document à joindre, ou (None, raison) quand le choix est ambigu : le robot ne devine jamais.

    1. Document que vous avez choisi plus tôt dans cette exécution (même nom et mêmes détails), s'il est seul.
    2. Sinon, avec un nom de CV dans Paramètres : le document de ce nom exact (accents, majuscules, tirets bas et
       extension ignorés), sinon le seul document dont le nom contient ce texte.
    3. Sinon, le seul document de votre espace.
    Dans tous les autres cas (aucun ou plusieurs documents possibles), le robot vous demande.
    """
    if choisi:
        trouves = [d for d in documents if (d["nom"], d.get("detail", "")) == tuple(choisi)]
        if len(trouves) == 1:
            return trouves[0], ""
        return None, (f"le document choisi plus tôt (« {choisi[0]} ») " +
                      ("n'est plus dans la liste" if not trouves else "apparaît plusieurs fois"))
    if cle := _cle(_nom_document(nom_cv)):
        exacts = [d for d in documents if _cle(_nom_document(d["nom"])) == cle]
        trouves = exacts or [d for d in documents if cle in _cle(d["nom"])]
        if len(trouves) == 1:
            return trouves[0], ""
        return None, (f"aucun document ne correspond au nom du CV « {nom_cv} » (Paramètres)" if not trouves else
                      f"{len(trouves)} documents correspondent au nom du CV « {nom_cv} » (Paramètres)")
    if len(documents) == 1:
        return documents[0], ""
    return None, f"{len(documents)} documents dans votre espace, et aucun nom de CV dans Paramètres"


def _libelle(document: dict, documents: list[dict]) -> str:
    """Nom du document, avec ses détails (date de mise en ligne…) si un autre porte le même nom."""
    homonyme = sum(d["nom"] == document["nom"] for d in documents) > 1
    return document["nom"] + (f" · {document['detail']}" if homonyme and document.get("detail") else "")


def _invisible() -> bool:
    """Navigateur sans fenêtre : uniquement pour les tests automatiques, en mode test."""
    return os.getenv("JOBHUNTER_ROBOT_INVISIBLE") == "1" and fake_ft_enabled()


ETAT_PAGE_JS = """() => ({
  formulaire: !!document.querySelector('#message-textarea')
    && !!document.querySelector('app-document-selection input[type=checkbox], #no-documents'),
  texte: (document.body && document.body.innerText || '').slice(0, 20000)
})"""

CAPTCHA_JS = """() => {
  const sel = 'iframe[src*="captcha" i], iframe[title*="captcha" i], iframe[src*="challenges.cloudflare.com"],'
    + ' .g-recaptcha, .h-captcha, .cf-turnstile, .frc-captcha, [id*="captcha" i], [class*="captcha" i], [name*="captcha" i]';
  const visible = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  if ([...document.querySelectorAll(sel)].some(visible)) return true;
  const t = (document.body && document.body.innerText || '').toLowerCase();
  return /captcha|je ne suis pas un robot|vérification de sécurité|vérifiez que vous êtes (un )?humain|verify you are human/.test(t);
}"""

DOCUMENTS_JS = """() => [...document.querySelectorAll('app-document-selection input[type=checkbox]')].map((cb) => {
  const label = cb.id ? document.querySelector('label[for="' + cb.id + '"]') : null;
  const nom = label && (label.querySelector('.font-extrabold') || label);
  const detail = label ? [...label.querySelectorAll('small')].map((s) => s.innerText.trim()).filter(Boolean).join(' · ') : '';
  return { id: cb.id, nom: nom ? nom.innerText.trim() : '', detail, coche: cb.checked };
})"""

BANDEAU_JS = """({ texte, boutons, cache }) => {
  let el = document.getElementById('jobhunter-bandeau');
  if (cache !== null) { if (el) el.style.visibility = cache ? 'hidden' : 'visible'; return; }
  const cle = JSON.stringify([texte, boutons]);
  if (el && el.dataset.cle === cle) return;
  if (!el) {
    el = document.createElement('div');
    el.id = 'jobhunter-bandeau';
    el.setAttribute('role', 'status');
    el.style.cssText = 'position:fixed;left:16px;right:16px;bottom:16px;z-index:2147483647;display:flex;'
      + 'flex-wrap:wrap;gap:10px;align-items:center;padding:12px 16px;border-radius:10px;background:#1b1f24;'
      + 'color:#fff;font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;'
      + 'box-shadow:0 6px 24px rgba(0,0,0,.3)';
    document.body.appendChild(el);
    document.body.style.paddingBottom = '110px';
  }
  el.dataset.cle = cle;
  el.textContent = '';
  const p = document.createElement('span');
  p.style.flex = '1 1 320px';
  const b = document.createElement('strong');
  b.textContent = 'JobHunter · ';
  p.append(b, texte);
  el.append(p);
  for (const [action, label] of boutons) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = label;
    const main = action === 'envoyee' || action.startsWith('cv:');
    btn.style.cssText = 'font:inherit;font-weight:600;border:0;border-radius:8px;padding:8px 12px;cursor:pointer;'
      + (main ? 'background:#2952cc;color:#fff' : 'background:#fff;color:#1b1f24');
    btn.addEventListener('click', () => window.__jobhunterDecision && window.__jobhunterDecision(action));
    el.append(btn);
  }
}"""


class Robot:
    """Une exécution : lit travail.json, écrit etat.json, journal.txt et les captures dans son dossier."""

    def __init__(self, dossier: Path):
        self.dossier = dossier
        self.travail = json.loads((dossier / "travail.json").read_text(encoding="utf-8"))
        self.db_path = Path(self.travail["base"])
        self.site = self.travail["site"]
        self.etat = _lire(dossier / "etat.json", {}) or {}
        self.captures, self.n_captures = [], 0
        self.decision_bandeau = None
        self.cv_choisi = None  # (nom, détails) du document que vous avez choisi, gardé pour toute l'exécution
        self.page = None
        self.rapide = fake_ft_enabled()
        self.delais = ({"connexion": 90, "decision": 90, "fenetre": 2, "page": 15} if self.rapide else
                       {"connexion": 600, "decision": 1200, "fenetre": 900, "page": 25})

    # Journal, état, captures

    def noter(self, message: str) -> None:
        with open(self.dossier / "journal.txt", "a", encoding="utf-8") as journal:
            journal.write(f"{datetime.now().strftime('%H:%M:%S')}  {message}\n")

    def ecrire_etat(self, **changes) -> None:
        self.etat.update(changes)
        _ecrire(self.dossier / "etat.json", self.etat)

    def capturer(self, slug: str, legende: str, pleine_page: bool = False) -> str:
        self.n_captures += 1
        nom = f"{self.n_captures:02d}-{slug}.png"
        try:
            self._bandeau(cache=True)
            self.page.screenshot(path=str(self.dossier / nom), full_page=pleine_page)
        finally:
            self._bandeau(cache=False)
        self.captures.append({"nom": nom, "legende": legende})
        self.ecrire_etat(captures=self.captures)
        return nom

    def _bandeau(self, texte: str = "", boutons=(), cache=None) -> None:
        try:
            self.page.evaluate(BANDEAU_JS, {"texte": texte, "boutons": [list(b) for b in boutons], "cache": cache})
        except Exception:
            pass  # page en cours de chargement : le bandeau sera remis au prochain passage

    def bandeau(self, texte: str, boutons=()) -> None:
        self._bandeau(texte, boutons)

    def _bouton(self, source, action) -> None:
        if _action_valide(action):
            self.decision_bandeau = action

    # Arrêt, décisions, pauses

    def verifier_arret(self) -> None:
        if (self.dossier / "arret").exists() or self.decision_bandeau == "arreter":
            raise ArretRobot("Arrêt demandé : le robot s'arrête. Rien n'a été envoyé par le robot.")

    def lire_decision(self) -> str | None:
        if (self.dossier / "arret").exists():
            return "arreter"
        action, self.decision_bandeau = self.decision_bandeau, None
        fichier = self.dossier / "decision.json"
        if action is None and fichier.exists():
            decision = _lire(fichier, {}) or {}
            action = f"cv:{decision.get('document')}" if decision.get("action") == "cv" else decision.get("action")
            fichier.unlink(missing_ok=True)
        return action if _action_valide(action) else None

    def attendre(self, secondes: float) -> None:
        fin = time.monotonic() + secondes
        while (reste := fin - time.monotonic()) > 0:
            self.verifier_arret()
            self.page.wait_for_timeout(int(min(reste, 1.0) * 1000))  # laisse passer les clics du bandeau

    def pause(self) -> None:
        self.attendre(random.uniform(0.2, 0.4) if self.rapide else random.uniform(1.0, 2.5))

    def pause_entre_entreprises(self) -> None:
        if self.rapide:
            secondes = random.uniform(0.3, 0.6)
        else:
            secondes = random.uniform(30, 75) if self.travail["envoi_automatique"] else random.uniform(4, 9)
        self.ecrire_etat(etape=f"Pause de {round(secondes)} s avant l'entreprise suivante")
        self.attendre(secondes)

    # Lecture de la page

    def etat_page(self) -> str:
        url = urlsplit(self.page.url)
        if url.hostname != urlsplit(self.site).hostname or url.path.startswith(("/login", "/auth")):
            return "connexion"
        if url.path.startswith("/errors/"):
            return "erreur"
        for prefix, etat in (("/candidature/recapitulatif/", "synthese"), ("/candidature/editer/", "brouillon"),
                             ("/candidature/envoyee", "envoyee")):
            if url.path.startswith(prefix):
                return etat
        if url.path.startswith("/candidature/nouvelle/"):
            try:
                return "formulaire" if self.page.evaluate(ETAT_PAGE_JS)["formulaire"] else "chargement"
            except Exception:
                return "chargement"
        return "autre"

    def captcha(self) -> bool:
        if any(re.search(r"(?i)captcha|challenges\.cloudflare\.com", frame.url) for frame in self.page.frames):
            return True
        try:
            return bool(self.page.evaluate(CAPTCHA_JS))
        except Exception:
            return False

    def verifier_captcha(self, nom: str) -> None:
        if self.captcha():
            capture = self.capturer(_slug(nom) + "-captcha", f"{nom} · captcha affiché")
            raise ArretRobot(f"Captcha affiché sur la page de {nom} : arrêt immédiat, rien n'a été envoyé "
                             f"(capture {capture}). Le robot ne résout jamais de captcha.")

    def attendre_page(self, attendus: set[str], quoi: str, nom: str) -> str:
        """Attend une page connue, stable sur 3 lectures ; s'arrête sur captcha, erreur ou page inattendue."""
        fin, precedent, stable = time.monotonic() + self.delais["page"], None, 0
        while time.monotonic() < fin:
            self.verifier_arret()
            etat = self.etat_page()
            if etat == "connexion":
                raise ArretRobot("La session France Travail s'est terminée (page de connexion affichée) : "
                                 "relancez le robot et reconnectez-vous. Rien n'a été envoyé.")
            if etat != "chargement":
                self.verifier_captcha(nom)
            if etat == "erreur":
                capture = self.capturer(_slug(nom) + "-erreur", f"{nom} · page d'erreur de La Bonne Boîte")
                raise ArretRobot(f"Page inattendue pour {nom} : La Bonne Boîte affiche une erreur "
                                 f"({urlsplit(self.page.url).path}). Arrêt, rien n'a été envoyé (capture {capture}).")
            stable = stable + 1 if etat == precedent else 1
            precedent = etat
            if etat in attendus and stable >= 3:
                return etat
            self.page.wait_for_timeout(600)
        capture = self.capturer(_slug(nom) + "-page-inattendue", f"{nom} · page inattendue")
        raise ArretRobot(f"Page inattendue pour {nom} : {quoi} n'est pas apparu (adresse {urlsplit(self.page.url).path}). "
                         f"Arrêt, rien n'a été envoyé (capture {capture}).")

    # Étapes

    def connexion(self, url: str) -> None:
        self.ecrire_etat(etape="Ouverture de La Bonne Boîte", attente=None)
        self.page.goto(url, wait_until="domcontentloaded")
        self.ecrire_etat(attente="connexion", etape="Connectez-vous avec France Travail dans la fenêtre du robot.")
        self.noter("En attente de votre connexion France Travail dans la fenêtre du robot "
                   "(le robot ne remplit jamais l'identifiant ni le mot de passe).")
        fin, dernier_essai, essais = time.monotonic() + self.delais["connexion"], time.monotonic(), 0
        depuis = {"autre": None, "chargement": None}
        while time.monotonic() < fin:
            self.verifier_arret()
            etat = self.etat_page()
            if etat in ("formulaire", "synthese", "brouillon"):
                self.ecrire_etat(attente=None)
                self.noter("Connexion détectée : le robot reprend la main.")
                return
            groupe = "autre" if etat in ("autre", "erreur") else etat if etat == "chargement" else None
            for cle in depuis:
                depuis[cle] = (depuis[cle] or time.monotonic()) if cle == groupe else None
            if groupe == "chargement" and time.monotonic() - depuis["chargement"] > self.delais["page"]:
                # Adresse du formulaire, sans formulaire : ce n'est ni la connexion ni un chargement.
                capture = self.capturer("page-inattendue", "page inattendue à la place du formulaire")
                raise ArretRobot("Page inattendue : le formulaire de candidature n'est pas apparu "
                                 f"(adresse {urlsplit(self.page.url).path}). Arrêt, rien n'a été envoyé (capture {capture}).")
            if groupe == "autre" and time.monotonic() - depuis["autre"] > 6 and time.monotonic() - dernier_essai > 15:
                # Revenu sur La Bonne Boîte hors du formulaire : on le rouvre (jamais pendant la connexion).
                if essais >= 3:
                    capture = self.capturer("page-inattendue", "page inattendue après la connexion")
                    raise ArretRobot("Page inattendue après la connexion : le formulaire ne s'ouvre pas "
                                     f"(adresse {urlsplit(self.page.url).path}). Arrêt, rien n'a été envoyé "
                                     f"(capture {capture}).")
                self.page.goto(url, wait_until="domcontentloaded")
                depuis["autre"], dernier_essai, essais = None, time.monotonic(), essais + 1
            self.page.wait_for_timeout(1000)
        raise ArretRobot(f"Délai de connexion dépassé ({self.delais['connexion'] // 60 or 1} min) : "
                         "relancez le robot quand vous êtes prêt.")

    def verifier_entreprise(self, nom: str) -> None:
        texte = self.page.evaluate(ETAT_PAGE_JS)["texte"]
        if _cle(nom) not in _cle(texte):
            capture = self.capturer(_slug(nom) + "-autre-entreprise", f"{nom} · entreprise différente")
            raise ArretRobot(f"Page inattendue : le formulaire ne mentionne pas {nom}. Arrêt, rien n'a été envoyé "
                             f"(capture {capture}).")

    def demander_cv(self, nom: str, documents: list[dict], raison: str) -> None:
        """Choix du CV ambigu : le robot s'arrête sur le formulaire, sans rien cocher, et vous demande."""
        liste = " ; ".join(_libelle(d, documents) for d in documents)
        capture = self.capturer(_slug(nom) + "-cv-a-choisir", f"{nom} · choix du CV demandé", pleine_page=True)
        self.noter(f"{nom} : choix du CV ambigu, {raison} ({liste}). Le robot ne coche rien et attend votre choix, "
                   f"dans JobHunter ou dans sa fenêtre (capture {capture}).")
        self.ecrire_etat(attente="cv", documents=[{"nom": d["nom"], "libelle": _libelle(d, documents)} for d in documents],
                         etape=f"{nom} : quel document joindre ? Le robot attend votre choix.")
        texte = f"Choix du CV ambigu ({raison}) : quel document joindre à la candidature ?"
        boutons = [(f"cv:{i}", f"Joindre « {_libelle(d, documents)} »") for i, d in enumerate(documents)]
        fin = time.monotonic() + self.delais["decision"]
        while time.monotonic() < fin:
            action = self.lire_decision()
            if action == "arreter":
                raise ArretRobot("Arrêt demandé pendant le choix du CV : rien n'a été coché ni envoyé.")
            if action and (choix := CHOIX_CV.fullmatch(action)) and int(choix[1]) < len(documents):
                document = documents[int(choix[1])]
                self.cv_choisi = (document["nom"], document.get("detail", ""))
                self.ecrire_etat(attente=None, documents=None)
                self.noter(f"Vous avez choisi « {_libelle(document, documents)} » : ce document sera joint pour la suite "
                           "de cette exécution. Pour ne plus avoir à choisir, indiquez son nom dans Paramètres "
                           "(Nom du CV à joindre).")
                return
            if self.etat_page() == "connexion":
                raise ArretRobot("La session France Travail s'est terminée pendant le choix du CV : relancez le robot "
                                 "et reconnectez-vous. Rien n'a été envoyé.")
            self.bandeau(texte, boutons + [("arreter", "Arrêter le robot")])
            self.page.wait_for_timeout(700)
        raise ArretRobot(f"Aucun choix de CV depuis {self.delais['decision'] // 60 or 1} min pour {nom} : le robot "
                         "s'arrête, rien n'a été coché ni envoyé.")

    def cocher_cv(self, nom: str) -> str:
        if self.page.locator("#no-documents").count():
            raise ArretRobot("Aucun document dans votre espace La Bonne Boîte : ajoutez votre CV dans « Vos documents » "
                             "sur La Bonne Boîte, puis relancez. Le robot ne téléverse jamais de fichier.")
        documents = self.page.evaluate(DOCUMENTS_JS)
        cv, raison = choix_cv(documents, self.travail["nom_cv"], self.cv_choisi)
        if cv is None:
            self.demander_cv(nom, documents, raison)
            documents = self.page.evaluate(DOCUMENTS_JS)  # relus : la page a pu changer pendant votre choix
            cv, raison = choix_cv(documents, self.travail["nom_cv"], self.cv_choisi)
            if cv is None:
                raise ArretRobot(f"Les documents ont changé pendant votre choix pour {nom} ({raison}) : arrêt, "
                                 "rien n'a été coché ni envoyé.")
        case = self.page.locator(f'input[id="{cv["id"]}"]')
        try:
            case.check(timeout=5000)
        except Exception:
            if self.page.is_closed():
                raise
            self.noter("Un élément masque peut-être le formulaire (bandeau cookies ?) : fermez-le dans la fenêtre du robot.")
            self.bandeau("Un élément masque le formulaire : fermez-le (bandeau cookies ?), le robot reprend ensuite.")
            case.check(timeout=60000)  # au-delà, page inattendue (traiter)
        for autre in documents:
            if autre["id"] != cv["id"] and self.page.locator(f'input[id="{autre["id"]}"]').is_checked():
                self.page.locator(f'input[id="{autre["id"]}"]').uncheck(timeout=5000)
        coches = [d["nom"] for d in self.page.evaluate(DOCUMENTS_JS) if d["coche"]]
        if coches != [cv["nom"]]:
            raise ArretRobot(f"Le CV n'a pas pu être coché seul pour {nom} (cochés : {', '.join(coches) or 'aucun'}). "
                             "Arrêt, rien n'a été envoyé.")
        return cv["nom"]

    def coller_message(self, entreprise: dict) -> None:
        zone = self.page.locator("#message-textarea")
        zone.fill(entreprise["message"], timeout=15000)
        zone.blur()
        self.page.wait_for_timeout(300)
        if zone.input_value() != entreprise["message"]:
            capture = self.capturer(_slug(entreprise["entreprise"]) + "-message-modifie", "message modifié par le site")
            raise ArretRobot(f"Le champ du message n'a pas gardé le texte tel quel pour {entreprise['entreprise']} : "
                             f"arrêt, rien n'a été envoyé (capture {capture}).")

    def traiter(self, entreprise: dict, index: int, total: int, deja_ouvert: bool) -> str:
        from playwright.sync_api import Error as PlaywrightError

        try:
            return self._traiter(entreprise, index, total, deja_ouvert)
        except PlaywrightError as exc:
            if self.page.is_closed():
                raise
            nom = entreprise["entreprise"]
            capture = self.capturer(_slug(nom) + "-page-inattendue", f"{nom} · page inattendue")
            raise ArretRobot(f"Page inattendue pour {nom} pendant l'étape « {self.etat.get('etape')} » "
                             f"({str(exc).splitlines()[0][:200]}). Arrêt, rien n'a été envoyé (capture {capture}).") from None

    def _traiter(self, entreprise: dict, index: int, total: int, deja_ouvert: bool) -> str:
        nom, slug, siret = entreprise["entreprise"], _slug(entreprise["entreprise"]), entreprise["siret"]
        self.ecrire_etat(entreprise=nom, siret=siret, attente=None, etape=f"{index}/{total} · {nom} : ouverture du formulaire")
        self.bandeau(f"Robot en cours : {nom} ({index}/{total}). Ne touchez pas au formulaire pendant ce temps.")
        if not deja_ouvert:
            self.page.goto(entreprise["url_formulaire"], wait_until="domcontentloaded")
        etat = self.attendre_page({"formulaire", "synthese", "brouillon"}, "le formulaire de candidature", nom)
        if etat == "synthese":
            capture = self.capturer(slug + "-deja-envoyee", f"{nom} · candidature déjà envoyée selon La Bonne Boîte")
            noter_robot(self.db_path, siret, "La Bonne Boîte indique une candidature déjà envoyée à cette entreprise.",
                        statut=DEJA_CONTACTEE)
            self.noter(f"{nom} : La Bonne Boîte indique une candidature déjà envoyée ; entreprise passée (capture {capture}).")
            return "deja"
        if etat == "brouillon":
            capture = self.capturer(slug + "-brouillon", f"{nom} · brouillon déjà commencé sur La Bonne Boîte")
            noter_robot(self.db_path, siret, "Un brouillon existe déjà sur La Bonne Boîte : terminez-le vous-même.")
            self.noter(f"{nom} : un brouillon existe déjà sur La Bonne Boîte ; entreprise passée (capture {capture}).")
            return "passees"
        self.verifier_entreprise(nom)
        capture = self.capturer(slug + "-formulaire", f"{nom} · formulaire ouvert")
        self.noter(f"{nom} : formulaire ouvert (capture {capture}).")
        self.pause()
        self.ecrire_etat(etape=f"{index}/{total} · {nom} : sélection du CV")
        cv = self.cocher_cv(nom)
        capture = self.capturer(slug + "-cv", f"{nom} · CV « {cv} » coché")
        self.noter(f"{nom} : CV « {cv} » coché (capture {capture}).")
        self.pause()
        self.ecrire_etat(etape=f"{index}/{total} · {nom} : message")
        self.coller_message(entreprise)
        capture = self.capturer(slug + "-message", f"{nom} · message collé")
        self.noter(f"{nom} : message collé, {len(entreprise['message'])} caractères (capture {capture}).")
        self.pause()
        self.verifier_captcha(nom)
        automatique = self.travail["envoi_automatique"]
        capture = self.capturer(slug + "-pret", f"{nom} · formulaire prêt" + ("" if automatique else ", arrêt avant l'envoi"),
                                pleine_page=True)
        self.noter(f"{nom} : formulaire prêt ; " + ("envoi automatique activé" if automatique else
                                                    "le robot s'arrête avant l'envoi") + f" (capture {capture}).")
        if self.travail["mode"] == "essai":
            noter_robot(self.db_path, siret, f"Essai du {datetime.now().strftime('%d/%m/%Y')} : formulaire rempli "
                                             "par le robot, non envoyé.")
            return "preparees"
        if self.travail["envoi_automatique"]:
            return self.envoyer(entreprise, slug)
        return self.attendre_decision(entreprise, slug)

    def envoyer(self, entreprise: dict, slug: str) -> str:
        """Envoi automatique (réglage activé) : synthèse vérifiée, envoi, confirmation exigée."""
        nom = entreprise["entreprise"]
        if envois_du_jour(self.db_path) >= self.travail["plafond"]:
            raise ArretRobot(f"Plafond du jour atteint ({self.travail['plafond']} envois) : arrêt avant d'envoyer à {nom}.")
        bouton = self.page.locator("#send-application")
        if bouton.inner_text(timeout=5000).strip() != "Continuer":
            raise ArretRobot(f"Page inattendue pour {nom} : bouton « Continuer » introuvable. Rien n'a été envoyé.")
        self.ecrire_etat(etape=f"{nom} : envoi automatique (synthèse)")
        bouton.click(timeout=15000)
        self.attendre_page({"synthese"}, "la synthèse de la candidature", nom)
        if self.page.locator("textarea[readonly]").input_value(timeout=5000).strip() != entreprise["message"].strip():
            raise ArretRobot(f"La synthèse ne montre pas le message attendu pour {nom} : candidature non envoyée "
                             "(un brouillon reste sur La Bonne Boîte).")
        capture = self.capturer(slug + "-synthese", f"{nom} · synthèse avant envoi", pleine_page=True)
        self.noter(f"{nom} : synthèse vérifiée (capture {capture}).")
        self.pause()
        bouton = self.page.locator("#send-application")
        if "Envoyer la candidature" not in bouton.inner_text(timeout=5000):
            raise ArretRobot(f"Page inattendue pour {nom} : bouton d'envoi introuvable. Rien n'a été envoyé.")
        bouton.click(timeout=15000)
        self.attendre_page({"envoyee"}, "la confirmation d'envoi", nom)
        if "a bien été envoyée" not in self.page.evaluate(ETAT_PAGE_JS)["texte"]:
            raise ArretRobot(f"Confirmation d'envoi introuvable pour {nom} : vérifiez « Gérer vos candidatures » "
                             "sur La Bonne Boîte.")
        capture = self.capturer(slug + "-envoyee", f"{nom} · candidature envoyée par le robot")
        marquer_envoyee(self.db_path, entreprise["siret"], par="robot", message=entreprise["message"])
        self.noter(f"{nom} : candidature envoyée par le robot (envoi automatique), relance dans 10 jours "
                   f"(capture {capture}).")
        return "envoyees"

    def attendre_decision(self, entreprise: dict, slug: str) -> str:
        """Mode assisté : vous envoyez vous-même dans la fenêtre, ou vous passez ; le robot attend."""
        nom = entreprise["entreprise"]
        texte = (f"Formulaire prêt pour {nom}. Relisez-le, puis envoyez-le vous-même (« Continuer », puis "
                 "« Envoyer la candidature »), ou passez.")
        boutons = (("envoyee", "J'ai envoyé cette candidature"), ("passer", "Passer"), ("arreter", "Arrêter le robot"))
        self.ecrire_etat(attente="decision", etape=f"{nom} : formulaire prêt. Relisez-le dans la fenêtre du robot "
                                                   "et envoyez-le vous-même, ou passez.")
        fin = time.monotonic() + self.delais["decision"]
        while time.monotonic() < fin:
            action = self.lire_decision()
            if action == "arreter":
                raise ArretRobot("Arrêt demandé : le robot s'arrête.")
            if action == "passer":
                noter_robot(self.db_path, entreprise["siret"], "Passée lors du robot assisté.")
                self.noter(f"{nom} : passée, rien n'a été envoyé.")
                return "passees"
            etat = self.etat_page()
            if etat == "envoyee" or action == "envoyee":
                detail = "confirmation de La Bonne Boîte détectée" if etat == "envoyee" else "vous l'avez indiqué"
                capture = self.capturer(slug + "-envoyee", f"{nom} · candidature envoyée par vous") if etat == "envoyee" else None
                marquer_envoyee(self.db_path, entreprise["siret"], par="vous", message=entreprise["message"])
                self.noter(f"{nom} : candidature envoyée par vous ({detail}), relance dans 10 jours"
                           + (f" (capture {capture})." if capture else "."))
                self.ecrire_etat(attente=None)
                return "envoyees"
            if etat == "connexion":
                raise ArretRobot("La session France Travail s'est terminée : relancez le robot et reconnectez-vous.")
            self.bandeau(texte, boutons)
            self.page.wait_for_timeout(700)
        raise ArretRobot(f"Aucune décision depuis {self.delais['decision'] // 60 or 1} min pour {nom} : "
                         "le robot s'arrête (rien n'a été envoyé par le robot).")

    def parcourir(self) -> str:
        entreprises, compteurs = [], {"preparees": 0, "envoyees": 0, "passees": 0, "deja": 0, "ignorees": 0}
        for e in self.travail["entreprises"]:
            if e["problemes"]:
                noter_robot(self.db_path, e["siret"], "Message à corriger : " + e["problemes"][0])
                self.noter(f"{e['entreprise']} : ignorée, message à corriger ({e['problemes'][0]})")
                compteurs["ignorees"] += 1
            else:
                entreprises.append(e)
        self.ecrire_etat(compteurs=compteurs)
        if not entreprises:
            raise ArretRobot("Aucun message utilisable : corrigez le modèle de message, puis relancez.")
        self.connexion(entreprises[0]["url_formulaire"])
        plafond = ""
        for index, entreprise in enumerate(entreprises, 1):
            if self.travail["mode"] != "essai" and envois_du_jour(self.db_path) >= self.travail["plafond"]:
                plafond = f" Plafond du jour atteint ({self.travail['plafond']} envois) : le robot s'est arrêté là."
                break
            if index > 1:
                self.pause_entre_entreprises()
            resultat = self.traiter(entreprise, index, len(entreprises), deja_ouvert=index == 1)
            compteurs[resultat] += 1
            self.ecrire_etat(compteurs=compteurs)
        if self.travail["mode"] == "essai":
            return "Essai terminé : formulaire rempli, le robot s'est arrêté avant l'envoi. Rien n'a été envoyé."
        return (f"Terminé : {compteurs['envoyees']} envoyée(s), {compteurs['passees']} passée(s), "
                f"{compteurs['deja']} déjà contactée(s), {compteurs['ignorees']} ignorée(s).{plafond}")

    def executer(self) -> None:
        from playwright.sync_api import Error as PlaywrightError, sync_playwright

        mode, total = self.travail["mode"], len(self.travail["entreprises"])
        if mode == "essai":
            self.noter("Essai du robot sur une entreprise : formulaire rempli puis arrêt avant l'envoi. Rien ne sera envoyé.")
        elif self.travail["envoi_automatique"]:
            self.noter(f"Robot avec envoi automatique sur {total} entreprise(s), au plus {self.travail['plafond']} "
                       "envois par jour : il enverra lui-même chaque candidature vérifiée.")
        else:
            self.noter(f"Robot assisté sur {total} entreprise(s) : il coche votre CV, colle le message et s'arrête "
                       "avant l'envoi ; vous envoyez vous-même.")
        self.noter("Navigateur neuf, sans session enregistrée : vous vous connectez vous-même avec France Travail.")
        self.ecrire_etat(pid=os.getpid(), statut="en_cours", captures=[])
        statut, resume = "erreur", "Erreur inattendue : voir le journal."
        try:
            local = urlsplit(self.site).hostname in ("127.0.0.1", "localhost")
            if (self.site != SITE_LBB and not local) or (fake_ft_enabled() and not local):
                raise ArretRobot("Adresse de La Bonne Boîte inattendue : le robot ne démarre pas.")
            with sync_playwright() as pw:
                options = {"headless": _invisible()}
                if fake_ft_enabled():  # mode test : aucune requête ne peut sortir de la machine
                    options["proxy"] = {"server": "http://127.0.0.1:9", "bypass": "127.0.0.1,localhost"}
                browser = pw.chromium.launch(**options)
                try:
                    context = browser.new_context(locale="fr-FR", viewport={"width": 1280, "height": 860})
                    self.page = context.new_page()
                    self.page.expose_binding("__jobhunterDecision", self._bouton)
                    try:
                        statut, resume = "termine", self.parcourir()
                    except ArretRobot as exc:
                        statut, resume = "arrete", str(exc)
                    except PlaywrightError as exc:
                        if self.page.is_closed() or "closed" in str(exc).lower():
                            statut, resume = "arrete", "Fenêtre du robot fermée : arrêt. Rien n'a été envoyé par le robot."
                        else:
                            statut, resume = "erreur", f"Erreur du navigateur : {str(exc).splitlines()[0][:300]}"
                    self.noter(("Fin : " if statut == "termine" else "ARRÊT : " if statut == "arrete" else "ERREUR : ") + resume)
                    self.ecrire_etat(statut=statut, resume=resume, attente=None, fin=_maintenant())
                    self.garder_ouverte(resume)
                finally:
                    try:
                        browser.close()  # session détruite : rien n'est conservé
                    except Exception:
                        pass
        except ArretRobot as exc:
            statut, resume = "arrete", str(exc)
            self.noter("ARRÊT : " + resume)
        except Exception as exc:  # noqa: BLE001 - tout est consigné dans le journal
            statut, resume = "erreur", f"Erreur inattendue : {str(exc).splitlines()[0][:300] if str(exc) else type(exc).__name__}"
            self.noter("ERREUR : " + resume)
        finally:
            self.ecrire_etat(statut=statut, resume=resume, attente=None, fin=self.etat.get("fin") or _maintenant(),
                             fenetre_ouverte=False, sortie=_maintenant())

    def garder_ouverte(self, resume: str) -> None:
        """Laisse la fenêtre ouverte pour relire la page ; fermée par vous, par « Arrêter » ou après le délai."""
        if self.page is None or self.page.is_closed():
            return
        self.ecrire_etat(fenetre_ouverte=True)
        self.bandeau(resume + " Vous pouvez fermer cette fenêtre.", (("arreter", "Fermer la fenêtre"),))
        fin = time.monotonic() + self.delais["fenetre"]
        try:
            while time.monotonic() < fin and not self.page.is_closed():
                if (self.dossier / "arret").exists() or self.decision_bandeau == "arreter":
                    break
                self.page.wait_for_timeout(500)
        except Exception:
            pass  # fenêtre fermée par vous


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Robot assisté des candidatures spontanées (lancé par JobHunter).")
    parser.add_argument("--dossier", required=True, type=Path, help="dossier de l'exécution (travail.json)")
    Robot(parser.parse_args(argv).dossier).executer()


if __name__ == "__main__":
    main()
