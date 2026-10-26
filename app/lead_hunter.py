"""Chasseur de Décideurs & Approche Directe (Tech Recruiter / Hiring Manager Finder & Outreach).

Génère des requêtes et liens ciblés LinkedIn / Google X-Ray ainsi que des messages d'approche
ultra-personnalisés (Notes de connexion < 300 car. et InMails) pour contourner les processus RH passifs.
"""

from datetime import datetime, timezone
import json
import logging
import re
import urllib.parse
from typing import Any
import httpx

from app.candidate_profile import load_profile
from app.config import get_settings

logger = logging.getLogger("jobhunter.lead_hunter")


def build_recruiter_links(company: str, title: str = "") -> dict[str, str]:
    """Construit des liens de recherche directs LinkedIn et Google X-Ray pour une entreprise."""
    clean_company = (company or "Entreprise").strip()
    encoded_company = urllib.parse.quote(f'"{clean_company}"')

    # 1. Tech Leads / Head of Data / Managers
    lead_query = f'{encoded_company} ("Head of Data" OR "Lead Data Scientist" OR "VP Engineering" OR "Directeur IA" OR "Engineering Manager")'
    lead_url = f"https://www.linkedin.com/search/results/people/?keywords={lead_query}"

    # 2. Tech Recruiters / Talent Acquisition
    recruiter_query = f'{encoded_company} ("Tech Recruiter" OR "Talent Acquisition" OR "Recruteur Tech" OR "Talent Partner")'
    recruiter_url = f"https://www.linkedin.com/search/results/people/?keywords={recruiter_query}"

    # 3. Google X-Ray Search (trouve les profils LinkedIn indexés sans être connecté à LinkedIn)
    xray_terms = urllib.parse.quote(f'site:linkedin.com/in/ "{clean_company}" ("Data Scientist" OR "Head of Data" OR "Tech Recruiter")')
    google_xray_url = f"https://www.google.com/search?q={xray_terms}"

    # 4. Page entreprise LinkedIn
    company_page_url = f"https://www.linkedin.com/search/results/companies/?keywords={urllib.parse.quote(clean_company)}"

    return {
        "tech_lead_url": lead_url,
        "recruiter_url": recruiter_url,
        "google_xray_url": google_xray_url,
        "company_page_url": company_page_url,
        "company": clean_company,
    }


def _fallback_outreach(candidate: dict[str, Any], company: str, title: str) -> dict[str, Any]:
    cand_name = f"{candidate.get('first_name', '')} {candidate.get('last_name', '')}".strip() or "Serigne Bassirou NDIAYE"
    first_name = candidate.get("first_name", "Serigne")
    phone = candidate.get("phone", "07 58 64 47 06")
    email = candidate.get("email", "dstndiaye@gmail.com")
    clean_title = title or "Data Scientist"
    clean_company = company or "votre équipe"

    # Note de connexion Tech Lead (< 300 car.)
    lead_note = (
        f"Bonjour [Prénom], admiratif des projets Data chez {clean_company}, "
        f"je suis {clean_title} (ML, Python, NLP). Votre offre de {clean_title} "
        f"résonne avec mes réalisations. Au plaisir d'échanger sur vos défis tech !"
    )
    if len(lead_note) > 295:
        lead_note = lead_note[:292] + "..."

    # InMail Tech Lead (~120 mots)
    lead_inmail = (
        f"Bonjour [Prénom],\n\n"
        f"Je me permets de vous contacter directement en tant que Lead / Décideur technique chez {clean_company}. "
        f"J'ai découvert votre opportunité de {clean_title} et la stack technique correspond exactement à mes compétences "
        f"en Machine Learning, modélisation prédictive et déploiement de modèles en production (Python, FastAPI, Docker).\n\n"
        f"Lors de mes récents projets, j'ai notamment conçu et optimisé des pipelines de données et des architectures IA "
        f"orientées impact métier et performance opérationnelle.\n\n"
        f"Seriez-vous ouvert à un rapide échange de 10 minutes cette semaine pour discuter de vos enjeux Data actuels ?\n\n"
        f"Bien cordialement,\n"
        f"{cand_name} — {phone} · {email}"
    )

    # Note de connexion Recruteur (< 300 car.)
    recruiter_note = (
        f"Bonjour [Prénom], j'ai postulé à l'offre de {clean_title} chez {clean_company}. "
        f"Data Scientist expérimenté (Python, ML, MLOps), mon profil coche l'ensemble des prérequis. "
        f"Je serais ravi d'échanger avec vous sur cette ouverture !"
    )
    if len(recruiter_note) > 295:
        recruiter_note = recruiter_note[:292] + "..."

    # InMail Recruteur
    recruiter_inmail = (
        f"Bonjour [Prénom],\n\n"
        f"Je vous contacte suite à la publication de votre offre de {clean_title} pour {clean_company}.\n\n"
        f"Data Scientist spécialisé en modélisation Machine Learning, IA générative et pipelines de production, "
        f"mes compétences répondent précisément aux attentes de la fiche de poste. Mon CV complet est disponible "
        f"et je suis immédiatement disponible pour un premier point téléphonique de cadrage.\n\n"
        f"Quand auriez-vous un créneau de disponible pour échanger ?\n\n"
        f"Bien cordialement,\n"
        f"{cand_name} — {phone} · {email}"
    )

    return {
        "lead_connection_note": lead_note,
        "lead_inmail": lead_inmail,
        "recruiter_connection_note": recruiter_note,
        "recruiter_inmail": recruiter_inmail,
        "generated_by": "template",
    }


