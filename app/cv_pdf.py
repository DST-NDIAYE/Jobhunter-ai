"""CV PDF d'une page à partir de la sélection de tailor_cv et de cv_master.yaml.

Aucun appel au LLM ni au réseau : les coordonnées sont lues dans le master au moment du rendu.
Si le CV dépasse une page : d'abord les matières des formations antérieures sont retirées, puis
un accomplissement à la fois, en gardant au moins un accomplissement par projet affiché. La taille
de police n'est jamais réduite.
"""

import copy
from html import escape
import re
import unicodedata
from pathlib import Path

import pymupdf

from app.config import PROJECT_ROOT
from app.cv_tailor import _project_proofs, _projects

OUTPUT_DIR = PROJECT_ROOT / "output"
BODY_FONT_PT = 10

CATEGORY_LABELS = {
    "langages": "Langages",
    "data_science_machine_deep_learning": "Data Science, Machine & Deep Learning",
    "ia_generative_nlp_rag": "IA générative, NLP & RAG",
    "outils_methodes": "Outils & méthodes",
}

CSS = f"""
@page {{ size: A4; margin: 11mm 13mm; }}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: Helvetica, Arial, "Liberation Sans", sans-serif; font-size: {BODY_FONT_PT}pt;
       line-height: 1.28; color: #1a1a1a; }}
header {{ margin-bottom: 7pt; }}
h1 {{ font-size: 17pt; letter-spacing: 0.3pt; }}
.titre {{ font-size: 12pt; font-weight: bold; color: #1f4e79; margin-top: 1pt; }}
.contacts {{ margin-top: 3pt; }}
.contacts span + span::before {{ content: " · "; color: #777; }}
.accroche {{ margin-bottom: 4pt; }}
h2 {{ font-size: 11pt; color: #1f4e79; text-transform: uppercase; letter-spacing: 0.6pt;
      border-bottom: 0.8pt solid #1f4e79; padding-bottom: 1pt; margin: 7pt 0 3pt; }}
.ligne {{ display: flex; justify-content: space-between; gap: 8pt; }}
.experience {{ margin-bottom: 4pt; }}
.poste {{ font-weight: bold; }}
.meta {{ color: #444; }}
.projet {{ margin-top: 2pt; }}
.projet-titre {{ font-weight: bold; }}
.techno {{ color: #444; }}
ul {{ margin: 1pt 0 0 13pt; }}
li {{ margin-bottom: 0.5pt; }}
.bloc {{ margin-bottom: 2pt; }}
"""


def _e(value) -> str:
    return escape(str(value or ""), quote=True)


def _contacts(master: dict) -> str:
    contacts = master.get("contacts") or {}
    parts = []
    for key in ("email", "telephone", "localisation"):
        if contacts.get(key):
            parts.append(f"<span>{_e(contacts[key])}</span>")
    for key in ("linkedin", "github"):
        url = contacts.get(key) or ""
        if url.startswith("https://"):
            label = re.sub(r"^https://(www\.)?", "", url).rstrip("/")
            parts.append(f'<span><a href="{_e(url)}" style="color:inherit;text-decoration:none">{_e(label)}</a></span>')
    return "".join(parts)


def _dates(item: dict) -> str:
    return " – ".join(_e(item.get(k)) for k in ("date_debut", "date_fin") if item.get(k))


def _experiences(cv: dict) -> str:
    blocks = []
    for exp in cv["experiences"]:
        employer = " – ".join(_e(exp.get(k)) for k in ("entreprise", "service") if exp.get(k))
        contract = f" · {_e(exp['type_contrat'])}" if exp.get("type_contrat") else ""
        projects = []
        for project in exp["projets"]:
            if not project["accomplissements"]:
                continue
            techno = (f'<div class="techno">Technologies : {_e(", ".join(project["technologies"]))}</div>'
                      if project.get("technologies") else "")
            items = "".join(f"<li>{_e(a['texte'])}</li>" for a in project["accomplissements"])
            projects.append(f'<div class="projet"><div class="projet-titre">{_e(project["titre"])}</div>'
                            f"{techno}<ul>{items}</ul></div>")
        blocks.append(
            f'<div class="experience"><div class="ligne"><span class="poste">{_e(exp["poste"])}</span>'
            f'<span class="meta">{_dates(exp)}</span></div>'
            f'<div class="ligne"><span>{employer}{contract}</span><span class="meta">{_e(exp.get("lieu"))}</span></div>'
            f'{"".join(projects)}</div>')
    return "".join(blocks)


def _shown_projects(cv: dict) -> set[str]:
    return {p["id"] for e in cv["experiences"] for p in e["projets"] if p["accomplissements"]}


