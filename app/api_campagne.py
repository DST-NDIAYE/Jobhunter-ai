"""Routes de la campagne La Bonne Boîte : plan, premier passage, robot, réglages, historique, export.

Aucune route n'envoie elle-même de candidature : seul le robot envoie, dans sa fenêtre, une fois le premier passage
terminé (essai, calibrage, cinq envois devant vous). Aucune coordonnée n'est renvoyée.
"""

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from app import campagne as cp
from app import robot_campagne as robot
from app.config import get_settings
from app.france_travail import FranceTravailError
from app.spontanees import LaBonneBoiteClient, SpontaneesError
from app.test_mode import fake_ft_enabled

campagne_router = APIRouter(prefix="/api/campagne", tags=["campagne"])


class RobotRequest(BaseModel):
    mode: str
    siret: str | None = None
    nombre: Any = None  # campagne : nombre de candidatures à envoyer (validé par la campagne)


class ManuelleRequest(BaseModel):
    siret: str
    entreprise: str
    rome: str
    departement: str = ""


class InventaireRequest(BaseModel):
    nouveau: bool = False  # refaire toutes les recherches (inventaire précédent terminé)


class StatutRequest(BaseModel):
    statut: str


class ReponseRequest(BaseModel):
    reponse: str | None = None


class RegleRequest(BaseModel):
    bouton: str
    url_motif: str
    texte: str


