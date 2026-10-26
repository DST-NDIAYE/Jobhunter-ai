"""Préparation locale et LLM, avec repli local, signature depuis le profil candidat et formatage La Bonne Boîte."""

from datetime import datetime, timezone
import hashlib
import re
import unicodedata

from app.candidate_profile import load_profile
from app.matching import calculate_match_score, extract_cv_text
from app.llm import PROVIDERS, resolve_provider
from app.llm.prompts import build_messages, validate_grounding


SKILLS = {
    "Python": ("python",), "SQL": ("sql",), "R": ("r",),
    "Machine Learning": ("machine learning", "apprentissage automatique"),
    "Deep Learning": ("deep learning", "apprentissage profond"),
    "NLP": ("nlp", "traitement du langage naturel"),
    "IA générative": ("ia generative", "generative ai", "intelligence artificielle generative"),
    "LLM": ("llm", "llms", "large language models"), "RAG": ("rag",),
    "Pandas": ("pandas",), "NumPy": ("numpy",),
    "Scikit-learn": ("scikit-learn", "sklearn", "scikit learn"),
    "PyTorch": ("pytorch",), "TensorFlow": ("tensorflow",),
    "Spark": ("spark", "pyspark"), "Airflow": ("airflow",),
    "Docker": ("docker",), "Kubernetes": ("kubernetes", "k8s"),
    "Git": ("git",), "Linux": ("linux",),
    "AWS": ("aws", "amazon web services"), "Azure": ("azure",),
    "GCP": ("gcp", "google cloud"), "Power BI": ("power bi", "powerbi"),
    "Tableau": ("tableau software", "tableau desktop"),
    "Excel": ("excel",), "ETL": ("etl",), "dbt": ("dbt",),
    "FastAPI": ("fastapi",), "Streamlit": ("streamlit",),
    "LangChain": ("langchain",), "Hugging Face": ("hugging face", "huggingface"),
    "MLOps": ("mlops",), "Statistiques": ("statistiques", "statistics"),
    "Java": ("java",), "Scala": ("scala",), "C++": ("c++",),
}

# Caractères autorisés par le validateur Angular de labonneboite.francetravail.fr (max 1500 caractères)
LBB_ALLOWED_RE = re.compile(
    r"[^a-zA-Z\xc0-\xff\u0178\u0153\u0152\xe6\xc60-9\n .,;:!?'\"()\-\u2019\xab\xbb`\\/@&\u20ac+*|<>^]"
)


def sanitize_lbb_message(text: str, max_len: int = 1480) -> str:
    """Nettoie un texte selon les règles exactes du champ #message-textarea de La Bonne Boîte."""
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("–", "-").replace("—", "-").replace("…", "...")
    # Supprimer les protocoles http:// et https:// qui déclenchent le filtre WAF anti-URL de France Travail
    text = re.sub(r"https?://(?:www\.)?", "", text)
    text = LBB_ALLOWED_RE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > max_len:
        text = text[:max_len].rsplit(" ", 1)[0].strip()
    return text


def normalized(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.casefold())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text)


def mentions(text: str, term: str) -> bool:
    return bool(re.search(r"(?<![\w])" + re.escape(normalized(term)) + r"(?![\w])", normalized(text)))


def analyze(cv_text: str, job: dict) -> dict:
    labels = [skill.get("libelle", "").strip() for skill in job.get("skills") or []
              if isinstance(skill, dict) and isinstance(skill.get("libelle"), str)]
    offer_text = "\n".join([job.get("title") or "", job.get("description") or "", *labels])
    catalog = {**SKILLS, **{label: (label,) for label in labels if label and label not in SKILLS}}
    detected = [name for name, aliases in catalog.items() if any(mentions(offer_text, alias) for alias in aliases)]
    cv_skills = [name for name, aliases in SKILLS.items() if any(mentions(cv_text, alias) for alias in aliases)]
    if job.get("source") == "labonneboite" and not detected:
        detected = cv_skills[:8]
    common = [name for name in detected if any(mentions(cv_text, alias) for alias in catalog.get(name, (name,)))]
    if job.get("source") == "labonneboite" and not common:
        common = cv_skills[:8]
    evidence = []
    for line in cv_text.splitlines():
        line = line.strip()
        if not line or "@" in line or "http" in line.lower():
            continue
        if any(mentions(line, alias) for name in common for alias in catalog.get(name, (name,))):
            if line not in evidence:
                evidence.append(line)
        if len(evidence) == 5:
            break
    return {
        "offer_skills": detected,
        "common_skills": common,
        "missing_skills": [name for name in detected if name not in common],
        "cv_evidence": evidence,
        "caveat": "Détection lexicale non exhaustive. Une mention ne prouve ni maîtrise ni durée d'expérience ; une absence ne prouve pas une lacune.",
    }


