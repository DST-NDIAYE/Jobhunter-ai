"""Candidatures spontanées : lot du jour La Bonne Boîte, message issu d'un modèle fixe, historique et réglages.

- Entreprises : API officielle La Bonne Boîte v2 (francetravail.io), métiers Data / ML / IA, France entière ou
  autour d'une ville, et seulement celles joignables par le formulaire du site (champ « email » à « yes »).
- Au plus 10 entreprises par jour, jamais une entreprise déjà proposée ou déjà contactée, ancien module compris.
- Le message vient du fichier modele_message_spontane.txt (champs {entreprise}, {ville}, {secteur}), jamais du LLM.
- Rien n'est envoyé ici : « Marquer comme envoyée » enregistre l'envoi fait par le candidat (relance à 10 jours).
"""

from contextlib import closing
from datetime import datetime, time as heure, timedelta, timezone
import json
import os
import re
import shutil
import sqlite3
import string
import time
import unicodedata
from urllib.parse import quote, urljoin, urlsplit
from zoneinfo import ZoneInfo

from app.config import PROJECT_ROOT, project_path
from app.france_travail import (FranceTravailClient, FranceTravailConfigurationError, FranceTravailError,
                                InvalidSearchError, _text)
from app.test_mode import fake_ft_enabled, fake_lbb_search

SEARCH_URL = "https://api.francetravail.io/partenaire/labonneboite/v2/recherche"
SITE_LBB = "https://labonneboite.francetravail.fr"
PARIS = ZoneInfo("Europe/Paris")
PLAFOND_MAX = 10  # entreprises par lot et envois par jour : jamais plus
RELANCE_APRES = timedelta(days=10)
DISTANCE_KM = 30
MESSAGE_MAX = 1500  # longueur maximale du champ du site
CHAMPS = ("entreprise", "ville", "secteur")
# Surchargeable (mode test) : JOBHUNTER_MODELE_SPONTANE ; créé depuis l'exemple versionné au premier usage.
MODELE_PATH = project_path(os.getenv("JOBHUNTER_MODELE_SPONTANE") or "modele_message_spontane.txt")
MODELE_EXEMPLE = PROJECT_ROOT / "modele_message_spontane.exemple.txt"
SEARCH_PROFILE = PROJECT_ROOT / "search_profile.json"
# Codes ROME de La Bonne Boîte (autocomplétion du site) ; « machine learning » relève de l'IA et de la data science.
METIERS = (("M1405", "Data scientist"), ("M1889", "Ingénieur / Ingénieure en Intelligence Artificielle (IA)"),
           ("M1811", "Data engineer"), ("M1419", "Data analyst"))
A_ENVOYER = "À envoyer"
DEJA_CONTACTEE = "Déjà contactée"
STATUTS_APRES_ENVOI = ("Candidature envoyée", "Entretien", "Refus", "Archivée")
# Ancien module : toute tentative compte comme un contact (deux « Échec » avaient bien été envoyées).
ANCIENS_STATUTS = ("Candidature envoyée", "Entretien", "Refus", "Archivée", "Échec")
REGLAGES_DEFAUT = {"envoi_automatique": False, "plafond_par_jour": PLAFOND_MAX, "nom_cv": ""}


