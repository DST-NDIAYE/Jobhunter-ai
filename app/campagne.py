"""Campagne de candidatures spontanées La Bonne Boîte : cibles, message, rythme, historique.

- Cibles : API officielle La Bonne Boîte v2, métiers des phases A (data, IA, informatique) et B (métiers voisins,
  désactivée par défaut), France entière, Île-de-France d'abord, potentiel d'embauche décroissant, toutes les pages.
  Dédoublonnage par SIRET ; toute entreprise déjà présente dans la base est exclue (ancien module, opportunités,
  candidatures spontanées, campagne).
- Message : modele_message_spontane.txt (et _B.txt s'il existe, en alternance), un seul champ {entreprise}, jamais
  de modèle de langage. Le nom de l'entreprise est nettoyé (casse, forme juridique) ou remplacé par « vous ».
- Envoi : vous choisissez le nombre de candidatures à envoyer ; le robot (app/robot_campagne.py) les enchaîne sans
  pause et s'arrête une fois ce nombre envoyé (confirmé). Une entreprise en échec est mise de côté ; une alerte
  (opposition du site : limite, code 403, captcha non résolu) bloque la journée.
"""

from contextlib import closing
import csv
from datetime import datetime, time as heure, timezone
import io
import json
import os
import re
import sqlite3
import threading
import time
import unicodedata
from urllib.parse import quote

from app.config import project_path
from app.france_travail import FranceTravailClient, FranceTravailConfigurationError, FranceTravailError, InvalidSearchError
from app.spontanees import (PARIS, LaBonneBoiteClient, SpontaneesError, nettoyer_message, normalize_company,
                            site_lbb)
from app.test_mode import fake_ft_enabled, fake_lbb_search
from app.villes_campagne import PETITES_VILLES, VILLES

MESSAGE_MAX = 1500
# Surchargeable (mode test) : JOBHUNTER_MODELE_SPONTANE. La version B est le même nom suivi de « _B ».
MODELE_A = project_path(os.getenv("JOBHUNTER_MODELE_SPONTANE") or "modele_message_spontane.txt")
MODELE_B = MODELE_A.with_name(MODELE_A.stem + "_B" + MODELE_A.suffix)
REFERENTIEL_URL = "https://api.francetravail.io/partenaire/offresdemploi/v2/referentiel/metiers"
PAGE_API = 100
ATTENTES_API = (10, 60)  # nouvel essai d'un appel en erreur passagère : après 10 s, puis 1 min
PAUSE_API = 0.6  # l'API La Bonne Boîte accepte 2 appels par seconde

# Codes ROME vérifiés dans le référentiel officiel des métiers (API Offres d'emploi, le 05/11/2026).
PHASE_A = (
    ("Data et IA", (("M1405", "Data scientist"), ("M1889", "Ingénieur / Ingénieure en Intelligence Artificielle (IA)"),
                    ("M1811", "Data engineer"), ("M1419", "Data analyst"),
                    ("M1414", "Ingénieur statisticien / Ingénieure statisticienne"),
                    ("M1873", "Spécialiste IA embarquée"), ("H1215", "Bioinformaticien / Bioinformaticienne en études, "
                                                                     "recherche et développement"),
                    ("K2403", "Biostatisticien / Biostatisticienne"))),
    ("Décisionnel et bases de données", (("M1824", "Développeur / Développeuse décisionnel - Business Intelligence"),
                                         ("M1851", "Analyste décisionnel - Business Intelligence"),
                                         ("M1872", "Consultant décisionnel / Consultante décisionnelle - Business Intelligence"),
                                         ("M1868", "Architecte base de données"),
                                         ("M1894", "Gestionnaire de base de données"))),
    ("Études et développement informatique", (("M1805", "Développeur / Développeuse informatique"),
                                              ("M1861", "Développeur / Développeuse logiciel ou d'application"),
                                              ("M1855", "Développeur / Développeuse web"),
                                              ("M1818", "Ingénieur / Ingénieure d'étude informatique"),
                                              ("M1841", "Ingénieur informaticien / Ingénieure informaticienne"),
                                              ("M1836", "Ingénieur concepteur / Ingénieure conceptrice informatique"),
                                              ("M1848", "Analyste Concepteur / Conceptrice informatique"),
                                              ("M1852", "Analyste d'étude informatique"),
                                              ("M1821", "Analyste d'application informatique"),
                                              ("M1827", "Ingénieur / Ingénieure DevOps"),
                                              ("M1879", "Ingénieur / Ingénieure Cloud computing"),
                                              ("M1815", "Spécialiste test et validation logiciel ou application"))),
    ("Systèmes d'information", (("M1806", "Consultant fonctionnel / Consultante fonctionnelle des systèmes d'information"),
                                ("M1813", "Intégrateur / Intégratrice logiciels métiers"),
                                ("M1871", "Gestionnaire d'applications système d'information"),
                                ("M1875", "Coordinateur / Coordinatrice Maitrise d'Ouvrage Système d'Information (MOA SI)"))),
)
PHASE_B = (
    ("Études, finance et actuariat", (("M1403", "Chargé / Chargée d'études socio-économiques"),
                                      ("M1430", "Chargé / Chargée d'études commerciales"),
                                      ("M1409", "Ingénieur / Ingénieure économiste en entreprise"),
                                      ("M1201", "Analyste financier / Analyste financière"),
                                      ("M1204", "Contrôleur / Contrôleuse de gestion"),
                                      ("M1228", "Gestionnaire de risques financiers"),
                                      ("C1202", "Analyste de crédits et risques bancaires"),
                                      ("C1120", "Conseiller / Conseillère en études actuarielles"))),
    ("Marketing et web", (("M1718", "Chargé / Chargée de marketing digital"), ("E1405", "Référenceur / Référenceuse web"),
                          ("M1434", "Développeur / Développeuse d'audience"))),
    ("Industrie, qualité et logistique", (("H1402", "Ingénieur / Ingénieure méthodes et process"),
                                          ("H1406", "Ingénieur / Ingénieure supply chain en industrie"),
                                          ("H1520", "Ingénieur qualité / Ingénieure qualité"),
                                          ("H1208", "Automaticien / Automaticienne en bureau d'études"))),
    ("Recherche", (("K2402", "Ingénieur / Ingénieure de recherche scientifique"),
                   ("H1218", "Responsable de projet recherche et développement"))),
    ("Projet, produit et support informatique", (("M1864", "Product Owner"), ("M1814", "Scrum Master"),
                                                 ("M1895", "Géomaticien / Géomaticienne"),
                                                 ("M1822", "Spécialiste Jumeau Numérique"),
                                                 ("M1874", "Spécialiste support"),
                                                 ("I1404", "Conseiller / Conseillère support technique informatique"),
                                                 ("M1810", "Technicien / Technicienne d'exploitation informatique"))),
)
PHASES = {"A": PHASE_A, "B": PHASE_B}

