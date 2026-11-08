"""Faux site La Bonne Boîte pour tester le robot sans jamais toucher au vrai site : données fictives uniquement.

Il reprend le parcours réel relevé sur captures (textes visibles, titres, boutons ; aucun identifiant sur lequel le
robot pourrait s'appuyer) : recherche (liste et fiche : sans e-mail, déjà candidaté, bouton « Débuter votre
candidature spontanée »), formulaire (étape 1/2 documents, étape 2/2 lettre préremplie et compteur x/1500), Synthèse
avec bouton « Envoyer la candidature », page de confirmation, « Gérer vos candidatures ».

Pannes par SIRET (/__test__/scenario) : documents_lents (vos documents affichés 2 s après le formulaire),
documents_absents (liste vide jusqu'au rechargement), continuer_sans_effet (1er clic sur « Continuer » sans effet),
continuer_jamais (« Continuer » toujours sans effet), lettre_modifiee (lettre différente sur la Synthèse), captcha, captcha_synthese,
session (session perdue), inconnue (page non
reconnue), incertain (pas de confirmation après l'envoi), sans_email_envoi (page d'erreur « does not have an email »
après le clic, comme le vrai site), bouton_inactif (« Continuer » inactif : erreur), blocage (message de limite),
http429, deja_texte, deja_envoyee, brouillon.

Lancement autonome : python tests/faux_site_lbb.py 8766
"""

from datetime import datetime
from html import escape
import json
import sys
import threading
import time
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
import uvicorn

from app.test_mode import FAKE_COMPANIES

PRENOM, NOM, EMAIL = "CAMILLE", "EXEMPLE", "camille.exemple@example.test"
COMPANIES = {}
for _c in FAKE_COMPANIES:
    COMPANIES.setdefault(_c["siret"], _c)
_LOCK = threading.Lock()


def _etat_initial():
    return {"connecte": False, "scenarios": {}, "documents": ["CV_Camille_EXEMPLE.pdf"], "candidatures": [],
            "clic": None, "requetes": 0, "connexion_externe": False, "remplissage": 0, "homonyme": True, "eclair": False,
            "panne": None, "recherches": [], "captcha_resolu": False}


# Captcha : il disparaît quand le test le déclare résolu (comme quand vous le résolvez vous-même dans la fenêtre).
CAPTCHA = ('<div class="captcha"><iframe title="reCAPTCHA" src="/faux-captcha" width="304" height="78" '
           'style="border:1px solid #d1d5db;border-radius:4px"></iframe></div>'
           '<script>setInterval(async () => { const r = await fetch("/__test__/captcha_etat"); if ((await r.json()).resolu) '
           'document.querySelectorAll(".captcha").forEach((c) => c.remove()); }, 300);</script>')


ETAT = _etat_initial()
app = FastAPI(title="Faux site La Bonne Boîte (tests)")

