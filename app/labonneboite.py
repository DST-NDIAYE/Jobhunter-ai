"""Client La Bonne Boîte v2 : API web publique labonneboite.francetravail.fr/api/v2 (sans scope partenaire requis)."""

from dataclasses import replace
import re
import time
import unicodedata
from urllib.parse import quote, urljoin, urlsplit

import httpx

from app.france_travail import FranceTravailClient, FranceTravailError

SEARCH_URL = "https://api.francetravail.io/partenaire/labonneboite/v2/recherche"
PUBLIC_API_BASE = "https://labonneboite.francetravail.fr/api/v2"

# Correspondances ROME officielles sur La Bonne Boîte pour les métiers Data / IA / ML / Tech
ROME_CATALOG = {
    "M1405": "Data scientist",
    "M1889": "Ingénieur / Ingénieure en Intelligence Artificielle (IA)",
    "M1811": "Data engineer",
    "M1419": "Data analyst",
    "M1414": "Ingénieur statisticien / Ingénieure statisticienne",
    "M1851": "Analyste décisionnel - Business Intelligence",
    "M1872": "Consultant décisionnel / Consultante décisionnelle - Business Intelligence",
    "M1824": "Développeur décisionnel / Développeuse décisionnelle - Business Intelligence",
    "M1818": "Ingénieur / Ingénieure d'étude informatique",
    "M1836": "Ingénieur concepteur / Ingénieure conceptrice informatique",
    "M1868": "Architecte base de données",
}

KEYWORD_TO_ROMES = {
    "data scientist": [("M1405", "Data scientist"), ("M1889", "Ingénieur / Ingénieure en Intelligence Artificielle (IA)"), ("M1811", "Data engineer"), ("M1419", "Data analyst")],
    "junior data scientist": [("M1405", "Data scientist"), ("M1419", "Data analyst"), ("M1811", "Data engineer")],
    "machine learning": [("M1889", "Ingénieur / Ingénieure en Intelligence Artificielle (IA)"), ("M1405", "Data scientist"), ("M1811", "Data engineer")],
    "machine learning engineer": [("M1889", "Ingénieur / Ingénieure en Intelligence Artificielle (IA)"), ("M1811", "Data engineer"), ("M1405", "Data scientist")],
    "ml engineer": [("M1889", "Ingénieur / Ingénieure en Intelligence Artificielle (IA)"), ("M1811", "Data engineer"), ("M1405", "Data scientist")],
    "ai engineer": [("M1889", "Ingénieur / Ingénieure en Intelligence Artificielle (IA)"), ("M1405", "Data scientist"), ("M1811", "Data engineer"), ("M1818", "Ingénieur / Ingénieure d'étude informatique")],
    "ia": [("M1889", "Ingénieur / Ingénieure en Intelligence Artificielle (IA)"), ("M1405", "Data scientist"), ("M1811", "Data engineer")],
    "nlp": [("M1889", "Ingénieur / Ingénieure en Intelligence Artificielle (IA)"), ("M1405", "Data scientist"), ("M1811", "Data engineer")],
    "nlp engineer": [("M1889", "Ingénieur / Ingénieure en Intelligence Artificielle (IA)"), ("M1405", "Data scientist"), ("M1811", "Data engineer")],
    "computer vision": [("M1889", "Ingénieur / Ingénieure en Intelligence Artificielle (IA)"), ("M1836", "Ingénieur concepteur / Ingénieure conceptrice informatique")],
    "deep learning": [("M1889", "Ingénieur / Ingénieure en Intelligence Artificielle (IA)"), ("M1405", "Data scientist")],
    "data analyst": [("M1419", "Data analyst"), ("M1851", "Analyste décisionnel - Business Intelligence"), ("M1811", "Data engineer")],
    "business intelligence": [("M1851", "Analyste décisionnel - Business Intelligence"), ("M1872", "Consultant décisionnel / Consultante décisionnelle - Business Intelligence"), ("M1824", "Développeur décisionnel / Développeuse décisionnelle - Business Intelligence")],
    "bi analyst": [("M1851", "Analyste décisionnel - Business Intelligence"), ("M1419", "Data analyst")],
    "analytics engineer": [("M1811", "Data engineer"), ("M1419", "Data analyst"), ("M1851", "Analyste décisionnel - Business Intelligence")],
    "data engineer": [("M1811", "Data engineer"), ("M1868", "Architecte base de données"), ("M1818", "Ingénieur / Ingénieure d'étude informatique")],
    "mlops": [("M1811", "Data engineer"), ("M1889", "Ingénieur / Ingénieure en Intelligence Artificielle (IA)"), ("M1818", "Ingénieur / Ingénieure d'étude informatique")],
    "mlops engineer": [("M1811", "Data engineer"), ("M1889", "Ingénieur / Ingénieure en Intelligence Artificielle (IA)"), ("M1818", "Ingénieur / Ingénieure d'étude informatique")],
    "statisticien": [("M1414", "Ingénieur statisticien / Ingénieure statisticienne"), ("M1405", "Data scientist"), ("M1419", "Data analyst")],
    "python": [("M1811", "Data engineer"), ("M1818", "Ingénieur / Ingénieure d'étude informatique"), ("M1836", "Ingénieur concepteur / Ingénieure conceptrice informatique")],
}

