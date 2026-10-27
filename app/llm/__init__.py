"""Deux adaptateurs explicites ; aucune détection de fournisseur par la clé."""

import time

from app.llm.base import Provider, ProviderError, QuotaProviderError, RetryableProviderError
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
    """La sélection UI Automatique respecte LLM_PROVIDER, notamment local.

    Mode test (JOBHUNTER_FAKE_LLM=1) : faux modèle local, aucun appel réseau ni quota consommé.
    """
    from app.test_mode import FAKE_MODEL, FakeLLM, fake_llm_enabled
    if fake_llm_enabled():
        PROVIDERS.setdefault("fake", FakeLLM())
        return "fake", FAKE_MODEL
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


# Délais entre les essais d'un même modèle : 3 essais au total, attente croissante.
RETRY_DELAYS = (2.0, 5.0)


def generate_with_retry(provider, model, settings, **kwargs):
    """Réessaie sur surcharge (503), puis passe au modèle de secours LLM_FALLBACK_MODEL.

    Quota atteint (429) : aucun nouvel essai sur le même modèle, passage direct au secours ; si tous les
    modèles sont à court de quota, QuotaProviderError avec un message clair. Le modèle de secours utilise
    le même fournisseur ; il est aussi essayé si le principal échoue pour une autre raison (modèle retiré,
    réponse invalide).
    """
    fallback = settings.llm_fallback_model.strip()
    if fallback.startswith(provider + "/"):
        fallback = fallback.split("/", 1)[1]
    models = [model] + ([fallback] if fallback and fallback != model else [])
    last_error, out_of_quota = None, []
    for current in models:
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                return PROVIDERS[provider].generate(model=current, api_key=settings.llm_api_key,
                                                    timeout=settings.http_timeout, **kwargs)
            except QuotaProviderError as exc:
                last_error = exc
                out_of_quota.append(current)
                break
            except RetryableProviderError as exc:
                last_error = exc
                if attempt < len(RETRY_DELAYS):
                    time.sleep(RETRY_DELAYS[attempt])
            except ProviderError as exc:
                last_error = exc
                break
    if out_of_quota and len(out_of_quota) == len(models):
        raise QuotaProviderError(f"Quota atteint pour {', '.join(models)} : réessayez plus tard "
                                 "(le quota se renouvelle en général le lendemain).")
    raise last_error