CSS = """
* { box-sizing: border-box; } body { margin: 0; font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  color: #1f1235; background: #f7f7fb; }
header { display: flex; gap: 18px; align-items: center; padding: 14px 28px; background: #fff; border-bottom: 1px solid #e5e7eb; }
header .logo { font-weight: 700; color: #b9176e; font-size: 20px; } header .fictif { font-size: 12px; background: #fef3c7;
  color: #92400e; padding: 3px 8px; border-radius: 6px; } header nav { margin-left: auto; display: flex; gap: 16px; align-items: center; }
header a { color: #1f1235; font-weight: 600; text-decoration: none; }
main { max-width: 1100px; margin: 0 auto; padding: 24px 28px; } .ariane { font-size: 14px; color: #555; margin-bottom: 10px; }
h1 { font-size: 34px; margin: 0 0 8px; font-weight: 400; } h1 b { font-weight: 800; } h2 { font-size: 22px; margin: 26px 0 12px; }
.info { background: #eef6ff; border: 1px solid #bcd8f5; border-radius: 10px; padding: 12px 16px; margin: 12px 0; }
.doc { display: grid; grid-template-columns: 60px 1fr auto; gap: 14px; align-items: center; background: #fff;
  border: 2px solid #fff; border-radius: 8px; padding: 14px 18px; margin-top: 12px; } .doc.coche { border-color: #b9176e; }
.case { position: relative; width: 26px; height: 26px; border: 2px solid #1f1235; border-radius: 4px; }
.case input { position: absolute; inset: 0; width: 100%; height: 100%; margin: 0; opacity: 0; cursor: pointer; }
.case.coche { background: #1f1235; } .doc small { display: block; color: #444; }
textarea { width: 100%; min-height: 320px; border: 1px solid #d1d5db; border-radius: 6px; padding: 14px; font: inherit; }
.compteur { text-align: right; font-size: 13px; color: #555; } .boutons { display: flex; gap: 12px; flex-wrap: wrap; margin: 16px 0; }
.btn { border: 2px solid #6b21a8; color: #4c1d95; background: #fff; border-radius: 999px; padding: 10px 22px; font: inherit;
  font-weight: 700; cursor: pointer; text-decoration: none; display: inline-block; } .btn.primaire { background: #b9176e;
  border-color: #b9176e; color: #fff; } .btn[disabled] { opacity: .5; cursor: not-allowed; }
.ligne { margin: 8px 0; } .ligne strong { margin-right: 10px; } .chip { background: #1f1235; color: #fff; border-radius: 6px;
  padding: 4px 10px; margin-right: 6px; } .liste { display: grid; grid-template-columns: 360px 1fr; gap: 20px; }
.item { display: block; background: #fff; border: 2px solid #fff; border-radius: 8px; padding: 12px 16px; margin-bottom: 10px;
  color: inherit; text-decoration: none; } .item.actif { border-color: #b9176e; } .item b { font-size: 20px; }
.fiche { background: #fff; border-radius: 8px; padding: 18px 22px; } .captcha { margin: 16px 0; }
.connexion { max-width: 440px; margin: 40px auto; background: #fff; padding: 24px; border-radius: 10px; }
.connexion input { width: 100%; padding: 10px; margin: 6px 0 14px; border: 1px solid #d1d5db; border-radius: 8px; font: inherit; }
"""

# Crochet de test : simule votre clic sur le bouton d'envoi (calibrage), repéré par son texte visible.
CLIC_JS = """
setInterval(async () => {
  const r = await fetch('/__test__/clic'); const d = await r.json(); if (!d.clic) return;
  const b = [...document.querySelectorAll('button, a')].find((x) => x.innerText.trim() === d.clic);
  if (b && !b.dataset.clique) { b.dataset.clique = '1'; await fetch('/__test__/clic', { method: 'POST',
    headers: { 'Content-Type': 'application/json' }, body: '{"action": null}' }); b.click(); }
}, 300);
"""

NETTOYAGE_JS = r"""
const REFUSES = /[^a-zA-ZÀ-ÿŸœŒæÆ0-9\n .,;:!?'"()\-’«»`\\/@&€+*|<>^]/g;
const nettoyer = (v) => v ? v.normalize('NFKC').replace(REFUSES, '').slice(0, 1500) : '';
"""


def _page(titre: str, contenu: str, script: str = "", status_code: int = 200) -> HTMLResponse:
    if ETAT["connecte"]:
        nav = (f'<a href="/">Accueil</a><a href="/candidatures">Gérer vos candidatures</a>'
               f'<button type="button" class="btn">{PRENOM} {NOM}</button>')  # menu du compte, comme le vrai site
    else:
        nav = '<a href="/">Accueil</a><a href="/login">Se connecter</a>'
    # Ressource externe : le robot doit la bloquer (seuls La Bonne Boîte et France Travail sont autorisés).
    externe = '<img alt="" src="https://example.org/pixel-de-mesure.gif" width="1" height="1">'
    return HTMLResponse(
        f'<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>{escape(titre)} | La Bonne Boite (fictif)</title>'
        f'<style>{CSS}</style></head><body><header><span class="logo">La Bonne Boîte</span>'
        f'<span class="fictif">Site fictif de test : aucune donnée réelle</span><nav>{nav}</nav></header>'
        f'<main>{contenu}</main>{externe}<script>{CLIC_JS}{script}</script></body></html>', status_code=status_code)


def _nom(siret: str) -> str:
    return COMPANIES[siret]["company_name"]  # le site affiche la raison sociale, en majuscules