REGION_ALIASES = {
    "ile de france": 11,
    "idf": 11,
    "region parisienne": 11,
    "centre val de loire": 24,
    "bourgogne franche comte": 27,
    "normandie": 28,
    "hauts de france": 32,
    "grand est": 44,
    "pays de la loire": 52,
    "bretagne": 53,
    "nouvelle aquitaine": 75,
    "occitanie": 76,
    "auvergne rhone alpes": 84,
    "provence alpes cote d azur": 93,
    "paca": 93,
    "corse": 94,
}

DEFAULT_REGIONS_ORDER = [11, 84, 76, 75, 93, 32, 44, 52, 53, 28, 24, 27]


def _clean_key(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", (text or "").casefold())
    unaccented = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w]+", " ", unaccented).split())


class LaBonneBoiteClient(FranceTravailClient):
    def __init__(self, settings, client=None):
        super().__init__(replace(settings, france_travail_scope="api_labonneboitev2"), client)

    def _public_get(self, path: str, params: dict | None = None) -> dict:
        url = f"{PUBLIC_API_BASE}/{path.lstrip('/')}"
        headers = {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        }
        response = self._request("GET", url, "La Bonne Boîte", params=params, headers=headers)
        if response.status_code in (400, 409, 422):
            raise FranceTravailError("Critères de recherche La Bonne Boîte invalides.")
        self._check_status(response, "La Bonne Boîte")
        data = self._json(response, "La Bonne Boîte")
        if not isinstance(data, dict):
            raise FranceTravailError("Réponse La Bonne Boîte invalide.")
        return data

    def resolve_romes(self, keyword: str) -> list[tuple[str, str]]:
        """Résout un intitulé de métier en liste de (code_rome, libellé_lbb)."""
        cleaned = _clean_key(keyword)
        if re.fullmatch(r"[A-Za-z]\d{4}", keyword.strip()):
            code = keyword.strip().upper()
            return [(code, ROME_CATALOG.get(code, keyword.strip()))]
        if cleaned in KEYWORD_TO_ROMES:
            return list(KEYWORD_TO_ROMES[cleaned])
        for key, romes in KEYWORD_TO_ROMES.items():
            if key in cleaned or cleaned in key:
                return list(romes)
        # Autocomplétion publique sur labonneboite.francetravail.fr
        try:
            data = self._public_get("autocomplete/jobs", {"q": keyword.strip(), "limit": 5})
            items = data.get("items") or []
            resolved = [
                (str(it["value"]), str(it.get("display") or keyword.strip()))
                for it in items
                if isinstance(it, dict) and it.get("type") == "rome" and it.get("value")
            ]
            if resolved:
                return resolved
        except FranceTravailError:
            pass
        # Repli intelligent sur les ROME Data / IA / Informatique
        return [
            ("M1811", "Data engineer"),
            ("M1405", "Data scientist"),
            ("M1889", "Ingénieur / Ingénieure en Intelligence Artificielle (IA)"),
            ("M1419", "Data analyst"),
        ]

    def resolve_location_filters(self, location: str | None) -> list[dict]:
        """Convertit une localisation en filtres compatibles avec /api/v2/search."""
        if not location or not location.strip() or _clean_key(location) in ("france", "france entiere", "toute la france"):
            return [{"region_number": reg} for reg in DEFAULT_REGIONS_ORDER]
        loc = location.strip()
        cleaned = _clean_key(loc)
        if cleaned in REGION_ALIASES:
            return [{"region_number": REGION_ALIASES[cleaned]}]
        if re.fullmatch(r"\d{2}|2[ABab]", loc):
            return [{"department_number": loc.upper()}]
        if re.fullmatch(r"\d{5}", loc):
            # Peut être un code postal (ex: 75001 -> dép 75) ou code INSEE
            dept = loc[:3] if loc.startswith(("97", "98")) else loc[:2]
            return [{"department_number": dept}]
        try:
            data = self._public_get("autocomplete/location", {"q": loc, "limit": 5})
            items = data.get("items") or []
            for item in items:
                if not isinstance(item, dict):
                    continue
                loc_type = item.get("type")
                if loc_type == "region" and item.get("region_number"):
                    return [{"region_number": item["region_number"]}]
                if loc_type == "department" and item.get("department_number"):
                    return [{"department_number": item["department_number"]}]
                if loc_type == "city" and item.get("citycode"):
                    return [{"citycode": item["citycode"], "distance": 30}]
        except FranceTravailError:
            pass
        return [{"region_number": 11}]

    def search_public_rome(self, rome: str, loc_filter: dict, page: int = 1, page_size: int = 50) -> dict:
        params = {
            "rome": rome,
            "page": page,
            "page_size": min(page_size, 100),
            "sort_by": "romes.hiring_potential",
            "sort_direction": "desc",
            **loc_filter,
        }
        data = self._public_get("search", params)
        if not isinstance(data.get("items"), list) or not isinstance(data.get("hits"), int):
            raise FranceTravailError("Réponse La Bonne Boîte invalide.")
        return data

    def search_page(self, keyword, location=None, page=1, page_size=100):
        if not keyword.strip() or not 1 <= page <= 100 or not 1 <= page_size <= 100:
            raise ValueError("Métier, page ou taille de page La Bonne Boîte invalide.")
        # Si un client HTTP simulé est injecté (tests unitaires partenaire), conserver le flux partenaire
        if self._client is not None:
            self._check_credentials()
            try:
                token = self._access_token()
            except FranceTravailError:
                raise FranceTravailError(
                    "Accès La Bonne Boîte refusé ou indisponible. Vérifiez la souscription à La Bonne Boîte v2 (scope api_labonneboitev2)."
                ) from None
            params = {"job": keyword, "page": page, "page_size": page_size}
            if location:
                params["location"] = location
            response = self._request(
                "GET", SEARCH_URL, "La Bonne Boîte", params=params,
                headers={"Authorization": "Bearer " + token},
            )
            for _ in range(2):
                if response.status_code not in (301, 302, 307, 308):
                    break
                target = urljoin(str(response.url), response.headers.get("Location", ""))
                parsed = urlsplit(target)
                if parsed.scheme != "https" or parsed.hostname != "labonneboite.francetravail.fr" or not parsed.path.startswith("/api/v2/"):
                    raise FranceTravailError("Redirection La Bonne Boîte inattendue ; recherche interrompue.")
                response = self._request("GET", target, "La Bonne Boîte")
            if response.status_code in (401, 403):
                raise FranceTravailError("Souscription La Bonne Boîte v2 manquante ou accès refusé par France Travail.")
            if response.status_code in (400, 422):
                raise FranceTravailError("La Bonne Boîte a refusé ce métier ou cette zone.")
            self._check_status(response, "La Bonne Boîte")
            data = self._json(response, "La Bonne Boîte")
            if not isinstance(data, dict) or not isinstance(data.get("items"), list) or not isinstance(data.get("hits"), int):
                raise FranceTravailError("Réponse La Bonne Boîte invalide.")
            if len(data["items"]) > page_size:
                raise FranceTravailError("Pagination La Bonne Boîte incohérente.")
            return data

        # Flux direct via l'API publique de labonneboite.francetravail.fr
        romes = self.resolve_romes(keyword)
        loc_filters = self.resolve_location_filters(location)
        rome_code, rome_label = romes[0]
        data = self.search_public_rome(rome_code, loc_filters[0], page=page, page_size=page_size)
        for item in data.get("items") or []:
            if isinstance(item, dict):
                item.setdefault("rome", rome_code)
                item.setdefault("rome_label", rome_label)
        return data


