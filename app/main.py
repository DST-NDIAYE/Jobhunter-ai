"""Application FastAPI : API de JobHunter AI et interface web."""

import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from app.api import api_router
from app.api_spontanees import spontanees_router
from app.config import PROJECT_ROOT
from app.database import init_db
from app.france_travail import FranceTravailError
from app.spontanees import SpontaneesError

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
        "candidatures spontanées La Bonne Boîte (lot du jour, robot assisté), suivi des candidatures. Aucun e-mail "
        "n'est envoyé ; aucune candidature non plus, sauf par le robot si vous activez l'envoi automatique "
        "(désactivé par défaut)."
    ),
    lifespan=lifespan,
)


@app.exception_handler(FranceTravailError)
async def france_travail_error_handler(request: Request, exc: FranceTravailError):
    return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})


@app.exception_handler(SpontaneesError)
async def spontanees_error_handler(request: Request, exc: SpontaneesError):
    return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})


@app.exception_handler(sqlite3.Error)
async def database_error_handler(request: Request, exc: sqlite3.Error):
    return JSONResponse(
        status_code=503,
        content={"detail": "Impossible d'accéder à la base SQLite. Vérifiez son chemin et ses permissions."},
    )


app.include_router(api_router)
app.include_router(spontanees_router)
if FRONTEND.is_dir():
    app.mount("/frontend", StaticFiles(directory=FRONTEND), name="frontend")


INDEX = FRONTEND / "index.html"
INTERFACE_ABSENTE = (f"<h1>Interface introuvable</h1><p>Le fichier {INDEX} est absent. "
                     "L'API fonctionne : voir <a href=\"/docs\">/docs</a>.</p>")


def _interface():
    # Jamais de 404 sur l'interface : la page, ou un message explicite si le fichier manque.
    if INDEX.is_file():
        return FileResponse(INDEX)
    return HTMLResponse(INTERFACE_ABSENTE, status_code=503)


@app.get("/", tags=["Interface"], summary="Interface web, ou état de l'API pour un client non navigateur")
def root(request: Request):
    if "text/html" in request.headers.get("accept", ""):
        return _interface()
    return {"message": "JobHunter AI fonctionne", "version": app.version, "docs": "/docs"}


@app.get("/app", tags=["Interface"], summary="Interface web")
def web_app():
    return _interface()


@app.get("/index.html", include_in_schema=False)
def index_html():
    return RedirectResponse("/")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)
