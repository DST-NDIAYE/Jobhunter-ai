"""Deux adaptateurs explicites ; aucune détection de fournisseur par la clé."""

from app.llm.base import Provider
from app.llm.mistral_provider import MistralProvider
from app.llm.openai_provider import OpenAIProvider
from app.llm.gemini_provider import GeminiProvider
from app.llm.groq_provider import GroqProvider

PROVIDERS: dict[str, Provider] = {
    "mistral": MistralProvider(),
    "openai": OpenAIProvider(),
    "gemini": GeminiProvider(),
    "groq": GroqProvider(),
}


def resolve_provider(settings, mode="auto"):
    """La sélection UI Automatique respecte LLM_PROVIDER, notamment local."""
    configured = (settings.llm_provider or "local").lower()
    model = settings.llm_model.strip()
    if mode == "local" or (mode == "auto" and configured == "local"):
        return None, ""
    provider = configured
    if provider in ("auto", "local"):
        # Le préfixe explicite permet d'utiliser aussi un modèle non reconnu.
        if "/" in model:
            provider, model = model.split("/", 1)
        elif model.startswith(("gpt-", "o1", "o3", "o4")):
            provider = "openai"
        elif model.startswith(("mistral-", "ministral-", "open-mistral-", "magistral-")):
            provider = "mistral"
        elif model.startswith(("gemini-",)):
            provider = "gemini"
        elif model.startswith(("llama-", "mixtral-")):
            provider = "groq"
    elif model.startswith(provider + "/"):
        model = model.split("/", 1)[1]
    if provider not in PROVIDERS or not model or not settings.llm_api_key:
        return None, ""
    return provider, model
