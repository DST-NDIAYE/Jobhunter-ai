"""Suivi manuel des candidatures en SQLite. Aucune action d'envoi."""

from contextlib import closing
from datetime import datetime, timezone
import json
import math
import sqlite3

from app.database import _database_path, _decode_job, _identity
from app.opportunities import entity_table, decode_opportunity

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


def save_preparation(external_id: str, message: str, letter: str, source="france_travail",
                     db_path=None, *, generated: dict | None = None, ready=False) -> None:
    """Sauvegarde atomique du contenu et du statut, sans toucher aux notes ni aux envois.

    Une modification de contenu prêt à envoyer le remet en préparation.
    Une régénération n'est persistée que lors de l'enregistrement explicite.
    """
    external_id, source = _identity(external_id, source)
    for text in (message, letter):
        if not isinstance(text, str) or not text.strip() or len(text) > 30000:
            raise ValueError("Le message et la lettre doivent contenir entre 1 et 30 000 caractères.")
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    with closing(sqlite3.connect(_database_path(db_path))) as connection, connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(f"SELECT * FROM {entity_table(source)} WHERE source=? AND external_id=?", (source, external_id)).fetchone()
        if row is None:
            raise ValueError("Offre introuvable dans SQLite.")
        if not generated and not row["application_generated_at"]:
            raise ValueError("Préparez d'abord le contenu de la candidature.")
        changed = message != row["application_message"] or letter != row["application_letter"] or generated is not None
        status = "Prête à envoyer" if ready else (
            "Candidature préparée" if generated or (changed and row["application_status"] == "Prête à envoyer")
            else row["application_status"]
        )
        connection.execute(
            f"""UPDATE {entity_table(source)} SET application_message=?, application_letter=?,
               application_content_updated_at=?, application_status=?,
               status_updated_at=CASE WHEN application_status IS NOT ? THEN ? ELSE status_updated_at END,
               application_prepared_at=COALESCE(application_prepared_at, ?)
               WHERE source=? AND external_id=?""",
            (message, letter, now if changed else row["application_content_updated_at"], status, status, now, now, source, external_id),
        )
        if generated:
            context = generated["context"]
            connection.execute(
                f"""UPDATE {entity_table(source)} SET application_analysis=?, application_generated_at=?,
                   application_generation_method=?, application_generation_provider=?,
                   application_generation_model=?, application_preparation_context=?,
                   application_cv_name=?, application_match_score=? WHERE source=? AND external_id=?""",
                (json.dumps(generated["analysis"], ensure_ascii=False), generated["generated_at"], generated["method"],
                 generated.get("provider", "local"), generated.get("model", ""),
                 json.dumps(context, ensure_ascii=False), context["cv_name"], context["match_score"], source, external_id),
            )