class SpontaneesError(Exception):
    """Erreur affichable telle quelle : aucun secret."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def site_lbb() -> str:
    """Site La Bonne Boîte ; JOBHUNTER_LBB_SITE (faux site des tests) n'accepte qu'une adresse locale."""
    value = (os.getenv("JOBHUNTER_LBB_SITE") or "").strip().rstrip("/")
    if not value:
        return SITE_LBB
    parts = urlsplit(value)
    if parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "localhost"):
        raise SpontaneesError("JOBHUNTER_LBB_SITE doit désigner un site local (http://127.0.0.1:…).", 500)
    return value


class LaBonneBoiteClient(FranceTravailClient):
    """API La Bonne Boîte v2 : même application France Travail que les offres, avec son propre scope."""

    SCOPES = ("api_labonneboitev2 search office", "api_labonneboitev2")
    SCOPE_REFUSED = ("L'API La Bonne Boîte v2 n'est pas activée pour votre application France Travail "
                     "(scope api_labonneboitev2 refusé). Sur francetravail.io, ouvrez « Mes applications », "
                     "choisissez votre application et ajoutez l'API « La Bonne Boîte v2 », puis réessayez.")
    AUTH_REFUSED = ("Authentification France Travail refusée : vérifiez FRANCE_TRAVAIL_CLIENT_ID et "
                    "FRANCE_TRAVAIL_CLIENT_SECRET dans .env.")

    def _scopes(self) -> tuple[str, ...]:
        return self.SCOPES

    def recherche(self, rome: str, *, citycode: str | None = None, page: int = 1, page_size: int = 100) -> list[dict]:
        """Une page d'entreprises pour un code ROME, classées par potentiel d'embauche."""
        self._check_credentials()
        params = {"rome": rome, "page": page, "page_size": page_size}
        if citycode:
            params.update(citycode=citycode, distance=DISTANCE_KM)
        token = self._access_token()
        for attempt in range(2):
            response = self._request("GET", SEARCH_URL, "La Bonne Boîte", params=params,
                                     headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
            if response.status_code != 401 or attempt == 1:
                break
            token = self._access_token(rejected_token=token)
        for _ in range(2):
            if response.status_code not in (301, 302, 307, 308):
                break
            # Redirection suivie seulement vers l'API de France Travail ou celle du site La Bonne Boîte.
            target = urljoin(str(response.url), response.headers.get("Location", ""))
            host = urlsplit(target).hostname
            if urlsplit(target).scheme != "https" or host not in ("api.francetravail.io", urlsplit(SITE_LBB).hostname):
                raise FranceTravailError("Redirection La Bonne Boîte inattendue : recherche interrompue.")
            headers = {"Accept": "application/json"}
            if host == "api.francetravail.io":
                headers["Authorization"] = f"Bearer {token}"
            response = self._request("GET", target, "La Bonne Boîte", headers=headers)
        if response.status_code in (401, 403):
            raise FranceTravailConfigurationError(self.SCOPE_REFUSED)
        if response.status_code in (400, 422):
            raise InvalidSearchError("La Bonne Boîte a refusé les critères de recherche (métier ou ville).")
        if response.status_code == 204:
            return []
        self._check_status(response, "La Bonne Boîte")
        payload = self._json(response, "La Bonne Boîte")
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list) or any(not isinstance(i, dict) for i in items) or len(items) > page_size:
            raise FranceTravailError("Réponse La Bonne Boîte invalide.")
        return items


MINUSCULES = {"de", "du", "des", "la", "le", "les", "en", "sur", "sous", "aux", "au", "et", "lès", "lez", "d", "l",
              "à", "arrondissement"}
SEPARATEURS = re.compile(r"([\s\-'’])")


def joli_nom(text: str) -> str:
    """Commune lisible : « VILLENEUVE-D'ASCQ » devient « Villeneuve-d'Ascq » ; une casse mixte est gardée."""
    text = " ".join((text or "").split())
    if not text or text != text.upper():
        return text
    words, first = [], True
    for part in SEPARATEURS.split(text.lower()):
        if part and not SEPARATEURS.fullmatch(part):
            part = part if part in MINUSCULES and not first else part[:1].upper() + part[1:]
            first = False
        words.append(part)
    return "".join(words)


def normalize_company(item: dict, rome: str, metier: str) -> dict | None:
    """Entreprise de l'API, ou None si elle n'a ni SIRET valide ni nom."""
    siret = str(item.get("siret") or "").strip()
    name = _text(item.get("office_name")) or _text(item.get("company_name"))
    if not re.fullmatch(r"\d{14}", siret) or not name:
        return None
    try:
        potentiel = round(float(item.get("hiring_potential")), 1)
    except (TypeError, ValueError):
        potentiel = None
    return {"siret": siret, "entreprise": name, "ville": joli_nom(_text(item.get("city")) or ""),
            "code_postal": _text(item.get("postcode")) or "", "secteur": _text(item.get("naf_label")) or "",
            "naf": _text(item.get("naf")) or "", "rome": rome, "metier": metier, "potentiel": potentiel,
            # « email » ne contient jamais d'adresse : « yes » signifie que le formulaire du site est actif.
            "formulaire": str(item.get("email") or "").strip().lower() == "yes"}


def metiers() -> list[tuple[str, str]]:
    """Métiers ciblés (code ROME, libellé) : « metiers_spontanees » de search_profile.json, sinon METIERS."""
    try:
        profile = json.loads(SEARCH_PROFILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        profile = {}
    items = profile.get("metiers_spontanees") if isinstance(profile, dict) else None
    found = []
    for item in items if isinstance(items, list) else []:
        rome, label = (item.get("rome"), item.get("libelle")) if isinstance(item, dict) else (None, None)
        if isinstance(rome, str) and re.fullmatch(r"[A-Za-z]\d{4}", rome.strip()) and isinstance(label, str) and label.strip():
            found.append((rome.strip().upper(), label.strip()))
    return found or list(METIERS)


def aujourd_hui(now: datetime | None = None) -> str:
    """Date du jour à Paris (AAAA-MM-JJ) : un lot par jour calendaire."""
    return (now or datetime.now(timezone.utc)).astimezone(PARIS).date().isoformat()


def _iso(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat(timespec="seconds")


def _anciennes(db) -> set[str]:
    placeholders = ",".join("?" for _ in ANCIENS_STATUTS)
    found = {r[0] for r in db.execute(
        f"SELECT COALESCE(siret, external_id) FROM opportunities WHERE application_sent_at IS NOT NULL "
        f"OR application_status IN ({placeholders})", ANCIENS_STATUTS)}
    found |= {r[0] for r in db.execute(
        "SELECT external_id FROM application_events WHERE channel='LABONNEBOITE' AND event IN ('SENT', 'ERROR')")}
    return {s for s in found if s}


def sirets_exclus(db) -> set[str]:
    """Entreprises déjà proposées dans un lot, ou contactées (ou tentées) par l'ancien module."""
    return {r[0] for r in db.execute("SELECT siret FROM spontanees")} | _anciennes(db)