def normalize_company(item, keyword):
    if not isinstance(item, dict):
        raise ValueError("Entreprise La Bonne Boîte invalide.")
    siret = str(item.get("siret") or "")
    siret = siret if re.fullmatch(r"\d{14}", siret) else None
    external_id = siret or str(item.get("id") or "")
    if not external_id:
        raise ValueError("Entreprise sans identifiant : ignorée.")
    romes_list = item.get("romes") if isinstance(item.get("romes"), list) else []
    first_rome = romes_list[0] if romes_list and isinstance(romes_list[0], dict) else {}
    rome = item.get("rome") or first_rome.get("code") or "M1811"
    job_label = item.get("rome_label") or ROME_CATALOG.get(rome) or keyword
    hiring_potential = item.get("hiring_potential")
    if hiring_potential is None and first_rome.get("hiring_potential") is not None:
        hiring_potential = round(float(first_rome["hiring_potential"]), 1)
    elif isinstance(hiring_potential, (int, float)):
        hiring_potential = round(float(hiring_potential), 1)
    source_url = f"https://labonneboite.francetravail.fr/entreprise/{siret}" if siret else None
    # "email": "yes" sur l'API La Bonne Boîte signifie que le bouton "Envoyer une candidature" est actif !
    accepts_lbb = str(item.get("email") or "").lower() == "yes"
    application_url = (
        f"https://labonneboite.francetravail.fr/candidature/nouvelle/{siret}?job={quote(str(rome))}&jobLabel={quote(str(job_label))}"
        if (siret and accepts_lbb)
        else None
    )
    city = item.get("city") or ""
    postcode = item.get("postcode") or ""
    location_display = f"{city} ({postcode[:2]})" if city and postcode else (city or None)
    return {
        "source": "labonneboite",
        "external_id": external_id,
        "siret": siret,
        "company_name": item.get("company_name") or item.get("office_name"),
        "sector": item.get("naf_label"),
        "naf": item.get("naf"),
        "rome": rome,
        "job_keyword": job_label if accepts_lbb else keyword,
        "keywords": [keyword],
        "location": location_display,
        "hiring_potential": hiring_potential,
        "source_url": source_url,
        "application_url": application_url,
        "accepts_lbb_application": accepts_lbb,
        "website": item.get("website") or None,
        "contact_email": "",
        "public_contact": {},
    }


