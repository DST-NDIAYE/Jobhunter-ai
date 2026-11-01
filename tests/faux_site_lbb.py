"""Faux site La Bonne Boîte pour tester le robot sans jamais toucher au vrai site : données fictives uniquement.

Il reprend la structure relevée dans le code public du site (identifiants #message-textarea, #send-application,
cases doc-N-select, étapes nouvelle → récapitulatif → envoyée, nettoyage du message à 1 500 caractères).
Crochets de test (/__test__/…) : connexion simulée, scénarios par SIRET (captcha, page inattendue, déjà envoyée,
brouillon, erreur), documents, clics simulés de l'utilisateur, état des candidatures reçues.

Lancement autonome : python tests/faux_site_lbb.py 8766
"""

from html import escape
import json
import sys
import threading
import time
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
import uvicorn

from app.test_mode import FAKE_COMPANIES

UTILISATEUR = "Camille EXEMPLE"
COMPANIES = {c["siret"]: c for c in FAKE_COMPANIES}
_LOCK = threading.Lock()


def _etat_initial():
    return {"connecte": False, "scenarios": {}, "documents": ["CV_Camille_EXEMPLE.pdf"], "candidatures": [],
            "clic": None}


ETAT = _etat_initial()
app = FastAPI(title="Faux site La Bonne Boîte (tests)")

CSS = """
* { box-sizing: border-box; } body { margin: 0; font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  color: #1f2937; background: #f3f4f6; }
header { display: flex; gap: 16px; align-items: center; padding: 14px 28px; background: #fff; border-bottom: 1px solid #e5e7eb; }
header .logo { font-weight: 700; color: #b9176e; } header .fictif { font-size: 12px; background: #fef3c7; color: #92400e;
  padding: 3px 8px; border-radius: 6px; } header .user { margin-left: auto; color: #6b7280; }
main { max-width: 980px; margin: 0 auto; padding: 28px; } section { background: #fff; border-radius: 10px; padding: 22px 26px;
  margin-bottom: 18px; } h1 { font-size: 24px; margin: 0 0 6px; font-weight: 500; } h2 { font-size: 18px; margin: 0 0 12px; font-weight: 500; }
.text-secondary { color: #b9176e; } .alert { background: #eff6ff; border-radius: 8px; padding: 10px 14px; margin: 10px 0; font-size: 14px; }
.alert.error { background: #fdf2f8; color: #9d174d; } article { display: grid; grid-template-columns: 48px 1fr auto; gap: 14px;
  align-items: center; border: 2px solid #e5e7eb; border-radius: 10px; padding: 12px 16px; margin-top: 10px; }
article.coche { border-color: #b9176e; } .sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }
.font-extrabold { font-weight: 800; } small { color: #6b7280; } input[type=checkbox] { width: 22px; height: 22px; }
textarea { width: 100%; min-height: 300px; border: 1px solid #d1d5db; border-radius: 8px; padding: 12px; font: inherit; }
.compteur { text-align: right; font-size: 13px; color: #6b7280; } .boutons { display: flex; gap: 10px; }
.btn { border: 2px solid #b9176e; color: #b9176e; background: #fff; border-radius: 8px; padding: 10px 18px; font: inherit;
  font-weight: 600; cursor: pointer; text-decoration: none; display: inline-block; } .btn-primary { background: #b9176e; color: #fff; }
dl { display: grid; grid-template-columns: 200px 1fr; gap: 6px 12px; } dt { font-weight: 700; } .tag { background: #e5e7eb;
  border-radius: 6px; padding: 2px 8px; margin-right: 6px; } .g-recaptcha { margin: 16px 0; }
.connexion { max-width: 420px; margin: 40px auto; } .connexion input { width: 100%; padding: 10px; margin: 6px 0 14px;
  border: 1px solid #d1d5db; border-radius: 8px; font: inherit; }
"""

CLIC_JS = """
// Crochet de test : simule les clics de l'utilisateur dans la fenêtre du robot.
setInterval(async () => {
  const r = await fetch('/__test__/clic'); const d = await r.json();
  if (d.clic === 'envoyer') { const b = document.querySelector('#send-application'); if (b && !b.dataset.clique) { b.dataset.clique = '1'; b.click(); } }
}, 400);
"""

