"""Mode test : faux modèle de langage et offres France Travail fictives, sans aucun appel réseau.

Activé uniquement par variables d'environnement, pour tester l'interface sans consommer de quota :
- JOBHUNTER_FAKE_LLM=1 : le modèle configuré est remplacé par FakeLLM ;
- JOBHUNTER_FAKE_FT=1 : la recherche France Travail renvoie FAKE_OFFERS.
Le faux modèle ne rédige qu'à partir du catalogue reçu : il n'invente aucun fait sur le candidat.
"""

import json
import os
import re

from app.cv_tailor import years_required_in_text

FAKE_MODEL = "faux-modele-test"
VOCABULAIRE = ("Python", "SQL", "Docker", "Kubernetes", "LangChain", "FAISS", "Scikit-learn", "Pandas",
               "Machine Learning", "MLOps", "IA générative", "LLM", "Communication", "Git", "Spark", "CI/CD")


def fake_llm_enabled() -> bool:
    return os.getenv("JOBHUNTER_FAKE_LLM") == "1"


def fake_ft_enabled() -> bool:
    return os.getenv("JOBHUNTER_FAKE_FT") == "1"


def _ph(texte, *preuves):
    return {"texte": texte, "preuves": list(preuves)}


def _words(text):
    return len(re.findall(r"\w+(?:[’'-]\w+)*", text))


class FakeLLM:
    """Réponses déterministes et valides pour chaque schéma utilisé par l'application."""

    def generate(self, *, messages, model, api_key, timeout, schema):
        payload = json.loads(messages[-1]["content"])
        builder = {"OffreAnalyse": self._analyse, "Selection": self._selection, "Lettre": self._lettre}[schema.__name__]
        return schema.model_validate(builder(payload))

    @staticmethod
    def _analyse(payload):
        text = payload["texte_offre"]
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        company = re.search(r"(?im)^\s*entreprise\s*:\s*(.+)$", text)
        contract = next((c for c in ("CDI", "CDD") if re.search(rf"\b{c}\b", text)), "inconnu")
        if re.search(r"(?i)\bcifre\b", text):
            contract = "these_cifre"
        found = [term for term in VOCABULAIRE if re.search(rf"(?i)(?<!\w){re.escape(term)}(?!\w)", text)]
        return {"intitule": lines[0][:120] if lines else "", "entreprise": company.group(1).strip() if company else "",
                "type_contrat": contract, "competences_requises": found, "competences_souhaitees": [],
                "mots_cles": [], "experience_min_annees": years_required_in_text(text),
                "niveau_experience": "non_precise", "langue": "fr"}

    @staticmethod
    def _selection(payload):
        catalogue = payload["catalogue"]
        return {"accroche": next(iter(catalogue["accroches"])),
                "projets": [{"projet": p["id"], "accomplissements": [a["id"] for a in p["accomplissements"]]}
                            for p in catalogue["projets"]]}

    @staticmethod
    def _lettre(payload):
        offre, catalogue = payload["offre"], payload["catalogue"]
        company = offre.get("entreprise") or "votre entreprise"
        title = offre.get("intitule") or "ce poste"
        projects = [p for e in catalogue["experiences"] for p in e["projets"]]
        missions = []
        for project in projects[:2]:
            items = [a for a in project["accomplissements"] if "certification" not in a["texte"].lower()][:2]
            phrases = [_ph(f"J'ai mené ce travail : {a['texte'][0].lower()}{a['texte'][1:]}", a["id"]) for a in items]
            missions.append({"phrases": phrases})
        certifications = payload.get("certifications_a_mentionner") or []
        cert_info = (catalogue.get("certifications") or [{}])[0]
        if certifications and cert_info.get("organisme") and missions:
            cert_item = next((a["id"] for p in projects for a in p["accomplissements"]
                              if "certification" in a["texte"].lower()), catalogue["experiences"][0]["id"])
            missions[-1]["phrases"].append(_ph(
                f"J'ai obtenu {cert_info.get('nombre') or ''} certifications {cert_info['organisme']} "
                f"en {', '.join(certifications)}.".replace("  ", " "), cert_item))
        accroche = [_ph(f"{company} recrute pour le poste de {title}, qui correspond au travail que je souhaite mener.")]
        motivation = [_ph(f"Le poste de {title} chez {company} rejoint les sujets sur lesquels je souhaite travailler dans la durée.")]
        if payload.get("competences_absentes"):
            motivation.append(_ph("J'aimerais approfondir les autres sujets de l'offre aux côtés de vos équipes."))
        filler = ("Je travaille avec rigueur, en documentant chaque choix pour que l'équipe puisse reprendre le sujet "
                  "sans difficulté et en expliquant clairement les résultats aux interlocuteurs non techniques.")
        lettre = {"accroche": {"phrases": accroche}, "missions": missions, "motivation": {"phrases": motivation}}

        def total():
            texts = [ph["texte"] for part in (lettre["accroche"], *lettre["missions"], lettre["motivation"])
                     for ph in part["phrases"]]
            return _words(" ".join(texts)) + 11  # + formule de politesse ajoutée par le code

        neutral = ["Cette approche me paraît essentielle pour que les modèles servent réellement les décisions.",
                   "Elle demande aussi d'écouter les utilisateurs et de vérifier chaque résultat avant diffusion.",
                   "Un modèle utile est un modèle compris, suivi dans le temps et adapté aux contraintes du terrain.",
                   "Je prête une attention particulière à la qualité des données, souvent décisive pour la suite.",
                   "Travailler en lien étroit avec les équipes qui utilisent les résultats me tient à cœur.",
                   "J'aime partir d'un besoin concret, le reformuler clairement puis proposer une solution simple.",
                   "Chaque étape mérite d'être expliquée, testée et partagée avant d'être considérée comme terminée.",
                   "Une solution sobre et bien documentée vaut souvent mieux qu'une solution complexe et fragile.",
                   "Les retours des utilisateurs permettent d'ajuster le travail au fil des semaines.",
                   "Je privilégie des échanges réguliers pour valider les choix au bon moment.",
                   "Je serais heureux d'en échanger avec vous lors d'un entretien.",
                   "Je reste disponible pour présenter plus en détail le travail décrit dans mon CV."]
        motivation.append(_ph(filler))
        while total() < 260 and neutral:
            motivation.append(_ph(neutral.pop(0)))
        return lettre


