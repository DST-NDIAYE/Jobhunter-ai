"""Routes des candidatures spontanées : lot du jour, historique, réglages et robot assisté.

Aucune route n'envoie de candidature : « envoyee » enregistre un envoi fait par le candidat. Seul le robot clique
sur l'envoi du site La Bonne Boîte, et seulement si l'envoi automatique est activé (désactivé par défaut).
"""

import re
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.config import PROJECT_ROOT, get_settings
from app.robot_spontanees import arreter_robot, chemin_capture, decider, etat_robot, lancer_robot
from app.spontanees import (CHAMPS, PLAFOND_MAX, LaBonneBoiteClient, SpontaneesError, anciennes_contactees,
                            aujourd_hui, charger_modele, constituer_lot, ecrire_reglages, entreprise,
                            envois_du_jour, historique, lire_reglages, lot_du_jour, marquer_envoyee, problemes_modele)
from app.test_mode import fake_ft_enabled
import app.spontanees as spontanees

spontanees_router = APIRouter(prefix="/api/spontanees", tags=["Candidatures spontanées"])


class LotRequest(BaseModel):
    ville: str | None = None


class ReglagesRequest(BaseModel):
    # Types vérifiés par ecrire_reglages, pour un message clair plutôt qu'une erreur de validation brute.
    envoi_automatique: Any = None
    plafond_par_jour: Any = None
    nom_cv: Any = None


class RobotRequest(BaseModel):
    mode: str = "essai"


class DecisionRequest(BaseModel):
    action: str
    document: int | None = None  # action « cv » : numéro du document à joindre


def _erreur(exc: SpontaneesError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


def _modele() -> tuple[str | None, list[str]]:
    try:
        modele = charger_modele()
    except SpontaneesError as exc:
        return None, [str(exc)]
    return modele, problemes_modele(modele)


def _chemin_modele() -> str:
    try:
        return str(spontanees.MODELE_PATH.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(spontanees.MODELE_PATH)


@spontanees_router.get("")
def get_spontanees():
    """Lot du jour (avec les messages), historique, modèle de message et réglages du robot."""
    db_path = get_settings().database_path
    modele, problemes = _modele()
    lot = lot_du_jour(db_path, modele=modele)
    return {"date": aujourd_hui(), "lot": lot, "zone": lot[0]["lot_zone"] if lot else None,
            "historique": historique(db_path), "anciennes_contactees": anciennes_contactees(db_path),
            "modele": {"chemin": _chemin_modele(), "champs": list(CHAMPS), "problemes": problemes},
            "plafond_max": PLAFOND_MAX, "envois_du_jour": envois_du_jour(db_path),
            "reglages": lire_reglages(db_path), "mode_test": fake_ft_enabled()}


@spontanees_router.post("/lot")
def post_lot(req: LotRequest):
    """Constitue le lot du jour (au plus 10 entreprises) ; un lot déjà constitué est renvoyé tel quel."""
    try:
        nouveau = constituer_lot(get_settings(), ville=req.ville)["nouveau"]
    except SpontaneesError as exc:
        raise _erreur(exc) from None
    return {**get_spontanees(), "nouveau": nouveau}


@spontanees_router.get("/acces")
def get_acces():
    """Vérifie que l'application France Travail a bien accès à l'API La Bonne Boîte v2 (jeton seulement)."""
    if fake_ft_enabled():
        return {"ok": True, "detail": "Mode test : entreprises fictives, aucun appel à France Travail."}
    client = LaBonneBoiteClient(get_settings())
    client._check_credentials()
    client._access_token()  # sinon erreur claire (gestionnaire FranceTravailError de main.py)
    return {"ok": True, "detail": "Accès à l'API La Bonne Boîte v2 accordé."}


@spontanees_router.get("/reglages")
def get_reglages():
    return {**lire_reglages(get_settings().database_path), "plafond_max": PLAFOND_MAX}


@spontanees_router.put("/reglages")
def put_reglages(req: ReglagesRequest):
    try:
        reglages = ecrire_reglages(get_settings().database_path, req.model_dump(exclude_none=True))
    except SpontaneesError as exc:
        raise _erreur(exc) from None
    return {**reglages, "plafond_max": PLAFOND_MAX}


@spontanees_router.get("/robot")
def get_robot():
    return etat_robot()


@spontanees_router.post("/robot")
def post_robot(req: RobotRequest):
    """Lance le robot (fenêtre visible) : « essai » sur une entreprise sans envoi, ou « assiste » sur le lot."""
    try:
        return lancer_robot(get_settings(), req.mode)
    except SpontaneesError as exc:
        raise _erreur(exc) from None


@spontanees_router.post("/robot/decision")
def post_decision(req: DecisionRequest):
    try:
        return decider(req.action, req.document)
    except SpontaneesError as exc:
        raise _erreur(exc) from None


@spontanees_router.post("/robot/arret")
def post_arret():
    return arreter_robot()


@spontanees_router.get("/robot/captures/{run_id}/{nom}")
def get_capture(run_id: str, nom: str):
    try:
        return FileResponse(chemin_capture(run_id, nom), media_type="image/png")
    except SpontaneesError as exc:
        raise _erreur(exc) from None


@spontanees_router.post("/{siret}/envoyee")
def post_envoyee(siret: str):
    """Enregistre l'envoi que vous avez fait vous-même : date du jour, relance conseillée à 10 jours."""
    if not re.fullmatch(r"\d{14}", siret):
        raise HTTPException(status_code=400, detail="SIRET invalide.")
    db_path = get_settings().database_path
    try:
        modele, _ = _modele()
        message = (entreprise(db_path, siret, modele=modele).get("message") or {}).get("texte") or None
        return {"ok": True, "item": marquer_envoyee(db_path, siret, par="vous", message=message)}
    except SpontaneesError as exc:
        raise _erreur(exc) from None
