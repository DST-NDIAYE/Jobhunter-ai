"""Robot de la campagne La Bonne Boîte : fenêtre visible, un seul onglet, processus séparé du serveur.

Parcours réel (sélecteurs fondés sur le texte visible et les rôles, jamais sur des identifiants supposés) :
formulaire /candidature/nouvelle/{SIRET} (étape 1/2 : documents ; étape 2/2 : lettre, compteur x/1500, « Continuer »),
puis Synthèse /candidature/recapitulatif/{id} et bouton d'envoi, enfin page de confirmation (règle apprise au
calibrage et validée par vous).

Modes :
- essai : une entreprise de votre choix jusqu'à la Synthèse, capture de la page entière, boutons listés, aucun envoi ;
- calibrage : vous cliquez vous-même sur le bouton d'envoi ; le robot observe et propose la règle de confirmation ;
- cinq : cinq envois automatiques devant vous ; campagne : le nombre de candidatures que vous choisissez, enchaînées
  sans pause, puis arrêt.

Sécurités : vous vous connectez vous-même (aucun mot de passe, aucune session conservée). Le robot ne s'arrête pas pour
une entreprise : en échec (page non reconnue, écart de vérification, documents absents, erreur du site…), elle est
mise de côté et reprise lors d'une prochaine exécution, une seule fois si l'échec venait d'elle (2e échec ou écart :
« erreur »). Site en difficulté (3 échecs de suite, 2 erreurs 5xx de suite, code 429) : pause, puis reprise. Captcha :
jamais résolu ni contourné ; le robot attend 20 min que vous le résolviez. Alerte, sans reprise le même jour, seulement
si le site s'y oppose : message de limite ou de blocage, code 403, captcha non résolu, code 429 répété malgré les
pauses. L'entreprise est marquée « tentée » avant le clic d'envoi : après un plantage ou un envoi incertain, elle n'est
jamais reprise. Aucune requête hors de La Bonne Boîte et de France Travail. Le journal ne contient jamais le texte des
pages ni vos coordonnées.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

from app import campagne as cp
from app.config import PROJECT_ROOT
from app.cv_pdf import OUTPUT_DIR
from app.spontanees import SITE_LBB, site_lbb
from app.test_mode import fake_ft_enabled

ROBOT_DIR = OUTPUT_DIR / "campagne"
RUN_ID = re.compile(r"\d{8}-\d{6}(?:-\d{1,2})?")
CAPTURE = re.compile(r"\d{2,4}-[a-z0-9-]{1,60}\.png")
PAGES_FICHE = 40  # pages de résultats parcourues au plus pour retrouver la fiche d'une entreprise
ATTENTES_RESULTATS = (30, 120)  # nouvel essai d'une page de résultats (lecture seule) après 30 s, puis 2 min
# Le robot ne s'arrête pas pour une entreprise : elle est mise de côté et il passe à la suivante. Plusieurs échecs de
# suite (le site est sans doute en difficulté) : pause, puis reprise. Arrêt seulement si continuer est impossible.
ECHECS_AVANT_PAUSE = 3  # 3 entreprises de suite en échec (ou 2 erreurs 5xx de suite) : pause, puis reprise
PAUSES = (600, 1200, 1800)  # 10 min, puis 20 min, puis 30 min à chaque nouvelle pause sans succès entre-temps
PAUSES_MAX = 5  # 5 pauses de suite sans aucun succès (environ 2 h de panne) : arrêt, sans alerte du jour
PAUSES_429 = (900, 1800, 3600)  # « trop de requêtes » : 15 min, 30 min, 1 h ; une 4e fois : arrêt pour la journée
INTROUVABLES_DE_SUITE = 10  # 10 fiches introuvables de suite : recherche du site en panne ? Elles restent à faire, pause
PREMIER_ECHEC = "1 échec : "  # entreprise reprise une fois lors d'une exécution suivante ; au 2e échec, « erreur »
MODES = ("essai", "calibrage", "cinq", "campagne")
HOTES = ("francetravail.fr", "francetravail.io")  # seuls domaines joignables par le navigateur du robot
# Page de connexion France Travail encore servie sous l'ancien nom (Pôle emploi) : cette adresse exacte seulement.
HOTES_EXACTS = ("authentification-candidat.pole-emploi.fr",)
CONNEXION = re.compile(r"(?i)authentification|connexion|login|oauth|auth\b")


def hote_autorise(hote: str, site: str, test: bool = False) -> bool:
    """La Bonne Boîte, France Travail (francetravail.fr, .io) et la page de connexion exacte ; en test, le faux site seul."""
    hote = (hote or "").lower()
    if hote == (site or "").lower():
        return True
    if test:
        return False
    return hote in HOTES_EXACTS or any(hote == h or hote.endswith("." + h) for h in HOTES)


def regles_dns(site: str) -> str:
    """Résolution d'adresses limitée aux sites autorisés : redirections comprises, rien d'autre n'est joignable."""
    hotes = [site] + list(HOTES_EXACTS) + [p for h in HOTES for p in (h, "*." + h)]
    return "MAP * ~NOTFOUND, " + ", ".join(f"EXCLUDE {h}" for h in dict.fromkeys(hotes))


def est_connexion(hote: str, chemin: str = "") -> bool:
    """Page de connexion France Travail (ou son ancienne adresse Pôle emploi)."""
    hote = (hote or "").lower()
    return hote in HOTES_EXACTS or (any(hote == h or hote.endswith("." + h) for h in HOTES) and
                                   bool(CONNEXION.search(hote + chemin)))
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
    tmp.replace(path)  # jamais de fichier à moitié écrit


def _accelere() -> float:
    """Temps accéléré (tests uniquement, en mode test) : les pauses sont divisées par ce facteur."""
    try:
        facteur = float(os.getenv("JOBHUNTER_ROBOT_ACCELERE") or 1)
    except ValueError:
        facteur = 1
    return max(1.0, facteur) if fake_ft_enabled() else 1.0


# --- Côté serveur : lancement, état, pause, arrêt ----------------------------------------------------------------

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
    if etat.get("sortie"):
        return False
    if (proc := _PROCESSUS.get(run.name)) is not None:
        return proc.poll() is None
    pid = _pid(run, etat)
    if pid is None:
        return False
    try:
        os.kill(pid, 0)  # lancé par une instance précédente du serveur
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def robot_actif() -> bool:
    run = _dernier()
    return run is not None and _actif(run, _lire(run / "etat.json", {}) or {})


def etat_robot() -> dict:
    """Dernière exécution : statut, étape, attente, compteurs, journal et captures."""
    run = _dernier()
    if run is None:
        return {"actif": False, "id": None, "journal": [], "captures": [], "statut": None}
    etat = _lire(run / "etat.json", {}) or {}
    actif = _actif(run, etat)
    if not actif and etat.get("statut") in ("en_cours", "en_pause"):
        etat.update(statut="erreur", attente=None,
                    resume="Le robot s'est arrêté de façon inattendue : voir le journal. Les entreprises en cours "
                           "ne seront jamais reprises.")
    try:
        journal = (run / "journal.txt").read_text(encoding="utf-8").splitlines()[-400:]
    except OSError:
        journal = []
    captures = [{"nom": c["nom"], "legende": c.get("legende") or "",
                 "url": f"/api/campagne/robot/captures/{run.name}/{c['nom']}"}
                for c in etat.get("captures") or [] if isinstance(c, dict) and CAPTURE.fullmatch(str(c.get("nom")))]
    public = {k: etat.get(k) for k in ("mode", "statut", "etape", "attente", "entreprise", "debut", "fin", "resume",
                                       "alerte", "compteurs", "boutons", "fenetre_ouverte", "pause_demandee")}
    public["pause_demandee"] = (run / "pause").exists()
    return {**public, "actif": actif, "id": run.name, "journal": journal, "captures": captures[-60:]}