def _dep(c: dict) -> str:
    """Département tel que le site l'enregistre et l'affiche : « 1 » pour l'Ain (« 01 » ne trouve rien, comme le 07/11)."""
    dep = c["postcode"][:2]
    return dep[1] if dep.startswith("0") else dep


def _scenario(siret: str) -> str | None:
    return ETAT["scenarios"].get(siret)


def _candidature_de(siret: str) -> dict | None:
    return next((c for c in ETAT["candidatures"] if c["siret"] == siret), None)


@app.middleware("http")
async def compter(request: Request, call_next):
    if not request.url.path.startswith("/__test__"):
        ETAT["requetes"] += 1
        panne = ETAT["panne"]
        if panne and panne["nombre"] > 0 and request.url.path.startswith(panne["chemin"]):
            # Panne du serveur (comme le 504 du 06/11) : code d'erreur, réponse vide ou courte page d'erreur.
            panne["nombre"] -= 1
            return HTMLResponse(panne["corps"], status_code=panne["code"])
    return await call_next(request)


@app.get("/")
def accueil():
    return _page("Accueil", "<h1><b>Postulez autrement,</b><br>Envoyez votre candidature spontanée</h1>"
                            "<p>Site fictif utilisé par les tests de JobHunter.</p>")


PAR_PAGE = 10  # comme le vrai site sans « page_size » dans l'adresse (relevé le 06/11)


def _remplissage(rome: str, dep: str) -> list[dict]:
    """Entreprises sans e-mail mieux classées que les vôtres (comme sur le vrai site), et un homonyme : les entreprises
    de l'inventaire se retrouvent en pages 2 et 3, après un établissement du même nom (autre SIRET)."""
    if rome != "M1811" or dep != "75" or not ETAT["remplissage"]:
        return []
    autres = [{"siret": f"7{n:013d}", "company_name": f"GRANDE ENTREPRISE {n:02d}", "office_name": "", "city": "PARIS",
               "postcode": "75008", "naf_label": "Activités des sièges sociaux", "rome": rome, "email": "no",
               "hiring_potential": 99.5 - n * 0.2} for n in range(ETAT["remplissage"])]
    homonyme = {"siret": "79999999999999", "company_name": "CONSEIL FICTIF", "office_name": "", "city": "PARIS",
                "postcode": "75019", "naf_label": "Conseil en systèmes et logiciels informatiques", "rome": rome,
                "email": "yes", "hiring_potential": 99.9}
    return ([homonyme] if ETAT["homonyme"] else []) + autres


def _toutes(rome: str, dep: str) -> list[dict]:
    items = list({c["siret"]: c for c in FAKE_COMPANIES if c["rome"] == rome and _dep(c) == dep}.values())
    items += _remplissage(rome, dep)
    return sorted(items, key=lambda c: -c["hiring_potential"])  # potentiel d'embauche décroissant


