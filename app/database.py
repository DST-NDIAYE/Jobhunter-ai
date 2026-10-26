"""Stockage local des offres dans SQLite, sans dépendance externe."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path


def _database_path(db_path: str | Path | None) -> Path:
    """Utilise la configuration seulement si aucun chemin n'est fourni."""
    if db_path is None:
        from app.config import get_settings

        return Path(get_settings().database_path)
    return Path(db_path)


def init_db(db_path: str | Path | None = None) -> None:
    """Crée le dossier et la table si nécessaire, sans effacer les offres."""
    path = _database_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    external_id TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'france_travail',
                    title TEXT,
                    company TEXT,
                    location TEXT,
                    contract_type TEXT,
                    description TEXT,
                    publication_date TEXT,
                    url TEXT,
                    salary TEXT,
                    experience TEXT,
                    skills TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL DEFAULT
                        (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    UNIQUE (source, external_id)
                )
                """
            )
            # Migration additive des bases V1 ; conserver les dates et données existantes.
            columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
            for name, definition in (
                ("first_seen_at", "TEXT"), ("last_seen_at", "TEXT"),
                ("keywords", "TEXT NOT NULL DEFAULT '[]'"),
                ("application_status", "TEXT"),
                ("personal_notes", "TEXT NOT NULL DEFAULT ''"),
                ("status_updated_at", "TEXT"),
                ("application_prepared_at", "TEXT"),
                ("application_sent_at", "TEXT"),
                ("application_match_score", "REAL"),
                ("application_cv_name", "TEXT"),
                ("application_message", "TEXT NOT NULL DEFAULT ''"),
                ("application_letter", "TEXT NOT NULL DEFAULT ''"),
                ("application_analysis", "TEXT NOT NULL DEFAULT '{}'"),
                ("application_generated_at", "TEXT"),
                ("application_content_updated_at", "TEXT"),
                ("application_generation_method", "TEXT"),
                ("application_generation_provider", "TEXT"),
                ("application_generation_model", "TEXT"),
                ("application_preparation_context", "TEXT NOT NULL DEFAULT '{}'"),
                ("application_channel", "TEXT NOT NULL DEFAULT 'UNKNOWN'"),
                ("contact_email", "TEXT NOT NULL DEFAULT ''"),
                ("email_subject", "TEXT NOT NULL DEFAULT ''"),
                ("email_status", "TEXT NOT NULL DEFAULT 'NOT_READY'"),
                ("gmail_draft_id", "TEXT"),
                ("gmail_message_id", "TEXT"),
                ("draft_created_at", "TEXT"),
                ("send_error", "TEXT"),
                ("gmail_account_email", "TEXT"),
                ("gmail_draft_pending", "INTEGER NOT NULL DEFAULT 0"),
                ("application_url", "TEXT"), ("website", "TEXT"),
                ("application_attempted_at", "TEXT"), ("application_error", "TEXT"),
                ("contact_email_source", "TEXT"), ("contact_email_manual", "INTEGER NOT NULL DEFAULT 0"),
                ("contact_checked_at", "TEXT"),
                ("remote", "TEXT NOT NULL DEFAULT 'partial'"),
            ):
                if name not in columns:
                    connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
            if "contact_email_manual" not in columns:
                connection.execute("UPDATE jobs SET contact_email_manual=1, contact_email_source=COALESCE(contact_email_source, 'manual') WHERE contact_email != ''")
            connection.execute(
                "UPDATE jobs SET first_seen_at = COALESCE(first_seen_at, created_at), "
                "last_seen_at = COALESCE(last_seen_at, created_at) "
                "WHERE first_seen_at IS NULL OR last_seen_at IS NULL"
            )
            connection.execute("""CREATE TABLE IF NOT EXISTS opportunities (
                id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL DEFAULT 'labonneboite',
                external_id TEXT NOT NULL, siret TEXT, company_name TEXT, sector TEXT, naf TEXT, rome TEXT,
                job_keyword TEXT, location TEXT, website TEXT, source_url TEXT, hiring_potential REAL,
                keywords TEXT NOT NULL DEFAULT '[]',
                first_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                last_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                UNIQUE(source,external_id))""")
            # Même contenu de candidature, mais jamais une entreprise dans la table jobs.
            present = {r[1] for r in connection.execute('PRAGMA table_info(opportunities)')}
            shared = {'personal_notes','status_updated_at','contact_email','contact_email_source','contact_email_manual',
                      'contact_checked_at','email_subject','email_status','gmail_draft_id','gmail_message_id',
                      'draft_created_at','send_error','gmail_account_email','gmail_draft_pending'}
            for _, name, kind, required, default, _ in connection.execute('PRAGMA table_info(jobs)').fetchall():
                if name not in present and (name.startswith('application_') or name in shared):
                    definition = kind + (' NOT NULL' if required else '') + (f' DEFAULT {default}' if default is not None else '')
                    connection.execute(f'ALTER TABLE opportunities ADD COLUMN {name} {definition}')
            connection.execute("""CREATE TABLE IF NOT EXISTS application_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, external_id TEXT NOT NULL,
                channel TEXT, event TEXT NOT NULL, detail TEXT, url TEXT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))""")


def _identity(external_id: str, source: str) -> tuple[str, str]:
    if not isinstance(external_id, str) or not external_id.strip():
        raise ValueError("L'offre doit posséder un external_id non vide.")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("La source de l'offre doit être non vide.")
    return external_id.strip(), source.strip()


def add_job(job: dict, db_path: str | Path | None = None) -> bool:
    """Ajoute une offre normalisée ; renvoie False si elle existe déjà.

    Appeler init_db au démarrage. L'identifiant SQLite ``id`` est distinct de
    ``external_id``, l'identifiant fourni par France Travail. Les doublons
    existants conservent leur contenu ; dernière détection et mots-clés sont mis à jour.
    """
    external_id, source = _identity(
        job.get("external_id"), job.get("source", "france_travail")
    )
    salary = job.get("salary")
    values = (
        external_id,
        source,
        job.get("title"),
        job.get("company"),
        job.get("location"),
        job.get("contract_type"),
        job.get("description"),
        job.get("publication_date"),
        job.get("url"),
        json.dumps(salary, ensure_ascii=False) if salary is not None else None,
        job.get("experience"),
        json.dumps(job.get("skills") or [], ensure_ascii=False),
    )
    with closing(sqlite3.connect(_database_path(db_path))) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT INTO jobs (
                    external_id, source, title, company, location,
                    contract_type, description, publication_date, url,
                    salary, experience, skills
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, external_id) DO NOTHING
                """,
                values,
            )
            inserted = cursor.rowcount == 1
            keywords = json.loads(connection.execute(
                "SELECT keywords FROM jobs WHERE source = ? AND external_id = ?", (source, external_id)
            ).fetchone()[0])
            keywords = list(dict.fromkeys(keywords + list(job.get("keywords") or [])))
            connection.execute(
                "UPDATE jobs SET first_seen_at = COALESCE(first_seen_at, created_at), "
                "last_seen_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), keywords = ? "
                "WHERE source = ? AND external_id = ?",
                (json.dumps(keywords, ensure_ascii=False), source, external_id),
            )
            from app.application_channels import public_email, safe_url
            email = public_email(job.get("contact_email"))
            connection.execute("""UPDATE jobs SET application_url=COALESCE(?,application_url), website=COALESCE(?,website),
                contact_email_source=CASE WHEN contact_email='' AND contact_email_manual=0 AND ?<>'' THEN 'france_travail' ELSE contact_email_source END,
                contact_email=CASE WHEN contact_email='' AND contact_email_manual=0 THEN ? ELSE contact_email END
                WHERE source=? AND external_id=?""",
                (safe_url(job.get("application_url")), safe_url(job.get("website")), email, email, source, external_id))
            from app.application_channels import detect_application_channel
            fields = ('application_url','website','contact_email','contact_email_manual','url')
            row = connection.execute('SELECT '+','.join(fields)+' FROM jobs WHERE source=? AND external_id=?', (source, external_id)).fetchone()
            channel = detect_application_channel(dict(zip(fields, row)))['channel']
            connection.execute('UPDATE jobs SET application_channel=? WHERE source=? AND external_id=?', (channel, source, external_id))
            return inserted


def job_exists(
    external_id: str,
    source: str = "france_travail",
    db_path: str | Path | None = None,
) -> bool:
    """Vérifie un identifiant dans sa source d'origine."""
    external_id, source = _identity(external_id, source)
    with closing(sqlite3.connect(_database_path(db_path))) as connection:
        row = connection.execute(
            "SELECT 1 FROM jobs WHERE source = ? AND external_id = ? LIMIT 1",
            (source, external_id),
        ).fetchone()
    return row is not None


def get_jobs(
    db_path: str | Path | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """Lit les offres, de la plus récemment enregistrée à la plus ancienne.

    Le salaire et les compétences retrouvent leurs structures JSON d'origine.
    ``limit`` doit être positif et ``offset`` doit être positif ou nul.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit doit être un entier strictement positif.")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset doit être un entier positif ou nul.")

    with closing(sqlite3.connect(_database_path(db_path))) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT * FROM jobs ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()

    return [_decode_job(row) for row in rows]


def _decode_job(row) -> dict:
    job = dict(row)
    job["salary"] = json.loads(job["salary"]) if job["salary"] else None
    job["skills"] = json.loads(job["skills"])
    job["keywords"] = json.loads(job["keywords"])
    return job