def _shown_subjects(master: dict, compact: bool) -> set[str]:
    """Formations dont les matières sont affichées ; en mode compact, seulement la plus récente."""
    entries = [f for f in master.get("formation") or [] if f.get("matieres")]
    return {f["id"] for f in (entries[:1] if compact else entries)}


def displayed_skills(cv: dict, master: dict, compact: bool = False) -> dict:
    """Une compétence principale n'est affichée que si un projet ou une formation affichés la prouvent.

    Sinon elle passe en « notions » si l'offre la cite, ou n'est pas affichée.
    """
    proofs = {s["nom"]: set(s.get("preuves") or []) for items in master["competences"].values() for s in items}
    shown = _shown_projects(cv) | _shown_subjects(master, compact)
    cited = set(cv.get("competences_citees_par_offre") or [])
    main, notions, moved, hidden = [], list(cv.get("competences_notions") or []), [], []
    for skill in cv["competences"]:
        if proofs.get(skill["nom"], set()) & shown:
            main.append(skill)
        elif skill["nom"] in cited:
            notions.append(skill)
            moved.append(skill["nom"])
        else:
            hidden.append(skill["nom"])
    return {"principales": main, "notions": notions, "passees_en_notions": moved, "masquees": hidden}


def _skills(cv: dict, master: dict, compact: bool) -> str:
    shown = displayed_skills(cv, master, compact)
    groups = {}
    for skill in shown["principales"]:
        groups.setdefault(skill["categorie"], []).append(skill["nom"])
    lines = [f'<div class="bloc"><b>{_e(CATEGORY_LABELS.get(cat, cat))} :</b> {_e(", ".join(groups[cat]))}</div>'
             for cat in master["competences"] if cat in groups]
    # Notions : seulement des compétences citées par l'offre (vérifié en code dans cv_tailor).
    if shown["notions"]:
        names = ", ".join(s["nom"] for s in shown["notions"])
        lines.append(f'<div class="bloc"><b>Notions :</b> {_e(names)}</div>')
    return "".join(lines)


def _education(master: dict, compact: bool) -> str:
    with_subjects = _shown_subjects(master, compact)
    blocks = []
    for item in master.get("formation") or []:
        subjects = (f'<div class="techno">{_e(", ".join(item["matieres"]))}</div>'
                    if item["id"] in with_subjects else "")
        blocks.append(
            f'<div class="bloc"><div class="ligne"><span class="poste">{_e(item["diplome"])}</span>'
            f'<span class="meta">{_dates(item)}</span></div>'
            f'<div class="ligne"><span>{_e(item.get("etablissement"))}</span>'
            f'<span class="meta">{_e(item.get("lieu"))}</span></div>{subjects}</div>')
    return "".join(blocks)


def _certifications(master: dict) -> str:
    lines = []
    for cert in master.get("certifications") or []:
        year = f" ({_e(cert['annee'])})" if cert.get("annee") else ""
        names = ", ".join(cert.get("intitules") or [])
        count = f"{cert['nombre']} certifications" if cert.get("nombre") else "Certifications"
        lines.append(f'<div class="bloc"><b>{_e(cert.get("organisme"))}</b> – {_e(count)}{year}'
                     f'{" : " + _e(names) if names else ""}</div>')
    return "".join(lines)


def _languages(master: dict) -> str:
    items = [f"{_e(l['langue'])} ({_e(l['niveau'])})" if l.get("niveau") else _e(l["langue"])
             for l in master.get("langues") or []]
    return f'<div class="bloc">{", ".join(items)}</div>' if items else ""


def render_html(cv: dict, master: dict, compact: bool = False) -> str:
    identity = master.get("identite") or {}
    name = " ".join(filter(None, [identity.get("prenom"), identity.get("nom")]))
    sections = [("Expériences professionnelles", _experiences(cv)), ("Compétences", _skills(cv, master, compact)),
                ("Formation", _education(master, compact)), ("Certifications", _certifications(master)),
                ("Langues", _languages(master))]
    body = "".join(f"<section><h2>{title}</h2>{content}</section>" for title, content in sections if content)
    return (f'<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>CV {_e(name)}</title>'
            f"<style>{CSS}</style></head><body>"
            f'<header><h1>{_e(name)}</h1><div class="titre">{_e(identity.get("titre"))}</div>'
            f'<div class="contacts">{_contacts(master)}</div></header>'
            f'<p class="accroche">{_e(cv["accroche"]["texte"])}</p>{body}</body></html>')


def _slug(text: str, max_len: int = 40) -> str:
    text = re.sub(r"\([^)]*\)", " ", text or "")  # « (H/F) »
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")[:max_len].strip("-")


def pdf_filename(cv: dict, master: dict) -> str:
    parts = ["CV", _slug((master.get("identite") or {}).get("nom") or "candidat", 30).upper(),
             _slug(cv["analyse"].get("entreprise")), _slug(cv["analyse"].get("intitule"))]
    return "_".join(p for p in parts if p) + ".pdf"