def constituer_lot(settings, *, ville: str | None = None, now: datetime | None = None, client=None,
                   pause: float = 0.6) -> dict:
    """Lot du jour : le lot existant (sans appel à l'API), ou au plus 10 nouvelles entreprises.

    Une recherche sans résultat ne crée pas de lot : une autre ville reste possible le même jour.
    """
    db_path, today = settings.database_path, aujourd_hui(now)
    if existing := lot_du_jour(db_path, now):
        return {"lot": existing, "nouveau": False}
    ville = (ville or "").strip() or None
    zone = f"{ville} et alentours ({DISTANCE_KM} km)" if ville else "France entière"
    if fake_ft_enabled():
        def search(rome, page):
            return fake_lbb_search(rome, ville=ville, page=page)
    else:
        lbb = client or LaBonneBoiteClient(settings)
        citycode = lbb._resolve_location(ville) if ville else None
        calls = []

        def search(rome, page):
            if calls:
                time.sleep(pause)  # l'API accepte 2 appels par seconde
            calls.append(rome)
            return lbb.recherche(rome, citycode=citycode, page=page)
    with closing(sqlite3.connect(db_path)) as db:
        seen = sirets_exclus(db)
    jobs = metiers()
    found = {rome: [] for rome, _ in jobs}
    to_read = [rome for rome, _ in jobs]
    received, flagged = 0, 0
    for page in (1, 2, 3):
        for rome, label in jobs:
            if rome not in to_read:
                continue
            items = search(rome, page)
            if len(items) < 100:
                to_read.remove(rome)  # dernière page de ce métier
            received += len(items)
            flagged += sum("email" in item for item in items)
            for item in items:
                company = normalize_company(item, rome, label)
                if company and company["formulaire"] and company["siret"] not in seen:
                    seen.add(company["siret"])
                    found[rome].append(company)
        if sum(map(len, found.values())) >= PLAFOND_MAX or not to_read:
            break
    if received and not flagged:
        raise SpontaneesError("La Bonne Boîte n'indique plus quelles entreprises acceptent son formulaire (champ "
                              "« email » absent de la réponse) : lot non constitué, rien n'a été enregistré.", 502)
    for rome in found:
        found[rome].sort(key=lambda c: -(c["potentiel"] or 0))
    lot = []
    while len(lot) < PLAFOND_MAX and any(found.values()):
        for rome, _ in jobs:  # un métier après l'autre, du premier au dernier
            if found[rome] and len(lot) < PLAFOND_MAX:
                lot.append(found[rome].pop(0))
    with closing(sqlite3.connect(db_path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM spontanees WHERE lot_du = ? LIMIT 1", (today,)).fetchone():
            db.executemany(
                "INSERT OR IGNORE INTO spontanees (siret, entreprise, ville, code_postal, secteur, naf, rome, metier, "
                "potentiel, lot_du, lot_zone) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(c["siret"], c["entreprise"], c["ville"], c["code_postal"], c["secteur"], c["naf"], c["rome"],
                  c["metier"], c["potentiel"], today, zone) for c in lot])
    return {"lot": lot_du_jour(db_path, now), "nouveau": True}


