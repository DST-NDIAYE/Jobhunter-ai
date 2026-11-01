"""Suivi manuel des candidatures en SQLite. Aucune action d'envoi."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import math
import sqlite3

from app.database import _database_path, _decode_job, _identity
from app.opportunities import entity_table, decode_opportunity
from app.spontanees import RELANCE_APRES as RELANCE_SPONTANEE, STATUTS_APRES_ENVOI, url_entreprise

SPONTANEE = "spontanee"  # source des candidatures spontanées dans le suivi (identifiant : SIRET)

APPLICATION_STATUSES = (
    "Nouvelle", "À étudier", "À postuler", "Candidature préparée",
    "Prête à envoyer", "Candidature envoyée", "Entretien", "Refus", "Archivée",
    "En cours", "Intervention requise", "Échec",
)


def get_offer(external_id: str, source: str = "france_travail", db_path=None) -> dict | None:
    external_id, source = _identity(external_id, source)
    with closing(sqlite3.connect(_database_path(db_path))) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            f"SELECT * FROM {entity_table(source)} WHERE external_id = ? AND source = ?", (external_id, source)
        ).fetchone()
    return (decode_opportunity(row) if source == "labonneboite" else _decode_job(row)) if row else None


def get_applications(statuses: list[str] | None = None, db_path=None) -> list[dict]:
    if statuses and any(status not in APPLICATION_STATUSES for status in statuses):
        raise ValueError("Statut de candidature inconnu.")
    where = "WHERE application_status IS NOT NULL"
    if statuses:
        where += " AND application_status IN (" + ",".join("?" for _ in statuses) + ")"
    order = " ORDER BY status_updated_at DESC, id DESC"
    params = statuses or []
    with closing(sqlite3.connect(_database_path(db_path))) as connection:
        connection.row_factory = sqlite3.Row
        jobs = [_decode_job(row) for row in connection.execute("SELECT * FROM jobs " + where + order, params).fetchall()]
        opps = [decode_opportunity(row) for row in connection.execute("SELECT * FROM opportunities " + where + order, params).fetchall()]
    combined = jobs + opps
    combined.sort(key=lambda item: item.get("status_updated_at") or "", reverse=True)
    return combined


def application_stats(db_path=None) -> dict:
    with closing(sqlite3.connect(_database_path(db_path))) as connection:
        job_counts = dict(connection.execute("SELECT application_status, COUNT(*) FROM jobs GROUP BY application_status"))
        opp_counts = dict(connection.execute("SELECT application_status, COUNT(*) FROM opportunities GROUP BY application_status"))
    counts = {k: job_counts.get(k, 0) + opp_counts.get(k, 0) for k in set(job_counts) | set(opp_counts)}
    return {"total_offers": sum(counts.values()), **{status: counts.get(status, 0) for status in APPLICATION_STATUSES}}


def update_status(identities: list[tuple[str, str]], status: str, *, snapshots: dict | None = None, db_path=None) -> int:
    """Met à jour un lot atomiquement. Identité : (source, external_id).

    Les dates préparée/envoyée correspondent à la première attribution manuelle
    de ces statuts. Réappliquer le même statut ne change pas sa date.
    """
    if status not in APPLICATION_STATUSES:
        raise ValueError("Statut de candidature inconnu.")
    identities = list(dict.fromkeys(identities))
    snapshots = snapshots or {}
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    with closing(sqlite3.connect(_database_path(db_path))) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        for source, external_id in identities:
            external_id, source = _identity(external_id, source)
            row = connection.execute(
                f"SELECT application_status, keywords FROM {entity_table(source)} WHERE source=? AND external_id=?", (source, external_id)
            ).fetchone()
            if row is None:
                raise ValueError("Une offre sélectionnée n'existe plus dans SQLite. Actualisez la vue.")
            snapshot = snapshots.get((source, external_id), {})
            score = snapshot.get("match_score")
            if score is not None and (isinstance(score, bool) or not isinstance(score, (int, float))
                                      or not math.isfinite(score) or not 0 <= score <= 100):
                raise ValueError("Le score CV doit être compris entre 0 et 100.")
            keywords = list(dict.fromkeys(json.loads(row[1]) + list(snapshot.get("keywords") or [])))
            connection.execute(
                f"""UPDATE {entity_table(source)} SET application_status = ?,
                    status_updated_at = CASE WHEN application_status IS NOT ? THEN ? ELSE status_updated_at END,
                    application_prepared_at = CASE WHEN ? = 'Candidature préparée'
                        THEN COALESCE(application_prepared_at, ?) ELSE application_prepared_at END,
                    application_sent_at = CASE WHEN ? = 'Candidature envoyée'
                        THEN COALESCE(application_sent_at, ?) ELSE application_sent_at END,
                    application_match_score = COALESCE(?, application_match_score),
                    application_cv_name = COALESCE(?, application_cv_name), keywords = ?
                    WHERE source = ? AND external_id = ?""",
                (status, status, now, status, now, status, now, score,
                 snapshot.get("cv_name"), json.dumps(keywords, ensure_ascii=False), source, external_id),
            )
    return len(identities)


def save_notes(external_id: str, notes: str, source: str = "france_travail", db_path=None) -> None:
    """Les notes seules ne créent pas de candidature et ne changent pas le statut."""
    external_id, source = _identity(external_id, source)
    if not isinstance(notes, str) or len(notes) > 20000:
        raise ValueError("Les notes doivent contenir au plus 20 000 caractères.")
    with closing(sqlite3.connect(_database_path(db_path))) as connection, connection:
        cursor = connection.execute(
            f"UPDATE {entity_table(source)} SET personal_notes = ? WHERE source = ? AND external_id = ?",
            (notes, source, external_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("Offre introuvable dans SQLite.")


RELANCE_APRES = timedelta(days=7)
STATUTS_CLOS = ("Entretien", "Refus", "Archivée")


def _relance(status: str | None, sent_at: str | None, now: datetime,
             delay: timedelta = RELANCE_APRES) -> tuple[str | None, bool]:
    """Date de relance (envoi + 7 jours, 10 pour une candidature spontanée) et relance conseillée si elle est passée."""
    if status != "Candidature envoyée" or not sent_at:
        return None, False
    try:
        due = datetime.fromisoformat(sent_at.replace("Z", "+00:00")) + delay
    except ValueError:
        return None, False
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    return due.isoformat(timespec="seconds"), now >= due


def tracked_applications(db_path=None, now: datetime | None = None) -> list[dict]:
    """Candidatures réelles : offres ayant un statut, candidatures spontanées envoyées, et opportunités
    La Bonne Boîte de l'ancien module (lecture seule)."""
    now = now or datetime.now(timezone.utc)
    rows = []
    with closing(sqlite3.connect(_database_path(db_path))) as connection:
        connection.row_factory = sqlite3.Row
        for row in connection.execute(
            "SELECT source, external_id, company, title, application_status, application_sent_at, status_updated_at, url "
            "FROM jobs WHERE application_status IS NOT NULL"
        ):
            rows.append({"source": row["source"], "external_id": row["external_id"], "entreprise": row["company"] or "",
                         "poste": row["title"] or "", "statut": row["application_status"],
                         "date_envoi": row["application_sent_at"], "date": row["application_sent_at"] or row["status_updated_at"],
                         "url": row["url"], "lecture_seule": False})
        for row in connection.execute(
            "SELECT source, external_id, company_name, job_keyword, application_status, application_sent_at, "
            "status_updated_at, application_url, source_url FROM opportunities "
            "WHERE application_status IS NOT NULL OR application_sent_at IS NOT NULL"
        ):
            rows.append({"source": row["source"], "external_id": row["external_id"], "entreprise": row["company_name"] or "",
                         "poste": row["job_keyword"] or "", "statut": row["application_status"] or "",
                         "date_envoi": row["application_sent_at"], "date": row["application_sent_at"] or row["status_updated_at"],
                         "url": row["application_url"] or row["source_url"], "lecture_seule": True})
        for row in connection.execute(
            "SELECT siret, entreprise, metier, statut, envoyee_le, statut_le FROM spontanees WHERE envoyee_le IS NOT NULL"
        ):
            rows.append({"source": SPONTANEE, "external_id": row["siret"], "entreprise": row["entreprise"],
                         "poste": f"Candidature spontanée · {row['metier']}", "statut": row["statut"],
                         "date_envoi": row["envoyee_le"], "date": row["envoyee_le"] or row["statut_le"],
                         "url": url_entreprise(row["siret"]), "lecture_seule": False, "statuts": list(STATUTS_APRES_ENVOI)})
    for item in rows:
        delay = RELANCE_SPONTANEE if item["source"] == SPONTANEE else RELANCE_APRES
        item["relance_le"], item["relance_conseillee"] = _relance(item["statut"], item["date_envoi"], now, delay)
    return sorted(rows, key=lambda r: r["date"] or "", reverse=True)
