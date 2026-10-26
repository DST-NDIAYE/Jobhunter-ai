"""Entreprises spontanées séparées des offres ; interface de préparation commune."""

from contextlib import closing
import json
import sqlite3


def entity_table(source):
    return "opportunities" if source == "labonneboite" else "jobs"


def decode_opportunity(row):
    item = dict(row)
    item["keywords"] = json.loads(item.get("keywords") or "[]")
    can_apply_direct = bool(item.get("application_url") and "candidature/nouvelle" in str(item.get("application_url")))
    canal_info = "Candidature directe en ligne possible sur La Bonne Boîte." if can_apply_direct else "Candidature via site/contact externe."
    item.update(
        title=item.get("job_keyword") or "Data / IA",
        company=item.get("company_name"),
        url=item.get("application_url") or item.get("source_url"),
        skills=[],
        salary=None,
        match_score=item.get("application_match_score"),
        description=(
            f"Candidature spontanée via La Bonne Boîte ({canal_info})\n"
            f"Entreprise : {item.get('company_name') or 'Non renseignée'}\n"
            f"Secteur : {item.get('sector') or 'Non renseigné'}\n"
            f"Ville : {item.get('location') or 'Non renseignée'}\n"
            f"Code ROME : {item.get('rome') or 'Non renseigné'} · Métier ciblé : {item.get('job_keyword') or 'Data / IA'}\n"
            f"Potentiel d'embauche : {item.get('hiring_potential') if item.get('hiring_potential') is not None else 'Non renseigné'}"
        ),
    )
    return item


def upsert_opportunity(item, db_path):
    with closing(sqlite3.connect(db_path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT keywords FROM opportunities WHERE source=? AND external_id=?",
                         (item["source"], item["external_id"])).fetchone()
        keywords = list(dict.fromkeys((json.loads(row[0]) if row else []) + (item.get("keywords") or [])))
        fields = (
            "source", "external_id", "siret", "company_name", "sector", "naf", "rome", "job_keyword",
            "location", "website", "source_url", "application_url", "hiring_potential",
        )
        db.execute(f"""INSERT INTO opportunities ({','.join(fields)},keywords) VALUES ({','.join('?' for _ in fields)},?)
            ON CONFLICT(source,external_id) DO UPDATE SET
            last_seen_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),
            keywords=excluded.keywords,
            rome=COALESCE(excluded.rome, opportunities.rome),
            job_keyword=COALESCE(excluded.job_keyword, opportunities.job_keyword),
            website=COALESCE(excluded.website, opportunities.website),
            source_url=COALESCE(excluded.source_url, opportunities.source_url),
            application_url=COALESCE(excluded.application_url, opportunities.application_url),
            hiring_potential=COALESCE(excluded.hiring_potential, opportunities.hiring_potential)""",
                   (*[item.get(f) for f in fields], json.dumps(keywords, ensure_ascii=False)))

        from app.application_channels import detect_application_channel
        check_fields = ("application_url", "website", "contact_email", "contact_email_manual", "source_url")
        row = db.execute(
            "SELECT " + ",".join(check_fields) + " FROM opportunities WHERE source=? AND external_id=?",
            (item["source"], item["external_id"]),
        ).fetchone()
        channel = detect_application_channel(dict(zip(check_fields, row)))["channel"]
        db.execute("UPDATE opportunities SET application_channel=? WHERE source=? AND external_id=?",
                   (channel, item["source"], item["external_id"]))


def list_opportunities(db_path):
    with closing(sqlite3.connect(db_path)) as db:
        db.row_factory = sqlite3.Row
        return [decode_opportunity(row) for row in db.execute(
            "SELECT * FROM opportunities ORDER BY (application_url IS NOT NULL) DESC, hiring_potential DESC, id DESC"
        )]