# Caractères gardés par le champ du site (directive appSanitizeResumeText de La Bonne Boîte) ; le reste est retiré.
REFUSES_LBB = re.compile(r"[^a-zA-ZÀ-ÿŸœŒæÆ0-9\n .,;:!?'\"()\-’«»`\\/@&€+*|<>^]")
EQUIVALENTS = str.maketrans({"–": "-", "—": "-", "‑": "-", "‘": "’", "“": '"',
                             "”": '"', "\t": " "})


def charger_modele(path=None) -> str:
    """Texte du modèle ; le fichier est créé depuis l'exemple versionné s'il n'existe pas encore."""
    path = path or MODELE_PATH
    if not path.is_file():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(MODELE_EXEMPLE, path)
        except OSError:
            raise SpontaneesError(f"Modèle de message introuvable : créez le fichier {path}.", 500) from None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise SpontaneesError(f"Modèle de message illisible : {path} (texte UTF-8 attendu).", 500) from None


def _champs(modele: str) -> list[str]:
    return [name for _, name, _, _ in string.Formatter().parse(modele) if name is not None]


def problemes_modele(modele: str) -> list[str]:
    """Ce qui empêche d'utiliser le modèle : accolades, champ inconnu, texte vide."""
    try:
        fields = _champs(modele)
    except ValueError:
        return ["Accolade mal fermée dans le modèle : écrivez les champs ainsi : {entreprise}, {ville}, {secteur}."]
    problems = []
    if unknown := sorted({f for f in fields if f not in CHAMPS}):
        problems.append("Champ inconnu dans le modèle : " + ", ".join("{" + f + "}" for f in unknown)
                        + ". Champs possibles : {entreprise}, {ville}, {secteur}.")
    if not modele.strip():
        problems.append("Le modèle de message est vide.")
    return problems


def nettoyer_message(texte: str) -> tuple[str, list[str]]:
    """Texte tel que le champ du site le garde, et caractères qu'il aurait retirés (tirets longs remplacés)."""
    texte = unicodedata.normalize("NFKC", texte or "").replace("\r\n", "\n").replace("\r", "\n").translate(EQUIVALENTS)
    removed = sorted(set(REFUSES_LBB.findall(texte)))
    texte = REFUSES_LBB.sub("", texte)
    texte = re.sub(r"[ ]+$", "", texte, flags=re.M)
    return re.sub(r"\n{3,}", "\n\n", texte).strip(), removed


def message_pour(modele: str, entreprise: dict) -> dict:
    """Message prêt à coller pour une entreprise, et les problèmes qui empêchent de l'utiliser tel quel."""
    problems = problemes_modele(modele)
    if problems:
        return {"texte": "", "problemes": problems, "retires": []}
    values = {field: (entreprise.get(field) or "").strip() for field in CHAMPS}
    texte, removed = nettoyer_message(modele.format_map(values))
    if empty := [f for f in CHAMPS if f in _champs(modele) and not values[f]]:
        problems.append("Information absente pour cette entreprise : " + ", ".join("{" + f + "}" for f in empty)
                        + ". Complétez le message avant de l'utiliser.")
    if re.search(r"(?i)https?:|www\.", texte):
        problems.append("Le message contient un lien (http, www) : France Travail peut refuser l'envoi. "
                        "Retirez-le du modèle.")
    if len(texte) > MESSAGE_MAX:
        problems.append(f"Message trop long : {len(texte)} caractères, {MESSAGE_MAX} au plus sur La Bonne Boîte.")
    if not texte:
        problems.append("Le message est vide.")
    return {"texte": texte, "problemes": problems, "retires": removed}