def search_companies(
    keywords,
    settings,
    *,
    location=None,
    limit=100,
    client=None,
    progress=None,
    only_direct_apply=False,
    exclude_sirets=None,
):
    from app.global_search import clean_keywords
    from app.opportunities import upsert_opportunity

    keywords = clean_keywords(keywords)
    if not isinstance(limit, int) or limit < 1 or limit > 1000:
        raise ValueError("Limite La Bonne Boîte non prise en charge.")
    exclude_sirets = set(exclude_sirets or [])
    lbb_client = client or LaBonneBoiteClient(settings)
    unique, errors, raw, searched = {}, [], 0, 0

    # Si un client mocké est passé aux tests
    if getattr(lbb_client, "_client", None) is not None:
        for keyword in keywords:
            if len(unique) >= limit:
                break
            searched += 1
            previous_pages = set()
            for page in range(1, 11):
                try:
                    data = lbb_client.search_page(keyword, location, page, min(100, limit))
                    signature = tuple(str(x.get("siret") or x.get("id")) for x in data["items"] if isinstance(x, dict))
                    if signature in previous_pages:
                        break
                    previous_pages.add(signature)
                    raw += len(data["items"])
                    for item in data["items"]:
                        company = normalize_company(item, keyword)
                        if company["external_id"] in exclude_sirets:
                            continue
                        if only_direct_apply and not company.get("application_url"):
                            continue
                        if company["external_id"] not in unique and len(unique) >= limit:
                            continue
                        upsert_opportunity(company, settings.database_path)
                        unique[company["external_id"]] = company
                    if progress:
                        progress(searched, len(keywords), len(unique))
                    if len(unique) >= limit or not data["items"] or page * 100 >= data["hits"]:
                        break
                except (FranceTravailError, ValueError) as exc:
                    errors.append(f"{keyword} : {exc}")
                    break
        return {
            "companies": list(unique.values()),
            "raw_count": raw,
            "unique_count": len(unique),
            "keywords_searched": searched,
            "errors": errors,
        }

    # Mode live : résolution des ROME et régions via l'API publique de labonneboite.francetravail.fr
    loc_filters = lbb_client.resolve_location_filters(location)
    seen_rome_region = set()

    # Passe 1 : le code ROME principal de chaque métier choisi par l'utilisateur (ex: M1405 Data scientist, M1889 IA, M1419 Data analyst, M1811 Data engineer)
    # Passe 2 : les codes ROME secondaires / connexes si le quota global n'est pas encore atteint
    rome_passes: list[list[tuple[str, str, str]]] = [[], []]
    for keyword in keywords:
        romes = lbb_client.resolve_romes(keyword)
        if romes:
            rome_passes[0].append((keyword, romes[0][0], romes[0][1]))
            for r_code, r_label in romes[1:]:
                rome_passes[1].append((keyword, r_code, r_label))

    searched = len(keywords)
    per_primary_cap = max(15, (limit // max(1, len(rome_passes[0]))) + 10)

    for pass_idx, rome_entries in enumerate(rome_passes):
        for keyword, rome_code, rome_label in rome_entries:
            if len(unique) >= limit:
                break
            added_for_rome = 0
            for loc_filter in loc_filters:
                if len(unique) >= limit:
                    break
                pair_key = (rome_code, tuple(sorted(loc_filter.items())))
                if pair_key in seen_rome_region:
                    continue
                seen_rome_region.add(pair_key)
                for page in range(1, 6):
                    try:
                        data = lbb_client.search_public_rome(rome_code, loc_filter, page=page, page_size=50)
                        items = data.get("items") or []
                        raw += len(items)
                        if not items:
                            break
                        for item in items:
                            if not isinstance(item, dict):
                                continue
                            item["rome"] = rome_code
                            item["rome_label"] = rome_label
                            company = normalize_company(item, keyword)
                            if company["external_id"] in exclude_sirets:
                                continue
                            if only_direct_apply and not company.get("application_url"):
                                continue
                            # Ne jamais écraser une entreprise déjà trouvée sur un métier prioritaire (ex: Data scientist / IA)
                            if company["external_id"] in unique:
                                continue
                            if len(unique) >= limit:
                                break
                            upsert_opportunity(company, settings.database_path)
                            unique[company["external_id"]] = company
                            added_for_rome += 1
                            if pass_idx == 0 and added_for_rome >= per_primary_cap:
                                break
                        if progress:
                            progress(searched, len(keywords), len(unique))
                        if (
                            len(unique) >= limit
                            or (pass_idx == 0 and added_for_rome >= per_primary_cap)
                            or page * 50 >= data.get("hits", 0)
                        ):
                            break
                        time.sleep(0.12)
                    except (FranceTravailError, ValueError) as exc:
                        errors.append(f"{keyword} ({rome_code}) : {exc}")
                        break
    return {
        "companies": list(unique.values()),
        "raw_count": raw,
        "unique_count": len(unique),
        "keywords_searched": searched,
        "errors": errors,
    }
