"""Recherche paginée multi-métiers, déduplication et matching global."""

from collections.abc import Callable
import math
from time import sleep

from app.config import get_settings
from app.database import add_job, init_db
from app.france_travail import FranceTravailClient, FranceTravailError, MAX_RESULTS
from app.matching import calculate_match_score, extract_cv_text

MAX_KEYWORDS = 50
MAX_REQUESTS = 200
REQUEST_INTERVAL = 0.25


def clean_keywords(keywords: list[str]) -> list[str]:
    unique = {}
    for keyword in keywords:
        keyword = " ".join(keyword.split())
        if keyword:
            if len(keyword) > 200:
                raise ValueError("Chaque mot-clé doit contenir au plus 200 caractères.")
            unique.setdefault(keyword.casefold(), keyword)
    if not 1 <= len(unique) <= MAX_KEYWORDS:
        raise ValueError(f"Renseignez entre 1 et {MAX_KEYWORDS} métiers distincts.")
    return list(unique.values())


def search_global(
    keywords: list[str], location: str | None = None, contract_type: str | None = None,
    limit: int = 200, on_progress: Callable[[dict], None] | None = None,
    *, client: FranceTravailClient | None = None,
) -> dict:
    """La limite porte sur les offres uniques, tous métiers confondus.

    Les pages sont réparties à tour de rôle. Le classement porte uniquement sur
    les offres effectivement récupérées, pas sur l'ensemble du catalogue.
    """
    keywords = clean_keywords(keywords)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError("La limite globale doit être comprise entre 1 et 500 offres uniques.")
    settings = get_settings()
    cv_text = extract_cv_text(settings.cv_path)
    init_db(settings.database_path)
    client = client if client is not None else FranceTravailClient()
    unique = {}
    offsets = dict.fromkeys(keywords, 0)
    seen_per_keyword = {keyword: set() for keyword in keywords}
    attempted = set()
    errors, warnings = [], []
    raw_count = request_count = 0
    stop_for_quota = False
    stats = {keyword: {"pages": 0, "received": 0, "total": None} for keyword in keywords}

    def progress(keyword: str) -> None:
        if on_progress:
            on_progress({
                "keyword": keyword, "keywords_searched": len(attempted),
                "keyword_count": len(keywords), "raw_count": raw_count,
                "unique_count": len(unique), "duplicates": raw_count - len(unique),
                "request_count": request_count,
            })

    while offsets and len(unique) < limit and request_count < MAX_REQUESTS and not stop_for_quota:
        round_keywords = list(offsets)
        for index, keyword in enumerate(round_keywords):
            if len(unique) >= limit or request_count >= MAX_REQUESTS or stop_for_quota:
                break
            remaining = limit - len(unique)
            page_size = min(MAX_RESULTS, math.ceil(remaining / (len(round_keywords) - index)))
            offset = offsets[keyword]
            if request_count:
                sleep(REQUEST_INTERVAL)
            attempted.add(keyword)
            request_count += 1
            progress(keyword)
            try:
                page = client.search_page(
                    keyword=keyword, location=location, contract_type=contract_type,
                    limit=page_size, offset=offset, france_only=True,
                )
            except FranceTravailError as exc:
                errors.append({"keyword": keyword, "offset": offset, "message": str(exc)})
                del offsets[keyword]
                if exc.retry_after is not None:
                    if exc.retry_after > 30:
                        stop_for_quota = True
                        warnings.append("Quota API : recherche interrompue pour respecter Retry-After (> 30 s). Réessayez plus tard.")
                    else:
                        sleep(exc.retry_after)
                progress(keyword)
                continue
            stats[keyword]["pages"] += 1
            stats[keyword]["received"] += len(page.jobs)
            stats[keyword]["total"] = page.total
            raw_count += len(page.jobs)
            page_ids = set()
            for job in page.jobs:
                identity = (job["source"], job["external_id"])
                page_ids.add(identity)
                if identity not in unique:
                    unique[identity] = {
                        **job, "keywords": [keyword],
                        "match_score": calculate_match_score(cv_text, job.get("description")),
                    }
                elif keyword not in unique[identity]["keywords"]:
                    unique[identity]["keywords"].append(keyword)
                add_job(unique[identity], settings.database_path)
            repeated_page = bool(page_ids) and page_ids.issubset(seen_per_keyword[keyword])
            seen_per_keyword[keyword].update(page_ids)
            if page.window_reached:
                warnings.append(f"{keyword} : fenêtre de pagination de prudence atteinte (1 000 offres).")
            if repeated_page or (page.next_offset is not None and page.next_offset <= offset):
                warnings.append(f"{keyword} : pagination répétée ou sans progression ; arrêt de ce mot-clé.")
                del offsets[keyword]
            elif page.next_offset is None:
                del offsets[keyword]
            else:
                offsets[keyword] = page.next_offset
            progress(keyword)
    if request_count >= MAX_REQUESTS and offsets:
        warnings.append(f"Budget de {MAX_REQUESTS} requêtes atteint ; résultats partiels.")
    limit_reached = len(unique) >= limit
    if limit_reached:
        warnings.append(f"Limite globale de {limit} offres uniques atteinte ; le catalogue peut contenir d'autres offres.")
    if len(attempted) < len(keywords):
        warnings.append(f"{len(keywords) - len(attempted)} métier(s) non interrogé(s) en raison des limites atteintes.")
    return {
        "jobs": sorted(unique.values(), key=lambda job: job["match_score"], reverse=True),
        "keyword_count": len(keywords), "keywords_searched": len(attempted),
        "raw_count": raw_count, "duplicates": raw_count - len(unique),
        "unique_count": len(unique), "request_count": request_count,
        "errors": errors, "warnings": warnings, "limit_reached": limit_reached,
        "keyword_stats": stats,
    }