IDF = ("75", "92", "93", "94", "91", "78", "95", "77")
DEPARTEMENTS = IDF + tuple(
    d for d in [f"{n:02d}" for n in range(1, 96) if n != 20] + ["2A", "2B", "971", "972", "973", "974", "976"]
    if d not in IDF)
RANG_DEPARTEMENT = {d: i for i, d in enumerate(DEPARTEMENTS)}

OBJECTIF_MAX = 1000  # candidatures à envoyer par lancement du robot, au plus

A_FAIRE, EN_COURS, BROUILLON = "a_faire", "en_cours", "brouillon"
TENTEE, ENVOYEE, INCERTAINE, ERREUR, IGNOREE = "tentee", "envoyee", "incertaine", "erreur", "ignoree"
STATUTS = {A_FAIRE: "À faire", EN_COURS: "En cours", BROUILLON: "Brouillon (non envoyé)", TENTEE: "Tentée",
           ENVOYEE: "Envoyée", INCERTAINE: "Incertaine", ERREUR: "Erreur", IGNOREE: "Ignorée"}
MOTIFS_IGNOREE = {"sans_email": "Sans e-mail", "deja_candidate": "Déjà candidaté", "formulaire_inactif": "Formulaire inactif",
                  "introuvable": "Introuvable sur le site"}
REPONSES = {"positive": "Positive", "refus": "Refus", "autre": "Autre"}
# Alertes : arrêt, pas de reprise le même jour. Les trois premières interdisent aussi l'essai. Depuis le 07/11, le robot
# n'en pose plus que si le site s'y oppose (captcha non résolu, limite, 403, 429 répété) ; les autres restent lisibles.
ALERTES = {"captcha": "Captcha", "blocage": "Message de limite ou de blocage", "http": "Code 429 ou 403",
           "page_inconnue": "Page non reconnue", "ecart": "Écart de vérification", "erreurs": "3 erreurs de suite",
           "incertain": "Envoi incertain"}
ALERTES_SITE = ("captcha", "blocage", "http")

REGLAGES_DEFAUT = {"objectif": 50, "phase_a": True, "phase_b": False, "nom_cv": ""}
PASSAGE_DEFAUT = {"metiers_valides": False, "essai": None, "calibrage": None, "regle": None, "regle_validee": False,
                  "cinq": None, "cinq_valide": False}

_INVENTAIRE = threading.Lock()


class CampagneError(SpontaneesError):
    """Erreur affichable telle quelle (aucun secret, aucune coordonnée)."""


# --- Base ------------------------------------------------------------------------------------------------------

def _connect(db_path):
    db = sqlite3.connect(db_path, timeout=30)
    db.row_factory = sqlite3.Row
    return closing(db)


