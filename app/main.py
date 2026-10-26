"""API HTTP : recherche, sauvegarde et classement des offres."""

import sqlite3
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.database import add_job, init_db
from app.france_travail import FranceTravailError, search_jobs
from app.matching import CVError, calculate_match_score, extract_cv_text
from app.models import Job, MatchedJob


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="JobHunter AI",
    version="0.1.0",
    description=(
        "Recherche d'offres France Travail, sauvegarde SQLite sans doublons et "
        "classement par similarité TF-IDF avec un CV local. "
        "Aucune candidature n'est envoyée."
    ),
    lifespan=lifespan,
)


@app.exception_handler(FranceTravailError)
async def france_travail_error_handler(request: Request, exc: FranceTravailError):
    return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})


@app.exception_handler(CVError)
async def cv_error_handler(request: Request, exc: CVError):
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.exception_handler(sqlite3.Error)
async def database_error_handler(request: Request, exc: sqlite3.Error):
    return JSONResponse(
        status_code=503,
        content={"detail": "Impossible d'accéder à la base SQLite. Vérifiez son chemin et ses permissions."},
    )


def search_parameters(
    keyword: Annotated[
        str | None, Query(max_length=200, description="Métier ou mots-clés, ex. Data Scientist")
    ] = None,
    location: Annotated[
        str | None,
        Query(max_length=100, description="Nom exact de commune (Paris) ou code INSEE (75056), pas un code postal"),
    ] = None,
    contract_type: Annotated[
        str | None, Query(max_length=100, description="Code de contrat France Travail, ex. CDI, CDD")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=150, description="Nombre maximal d'offres à récupérer")] = 50,
) -> dict:
    return {
        "keyword": keyword,
        "location": location,
        "contract_type": contract_type,
        "limit": limit,
    }


def find_and_save_jobs(parameters: dict) -> list[dict]:
    jobs = search_jobs(**parameters)
    for job in jobs:
        add_job(job)
    return jobs


from pathlib import Path
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import api_router

app.include_router(api_router)
if Path("frontend").is_dir():
    app.mount("/frontend", StaticFiles(directory="frontend"), name="frontend")


@app.get("/", tags=["État"], summary="Interface Web JobHunter AI ou état API")
def root(request: Request):
    accept = request.headers.get("accept", "")
    frontend_index = Path("frontend/index.html")
    if "text/html" in accept and frontend_index.is_file():
        return FileResponse(frontend_index)
    return {"message": "JobHunter AI fonctionne", "version": "0.1.0", "docs": "/docs"}


@app.get("/app", tags=["Interface"], summary="Application Web")
def web_app():
    return FileResponse("frontend/index.html")


@app.get(
    "/jobs",
    response_model=list[Job],
    tags=["Offres"],
    summary="Rechercher et enregistrer des offres",
    responses={
        422: {"description": "Paramètres de recherche invalides"},
        502: {"description": "Erreur du service externe"},
        503: {"description": "Configuration manquante, quota ou stockage indisponible"},
        504: {"description": "Délai d'attente du service externe dépassé"},
    },
)
def jobs(parameters: Annotated[dict, Depends(search_parameters)]) -> list[dict]:
    """Retourne au plus `limit` offres et les conserve en SQLite ; aucune offre donne `[]`."""
    return find_and_save_jobs(parameters)


@app.get(
    "/jobs/match",
    response_model=list[MatchedJob],
    tags=["Offres"],
    summary="Classer les offres selon le CV local",
    responses={
        422: {"description": "Paramètres invalides ou CV absent, illisible ou sans texte"},
        502: {"description": "Erreur du service externe"},
        503: {"description": "Configuration manquante, quota ou stockage indisponible"},
        504: {"description": "Délai d'attente du service externe dépassé"},
    },
)
def matched_jobs(parameters: Annotated[dict, Depends(search_parameters)]) -> list[dict]:
    """Score lexical de 0 à 100, décroissant, parmi les offres récupérées uniquement."""
    cv_text = extract_cv_text(get_settings().cv_path)
    jobs = find_and_save_jobs(parameters)
    scored_jobs = [
        {**job, "match_score": calculate_match_score(cv_text, job.get("description"))}
        for job in jobs
    ]
    return sorted(scored_jobs, key=lambda job: job["match_score"], reverse=True)