def _candidate_signature(cv_text: str) -> tuple[str, str]:
    """Retourne (nom_complet, bloc_signature_complet) à partir du profil candidat ou du CV."""
    prof = load_profile()
    full_name = " ".join(filter(None, [prof.get("first_name", "").strip(), prof.get("last_name", "").strip()]))
    if not full_name:
        first = next((line.strip() for line in cv_text.splitlines() if line.strip()), "")
        if re.fullmatch(r"[^\W\d_]+(?:[ '\-][^\W\d_]+){1,4}", first) and not any(
            mentions(first, word) for word in ("scientist", "engineer", "ingenieur", "analyst", "curriculum", "vitae", "developpeur")
        ):
            full_name = first
        else:
            full_name = "[Votre nom]"
    contact_parts = [p for p in (prof.get("phone", "").strip(), prof.get("email", "").strip()) if p]
    links_parts = [p for p in (prof.get("linkedin_url", "").strip(), prof.get("portfolio_url", "").strip()) if p]
    lines = [full_name]
    if contact_parts:
        lines.append(" | ".join(contact_parts))
    if links_parts:
        lines.append(" | ".join(links_parts))
    return full_name, "\n".join(lines)


def detect_tone(job: dict) -> str:
    """Détecte automatiquement le ton le plus naturel et adapté à l'offre d'emploi."""
    text = f"{job.get('title', '')} {job.get('company', '')} {job.get('description', '')}".lower()
    classic_keywords = ("banque", "assurance", "mutuelle", "conformité", "gouvernance", "cadrage", "ministère", "audit", "direction", "groupe", "secteur public")
    impact_keywords = ("roi", "chiffré", "business", "rentabilité", "conversion", "kpi", "décisionnel", "optimisation", "stratégie", "métier", "revenus")
    classic_score = sum(1 for kw in classic_keywords if kw in text)
    impact_score = sum(1 for kw in impact_keywords if kw in text)
    if classic_score > impact_score and classic_score >= 2:
        return "classic"
    if impact_score > classic_score and impact_score >= 2:
        return "impact"
    return "modern"