def lancer_robot(settings, mode: str, siret: str | None = None, nombre: int | None = None) -> dict:
    """Vérifie que le mode est permis (premier passage, alerte) et lance le robot ; en campagne, il envoie « nombre »
    candidatures (le dernier nombre choisi s'il n'est pas donné), puis s'arrête."""
    if mode not in MODES:
        raise cp.CampagneError("Mode du robot inconnu.")
    if robot_actif():
        raise cp.CampagneError("Le robot est déjà lancé : arrêtez-le avant d'en relancer un.", 409)
    db_path = settings.database_path
    cp.reprise_apres_plantage(db_path)
    reglages, passage = cp.lire_reglages(db_path), cp.lire_passage(db_path)
    alerte = cp.alerte_du_jour(db_path)
    if alerte and (mode in ("cinq", "campagne") or alerte["bloque_essai"]):
        raise cp.CampagneError(f"Alerte aujourd'hui ({alerte['libelle']} : {alerte['raison']}). Aucune reprise "
                               "avant demain.", 409)
    etape = cp.etape_passage(passage)
    besoin = {"essai": 2, "calibrage": 3, "cinq": 4, "campagne": 5}[mode]
    if etape < besoin and not (mode == "essai" and etape >= 2):
        noms = {1: "valider la liste des métiers", 2: "faire l'essai sur une entreprise",
                3: "faire le calibrage et valider la règle de confirmation", 4: "valider les cinq envois devant vous"}
        raise cp.CampagneError(f"Premier passage : il faut d'abord {noms[etape]}.", 409)
    if not reglages["nom_cv"]:
        raise cp.CampagneError("Indiquez le nom du CV à joindre dans les réglages de la campagne.", 409)
    versions = cp.charger_modeles()
    if problemes := cp.problemes_modele(versions.get("A")):
        raise cp.CampagneError("Modèle de message à corriger : " + problemes[0], 409)
    phases = [p for p in ("A", "B") if reglages[f"phase_{p.lower()}"]]
    if mode in ("essai", "calibrage"):
        if not siret:
            raise cp.CampagneError("Choisissez l'entreprise.")
        cible = cp.entreprise_par_siret(db_path, siret)
        if cible["statut"] not in (cp.A_FAIRE, cp.BROUILLON):
            raise cp.CampagneError(f"Entreprise déjà traitée ({cible['statut_libelle']}) : choisissez-en une autre.", 409)
    else:
        if mode == "campagne" and nombre is not None:
            reglages = cp.ecrire_reglages(db_path, {"objectif": nombre})  # nombre gardé pour le prochain lancement
        if not cp.prochaines(db_path, phases):
            raise cp.CampagneError("Aucune entreprise à faire dans les phases actives : lancez l'inventaire.", 409)
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = ROBOT_DIR / run_id
    for suffix in range(1, 100):
        if not folder.exists():
            break
        folder = ROBOT_DIR / f"{run_id}-{suffix}"
    folder.mkdir(parents=True)
    objectif = {"cinq": 5, "campagne": reglages["objectif"]}.get(mode)
    _ecrire(folder / "travail.json", {
        "mode": mode, "siret": siret, "reglages": reglages, "phases": phases, "site": site_lbb(), "base": str(db_path),
        "regle": passage["regle"] if passage["regle_validee"] else None, "objectif": objectif})
    _ecrire(folder / "etat.json", {"mode": mode, "statut": "en_cours", "etape": "Démarrage du robot…",
                                   "debut": _maintenant(), "captures": []})
    with open(folder / "robot.log", "ab") as log:
        proc = subprocess.Popen([sys.executable, "-m", "app.robot_campagne", "--dossier", str(folder)],
                                cwd=PROJECT_ROOT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    _PROCESSUS[folder.name] = proc
    (folder / "pid").write_text(str(proc.pid), encoding="utf-8")
    if sys.platform == "darwin" and not fake_ft_enabled():
        try:  # le Mac ne se met pas en veille tant que le robot tourne
            subprocess.Popen(["caffeinate", "-i", "-w", str(proc.pid)], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            pass
    return etat_robot()


def pause_robot(active: bool) -> dict:
    """Pause (prise en compte entre deux entreprises, jamais entre le clic d'envoi et la confirmation) ou reprise."""
    run = _dernier()
    if run is None or not _actif(run, _lire(run / "etat.json", {}) or {}):
        raise cp.CampagneError("Le robot n'est pas lancé.", 409)
    if active:
        (run / "pause").write_text(_maintenant(), encoding="utf-8")
    else:
        (run / "pause").unlink(missing_ok=True)
    return etat_robot()


def decider(action: str) -> dict:
    """Calibrage : « passer » garde le brouillon sans l'envoyer."""
    if action != "passer":
        raise cp.CampagneError("Décision inconnue.")
    run = _dernier()
    etat = (_lire(run / "etat.json", {}) or {}) if run else {}
    if run is None or not _actif(run, etat) or etat.get("attente") != "clic":
        raise cp.CampagneError("Le robot n'attend pas votre clic en ce moment.", 409)
    _ecrire(run / "decision.json", {"action": action})
    return etat_robot()


def arreter_robot() -> dict:
    """Arrêt demandé : pris en compte à la prochaine vérification (forcé après 30 s, jamais pendant un envoi)."""
    run = _dernier()
    etat = (_lire(run / "etat.json", {}) or {}) if run else {}
    if run is not None and _actif(run, etat):
        (run / "arret").write_text(_maintenant(), encoding="utf-8")
        timer = threading.Timer(30, _forcer_arret, args=(run, _pid(run, etat)))
        timer.daemon = True
        timer.start()
    return etat_robot()


def _forcer_arret(run: Path, pid) -> None:
    etat = _lire(run / "etat.json", {}) or {}
    if pid is not None and _actif(run, etat) and etat.get("attente") != "envoi":
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass


def chemin_capture(run_id: str, nom: str) -> Path:
    path = ROBOT_DIR / run_id / nom
    if not RUN_ID.fullmatch(run_id) or not CAPTURE.fullmatch(nom) or not path.is_file():
        raise cp.CampagneError("Capture introuvable.", 404)
    return path


# --- Côté robot -------------------------------------------------------------------------------------------------

def delai_fenetre(mode: str) -> int:
    """Secondes pendant lesquelles la fenêtre reste ouverte à la fin : 1 min après une campagne ou les cinq envois,
    10 min après un essai ou un calibrage (le temps de relire la Synthèse, voire d'envoyer vous-même)."""
    return 600 if mode in ("essai", "calibrage") else 60


class ArretRobot(Exception):
    """Arrêt du robot ; avec une alerte, aucune reprise de la campagne le même jour."""

    def __init__(self, message: str, alerte: str | None = None, liberer: bool = False):
        super().__init__(message)
        self.alerte = alerte
        self.liberer = liberer  # entreprise rendue « à faire » si rien n'a encore été créé sur le site


class EchecEntreprise(ArretRobot):
    """Échec limité à une entreprise : elle est mise de côté (reprise plus tard, ou « erreur ») et le robot continue.
    « definitif » : jamais reprise (écart : un nouvel essai referait le même) ; « passager » : le site ou la session
    en cause, pas l'entreprise (aucun échec compté contre elle)."""

    def __init__(self, message: str, nature: str, *, definitif: bool = False, passager: bool = False):
        super().__init__(message, liberer=True)
        self.nature, self.definitif, self.passager = nature, definitif, passager


class Ralenti(EchecEntreprise):
    """Code 429 (trop de requêtes) : l'entreprise est reprise plus tard, le robot attend puis continue."""

    def __init__(self, message: str):
        super().__init__(message, "code 429", passager=True)


class _SyntheseAbsente(Exception):
    """La Synthèse n'est pas apparue après « Continuer » (le formulaire est resté affiché)."""


class FicheIntrouvable(Exception):
    """Fiche de l'entreprise introuvable dans la recherche du site : rien n'est ouvert, l'entreprise est ignorée
    (« introuvable »), sans compter comme une erreur ni bloquer la journée."""


class ErreurServeur(Exception):
    """Page reçue avec un code 5xx (le site ne répond pas correctement) : erreur comptée, pas une page inconnue."""


class DelaiDepasse(Exception):
    """Page de résultats restée incomplète (le site ne l'a pas affichée à temps)."""


class SessionPerdue(Exception):
    """Page de connexion France Travail affichée : le robot attend votre reconnexion."""


def _secondes(retry_after: str | None) -> float | None:
    """En-tête Retry-After : un nombre de secondes ou une date HTTP ; illisible : rien."""
    if not retry_after:
        return None
    try:
        return max(0.0, float(retry_after))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        return max(0.0, (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def _capture(fichier: str | None) -> str:
    """Pour le journal et les alertes : « capture 07-x.png », ou « capture impossible » (page fermée, capture ratée)."""
    return f"capture {fichier}" if fichier else "capture impossible"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", cp._cle(text)).strip("-")[:40] or "entreprise"


def _invisible() -> bool:
    """Navigateur sans fenêtre : uniquement pour les tests automatiques, en mode test."""
    return os.getenv("JOBHUNTER_ROBOT_INVISIBLE") == "1" and fake_ft_enabled()


def _nom_document(nom: str) -> str:
    """« CV_Data (document PDF) » ou « CV_Data.pdf » → « CV_Data »."""
    return re.sub(r"\.[A-Za-z0-9]{2,4}$", "", re.sub(r"\s*\(document [^)]*\)\s*$", "", nom or "").strip())


def choix_cv(documents: list[dict], nom_cv: str) -> tuple[dict | None, str]:
    """Le CV désigné : nom exact (casse, accents, extension ignorés), sinon seul nom qui le contient."""
    cle = cp._cle(_nom_document(nom_cv))
    if not cle:
        return None, "aucun nom de CV dans les réglages"
    exacts = [d for d in documents if cp._cle(_nom_document(d["nom"])) == cle]
    trouves = exacts or [d for d in documents if cle in cp._cle(d["nom"])]
    if len(trouves) == 1:
        return trouves[0], ""
    noms = " ; ".join(d["nom"] for d in documents) or "aucun document"
    return None, (f"aucun document ne correspond au CV « {nom_cv} » ({noms})" if not trouves else
                  f"{len(trouves)} documents correspondent au CV « {nom_cv} » ({noms})")


PAGE_JS = r"""() => {
  const visible = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const t = (document.body && document.body.innerText) || '';
  const titres = [...document.querySelectorAll('h1, h2')].filter(visible).map((e) => e.innerText.replace(/\s+/g, ' ').trim());
  const boutons = [...document.querySelectorAll('button, a, input[type=submit], [role=button]')].filter(visible)
    .map((b) => (b.innerText || b.value || b.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim()).filter(Boolean);
  return {
    titres, longueur: t.trim().length,
    sansEmail: /Aucune adresse e-?mail n.a été fournie/i.test(t),
    deja: /Vous avez déjà débuté ou envoyé une candidature/i.test(t),
    debuter: boutons.some((b) => /^Débuter votre candidature spontanée$/i.test(b)),
    formulaire: titres.some((x) => /Débuter votre candidature spontanée/i.test(x)) && /étape 1\s*\/\s*2/i.test(t)
      && !!document.querySelector('textarea'),
    synthese: titres.some((x) => /Synthèse de votre candidature spontanée/i.test(x)),
    recherche: /Résultats de votre recherche/i.test(t) || titres.some((x) => /^\d[\d\s]*entreprises?\s*:/i.test(x)),
    siret: ((t.match(/Num[ée]ro de siret\s*:?\s*([\d ]{14,20})/i) || [])[1] || '').replace(/\s/g, ''),
    blocage: /(trop de candidatures|limite (de candidatures|journalière|quotidienne|atteinte)|nombre maximum de candidatures|compte (a été )?(bloqué|suspendu)|accès (est |a été )?(bloqué|refusé|restreint)|activité inhabituelle|too many requests)/i.test(t),
    inactif: /(n.accepte (plus|pas) de candidatures|candidatures? (spontanées? )?(est |sont )?(indisponibles?|désactivées?|fermées?))/i.test(t),
    erreur: /(une erreur (est survenue|s.est produite)|page (introuvable|non trouvée)|erreur (interne|500)|service (momentanément )?indisponible)/i.test(t),
  };
}"""

# Résultats de la recherche : nombre annoncé (« 54 entreprises : … ») et entreprises affichées sur la page, comptées
# par leur ligne « Ville - 75 » (aucune fiche n'est ouverte à ce moment-là).
RESULTATS_JS = r"""() => {
  const t = (document.body && document.body.innerText) || '';
  const annonce = t.match(/(?:^|\n)\s*(\d[\d\s]*?)\s+entreprises?\s*:/i);
  const aucun = /aucuns?\s+r[ée]sultats?\s+trouv/i.test(t);  // « Aucun résultats trouvés » : liste vide
  const lignes = t.split('\n').map((l) => l.trim()).filter(Boolean);
  // Nom de chaque entreprise de la liste : la ligne qui précède sa ligne « Ville - 75 » (ou « Ville - 1 » dans l'Ain).
  const noms = [];
  lignes.forEach((l, i) => { if (i > 0 && /^\S.*\s-\s(\d{1,3}|2A|2B)$/.test(l)) noms.push(lignes[i - 1]); });
  return { total: annonce ? Number(annonce[1].replace(/\s/g, '')) : (aucun ? 0 : null), noms };
}"""

CAPTCHA_JS = r"""() => {
  const sel = 'iframe[src*="captcha" i], iframe[title*="captcha" i], iframe[src*="challenges.cloudflare.com"],'
    + ' .g-recaptcha, .h-captcha, .cf-turnstile, .frc-captcha, [id*="captcha" i], [class*="captcha" i], [name*="captcha" i]';
  const visible = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  if ([...document.querySelectorAll(sel)].some(visible)) return true;
  const t = (document.body && document.body.innerText || '').toLowerCase();
  return /captcha|je ne suis pas un robot|vérification de sécurité|vérifiez que vous êtes (un )?humain|verify you are human/.test(t);
}"""

# Section d'une étape : le plus petit bloc qui contient son titre et l'élément cherché (aucun identifiant supposé).
SECTION_JS = r"""
const section = (motif, sel) => {
  const titre = [...document.querySelectorAll('h1, h2, h3')].find((h) => motif.test(h.innerText));
  if (!titre) return null;
  let el = titre.parentElement;
  while (el && !el.querySelector(sel)) el = el.parentElement;
  return el;
};"""

DOCUMENTS_JS = "() => {" + SECTION_JS + r"""
  const bloc = section(/Les documents que je souhaite joindre/i, 'input[type=checkbox]');
  if (!bloc) return null;
  // Ligne d'un document : le plus petit bloc qui contient sa case (une seule) et « (document PDF) ».
  return [...bloc.querySelectorAll('input[type=checkbox]')].map((cb, i) => {
    let ligne = cb.parentElement;
    while (ligne && ligne !== bloc && !(/\(document [^)]+\)/i.test(ligne.innerText)
           && ligne.querySelectorAll('input[type=checkbox]').length === 1)) ligne = ligne.parentElement;
    const texte = ligne && ligne !== bloc ? ligne.innerText : ((cb.labels && cb.labels[0]) ? cb.labels[0].innerText : '');
    const lignes = texte.split('\n').map((l) => l.trim()).filter(Boolean);
    const nom = (lignes.find((l) => /\(document [^)]+\)/i.test(l)) || '').replace(/^Sélectionner le document\s*/i, '');
    return { index: i, nom, coche: cb.checked };
  });
}"""

LETTRE_JS = "() => {" + SECTION_JS + r"""
  const bloc = section(/Ma lettre de motivation/i, 'textarea');
  if (!bloc) return null;
  const zones = [...bloc.querySelectorAll('textarea')];
  const compteur = (bloc.innerText.match(/(\d+)\s*\/\s*(\d+)\s*caract/i) || []);
  return { zones: zones.length, valeur: zones.length === 1 ? zones[0].value : null,
           compteur: compteur[1] ? Number(compteur[1]) : null, maximum: compteur[2] ? Number(compteur[2]) : null };
}"""

# Synthèse : seulement l'entreprise, la pièce jointe et la lettre ; jamais « Vos coordonnées ».
SYNTHESE_JS = r"""() => {
  // Valeur d'un libellé : les éléments qui le suivent jusqu'au libellé suivant (plusieurs pièces jointes comprises).
  const valeur = (motif) => {
    const label = [...document.querySelectorAll('dt, th, strong, b, span, label, p, div')]
      .find((e) => e.children.length <= 1 && motif.test(e.innerText.trim()));
    if (!label) return null;
    const parts = [];
    for (let s = label.nextElementSibling; s && !/^(STRONG|DT|TH|B|LABEL|H[1-6])$/.test(s.tagName); s = s.nextElementSibling)
      parts.push(s.innerText);
    if (parts.length) return parts.join(' ').replace(/\s+/g, ' ').trim();
    const parent = label.parentElement;
    return parent ? parent.innerText.replace(label.innerText, '').replace(/\s+/g, ' ').trim() : null;
  };
  const zones = [...document.querySelectorAll('textarea')];
  return { entreprise: valeur(/^Nom de l.entreprise\s*:?$/i), pieces: valeur(/^Pièces? jointes?\s*:?$/i),
           lettre: zones.length === 1 ? zones[0].value : null, zones: zones.length };
}"""

BOUTONS_JS = r"""() => [...document.querySelectorAll('button, a, [role=button], input[type=submit]')]
  .filter((b) => !b.closest('header, nav, footer, [role=banner], [role=navigation], [role=contentinfo], #jobhunter-bandeau'))
  .filter((b) => { const r = b.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
  .map((b) => b.innerText || b.value || '')"""

CONFIRMATION_JS = r"""() => {
  const t = (document.body && document.body.innerText) || '';
  const titres = [...document.querySelectorAll('h1, h2, h3, [role=alert], [role=status]')]
    .map((e) => e.innerText.replace(/\s+/g, ' ').trim()).filter(Boolean);
  const phrase = (t.match(/[^.\n]*envoy[ée]e?s?[^.\n]*/i) || [''])[0].replace(/\s+/g, ' ').trim();
  return { titres: titres.slice(0, 8), phrase: phrase.slice(0, 160) };
}"""

CLIC_JS = r"""() => {
  if (window.__jobhunterEcoute) return;
  window.__jobhunterEcoute = true;
  document.addEventListener('click', (ev) => {
    const b = ev.target && ev.target.closest && ev.target.closest('button, a, input[type=submit], [role=button]');
    if (b && window.__jobhunterClic) window.__jobhunterClic((b.innerText || b.value || '').replace(/\s+/g, ' ').trim());
  }, true);
}"""

BANDEAU_JS = r"""({ texte, boutons, cache }) => {
  let el = document.getElementById('jobhunter-bandeau');
  if (cache !== null) { if (el) el.style.visibility = cache ? 'hidden' : 'visible'; return; }
  const cle = JSON.stringify([texte, boutons]);
  if (el && el.dataset.cle === cle) return;
  if (!el) {
    el = document.createElement('div');
    el.id = 'jobhunter-bandeau';
    el.setAttribute('role', 'status');
    el.style.cssText = 'position:fixed;left:16px;right:16px;bottom:16px;z-index:2147483647;display:flex;flex-wrap:wrap;'
      + 'gap:10px;align-items:center;padding:12px 16px;border-radius:10px;background:#1b1f24;color:#fff;'
      + 'font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;box-shadow:0 6px 24px rgba(0,0,0,.3)';
    document.body.appendChild(el);
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
    btn.style.cssText = 'font:inherit;font-weight:600;border:0;border-radius:8px;padding:8px 12px;cursor:pointer;background:#fff;color:#1b1f24';
    btn.addEventListener('click', () => window.__jobhunterDecision && window.__jobhunterDecision(action));
    el.append(btn);
  }
}"""


class Robot:
    """Une exécution : lit travail.json ; écrit etat.json, journal.txt et les captures à chaque étape."""

    def __init__(self, dossier: Path):
        self.dossier = dossier
        self.travail = json.loads((dossier / "travail.json").read_text(encoding="utf-8"))
        self.mode = self.travail["mode"]
        self.db = Path(self.travail["base"])
        self.site = self.travail["site"]
        self.hote = urlsplit(self.site).hostname
        self.reglages = self.travail["reglages"]
        self.regle = self.travail.get("regle")
        self.etat = _lire(dossier / "etat.json", {}) or {}
        self.captures, self.n_captures = [], 0
        self.page = None
        self.brouillon = False
        self.decision_bandeau = None
        self.clic = None
        self.http = None  # premier code 429 ou 403 reçu de La Bonne Boîte ou de France Travail
        self.erreur_http = None  # dernière réponse en erreur (page ≥ 400, API du site ≥ 500) pour l'entreprise en cours
        self.pages_total = self.pages_entreprise = 0  # pages chargées sur La Bonne Boîte (requêtes de page)
        self.bloques: set[str] = set()
        self.navigation_bloquee = None  # dernière page (pas une ressource) bloquée : hôte et chemin, sans paramètres
        self.facteur = _accelere()
        test = fake_ft_enabled()
        self.delais = ({"connexion": 60, "reconnexion": 20, "clic": 60, "page": 6, "action": 2500,
                        "fenetre": float(os.getenv("JOBHUNTER_ROBOT_FENETRE") or 1)} if test
                       else {"connexion": 1200, "reconnexion": 1200, "clic": 1200, "page": 30,
                             "fenetre": delai_fenetre(self.mode),
                             "action": 10000})
        self.compteurs = {"envoyees": 0, "incertaines": 0, "ignorees": 0, "erreurs": 0}
        self.echecs_suite = self.erreurs_5xx_suite = self.pauses_sans_succes = self.ralentis_suite = 0
        self.serie_introuvables: list[str] = []  # fiches introuvables de suite (SIRET)
        self.exclues: set[str] = set()  # entreprises mises de côté pendant cette exécution (reprises plus tard)
        self.dans_entreprise = False
        self.candidature_id = None  # brouillon de l'entreprise en cours, une fois la Synthèse atteinte
        self.pages_connues: dict[str, dict] = {}  # pages de résultats lues, par métier et département
        self.etape_courante = "début"
        # Captures des étapes normales pour l'essai et le calibrage seulement ; en campagne, seulement les erreurs.
        self.capturer_envoi = self.mode in ("essai", "calibrage")
        versions = cp.charger_modeles()
        self.modeles = versions

    # Journal, état, captures, bandeau

    def noter(self, message: str) -> None:
        with open(self.dossier / "journal.txt", "a", encoding="utf-8") as journal:
            journal.write(f"{datetime.now().strftime('%H:%M:%S')}  {message}\n")

    def ecrire_etat(self, **changes) -> None:
        self.etat.update(changes)
        _ecrire(self.dossier / "etat.json", self.etat)

    def capturer(self, nom: str, legende: str, *, force: bool = False, pleine: bool = False) -> str | None:
        if not (force or self.capturer_envoi) or self.page is None or self.page.is_closed():
            return None
        self.n_captures += 1
        fichier = f"{self.n_captures:02d}-{_slug(nom)}.png"
        try:
            self._bandeau(cache=True)
            self.page.screenshot(path=str(self.dossier / fichier), full_page=pleine)
        except Exception:  # noqa: BLE001 - une capture manquée ne doit pas masquer l'erreur
            return None
        finally:
            self._bandeau(cache=False)
        self.captures.append({"nom": fichier, "legende": legende})
        self.ecrire_etat(captures=self.captures)
        return fichier

    def _bandeau(self, texte: str = "", boutons=(), cache=None) -> None:
        try:
            self.page.evaluate(BANDEAU_JS, {"texte": texte, "boutons": [list(b) for b in boutons], "cache": cache})
        except Exception:  # noqa: BLE001 - page en cours de chargement
            pass

    def _decision(self, source, action) -> None:
        if action in ("passer", "arreter"):
            self.decision_bandeau = action

    def _clic(self, source, texte) -> None:
        if isinstance(texte, str) and texte:
            self.clic = texte[:120]

    # Temps : pauses, arrêt, pause demandée

    def verifier_arret(self) -> None:
        if (self.dossier / "arret").exists() or self.decision_bandeau == "arreter":
            raise ArretRobot("Arrêt demandé : le robot s'arrête.", liberer=True)

    def attendre(self, secondes: float) -> None:
        fin = time.monotonic() + secondes / self.facteur
        while (reste := fin - time.monotonic()) > 0:
            self.verifier_arret()
            self.page.wait_for_timeout(int(min(reste, 0.5) * 1000))

    def pause_action(self) -> None:
        """Pas une pause : un court temps de réaction après chaque action, le temps que la page se mette à jour."""
        self.attendre(0.3)

    def point_de_pause(self) -> None:
        """Pause demandée depuis l'écran Campagne : prise en compte ici, entre deux entreprises."""
        if not (self.dossier / "pause").exists():
            return
        self.noter("Pause demandée : le robot attend votre reprise.")
        self.ecrire_etat(statut="en_pause", attente="pause", etape="En pause : cliquez sur « Reprendre ».")
        while (self.dossier / "pause").exists():
            self.verifier_arret()
            self._bandeau("Campagne en pause. Reprenez depuis JobHunter.")
            self.page.wait_for_timeout(500)
        self.ecrire_etat(statut="en_cours", attente=None)
        self.noter("Reprise de la campagne.")

    # Lecture de la page

    def lire(self) -> dict:
        try:
            return self.page.evaluate(PAGE_JS)
        except Exception:  # noqa: BLE001 - page en cours de navigation
            return {"titres": [], "longueur": 0}

    def captcha(self) -> bool:
        if any(re.search(r"(?i)captcha|challenges\.cloudflare\.com", frame.url) for frame in self.page.frames):
            return True
        try:
            return bool(self.page.evaluate(CAPTCHA_JS))
        except Exception:  # noqa: BLE001
            return False

    def etat_page(self) -> str:
        url = urlsplit(self.page.url)
        if url.scheme == "chrome-error" and self.navigation_bloquee:
            return "bloquee"
        if url.hostname != self.hote:
            return "connexion" if hote_autorise(url.hostname, self.hote) else "autre"
        if url.path.startswith(("/login", "/auth", "/connexion")):
            return "connexion"
        if self.captcha():
            return "captcha"
        if self.http:
            return "http"
        page = self.lire()
        if page.get("blocage"):
            return "blocage"
        if url.path.startswith("/errors/") or page.get("erreur"):
            return "erreur"
        if url.path.startswith("/candidature/editer/") or page.get("deja"):
            return "deja"
        if page.get("sansEmail"):
            return "sans_email"
        if page.get("inactif"):
            return "inactif"
        if page.get("synthese") and url.path.startswith("/candidature/recapitulatif/"):
            return "synthese"
        if page.get("formulaire"):
            return "formulaire"
        if url.path.startswith("/recherche") and page.get("recherche"):
            return "recherche"
        if page.get("debuter"):
            return "fiche"
        if self.regle and self.confirmation():
            return "confirmation"
        if page.get("longueur", 0) < 40:
            return "chargement"
        return "inconnue"

    def alerte_page(self, etat: str, nom: str) -> None:
        """Captcha : le robot attend que vous le résolviez (il ne le résout ni ne le contourne jamais). Message de
        limite ou code 403 : arrêt et alerte du jour. Code 429 : pause, puis reprise."""
        if etat == "bloquee":
            self.arret_navigation_bloquee()
        if etat == "captcha":
            self.attendre_captcha(nom)
        if etat == "blocage":
            capture = self.capturer(f"{nom}-blocage", f"{nom} · message de limite ou de blocage", force=True)
            raise ArretRobot(f"Message de limite ou de blocage affiché ({nom}) : arrêt immédiat, pas de reprise "
                             f"aujourd'hui ({_capture(capture)}).", "blocage", liberer=True)
        if etat == "http":
            capture = self.capturer(f"{nom}-http", f"{nom} · code {self.http}", force=True)
            if self.http.startswith("429"):
                raise Ralenti(f"code {self.http} reçu de La Bonne Boîte (trop de requêtes, {_capture(capture)})")
            raise ArretRobot(f"Code {self.http} reçu de La Bonne Boîte : arrêt immédiat, pas de reprise aujourd'hui "
                             f"({_capture(capture)}).", "http", liberer=True)
        if etat == "connexion" and self.connexion_durable():
            raise SessionPerdue()

    def attendre_captcha(self, nom: str) -> None:
        """Captcha : jamais résolu ni contourné par le robot. Vous le résolvez vous-même dans la fenêtre (20 min au
        plus) : l'entreprise en cours est reprise plus tard et le robot continue. Sinon, arrêt pour la journée."""
        capture = self.capturer(f"{nom}-captcha", f"{nom} · captcha", force=True)
        self.noter(f"Captcha affiché ({nom}) : le robot ne le résout jamais. Résolvez-le vous-même dans la fenêtre du "
                   f"robot ; il attend 20 min au plus ({_capture(capture)}).")
        self.ecrire_etat(attente="captcha", etape="Captcha : résolvez-le vous-même dans la fenêtre du robot.")
        fin = time.monotonic() + self.delais["reconnexion"]
        while time.monotonic() < fin:
            self.verifier_arret()
            if not self.captcha():
                self.ecrire_etat(attente=None)
                raise EchecEntreprise(f"captcha résolu par vous ({_capture(capture)})", "captcha", passager=True)
            self.page.wait_for_timeout(1000)
        self.ecrire_etat(attente=None)
        raise ArretRobot(f"Captcha affiché ({nom}) et non résolu en 20 min : arrêt. Le robot ne résout ni ne contourne "
                         f"jamais un captcha. Pas de reprise aujourd'hui ({_capture(capture)}).", "captcha", liberer=True)

    def connexion_durable(self) -> bool:
        """Page de connexion encore là après 3 s : session perdue. Un passage éclair (rafraîchissement de la session
        par le site) ne compte pas."""
        fin = time.monotonic() + 3
        while time.monotonic() < fin:
            self.page.wait_for_timeout(400)
            if self.etat_page() != "connexion":
                return False
        return True

    def attendre_page(self, attendus: set[str], quoi: str, nom: str, sinon=None) -> str:
        """Attend une page reconnue (3 lectures identiques) ; toute autre page arrête la campagne (alerte), sauf si
        « sinon » fournit l'exception à lever à la place (page de résultats au-delà de la dernière, par exemple)."""
        fin, precedent, stable = time.monotonic() + self.delais["page"], None, 0
        while time.monotonic() < fin:
            self.verifier_arret()
            etat = self.etat_page()
            self.alerte_page(etat, nom)
            if etat not in attendus and self.page_en_erreur_serveur():
                raise ErreurServeur(self.detail_http())
            if etat in attendus:
                stable = stable + 1 if etat == precedent else 1
                if stable >= 3:
                    return etat
            elif etat not in ("chargement", "inconnue"):
                stable = 0
            precedent = etat
            self.page.wait_for_timeout(400)
        if sinon is not None:
            raise sinon()
        capture = self.capturer(f"{nom}-page-non-reconnue", f"{nom} · page non reconnue", force=True, pleine=True)
        detail = self.detail_http()
        raise EchecEntreprise(f"page non reconnue : {quoi} n'est pas apparu (adresse {urlsplit(self.page.url).path}"
                              + (f" ; {detail}" if detail else "") + f", {_capture(capture)})", "page non reconnue")

    def confirmation(self) -> bool:
        """Page de confirmation selon la règle validée ; la Synthèse (et sa « Date d'envoi ») ne compte jamais."""
        if not self.regle:
            return False
        try:
            texte = self.page.evaluate("() => (document.body && document.body.innerText) || ''")
        except Exception:  # noqa: BLE001
            return False
        return cp.est_confirmation(self.regle, urlsplit(self.page.url).path, texte)

    # Connexion

    def connexion(self, url: str, nom: str) -> None:
        """Vous vous connectez vous-même dans la fenêtre ; le robot ne remplit jamais l'identifiant ni le mot de passe."""
        self.ouvrir(url)
        self.ecrire_etat(attente="connexion", etape="Connectez-vous avec France Travail dans la fenêtre du robot.")
        self.noter("En attente de votre connexion France Travail dans la fenêtre du robot.")
        fin, essais, autre_depuis, vu_connexion = time.monotonic() + self.delais["connexion"], 0, None, False
        while time.monotonic() < fin:
            self.verifier_arret()
            etat = self.etat_page()
            vu_connexion = vu_connexion or etat == "connexion"
            if etat == "bloquee":
                self.arret_navigation_bloquee()
            if etat in ("captcha", "blocage", "http"):
                try:
                    self.alerte_page(etat, nom)
                except Ralenti:
                    self.attendre_ralenti()
                    continue
                except EchecEntreprise:  # captcha résolu par vous
                    continue
            # Revenu sur La Bonne Boîte après la page de connexion : connecté, même si la page ouverte est une erreur
            # (entreprise inconnue du site) ; la fiche de l'entreprise, vérifiée ensuite, décide de la suite.
            de_retour = vu_connexion and etat in ("erreur", "inconnue", "recherche") and \
                urlsplit(self.page.url).hostname == self.hote
            if etat in ("formulaire", "fiche", "deja", "sans_email", "inactif", "synthese") or de_retour:
                self.ecrire_etat(attente=None)
                self.noter("Connexion détectée : le robot reprend la main.")
                return
            if etat in ("inconnue", "erreur", "autre"):
                autre_depuis = autre_depuis or time.monotonic()
                if time.monotonic() - autre_depuis > 8:
                    if etat == "erreur" and urlsplit(self.page.url).hostname == self.hote:
                        # Page d'erreur de La Bonne Boîte, stable : le site a répondu sans demander la connexion.
                        self.ecrire_etat(attente=None)
                        self.noter("Connexion détectée : le robot reprend la main.")
                        return
                    if essais >= 3:
                        capture = self.capturer("page-non-reconnue", "page non reconnue après la connexion", force=True)
                        detail = self.detail_http()
                        raise ArretRobot("Page non reconnue après la connexion : le formulaire ne s'ouvre pas "
                                         f"(adresse {urlsplit(self.page.url).path}" + (f" ; {detail}" if detail else "")
                                         + f", {_capture(capture)}). Arrêt sans alerte : relancez plus tard.")
                    essais, autre_depuis = essais + 1, None
                    self.ouvrir(url)
            else:
                autre_depuis = None
            self.page.wait_for_timeout(800)
        raise ArretRobot(f"Délai de connexion dépassé ({self.delais['connexion'] // 60 or 1} min) : relancez le robot "
                         "quand vous êtes prêt.")

    def ouvrir(self, url: str) -> None:
        """Ouvre une page ; si elle mène à un site non autorisé (redirection comprise), arrêt avec un message clair."""
        from playwright.sync_api import Error as PlaywrightError

        try:
            self.page.goto(url, wait_until="domcontentloaded")
        except PlaywrightError:
            if self.navigation_bloquee and not self.page.is_closed():
                self.arret_navigation_bloquee()
            if self.http and not self.page.is_closed():  # 429 ou 403 sans page : limitation ou blocage
                self.alerte_page("http", "La Bonne Boîte")
            raise

    def arret_navigation_bloquee(self) -> None:
        """Une page entière a été bloquée par la liste des sites autorisés : le dire clairement (sans alerte du jour)."""
        hote, chemin = self.navigation_bloquee
        adresse = f"https://{hote}{chemin}"  # sans paramètres (jetons de connexion)
        capture = self.capturer("page-bloquee", "page bloquée par le robot", force=True)
        if est_connexion(hote, chemin) or CONNEXION.search(hote + chemin):
            raise ArretRobot(f"Page de connexion France Travail bloquée : {adresse}. Le robot ne peut pas l'ouvrir ; "
                             f"rien n'a été envoyé ({_capture(capture)}).")
        if self.dans_entreprise:
            self.navigation_bloquee = None
            raise EchecEntreprise(f"page vers un site non autorisé ({adresse}), bloquée ; rien n'a été envoyé "
                                  f"({_capture(capture)})", "page bloquée")
        raise ArretRobot(f"Page bloquée (site non autorisé) : {adresse}. Rien n'a été envoyé ({_capture(capture)}).")

    def attendre_reconnexion(self) -> None:
        """Session perdue : pause, attente de votre reconnexion manuelle (20 min au plus), puis reprise."""
        self.noter("Session France Travail perdue : reconnectez-vous dans la fenêtre du robot (20 min au plus).")
        self.ecrire_etat(attente="reconnexion", etape="Session perdue : reconnectez-vous dans la fenêtre du robot.")
        fin = time.monotonic() + self.delais["reconnexion"]
        while time.monotonic() < fin:
            self.verifier_arret()
            url = urlsplit(self.page.url)
            if url.scheme == "chrome-error" and self.navigation_bloquee:
                self.arret_navigation_bloquee()
            if url.hostname == self.hote and not url.path.startswith(("/login", "/auth", "/connexion")):
                if self.etat_page() not in ("connexion", "chargement"):
                    self.ecrire_etat(attente=None)
                    self.noter("Reconnexion détectée : reprise de la campagne.")
                    return
            self.page.wait_for_timeout(1000)
        raise ArretRobot("Pas de reconnexion depuis 20 minutes : la campagne s'arrête (sans alerte).")

    # Étapes d'une candidature

    def debuter(self):
        motif = re.compile(r"^\s*Débuter votre candidature spontanée\s*$", re.I)
        return self.page.get_by_role("button", name=motif).or_(self.page.get_by_role("link", name=motif)).first

    def ouvrir_fiche(self, e: dict) -> str:
        """Ouvre la fiche dans la recherche du site (métier, département), vérifie son SIRET et lit son état :
        « sans_email », « deja », « inactif » ou « fiche » (bouton « Débuter votre candidature spontanée »).

        Les pages de résultats déjà lues aujourd'hui sont retenues pour chaque couple métier et département : le robot
        va droit à la page où l'entreprise a été vue (puis ses voisines), et ne lit une page nouvelle que si le nom n'est
        sur aucune page connue. La fiche, elle, est toujours rouverte et relue juste avant le formulaire.
        """
        nom = e["entreprise"]
        if not e.get("departement"):
            raise FicheIntrouvable("département inconnu")
        noms = list(dict.fromkeys(n for n in (e.get("raison_sociale"), nom) if n))
        cles = {cp._cle(n) for n in noms}
        self.etape_courante = "fiche"
        self.ecrire_etat(etape=f"{nom} : fiche de l'entreprise")
        memoire = self.memoire_pages(e)
        lues_ici = []

        def chercher(numero: int) -> str | None:
            lues_ici.append(numero)
            noms_page = self.charger_resultats(e, numero, nom)
            if cles & {cp._cle(n) for n in noms_page}:
                return self.chercher_dans_page(e, noms)
            return None

        connues = sorted({p for c in cles for p in memoire["noms"].get(c, ())})
        for numero in dict.fromkeys(connues + [q for p in connues for q in (p - 1, p + 1)]):
            if 1 <= numero <= self.derniere_page(memoire) and numero not in lues_ici:
                if etat := chercher(numero):
                    return etat
        numero = 1
        while numero <= min(PAGES_FICHE, self.derniere_page(memoire)):
            if numero not in lues_ici and numero not in memoire["lues"]:
                if etat := chercher(numero):
                    return etat
                if memoire["fin"] is not None and numero >= memoire["fin"]:
                    break
            numero += 1
        lu = (f"{len(memoire['lues'])} page(s) lue(s) aujourd'hui pour ce métier et ce département"
              + (f", {memoire['total']} entreprise(s) annoncée(s)" if memoire["total"] is not None else ""))
        if lues_ici:  # capture de la dernière page lue pour cette entreprise (jamais une page d'une autre entreprise)
            capture = self.capturer(f"{nom}-fiche-introuvable", "fiche introuvable dans les résultats", force=True,
                                    pleine=True)
            lu += f" ; {_capture(capture)}"
        else:
            lu += " ; nom absent de toutes ces pages, aucune page rechargée"
        raise FicheIntrouvable(f"absente des résultats du site ({e['rome']}, département {e['departement']} : {lu})")

    def memoire_pages(self, e: dict) -> dict:
        """Pages de résultats lues aujourd'hui pour ce métier et ce département (gardées d'une exécution à l'autre)."""
        cle = self.cle_pages(e)
        if cle not in self.pages_connues:
            stocke = cp._lire_cle(self.db, f"campagne.pages.{cle}") or {}
            if stocke.get("jour") != cp.jour():
                stocke = {}  # un autre jour : le classement a pu changer
            self.pages_connues[cle] = {"total": stocke.get("total"), "par_page": stocke.get("par_page"),
                                       "fin": stocke.get("fin"), "lues": set(stocke.get("lues", [])),
                                       "noms": {k: set(v) for k, v in (stocke.get("noms") or {}).items()}}
        return self.pages_connues[cle]

    @staticmethod
    def cle_pages(e: dict) -> str:
        """Métier et département tels que la recherche du site les reçoit (« 1 » pour l'Ain : les pages lues avec
        « 01 », toujours vides, ne servent plus)."""
        return f"{e['rome']}:{cp.departement_site(e['departement'])}"

    def derniere_page(self, memoire: dict) -> int:
        if memoire["fin"] is not None:
            return memoire["fin"]
        if memoire["total"] is not None and memoire["par_page"]:
            return max(1, -(-memoire["total"] // memoire["par_page"]))
        return PAGES_FICHE

    def retenir(self, e: dict, numero: int, total: int | None, noms: list[str]) -> None:
        memoire = self.memoire_pages(e)
        if total is not None:
            memoire["total"] = total
        if numero == 1 and noms:
            memoire["par_page"] = len(noms)
        if not noms or (memoire["par_page"] and len(noms) < memoire["par_page"]):
            memoire["fin"] = numero if noms else max(1, numero - 1)  # dernière page atteinte
        memoire["lues"].add(numero)
        for n in noms:
            memoire["noms"].setdefault(cp._cle(n), set()).add(numero)
        cp._ecrire_cle(self.db, f"campagne.pages.{self.cle_pages(e)}", {
            "jour": cp.jour(), "total": memoire["total"], "par_page": memoire["par_page"], "fin": memoire["fin"],
            "lues": sorted(memoire["lues"]), "noms": {k: sorted(v) for k, v in memoire["noms"].items()}})

    def charger_resultats(self, e: dict, numero: int, nom: str) -> list[str]:
        """Une page de résultats (lecture seule) : en cas d'erreur 5xx ou de délai dépassé, deux nouveaux essais après
        30 s puis 2 min, avant de compter l'erreur. Jamais pour une page qui envoie quelque chose."""
        from playwright.sync_api import Error as PlaywrightError

        derniere = None
        for essai, attente in enumerate((0,) + ATTENTES_RESULTATS):
            if attente:
                quand = f"{attente} s" if attente < 60 else f"{attente // 60} min"
                self.noter(f"{nom} : page {numero} des résultats en échec ({derniere}) ; nouvel essai dans {quand}.")
                self.ecrire_etat(etape=f"{nom} : nouvel essai de la page {numero} des résultats dans {quand}")
                self.attendre(attente)
            self.erreur_http = None
            try:
                self.ouvrir(cp.url_recherche(e, numero))
                self.attendre_page({"recherche"}, "la page de recherche", nom,
                                   sinon=lambda: DelaiDepasse(f"page {numero} des résultats incomplète"))
                total, noms = self.lire_resultats()
                self.retenir(e, numero, total, noms)
                return noms
            except ErreurServeur as exc:
                derniere, erreur = str(exc), exc
            except DelaiDepasse as exc:
                derniere, erreur = "délai dépassé", exc
            except PlaywrightError as exc:
                if self.page.is_closed():
                    raise
                code = (self.erreur_http or {}).get("code")
                if code and code >= 500:
                    derniere, erreur = self.detail_http(), exc
                elif "Timeout" in str(exc):
                    derniere, erreur = "délai dépassé", DelaiDepasse(f"page {numero} des résultats : délai dépassé")
                else:
                    raise
        raise erreur

    def lire_resultats(self) -> tuple[int | None, list[str]]:
        """Nombre annoncé et noms des entreprises affichées, une fois la liste chargée (elle arrive après le titre).
        « Aucun résultat » n'est retenu qu'après 2 s sans changement (jamais un état passager du chargement)."""
        fin, precedent, depuis = time.monotonic() + self.delais["page"], None, time.monotonic()
        resultats = {"total": None, "noms": []}
        while time.monotonic() < fin:
            self.verifier_arret()
            try:
                resultats = self.page.evaluate(RESULTATS_JS)
            except Exception:  # noqa: BLE001 - page en cours de chargement
                resultats = {"total": None, "noms": []}
            if resultats != precedent:
                precedent, depuis = resultats, time.monotonic()
            elif resultats["noms"] or (resultats["total"] == 0 and time.monotonic() - depuis >= 2 / self.facteur):
                break
            self.page.wait_for_timeout(400)
        return resultats["total"], resultats["noms"]

    def chercher_dans_page(self, e: dict, noms: list[str]) -> str | None:
        """Clique sur chaque entreprise du même nom affichée sur la page ; la bonne est celle de ce SIRET."""
        nom, siret = e["entreprise"], e["siret"]
        for libelle in noms:
            cibles = self.page.get_by_text(re.compile(r"^\s*" + re.escape(libelle) + r"\s*$", re.I))
            for i in range(min(cibles.count(), 5)):
                avant = self.lire().get("siret")
                self.pause_action()
                cibles.nth(i).click(timeout=self.delais["action"])
                fin = time.monotonic() + self.delais["page"]
                while time.monotonic() < fin:  # la fiche se charge à droite : attendre son SIRET (nouveau)
                    self.verifier_arret()
                    lu = self.lire().get("siret")
                    if lu and lu != avant:
                        break
                    self.page.wait_for_timeout(300)
                if self.lire().get("siret") == siret:
                    return self.etat_fiche(nom)
        return None

    def etat_fiche(self, nom: str) -> str:
        """État de la fiche, une fois stable : le bandeau « déjà débuté » arrive parfois après le reste de la fiche."""
        fin, stable = time.monotonic() + max(3.0, self.delais["page"] / 3), 0
        while time.monotonic() < fin:
            self.verifier_arret()
            etat = self.etat_page()
            if etat in ("captcha", "blocage", "http", "connexion", "bloquee"):
                self.alerte_page(etat, nom)
                continue
            if etat in ("sans_email", "deja", "inactif"):
                return etat
            stable = stable + 1 if self.lire().get("debuter") else 0
            if stable >= 5:
                self.capturer(f"{nom}-fiche", f"{nom} · fiche de l'entreprise")
                return "fiche"
            self.page.wait_for_timeout(300)
        capture = self.capturer(f"{nom}-fiche-non-reconnue", f"{nom} · fiche non reconnue", force=True, pleine=True)
        raise EchecEntreprise(f"fiche non reconnue : ni « Débuter votre candidature spontanée », ni « Aucune adresse "
                              f"email », ni « déjà débuté » ({_capture(capture)})", "fiche non reconnue")

    def verifier_formulaire(self, e: dict) -> None:
        titre = next((t for t in self.lire().get("titres") or [] if re.search(r"(?i)dans l.entreprise", t)), "")
        affiche = re.split(r"(?i)dans l.entreprise", titre)[-1] if titre else ""
        if not cp.meme_entreprise([e["entreprise"], e.get("raison_sociale")], affiche):
            capture = self.capturer(f"{e['entreprise']}-autre-entreprise", "entreprise différente", force=True)
            raise EchecEntreprise(f"écart : le formulaire ne mentionne pas {e['entreprise']} ({_capture(capture)})",
                                  "écart", definitif=True)

    def attendre_documents(self) -> list[dict]:
        """Vos documents à l'étape 1/2 : la liste arrive après le reste du formulaire (le 07/11, AMILTONE l'a lue vide).
        Attendue jusqu'au délai de page (30 s), puis retenue une fois stable sur deux lectures."""
        fin, precedent = time.monotonic() + self.delais["page"], None
        while time.monotonic() < fin:
            self.verifier_arret()
            try:
                documents = self.page.evaluate(DOCUMENTS_JS)
            except Exception:  # noqa: BLE001 - page en cours de chargement
                documents = None
            if documents and documents == precedent:
                return documents
            precedent = documents
            self.page.wait_for_timeout(400)
        return []

    def cocher_cv(self, e: dict) -> str:
        nom = e["entreprise"]
        documents = self.attendre_documents()
        if not documents:  # rien n'est encore créé sur le site : le formulaire peut être rechargé
            self.noter(f"{nom} : étape 1/2, liste de vos documents toujours vide ; formulaire rechargé une fois.")
            self.ouvrir(self.page.url)
            self.attendre_page({"formulaire"}, "le formulaire", nom)
            self.verifier_formulaire(e)
            documents = self.attendre_documents()
        if not documents:
            capture = self.capturer(f"{nom}-sans-documents", f"{nom} · étape 1/2 sans vos documents", force=True,
                                    pleine=True)
            raise EchecEntreprise(f"étape 1/2 : la liste de vos documents reste vide, même après un rechargement "
                                  f"({_capture(capture)}) ; si cela se répète, vérifiez que votre CV est dans « Vos "
                                  "documents » sur La Bonne Boîte (le robot ne téléverse jamais de fichier)",
                                  "liste de vos documents vide", passager=True)
        cv, raison = choix_cv(documents, self.reglages["nom_cv"])
        if cv is None:
            raise ArretRobot(f"CV à joindre ambigu ou introuvable : {raison}. Corrigez le nom du CV dans les réglages ; "
                             "la campagne s'arrête (sans alerte).", liberer=True)
        bloc = self.page.locator("section, div").filter(
            has=self.page.get_by_role("heading", name=re.compile("Les documents que je souhaite joindre"))).filter(
            has=self.page.get_by_role("checkbox")).last
        cases = bloc.get_by_role("checkbox")
        for doc in documents:
            case = cases.nth(doc["index"])
            if (doc["index"] == cv["index"]) != case.is_checked():
                try:
                    case.set_checked(doc["index"] == cv["index"], timeout=self.delais["action"])
                except Exception:  # noqa: BLE001 - case stylée : clic sur la case elle-même
                    case.evaluate("(cb) => cb.click()")
        coches = [d["nom"] for d in self.page.evaluate(DOCUMENTS_JS) if d["coche"]]
        if coches != [cv["nom"]]:
            raise EchecEntreprise(f"écart : le CV n'a pas pu être coché seul (cochés : {', '.join(coches) or 'aucun'})",
                                  "écart")
        return cv["nom"]

    def coller_message(self, message: str) -> None:
        lettre = self.page.evaluate(LETTRE_JS)
        if not lettre or lettre["zones"] != 1:
            raise EchecEntreprise("étape 2/2 : zone de la lettre introuvable", "page non reconnue")
        bloc = self.page.locator("section, div").filter(
            has=self.page.get_by_role("heading", name=re.compile("Ma lettre de motivation"))).filter(
            has=self.page.locator("textarea")).last
        zone = bloc.get_by_role("textbox")
        zone.fill(message, timeout=self.delais["action"])  # remplace tout le contenu (message par défaut du site compris)
        zone.blur()
        self.page.wait_for_timeout(300)
        lettre = self.page.evaluate(LETTRE_JS)
        if lettre["valeur"] != message:
            raise EchecEntreprise("écart : la zone de texte ne contient pas exactement le message collé", "écart")
        if lettre["compteur"] is None:
            raise EchecEntreprise("écart : compteur de caractères introuvable à l'étape 2/2", "écart")
        if lettre["compteur"] != len(message) or lettre["compteur"] > (lettre["maximum"] or cp.MESSAGE_MAX):
            raise EchecEntreprise(f"écart : le compteur indique {lettre['compteur']}/{lettre['maximum']} pour un message "
                                  f"de {len(message)} caractères", "écart")

    def verifier_synthese(self, e: dict, cv: str | None, message: str) -> list[str]:
        """Entreprise, pièce jointe (une seule : le CV) et lettre ; « Vos coordonnées » n'est jamais lu."""
        synthese = self.page.evaluate(SYNTHESE_JS)
        ecarts = []
        if not synthese.get("entreprise") or not cp.meme_entreprise([e["entreprise"], e.get("raison_sociale")],
                                                                    synthese["entreprise"]):
            ecarts.append("nom de l'entreprise")
        pieces = cp._cle(_nom_document(synthese.get("pieces") or ""))
        attendu = cp._cle(_nom_document(cv)) if cv else None
        if not pieces or (pieces != attendu if attendu else cp._cle(_nom_document(self.reglages["nom_cv"])) not in pieces):
            ecarts.append("pièce jointe")
        lettre = synthese.get("lettre")
        if synthese.get("zones") != 1 or lettre is None or _lignes(lettre) != _lignes(message):
            ecarts.append("texte de la lettre")
        return ecarts

    def boutons(self) -> list[str]:
        """Boutons et liens du contenu de la page, hors en-tête, menus et pied de page (votre nom y figure)."""
        try:
            noms = self.page.evaluate(BOUTONS_JS)
        except Exception:  # noqa: BLE001
            return []
        return list(dict.fromkeys(" ".join(n.split()) for n in noms if n.strip()))[:40]

    def message(self, e: dict) -> tuple[str, str]:
        version = e.get("version") if e["statut"] == cp.BROUILLON and e.get("version") in self.modeles else \
            cp.version_suivante(self.db, self.modeles)
        prepare = cp.preparer_message(self.modeles[version], e["entreprise"])
        if prepare["problemes"]:
            raise ArretRobot("Message à corriger : " + prepare["problemes"][0], liberer=True)
        return prepare["texte"], version

    def candidature(self, e: dict, ouvert: bool = False) -> str:
        """Une entreprise, du formulaire à la confirmation (ou à la Synthèse pour l'essai)."""
        nom, siret = e["entreprise"], e["siret"]
        cp.marquer(self.db, siret, cp.EN_COURS, motif=None)  # un échec précédent reste connu par e["motif"]
        self.ecrire_etat(entreprise=nom, siret=siret, attente=None, etape=f"{nom} : ouverture du formulaire")
        self._bandeau(f"Robot en cours : {nom}. Ne touchez pas à cette fenêtre.")
        texte, version = self.message(e)
        if e["statut"] == cp.BROUILLON and e.get("candidature_id"):
            self.ouvrir(f"{self.site}/candidature/recapitulatif/{e['candidature_id']}")
            self.attendre_page({"synthese"}, "la Synthèse de votre brouillon", nom)
            self.brouillon = True
            cv = None
        else:
            # Fiche de l'entreprise d'abord (sans e-mail, déjà candidaté ou « Débuter… »), comme sur le site.
            etat = self.ouvrir_fiche(e)
            if etat == "fiche":
                self.pause_action()
                self.etape_courante = "formulaire"
                self.debuter().click(timeout=self.delais["action"])
                etat = self.attendre_page({"formulaire", "deja", "sans_email", "inactif", "synthese"}, "le formulaire", nom)
            motif = {"deja": "deja_candidate", "synthese": "deja_candidate", "sans_email": "sans_email",
                     "inactif": "formulaire_inactif"}.get(etat)
            if motif:
                cp.marquer(self.db, siret, cp.IGNOREE, motif=motif)
                self.compteurs["ignorees"] += 1
                self.noter(f"{nom} : ignorée ({cp.MOTIFS_IGNOREE[motif]}){self.pages()}.")
                return "ignoree"
            self.verifier_formulaire(e)
            self.capturer(f"{nom}-formulaire", f"{nom} · formulaire ouvert")
            self.pause_action()
            self.ecrire_etat(etape=f"{nom} : étape 1/2, CV")
            cv = self.cocher_cv(e)
            self.noter(f"{nom} : CV « {cv} » coché, seul.")
            self.pause_action()
            self.ecrire_etat(etape=f"{nom} : étape 2/2, message")
            self.coller_message(texte)
            self.capturer(f"{nom}-message", f"{nom} · CV coché et message collé ({len(texte)} caractères)", pleine=True)
            self.noter(f"{nom} : message {version} collé ({len(texte)} caractères), compteur vérifié.")
            self.pause_action()
            self.verifier_arret()
            self.brouillon = True  # « Continuer » crée un brouillon sur La Bonne Boîte
            self.etape_courante = "synthèse"
            self.continuer(nom)
        candidature_id = urlsplit(self.page.url).path.rstrip("/").rsplit("/", 1)[-1]
        self.candidature_id = candidature_id
        ecarts = self.verifier_synthese(e, cv, texte)
        capture = self.capturer(f"{nom}-synthese", f"{nom} · Synthèse (page entière)", pleine=True,
                                force=self.mode in ("essai", "calibrage") or bool(ecarts))  # écart : toujours
        if self.mode == "essai":
            boutons = self.boutons()
            cp.marquer(self.db, siret, cp.BROUILLON, candidature_id=candidature_id, version=version)
            cp.ecrire_passage(self.db, essai={"siret": siret, "entreprise": nom, "le": _maintenant(),
                                              "candidature_id": candidature_id, "ecarts": ecarts, "boutons": boutons,
                                              "capture": capture, "run": self.dossier.name})
            self.ecrire_etat(boutons=boutons)
            self.noter(f"{nom} : Synthèse atteinte (brouillon {candidature_id}), rien n'a été envoyé "
                       f"({_capture(capture)}).")
            self.noter("Boutons trouvés sur la Synthèse : " + " | ".join(boutons))
            if ecarts:
                self.noter("ÉCART à la Synthèse : " + ", ".join(ecarts) + ". À corriger avant toute campagne.")
            return "essai"
        if ecarts:
            raise EchecEntreprise(f"écart à la Synthèse ({', '.join(ecarts)}) : rien n'a été envoyé ; un brouillon reste "
                                  f"sur La Bonne Boîte ({_capture(capture)})", "écart à la Synthèse", definitif=True)
        self.noter(f"{nom} : Synthèse vérifiée (entreprise, pièce jointe, lettre).")
        if self.mode == "calibrage":
            return self.calibrer(e, candidature_id, version)
        return self.envoyer(e, candidature_id, version)

    def continuer(self, nom: str) -> None:
        """« Continuer » (crée le brouillon), puis la Synthèse. Le 07/11, VIVERIS est resté sur le formulaire : si la
        Synthèse n'arrive pas, un 2e clic (un brouillon n'est jamais envoyé), puis l'entreprise est mise de côté."""
        bouton = self.page.get_by_role("button", name=re.compile(r"^\s*Continuer\s*$"))
        bouton.click(timeout=self.delais["action"])
        try:
            self.attendre_page({"synthese"}, "la Synthèse", nom, sinon=_SyntheseAbsente)
            return
        except _SyntheseAbsente:
            if self.etat_page() != "formulaire":
                self.attendre_page({"synthese"}, "la Synthèse", nom)
                return
        self.noter(f"{nom} : la Synthèse n'est pas apparue ; nouveau clic sur « Continuer ».")
        bouton.click(timeout=self.delais["action"])
        self.attendre_page({"synthese"}, "la Synthèse (après deux clics sur « Continuer »)", nom)

    def envoyer(self, e: dict, candidature_id: str, version: str) -> str:
        nom, siret = e["entreprise"], e["siret"]
        bouton = self.page.get_by_role("button", name=re.compile(r"^\s*" + re.escape(self.regle["bouton"]) + r"\s*$"))
        if bouton.count() != 1:
            capture = self.capturer(f"{nom}-sans-bouton", "bouton d'envoi introuvable", force=True, pleine=True)
            raise EchecEntreprise(f"bouton « {self.regle['bouton']} » introuvable sur la Synthèse ({_capture(capture)})",
                                  "bouton d'envoi introuvable")
        self.pause_action()
        self.verifier_arret()
        self.etape_courante = "envoi"
        cp.marquer(self.db, siret, cp.TENTEE, version=version, candidature_id=candidature_id, tentee_le=_maintenant())
        self.ecrire_etat(attente="envoi", etape=f"{nom} : envoi")
        bouton.click(timeout=self.delais["action"])
        fin = time.monotonic() + self.delais["page"]
        while time.monotonic() < fin:
            etat = self.etat_page()
            if etat == "connexion" and not self.connexion_durable():
                continue  # passage éclair par la page de connexion
            if etat in ("captcha", "blocage", "http", "connexion"):
                cp.marquer(self.db, siret, cp.INCERTAINE, motif=f"{etat} après le clic d'envoi")
                self.compteurs["incertaines"] += 1
                self.ecrire_etat(attente=None)
                if etat == "connexion":
                    raise SessionPerdue()
                self.alerte_page(etat, nom)
            if etat == "confirmation":
                break
            try:
                texte = self.page.evaluate("() => (document.body && document.body.innerText) || ''")
            except Exception:  # noqa: BLE001 - page en cours de chargement
                texte = ""
            if cp.sans_email_apres_envoi(texte):
                # Le site refuse l'envoi : l'entreprise n'a pas d'adresse e-mail. Rien n'est parti, pas d'alerte.
                capture = self.capturer(f"{nom}-sans-email", f"{nom} · refus du site : sans e-mail", force=True)
                cp.marquer(self.db, siret, cp.IGNOREE, motif="sans_email")
                self.compteurs["ignorees"] += 1
                self.ecrire_etat(attente=None)
                self.noter(f"{nom} : ignorée (Sans e-mail) : le site indique après le clic que l'entreprise n'a pas "
                           f"d'adresse e-mail ; rien n'est parti ({_capture(capture)}).")
                return "ignoree"
            self.page.wait_for_timeout(400)
        else:
            capture = self.capturer(f"{nom}-incertain", f"{nom} · envoi incertain", force=True)
            chemin = urlsplit(self.page.url).path
            motif = ("erreur serveur du site après le clic (" + chemin + ")") if chemin.startswith("/errors/5") else \
                f"confirmation non reconnue ({chemin})"
            detail = self.detail_http()
            cp.marquer(self.db, siret, cp.INCERTAINE, motif=(motif + (f" ; {detail}" if detail else ""))[:240])
            self.compteurs["incertaines"] += 1
            self.ecrire_etat(attente=None)
            raise EchecEntreprise(f"envoi incertain : la page de confirmation n'est pas apparue (adresse "
                                  f"{urlsplit(self.page.url).path}" + (f" ; {detail}" if detail else "")
                                  + f", {_capture(capture)}) ; jamais de nouvel essai, contrôle en fin d'exécution",
                                  "envoi incertain")
        cp.marquer(self.db, siret, cp.ENVOYEE, envoyee_le=_maintenant())
        self.ecrire_etat(attente=None)
        self.compteurs["envoyees"] += 1
        capture = self.capturer(f"{nom}-envoyee", f"{nom} · confirmation d'envoi")
        self.noter(f"{nom} : candidature ENVOYÉE (version {version}, n° {candidature_id})"
                   + (f" ({_capture(capture)})" if capture else "") + f"{self.pages()}.")
        return "envoyee"

    def calibrer(self, e: dict, candidature_id: str, version: str) -> str:
        """Vous cliquez vous-même sur le bouton d'envoi ; le robot observe la page qui suit et propose la règle."""
        nom, siret = e["entreprise"], e["siret"]
        avant = urlsplit(self.page.url).path
        self.page.evaluate(CLIC_JS)
        self.clic = None
        self.ecrire_etat(attente="clic", etape=f"{nom} : cliquez vous-même sur le bouton d'envoi, ou passez.")
        self.noter(f"{nom} : Synthèse prête. Cliquez vous-même sur « Envoyer la candidature » dans la fenêtre du robot.")
        fin = time.monotonic() + self.delais["clic"]
        while time.monotonic() < fin:
            self.verifier_arret()
            decision = _lire(self.dossier / "decision.json", {}) or {}
            if decision.get("action") == "passer" or self.decision_bandeau == "passer":
                (self.dossier / "decision.json").unlink(missing_ok=True)
                cp.marquer(self.db, siret, cp.BROUILLON, candidature_id=candidature_id, version=version)
                self.noter(f"{nom} : passée, rien n'a été envoyé (brouillon gardé).")
                self.ecrire_etat(attente=None)
                return "passee"
            if urlsplit(self.page.url).path != avant:
                break
            self._bandeau(f"Calibrage : cliquez vous-même sur le bouton d'envoi pour {nom}, ou passez.",
                          (("passer", "Passer"), ("arreter", "Arrêter")))
            self.page.wait_for_timeout(400)
        else:
            cp.marquer(self.db, siret, cp.BROUILLON, candidature_id=candidature_id, version=version)
            raise ArretRobot(f"Aucun clic depuis {self.delais['clic'] // 60 or 1} min : calibrage arrêté, brouillon gardé.")
        if self.clic and not re.search(r"(?i)envoy", self.clic):
            cp.marquer(self.db, siret, cp.BROUILLON, candidature_id=candidature_id, version=version)
            self.noter(f"{nom} : vous avez cliqué sur « {self.clic} », qui n'est pas un envoi : aucune règle proposée, "
                       "brouillon gardé. Relancez le calibrage.")
            self.ecrire_etat(attente=None)
            return "passee"
        tentee_le = _maintenant()
        self.ecrire_etat(attente=None, etape=f"{nom} : observation de la page qui suit l'envoi")
        self.page.wait_for_load_state("domcontentloaded")
        self.page.wait_for_timeout(1500)
        etat = self.etat_page()
        if etat in ("captcha", "blocage", "http"):
            cp.marquer(self.db, siret, cp.INCERTAINE, motif="calibrage : alerte après votre clic", version=version,
                       tentee_le=tentee_le)
            self.alerte_page(etat, nom)
        observe = self.page.evaluate(CONFIRMATION_JS)
        path = urlsplit(self.page.url).path
        texte = next((t for t in observe["titres"] if re.search(r"(?i)envoy", t)), None) or observe["phrase"] \
            or (observe["titres"][0] if observe["titres"] else "")
        texte = re.sub(r"\s+le\s+\d{2}/\d{2}/\d{4}.*$", "", texte).strip()[:120]  # sans date ni heure
        regle = {"bouton": self.clic or "Envoyer la candidature", "url_motif": re.sub(r"\d+", "{id}", path),
                 "texte": texte, "url_observee": path}
        capture = self.capturer(f"{nom}-confirmation", f"{nom} · page après votre envoi", force=True, pleine=True)
        cp.marquer(self.db, siret, cp.INCERTAINE, motif="calibrage : envoyée par vous, règle à valider",
                   version=version, tentee_le=tentee_le)
        cp.ecrire_passage(self.db, calibrage={"siret": siret, "entreprise": nom, "le": tentee_le, "capture": capture,
                                              "run": self.dossier.name, "titres": observe["titres"]},
                          regle=regle, regle_validee=False)
        self.noter(f"{nom} : vous avez cliqué sur « {regle['bouton']} ». Page suivante : {path} ({_capture(capture)}).")
        self.noter(f"Règle proposée : adresse « {regle['url_motif']} » et texte « {regle['texte']} ». "
                   "Validez-la dans l'écran Campagne.")
        return "calibrage"

    def traiter(self, e: dict, ouvert: bool = False) -> str:
        """Une entreprise, avec le statut correct quoi qu'il arrive (jamais reprise si elle a été tentée). Un échec ne
        concerne qu'elle : elle est mise de côté et le robot passe à la suivante."""
        from playwright.sync_api import Error as PlaywrightError

        nom = e["entreprise"]
        self.brouillon, self.candidature_id = False, None
        self.etape_courante = "début"
        self.erreur_http, self.pages_entreprise = None, 0
        self.dans_entreprise = True
        try:
            resultat = self.candidature(e, ouvert)
            self.succes()
            return resultat
        except Ralenti as exc:
            self.mettre_de_cote(e, str(exc), passager=True)
            self.noter(f"{nom} : {exc} ; entreprise reprise plus tard{self.pages()}.")
            self.attendre_ralenti()
            return "erreur"
        except EchecEntreprise as exc:
            self.mettre_de_cote(e, str(exc), definitif=exc.definitif, passager=exc.passager)
            self.noter(f"{nom} : ÉCHEC, {exc} ; entreprise mise de côté, le robot continue{self.pages()}.")
            return self.compter_echec(exc.nature)
        except DelaiDepasse as exc:
            capture = self.capturer(f"{nom}-erreur", f"{nom} · page de résultats incomplète", force=True)
            self.mettre_de_cote(e, f"délai dépassé : {exc}", passager=True)
            self.noter(f"{nom} : ERREUR, délai dépassé ({exc}, après 3 essais ; {_capture(capture)}) ; entreprise "
                       f"reprise plus tard{self.pages()}.")
            return self.compter_echec("délai dépassé")
        except ErreurServeur as exc:
            capture = self.capturer(f"{nom}-erreur", f"{nom} · erreur du serveur", force=True)
            self.mettre_de_cote(e, f"erreur serveur : {exc}"[:140], passager=True)
            self.noter(f"{nom} : ERREUR du serveur pendant « {self.etat.get('etape')} » ({exc}, {_capture(capture)}) ; "
                       f"entreprise reprise plus tard{self.pages()}.")
            return self.compter_echec(f"erreur serveur HTTP {(self.erreur_http or {}).get('code', '5xx')}")
        except FicheIntrouvable as exc:
            return self.introuvable(e, exc)
        except SessionPerdue:
            self.mettre_de_cote(e, "session perdue", passager=True)
            self.noter(f"{nom} : interrompue par la perte de session (étape : {self.etape_courante}) ; reprise plus tard.")
            self.attendre_reconnexion()
            return "session"
        except ArretRobot as exc:
            self._statut_si_interrompue(e, str(exc)[:120], liberer=exc.liberer)
            raise
        except PlaywrightError as exc:
            if self.page.is_closed():
                self._statut_si_interrompue(e, "fenêtre fermée", liberer=True)
                raise
            message = sans_donnees(str(exc).splitlines()[0])[:160]
            if self.http:  # 429 ou 403 sans page (réponse vide) : pause (429) ou arrêt (403)
                try:
                    self.alerte_page("http", nom)
                except Ralenti as ralenti:
                    self.mettre_de_cote(e, str(ralenti), passager=True)
                    self.noter(f"{nom} : {ralenti} ; entreprise reprise plus tard{self.pages()}.")
                    self.attendre_ralenti()
                    return "erreur"
                except ArretRobot as arret:
                    self._statut_si_interrompue(e, str(arret)[:120], liberer=True)
                    raise
            capture = self.capturer(f"{nom}-erreur", f"{nom} · erreur", force=True)
            detail = self.detail_http()
            code = (self.erreur_http or {}).get("code")
            serveur = bool(code and code >= 500)
            self.mettre_de_cote(e, "erreur : " + (detail or message)[:120], passager=serveur)
            self.noter(f"{nom} : ERREUR pendant « {self.etat.get('etape')} » ({message}"
                       + (f" ; {detail}" if detail else "") + f", {_capture(capture)}) ; entreprise mise de côté, le robot "
                       f"continue{self.pages()}.")
            return self.compter_echec(f"erreur serveur HTTP {code}" if serveur else "erreur du navigateur")
        finally:
            self.dans_entreprise = False

    def pages(self) -> str:
        return f" [{self.pages_entreprise} page(s) chargée(s)]"

    def brouillon_courant(self) -> str | None:
        """Numéro du brouillon de l'entreprise en cours : retenu à la Synthèse, ou lu dans l'adresse de la Synthèse
        affichée (un captcha peut l'interrompre avant)."""
        if self.candidature_id:
            return self.candidature_id
        chemin = urlsplit(self.page.url).path if self.page is not None and not self.page.is_closed() else ""
        trouve = re.fullmatch(r"/candidature/recapitulatif/(\d+)/?", chemin)
        return trouve[1] if trouve and self.brouillon else None

    def succes(self) -> None:
        """Une entreprise menée à son terme (envoyée, ignorée, essai…) : le site fonctionne, les séries repartent à zéro.
        Les fiches introuvables de la série en cours le sont donc vraiment."""
        self.echecs_suite = self.erreurs_5xx_suite = self.pauses_sans_succes = self.ralentis_suite = 0
        self.serie_introuvables = []

    def mettre_de_cote(self, e: dict, raison: str, *, definitif: bool = False, passager: bool = False) -> None:
        """Rien n'est perdu : sans brouillon, l'entreprise redevient « à faire » ; avec un brouillon (Synthèse atteinte),
        « brouillon ». Elle est reprise lors d'une exécution suivante, une seule fois si l'échec venait d'elle ; au 2e
        échec, ou pour un écart, « erreur ». Envoi tenté : « incertaine », jamais reprise."""
        self.exclues.add(e["siret"])
        motif = f"{raison} (étape : {self.etape_courante})"[:240]
        with cp._connect(self.db) as db:
            row = db.execute("SELECT statut FROM campagne WHERE siret = ?", (e["siret"],)).fetchone()
        if row and row["statut"] == cp.TENTEE:
            cp.marquer(self.db, e["siret"], cp.INCERTAINE, motif=motif)
            self.compteurs["incertaines"] += 1
            return
        if not row or row["statut"] != cp.EN_COURS:
            return  # statut déjà posé (envoi incertain)
        deja = (e.get("motif") or "").startswith(PREMIER_ECHEC)
        brouillon = self.brouillon_courant()
        if definitif or (deja and not passager):
            cp.marquer(self.db, e["siret"], cp.ERREUR, motif=motif, **({"candidature_id": brouillon} if brouillon else {}))
            return
        nouveau = (e.get("motif") if deja else None) if passager else PREMIER_ECHEC + motif
        if brouillon:
            cp.marquer(self.db, e["siret"], cp.BROUILLON, candidature_id=brouillon, motif=nouveau)
        else:
            cp.marquer(self.db, e["siret"], cp.A_FAIRE, motif=nouveau)

    def compter_echec(self, nature: str) -> str:
        """Pas d'arrêt : après 3 échecs de suite (ou 2 erreurs 5xx de suite), pause, puis reprise."""
        self.echecs_suite += 1
        self.erreurs_5xx_suite = self.erreurs_5xx_suite + 1 if nature.startswith("erreur serveur HTTP 5") else 0
        self.compteurs["erreurs"] += 1
        self.ecrire_etat(compteurs=self.compteurs)
        if self.erreurs_5xx_suite >= 2:
            self.pause_echecs("2 erreurs 5xx de suite, le site de La Bonne Boîte est en difficulté")
        elif self.echecs_suite >= ECHECS_AVANT_PAUSE:
            self.pause_echecs(f"{self.echecs_suite} échecs de suite (dernier : {nature}), le site de La Bonne Boîte est "
                              "peut-être en difficulté")
        return "erreur"

    def pause_echecs(self, raison: str) -> None:
        """10 min, puis 20, puis 30 ; après 5 pauses de suite sans aucun succès, arrêt sans alerte du jour."""
        if self.pauses_sans_succes >= PAUSES_MAX:
            raise ArretRobot(f"Toujours des échecs après {PAUSES_MAX} pauses ({raison}) : La Bonne Boîte ne fonctionne pas "
                             "normalement. Arrêt sans alerte : relancez plus tard ; les entreprises concernées restent à "
                             "faire.")
        duree = PAUSES[min(self.pauses_sans_succes, len(PAUSES) - 1)]
        self.pauses_sans_succes += 1
        self.noter(f"Pause de {duree // 60} min : {raison}. Le robot reprend ensuite.")
        self.ecrire_etat(etape=f"Pause de {duree // 60} min ({raison})")
        self.attendre(duree)
        self.echecs_suite = self.erreurs_5xx_suite = 0

    def attendre_ralenti(self) -> None:
        """Code 429 : 15 min, puis 30 min, puis 1 h (ou plus si le site le demande) ; une 4e fois de suite, arrêt."""
        if self.ralentis_suite >= len(PAUSES_429):
            raise ArretRobot(f"Code {self.http} reçu {len(PAUSES_429) + 1} fois de suite malgré les pauses : La Bonne Boîte "
                             "limite les requêtes. Arrêt, pas de reprise aujourd'hui.", "http")
        duree = PAUSES_429[self.ralentis_suite]
        demande = _secondes((self.erreur_http or {}).get("retry_after"))
        duree = max(duree, min(demande or 0, 4 * 3600))
        self.ralentis_suite += 1
        self.noter(f"Pause de {duree // 60} min : La Bonne Boîte demande de ralentir (code 429). Le robot reprend ensuite.")
        self.ecrire_etat(etape=f"Pause de {duree // 60} min (code 429)")
        self.attendre(duree)
        self.http = None

    def introuvable(self, e: dict, exc: Exception) -> str:
        """Rien n'a été ouvert : l'entreprise est ignorée (« introuvable »), sans erreur. 10 de suite : la recherche du
        site est peut-être en panne ; elles redeviennent « à faire » (reprises plus tard) et le robot fait une pause."""
        nom = e["entreprise"]
        cp.marquer(self.db, e["siret"], cp.IGNOREE, motif="introuvable")
        self.compteurs["ignorees"] += 1
        self.serie_introuvables.append(e["siret"])
        self.ecrire_etat(compteurs=self.compteurs)
        self.noter(f"{nom} : ignorée, fiche introuvable ({exc}) ; rien n'a été ouvert{self.pages()}.")
        if len(self.serie_introuvables) >= INTROUVABLES_DE_SUITE:
            serie, self.serie_introuvables = self.serie_introuvables, []
            rendues = cp.rendre_a_faire(self.db, serie)
            self.exclues.update(serie)
            self.compteurs["ignorees"] -= rendues
            self.ecrire_etat(compteurs=self.compteurs)
            self.noter(f"{INTROUVABLES_DE_SUITE} fiches introuvables de suite : la recherche du site est peut-être en "
                       f"panne ; ces {rendues} entreprises restent à faire (reprises plus tard).")
            self.pause_echecs(f"{INTROUVABLES_DE_SUITE} fiches introuvables de suite")
        return "ignoree"

    def _statut_si_interrompue(self, e: dict, motif: str, liberer: bool = False) -> None:
        motif = f"{motif} (étape : {self.etape_courante})"
        with cp._connect(self.db) as db:
            row = db.execute("SELECT statut FROM campagne WHERE siret = ?", (e["siret"],)).fetchone()
        if row and row["statut"] == cp.EN_COURS and liberer:
            # Arrêt demandé, CV ambigu… : rien n'a échoué. Brouillon créé sur le site : « brouillon », sinon « à faire ».
            if brouillon := self.brouillon_courant():
                cp.marquer(self.db, e["siret"], cp.BROUILLON, candidature_id=brouillon)
            elif e["statut"] == cp.BROUILLON and e.get("candidature_id"):
                cp.marquer(self.db, e["siret"], cp.BROUILLON)
            else:
                cp.marquer(self.db, e["siret"], cp.A_FAIRE)
        elif row and row["statut"] == cp.EN_COURS:
            cp.marquer(self.db, e["siret"], cp.ERREUR, motif=motif)
        elif row and row["statut"] == cp.TENTEE:
            cp.marquer(self.db, e["siret"], cp.INCERTAINE, motif=motif)
            self.compteurs["incertaines"] += 1

    # Parcours

    def parcourir(self) -> str:
        if self.mode in ("essai", "calibrage"):
            e = cp.entreprise_par_siret(self.db, self.travail["siret"])
            url = (f"{self.site}/candidature/recapitulatif/{e['candidature_id']}" if e["statut"] == cp.BROUILLON
                   and e.get("candidature_id") else cp.url_formulaire(e))
            self.connexion(url, e["entreprise"])
            resultat = self.traiter(e, ouvert=e["statut"] != cp.BROUILLON)
            if self.mode == "essai":
                return ("Essai terminé : Synthèse atteinte et capturée, rien n'a été envoyé." if resultat == "essai"
                        else f"Essai terminé sans atteindre la Synthèse ({resultat}).")
            return ("Calibrage : règle proposée, à valider dans l'écran Campagne." if resultat == "calibrage"
                    else f"Calibrage terminé sans envoi ({resultat}).")
        objectif = self.travail["objectif"]
        premiere = cp.prochaines(self.db, self.travail["phases"])
        self.connexion(cp.url_formulaire(premiere[0]), premiere[0]["entreprise"])
        ouvert, fin = True, "Plus aucune entreprise à faire dans les phases actives."
        while True:
            self.verifier_arret()
            self.point_de_pause()
            if self.compteurs["envoyees"] >= objectif:
                fin = ("Cinq envois faits : comparez-les à vos accusés de réception, puis validez." if self.mode == "cinq"
                       else f"Objectif atteint : {objectif} candidature(s) envoyée(s).")
                break
            suivantes = cp.prochaines(self.db, self.travail["phases"], exclure=self.exclues)
            if not suivantes:
                if self.exclues & {e["siret"] for e in cp.prochaines(self.db, self.travail["phases"], limite=10 ** 6)}:
                    fin = ("Plus aucune autre entreprise à faire : celles mises de côté pendant cette exécution seront "
                           "reprises la prochaine fois.")
                break
            resultat = self.traiter(suivantes[0], ouvert)
            ouvert = False
            self.ecrire_etat(compteurs=self.compteurs)
            if resultat == "session":
                ouvert = False
        return (f"{fin} Envoyées : {self.compteurs['envoyees']}, ignorées : {self.compteurs['ignorees']}, "
                f"erreurs : {self.compteurs['erreurs']}, incertaines : {self.compteurs['incertaines']}.")

    def bilan_pages(self) -> None:
        traitees = sum(self.compteurs.values())
        moyenne = f", {self.pages_total / traitees:.1f} par entreprise en moyenne" if traitees else ""
        self.noter(f"Pages chargées sur La Bonne Boîte pendant cette exécution : {self.pages_total}{moyenne}.")

    def controle_incertaines(self) -> None:
        """Fin de session : « Gérer vos candidatures » pour les envois incertains du jour (aucun nouvel essai).

        La liste se charge après la page : le contrôle n'est conclu que si une entreprise déjà envoyée y figure ;
        sinon il est déclaré impossible (et non « absente »).
        """
        with cp._connect(self.db) as db:
            noms = [r["entreprise"] for r in db.execute("SELECT entreprise FROM campagne WHERE statut = ? AND maj_le >= ?",
                                                        (cp.INCERTAINE, cp._debut_jour()))]
            temoins = [r["entreprise"] for r in db.execute(
                "SELECT entreprise FROM campagne WHERE statut = ? ORDER BY envoyee_le DESC LIMIT 10", (cp.ENVOYEE,))]
        if not noms or self.page is None or self.page.is_closed():
            return
        lien = self.page.get_by_role("link", name=re.compile(r"Gérer vos candidatures", re.I))
        if lien.count() == 0:
            self.noter("Contrôle des envois incertains : lien « Gérer vos candidatures » introuvable sur cette page ; "
                       "contrôlez-les vous-même sur La Bonne Boîte.")
            return
        lien.first.click(timeout=10000)
        self.page.wait_for_load_state("domcontentloaded")
        texte, fin = "", time.monotonic() + self.delais["page"]
        while time.monotonic() < fin:
            texte = cp._cle(self.page.evaluate("() => (document.body && document.body.innerText) || ''"))
            if any(cp._cle(n) in texte for n in noms + temoins):
                break
            self.page.wait_for_timeout(500)
        charge = any(cp._cle(n) in texte for n in temoins)
        capture = self.capturer("gerer-vos-candidatures", "Gérer vos candidatures", force=True, pleine=True)
        for nom in noms:
            if cp._cle(nom) in texte:
                self.noter(f"Contrôle : {nom} apparaît dans « Gérer vos candidatures » ; confirmez son statut dans "
                           "l'historique.")
            elif charge:
                self.noter(f"Contrôle : {nom} n'apparaît pas dans « Gérer vos candidatures » (liste chargée) ; "
                           "confirmez son statut dans l'historique.")
            else:
                self.noter(f"Contrôle impossible pour {nom} : la liste de « Gérer vos candidatures » n'était pas chargée "
                           "(aucune candidature envoyée n'y figurait) ; vérifiez vous-même sur La Bonne Boîte.")
        self.noter(f"Capture du contrôle : {capture}.")

    def _requete(self, route) -> None:
        url = urlsplit(route.request.url)
        hote = url.hostname or ""
        if url.scheme in ("data", "blob", "about") or hote_autorise(hote, self.hote, test=fake_ft_enabled()):
            route.continue_()
            return
        try:
            if route.request.is_navigation_request() and route.request.frame == self.page.main_frame:
                self.navigation_bloquee = (hote, url.path)
        except Exception:  # noqa: BLE001 - requête sans cadre (service worker)
            pass
        if hote not in self.bloques:
            self.bloques.add(hote)
            self.noter(f"Requête bloquée vers {hote} (seuls La Bonne Boîte et France Travail sont autorisés).")
        route.abort()

    def _echec(self, request) -> None:
        """Requête échouée : une page (pas une ressource) vers un site non autorisé, souvent après une redirection."""
        url = urlsplit(request.url)
        hote = url.hostname or ""
        if url.scheme in ("http", "https") and not hote_autorise(hote, self.hote, test=fake_ft_enabled()):
            try:
                principale = request.is_navigation_request() and request.frame == self.page.main_frame
            except Exception:  # noqa: BLE001
                principale = False
            if principale:
                self.navigation_bloquee = (hote, url.path)
            if hote not in self.bloques:
                self.bloques.add(hote)
                self.noter(f"Requête bloquée vers {hote} (seuls La Bonne Boîte et France Travail sont autorisés).")

    def _reponse(self, response) -> None:
        url = urlsplit(response.url)
        hote = url.hostname or ""
        if not hote_autorise(hote, self.hote, test=fake_ft_enabled()) or est_connexion(hote):
            return
        try:
            page = response.request.resource_type == "document" and response.frame == self.page.main_frame
        except Exception:  # noqa: BLE001 - réponse sans cadre (service worker)
            page = False
        if page and hote == self.hote:
            self.pages_total += 1
            self.pages_entreprise += 1
        if response.status in (429, 403) and not self.http:
            self.http = f"{response.status} ({url.path[:80]})"
        if (page and response.status >= 400) or (hote == self.hote and response.status >= 500):
            self.erreur_http = {"code": response.status, "texte": response.status_text or "",
                                "chemin": url.path[:80], "type": "page" if page else "API du site",
                                "url": response.url.split("#")[0], "retry_after": response.headers.get("retry-after"),
                                "reponse": response}
        elif page and self.erreur_http and self.erreur_http["type"] == "page":
            self.erreur_http = None  # page suivante chargée normalement : l'erreur précédente ne la concerne pas

    def page_en_erreur_serveur(self) -> bool:
        """La page affichée est celle qui a reçu un code 5xx (Chrome peut l'afficher ou échouer, selon le mode)."""
        e = self.erreur_http
        affichee = self.page.url.split("#")[0] == (e or {}).get("url") or self.page.url.startswith("chrome-error://")
        return bool(e and e["type"] == "page" and e["code"] >= 500 and affichee)

    def detail_http(self) -> str:
        """« HTTP 504 Gateway Timeout sur /recherche (page), réponse vide » : code et premiers mots, sans données."""
        e = self.erreur_http
        if not e:
            return ""
        try:
            corps = e["reponse"].text()
        except Exception:  # noqa: BLE001 - corps indisponible (navigation interrompue)
            corps = None
        mots = premiers_mots(corps) if corps else ""
        detail = f"HTTP {e['code']}{(' ' + e['texte']) if e['texte'] else ''} sur {e['chemin']} ({e['type']})"
        detail += f", réponse : « {mots} »" if mots else (", réponse vide" if corps is not None else ", réponse illisible")
        if e["retry_after"]:
            detail += f", Retry-After : {sans_donnees(e['retry_after'])[:20]}"
        return detail

    def executer(self) -> None:
        from playwright.sync_api import Error as PlaywrightError, sync_playwright

        textes = {"essai": "Essai sur une entreprise : jusqu'à la Synthèse, sans envoyer.",
                  "calibrage": "Calibrage : vous cliquez vous-même sur le bouton d'envoi, le robot observe.",
                  "cinq": "Cinq envois automatiques devant vous.",
                  "campagne": f"Campagne : {self.travail.get('objectif')} candidature(s) à envoyer, sans pause, puis arrêt."}
        self.noter(textes[self.mode] + " Navigateur neuf, sans session enregistrée : vous vous connectez vous-même.")
        self.ecrire_etat(pid=os.getpid(), statut="en_cours", captures=[], compteurs=self.compteurs)
        statut, resume, alerte = "erreur", "Erreur inattendue : voir le journal.", None
        try:
            local = self.hote in ("127.0.0.1", "localhost")
            if (self.site != SITE_LBB and not local) or (fake_ft_enabled() and not local):
                raise ArretRobot("Adresse de La Bonne Boîte inattendue : le robot ne démarre pas.")
            with sync_playwright() as pw:
                # Seuls les sites autorisés sont résolus : une redirection vers un autre site échoue elle aussi.
                options = {"headless": _invisible(), "args": [f"--host-resolver-rules={regles_dns(self.hote)}"]}
                if fake_ft_enabled():  # mode test : aucune requête ne peut sortir de la machine
                    options["proxy"] = {"server": "http://127.0.0.1:9", "bypass": "127.0.0.1,localhost"}
                browser = pw.chromium.launch(**options)
                try:
                    context = browser.new_context(locale="fr-FR", viewport={"width": 1280, "height": 860})
                    context.route("**/*", self._requete)
                    context.on("response", self._reponse)
                    context.on("requestfailed", self._echec)
                    self.page = context.new_page()
                    self.page.expose_binding("__jobhunterDecision", self._decision)
                    self.page.expose_binding("__jobhunterClic", self._clic)
                    try:
                        statut, resume = "termine", self.parcourir()
                    except ArretRobot as exc:
                        statut, resume, alerte = "arrete", str(exc), exc.alerte
                    except PlaywrightError as exc:
                        fermee = self.page.is_closed() or "closed" in str(exc).lower()
                        statut, resume = ("arrete", "Fenêtre du robot fermée : arrêt.") if fermee else \
                            ("erreur", f"Erreur du navigateur : {str(exc).splitlines()[0][:300]}")
                    self.bilan_pages()
                    if alerte:
                        cp.poser_alerte(self.db, alerte, resume)
                    if self.mode in ("cinq", "campagne") and alerte not in ("captcha", "blocage", "http"):
                        try:
                            self.controle_incertaines()
                        except Exception as exc:  # noqa: BLE001 - le contrôle ne doit pas masquer la fin
                            self.noter(f"Contrôle des incertaines impossible ({str(exc).splitlines()[0][:120]}).")
                    if self.mode == "cinq":
                        cp.ecrire_passage(self.db, cinq={"le": _maintenant(), "envoyees": self.compteurs["envoyees"],
                                                         "run": self.dossier.name, "alerte": alerte})
                    self.noter(("Fin : " if statut == "termine" else "ARRÊT : " if statut == "arrete" else "ERREUR : ")
                               + resume)
                    self.ecrire_etat(statut=statut, resume=resume, alerte=alerte, attente=None, fin=_maintenant(),
                                     compteurs=self.compteurs)
                    self.garder_ouverte(resume)
                finally:
                    try:
                        browser.close()  # session détruite : rien n'est conservé
                    except Exception:  # noqa: BLE001
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
        if self.page is None or self.page.is_closed():
            return
        self.ecrire_etat(fenetre_ouverte=True)
        attente = self.delais["fenetre"]
        duree = f"{round(attente / 60)} min" if attente >= 60 else f"{round(attente)} s"
        self._bandeau(f"{resume} Travail enregistré. La fenêtre se ferme toute seule dans {duree} ; vous pouvez la "
                      "fermer avant.", (("arreter", "Fermer la fenêtre"),))
        fin = time.monotonic() + self.delais["fenetre"]
        siret = self.brouillon_a_surveiller()
        try:
            while time.monotonic() < fin and not self.page.is_closed():
                if (self.dossier / "arret").exists() or self.decision_bandeau == "arreter":
                    break
                if siret and self.confirmation():
                    self.envoi_a_la_main(siret)
                    siret = None
                self.page.wait_for_timeout(500)
        except Exception:  # noqa: BLE001 - fenêtre fermée par vous
            pass

    def brouillon_a_surveiller(self) -> str | None:
        """Après un essai seulement : l'entreprise restée en brouillon, si sa Synthèse est la page affichée. Une page de
        confirmation déjà à l'écran (envoi précédent) n'est jamais attribuée à une autre entreprise."""
        siret = self.etat.get("siret")
        if self.mode != "essai" or not siret:
            return None
        e = cp.entreprise_par_siret(self.db, siret)
        synthese = f"/candidature/recapitulatif/{e.get('candidature_id')}"
        if e["statut"] != cp.BROUILLON or urlsplit(self.page.url).path.rstrip("/") != synthese:
            return None
        return siret

    def envoi_a_la_main(self, siret: str) -> None:
        """Vous avez envoyé vous-même dans la fenêtre du robot (après l'essai) : enregistré, jamais retenté."""
        if cp.entreprise_par_siret(self.db, siret)["statut"] != cp.BROUILLON:
            return
        try:
            e = cp.envoyee_a_la_main(self.db, siret)
        except cp.CampagneError:
            return  # déjà envoyée ou tentée : rien à changer
        capture = self.capturer(f"{e['entreprise']}-envoyee-a-la-main", f"{e['entreprise']} · envoyée par vous", force=True)
        self.noter(f"{e['entreprise']} : page de confirmation détectée après votre clic : candidature notée « envoyée à la "
                   f"main » ({_capture(capture)}).")


_URL = re.compile(r"(https?://[^\s?#,)]+)[?#][^\s,)]*")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_CHIFFRES = re.compile(r"\+?\d[\d .-]{7,}\d")


def sans_donnees(texte: str) -> str:
    """Texte pour le journal : adresses sans paramètres, e-mails et longues suites de chiffres masqués."""
    texte = _URL.sub(r"\1", texte or "")
    return _CHIFFRES.sub("[chiffres masqués]", _EMAIL.sub("[e-mail masqué]", texte))


def premiers_mots(corps: str, mots: int = 12) -> str:
    """Premiers mots d'une réponse (balises retirées), pour le journal, sans données personnelles."""
    texte = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", corps or "")
    texte = " ".join(re.sub(r"<[^>]+>", " ", texte).split())
    return sans_donnees(" ".join(texte.split(" ")[:mots]))[:160]


def _lignes(texte: str) -> list[str]:
    """Texte comparé ligne à ligne, sans les espaces de fin de ligne ni les lignes vides en bout."""
    return [l.rstrip() for l in (texte or "").replace("\r\n", "\n").strip().split("\n")]


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Robot de la campagne La Bonne Boîte (lancé par JobHunter).")
    parser.add_argument("--dossier", required=True, type=Path, help="dossier de l'exécution (travail.json)")
    Robot(parser.parse_args(argv).dossier).executer()


if __name__ == "__main__":
    main()
