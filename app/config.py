"""Configuration locale ; clé LLM exclusivement dans .env, autres valeurs surchargeables."""

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def project_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


@dataclass(frozen=True)
class Settings:
    france_travail_client_id: str = field(default="", repr=False)
    france_travail_client_secret: str = field(default="", repr=False)
    france_travail_scope: str = "api_offresdemploiv2 o2dsoffre"
    http_timeout: float = 20.0
    database_path: Path = PROJECT_ROOT / "data/jobhunter.db"
    llm_provider: str = "local"
    llm_api_key: str = field(default="", repr=False)
    llm_model: str = ""
    llm_fallback_model: str = ""
    # Modèles de la lettre de motivation (par défaut ceux de l'analyse) ; jamais un modèle « lite ».
    letter_model: str = ""
    letter_fallback_model: str = ""
    gmail_token_file: Path = PROJECT_ROOT / "tokens/gmail_token.json"
    gmail_user: str = ""
    gmail_app_password: str = field(default="", repr=False)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    timeout = float(os.getenv("HTTP_TIMEOUT", "20"))
    if not 0 < timeout <= 120:
        raise ValueError("HTTP_TIMEOUT doit être compris entre 0 (exclu) et 120 secondes.")
    env_dict = dotenv_values(PROJECT_ROOT / ".env")
    return Settings(
        france_travail_client_id=os.getenv("FRANCE_TRAVAIL_CLIENT_ID", "").strip(),
        france_travail_client_secret=os.getenv("FRANCE_TRAVAIL_CLIENT_SECRET", "").strip(),
        france_travail_scope=os.getenv(
            "FRANCE_TRAVAIL_SCOPE", "api_offresdemploiv2 o2dsoffre"
        ).strip(),
        http_timeout=timeout,
        database_path=project_path(os.getenv("DATABASE_PATH", "data/jobhunter.db")),
        llm_provider=os.getenv("LLM_PROVIDER", "local").strip() or "local",
        # La clé LLM provient uniquement du fichier local, jamais d'un widget ou de SQLite.
        llm_api_key=(env_dict.get("LLM_API_KEY") or "").strip(),
        llm_model=os.getenv("LLM_MODEL", "").strip(),
        llm_fallback_model=os.getenv("LLM_FALLBACK_MODEL", "").strip(),
        letter_model=os.getenv("LETTER_MODEL", "").strip(),
        letter_fallback_model=os.getenv("LETTER_FALLBACK_MODEL", "").strip(),
        gmail_token_file=project_path(os.getenv("GMAIL_TOKEN_FILE") or "tokens/gmail_token.json"),
        gmail_user=os.getenv("GMAIL_USER", "").strip(),
        gmail_app_password=(env_dict.get("GMAIL_APP_PASSWORD") or os.getenv("GMAIL_APP_PASSWORD") or "").strip(),
    )