def template_texts(*, job, cv_text, analysis, settings, variant=0, tone="auto"):
    if tone == "auto" or not tone:
        tone = detect_tone(job)
    title = re.sub(r"\s+", " ", job.get("title") or job.get("job_keyword") or "Data Scientist / Ingénieur IA").strip()
    company = re.sub(r"\s+", " ", job.get("company") or job.get("company_name") or "").strip()
    sector = re.sub(r"\s+", " ", job.get("sector") or "").strip()
    target = f" au sein de {company}" if company else ""
    name, full_signature = _candidate_signature(cv_text)
    common = ", ".join(analysis["common_skills"][:6])
    offered = ", ".join(analysis["offer_skills"][:6])
    skills_lead = f"notamment en {common}" if common else "en Machine Learning, NLP/LLM et ingénierie des données"

    if job.get("source") == "labonneboite":
        sector_clause = f", acteur reconnu du secteur ({sector.lower()})" if sector else ""
        if tone == "modern":
            lbb_letter = (
                f"Bonjour,\n\n"
                f"Je vous contacte afin de vous proposer ma candidature spontanée pour un rôle de {title}{target}{sector_clause}.\n\n"
                f"Fort d'une pratique active des stacks Data et IA modernes ({skills_lead}), je conçois et déploie des solutions concrètes : pipelines de données optimisés, modélisation prédictive, agents & architectures RAG / LLM, et intégration continue via des APIs sécurisées.\n\n"
                f"Je serais ravi d'échanger avec vos équipes techniques pour découvrir vos défis actuels et explorer comment mon profil peut accélérer vos livrables.\n\n"
                f"Vous trouverez mon CV ci-joint. Au plaisir d'un prochain échange !\n\n"
                f"Bien cordialement,\n\n{full_signature}"
            )
        elif tone == "impact":
            lbb_letter = (
                f"Madame, Monsieur,\n\n"
                f"Je vous adresse ma candidature spontanée en tant que {title}{target}{sector_clause}, avec pour objectif d'apporter un impact direct et mesurable à vos projets Data et IA.\n\n"
                f"Mon parcours met l'accent sur le retour sur investissement des solutions analytiques : structuration rigoureuse des données, optimisation de modèles prédictifs ({skills_lead}) et déploiement d'outils d'aide à la décision réduisant les cycles de traitement et fiabilisant les prises de décision.\n\n"
                f"Disponible et motivé pour relever des challenges à fort enjeu métier, je reste à votre disposition pour vous présenter mes réalisations chiffrées lors d'un entretien.\n\n"
                f"Sincères salutations,\n\n{full_signature}"
            )
        else:  # classic
            skills_text = (
                f"Mon parcours combine des compétences solides en {common} (modélisation prédictive, Machine Learning, NLP/LLM, industrialisation de pipelines de données et déploiement d'applications)."
                if common
                else "Mon parcours en Data Science et Intelligence Artificielle couvre l'analyse de données, le Machine Learning, le NLP et le déploiement de modèles."
            )
            lbb_letter = (
                f"Madame, Monsieur,\n\n"
                f"Je vous adresse ma candidature spontanée pour un poste lié aux métiers de {title}{target}{sector_clause}.\n\n"
                f"{skills_text} "
                f"Au cours de mes expériences et projets, j'ai conçu et déployé des solutions concrètes de valorisation de la donnée : préparation et structuration de jeux de données complexes, entraînement et évaluation de modèles de Machine Learning et d'IA générative (NLP, RAG), ainsi que création d'outils d'aide à la décision.\n\n"
                f"Rigoureux, autonome et orienté résultats, je souhaite mettre mes compétences techniques et ma capacité d'adaptation au service de vos équipes pour contribuer directement à vos projets Data et IA.\n\n"
                f"Vous trouverez mon CV ci-joint détaillant mes réalisations. Je me tiens à votre entière disposition pour un entretien afin d'échanger sur vos besoins actuels ou futurs.\n\n"
                f"Dans l'attente de votre retour, je vous prie d'agréer, Madame, Monsieur, l'expression de mes salutations distinguées.\n\n"
                f"{full_signature}"
            )
        lbb_letter = sanitize_lbb_message(lbb_letter, max_len=1480)
        return lbb_letter, lbb_letter

    # Offres standards (France Travail, WTTJ, etc.)
    if tone == "modern":
        bridge = (f"Mes réalisations s'articulent directement autour de vos prérequis ({common})."
                  if common else "Mon profil technique et mes projets récents correspondent étroitement aux besoins de votre offre.")
        opening = (f"Votre recherche de {title}{target} a retenu toute mon attention." if variant % 2 == 0
                   else f"Je souhaite vous proposer ma candidature pour le poste de {title}{target}.")
        message = (
            f"Bonjour,\n\n"
            f"{opening} {bridge}\n\n"
            f"Adepte de solutions robustes et modernes en Data Science & IA (Machine Learning, LLM/RAG, Python, FastAPI), "
            f"je privilégie l'agilité, la qualité de code et l'impact opérationnel rapide.\n\n"
            f"Vous trouverez mon CV ci-joint. Je me tiens à votre disposition pour un échange technique et convivial.\n\n"
            f"Bien cordialement,\n{full_signature}"
        )
        letter = (
            f"Madame, Monsieur,\n\n"
            f"C'est avec grand intérêt que je vous propose ma candidature pour le poste de {title}{target}.\n\n"
            f"Passionné par l'écosystème de l'Intelligence Artificielle et de la Data Science, j'ai développé une solide expertise "
            f"dans la conception et l'industrialisation de pipelines analytiques et de modèles prédictifs. "
            f"{bridge} Mes projets récents m'ont permis de mettre en œuvre des architectures modernes (IA générative, RAG, PyTorch, APIs REST) "
            f"visant à automatiser des traitements complexes et extraire une valeur actionnable à partir de données hétérogènes.\n\n"
            f"L'alignement avec les technologies mentionnées dans votre offre ({offered or 'Data / ML'}) "
            f"me motive particulièrement à rejoindre votre équipe pour apporter des solutions concrètes et rapidement exploitables.\n\n"
            f"Je serais ravi de vous présenter mon parcours et de détailler mes réalisations techniques lors d'un entretien.\n\n"
            f"Bien cordialement,\n{full_signature}"
        )
    elif tone == "impact":
        bridge = (f"Mes compétences en {common} sont directement mobilisables pour vos priorités."
                  if common else "Mon profil est résolument axé sur la résolution de problèmes et la performance.")
        opening = (f"Je vous adresse ma candidature pour le poste de {title}{target}." if variant % 2 == 0
                   else f"Votre opportunité de {title}{target} correspond précisément à mes ambitions professionnelles.")
        message = (
            f"Madame, Monsieur,\n\n"
            f"{opening} {bridge}\n\n"
            f"Orienté résultats et retour sur investissement, je conçois des solutions Data & IA qui optimisent les processus, "
            f"accélèrent la prise de décision et apportent une valeur mesurable aux équipes métier.\n\n"
            f"Mon CV ci-joint retrace mes réalisations. Je reste disponible pour un échange approfondi.\n\n"
            f"Sincères salutations,\n{full_signature}"
        )
        letter = (
            f"Madame, Monsieur,\n\n"
            f"Dans le cadre de votre recrutement pour le poste de {title}{target}, je souhaite vous soumettre ma candidature "
            f"avec la volonté d'apporter une contribution mesurable et pérenne à vos objectifs opérationnels.\n\n"
            f"Au cours de mes expériences en Data Science et Machine Learning, j'ai concentré mon travail sur l'efficacité des modèles : "
            f"optimisation des métriques de performance, réduction des temps d'inférence, automatisation de flux de données "
            f"et clarté des indicateurs pour les décideurs. {bridge}\n\n"
            f"Votre opportunité retient particulièrement mon attention car elle fait écho à des enjeux d'envergure "
            f"({offered or 'Data & IA'}) où la rigueur analytique et l'orientation résultats font la différence.\n\n"
            f"Je me tiens à votre entière disposition pour échanger sur la manière dont mes compétences peuvent accélérer vos projets.\n\n"
            f"Avec mes respectueuses salutations,\n{full_signature}"
        )
    else:  # classic
        bridge = (f"Mon CV mentionne notamment {common}, également présents dans votre offre."
                  if common else "Vous trouverez dans mon CV les éléments de mon parcours à examiner au regard de vos besoins.")
        opening = (f"Je vous contacte concernant votre offre de {title}{target}." if variant % 2 == 0
                   else f"Je souhaite vous proposer ma candidature pour le poste de {title}{target}.")
        message = (
            f"Bonjour,\n\n{opening}\n{bridge}\n"
            f"Vous trouverez ci-joint mon CV détaillant mes expériences et réalisations.\n"
            f"Je reste disponible pour échanger sur le poste et vous présenter mon parcours lors d'un entretien.\n\n"
            f"Cordialement,\n{full_signature}"
        )
        focus = (f"L'annonce fait notamment référence à {offered}. Ces éléments me donnent un premier aperçu des sujets à aborder avec vous."
                 if offered else "La description du poste constitue le point de départ de ma candidature et des questions que je souhaite aborder avec vous.")
        letter = (
            f"Madame, Monsieur,\n\n{opening} Je souhaite pouvoir discuter avec vous du contenu du poste, de ses priorités "
            "et de la manière dont mon parcours pourrait correspondre à vos attentes. Cette lettre accompagne mon CV, "
            "auquel vous pouvez vous reporter pour le détail de mon parcours.\n\n"
            f"{bridge} Je préfère partir de ces éléments concrets pour vous présenter ma candidature. "
            "Lors d'un échange, je pourrai revenir sur les éléments de mon CV qui vous intéressent, préciser leur contexte "
            "et répondre à vos questions. Nous pourrons ainsi voir quels éléments sont les plus pertinents pour "
            "vos besoins actuels, et lesquels méritent davantage de précisions pour apprécier ma candidature.\n\n"
            f"{focus} J'aimerais notamment comprendre quelles seraient les premières missions, les résultats attendus "
            "et les contraintes à prendre en compte. Le périmètre du rôle, les outils utilisés et les échanges avec "
            "les autres membres de l'équipe sont également des points sur lesquels je souhaiterais en savoir plus.\n\n"
            "Je vous propose donc un échange pour examiner ensemble les correspondances entre mon parcours et le poste. "
            "Il serait aussi l'occasion de clarifier vos attentes et de mieux comprendre le quotidien de l'équipe. "
            "Je souhaite que cette discussion parte des informations présentées dans mon CV et de situations concrètes, "
            "afin que vous puissiez apprécier ma candidature sur une base précise.\n\n"
            "Je vous remercie pour le temps consacré à l'examen de mon dossier et reste disponible pour un entretien.\n\n"
            f"Cordialement,\n{full_signature}"
        )
    return message, letter