def url_entreprise(siret: str) -> str:
    return f"{site_lbb()}/entreprise/{siret}"


def url_formulaire(item: dict) -> str:
    """Formulaire de candidature spontanée du site, avec le métier ciblé (comme depuis sa recherche)."""
    return (f"{site_lbb()}/candidature/nouvelle/{item['siret']}?job={quote(item['rome'])}"
            f"&jobLabel={quote(item['metier'])}")


def relance(envoyee_le: str | None, now: datetime) -> tuple[str | None, bool]:
    """Relance conseillée dix jours après l'envoi."""
    if not envoyee_le:
        return None, False
    due = datetime.fromisoformat(envoyee_le) + RELANCE_APRES
    return _iso(due), now >= due


def _item(row: sqlite3.Row, modele: str | None, now: datetime) -> dict:
    item = dict(row)
    # Pas de relance après un entretien, un refus ou un archivage.
    item["relance_le"], item["relance_conseillee"] = relance(
        item["envoyee_le"] if item["statut"] == "Candidature envoyée" else None, now)
    item["url_entreprise"], item["url_formulaire"] = url_entreprise(item["siret"]), url_formulaire(item)
    if item["envoyee_le"] or item["statut"] != A_ENVOYER:
        item["message"] = {"texte": item["message_envoye"] or "", "problemes": [], "retires": []}
    else:
        item["message"] = message_pour(modele, item) if modele is not None else None
    return item