NETTOYAGE_JS = r"""
const REFUSES = /[^a-zA-Z\u00c0-\u00ff\u0178\u0153\u0152\u00e6\u00c60-9\n .,;:!?'"()\-\u2019\u00ab\u00bb`\\/@&\u20ac+*|<>^]/g;
const nettoyer = (v) => v ? v.normalize('NFKC').replace(REFUSES, '').replace(/\n{3,}/g, '\n\n').slice(0, 1500) : '';
"""


def _page(titre: str, contenu: str, script: str = "", status_code: int = 200) -> HTMLResponse:
    user = f'<span class="user">{UTILISATEUR} · <a href="/auth/logout">Se déconnecter</a></span>' if ETAT["connecte"] else ""
    return HTMLResponse(
        f'<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>{escape(titre)} | La Bonne Boite (fictif)</title>'
        f'<style>{CSS}</style></head><body><header><span class="logo">La Bonne Boîte</span>'
        f'<span class="fictif">Site fictif de test : aucune donnée réelle</span>{user}</header><main>{contenu}</main>'
        f'<script>{script}</script></body></html>', status_code=status_code)


def _nom(siret: str) -> str:
    company = COMPANIES[siret]
    return company["office_name"] or company["company_name"]


@app.get("/")
def accueil():
    return _page("Accueil", "<section><h1>La Bonne Boîte</h1><p>Site fictif utilisé par les tests de JobHunter.</p></section>")


@app.get("/entreprise/{siret}")
def fiche(siret: str):
    if siret not in COMPANIES:
        return RedirectResponse("/errors/404")
    c = COMPANIES[siret]
    return _page("Fiche Entreprise", f"""<section><h1>Fiche Entreprise</h1><h2>{escape(_nom(siret))}</h2>
      <p>{escape(c['city'])} · {escape(c['naf_label'])}</p>
      <a class="btn btn-primary" href="/candidature/nouvelle/{siret}?job={c['rome']}&jobLabel=Data%20scientist">
      Débuter votre candidature spontanée</a></section>""")


@app.get("/login")
def page_connexion(retour: str = "/"):
    retour_js = json.dumps(retour if retour.startswith("/") else "/")
    return _page("Connexion", f"""<section class="connexion"><h1>Se connecter avec France Travail</h1>
      <p class="alert">Page de connexion fictive : dans le vrai site, vous saisissez vous-même vos identifiants.</p>
      <form method="post" action="/login?retour={quote(retour)}"><label>Identifiant<input name="identifiant" autocomplete="off"></label>
      <label>Mot de passe<input name="motdepasse" type="password"></label><button class="btn btn-primary">Se connecter</button></form>
      </section>""", f"""setInterval(async () => {{ const r = await fetch('/__test__/connecte'); const d = await r.json();
        if (d.connecte) location.href = {retour_js}; }}, 400);""")


@app.post("/login")
def connexion(retour: str = "/"):
    ETAT["connecte"] = True
    return RedirectResponse(retour if retour.startswith("/") else "/", status_code=303)


def _documents_html() -> str:
    if not ETAT["documents"]:
        return ('<div id="no-documents"><h2><strong>Aucun document</strong> enregistré</h2>'
                "<p>Veuillez importer un ou plusieurs documents.</p></div>")
    rows = []
    for i, nom in enumerate(ETAT["documents"]):
        base, ext = (nom.split(".", 1) + ["pdf"])[:2]
        rows.append(f"""<article><div><input type="checkbox" class="checkbox" id="doc-{i}-select"></div><div>
          <label for="doc-{i}-select"><span class="sr-only">Sélectionner le document </span>
          <span class="font-extrabold">{escape(base)} (document {escape(ext.upper())})</span><br>
          <small>Date de mise en ligne : <b>12/10/2026 09:14</b></small><br><small>Poids du fichier : <b>182.4 Ko</b></small>
          </label></div><div><button type="button" class="btn" id="doc-{i}-download">Télécharger</button></div></article>""")
    return ('<div role="group" aria-labelledby="doc-group-label"><span id="doc-group-label">Sélectionner au moins un '
            'document <span class="text-secondary">(obligatoire)</span></span>' + "".join(rows) + "</div>")