def _maintenant() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def jour(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(PARIS).date().isoformat()


def _debut_jour(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    start = datetime.combine(now.astimezone(PARIS).date(), heure.min, tzinfo=PARIS)
    return start.astimezone(timezone.utc).isoformat(timespec="seconds")


# --- Réglages et premier passage -------------------------------------------------------------------------------

def _valide(cle: str, valeur):
    entier = isinstance(valeur, int) and not isinstance(valeur, bool)
    if cle == "objectif" and entier and 1 <= valeur <= OBJECTIF_MAX:
        return valeur
    if cle in ("phase_a", "phase_b") and isinstance(valeur, bool):
        return valeur
    if cle == "nom_cv" and isinstance(valeur, str) and len(valeur.strip()) <= 120:
        return valeur.strip()
    raise ValueError(cle)


MESSAGES_REGLAGES = {
    "objectif": f"Le nombre de candidatures à envoyer doit être un nombre entier entre 1 et {OBJECTIF_MAX}.",
    "phase_a": "La phase A doit valoir vrai ou faux.", "phase_b": "La phase B doit valoir vrai ou faux.",
    "nom_cv": "Le nom du CV doit faire 120 caractères au plus.",
}


def _lire_cle(db_path, cle: str):
    with _connect(db_path) as db:
        row = db.execute("SELECT valeur FROM reglages WHERE cle = ?", (cle,)).fetchone()
    try:
        return json.loads(row[0]) if row else None
    except ValueError:
        return None


def _ecrire_cle(db_path, cle: str, valeur) -> None:
    with _connect(db_path) as db, db:
        db.execute("INSERT INTO reglages (cle, valeur) VALUES (?, ?) ON CONFLICT(cle) DO UPDATE SET valeur = excluded.valeur",
                   (cle, json.dumps(valeur, ensure_ascii=False)))


def lire_reglages(db_path) -> dict:
    """Réglages de la campagne ; une valeur absente ou invalide reprend sa valeur par défaut."""
    stored = _lire_cle(db_path, "campagne.reglages") or {}
    reglages = json.loads(json.dumps(REGLAGES_DEFAUT))
    for cle in reglages:
        if cle in stored:
            try:
                reglages[cle] = _valide(cle, stored[cle])
            except ValueError:
                pass
    return reglages


def ecrire_reglages(db_path, changes: dict) -> dict:
    if unknown := set(changes) - set(REGLAGES_DEFAUT):
        raise CampagneError("Réglage inconnu : " + ", ".join(sorted(unknown)) + ".")
    reglages = lire_reglages(db_path)
    for cle, valeur in changes.items():
        try:
            reglages[cle] = _valide(cle, valeur)
        except ValueError:
            raise CampagneError(MESSAGES_REGLAGES[cle]) from None
    if not (reglages["phase_a"] or reglages["phase_b"]):
        raise CampagneError("Activez au moins une phase (A ou B).")
    _ecrire_cle(db_path, "campagne.reglages", reglages)
    return reglages


def lire_passage(db_path) -> dict:
    """Premier passage : métiers validés, essai, calibrage (règle de confirmation), cinq envois devant vous."""
    stored = _lire_cle(db_path, "campagne.passage") or {}
    return {**PASSAGE_DEFAUT, **{k: v for k, v in stored.items() if k in PASSAGE_DEFAUT}}


def ecrire_passage(db_path, **changes) -> dict:
    passage = {**lire_passage(db_path), **changes}
    _ecrire_cle(db_path, "campagne.passage", passage)
    return passage


def etape_passage(passage: dict) -> int:
    """Première étape non faite (1 à 5) ; 5 : la campagne peut démarrer."""
    if not passage["metiers_valides"]:
        return 1
    if not passage["essai"]:
        return 2
    if not passage["regle_validee"]:
        return 3
    if not passage["cinq_valide"]:
        return 4
    return 5


def regle_confirmation(db_path) -> dict | None:
    passage = lire_passage(db_path)
    return passage["regle"] if passage["regle_validee"] else None


# --- Métiers ----------------------------------------------------------------------------------------------------

def metiers(phase: str) -> list[tuple[str, str]]:
    return [m for _, groupe in PHASES[phase] for m in groupe]


def metiers_publics() -> dict:
    return {phase: [{"groupe": nom, "metiers": [{"rome": r, "libelle": l} for r, l in groupe]}
                    for nom, groupe in groupes] for phase, groupes in PHASES.items()}


def verifier_metiers(settings) -> dict:
    """Compare chaque code au référentiel officiel des métiers (un seul appel à l'API Offres d'emploi)."""
    if fake_ft_enabled():
        referentiel = {r: l for phase in PHASES for r, l in metiers(phase)}
    else:
        client = FranceTravailClient(settings)
        client._check_credentials()
        token = client._access_token()
        response = client._request("GET", REFERENTIEL_URL, "le référentiel des métiers",
                                   headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        client._check_status(response, "le référentiel des métiers")
        data = client._json(response, "le référentiel des métiers")
        if not isinstance(data, list):
            raise FranceTravailError("Référentiel des métiers invalide.")
        referentiel = {str(x.get("code")): str(x.get("libelle")) for x in data if isinstance(x, dict)}
    resultat = {}
    for phase in PHASES:
        for rome, libelle in metiers(phase):
            officiel = referentiel.get(rome)
            resultat[rome] = {"phase": phase, "libelle": libelle, "officiel": officiel,
                              "ok": officiel is not None and _cle(officiel) == _cle(libelle)}
    return {"metiers": resultat, "ok": all(m["ok"] for m in resultat.values()), "nombre_referentiel": len(referentiel)}


# --- Nom de l'entreprise et message -----------------------------------------------------------------------------

FORMES_JURIDIQUES = {"sas", "sasu", "sarl", "sarlu", "sa", "eurl", "snc", "sci", "scp", "scm", "selarl", "selas", "selafa",
                     "selca", "scop", "scic", "sca", "scs", "gie", "ei", "eirl", "se", "sem", "sasp", "earl", "gaec",
                     "scea", "spa", "gmbh", "ltd", "limited", "inc", "llc", "bv", "nv", "ag", "srl", "sl", "plc"}
MOTS_LIAISON = {"de", "du", "des", "la", "le", "les", "et", "en", "au", "aux", "à", "d", "l", "sur", "sous", "pour",
                "of", "the", "and", "for", "by"}
# Mots courts qui ne sont pas des sigles (sinon : 3 lettres ou moins, ou 4 lettres avec au plus une voyelle).
MOTS_COURTS = {"art", "sud", "est", "bio", "eco", "geo", "net", "web", "lab", "app", "top", "one", "new", "big", "pro",
               "air", "sun", "car", "bus", "bar", "box", "fix", "cap", "lux", "max", "mer", "sol", "ciel", "vie", "eau",
               "bon", "tech", "soft", "park", "link", "mind", "best", "fast", "shop", "bank", "work", "land", "form",
               "club", "plus", "lynx", "smart", "data", "info", "group", "pack", "food", "next", "life", "line", "team",
               "hub", "son", "fils", "cie", "jet", "pet", "fit", "lift", "kids", "gold", "sky", "zen", "dev", "way",
               "pay", "job", "ads", "lab", "led", "eat", "go", "my"}
VOYELLES = set("aeiouyàâäéèêëîïôöùûü")


def _mot(mot: str, premier: bool) -> str:
    bas = mot.lower()
    if not mot.isalpha():
        return mot
    if bas in MOTS_LIAISON and not premier:
        return bas
    if bas not in MOTS_COURTS and (len(mot) <= 3 or (len(mot) == 4 and sum(c in VOYELLES for c in bas) <= 1)):
        return mot.upper()  # sigle de 4 lettres ou moins : gardé en majuscules
    return bas[:1].upper() + bas[1:]


def nom_pour_message(brut: str | None) -> str:
    """« UBER EATS FRANCE SAS » devient « Uber Eats France » ; nom vide, trop long, avec chiffre ou avec « & » (que La
    Bonne Boîte modifie dans la lettre, d'où un écart à la Synthèse) : « vous »."""
    texte = " ".join((brut or "").replace("(", " ").replace(")", " ").split())
    mots = texte.split(" ") if texte else []
    while mots and mots[-1].replace(".", "").lower() in FORMES_JURIDIQUES:
        mots.pop()  # forme juridique finale (SAS, SARL, S.A.S.…)
    while mots and mots[0].replace(".", "").lower() in FORMES_JURIDIQUES:
        mots.pop(0)  # et en tête (« SARL DUPONT »)
    texte = " ".join(mots).strip(" ,-")
    if not texte or len(texte) > 50 or re.search(r"\d", texte) or "&" in texte:
        return "vous"
    if texte != texte.upper():
        return texte  # casse déjà travaillée : gardée
    morceaux, premier = [], True
    for part in re.split(r"([\s\-'’&/.])", texte):
        if part and not re.fullmatch(r"[\s\-'’&/.]", part):
            part, premier = _mot(part, premier), False
        morceaux.append(part)
    return "".join(morceaux)


def charger_modeles() -> dict:
    """Versions du message : A (obligatoire), B si modele_message_spontane_B.txt existe."""
    versions = {}
    for version, path in (("A", MODELE_A), ("B", MODELE_B)):
        if not path.is_file():
            continue
        try:
            versions[version] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            raise CampagneError(f"Modèle de message illisible : {path} (texte UTF-8 attendu).", 500) from None
    return versions


def problemes_modele(modele: str | None) -> list[str]:
    if modele is None:
        return [f"Modèle de message absent : créez {MODELE_A.name} à la racine du projet."]
    problemes = []
    autres = sorted(set(re.findall(r"\{[^{}]*\}", modele)) - {"{entreprise}"})
    if autres or modele.replace("{entreprise}", "").count("{") or modele.replace("{entreprise}", "").count("}"):
        problemes.append("Un seul champ est possible dans le modèle : {entreprise}"
                         + (f" (trouvé : {', '.join(autres)})." if autres else "."))
    if "{entreprise}" not in modele:
        problemes.append("Le champ {entreprise} est absent du modèle.")
    if re.search(r"(?i)https?:|www\.", modele):
        problemes.append("Le modèle contient un lien (http, www) : France Travail peut refuser l'envoi.")
    if "&" in modele:
        problemes.append("Le modèle contient « & » : La Bonne Boîte le modifie dans la lettre (écart à la Synthèse). "
                         "Écrivez « et ».")
    if not modele.strip():
        problemes.append("Le modèle de message est vide.")
    return problemes


def preparer_message(modele: str, entreprise: str | None) -> dict:
    """Message prêt à coller : nom nettoyé, emojis, % et # retirés, 1 500 caractères au plus."""
    nom = nom_pour_message(entreprise)
    problemes = problemes_modele(modele)
    texte, retires = nettoyer_message(modele.replace("{entreprise}", nom)) if not problemes else ("", [])
    if not problemes and len(texte) > MESSAGE_MAX:
        problemes.append(f"Message trop long : {len(texte)} caractères, {MESSAGE_MAX} au plus.")
    if not problemes and not texte:
        problemes.append("Le message est vide.")
    return {"texte": texte, "nom": nom, "retires": retires, "problemes": problemes, "longueur": len(texte)}


def version_suivante(db_path, versions: dict) -> str:
    """A et B en alternance (la moins utilisée d'abord) ; A seule si B n'existe pas ou n'est pas utilisable."""
    if "B" not in versions or problemes_modele(versions["B"]) or "A" not in versions:
        return "A"
    with _connect(db_path) as db:
        counts = dict(db.execute("SELECT version, COUNT(*) FROM campagne WHERE version IN ('A', 'B') GROUP BY version"))
    return "B" if counts.get("A", 0) > counts.get("B", 0) else "A"


def apercu(db_path, entreprise: str = "UBER EATS FRANCE SAS") -> dict:
    versions = charger_modeles()
    return {"entreprise": entreprise, "versions": {v: preparer_message(t, entreprise) for v, t in versions.items()},
            "problemes": problemes_modele(versions.get("A")), "chemin_a": str(MODELE_A), "chemin_b": str(MODELE_B),
            "version_b": "B" in versions}


# --- Envois du jour et alertes -----------------------------------------------------------------------------------

def envois_du_jour(db_path, now: datetime | None = None) -> int:
    with _connect(db_path) as db:
        return db.execute("SELECT COUNT(*) FROM campagne WHERE statut = ? AND envoyee_le >= ?",
                          (ENVOYEE, _debut_jour(now))).fetchone()[0]


def alerte_du_jour(db_path, now: datetime | None = None) -> dict | None:
    with _connect(db_path) as db:
        row = db.execute("SELECT alerte, alerte_raison, alerte_le FROM campagne_jours WHERE jour = ? AND alerte IS NOT NULL",
                         (jour(now),)).fetchone()
    if row is None:
        return None
    return {"type": row["alerte"], "libelle": ALERTES.get(row["alerte"], row["alerte"]), "raison": row["alerte_raison"],
            "le": row["alerte_le"], "bloque_essai": row["alerte"] in ALERTES_SITE}


def poser_alerte(db_path, type_: str, raison: str, now: datetime | None = None) -> None:
    """Alerte du jour : la première active est gardée (une alerte levée est remplacée, raison et heure comprises) ;
    aucune reprise (ni campagne ni envoi) avant demain."""
    with _connect(db_path) as db, db:
        db.execute("INSERT INTO campagne_jours (jour, alerte, alerte_raison, alerte_le) VALUES (?, ?, ?, ?) "
                   "ON CONFLICT(jour) DO UPDATE SET "
                   "alerte_raison = CASE WHEN campagne_jours.alerte IS NULL THEN excluded.alerte_raison "
                   "ELSE campagne_jours.alerte_raison END, "
                   "alerte_le = CASE WHEN campagne_jours.alerte IS NULL THEN excluded.alerte_le ELSE campagne_jours.alerte_le END, "
                   "alerte = COALESCE(campagne_jours.alerte, excluded.alerte)",
                   (jour(now), type_, raison[:500], _maintenant()))


# --- Exclusions et cibles ----------------------------------------------------------------------------------------

def departement(code_postal: str) -> str:
    cp = (code_postal or "").strip()
    if not re.fullmatch(r"\d{5}", cp):
        return ""
    if cp.startswith("97"):
        return cp[:3]
    if cp.startswith("20"):
        return "2A" if int(cp) < 20200 else "2B"
    return cp[:2]


def sirets_exclus(db) -> set[str]:
    """Toute entreprise déjà en base : ancien module (opportunités, envois, échecs), spontanées, campagne."""
    found = {r[0] for r in db.execute("SELECT siret FROM opportunities WHERE siret IS NOT NULL")}
    found |= {r[0] for r in db.execute("SELECT external_id FROM opportunities")}
    found |= {r[0] for r in db.execute("SELECT external_id FROM application_events WHERE channel = 'LABONNEBOITE'")}
    found |= {r[0] for r in db.execute("SELECT siret FROM spontanees")}
    found |= {r[0] for r in db.execute("SELECT siret FROM campagne")}
    return {s.strip() for s in found if isinstance(s, str) and re.fullmatch(r"\d{14}", s.strip())}


def _ajouter(db, entreprise: dict, phase: str, source: str = "api") -> bool:
    cursor = db.execute(
        "INSERT OR IGNORE INTO campagne (siret, entreprise, raison_sociale, ville, code_postal, departement, secteur, "
        "naf, rome, metier, phase, potentiel, source, maj_le) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (entreprise["siret"], entreprise["entreprise"], entreprise.get("raison_sociale") or "", entreprise["ville"],
         entreprise["code_postal"],
         departement(entreprise["code_postal"]), entreprise["secteur"], entreprise["naf"], entreprise["rome"],
         entreprise["metier"], phase, entreprise["potentiel"], source, _maintenant()))
    return cursor.rowcount == 1


def etat_inventaire(db_path) -> dict:
    return _lire_cle(db_path, "campagne.inventaire") or {"etat": "jamais", "fait": {}}


# Île-de-France d'abord : recherche autour des villes principales (code commune INSEE, rayon de 30 km).
CENTRES_IDF = (("75056", "Paris"), ("92050", "Nanterre"), ("93008", "Bobigny"), ("94028", "Créteil"),
               ("91228", "Évry-Courcouronnes"), ("78646", "Versailles"), ("95127", "Cergy"), ("77288", "Melun"),
               ("77284", "Meaux"), ("77379", "Provins"), ("78361", "Mantes-la-Jolie"), ("78517", "Rambouillet"),
               ("91223", "Étampes"), ("95355", "Magny-en-Vexin"))


def _zones() -> list[tuple[str, str, str, str]]:
    """(étape, département, code commune, nom) : l'Île-de-France d'abord, puis les villes de chaque département (les
    principales, puis les petites villes éloignées), dans l'ordre des départements. La Bonne Boîte refuse la recherche
    sans ville : chaque recherche se fait autour d'une ville (rayon de 30 km)."""
    zones = [("idf", departement(code), code, nom) for code, nom in CENTRES_IDF]
    for dep in DEPARTEMENTS:
        zones += [("france", dep, code, nom) for code, nom in VILLES.get(dep, ()) + PETITES_VILLES.get(dep, ())]
    return zones


def avancement_inventaire(state: dict, phases: list[str]) -> tuple[int, int]:
    """(recherches faites, recherches restantes) pour ces phases : de nouvelles villes ou la phase B en ajoutent."""
    fait, faites, total = state.get("fait") or {}, 0, 0
    romes_refuses, villes_refusees = set(state.get("romes_refuses") or ()), set(state.get("villes_refusees") or ())
    for _, _, code, _ in _zones():
        for phase in phases:
            for rome, _ in metiers(phase):
                total += 1
                faites += (fait.get(f"{phase}:{rome}:{code}") in ("fini", "refusé") or rome in romes_refuses
                           or code in villes_refusees)
    return faites, total - faites


def recherches_prevues(phases: list[str]) -> int:
    return len(_zones()) * sum(len(metiers(p)) for p in phases)


def inventaire(settings, phases: list[str], *, client=None, pause: float = PAUSE_API, sleep=time.sleep,
               nouveau: bool = False) -> dict:
    """Liste des cibles : Île-de-France d'abord, puis les principales villes de chaque département ; chaque métier,
    toutes les pages, 2 appels par seconde au plus. Déduplication par SIRET ; rien de ce qui est déjà en base n'est
    modifié.

    Un refus des critères (400) est sauté et noté sans couper l'inventaire : code métier refusé (il a échoué là où
    d'autres métiers passent) ou ville refusée (là où ce métier passe ailleurs). Toute autre erreur l'interrompt avec un
    message clair ; relancé, il reprend où il s'était arrêté. « nouveau » : toutes les recherches sont refaites (la
    liste de La Bonne Boîte change avec le temps) ; les entreprises déjà en base ne sont jamais reprises.
    """
    if not _INVENTAIRE.acquire(blocking=False):
        raise CampagneError("Un inventaire est déjà en cours.", 409)
    db_path = settings.database_path
    try:
        state = etat_inventaire(db_path)
        if nouveau:
            precedent = {k: state.get(k) for k in ("debut", "fin", "appels", "ajoutees")}
            state = {"etat": "jamais", "fait": {}, "ajoutees": 0, "precedent": precedent}
        state.update(etat="en_cours", debut=_maintenant(), erreur=None, phases=phases, zone=None,
                     prevues=recherches_prevues(phases))
        for cle, defaut in (("fait", {}), ("appels", 0), ("refus", []), ("romes_ok", []), ("villes_ok", []),
                            ("romes_refuses", []), ("villes_refusees", []), ("ajoutees", 0)):
            state.setdefault(cle, defaut)
        dernier_ecrit = [time.monotonic()]

        def sauver(force: bool = False) -> None:  # état écrit régulièrement (reprise), pas à chaque appel
            if force or time.monotonic() - dernier_ecrit[0] > 3:
                _ecrire_cle(db_path, "campagne.inventaire", state)
                dernier_ecrit[0] = time.monotonic()

        sauver(force=True)
        lbb = None if fake_ft_enabled() else (client or LaBonneBoiteClient(settings))
        with _connect(db_path) as db:
            exclus = sirets_exclus(db)  # entreprises déjà en base : jamais ajoutées ni modifiées
        recus = signales = 0
        for etape in ("idf", "france"):
            for _, dep, code, nom in (z for z in _zones() if z[0] == etape):
                for phase in phases:
                    for rome, libelle in metiers(phase):
                        cle = f"{phase}:{rome}:{code}"
                        if state["fait"].get(cle) in ("fini", "refusé") or rome in state["romes_refuses"] \
                                or code in state["villes_refusees"]:
                            continue
                        state["zone"] = "Île-de-France" if etape == "idf" else f"{nom} ({dep})"
                        page = 1
                        while True:
                            try:
                                items = _rechercher(lbb, rome, code, page, state, pause, sleep)
                            except InvalidSearchError as exc:
                                state["appels"] += 1
                                _noter_refus(state, rome, code, nom, dep, exc)
                                state["fait"][cle] = "refusé"
                                sauver(force=True)
                                break
                            except Exception as exc:  # noqa: BLE001 - message lisible, sans secret
                                raise CampagneError(_message_echec(db_path, etape, nom, dep, exc), 502) from None
                            state["appels"] += 1
                            if rome not in state["romes_ok"]:
                                state["romes_ok"].append(rome)
                            if code not in state["villes_ok"]:
                                state["villes_ok"].append(code)
                            recus += len(items)
                            signales += sum("email" in item for item in items)
                            nouvelles = []
                            for item in items:
                                entreprise = normalize_company(item, rome, libelle)
                                if not entreprise or not entreprise["formulaire"] or entreprise["siret"] in exclus:
                                    continue
                                if etape == "idf" and departement(entreprise["code_postal"]) not in IDF:
                                    continue  # hors Île-de-France : trouvée autour des villes de son département
                                nouvelles.append(entreprise)
                                exclus.add(entreprise["siret"])
                            if nouvelles:
                                with _connect(db_path) as db, db:
                                    for entreprise in nouvelles:
                                        _ajouter(db, entreprise, phase)
                                state["ajoutees"] += len(nouvelles)
                            state["fait"][cle] = page
                            if len(items) < PAGE_API:
                                break
                            page += 1
                        if state["fait"].get(cle) != "refusé":
                            state["fait"][cle] = "fini"
                        sauver()
            if recus and not signales:
                raise CampagneError("La Bonne Boîte n'indique plus quelles entreprises acceptent son formulaire (champ "
                                    "« email » absent) : inventaire arrêté.", 502)
        state.update(etat="termine", fin=_maintenant(), zone=None)
    except Exception as exc:  # noqa: BLE001 - message lisible, sans secret
        state.update(etat="erreur", erreur=str(exc) or type(exc).__name__, fin=_maintenant())
        _ecrire_cle(db_path, "campagne.inventaire", state)
        raise
    finally:
        _INVENTAIRE.release()
    _ecrire_cle(db_path, "campagne.inventaire", state)
    return state


def _rechercher(lbb, rome: str, code: str, page: int, state: dict, pause: float, sleep) -> list[dict]:
    """Un appel à l'API (2 par seconde au plus). Erreur passagère (5xx, délai dépassé, réseau, 429) : deux nouveaux
    essais, après 10 s puis 1 min. Un refus des critères ou un accès refusé n'est jamais retenté."""
    if lbb is None:
        return fake_lbb_search(rome, citycode=code, page=page, page_size=PAGE_API)
    for essai in range(len(ATTENTES_API) + 1):
        if state["appels"]:
            sleep(pause)
        try:
            return lbb.recherche(rome, citycode=code, page=page, page_size=PAGE_API)
        except (InvalidSearchError, FranceTravailConfigurationError):
            raise
        except FranceTravailError as exc:
            if essai == len(ATTENTES_API):
                raise FranceTravailError(f"{exc} (après {essai + 1} essais)") from None
            state["appels"] += 1
            state["reessais"] = state.get("reessais", 0) + 1
            sleep(max(ATTENTES_API[essai], exc.retry_after or 0))
    raise AssertionError("inatteignable")


def _noter_refus(state: dict, rome: str, code: str, nom: str, dep: str, exc: Exception) -> None:
    """Refus des critères : la ville si ce métier passe ailleurs, le métier si d'autres passent à cette ville."""
    if rome in state["romes_ok"]:
        if code not in state["villes_refusees"]:
            state["villes_refusees"].append(code)
            state["refus"].append(f"Ville refusée et sautée : {nom} ({dep}, code {code})")
    elif code in state["villes_ok"]:
        if rome not in state["romes_refuses"]:
            state["romes_refuses"].append(rome)
            state["refus"].append(f"Code métier refusé et sauté : {rome} (refusé autour de {nom}, accepté pour d'autres "
                                  "métiers)")
    else:
        state["refus"].append(f"Recherche refusée et sautée : {rome} autour de {nom} ({dep})")


def _message_echec(db_path, etape: str, nom: str, dep: str, exc: Exception) -> str:
    detail = str(exc) or type(exc).__name__
    if etape == "idf":
        return f"Recherche en Île-de-France impossible (autour de {nom}) : {detail}"
    with _connect(db_path) as db:
        n = db.execute("SELECT COUNT(*) FROM campagne WHERE statut = ?", (A_FAIRE,)).fetchone()[0]
    return (f"Inventaire interrompu autour de {nom} ({dep}) : {detail} Les {n} entreprises à faire déjà trouvées "
            "restent enregistrées ; relancez l'inventaire pour reprendre là où il s'est arrêté.")


def lancer_inventaire(settings, phases: list[str], nouveau: bool = False) -> dict:
    """Inventaire en arrière-plan (plusieurs minutes pour toute la France) ; l'état se lit avec plan()."""
    if _INVENTAIRE.locked():
        raise CampagneError("Un inventaire est déjà en cours.", 409)
    if nouveau and etat_inventaire(settings.database_path).get("etat") != "termine":
        raise CampagneError("Nouvel inventaire possible une fois le précédent terminé : « Lancer l'inventaire » le "
                            "reprend là où il s'est arrêté.", 409)
    if not fake_ft_enabled():
        client = LaBonneBoiteClient(settings)
        client._check_credentials()
        client._access_token()  # accès refusé : erreur immédiate, avant le lancement
    thread = threading.Thread(target=lambda: _silencieux(lambda: inventaire(settings, phases, nouveau=nouveau)),
                              daemon=True)
    thread.start()
    return etat_inventaire(settings.database_path)


def _silencieux(fonction, *args):
    try:
        fonction(*args)
    except Exception:  # noqa: BLE001 - déjà enregistré dans l'état de l'inventaire
        pass


def ajouter_manuelle(db_path, siret: str, entreprise: str, rome: str, dep: str = "") -> dict:
    """Entreprise de votre choix pour l'essai ou le calibrage (sans passer par l'API).

    Le département est obligatoire : le robot retrouve la fiche de l'entreprise dans la recherche du site.
    """
    siret = (siret or "").replace(" ", "")
    if not re.fullmatch(r"\d{14}", siret):
        raise CampagneError("Le SIRET doit compter 14 chiffres.")
    dep = (dep or "").strip().upper()
    if dep not in RANG_DEPARTEMENT:
        raise CampagneError("Indiquez le département de l'entreprise (75, 92, 2A, 971…) : le robot en a besoin pour "
                            "retrouver sa fiche sur La Bonne Boîte.")
    connus = {r: l for phase in PHASES for r, l in metiers(phase)}
    if rome not in connus:
        raise CampagneError("Métier inconnu : choisissez un code ROME des phases A ou B.")
    if not (entreprise or "").strip():
        raise CampagneError("Indiquez le nom de l'entreprise, tel qu'il apparaît sur La Bonne Boîte.")
    with _connect(db_path) as db, db:
        row = db.execute("SELECT statut FROM campagne WHERE siret = ?", (siret,)).fetchone()
        if row is None and siret in sirets_exclus(db):
            raise CampagneError("Cette entreprise est déjà dans votre base (ancien module ou candidatures spontanées).", 409)
        if row is None:
            _ajouter(db, {"siret": siret, "entreprise": entreprise.strip()[:200], "ville": "", "code_postal": "",
                          "secteur": "", "naf": "", "rome": rome, "metier": connus[rome], "potentiel": None},
                     "A" if rome in dict(metiers("A")) else "B", source="manuel")
            db.execute("UPDATE campagne SET departement = ? WHERE siret = ?", (dep, siret))
    return entreprise_par_siret(db_path, siret)


# --- Statuts ----------------------------------------------------------------------------------------------------

def entreprise_par_siret(db_path, siret: str) -> dict:
    with _connect(db_path) as db:
        row = db.execute("SELECT * FROM campagne WHERE siret = ?", (siret,)).fetchone()
    if row is None:
        raise CampagneError("Entreprise introuvable dans la campagne.", 404)
    return _public(row)


def departement_site(dep: str | None) -> str:
    """Numéro de département pour la recherche du site : « 01 » n'y trouve rien, « 1 » trouve les entreprises de l'Ain
    (vérifié le 07/11). Corse (2A, 2B), outre-mer (971…) et départements 10 à 95 : inchangés."""
    dep = (dep or "").strip()
    return dep[1] if re.fullmatch(r"0[1-9]", dep) else dep


def url_recherche(entreprise: dict, page: int = 1) -> str:
    """Recherche du site (comme sur vos captures) : métier et département de l'entreprise, pour ouvrir sa fiche."""
    return (f"{site_lbb()}/recherche?rome={quote(entreprise['rome'])}"
            f"&department_number={quote(departement_site(entreprise['departement']))}"
            f"&bbox=&distance=10&sort_by=romes.hiring_potential&sort_direction=desc&page={page}")


# Après le clic d'envoi : page d'erreur du site pour une entreprise sans adresse e-mail (rien n'est parti).
SANS_EMAIL_APRES_ENVOI = re.compile(r"(?i)does not have an email")


def sans_email_apres_envoi(texte: str) -> bool:
    return bool(SANS_EMAIL_APRES_ENVOI.search(texte or ""))


def url_formulaire(entreprise: dict) -> str:
    return (f"{site_lbb()}/candidature/nouvelle/{entreprise['siret']}?job={quote(entreprise['rome'])}"
            f"&jobLabel={quote(entreprise['metier'] or '')}")


def _public(row) -> dict:
    item = dict(row)
    item["statut_libelle"] = STATUTS.get(item["statut"], item["statut"])
    item["motif_libelle"] = MOTIFS_IGNOREE.get(item["motif"] or "", item["motif"] or "")
    item["nom_message"] = nom_pour_message(item["entreprise"])
    item["url_formulaire"] = url_formulaire(item)
    return item


def prochaines(db_path, phases: list[str], limite: int = 1, exclure: set | None = None) -> list[dict]:
    """Entreprises à faire : phase A puis B, Île-de-France d'abord, potentiel d'embauche décroissant."""
    if not phases:
        return []
    marks = ",".join("?" for _ in phases)
    with _connect(db_path) as db:
        rows = db.execute(f"SELECT * FROM campagne WHERE statut = ? AND phase IN ({marks})", (A_FAIRE, *phases)).fetchall()
    rows = [r for r in rows if not exclure or r["siret"] not in exclure]
    rows.sort(key=lambda r: (r["phase"], RANG_DEPARTEMENT.get(r["departement"], 999), -(r["potentiel"] or 0),
                             r["siret"]))
    return [_public(r) for r in rows[:limite]]


def rendre_a_faire(db_path, sirets: list[str]) -> int:
    """Fiches « introuvables » pendant une panne apparente de la recherche du site (10 de suite) : de nouveau à faire."""
    if not sirets:
        return 0
    marks = ",".join("?" for _ in sirets)
    with _connect(db_path) as db, db:
        return db.execute(f"UPDATE campagne SET statut = ?, motif = NULL, maj_le = ? WHERE statut = ? AND motif = "
                          f"'introuvable' AND siret IN ({marks})", (A_FAIRE, _maintenant(), IGNOREE, *sirets)).rowcount


TRANSITIONS = {EN_COURS: {A_FAIRE, BROUILLON}, TENTEE: {EN_COURS}, ENVOYEE: {TENTEE, INCERTAINE},
               INCERTAINE: {TENTEE, EN_COURS, BROUILLON}, ERREUR: {EN_COURS, INCERTAINE, TENTEE},
               IGNOREE: {EN_COURS, A_FAIRE, TENTEE}, BROUILLON: {EN_COURS}, A_FAIRE: {EN_COURS}}


def marquer(db_path, siret: str, statut: str, **champs) -> dict:
    """Changement de statut contrôlé : seule une entreprise « en cours » sans rien de créé sur le site redevient « à
    faire » (arrêt demandé, CV ambigu) ; une entreprise tentée ne l'est jamais."""
    autorises = {"motif", "version", "candidature_id", "tentee_le", "envoyee_le"}
    if set(champs) - autorises:
        raise ValueError(champs)
    with _connect(db_path) as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT statut FROM campagne WHERE siret = ?", (siret,)).fetchone()
        if row is None:
            raise CampagneError("Entreprise introuvable dans la campagne.", 404)
        if row["statut"] not in TRANSITIONS[statut]:
            raise CampagneError(f"Changement de statut impossible : {STATUTS.get(row['statut'])} → {STATUTS[statut]}.", 409)
        sets = ", ".join(f"{k} = ?" for k in champs)
        db.execute(f"UPDATE campagne SET statut = ?, maj_le = ?{', ' + sets if sets else ''} WHERE siret = ?",
                   (statut, _maintenant(), *champs.values(), siret))
    return entreprise_par_siret(db_path, siret)


def reprise_apres_plantage(db_path) -> list[str]:
    """Après un plantage : « en cours » devient « erreur », « tentée » devient « incertaine ». Jamais reprises."""
    with _connect(db_path) as db, db:
        en_cours = [r[0] for r in db.execute("SELECT siret FROM campagne WHERE statut = ?", (EN_COURS,))]
        tentees = [r[0] for r in db.execute("SELECT siret FROM campagne WHERE statut = ?", (TENTEE,))]
        db.execute("UPDATE campagne SET statut = ?, motif = 'interrompue', maj_le = ? WHERE statut = ?",
                   (ERREUR, _maintenant(), EN_COURS))
        db.execute("UPDATE campagne SET statut = ?, motif = 'interrompue après le clic', maj_le = ? WHERE statut = ?",
                   (INCERTAINE, _maintenant(), TENTEE))
    return en_cours + tentees


MOTIF_MAIN = "envoyée à la main"
SYNTHESE = "/candidature/recapitulatif"


def envoyee_a_la_main(db_path, siret: str, envoyee_le: str | None = None) -> dict:
    """Envoi fait par vous sur le site (hors robot) : compté comme envoyé, jamais retenté."""
    with _connect(db_path) as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT statut FROM campagne WHERE siret = ?", (siret,)).fetchone()
        if row is None:
            raise CampagneError("Entreprise introuvable dans la campagne.", 404)
        if row["statut"] not in (A_FAIRE, BROUILLON, INCERTAINE, ERREUR):
            raise CampagneError(f"Impossible : cette entreprise est « {STATUTS.get(row['statut'])} ».", 409)
        quand = envoyee_le or _maintenant()
        db.execute("UPDATE campagne SET statut = ?, motif = ?, envoyee_le = ?, tentee_le = COALESCE(tentee_le, ?), "
                   "maj_le = ? WHERE siret = ?", (ENVOYEE, MOTIF_MAIN, quand, quand, _maintenant(), siret))
    return entreprise_par_siret(db_path, siret)


def probleme_regle(regle: dict) -> str | None:
    """Une règle qui reconnaîtrait la Synthèse n'est pas une preuve d'envoi : sa « Date d'envoi » s'affiche avant."""
    if regle["url_motif"].rstrip("/").startswith(SYNTHESE) or "date d" in _cle(regle["texte"]).replace("'", " "):
        return ("La Synthèse (et sa « Date d'envoi ») s'affiche avant l'envoi : ce n'est pas une preuve. "
                "Utilisez l'adresse et le texte de la page de confirmation.")
    return None


def est_confirmation(regle: dict | None, chemin: str, texte: str) -> bool:
    """Envoi confirmé seulement par la page de confirmation (adresse et texte de la règle validée), jamais la Synthèse."""
    if not regle or chemin.rstrip("/").startswith(SYNTHESE) or probleme_regle(regle):
        return False
    motif = re.compile("^" + re.escape(regle["url_motif"]).replace(r"\{id\}", r"[^/]+") + "/?$")
    return bool(motif.match(chemin)) and _cle(regle["texte"]) in _cle(texte)


def resoudre_incertaine(db_path, siret: str, statut: str) -> dict:
    """Après contrôle dans « Gérer vos candidatures » : envoyée ou erreur (jamais de nouvel essai)."""
    if statut not in (ENVOYEE, ERREUR):
        raise CampagneError("Une candidature incertaine devient envoyée ou erreur.")
    with _connect(db_path) as db:
        row = db.execute("SELECT statut, tentee_le FROM campagne WHERE siret = ?", (siret,)).fetchone()
    if row is None or row["statut"] != INCERTAINE:
        raise CampagneError("Cette candidature n'est pas incertaine.", 409)
    champs = {"motif": "vérifiée par vous"}
    if statut == ENVOYEE:
        champs["envoyee_le"] = row["tentee_le"] or _maintenant()
    return marquer(db_path, siret, statut, **champs)


def noter_reponse(db_path, siret: str, reponse: str | None) -> dict:
    if reponse is not None and reponse not in REPONSES:
        raise CampagneError("Réponse inconnue : positive, refus ou autre.")
    with _connect(db_path) as db, db:
        cursor = db.execute("UPDATE campagne SET reponse = ?, reponse_le = ? WHERE siret = ? AND statut = ?",
                            (reponse, _maintenant() if reponse else None, siret, ENVOYEE))
        if cursor.rowcount != 1:
            raise CampagneError("Seule une candidature envoyée peut recevoir une réponse.", 409)
    return entreprise_par_siret(db_path, siret)


# --- Compteurs, plan, historique, export ------------------------------------------------------------------------

def compteurs(db_path, now: datetime | None = None) -> dict:
    debut = _debut_jour(now)
    with _connect(db_path) as db:
        def compte(where: str, params=()):
            return db.execute(f"SELECT COUNT(*) FROM campagne WHERE {where}", params).fetchone()[0]

        def bloc(depuis: str | None) -> dict:
            date_ok = " AND maj_le >= ?" if depuis else ""
            p = (depuis,) if depuis else ()
            return {"envoyees": compte(f"statut = '{ENVOYEE}'" + (" AND envoyee_le >= ?" if depuis else ""), p),
                    "incertaines": compte(f"statut = '{INCERTAINE}'" + date_ok, p),
                    "erreurs": compte(f"statut = '{ERREUR}'" + date_ok, p),
                    "ignorees": {m: compte(f"statut = '{IGNOREE}' AND motif = ?" + date_ok, (m, *p)) for m in MOTIFS_IGNOREE}}
        restantes = {p: compte("statut = ? AND phase = ?", (A_FAIRE, p)) for p in PHASES}
        return {"jour": bloc(debut), "total": bloc(None), "restantes": restantes,
                "brouillons": compte("statut = ?", (BROUILLON,))}


def plan(db_path) -> dict:
    """Ce que la campagne ferait : entreprises éligibles par phase, par département (Île-de-France détaillée)."""
    with _connect(db_path) as db:
        rows = db.execute("SELECT phase, departement, COUNT(*) FROM campagne WHERE statut = ? GROUP BY phase, departement",
                          (A_FAIRE,)).fetchall()
        exclues = len(sirets_exclus(db) - {r[0] for r in db.execute("SELECT siret FROM campagne")})
    phases = {}
    for phase in PHASES:
        par_dep = {r[1]: r[2] for r in rows if r[0] == phase}
        phases[phase] = {"eligibles": sum(par_dep.values()),
                         "idf": [{"departement": d, "nombre": par_dep.get(d, 0)} for d in IDF],
                         "reste": sum(n for d, n in par_dep.items() if d not in IDF),
                         "metiers": len(metiers(phase))}
    reglages = lire_reglages(db_path)
    actives = [p for p in PHASES if reglages.get(f"phase_{p.lower()}")]
    etat = etat_inventaire(db_path)
    inv = {k: v for k, v in etat.items() if k != "fait"}  # la liste détaillée (des milliers de recherches) reste en base
    inv["faites"], inv["restantes"] = avancement_inventaire(etat, actives)
    return {"phases": phases, "deja_en_base": exclues, "inventaire": inv, "departements": list(DEPARTEMENTS)}


FILTRES_STATUT = set(STATUTS) | {"tous"}


def historique(db_path, *, statut: str = "tous", phase: str = "", recherche: str = "", reponse: str = "",
               page: int = 1, par_page: int = 50) -> dict:
    """Candidatures de la campagne (hors entreprises à faire), les plus récentes d'abord, avec filtres."""
    if statut not in FILTRES_STATUT:
        raise CampagneError("Filtre de statut inconnu.")
    where, params = ["statut != ?"], [A_FAIRE]
    if statut != "tous":
        where.append("statut = ?")
        params.append(statut)
    if phase in PHASES:
        where.append("phase = ?")
        params.append(phase)
    if reponse in REPONSES:
        where.append("reponse = ?")
        params.append(reponse)
    elif reponse == "aucune":
        where.append("reponse IS NULL")
    if recherche.strip():
        where.append("(entreprise LIKE ? OR ville LIKE ? OR siret LIKE ? OR metier LIKE ?)")
        params += [f"%{recherche.strip()}%"] * 4
    page, par_page = max(1, page), min(max(1, par_page), 200)
    clause = " AND ".join(where)
    with _connect(db_path) as db:
        total = db.execute(f"SELECT COUNT(*) FROM campagne WHERE {clause}", params).fetchone()[0]
        rows = db.execute(f"SELECT * FROM campagne WHERE {clause} ORDER BY COALESCE(envoyee_le, tentee_le, maj_le) DESC, "
                          "siret LIMIT ? OFFSET ?", (*params, par_page, (page - 1) * par_page)).fetchall()
    return {"items": [_public(r) for r in rows], "total": total, "page": page, "par_page": par_page,
            "pages": max(1, -(-total // par_page))}


COLONNES_CSV = ("siret", "entreprise", "ville", "departement", "secteur", "metier", "rome", "phase", "statut", "motif",
                "version", "candidature_id", "tentee_le", "envoyee_le", "reponse", "reponse_le")


def export_csv(db_path, **filtres) -> str:
    """Historique filtré en CSV (séparateur ; pour un tableur français), sans aucune coordonnée."""
    sortie = io.StringIO()
    writer = csv.writer(sortie, delimiter=";")
    writer.writerow(COLONNES_CSV)
    page = 1
    while True:
        lot = historique(db_path, page=page, par_page=200, **filtres)
        for item in lot["items"]:
            ligne = dict(item, statut=item["statut_libelle"], reponse=REPONSES.get(item["reponse"] or "", ""))
            writer.writerow([_cellule(ligne.get(c)) for c in COLONNES_CSV])
        if page >= lot["pages"]:
            break
        page += 1
    return "﻿" + sortie.getvalue()  # BOM : accents corrects à l'ouverture dans Excel


def _cellule(valeur) -> str:
    texte = "" if valeur is None else str(valeur)
    return "'" + texte if texte[:1] in ("=", "+", "-", "@") else texte  # pas de formule dans un tableur


def stats_versions(db_path) -> list[dict]:
    with _connect(db_path) as db:
        rows = db.execute("SELECT version, COUNT(*) AS envoyees, SUM(reponse = 'positive') AS positive, "
                          "SUM(reponse = 'refus') AS refus, SUM(reponse = 'autre') AS autre FROM campagne "
                          "WHERE statut = ? AND version IS NOT NULL GROUP BY version ORDER BY version", (ENVOYEE,)).fetchall()
    return [{"version": r["version"], "envoyees": r["envoyees"], "positive": r["positive"] or 0, "refus": r["refus"] or 0,
             "autre": r["autre"] or 0, "reponses": (r["positive"] or 0) + (r["refus"] or 0) + (r["autre"] or 0)}
            for r in rows]


def _cle(texte: str) -> str:
    decomposed = unicodedata.normalize("NFKD", (texte or "").casefold())
    return " ".join(re.sub(r"[\W_]+", " ", "".join(c for c in decomposed if not unicodedata.combining(c))).split())


def meme_entreprise(connus: list[str], affiche: str) -> bool:
    """Nom de la synthèse comparé aux noms connus (enseigne, raison sociale) : l'un contient l'autre."""
    b = _cle(affiche)
    for attendu in filter(None, connus):
        a = _cle(attendu)
        sans_forme = _cle(nom_pour_message(attendu)), _cle(nom_pour_message(affiche))
        if a and b and (a == b or a in b or b in a or (sans_forme[0] != "vous" and sans_forme[0] == sans_forme[1])):
            return True
    return False