def _rows(db_path, where: str, params: tuple, now: datetime | None, modele: str | None) -> list[dict]:
    with closing(sqlite3.connect(db_path)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(f"SELECT * FROM spontanees {where}", params).fetchall()
    return [_item(row, modele, now or datetime.now(timezone.utc)) for row in rows]


def lot_du_jour(db_path, now: datetime | None = None, modele: str | None = None) -> list[dict]:
    return _rows(db_path, "WHERE lot_du = ? ORDER BY rowid", (aujourd_hui(now),), now, modele)


def historique(db_path, now: datetime | None = None) -> list[dict]:
    """Toutes les candidatures spontanées, des lots les plus récents aux plus anciens."""
    return _rows(db_path, "ORDER BY lot_du DESC, rowid", (), now, None)


def entreprise(db_path, siret: str, modele: str | None = None, now: datetime | None = None) -> dict:
    found = _rows(db_path, "WHERE siret = ?", (siret,), now, modele)
    if not found:
        raise SpontaneesError("Entreprise introuvable dans vos candidatures spontanées.", 404)
    return found[0]


def anciennes_contactees(db_path) -> int:
    """Entreprises contactées (ou tentées) par l'ancien module, exclues des lots."""
    with closing(sqlite3.connect(db_path)) as db:
        return len(_anciennes(db))


def marquer_envoyee(db_path, siret: str, *, par: str = "vous", message: str | None = None,
                    now: datetime | None = None) -> dict:
    """Envoi fait par le candidat (ou par le robot si l'envoi automatique est activé) ; la date reste la première."""
    now = now or datetime.now(timezone.utc)
    with closing(sqlite3.connect(db_path)) as db, db:
        row = db.execute("SELECT envoyee_le FROM spontanees WHERE siret = ?", (siret,)).fetchone()
        if row is None:
            raise SpontaneesError("Entreprise introuvable dans vos candidatures spontanées.", 404)
        if row[0] is None:
            db.execute("UPDATE spontanees SET statut = ?, statut_le = ?, envoyee_le = ?, envoyee_par = ?, "
                       "message_envoye = ? WHERE siret = ?",
                       ("Candidature envoyée", _iso(now), _iso(now), par, message, siret))
    return entreprise(db_path, siret, now=now)


def changer_statut(db_path, siret: str, statut: str, now: datetime | None = None) -> dict:
    """Suite d'une candidature envoyée (entretien, refus…), depuis le suivi."""
    if statut not in STATUTS_APRES_ENVOI:
        raise SpontaneesError(f"Statut non disponible pour une candidature spontanée : {statut}.")
    now = now or datetime.now(timezone.utc)
    with closing(sqlite3.connect(db_path)) as db, db:
        cursor = db.execute("UPDATE spontanees SET statut_le = CASE WHEN statut IS NOT ? THEN ? ELSE statut_le END, "
                            "statut = ?, envoyee_le = COALESCE(envoyee_le, ?) WHERE siret = ?",
                            (statut, _iso(now), statut, _iso(now), siret))
        if cursor.rowcount != 1:
            raise SpontaneesError("Entreprise introuvable dans vos candidatures spontanées.", 404)
    return entreprise(db_path, siret, now=now)


def noter_robot(db_path, siret: str, note: str, *, statut: str | None = None, now: datetime | None = None) -> None:
    """Dernier résultat du robot pour une entreprise (lisible), et statut « Déjà contactée » le cas échéant."""
    now = now or datetime.now(timezone.utc)
    with closing(sqlite3.connect(db_path)) as db, db:
        db.execute("UPDATE spontanees SET robot_note = ?, robot_le = ?, statut = COALESCE(?, statut), "
                   "statut_le = CASE WHEN ? IS NOT NULL THEN ? ELSE statut_le END WHERE siret = ?",
                   (note, _iso(now), statut, statut, _iso(now), siret))


def envois_du_jour(db_path, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    start = datetime.combine(now.astimezone(PARIS).date(), heure.min, tzinfo=PARIS)
    with closing(sqlite3.connect(db_path)) as db:
        return db.execute("SELECT COUNT(*) FROM spontanees WHERE envoyee_le >= ?", (_iso(start),)).fetchone()[0]


def _valide(cle: str, valeur):
    if cle == "envoi_automatique" and isinstance(valeur, bool):
        return valeur
    if cle == "plafond_par_jour" and isinstance(valeur, int) and not isinstance(valeur, bool) \
            and 1 <= valeur <= PLAFOND_MAX:
        return valeur
    if cle == "nom_cv" and isinstance(valeur, str) and len(valeur.strip()) <= 120:
        return valeur.strip()
    raise ValueError(cle)


MESSAGES_REGLAGES = {"envoi_automatique": "L'envoi automatique doit valoir vrai ou faux.",
                     "plafond_par_jour": f"Le plafond doit être un nombre entier entre 1 et {PLAFOND_MAX} par jour.",
                     "nom_cv": "Le nom du CV doit faire 120 caractères au plus."}


def lire_reglages(db_path) -> dict:
    """Réglages du robot ; une valeur absente ou invalide reprend sa valeur par défaut (envoi automatique : non)."""
    with closing(sqlite3.connect(db_path)) as db:
        stored = dict(db.execute("SELECT cle, valeur FROM reglages WHERE cle LIKE 'spontanees.%'"))
    reglages = dict(REGLAGES_DEFAUT)
    for cle in reglages:
        try:
            reglages[cle] = _valide(cle, json.loads(stored[f"spontanees.{cle}"]))
        except (KeyError, ValueError):
            pass
    return reglages


def ecrire_reglages(db_path, changes: dict) -> dict:
    unknown = set(changes) - set(REGLAGES_DEFAUT)
    if unknown:
        raise SpontaneesError("Réglage inconnu : " + ", ".join(sorted(unknown)) + ".")
    reglages = lire_reglages(db_path)
    for cle, valeur in changes.items():
        try:
            reglages[cle] = _valide(cle, valeur)
        except ValueError:
            raise SpontaneesError(MESSAGES_REGLAGES[cle]) from None
    with closing(sqlite3.connect(db_path)) as db, db:
        db.executemany("INSERT INTO reglages (cle, valeur) VALUES (?, ?) ON CONFLICT(cle) DO UPDATE SET valeur = excluded.valeur",
                       [(f"spontanees.{cle}", json.dumps(valeur, ensure_ascii=False)) for cle, valeur in reglages.items()])
    return reglages
