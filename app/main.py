"""Application FastAPI : API de JobHunter AI et interface web."""

import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import api_router
from app.config import PROJECT_ROOT
from app.database import init_db
from app.france_travail import FranceTravailError

FRONTEND = PROJECT_ROOT / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="JobHunter AI",
    version="0.2.0",
    description=(
        "Recherche d'offres France Travail classées selon le CV maître, CV et lettre adaptés à chaque offre, "
        "suivi des candidatures. Aucune candidature ni aucun e-mail n'est envoyé automatiquement."
    ),
    lifespan=lifespan,
)


@app.exception_handler(FranceTravailError)
async def france_travail_error_handler(request: Request, exc: FranceTravailError):
    return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})


@app.exception_handler(sqlite3.Error)
async def database_error_handler(request: Request, exc: sqlite3.Error):
    return JSONResponse(
        status_code=503,
        content={"detail": "Impossible d'accéder à la base SQLite. Vérifiez son chemin et ses permissions."},
    )


app.include_router(api_router)
if FRONTEND.is_dir():
    app.mount("/frontend", StaticFiles(directory=FRONTEND), name="frontend")


@app.get("/", tags=["Interface"], summary="Interface web, ou état de l'API pour un client non navigateur")
def root(request: Request):
    index = FRONTEND / "index.html"
    if "text/html" in request.headers.get("accept", "") and index.is_file():
        return FileResponse(index)
    return {"message": "JobHunter AI fonctionne", "version": app.version, "docs": "/docs"}


@app.get("/app", tags=["Interface"], summary="Interface web")
def web_app():
    return FileResponse(FRONTEND / "index.html")
