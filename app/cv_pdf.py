"""CV PDF d'une page à partir de tailor_cv et de cv_master.yaml, mise en page proche du CV ATS.

Aucun appel au LLM ni au réseau : les coordonnées sont lues dans le master au moment du rendu.
Tout le contenu est affiché. Seulement si le CV dépasse une page, dans cet ordre :
  a) resserrer interligne, marges (jamais sous 1,2 cm) et police du corps (jamais sous 9 pt) ;
  b) retirer la ligne Qualités ; c) retirer les matières des formations antérieures au Master ;
  d) retirer les puces les moins pertinentes, en gardant au moins 3 puces par projet.
Si cela ne suffit pas, une erreur claire est levée : aucun projet n'est jamais retiré.
"""

import copy
from html import escape
import re
import unicodedata
from pathlib import Path

import pymupdf

from app.config import PROJECT_ROOT
from app.cv_tailor import OffreAnalyse, keyword_coverage

OUTPUT_DIR = PROJECT_ROOT / "output"
MIN_PUCES_PAR_PROJET = 3

# Paliers de resserrement (police du corps en pt, interligne, marges en mm), du plus aéré au plus serré.
STYLES = (
    (10.0, 1.25, 15), (10.0, 1.15, 15), (10.0, 1.15, 13), (10.0, 1.1, 12),
    (9.5, 1.1, 12), (9.0, 1.1, 12), (9.0, 1.05, 12),
)
MIN_FONT_PT = 9.0
MIN_MARGIN_MM = 12

CATEGORY_LABELS = {
    "langages": "Langages",
    "data_science_machine_deep_learning": "Data Science, Machine & Deep Learning",
    "ia_generative_nlp_rag": "IA générative, NLP & RAG",
    "notions_master": "Notions (Master)",
    "outils_methodes": "Outils & méthodes",
}


def css(font: float, line_height: float, margin_mm: float) -> str:
    return f"""
@page {{ size: A4; margin: {margin_mm}mm; }}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: Calibri, Carlito, "Segoe UI", Helvetica, Arial, sans-serif; font-size: {font}pt;
       line-height: {line_height}; color: #1a1a1a; }}
a {{ color: #2f6db5; text-decoration: none; }}
h1 {{ font-size: {font * 2.1:.1f}pt; color: #1f3864; line-height: 1.1; }}
.titre {{ font-size: {font + 2:.1f}pt; font-weight: bold; color: #2f6db5; }}
.contacts {{ margin-top: 0.2em; }}
.accroche {{ margin-top: 0.5em; }}
h2 {{ font-size: {font + 2:.1f}pt; color: #1f3864; border-bottom: 0.8pt solid #2f6db5;
      padding-bottom: 0.1em; margin: 0.7em 0 0.35em; }}
.ligne {{ display: flex; justify-content: space-between; align-items: baseline; gap: 8pt; }}
.experience {{ margin-bottom: 0.3em; }}
.poste {{ font-weight: bold; font-size: {font + 1:.1f}pt; }}
/* Espace à droite : la dernière lettre en italique déborde sinon dans la marge et est rognée. */
.date {{ font-style: italic; color: #333; white-space: nowrap; padding-right: 0.25em; }}
.org {{ color: #444; }}
.projet {{ margin-top: 0.3em; }}
.projet-titre {{ font-weight: bold; color: #2f6db5; }}
.etab {{ font-style: italic; color: #333; }}
ul {{ list-style: none; margin-top: 0.05em; }}
/* Puce en vrai caractère « • » (lisible par les ATS), retrait suspendu. */
li {{ padding-left: 1em; text-indent: -1em; }}
.puce {{ color: #2f6db5; display: inline-block; width: 1em; text-indent: 0; }}
.bloc {{ margin-bottom: 0.2em; }}
"""


def _e(value) -> str:
    return escape(str(value or ""), quote=True)


def _rich(text: str) -> str:
    """Texte échappé, puis **…** du master rendu en gras."""
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", _e(text))


