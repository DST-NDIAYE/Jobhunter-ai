# JobHunter AI

Application locale pour préparer ses candidatures :

- **Nouvelle candidature** : collez une offre ; l'application produit un CV d'une page adapté à l'offre
  (PDF), une lettre de motivation vérifiée et un rapport (points forts, manques, informations).
- **Offres** : recherche France Travail (France entière ou une ville), offres classées selon le CV maître.
- **Spontanées** : chaque jour, au plus 10 entreprises Data / ML / IA trouvées par La Bonne Boîte et joignables par
  son formulaire, un message issu de votre modèle de texte, et un robot assisté qui prépare les formulaires.
- **Suivi** : candidatures, statuts et relance conseillée sept jours après l'envoi (dix pour une spontanée).
- **Paramètres** : état de France Travail, de La Bonne Boîte, du modèle de langage et de Gmail, sans aucun secret ;
  réglages du robot.

**Rien n'est envoyé automatiquement** : ni e-mail, ni formulaire. C'est vous qui postulez, puis vous cliquez
sur « Marquer comme postulé ». Seule exception, que vous choisissez : l'envoi automatique du robot des candidatures
spontanées, désactivé par défaut et plafonné (voir plus bas).

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
| `FRANCE_TRAVAIL_CLIENT_ID`, `FRANCE_TRAVAIL_CLIENT_SECRET` | API Offres d'emploi v2, et La Bonne Boîte v2 (même application) |
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

## Candidatures spontanées (La Bonne Boîte)

### Accès à l'API, à activer une fois

Les entreprises viennent de l'API officielle **La Bonne Boîte v2** de France Travail, avec la même application que
les offres (`FRANCE_TRAVAIL_CLIENT_ID` et `FRANCE_TRAVAIL_CLIENT_SECRET`). Sur francetravail.io, ouvrez « Mes
applications », choisissez votre application et ajoutez l'API « La Bonne Boîte v2 » (scope `api_labonneboitev2`).
France Travail peut demander une validation. Ensuite, Paramètres → La Bonne Boîte → « Vérifier l'accès ».

### Lot du jour

- Métiers : codes ROME `metiers_spontanees` de `search_profile.json` (data science, IA, data engineering, data
  analyse) ; France entière, ou 30 km autour d'une ville.
- Seulement les entreprises joignables par le formulaire du site (champ `email` de l'API à `yes`).
- Au plus 10 entreprises par jour ; un seul lot par jour, qui reste le même toute la journée.
- Jamais une entreprise déjà proposée dans un lot, ni une entreprise contactée ou tentée par l'ancien module.
- L'historique garde toutes les entreprises proposées, envoyées ou non (table `spontanees` de la base).
- Pour chaque entreprise : « Copier le message », « Ouvrir l'entreprise » (fiche La Bonne Boîte), « Marquer comme
  envoyée » (relance conseillée dix jours après).

Le message ne vient **pas** du modèle de langage : il vient de `modele_message_spontane.txt`, créé au premier usage
depuis `modele_message_spontane.exemple.txt`. Modifiez-le librement : champs `{entreprise}`, `{ville}`,
`{secteur}`, et ajoutez-y votre signature. Ce fichier n'est pas versionné. L'application signale un champ inconnu, un
lien (souvent refusé par France Travail), un message de plus de 1 500 caractères, et les caractères que le
formulaire du site retire (emojis, %, #…).

### Robot assisté

« Lancer le robot » ouvre une fenêtre de navigateur visible :

1. vous vous connectez vous-même avec France Travail : aucun mot de passe dans l'application, et la session
   disparaît avec la fenêtre (navigateur neuf, rien n'est enregistré) ;
2. pour chaque entreprise du lot, le robot ouvre le formulaire, coche votre CV déjà téléversé sur La Bonne Boîte,
   colle le message, puis **s'arrête avant l'envoi** ;
3. vous relisez, puis cliquez vous-même sur « Continuer » et « Envoyer la candidature » ; le robot détecte la
   confirmation (sinon « J'ai envoyé cette candidature »), ou vous choisissez « Passer ».

« Essai sur une entreprise » remplit un seul formulaire et n'envoie jamais rien. Le robot fait des pauses entre les
actions, prend une capture d'écran à chaque étape, et s'arrête immédiatement sur un captcha (il n'en résout jamais)
ou sur une page inattendue. Le journal et les captures s'affichent dans l'écran Spontanées et restent dans
`output/robot/<date-heure>/` (`journal.txt`, images PNG). Si plusieurs documents sont présents sur La Bonne Boîte,
indiquez le nom du CV à joindre dans Paramètres.

**Envoi automatique** (Paramètres) : désactivé par défaut. Activé, le robot clique lui-même sur « Continuer », vérifie
la synthèse (message identique), envoie, puis exige la page de confirmation. Plafond : au plus 10 envois par jour
(réglable de 1 à 10) ; les envois que vous marquez vous-même comptent aussi.

## Mode test (aucun quota consommé)

```bash
JOBHUNTER_FAKE_LLM=1 JOBHUNTER_FAKE_FT=1 \
CV_MASTER_PATH=tests/fixtures/cv_master_fictif.yaml DATABASE_PATH=/tmp/demo.db \
JOBHUNTER_OUTPUT_DIR=/tmp/demo-output JOBHUNTER_CACHE_DIR=/tmp/demo-cache \
.venv/bin/python run_web.py
```

- `JOBHUNTER_FAKE_LLM=1` remplace le modèle par un faux modèle local, déterministe ;
- `JOBHUNTER_FAKE_FT=1` renvoie des offres et des entreprises La Bonne Boîte fictives ;
- les autres variables isolent le CV, la base, les sorties et le cache ;
- `JOBHUNTER_MODELE_SPONTANE` isole le modèle de message, et `JOBHUNTER_LBB_SITE=http://127.0.0.1:8766` envoie le
  robot vers le faux site La Bonne Boîte des tests (`python tests/faux_site_lbb.py 8766`) ; seule une adresse locale
  est acceptée. En mode test, le navigateur du robot ne peut joindre que la machine locale.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -t .
```

Les tests utilisent le faux modèle, un CV maître fictif et des dossiers temporaires : ils ne font aucun appel
réseau et ne touchent ni à la vraie base ni à `output/`. Les tests du robot le lancent, sans fenêtre, contre le faux
site La Bonne Boîte local (entreprises et candidat fictifs) : essai, mode assisté, captcha, page inattendue,
plusieurs documents, envoi automatique plafonné, arrêt demandé.

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
| `app/spontanees.py` | Candidatures spontanées : API La Bonne Boîte v2, lot du jour, modèle de message, historique, réglages |
| `app/robot_spontanees.py` | Robot assisté (Playwright, fenêtre visible), lancé dans un processus séparé |
| `app/api_spontanees.py` | Routes des candidatures spontanées et du robot |
| `app/opportunities.py` | Opportunités La Bonne Boîte de l'ancien module, en lecture seule |
| `app/test_mode.py` | Faux modèle, offres et entreprises fictives |
| `tests/faux_site_lbb.py` | Faux site La Bonne Boîte pour tester le robot |
| `frontend/index.html` | Interface (Vue, sans étape de compilation) |
