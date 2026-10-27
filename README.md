# JobHunter AI

Application locale pour préparer ses candidatures :

- **Nouvelle candidature** : collez une offre ; l'application produit un CV d'une page adapté à l'offre
  (PDF), une lettre de motivation vérifiée et un rapport (points forts, manques, informations).
- **Offres** : recherche France Travail (France entière ou une ville), offres classées selon le CV maître.
- **Suivi** : candidatures, statuts et relance conseillée sept jours après l'envoi.
- **Paramètres** : état de France Travail, du modèle de langage et de Gmail, sans aucun secret.

**Rien n'est envoyé automatiquement** : ni e-mail, ni formulaire. C'est vous qui postulez, puis vous cliquez
sur « Marquer comme postulé ».

## Installation

Python 3.11 ou supérieur.

```bash
cd jobhunter-ai
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium   # rendu des PDF
cp .env.example .env                              # puis renseignez .env
```

Le CV vient uniquement de `cv_master.yaml`, à la racine du projet ; ce fichier n'est pas versionné.

## Lancer l'application

```bash
cd jobhunter-ai && .venv/bin/python run_web.py
```

Ouvrez ensuite http://127.0.0.1:8000. Ctrl+C arrête le serveur.

Utilisez toujours le Python de `.venv` : `run_web.py` s'arrête avec un message clair s'il manque un module, ou si
le port 8000 est déjà pris par un ancien serveur. L'interface n'utilise aucune ressource externe : Vue 3.4.38 est
servi depuis `frontend/vendor/`.

## Configuration (`.env`)

Voir `.env.example`. En résumé :

| Variable | Rôle |
|---|---|
| `FRANCE_TRAVAIL_CLIENT_ID`, `FRANCE_TRAVAIL_CLIENT_SECRET` | API Offres d'emploi v2 |
| `LLM_PROVIDER`, `LLM_API_KEY`, `LLM_MODEL` | Analyse de l'offre et ordre du CV (un modèle « lite » suffit) |
| `LLM_FALLBACK_MODEL` | Secours : après 3 essais en surcharge (503) ou dès un quota atteint (429) |
| `LETTER_MODEL`, `LETTER_FALLBACK_MODEL` | Lettre de motivation ; jamais un modèle « lite » |
| `DATABASE_PATH` | Base SQLite (par défaut `data/jobhunter.db`) |
| `HTTP_TIMEOUT` | Attente réseau maximale, en secondes |

`search_profile.json` fournit les valeurs par défaut de la recherche : postes, et mots qui masquent une offre
(Senior, Lead…).

## Ce que fait le modèle de langage, et ce qu'il ne fait pas

- **CV** : le modèle analyse l'offre, puis renvoie seulement un **ordre** d'identifiants. Tout le texte du CV
  vient de `cv_master.yaml`, et le code valide la sélection.
- **Lettre** : chaque phrase cite les éléments du master qui la prouvent. Le code vérifie la lettre et la refuse,
  ou la rend en « brouillon » avec les points à corriger.
- **Confidentialité** : le modèle ne reçoit jamais votre nom, votre e-mail, votre téléphone ni votre adresse.
  Les coordonnées ne sont ajoutées que dans les fichiers générés.
- **Cache** (`data/cache/`) : l'analyse, la sélection et la lettre sont gardées en mémoire.
  - « Préparer » une offre déjà vue ne rappelle pas le modèle, et ne régénère jamais une lettre déjà rédigée.
  - La clé de la lettre combine l'empreinte de l'offre, la sélection du CV et le modèle.
  - « Régénérer la lettre » fait rédiger une nouvelle lettre, qui remplace celle du cache.

Les fichiers générés (CV, lettre, aperçu) sont écrits dans `output/`.

## Mode test (aucun quota consommé)

```bash
JOBHUNTER_FAKE_LLM=1 JOBHUNTER_FAKE_FT=1 \
CV_MASTER_PATH=tests/fixtures/cv_master_fictif.yaml DATABASE_PATH=/tmp/demo.db \
JOBHUNTER_OUTPUT_DIR=/tmp/demo-output JOBHUNTER_CACHE_DIR=/tmp/demo-cache \
.venv/bin/python run_web.py
```

- `JOBHUNTER_FAKE_LLM=1` remplace le modèle par un faux modèle local, déterministe ;
- `JOBHUNTER_FAKE_FT=1` renvoie des offres France Travail fictives ;
- les autres variables isolent le CV, la base, les sorties et le cache.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -t .
```

Les tests utilisent le faux modèle, un CV maître fictif et des dossiers temporaires : ils ne font aucun appel
réseau et ne touchent ni à la vraie base ni à `output/`.

## Structure

| Fichier | Rôle |
|---|---|
| `app/main.py` | Application FastAPI : sert l'interface sur « / » et l'API sur `/api` (documentation sur `/docs`) |
| `app/api.py` | Routes : état, recherche, préparation, régénération de la lettre, fichiers, suivi |
| `app/cv_tailor.py` | Analyse de l'offre, ordre du CV, validation, rapport, cache |
| `app/cv_pdf.py` | CV en HTML puis PDF d'une page (Playwright), aperçu PNG |
| `app/cover_letter.py` | Lettre : rédaction, vérification phrase par phrase, cache, PDF et texte |
| `app/llm/` | Fournisseurs (Gemini, Mistral, OpenAI, Groq), nouveaux essais et modèle de secours |
| `app/france_travail.py` | Client de l'API France Travail |
| `app/matching.py` | Score TF-IDF / cosinus entre l'offre et le CV maître |
| `app/applications.py` | Statuts, suivi et relances |
| `app/opportunities.py` | Opportunités La Bonne Boîte existantes, en lecture seule |
| `app/test_mode.py` | Faux modèle et offres fictives |
| `frontend/index.html` | Interface (Vue, sans étape de compilation) |