FAKE_OFFERS = [
    {"external_id": "DEMO-001", "title": "Data Scientist (H/F)", "company": "Exemplia Analytics",
     "location": "75 - Paris", "contract_type": "CDI", "publication_date": "2026-10-20",
     "description": "Nous recherchons un Data Scientist pour industrialiser nos modèles "
                    "de classification. Python, Scikit-learn, SQL, Docker. Une première expérience en MLOps est un plus."},
    {"external_id": "DEMO-002", "title": "Ingénieur IA générative (H/F)", "company": "Nuage Fictif",
     "location": "69 - Lyon", "contract_type": "CDI", "publication_date": "2026-10-18",
     "description": "Conception d'assistants RAG avec LangChain et FAISS, évaluation des "
                    "réponses de LLM, déploiement sur Kubernetes."},
    {"external_id": "DEMO-003", "title": "Data Analyst (H/F)", "company": "Démo Retail",
     "location": "33 - Bordeaux", "contract_type": "CDD", "publication_date": "2026-10-15",
     "description": "Tableaux de bord, SQL, Pandas et communication avec les équipes métier."},
    {"external_id": "DEMO-004", "title": "Lead Data Scientist (H/F)", "company": "Exemplia Analytics",
     "location": "75 - Paris", "contract_type": "CDI", "publication_date": "2026-10-12",
     "description": "Encadrement d'une équipe, 8 ans d'expérience exigés, Spark."},
    {"external_id": "DEMO-005", "title": "Boulanger (H/F)", "company": "Fournil Imaginaire",
     "location": "13 - Marseille", "contract_type": "CDI", "publication_date": "2026-10-10",
     "description": "Fabrication du pain et des viennoiseries."},
]


def fake_search_jobs(keyword=None, location=None, contract_type=None, limit=20):
    offers = [{**o, "source": "france_travail", "url": f"https://example.org/offres/{o['external_id']}",
               "skills": [], "salary": None, "experience": None} for o in FAKE_OFFERS]
    if contract_type:
        offers = [o for o in offers if o["contract_type"] == contract_type]
    return offers[:limit]
