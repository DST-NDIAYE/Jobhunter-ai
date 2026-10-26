"""Contrat commun, sortie validée et transport sans journaliser les données."""

from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class GenerationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    fit_summary: str
    matching_skills: list[str]
    relevant_cv_points: list[str]
    missing_skills: list[str]
    short_message: str
    cover_letter: str

    @field_validator("fit_summary", "short_message", "cover_letter")
    @classmethod
    def bounded_text(cls, value):
        value = value.strip()
        if not value or len(value) > 12000:
            raise ValueError("Texte absent ou trop long.")
        return value

    @field_validator("matching_skills", "relevant_cv_points", "missing_skills")
    @classmethod
    def bounded_list(cls, value):
        if len(value) > 100 or any(not s.strip() or len(s) > 2000 for s in value):
            raise ValueError("Liste invalide.")
        return [s.strip() for s in value]

    @model_validator(mode="after")
    def concise_texts(self):
        if len(self.cover_letter.split()) > 350 or len(self.short_message.split()) > 180:
            raise ValueError("Rédaction trop longue.")
        return self


class ProviderError(Exception):
    """Erreur publique expurgée : aucun corps HTTP ni secret."""


class Provider(Protocol):
    def generate(self, *, messages: list[dict], model: str, api_key: str,
                 timeout: float) -> GenerationResult: ...


def chat_completion(endpoint, *, messages, model, api_key, timeout, openai=False):
    payload = {
        "model": model, "messages": messages,
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "application_preparation", "strict": True,
            "schema": GenerationResult.model_json_schema(),
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
            return GenerationResult.model_validate_json(choice["message"]["content"])
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
        raise ProviderError("Service IA indisponible ou réponse invalide.") from None
