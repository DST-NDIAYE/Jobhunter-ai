"""La Bonne Boîte : client de l'API v2, nettoyage des noms et des messages, anciennes candidatures spontanées.

- API officielle La Bonne Boîte v2 (francetravail.io), avec la même application France Travail que les offres.
- Les candidatures spontanées du lot du jour (table « spontanees », remplacé par la campagne : app/campagne.py)
  restent dans le suivi et sont exclues de la campagne.
"""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import os
import re
import sqlite3
import unicodedata
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from app.france_travail import FranceTravailClient, FranceTravailConfigurationError, FranceTravailError, \
    InvalidSearchError, _text

SEARCH_URL = "https://api.francetravail.io/partenaire/labonneboite/v2/recherche"
SITE_LBB = "https://labonneboite.francetravail.fr"
PARIS = ZoneInfo("Europe/Paris")
RELANCE_APRES = timedelta(days=10)
DISTANCE_KM = 30
STATUTS_APRES_ENVOI = ("Candidature envoyée", "Entretien", "Refus", "Archivée")


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
            "raison_sociale": _text(item.get("company_name")) or "",
            "code_postal": _text(item.get("postcode")) or "", "secteur": _text(item.get("naf_label")) or "",
            "naf": _text(item.get("naf")) or "", "rome": rome, "metier": metier, "potentiel": potentiel,
            # « email » ne contient jamais d'adresse : « yes » signifie que le formulaire du site est actif.
            "formulaire": str(item.get("email") or "").strip().lower() == "yes"}


def _iso(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat(timespec="seconds")


# Caractères gardés par le champ du site (directive appSanitizeResumeText de La Bonne Boîte) ; le reste est retiré.
REFUSES_LBB = re.compile(r"[^a-zA-ZÀ-ÿŸœŒæÆ0-9\n .,;:!?'\"()\-’«»`\\/@&€+*|<>^]")
EQUIVALENTS = str.maketrans({"–": "-", "—": "-", "‑": "-", "‘": "’", "“": '"',
                             "”": '"', "\t": " "})


def nettoyer_message(texte: str) -> tuple[str, list[str]]:
    """Texte tel que le champ du site le garde, et caractères qu'il aurait retirés (tirets longs remplacés)."""
    texte = unicodedata.normalize("NFKC", texte or "").replace("\r\n", "\n").replace("\r", "\n").translate(EQUIVALENTS)
    removed = sorted(set(REFUSES_LBB.findall(texte)))
    texte = REFUSES_LBB.sub("", texte)
    texte = re.sub(r"[ ]+$", "", texte, flags=re.M)
    return re.sub(r"\n{3,}", "\n\n", texte).strip(), removed


def url_entreprise(siret: str) -> str:
    return f"{site_lbb()}/entreprise/{siret}"


def relance(envoyee_le: str | None, now: datetime) -> tuple[str | None, bool]:
    """Relance conseillée dix jours après l'envoi."""
    if not envoyee_le:
        return None, False
    due = datetime.fromisoformat(envoyee_le) + RELANCE_APRES
    return _iso(due), now >= due


def _item(row: sqlite3.Row, now: datetime) -> dict:
    item = dict(row)
    # Pas de relance après un entretien, un refus ou un archivage.
    item["relance_le"], item["relance_conseillee"] = relance(
        item["envoyee_le"] if item["statut"] == "Candidature envoyée" else None, now)
    item["url_entreprise"] = url_entreprise(item["siret"])
    return item


def _rows(db_path, where: str, params: tuple, now: datetime | None) -> list[dict]:
    with closing(sqlite3.connect(db_path)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(f"SELECT * FROM spontanees {where}", params).fetchall()
    return [_item(row, now or datetime.now(timezone.utc)) for row in rows]


def entreprise(db_path, siret: str, now: datetime | None = None) -> dict:
    found = _rows(db_path, "WHERE siret = ?", (siret,), now)
    if not found:
        raise SpontaneesError("Entreprise introuvable dans vos candidatures spontanées.", 404)
    return found[0]


def marquer_envoyee(db_path, siret: str, *, par: str = "vous", message: str | None = None,
                    now: datetime | None = None) -> dict:
    """Envoi fait par le candidat (ancien lot du jour) ; la date reste la première."""
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
