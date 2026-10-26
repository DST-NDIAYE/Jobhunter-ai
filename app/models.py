"""Schémas des réponses JSON, également visibles dans Swagger."""

from typing import Any

from pydantic import BaseModel, Field


class Job(BaseModel):
    external_id: str = Field(description="Identifiant de l'offre chez France Travail")
    title: str | None = None
    company: str | None = None
    location: str | None = None
    contract_type: str | None = None
    description: str | None = None
    publication_date: str | None = None
    salary: dict[str, Any] | None = None
    experience: str | None = None
    skills: list[dict[str, Any]] = Field(default_factory=list)
    url: str | None = None
    source: str = "france_travail"
    contact_email: str = ""
    application_url: str | None = None
    website: str | None = None


class MatchedJob(Job):
    match_score: float = Field(ge=0, le=100, description="Similarité lexicale CV/offre sur 100")