@app.get("/candidature/nouvelle/{siret}")
def formulaire(siret: str, request: Request, job: str = "", jobLabel: str = ""):  # noqa: N803 - nom du vrai site
    if not ETAT["connecte"]:
        return RedirectResponse("/login?retour=" + quote(str(request.url.path) + "?" + request.url.query))
    scenario = ETAT["scenarios"].get(siret)
    if siret not in COMPANIES or scenario == "erreur":
        return RedirectResponse("/errors/404")
    if scenario == "inattendue":
        return _page("Maintenance", "<section><h1>Service momentanément indisponible</h1>"
                                    "<p>Merci de réessayer plus tard.</p></section>")
    redirection = ""
    if scenario in ("deja_envoyee", "brouillon"):
        with _LOCK:
            ETAT["candidatures"].append({"id": len(ETAT["candidatures"]) + 1, "siret": siret, "etat": "sent" if
                                         scenario == "deja_envoyee" else "draft", "message": "Ancien message", "documents": []})
            cible = ("/candidature/recapitulatif/" if scenario == "deja_envoyee" else "/candidature/editer/") + str(len(ETAT["candidatures"]))
        redirection = f"setTimeout(() => location.replace({json.dumps(cible)}), 500);"
    captcha = ('<div class="g-recaptcha"><iframe title="reCAPTCHA" src="/faux-captcha" width="304" height="78" '
               'style="border:1px solid #d1d5db;border-radius:4px"></iframe></div>') if scenario == "captcha" else ""
    defaut = (f"Bonjour,\n\n    Votre entreprise suscite tout mon intérêt, c'est pourquoi je me permets de vous transmettre "
              f"ma candidature spontanée. Je vous prie de trouver mon dossier en pièce jointe.\n\n  Cordialement,\n\n  {UTILISATEUR}.")
    contenu = f"""<section><h1><strong>Débuter</strong> votre candidature spontanée<br>
      <span class="h2"><strong>Pour le métier de : </strong><span class="text-secondary">{escape(jobLabel)}</span>
      dans l'entreprise {escape(_nom(siret))}</span></h1></section>
      <section><h2><strong>Les documents que je souhaite joindre à ma candidature</strong> (étape 1/2)</h2>
      <div class="alert">Vous pouvez ajouter jusqu'à 3 fichiers maximum. Les types de fichiers acceptés sont : pdf, doc, docx,
      png, odt et rtf.</div><div id="select-document-error"></div>
      <app-document-selection>{_documents_html()}</app-document-selection>
      <app-motivation-message><h2 style="margin-top:28px"><strong>Ma lettre de motivation</strong> (étape 2/2)</h2>
      <div id="motivation-error"></div>
      <textarea id="message-textarea" name="message-textarea" placeholder="Votre message..." maxlength="1500">{escape(defaut)}</textarea>
      <div class="compteur">0/1500 caractères</div></app-motivation-message>{captcha}</section>
      <section class="boutons"><button id="save-application" class="btn">Enregistrer et finir plus tard</button>
      <button id="send-application" class="btn btn-primary">Continuer</button></section>"""
    script = NETTOYAGE_JS + CLIC_JS + redirection + f"""
      const zone = document.querySelector('#message-textarea'), compteur = document.querySelector('.compteur');
      const compter = () => {{ compteur.textContent = zone.value.length + '/1500 caractères'; }};
      zone.addEventListener('input', () => {{ const v = nettoyer(zone.value); if (v !== zone.value) zone.value = v; compter(); }});
      zone.addEventListener('blur', () => {{ const v = zone.value.replace(/[ \\t]+$/gm, '').replace(/\\n{{3,}}/g, '\\n\\n');
        if (v !== zone.value) zone.value = v; }});
      compter();
      document.querySelectorAll('input[type=checkbox]').forEach((cb) => cb.addEventListener('change', () => {{
        cb.closest('article').classList.toggle('coche', cb.checked); }}));
      document.querySelector('#send-application').addEventListener('click', async () => {{
        const docs = [...document.querySelectorAll('app-document-selection input:checked')].map((cb) =>
          document.querySelector('label[for="' + cb.id + '"] .font-extrabold').innerText);
        document.querySelector('#select-document-error').innerHTML = docs.length ? '' :
          '<div class="alert error">Aucun document sélectionné. Veuillez en sélectionner au moins un.</div>';
        document.querySelector('#motivation-error').innerHTML = zone.value ? '' :
          '<div class="alert error">Vous devez obligatoirement accompagner votre candidature par un message adressé à l\\'employeur.</div>';
        if (!docs.length || !zone.value) return;
        const r = await fetch('/api/candidatures', {{ method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ siret: {json.dumps(siret)}, job: {json.dumps(job)}, msg: zone.value, documents: docs }}) }});
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
        candidature = {"id": len(ETAT["candidatures"]) + 1, "siret": data["siret"], "etat": "draft",
                       "message": data["msg"], "documents": data["documents"], "job": data.get("job")}
        ETAT["candidatures"].append(candidature)
    return {"id": candidature["id"]}


def _candidature(id_: int) -> dict | None:
    return next((c for c in ETAT["candidatures"] if c["id"] == id_), None)


@app.get("/candidature/recapitulatif/{id_}")
def recapitulatif(id_: int):
    c = _candidature(id_)
    if c is None:
        return RedirectResponse("/errors/404")
    company = COMPANIES.get(c["siret"], {})
    bouton = ('<section class="boutons"><button id="send-application" class="btn btn-primary"> Envoyer la candidature '
              "</button></section>") if c["etat"] == "draft" else ""
    contenu = f"""<section><h1><strong>Synthèse </strong>de votre candidature spontanée</h1></section><section>
      <h2><strong>Vérifier les <span class="text-secondary">informations ci-dessous</span></strong> avant d'envoyer votre candidature</h2>
      <h3>Vos coordonnées</h3><dl><dt>Nom</dt><dd>{UTILISATEUR}</dd><dt>E-mail</dt><dd>camille.exemple@example.com</dd></dl>
      <h3>L'entreprise</h3><dl><dt>Nom de l'entreprise :</dt><dd>{escape(_nom(c['siret']))}</dd>
      <dt>Lieu :</dt><dd>{escape(company.get('city', 'Inconnu'))}</dd></dl>
      <h3>Votre candidature spontanée</h3><dl><dt>Pièces jointes :</dt><dd>{''.join(f'<span class="tag">{escape(d)}</span>' for d in c['documents'])}</dd></dl>
      <textarea readonly>{escape(c['message'])}</textarea></section>{bouton}"""
    script = CLIC_JS + f"""const b = document.querySelector('#send-application');
      if (b) b.addEventListener('click', async () => {{ await fetch('/api/candidatures/{id_}/envoi', {{ method: 'POST' }});
        location.href = '/candidature/envoyee'; }});"""
    return _page("Synthèse de votre candidature spontanée", contenu, script)


@app.get("/candidature/editer/{id_}")
def editer(id_: int):
    return _page("Modifier votre candidature", "<section><h1>Modifier votre candidature spontanée</h1>"
                                               "<p class='alert'>Vous avez déjà débuté une candidature pour cette entreprise.</p></section>")


@app.post("/api/candidatures/{id_}/envoi")
def envoyer(id_: int):
    with _LOCK:
        c = _candidature(id_)
        if c is None:
            return JSONResponse({"detail": "inconnue"}, status_code=404)
        c["etat"], c["envoyee_le"], ETAT["clic"] = "sent", time.time(), None
    return {"ok": True}


@app.get("/candidature/envoyee")
def envoyee():
    return _page("Candidature Envoyée", """<section><h1><strong>Candidature </strong>envoyée</h1>
      <h3><strong>Félicitation, votre candidature est envoyée !</strong></h3>
      <p><strong>Votre candidature a bien été envoyée le 31 oct. 2026.</strong><br>Afin d'optimiser votre chance, il est
      recommandé d'attendre entre 7 et 10 jours avant de relancer l'employeur.</p></section>""")


@app.get("/errors/{code}")
def erreur(code: str):
    return _page("Erreur", f"<section><h1>Erreur {escape(code)}</h1><p>Page introuvable.</p></section>")


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
    return {"connecte": ETAT["connecte"], "candidatures": ETAT["candidatures"]}


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
