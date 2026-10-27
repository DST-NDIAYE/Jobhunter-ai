"""Contrat commun, sortie validée et transport sans journaliser les données."""

from typing import Protocol

import httpx
from pydantic import BaseModel


class ProviderError(Exception):
    """Erreur publique expurgée : aucun corps HTTP ni secret."""


class RetryableProviderError(ProviderError):
    """Service surchargé (HTTP 503) : un nouvel essai sur le même modèle peut réussir."""


class QuotaProviderError(ProviderError):
    """Quota atteint (HTTP 429) : inutile de réessayer le même modèle."""


RETRYABLE_STATUSES = (503,)
QUOTA_STATUS = 429


def is_retryable(exc: Exception) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in RETRYABLE_STATUSES


def is_quota(exc: Exception) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == QUOTA_STATUS


class Provider(Protocol):
    def generate(self, *, messages: list[dict], model: str, api_key: str,
                 timeout: float, schema: type[BaseModel]) -> BaseModel: ...


def chat_completion(endpoint, *, messages, model, api_key, timeout, openai=False,
                    schema: type[BaseModel]):
    name = schema.__name__.lower()
    payload = {
        "model": model, "messages": messages,
        "response_format": {"type": "json_schema", "json_schema": {
            "name": name, "strict": True,
            "schema": schema.model_json_schema(),
        }},
        "max_completion_tokens" if openai else "max_tokens": 3000,
    }
    if openai:
        payload["store"] = False
    try:
        # Aucun retry ni redirection : un clic déclenche au plus une requête.
        with httpx.Client(timeout=httpx.Timeout(min(max(timeout, 1), 60), connect=10),
                          follow_redirects=False) as client:
            response = client.post(endpoint, headers={"Authorization": "Bearer " + api_key}, json=payload)
            response.raise_for_status()
            if len(response.content) > 150000:
                raise ValueError("Réponse trop longue.")
            choice = response.json()["choices"][0]
            if choice["finish_reason"] != "stop" or choice["message"].get("refusal"):
                raise ValueError("Réponse incomplète ou refusée.")
            return schema.model_validate_json(choice["message"]["content"])
    except httpx.HTTPStatusError as exc:
        if is_quota(exc):
            raise QuotaProviderError("Quota du service IA atteint.") from None
        if is_retryable(exc):
            raise RetryableProviderError("Service IA surchargé, réessayez plus tard.") from None
        raise ProviderError("Service IA indisponible ou réponse invalide.") from None
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
        raise ProviderError("Service IA indisponible ou réponse invalide.") from None