def without_accomplishments(cv: dict, item_ids) -> dict:
    removed = set(item_ids)
    trimmed = copy.deepcopy(cv)
    for exp in trimmed["experiences"]:
        for project in exp["projets"]:
            project["accomplissements"] = [a for a in project["accomplissements"] if a["id"] not in removed]
    trimmed["ordre_accomplissements"] = [i for i in trimmed["ordre_accomplissements"] if i not in removed]
    return trimmed


def _project_order(cv: dict) -> list[tuple[str, list[str]]]:
    """Projets affichés dans l'ordre de priorité du LLM, avec leurs accomplissements restants."""
    owner = {a["id"]: p["id"] for e in cv["experiences"] for p in e["projets"] for a in p["accomplissements"]}
    order = {}
    for item in cv["ordre_accomplissements"]:
        order.setdefault(owner[item], []).append(item)
    return list(order.items())


def linked_to_offer(project_id: str, cv: dict, master: dict) -> bool:
    """Le projet partage-t-il au moins une compétence ou un mot-clé avec l'offre ?"""
    analyse = cv.get("analyse") or {}
    items = [*(analyse.get("competences_requises") or []), *(analyse.get("competences_souhaitees") or []),
             *(analyse.get("mots_cles") or [])]
    project = {project_id: _projects(master)[project_id]}
    return any(_project_proofs(item, project) for item in items)


def next_reduction(cv: dict, master: dict) -> tuple[str, list[str]] | None:
    """Prochaine réduction : (projet, accomplissements à retirer), ou None s'il n'y en a plus.

    D'abord le dernier accomplissement du projet qui en a le plus (à égalité, le moins prioritaire) ;
    quand chaque projet n'en a plus qu'un, un projet entier mais seulement s'il n'a aucun lien avec l'offre.
    """
    projects = _project_order(cv)
    rank = {pid: i for i, (pid, _) in enumerate(projects)}
    crowded = [(pid, items) for pid, items in projects if len(items) > 1]
    if crowded:
        pid, items = max(crowded, key=lambda p: (len(p[1]), rank[p[0]]))
        return pid, [items[-1]]
    for pid, items in reversed(projects):
        if not linked_to_offer(pid, cv, master):
            return pid, items
    return None


def page_count(pdf: bytes) -> int:
    with pymupdf.open(stream=pdf, filetype="pdf") as document:
        return document.page_count


class ChromiumRenderer:
    """Convertit du HTML en PDF A4 avec Playwright ; un seul navigateur pour tous les essais."""

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch()
        self._page = self._browser.new_page()
        return self

    def __call__(self, html: str) -> bytes:
        self._page.set_content(html, wait_until="load")
        return self._page.pdf(format="A4", print_background=True, prefer_css_page_size=True)

    def __exit__(self, *exc):
        self._browser.close()
        self._playwright.stop()


def fit_one_page(cv: dict, master: dict, render) -> tuple[bytes, dict, dict]:
    """Réduit le contenu jusqu'à tenir sur une page, sans jamais réduire la police."""
    compact = False
    removed_items, removed_projects = [], []
    while True:
        pdf = render(render_html(cv, master, compact))
        if page_count(pdf) <= 1:
            skills = displayed_skills(cv, master, compact)
            return pdf, cv, {"matieres_formations_anterieures_retirees": compact,
                             "accomplissements_retires": removed_items, "projets_retires": removed_projects,
                             "competences_passees_en_notions": skills["passees_en_notions"],
                             "competences_masquees": skills["masquees"]}
        # Gagner de la place avant de toucher aux accomplissements.
        if not compact and len(_shown_subjects(master, False)) > 1:
            compact = True
            continue
        step = next_reduction(cv, master)
        if step is None:
            raise ValueError("Le CV dépasse une page : chaque projet lié à l'offre n'a plus qu'un accomplissement.")
        project_id, items = step
        if len(items) == len(dict(_project_order(cv))[project_id]):
            removed_projects.append(project_id)
        removed_items.extend(items)
        cv = without_accomplishments(cv, items)


def build_cv_pdf(cv: dict, master: dict, output_dir: Path = OUTPUT_DIR, render=None) -> dict:
    """Écrit le PDF d'une page et renvoie son chemin et le détail des réductions."""
    if render is None:
        with ChromiumRenderer() as chromium:
            pdf, final, details = fit_one_page(cv, master, chromium)
    else:
        pdf, final, details = fit_one_page(cv, master, render)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / pdf_filename(cv, master)
    path.write_bytes(pdf)
    return {"path": path, "pages": page_count(pdf), **details,
            "accomplissements_affiches": final["ordre_accomplissements"]}