def _contacts(master: dict) -> str:
    contacts = master.get("contacts") or {}
    parts = [_e(contacts[key]) for key in ("email", "telephone", "localisation") if contacts.get(key)]
    for key, label in (("linkedin", "LinkedIn"), ("github", "GitHub")):
        url = contacts.get(key) or ""
        if url.startswith("https://"):
            shown = re.sub(r"^https://(www\.)?", "", url).rstrip("/")
            parts.append(f'{label} : <a href="{_e(url)}">{_e(shown)}</a>')
    return " | ".join(parts)


def _period(item: dict) -> str:
    dates = " à ".join(_e(item.get(k)) for k in ("date_debut", "date_fin") if item.get(k))
    text = f"De {dates}" if dates else ""
    return " | ".join(p for p in (text, _e(item.get("lieu"))) if p)


def _with_period(left: str, item: dict, css_class: str) -> str:
    return f'<div class="ligne"><span class="{css_class}">{left}</span><span class="date">{_period(item)}</span></div>'


def _experiences(cv: dict) -> str:
    blocks = []
    for exp in cv["experiences"]:
        contract = exp.get("type_contrat") or ""
        # « Data Scientist (Alternance) » comme le CV ATS ; un stage est déjà dit par « Stagiaire ».
        title = _e(exp["poste"]) + (f" ({_e(contract)})" if contract and contract.lower() != "stage" else "")
        employer = " – ".join(_e(exp.get(k)) for k in ("entreprise", "service") if exp.get(k))
        projects = []
        for project in exp["projets"]:
            techno = (f'<div><b>Technologies :</b> {_e(", ".join(project["technologies"]))}.</div>'
                      if project.get("technologies") else "")
            items = "".join(f'<li><span class="puce">•</span>{_rich(a["texte"])}</li>'
                            for a in project["accomplissements"])
            projects.append(f'<div class="projet"><div class="projet-titre">{_e(project["titre"])}</div>'
                            f"{techno}<ul>{items}</ul></div>")
        blocks.append(f'<div class="experience">{_with_period(title, exp, "poste")}'
                      f'<div class="org">{employer}</div>{"".join(projects)}</div>')
    return "".join(blocks)


def _subjects_shown(master: dict, earlier_subjects: bool) -> set[str]:
    """Formations dont les matières sont affichées ; sinon seulement la plus récente (le Master)."""
    entries = [f for f in master.get("formation") or [] if f.get("matieres")]
    return {f["id"] for f in (entries if earlier_subjects else entries[:1])}


def _education(master: dict, earlier_subjects: bool) -> str:
    shown = _subjects_shown(master, earlier_subjects)
    blocks = []
    for item in master.get("formation") or []:
        subjects = f'<div>{_e(", ".join(item["matieres"]))}.</div>' if item["id"] in shown else ""
        blocks.append(f'<div class="bloc">{_with_period(_e(item["diplome"]), item, "poste")}'
                      f'<div class="etab">{_e(item.get("etablissement"))}</div>{subjects}</div>')
    return "".join(blocks)


def _skills(cv: dict) -> str:
    return "".join(f'<div class="bloc"><b>{_e(CATEGORY_LABELS.get(group["categorie"], group["categorie"]))}</b> : '
                   f'{_e(", ".join(group["noms"]))}</div>' for group in cv["competences"] if group["noms"])


def _certifications(master: dict) -> tuple[str, str]:
    certs = master.get("certifications") or []
    if not certs:
        return "", ""
    if len(certs) == 1 and certs[0].get("organisme"):
        cert = certs[0]
        count = f" ({cert['nombre']})" if cert.get("nombre") else ""
        title = f"Certifications {cert['organisme']}{count}"
    else:
        title = "Certifications"
    names = [re.sub(r"\s+Certificate$", "", n) for c in certs for n in c.get("intitules") or []]
    return title, f'<div class="bloc">{" • ".join(_e(n) for n in names)}</div>'


def _languages_and_qualities(master: dict, qualities: bool) -> tuple[str, str]:
    languages = ", ".join(f"<b>{_e(l['langue'])}</b> ({_e(l['niveau'])})" if l.get("niveau") else f"<b>{_e(l['langue'])}</b>"
                          for l in master.get("langues") or [])
    parts = [f"<b>Langues</b> : {languages}"] if languages else []
    if qualities and master.get("qualites"):
        parts.append(f"<b>Qualités</b> : {_e(', '.join(master['qualites']))}")
    title = "Langues & Qualités" if len(parts) == 2 else ("Langues" if languages else "Qualités")
    return (title, f'<div class="bloc">{" | ".join(parts)}</div>') if parts else ("", "")