def _erreur(exc: SpontaneesError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


def _db():
    return get_settings().database_path


@campagne_router.get("")
def get_campagne():
    """Tout l'écran Campagne en une lecture : état, premier passage, envois du jour, compteurs, plan, réglages, message."""
    db = _db()
    if not robot.robot_actif():
        cp.reprise_apres_plantage(db)  # robot arrêté brutalement : ses entreprises ne sont jamais reprises
    reglages, passage = cp.lire_reglages(db), cp.lire_passage(db)
    return {"reglages": reglages, "passage": passage, "etape": cp.etape_passage(passage),
            "envois_du_jour": cp.envois_du_jour(db), "alerte": cp.alerte_du_jour(db),
            "compteurs": cp.compteurs(db), "plan": cp.plan(db),
            "metiers": cp.metiers_publics(), "message": cp.apercu(db), "versions": cp.stats_versions(db),
            "robot": robot.etat_robot(), "objectif_max": cp.OBJECTIF_MAX,
            "statuts": cp.STATUTS, "motifs": cp.MOTIFS_IGNOREE, "reponses": cp.REPONSES, "mode_test": fake_ft_enabled()}


@campagne_router.get("/acces")
def get_acces():
    """Vérifie l'accès à l'API La Bonne Boîte v2 (jeton seulement, aucune recherche)."""
    if fake_ft_enabled():
        return {"ok": True, "detail": "Mode test : entreprises fictives, aucun appel à France Travail."}
    try:
        client = LaBonneBoiteClient(get_settings())
        client._check_credentials()
        client._access_token()
    except FranceTravailError as exc:
        return {"ok": False, "detail": str(exc)}
    return {"ok": True, "detail": "Accès à l'API La Bonne Boîte v2 autorisé."}


@campagne_router.post("/metiers/verifier")
def post_verifier_metiers():
    """Compare les codes ROME des phases A et B au référentiel officiel (un appel à l'API Offres d'emploi)."""
    return cp.verifier_metiers(get_settings())


@campagne_router.post("/metiers/valider")
def post_valider_metiers():
    return cp.ecrire_passage(_db(), metiers_valides=True)


@campagne_router.post("/inventaire")
def post_inventaire(req: InventaireRequest | None = None):
    reglages = cp.lire_reglages(_db())
    phases = [p for p in ("A", "B") if reglages[f"phase_{p.lower()}"]]
    try:
        return cp.lancer_inventaire(get_settings(), phases, nouveau=bool(req and req.nouveau))
    except SpontaneesError as exc:
        raise _erreur(exc) from None


@campagne_router.put("/reglages")
def put_reglages(changes: dict[str, Any]):
    try:
        return cp.ecrire_reglages(_db(), changes)
    except SpontaneesError as exc:
        raise _erreur(exc) from None


@campagne_router.get("/apercu")
def get_apercu(entreprise: str = "UBER EATS FRANCE SAS"):
    return cp.apercu(_db(), entreprise[:200])


@campagne_router.get("/entreprises")
def get_entreprises(recherche: str = "", limite: int = 20):
    """Entreprises à faire (ou en brouillon), pour choisir celle de l'essai ou du calibrage."""
    db = _db()
    reglages = cp.lire_reglages(db)
    phases = [p for p in ("A", "B") if reglages[f"phase_{p.lower()}"]] or ["A"]
    items = cp.prochaines(db, phases, limite=2000)
    with cp._connect(db) as connection:
        brouillons = [cp._public(r) for r in connection.execute("SELECT * FROM campagne WHERE statut = ?", (cp.BROUILLON,))]
    cle = cp._cle(recherche)
    items = [i for i in brouillons + items if not cle or cle in cp._cle(i["entreprise"] + " " + i["ville"] + " " + i["siret"])]
    return {"items": items[:max(1, min(limite, 100))]}


@campagne_router.post("/entreprises")
def post_entreprise(req: ManuelleRequest):
    try:
        return cp.ajouter_manuelle(_db(), req.siret, req.entreprise, req.rome, req.departement)
    except SpontaneesError as exc:
        raise _erreur(exc) from None


@campagne_router.post("/regle/valider")
def post_valider_regle(req: RegleRequest):
    """Valide la règle de confirmation proposée au calibrage (éventuellement corrigée) ; l'envoi calibré devient envoyé."""
    db = _db()
    passage = cp.lire_passage(db)
    if not passage["regle"]:
        raise HTTPException(status_code=409, detail="Aucune règle proposée : faites d'abord le calibrage.")
    regle = {"bouton": req.bouton.strip()[:120], "url_motif": req.url_motif.strip()[:200], "texte": req.texte.strip()[:200]}
    if not all(regle.values()) or not regle["url_motif"].startswith("/"):
        raise HTTPException(status_code=400, detail="Règle incomplète : bouton, adresse (commençant par /) et texte.")
    if probleme := cp.probleme_regle(regle):
        raise HTTPException(status_code=400, detail=probleme)
    cp.ecrire_passage(db, regle={**passage["regle"], **regle}, regle_validee=True)
    siret = (passage.get("calibrage") or {}).get("siret")
    if siret:
        try:
            cp.resoudre_incertaine(db, siret, cp.ENVOYEE)
        except SpontaneesError:
            pass  # déjà résolue
    return cp.lire_passage(db)


@campagne_router.post("/cinq/valider")
def post_valider_cinq():
    db = _db()
    passage = cp.lire_passage(db)
    if not passage["cinq"] or not passage["cinq"].get("envoyees"):
        raise HTTPException(status_code=409, detail="Faites d'abord les envois devant vous.")
    return cp.ecrire_passage(db, cinq_valide=True)


@campagne_router.get("/robot")
def get_robot():
    return robot.etat_robot()


@campagne_router.post("/robot")
def post_robot(req: RobotRequest):
    try:
        return robot.lancer_robot(get_settings(), req.mode, req.siret, req.nombre)
    except SpontaneesError as exc:
        raise _erreur(exc) from None


@campagne_router.post("/robot/pause")
def post_pause():
    try:
        return robot.pause_robot(True)
    except SpontaneesError as exc:
        raise _erreur(exc) from None


@campagne_router.post("/robot/reprendre")
def post_reprendre():
    try:
        return robot.pause_robot(False)
    except SpontaneesError as exc:
        raise _erreur(exc) from None


@campagne_router.post("/robot/passer")
def post_passer():
    try:
        return robot.decider("passer")
    except SpontaneesError as exc:
        raise _erreur(exc) from None


@campagne_router.post("/robot/arret")
def post_arret():
    return robot.arreter_robot()


@campagne_router.get("/robot/captures/{run_id}/{nom}")
def get_capture(run_id: str, nom: str):
    try:
        return FileResponse(robot.chemin_capture(run_id, nom), media_type="image/png")
    except SpontaneesError as exc:
        raise _erreur(exc) from None


@campagne_router.get("/historique")
def get_historique(statut: str = "tous", phase: str = "", recherche: str = "", reponse: str = "", page: int = 1):
    try:
        return cp.historique(_db(), statut=statut, phase=phase, recherche=recherche[:100], reponse=reponse, page=page)
    except SpontaneesError as exc:
        raise _erreur(exc) from None


@campagne_router.get("/export.csv")
def get_export(statut: str = "tous", phase: str = "", recherche: str = "", reponse: str = ""):
    try:
        contenu = cp.export_csv(_db(), statut=statut, phase=phase, recherche=recherche[:100], reponse=reponse)
    except SpontaneesError as exc:
        raise _erreur(exc) from None
    return Response(contenu, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="campagne-la-bonne-boite.csv"'})


@campagne_router.post("/{siret}/reponse")
def post_reponse(siret: str, req: ReponseRequest):
    try:
        return cp.noter_reponse(_db(), siret, req.reponse)
    except SpontaneesError as exc:
        raise _erreur(exc) from None


@campagne_router.post("/{siret}/main")
def post_main(siret: str):
    """Envoi fait par vous sur le site, hors robot : envoyée, comptée dans le jour, jamais retentée."""
    try:
        return cp.envoyee_a_la_main(_db(), siret)
    except SpontaneesError as exc:
        raise _erreur(exc) from None


@campagne_router.post("/{siret}/statut")
def post_statut(siret: str, req: StatutRequest):
    """Envoi incertain contrôlé par vous : envoyée ou erreur (jamais de nouvel essai)."""
    try:
        return cp.resoudre_incertaine(_db(), siret, req.statut)
    except SpontaneesError as exc:
        raise _erreur(exc) from None
