"""Client synchrone des API publiques France Travail et découpage administratif."""

from functools import lru_cache
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import re
from threading import Lock
import time
import unicodedata

import httpx

from app.config import Settings, get_settings


TOKEN_URL = "https://entreprise.francetravail.fr/connexion/oauth2/access_token"
SEARCH_URL = "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search"
COMMUNES_URL = "https://geo.api.gouv.fr/communes"
MAX_RESULTS = 150
# Borne de prudence de l'application, indépendante du total annoncé par l'API.
MAX_SEARCH_WINDOW = 1000
INSEE_CODE = re.compile(r"(?:[0-9]{5}|2[AB][0-9]{3})\Z")


class FranceTravailError(Exception):
    """Erreur externe dont le message peut être affiché sans révéler de secrets."""

    def __init__(self, message: str, status_code: int = 502, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


@dataclass
class JobPage:
    jobs: list[dict]
    next_offset: int | None = None
    total: int | None = None
    window_reached: bool = False


class FranceTravailConfigurationError(FranceTravailError):
    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=503)


class InvalidSearchError(FranceTravailError):
    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=422)


def _text(value: object) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _object(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _normalized_name(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    unaccented = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^\w]", " ", unaccented).split())


def normalize_job(offer: dict) -> dict:
    """Conserve les valeurs fournies ; une valeur absente reste None ou []."""
    external_id = _text(offer.get("id"))
    if not external_id:
        raise FranceTravailError("France Travail a renvoyé une offre sans identifiant valide.")
    salary = offer.get("salaire")
    skills = offer.get("competences")
    from app.application_channels import public_email, safe_url
    contact = _object(offer.get("contact"))
    description = _text(offer.get("description"))
    detected_email = public_email(contact.get("courriel")) or public_email(contact.get("urlPostulation"))
    if not detected_email and description:
        for candidate in re.findall(r"[\w.!#$%&'*+/=?^`{|}~-]+@[\w.-]+\.[A-Za-z]{2,}", description):
            valid = public_email(candidate)
            if valid and not any(ignore in valid.lower() for ignore in ("noreply", "no-reply", "francetravail.fr", "pole-emploi.fr")):
                detected_email = valid
                break
    return {
        "external_id": external_id,
        "title": _text(offer.get("intitule")),
        "company": _text(_object(offer.get("entreprise")).get("nom")),
        "location": _text(_object(offer.get("lieuTravail")).get("libelle")),
        "contract_type": _text(offer.get("typeContrat")),
        "description": description,
        "publication_date": _text(offer.get("dateCreation")),
        "salary": salary if isinstance(salary, dict) else None,
        "experience": _text(offer.get("experienceLibelle")),
        "skills": [skill for skill in skills if isinstance(skill, dict)]
        if isinstance(skills, list) else [],
        "url": _text(_object(offer.get("origineOffre")).get("urlOrigine"))
        or _text(_object(offer.get("contact")).get("urlPostulation")),
        "source": "france_travail",
        "contact_email": detected_email,
        "application_url": safe_url(contact.get("urlPostulation")),
        "website": safe_url(_object(offer.get("entreprise")).get("url")),
    }


class FranceTravailClient:
    """Met le jeton en mémoire ; `client` permet les tests sans accès réseau."""

    def __init__(self, settings: Settings | None = None, client: httpx.Client | None = None):
        self.settings = settings if settings is not None else get_settings()
        self._client = client
        self._token: str | None = None
        self._expires_at = 0.0
        self._token_lock = Lock()

    def _check_credentials(self) -> None:
        if not self.settings.france_travail_client_id or not self.settings.france_travail_client_secret:
            raise FranceTravailConfigurationError(
                "Renseignez FRANCE_TRAVAIL_CLIENT_ID et FRANCE_TRAVAIL_CLIENT_SECRET dans .env."
            )

    def _request(self, method: str, url: str, service: str, **kwargs) -> httpx.Response:
        try:
            request = self._client.request if self._client is not None else httpx.request
            return request(method, url, timeout=self.settings.http_timeout, **kwargs)
        except httpx.TimeoutException:
            raise FranceTravailError(f"Le service {service} a dépassé le délai de réponse.", 504) from None
        except httpx.RequestError:
            raise FranceTravailError(f"Impossible de joindre le service {service}.") from None

    @staticmethod
    def _json(response: httpx.Response, service: str) -> object:
        try:
            return response.json()
        except (ValueError, UnicodeDecodeError):
            raise FranceTravailError(f"Le service {service} a renvoyé un JSON invalide.") from None

    def _access_token(self, rejected_token: str | None = None) -> str:
        # Les routes FastAPI synchrones peuvent demander un jeton simultanément.
        with self._token_lock:
            if rejected_token is not None and self._token == rejected_token:
                self._token = None
            if self._token is not None and time.monotonic() < self._expires_at:
                return self._token
            response = self._request(
                "POST", TOKEN_URL, "d'authentification France Travail",
                params={"realm": "/partenaire"},
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.settings.france_travail_client_id,
                    "client_secret": self.settings.france_travail_client_secret,
                    "scope": self.settings.france_travail_scope,
                },
                headers={"Accept": "application/json"},
            )
            if response.status_code in (400, 401, 403):
                raise FranceTravailConfigurationError(
                    "Authentification France Travail refusée : vérifiez les identifiants "
                    "et la souscription à l'API Offres d'emploi v2."
                )
            self._check_status(response, "d'authentification France Travail")
            payload = self._json(response, "d'authentification France Travail")
            token = _text(payload.get("access_token")) if isinstance(payload, dict) else None
            expires_in = payload.get("expires_in") if isinstance(payload, dict) else None
            try:
                lifetime = float(expires_in)
            except (TypeError, ValueError):
                lifetime = 0.0
            if not token or isinstance(expires_in, bool) or not math.isfinite(lifetime) or lifetime <= 0:
                raise FranceTravailError("Réponse d'authentification France Travail incomplète.")
            self._token = token
            self._expires_at = time.monotonic() + lifetime - min(30.0, lifetime / 10)
            return token

    @staticmethod
    def _check_status(response: httpx.Response, service: str) -> None:
        if response.status_code == 429:
            value = response.headers.get("Retry-After", "1")
            try:
                retry_after = float(value)
            except ValueError:
                try:
                    retry_after = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    retry_after = 1.0
            if not math.isfinite(retry_after):
                retry_after = 1.0
            raise FranceTravailError(
                f"Le service {service} limite temporairement les requêtes. Réessayez plus tard.",
                503, retry_after=max(0.0, retry_after),
            )
        if not response.is_success:
            raise FranceTravailError(f"Erreur du service {service} (HTTP {response.status_code}).")

    def _resolve_location(self, location: str) -> str:
        """Accepte un code INSEE ou un nom complet de commune sans ambiguïté."""
        if INSEE_CODE.fullmatch(location.upper()):
            return location.upper()
        if not any(char.isalpha() for char in location):
            raise InvalidSearchError("location doit être un nom de commune ou un code INSEE à cinq caractères.")
        response = self._request(
            "GET", COMMUNES_URL, "de recherche des communes",
            params={"nom": location, "fields": "nom,code"},
        )
        self._check_status(response, "de recherche des communes")
        payload = self._json(response, "de recherche des communes")
        if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
            raise FranceTravailError("Réponse du service des communes invalide.")
        matches = {
            item["code"]: item["nom"] for item in payload
            if isinstance(item.get("nom"), str)
            and isinstance(item.get("code"), str)
            and INSEE_CODE.fullmatch(item["code"])
            and _normalized_name(item["nom"]) == _normalized_name(location)
        }
        if len(matches) == 1:
            return next(iter(matches))
        if len(matches) > 1:
            choices = ", ".join(f"{name} ({code})" for code, name in sorted(matches.items())[:10])
            raise InvalidSearchError(f"Nom de commune ambigu. Utilisez son code INSEE : {choices}.")
        raise InvalidSearchError("Commune introuvable. Utilisez son nom complet ou son code INSEE (pas le code postal).")

    def search_jobs(
        self, keyword: str | None = None, location: str | None = None,
        contract_type: str | None = None, limit: int = 50,
    ) -> list[dict]:
        return self.search_page(keyword, location, contract_type, limit).jobs

    def search_page(
        self, keyword: str | None = None, location: str | None = None,
        contract_type: str | None = None, limit: int = 50, offset: int = 0,
        france_only: bool = False,
    ) -> JobPage:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_RESULTS:
            raise InvalidSearchError(f"limit doit être un entier compris entre 1 et {MAX_RESULTS}.")
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset < MAX_SEARCH_WINDOW:
            raise InvalidSearchError(f"offset doit être compris entre 0 et {MAX_SEARCH_WINDOW - 1}.")
        self._check_credentials()
        limit = min(limit, MAX_SEARCH_WINDOW - offset)
        params = {"range": f"{offset}-{offset + limit - 1}"}
        if france_only:
            params["pays"] = "99100"
        if keyword := _text(keyword):
            params["motsCles"] = keyword
        if contract_type := _text(contract_type):
            params["typeContrat"] = contract_type.upper()
        if location := _text(location):
            params["commune"] = self._resolve_location(location)
            params["distance"] = "0"
        token = self._access_token()
        for attempt in range(2):
            response = self._request(
                "GET", SEARCH_URL, "France Travail", params=params,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
            if response.status_code != 401 or attempt == 1:
                break
            token = self._access_token(rejected_token=token)
        if response.status_code in (400, 422):
            raise InvalidSearchError("France Travail a refusé les critères de recherche. Vérifiez le mot-clé, le contrat et le code INSEE.")
        if response.status_code in (401, 403):
            raise FranceTravailConfigurationError("Accès France Travail refusé. Vérifiez la souscription à l'API Offres d'emploi v2.")
        if response.status_code in (204, 416):
            return JobPage([])
        self._check_status(response, "France Travail")
        payload = self._json(response, "France Travail")
        if not isinstance(payload, dict) or not isinstance(payload.get("resultats"), list):
            raise FranceTravailError("Réponse de recherche France Travail invalide.")
        offers = payload["resultats"]
        if any(not isinstance(offer, dict) for offer in offers):
            raise FranceTravailError("Réponse de recherche France Travail invalide.")
        if len(offers) > limit:
            raise FranceTravailError("France Travail n'a pas respecté la taille de page demandée.")
        jobs = [normalize_job(offer) for offer in offers]
        if not jobs:
            return JobPage([])
        total = None
        next_offset = offset + len(jobs)
        has_more = len(jobs) == limit
        if content_range := response.headers.get("Content-Range"):
            match = re.fullmatch(r"(?:\w+\s+)?(\d+)-(\d+)/(\d+|\*)", content_range.strip())
            if not match:
                raise FranceTravailError("Pagination France Travail invalide (Content-Range).")
            start, end = int(match[1]), int(match[2])
            total = None if match[3] == "*" else int(match[3])
            if start != offset or end - start + 1 != len(jobs) or (total is not None and total <= end):
                raise FranceTravailError("Pagination France Travail incohérente.")
            next_offset = end + 1
            has_more = next_offset < total if total is not None else len(jobs) == limit
        window_reached = has_more and next_offset >= MAX_SEARCH_WINDOW
        return JobPage(jobs, next_offset if has_more and not window_reached else None, total, window_reached)


@lru_cache(maxsize=1)
def _get_client() -> FranceTravailClient:
    return FranceTravailClient()


def search_jobs(
    keyword: str | None = None, location: str | None = None,
    contract_type: str | None = None, limit: int = 50,
) -> list[dict]:
    """Recherche au plus 150 offres ; location désigne une commune, pas un code postal."""
    return _get_client().search_jobs(keyword, location, contract_type, limit)
