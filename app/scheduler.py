"""Planificateur de candidatures automatiques matinales (JobHunter Scheduler)."""

import asyncio
from datetime import datetime, time, timedelta, timezone
import json
import logging
from pathlib import Path
from typing import Any

from app.config import PROJECT_ROOT, get_settings

logger = logging.getLogger("jobhunter.scheduler")

CONFIG_PATH = PROJECT_ROOT / "data" / "scheduler_config.json"

# Planificateur désactivé : aucune candidature ne doit partir automatiquement.
SCHEDULER_DISABLED = True

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "time": "08:30",
    "target": "france_travail",  # "france_travail" ou "lbb"
    "max_applications": 5,
    "auto_send": False,  # True = envoi direct, False = mode préparation & vérification
    "tone": "modern",    # "modern", "classic", "impact"
    "last_run": None,
    "last_status": None,
    "last_summary": None,
    "next_run": None,
}

_scheduler_task: asyncio.Task | None = None
_is_running = False


def compute_next_run(time_str: str) -> str:
    """Calcule la prochaine occurrence ISO locale pour l'heure donnée (ex: '08:30')."""
    try:
        hour, minute = map(int, time_str.strip().split(":"))
    except Exception:
        hour, minute = 8, 30

    now = datetime.now()
    scheduled_today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < scheduled_today:
        next_dt = scheduled_today
    else:
        next_dt = scheduled_today + timedelta(days=1)
    return next_dt.isoformat(timespec="seconds")


def load_scheduler_config() -> dict[str, Any]:
    """Charge la configuration du planificateur depuis le fichier JSON persistant."""
    config = dict(DEFAULT_CONFIG)
    try:
        if CONFIG_PATH.is_file():
            stored = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                config.update(stored)
    except Exception as exc:
        logger.warning(f"Erreur lors du chargement de {CONFIG_PATH}: {exc}")

    # Assurer que next_run est cohérent
    if not config.get("next_run") or config.get("enabled"):
        config["next_run"] = compute_next_run(config.get("time", "08:30"))
    return config


def save_scheduler_config(data: dict[str, Any]) -> dict[str, Any]:
    """Met à jour et sauvegarde la configuration du planificateur."""
    config = load_scheduler_config()
    for key in ("enabled", "time", "target", "max_applications", "auto_send", "tone", "last_run", "last_status", "last_summary", "next_run"):
        if key in data:
            config[key] = data[key]

    if "time" in data:
        config["next_run"] = compute_next_run(config["time"])

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    return config


async def execute_scheduled_autopilot(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Exécute la tâche planifiée d'autopilote."""
    if SCHEDULER_DISABLED:
        logger.info("Autopilote planifié désactivé : aucune exécution.")
        return {"status": "disabled", "summary": {}}
    if config is None:
        config = load_scheduler_config()

    target = config.get("target", "france_travail")
    max_apps = int(config.get("max_applications", 5))
    auto_send = bool(config.get("auto_send", False))
    tone = str(config.get("tone", "modern"))
    settings = get_settings()

    logger.info(f"Lancement de l'autopilote planifié ({target}, max={max_apps}, auto_send={auto_send}, tone={tone})")
    start_time = datetime.now().isoformat(timespec="seconds")

    summary: dict[str, Any] = {}
    status = "success"
    try:
        if target == "france_travail":
            from app.ft_automation import run_daily_ft_autopilot
            summary = await asyncio.to_thread(
                run_daily_ft_autopilot,
                settings,
                max_applications=max_apps,
                dry_run=not auto_send,
                headless=False,
                tone=tone,
            )
        else:
            from app.lbb_automation import run_daily_autopilot
            summary = await asyncio.to_thread(
                run_daily_autopilot,
                settings,
                max_applications=max_apps,
                dry_run=not auto_send,
                headless=False,
                tone=tone,
            )
    except Exception as exc:
        status = "error"
        summary = {"error": str(exc)}
        logger.error(f"Échec de l'autopilote planifié : {exc}", exc_info=True)

    save_scheduler_config({
        "last_run": start_time,
        "last_status": status,
        "last_summary": summary,
        "next_run": compute_next_run(config.get("time", "08:30")),
    })

    return {
        "status": status,
        "target": target,
        "started_at": start_time,
        "summary": summary,
    }


async def _scheduler_loop():
    """Boucle d'arrière-plan vérifiant les horaires programmés."""
    global _is_running
    _is_running = True
    logger.info("Démarrage de la boucle du planificateur JobHunter")

    while _is_running:
        try:
            config = load_scheduler_config()
            if config.get("enabled"):
                next_run_str = config.get("next_run")
                if next_run_str:
                    try:
                        next_dt = datetime.fromisoformat(next_run_str)
                        now = datetime.now()
                        if now >= next_dt:
                            # Déclencher le run automatique
                            await execute_scheduled_autopilot(config)
                    except Exception as e:
                        logger.error(f"Erreur d'évaluation next_run: {e}")
        except Exception as exc:
            logger.error(f"Erreur dans la boucle du planificateur: {exc}")

        await asyncio.sleep(20)


def start_scheduler():
    """Démarre le worker asyncio du planificateur."""
    global _scheduler_task
    if SCHEDULER_DISABLED:
        return
    if _scheduler_task is None or _scheduler_task.done():
        _scheduler_task = asyncio.create_task(_scheduler_loop())


def stop_scheduler():
    """Arrête le worker asyncio du planificateur."""
    global _is_running, _scheduler_task
    _is_running = False
    if _scheduler_task and not _scheduler_task.done():
        _scheduler_task.cancel()
        _scheduler_task = None
