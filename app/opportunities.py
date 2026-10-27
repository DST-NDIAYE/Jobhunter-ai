"""Historique des entreprises La Bonne Boîte (lecture seule : plus aucune écriture)."""

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


def list_opportunities(db_path):
    with closing(sqlite3.connect(db_path)) as db:
        db.row_factory = sqlite3.Row
        return [decode_opportunity(row) for row in db.execute(
            "SELECT * FROM opportunities ORDER BY (application_url IS NOT NULL) DESC, hiring_potential DESC, id DESC"
        )]