def render_html(cv: dict, master: dict, layout: dict | None = None) -> str:
    layout = {"style": 0, "qualites": True, "matieres_anterieures": True, **(layout or {})}
    font, line_height, margin = STYLES[layout["style"]]
    identity = master.get("identite") or {}
    name = " ".join(filter(None, [identity.get("prenom"), identity.get("nom")]))
    sections = [("Expériences professionnelles", _experiences(cv)),
                ("Formations", _education(master, layout["matieres_anterieures"])),
                ("Compétences", _skills(cv)),
                _certifications(master),
                _languages_and_qualities(master, layout["qualites"])]
    body = "".join(f"<section><h2>{_e(title)}</h2>{content}</section>" for title, content in sections if content)
    return (f'<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>CV {_e(name)}</title>'
            f"<style>{css(font, line_height, margin)}</style></head><body>"
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


def next_bullet(cv: dict) -> str | None:
    """La puce la moins pertinente dont le projet garde ensuite au moins MIN_PUCES_PAR_PROJET puces."""
    owner = {a["id"]: p["id"] for e in cv["experiences"] for p in e["projets"] for a in p["accomplissements"]}
    counts = {}
    for item in cv["ordre_accomplissements"]:
        counts[owner[item]] = counts.get(owner[item], 0) + 1
    return next((i for i in reversed(cv["ordre_accomplissements"]) if counts[owner[i]] > MIN_PUCES_PAR_PROJET), None)


def page_count(pdf: bytes) -> int:
    with pymupdf.open(stream=pdf, filetype="pdf") as document:
        return document.page_count


def pdf_text(pdf: bytes) -> str:
    with pymupdf.open(stream=pdf, filetype="pdf") as document:
        return "\n".join(page.get_text() for page in document)


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
    """Applique les réductions dans l'ordre, seulement tant que le CV dépasse une page."""
    layout = {"style": 0, "qualites": True, "matieres_anterieures": True}
    removed = []
    while True:
        pdf = render(render_html(cv, master, layout))
        if page_count(pdf) <= 1:
            font, line_height, margin = STYLES[layout["style"]]
            return pdf, cv, {"police_corps_pt": font, "interligne": line_height, "marges_mm": margin,
                             "qualites_retirees": not layout["qualites"],
                             "matieres_anterieures_retirees": not layout["matieres_anterieures"],
                             "puces_retirees": removed}
        if layout["style"] < len(STYLES) - 1:
            layout["style"] += 1
        elif layout["qualites"] and master.get("qualites"):
            layout["qualites"] = False
        elif layout["matieres_anterieures"] and len(_subjects_shown(master, True)) > 1:
            layout["matieres_anterieures"] = False
        else:
            item = next_bullet(cv)
            if item is None:
                raise ValueError(f"Le CV dépasse une page même resserré au maximum, sans Qualités ni matières "
                                 f"des licences et avec {MIN_PUCES_PAR_PROJET} puces par projet : "
                                 "raccourcissez le master.")
            removed.append(item)
            cv = without_accomplishments(cv, [item])


def build_cv_pdf(cv: dict, master: dict, output_dir: Path = OUTPUT_DIR, render=None) -> dict:
    """Écrit le PDF d'une page ; renvoie le chemin, les réductions et les mots-clés présents/absents."""
    if render is None:
        with ChromiumRenderer() as chromium:
            pdf, final, details = fit_one_page(cv, master, chromium)
    else:
        pdf, final, details = fit_one_page(cv, master, render)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / pdf_filename(cv, master)
    path.write_bytes(pdf)
    # Rapport calculé sur tout le texte du PDF final, notions comprises.
    coverage = keyword_coverage(OffreAnalyse.model_validate(cv["analyse"]), pdf_text(pdf), master)
    return {"path": path, "pages": page_count(pdf), **details,
            "puces_affichees": final["ordre_accomplissements"], "mots_cles": coverage}