def generate_preparation(job: dict, settings, variant: int = 0, *, mode="auto", instructions="", tone="auto") -> dict:
    if tone == "auto" or not tone:
        tone = detect_tone(job)
    cv_text = extract_cv_text(settings.cv_path)
    analysis = analyze(cv_text, job)
    score = None if job.get("source") == "labonneboite" else calculate_match_score(cv_text, job.get("description"))
    provider, model = resolve_provider(settings, mode)
    message, letter = template_texts(job=job, cv_text=cv_text, analysis=analysis, settings=settings, variant=variant, tone=tone)
    analysis.update({
        "fit_summary": "Candidature spontanée ciblée via La Bonne Boîte." if job.get("source") == "labonneboite" else "Correspondances lexicales à examiner au regard des missions de l'offre.",
        "matching_skills": analysis["common_skills"], "relevant_cv_points": analysis["cv_evidence"],
    })
    method, used_provider, used_model = "templates", "local", ""
    notice = "Généré localement"
    if provider:
        try:
            tone_directives = {
                "modern": "Ton : Moderne, dynamique et direct, axé sur les stacks technologiques récentes (Machine Learning, IA générative, RAG, MLOps, APIs) et l'impact opérationnel rapide. ",
                "impact": "Ton : Orienté résultats concrets, retour sur investissement (ROI), résolution de problèmes métiers et optimisation de la performance des modèles. ",
                "classic": "Ton : Classique, formel, courtois et rigoureux selon les usages d'entreprise et grands comptes. ",
            }
            combined_instructions = f"{tone_directives.get(tone, '')}{instructions}".strip()
            messages = build_messages(job, cv_text, analysis, score, combined_instructions)
            result = PROVIDERS[provider].generate(messages=messages, model=model,
                                                   api_key=settings.llm_api_key, timeout=settings.http_timeout)
            validate_grounding(result, cv_text, analysis)
            if settings.llm_api_key in result.model_dump_json():
                raise ValueError("Réponse non publiable.")
            message, letter = result.short_message, result.cover_letter
            if job.get("source") == "labonneboite":
                letter = sanitize_lbb_message(letter, max_len=1480)
                message = letter
            analysis.update({"fit_summary": result.fit_summary, "matching_skills": result.matching_skills,
                             "relevant_cv_points": result.relevant_cv_points,
                             "cv_evidence": result.relevant_cv_points, "missing_skills": result.missing_skills})
            method, used_provider, used_model = "llm", provider, model
            provider_labels = {"openai": "OpenAI", "mistral": "Mistral", "gemini": "Google Gemini", "groq": "Groq"}
            notice = f"Généré avec : {provider_labels.get(provider, provider.capitalize())} / {model}"
        except Exception:
            notice = "Le service IA n'est pas disponible. Une version locale a été générée."
    elif mode == "ai" or (mode != "local" and settings.llm_provider.lower() not in ("", "local")):
        notice = "Configuration IA incomplète ou non reconnue. Une version locale a été générée."
    return {
        "message": message,
        "letter": letter,
        "analysis": analysis,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "method": method,
        "provider": used_provider,
        "model": used_model,
        "notice": notice,
        "context": {
            "title": job.get("title") or job.get("job_keyword"),
            "company": job.get("company") or job.get("company_name"),
            "description": job.get("description"),
            "cv_name": settings.cv_path.name,
            "cv_text_sha256": hashlib.sha256(cv_text.encode()).hexdigest(),
            "match_score": score,
            "tone": tone,
        },
    }