def generate_lead_outreach(
    company: str,
    title: str = "Data Scientist",
    description: str = "",
) -> dict[str, Any]:
    """Génère la trousse d'approche directe complète pour les décideurs techniques et recruteurs."""
    candidate = load_profile()
    settings = get_settings()
    links = build_recruiter_links(company, title)

    # Tentative d'appel à Gemini si configuré
    if settings.llm_provider.lower() == "gemini" and settings.llm_api_key:
        try:
            cand_name = f"{candidate.get('first_name', '')} {candidate.get('last_name', '')}".strip() or "Serigne Bassirou NDIAYE"
            prompt = (
                f"Tu es un expert mondial en recrutement Tech et networking LinkedIn.\n"
                f"Candidat : {cand_name}, Data Scientist / Machine Learning Engineer (Python, SQL, PyTorch, Scikit-learn, LLM, Docker).\n"
                f"Entreprise cible : {company}\n"
                f"Poste ciblé : {title}\n"
                f"Extrait de l'offre : {description[:800]}\n\n"
                f"Génère au format JSON strict avec exactement ces clés :\n"
                f"1. 'lead_connection_note' : Note de connexion LinkedIn pour le Tech Lead / Head of Data (MAXIMUM 280 caractères, chaleureux, accrocheur, axé sur la stack technique et la valeur ajoutée).\n"
                f"2. 'lead_inmail' : Message InMail ou email direct pour le Tech Lead (~120 mots, 3 paragraphes : accroche, réalisations/stack, proposition d'échange 10 min).\n"
                f"3. 'recruiter_connection_note' : Note de connexion LinkedIn pour le Recruteur Tech (MAXIMUM 280 caractères, direct, mentionne l'offre et l'adéquation CV).\n"
                f"4. 'recruiter_inmail' : Message InMail pour le Recruteur Tech (~100 mots, disponibilité immédiate, compétences clés, proposition d'appel)."
            )
            model_name = (settings.llm_model or "gemini-1.5-flash").replace("gemini/", "")
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={settings.llm_api_key}"
            payload = {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.4, "responseMimeType": "application/json"}
            }
            with httpx.Client(timeout=15.0) as client:
                resp = client.post(url, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    candidates = data.get("candidates", [])
                    if candidates:
                        parts = candidates[0].get("content", {}).get("parts", [])
                        if parts:
                            raw_text = parts[0].get("text", "").strip()
                            clean_json = raw_text
                            if clean_json.startswith("```json"):
                                clean_json = clean_json[7:]
                            if clean_json.startswith("```"):
                                clean_json = clean_json[3:]
                            if clean_json.endswith("```"):
                                clean_json = clean_json[:-3]
                            parsed = json.loads(clean_json.strip())
                            lead_note = parsed.get("lead_connection_note", "")
                            if len(lead_note) > 295:
                                lead_note = lead_note[:292] + "..."
                            rec_note = parsed.get("recruiter_connection_note", "")
                            if len(rec_note) > 295:
                                rec_note = rec_note[:292] + "..."

                            return {
                                **links,
                                "lead_connection_note": lead_note,
                                "lead_inmail": parsed.get("lead_inmail", ""),
                                "recruiter_connection_note": rec_note,
                                "recruiter_inmail": parsed.get("recruiter_inmail", ""),
                                "generated_by": f"Gemini ({model_name})",
                            }
        except Exception as exc:
            logger.warning(f"Gemini lead outreach échoué, repli template: {exc}")

    # Fallback template
    fallback = _fallback_outreach(candidate, company, title)
    return {
        **links,
        **fallback,
    }