@app.get("/recherche")
def recherche(rome: str = "M1811", department_number: str = "75", page: int = 1, siret: str = "", page_size: int = PAR_PAGE):
    """Liste à gauche (nom, ville, secteur), 10 par page ; fiche à droite au clic : trois états.

    La pagination n'a ni rôle ni lien : le robot doit passer de page en page par l'adresse (paramètre « page »).
    Le bandeau « déjà débuté » arrive après le reste de la fiche, comme sur le vrai site.
    """
    if not siret:
        ETAT["recherches"].append([rome, department_number, page])  # pages de résultats chargées (hors clic sur une fiche)
    toutes = _toutes(rome, department_number)
    items = toutes[(page - 1) * page_size: page * page_size]
    liste = "".join(
        f'<a class="item{" actif" if c["siret"] == siret else ""}" href="/recherche?{urlencode(dict(rome=rome, department_number=department_number, page=page, siret=c["siret"]))}">'
        f'<b>{escape(c["company_name"])}</b><br>{escape(c["city"].title())} - {_dep(c)}<br>{escape(c["naf_label"])}</a>'
        for c in items)
    pages = max(1, -(-len(toutes) // page_size))
    pagination = ('<div class="pagination">' + " ".join(f"<span>{n}</span>" for n in range(1, pages + 1))
                  + ' <span class="suivant">Suivant ›</span></div>')
    fiche, script = "<h2>Aucune fiche entreprise sélectionnée</h2><p>Veuillez choisir un élément dans la liste de recherche.</p>", ""
    c = next((x for x in toutes if x["siret"] == siret), None)
    if c is not None:
        debuter = (f'<a class="btn primaire" href="/candidature/nouvelle/{siret}?job={rome}&jobLabel=Data%20engineer">'
                   "Débuter votre candidature spontanée</a>")
        if c["email"] != "yes":
            etat = ('<div class="info"><b>Aucune adresse email n\'a été fournie par cette entreprise.</b><br>Pour postuler, '
                    'nous vous recommandons de contacter l\'entreprise par téléphone ou de visiter son site web.</div>')
        elif _candidature_de(siret) or _scenario(siret) in ("deja_texte", "deja_envoyee", "brouillon"):
            etat = f'<div class="etat-fiche">{debuter}</div>'  # remplacé un peu plus tard par le bandeau
            script = ("setTimeout(() => { document.querySelector('.etat-fiche').innerHTML = '<div class=\"info\">Vous avez "
                      "déjà débuté ou envoyé une candidature pour cette entreprise.</div>'; }, 700);")
        else:
            etat = debuter
        fiche = (f'{etat}<h2>{escape(c["company_name"])}</h2><p>{escape(c["city"].title())} - {_dep(c)}</p>'
                 f'<h3>Autre informations</h3><p>Secteur d\'activité<br>{escape(c["naf_label"])}</p>'
                 f'<p>Numéro de siret<br>{siret}</p>')
    if ETAT["eclair"]:  # rafraîchissement de session : passage éclair par la page de connexion, puis retour
        ETAT["eclair"] = False
        script += "setTimeout(() => { location.href = '/login?retour=' + encodeURIComponent(location.pathname + location.search); }, 300);"
    if not toutes:  # comme le vrai site : ni nombre annoncé, ni liste
        return _page("Recherche", '<div class="ariane">Accueil &gt; Recherche</div><p>Résultats de votre recherche</p>'
                                  '<h2>Aucun <b>résultats</b> trouvés</h2><h2>Les questions les plus fréquentes</h2>', script)
    return _page("Recherche", f'<div class="ariane">Accueil &gt; Recherche</div><p>Résultats de votre recherche</p>'
                              f'<h1><b>{len(toutes)} entreprises :</b> Data engineer à Paris</h1>'
                              f'<div class="liste"><div>{liste}{pagination}</div><div class="fiche">{fiche}</div></div>', script)


@app.get("/login")
def page_connexion(retour: str = "/"):
    retour_js = json.dumps(retour if retour.startswith("/") else "/")
    return _page("Connexion", f"""<section class="connexion"><h1>Se connecter avec France Travail</h1>
      <p class="info">Page de connexion fictive : sur le vrai site, vous saisissez vous-même vos identifiants.</p>
      <form method="post" action="/login?retour={quote(retour)}"><label>Identifiant<input name="identifiant" autocomplete="off"></label>
      <label>Mot de passe<input name="motdepasse" type="password"></label><button class="btn primaire">Se connecter</button></form>
      </section>""", f"""setInterval(async () => {{ const r = await fetch('/__test__/connecte'); const d = await r.json();
        if (d.connecte) location.href = {retour_js}; }}, 400);""")


@app.post("/login")
def connexion(retour: str = "/"):
    ETAT["connecte"] = True
    return RedirectResponse(retour if retour.startswith("/") else "/", status_code=303)


def _documents_html() -> str:
    lignes = []
    for nom in ETAT["documents"]:
        base, ext = (nom.rsplit(".", 1) + ["pdf"])[:2]
        lignes.append(f"""<div class="doc"><label class="case"><input type="checkbox">
          <span class="sr" style="position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)">Sélectionner le document</span></label>
          <div><b>{escape(base)} (document {escape(ext.upper())})</b><small>Date de mise en ligne : 16/10/2026 13:20</small>
          <small>Poids du fichier : 317.1 Ko</small></div><button type="button" class="btn">Télécharger</button></div>""")
    return "".join(lignes)


DEFAUT = (f"Bonjour,\n\n   Votre entreprise suscite tout mon intérêt, c'est pourquoi je me permets de vous transmettre ma "
          f"candidature spontanée. Je vous prie de trouver mon dossier en pièce jointe.\n\n   Dans l'attente de votre retour, "
          f"je reste à votre écoute.\n\n  Cordialement,\n\n  {PRENOM} {NOM}.")


@app.get("/candidature/nouvelle/{siret}")
def formulaire(siret: str, request: Request, job: str = "", jobLabel: str = ""):  # noqa: N803 - nom du vrai site
    scenario = _scenario(siret)
    if scenario == "session" and ETAT["connecte"]:
        ETAT["connecte"] = False  # session expirée sur cette entreprise
        ETAT["scenarios"].pop(siret)
    if not ETAT["connecte"] and ETAT["connexion_externe"]:
        # Comme le vrai site : connexion sur l'ancienne adresse de France Travail (bloquée en mode test).
        return RedirectResponse("https://authentification-candidat.pole-emploi.fr/connexion/oauth2/authorize"
                                "?realm=%2Findividu&state=jeton-fictif")
    if not ETAT["connecte"]:
        return RedirectResponse("/login?retour=" + quote(str(request.url.path) + "?" + request.url.query))
    if siret not in COMPANIES:
        return RedirectResponse("/errors/404")
    c = COMPANIES[siret]
    if scenario == "http429":
        return _page("Trop de requêtes", "<h1>Trop de requêtes</h1>", status_code=429)
    if scenario == "inconnue":
        return _page("Nouveautés", "<h1>Découvrez la nouvelle version de La Bonne Boîte</h1><p>Nous avons fait peau neuve "
                                   "pour mieux vous accompagner dans vos démarches. Bonne visite sur le site rénové.</p>")
    if scenario == "blocage":
        return _page("Candidatures", "<h1>Candidatures spontanées</h1><div class='info'>Vous avez atteint la limite de "
                                     "candidatures autorisées aujourd'hui. Merci de revenir demain.</div>")
    if c["email"] != "yes":
        return _page("Entreprise", f"<h2>{escape(_nom(siret))}</h2><div class='info'><b>Aucune adresse email n'a été "
                                   "fournie par cette entreprise.</b></div>")
    if scenario == "deja_texte":
        return _page("Entreprise", f"<h2>{escape(_nom(siret))}</h2><div class='info'>Vous avez déjà débuté ou envoyé une "
                                   "candidature pour cette entreprise.</div>")
    if scenario in ("deja_envoyee", "brouillon") and not _candidature_de(siret):
        with _LOCK:
            ETAT["candidatures"].append({"id": 7000 + len(ETAT["candidatures"]), "siret": siret,
                                         "etat": "sent" if scenario == "deja_envoyee" else "draft",
                                         "message": "Ancien message", "documents": [], "ancienne": True})
    if existante := _candidature_de(siret):
        cible = ("/candidature/recapitulatif/" if existante["etat"] == "sent" else "/candidature/editer/") + str(existante["id"])
        return RedirectResponse(cible)
    if scenario == "documents_absents":
        ETAT["scenarios"].pop(siret)  # liste jamais arrivée à ce chargement-ci ; au rechargement, elle est là
    captcha = CAPTCHA if scenario == "captcha" and not ETAT["captcha_resolu"] else ""
    inactif = " disabled" if scenario == "bouton_inactif" else ""
    contenu = f"""<div class="ariane">Accueil &gt; Recherche &gt; Débuter votre candidature spontanée</div>
      <h1><b>Débuter</b> votre candidature spontanée</h1>
      <h2>Pour le métier de : <span style="color:#6b21a8">{escape(jobLabel)}</span> dans l'entreprise {escape(_nom(siret))}</h2>
      <section><h2><b>Les documents que je souhaite joindre à ma candidature</b> (étape 1/2)</h2>
      <div class="info"><b>Vous pouvez ajouter jusqu'à 3 fichiers maximum.</b><br>Les types de fichiers acceptés sont : pdf,
      doc, docx, tnt, png, odt et rtf. Le poids maximum autorisé par fichier est de 5 Mo.</div>
      <p><b>Sélectionner au moins un document</b> (obligatoire) <button type="button" class="btn primaire">Importer un document</button></p>
      <div class="docs">{"" if scenario in ("documents_lents", "documents_absents") else _documents_html()}</div></section>
      <section><h2><b>Ma lettre de motivation</b> (étape 2/2)</h2>
      <p><b>Expliquez pourquoi vous souhaitez rejoindre cette entreprise</b> (champ obligatoire)</p>
      <div class="info"><b>Il est fortement déconseillé d'utiliser des emojis ou pictogrammes de type 🙂 👍 dans une lettre de
      motivation.</b><br>Ceux-ci ne seront pas pris en compte par La Bonne Boîte.</div>
      <textarea>{escape(DEFAUT)}</textarea><div class="compteur"></div>{captcha}
      <div class="erreurs"></div>
      <div class="boutons"><button type="button" class="btn">Enregistrer et finir plus tard</button>
      <button type="button" class="btn primaire"{inactif}>Continuer</button></div>
      <button type="button" class="btn">← Retour à l'étape précédente</button></section>"""
    script = NETTOYAGE_JS + f"""
      const zone = document.querySelector('textarea'), compteur = document.querySelector('.compteur');
      const compter = () => {{ compteur.textContent = zone.value.length + '/1500 caractères'; }};
      zone.addEventListener('input', () => {{ const v = nettoyer(zone.value); if (v !== zone.value) zone.value = v; compter(); }});
      compter();
      document.addEventListener('change', (ev) => {{ const cb = ev.target; if (!cb.closest || !cb.closest('.doc')) return;
        cb.closest('.doc').classList.toggle('coche', cb.checked); cb.closest('.case').classList.toggle('coche', cb.checked); }});
      {f"setTimeout(() => {{ document.querySelector('.docs').innerHTML = {json.dumps(_documents_html())}; }}, 2000);"
       if scenario == "documents_lents" else ""}
      const continuer = [...document.querySelectorAll('button')].find((b) => b.innerText.trim() === 'Continuer');
      let sansEffet = {json.dumps({"continuer_sans_effet": 1, "continuer_jamais": 10 ** 6}.get(scenario, 0))};
      continuer.addEventListener('click', async () => {{
        if (sansEffet > 0) {{ sansEffet -= 1; return; }}  // comme VIVERIS le 07/11 : le clic ne mène nulle part
        const docs = [...document.querySelectorAll('.doc')].filter((d) => d.querySelector('input').checked)
          .map((d) => d.querySelector('b').innerText);
        const erreurs = document.querySelector('.erreurs');
        erreurs.innerHTML = !docs.length ? '<div class="info">Aucun document sélectionné.</div>' : (docs.length > 3 ?
          '<div class="info">3 documents au plus.</div>' : '');
        if (!docs.length || docs.length > 3 || !zone.value) return;
        const r = await fetch('/api/candidatures', {{ method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ siret: {json.dumps(siret)}, job: {json.dumps(jobLabel)}, msg: zone.value, documents: docs }}) }});
        const d = await r.json(); location.href = '/candidature/recapitulatif/' + d.id;
      }});"""
    return _page("Débuter votre candidature spontanée", contenu, script)


@app.get("/faux-captcha")
def faux_captcha():
    return HTMLResponse('<body style="font:14px sans-serif;margin:12px"><input type="checkbox"> Je ne suis pas un robot</body>')


@app.post("/api/candidatures")
async def creer(request: Request):
    data = await request.json()
    with _LOCK:
        candidature = {"id": 5352900 + len(ETAT["candidatures"]), "siret": data["siret"], "etat": "draft",
                       "message": data["msg"], "job": data.get("job"),
                       # La Synthèse affiche le nom du fichier (« CV_Camille_EXEMPLE.pdf »).
                       "documents": [d.replace(" (document PDF)", ".pdf").replace(" (document DOCX)", ".docx")
                                     for d in data["documents"]]}
        ETAT["candidatures"].append(candidature)
    return {"id": candidature["id"]}


def _candidature(id_: int) -> dict | None:
    return next((c for c in ETAT["candidatures"] if c["id"] == id_), None)


@app.get("/candidature/recapitulatif/{id_}")
def recapitulatif(id_: int):
    if not ETAT["connecte"]:
        return RedirectResponse(f"/login?retour=/candidature/recapitulatif/{id_}")
    c = _candidature(id_)
    if c is None:
        return RedirectResponse("/errors/404")
    company = COMPANIES.get(c["siret"], {})
    captcha = CAPTCHA if _scenario(c["siret"]) == "captcha_synthese" and not ETAT["captcha_resolu"] else ""
    bouton = ('<div class="boutons"><button type="button" class="btn primaire">Envoyer la candidature</button></div>'
              if c["etat"] == "draft" else "")
    contenu = f"""<div class="ariane">Accueil &gt; <a href="/candidatures">Gérer vos candidatures</a> &gt; Synthèse de votre
      candidature spontanée</div><h1><b>Synthèse</b> de votre candidature spontanée</h1>
      <h2>Vérifier les informations ci-dessous avant d'envoyer votre candidature</h2>
      <h3>Vos coordonnées</h3><div class="ligne"><strong>Nom :</strong><span>{NOM}</span></div>
      <div class="ligne"><strong>Prénom :</strong><span>{PRENOM}</span></div>
      <div class="ligne"><strong>Email :</strong><span>{EMAIL}</span></div>
      <h3>L'entreprise</h3><div class="ligne"><strong>Nom de l'entreprise :</strong><a href="#">{escape(_nom(c['siret']))}</a></div>
      <div class="ligne"><strong>Métier recherché :</strong><span>{escape(c.get('job') or '')}</span></div>
      <div class="ligne"><strong>Lieu :</strong><span>{escape(company.get('city', '').title())}</span></div>
      <h3>Votre candidature spontanée</h3><div class="ligne"><strong>Date d'envoi :</strong>
      <span>{datetime.now().strftime('%d/%m/%Y %H:%M')}</span></div>
      <div class="ligne"><strong>Pièces jointes :</strong>{''.join(f'<span class="chip">{escape(d)}</span>' for d in c['documents'])}</div>
      <div class="ligne"><strong>Lettre de motivation :</strong></div><textarea readonly>{escape(_lettre_affichee(c))}</textarea>
      {captcha}{bouton}<button type="button" class="btn">← Retour l'étape précédente</button>"""
    script = f"""const b = [...document.querySelectorAll('button')].find((x) => x.innerText.trim() === 'Envoyer la candidature');
      if (b) b.addEventListener('click', async () => {{
        const r = await fetch('/api/candidatures/{id_}/envoi', {{ method: 'POST' }}); const d = await r.json();
        if (d.erreur) {{ location.href = '/errors/404?message=' + encodeURIComponent(d.erreur); return; }}
        if (d.code === 500) {{ location.href = '/errors/500?message=' + encodeURIComponent('500 Internal Server Error'); return; }}
        location.href = d.incertain ? '/candidature/traitement' : '/candidature/envoyee'; }});"""
    return _page("Synthèse de votre candidature spontanée", contenu, script)


def _lettre_affichee(c: dict) -> str:
    """Lettre telle que la Synthèse l'affiche : « & » devient « &amp; » (comme, semble-t-il, le vrai site le 06/11)."""
    lettre = c["message"].replace("&", "&amp;")
    return lettre + (" (modifié)" if _scenario(c["siret"]) == "lettre_modifiee" else "")


@app.get("/candidature/editer/{id_}")
def editer(id_: int):
    return _page("Modifier votre candidature", "<h1>Modifier votre candidature spontanée</h1>"
                                               "<p class='info'>Reprenez votre brouillon.</p>")


@app.post("/api/candidatures/{id_}/envoi")
def envoyer(id_: int):
    with _LOCK:
        c = _candidature(id_)
        if c is None or c["etat"] != "draft":
            return JSONResponse({"detail": "inconnue"}, status_code=404)
        if _scenario(c["siret"]) == "sans_email_envoi":  # comme le vrai site : refus, rien n'est envoyé
            return {"erreur": "The office for this application does not have an email"}
        if _scenario(c["siret"]) == "erreur_serveur":  # comme le vrai site le 06/11 : erreur 500, rien n'est confirmé
            return {"code": 500}
        incertain = _scenario(c["siret"]) == "incertain"
        c["etat"], c["envoyee_le"] = "sent", time.time()
    return {"ok": True, "incertain": incertain}


@app.get("/candidature/envoyee")
def envoyee():
    return _page("Candidature envoyée", f"""<h1><b>Candidature</b> envoyée</h1>
      <h3>Félicitation, votre candidature est envoyée !</h3>
      <p><strong>Votre candidature a bien été envoyée le {datetime.now().strftime('%d/%m/%Y')}.</strong><br>Afin d'optimiser
      votre chance, il est recommandé d'attendre entre 7 et 10 jours avant de relancer l'employeur.</p>""")


@app.get("/candidature/traitement")
def traitement():
    return _page("Traitement", "<h1>Traitement de votre demande</h1><p>Merci de patienter quelques instants.</p>")


@app.get("/candidatures")
def gerer():
    """La liste arrive après la page (comme sur le vrai site, vide au premier affichage)."""
    lignes = "".join(f"<li>{escape(_nom(c['siret']))} : {'Envoyée' if c['etat'] == 'sent' else 'Brouillon'}</li>"
                     for c in ETAT["candidatures"])
    return _page("Gérer vos candidatures", "<h1><b>Gérer</b> vos candidatures</h1><ul class='liste-candidatures'></ul>",
                 f"setTimeout(() => {{ document.querySelector('.liste-candidatures').innerHTML = {json.dumps(lignes)}; }}, 1800);")


@app.get("/errors/{code}")
def erreur(code: str, message: str = ""):
    rouge = f'<p style="color:#b42318;font-weight:600">{escape(message)}</p>' if message else ""
    titre = "Une erreur serveur s'est produite" if code.startswith("5") else "Page non trouvée"
    return _page("Erreur", f"<h1>{titre}</h1>{rouge}<p>Nous vous invitons à revenir sur cette page plus tard.</p>")


# Crochets de test

@app.post("/__test__/reinitialiser")
def reinitialiser():
    ETAT.clear()
    ETAT.update(_etat_initial())
    return {"ok": True}


@app.post("/__test__/connexion")
def simuler_connexion():
    ETAT["connecte"] = True
    return {"ok": True}


@app.post("/__test__/remplissage")
async def remplissage(request: Request):
    data = await request.json()
    ETAT["remplissage"], ETAT["homonyme"] = int(data["nombre"]), bool(data.get("homonyme", True))
    return {"ok": True}


@app.post("/__test__/panne")
async def panne(request: Request):
    data = await request.json()
    ETAT["panne"] = {"chemin": data.get("chemin", "/recherche"), "code": int(data["code"]),
                     "nombre": int(data.get("nombre", 1)), "corps": data.get("corps", "")}
    return {"ok": True}


@app.post("/__test__/captcha_resolu")
def captcha_resolu():
    ETAT["captcha_resolu"] = True
    return {"ok": True}


@app.get("/__test__/captcha_etat")
def captcha_etat():
    return {"resolu": ETAT["captcha_resolu"]}


@app.post("/__test__/eclair")
def eclair():
    ETAT["eclair"] = True
    return {"ok": True}


@app.post("/__test__/connexion_externe")
def connexion_externe():
    ETAT["connexion_externe"] = True
    return {"ok": True}


@app.get("/__test__/connecte")
def est_connecte():
    return {"connecte": ETAT["connecte"]}


@app.post("/__test__/scenario")
async def scenario(request: Request):
    data = await request.json()
    ETAT["scenarios"][data["siret"]] = data["type"]
    return {"ok": True}


@app.post("/__test__/documents")
async def documents(request: Request):
    ETAT["documents"] = list((await request.json())["noms"])
    return {"ok": True}


@app.post("/__test__/clic")
async def demander_clic(request: Request):
    ETAT["clic"] = (await request.json()).get("action")
    return {"ok": True}


@app.get("/__test__/clic")
def lire_clic():
    return {"clic": ETAT["clic"]}


@app.get("/__test__/etat")
def etat():
    return {"connecte": ETAT["connecte"], "candidatures": ETAT["candidatures"], "requetes": ETAT["requetes"],
            "eclair": ETAT["eclair"], "recherches": ETAT["recherches"]}


def demarrer(port: int) -> uvicorn.Server:
    """Démarre le faux site dans un fil d'exécution (tests) et attend qu'il réponde."""
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            return server
        time.sleep(0.05)
    raise RuntimeError("Le faux site La Bonne Boîte n'a pas démarré.")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]) if len(sys.argv) > 1 else 8766, log_level="warning")
