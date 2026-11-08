# JobHunter AI

Application locale pour préparer ses candidatures :

- **Nouvelle candidature** : collez une offre ; l'application produit un CV d'une page adapté à l'offre
  (PDF), une lettre de motivation vérifiée et un rapport (points forts, manques, informations).
- **Offres** : recherche France Travail (France entière ou une ville), offres classées selon le CV maître.
- **Campagne** : candidatures spontanées en nombre sur La Bonne Boîte (CV déjà téléversé, message général issu de
  votre modèle de texte), envoyées par un robot dans sa fenêtre après un premier passage contrôlé : vous choisissez
  combien en envoyer.
- **Suivi** : candidatures, statuts et relance conseillée sept jours après l'envoi (dix pour une spontanée de
  l'ancien lot du jour ; la campagne a son propre historique).
- **Paramètres** : état de France Travail, de La Bonne Boîte, du modèle de langage et de Gmail, sans aucun secret.

**Aucun e-mail n'est envoyé**, et aucune candidature aux offres : c'est vous qui postulez, puis vous cliquez sur
« Marquer comme postulé ». Seule exception, que vous lancez vous-même : le robot de la campagne La Bonne Boîte
(voir plus bas), plafonné, qui s'arrête au moindre doute.

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

## Campagne La Bonne Boîte

### Accès à l'API, à activer une fois

Les entreprises viennent de l'API officielle **La Bonne Boîte v2** de France Travail, avec la même application que
les offres (`FRANCE_TRAVAIL_CLIENT_ID` et `FRANCE_TRAVAIL_CLIENT_SECRET`). Sur francetravail.io, ouvrez « Mes
applications », choisissez votre application et ajoutez l'API « La Bonne Boîte v2 » (scope `api_labonneboitev2`).
France Travail peut demander une validation. Ensuite : écran Campagne → « Vérifier l'accès à l'API ».

### Cibles

- Phase A (par défaut) : 29 métiers data, IA et informatique ; phase B (désactivée par défaut) : 24 métiers voisins.
  Codes ROME dans `app/campagne.py`, vérifiés dans le référentiel officiel (bouton « Vérifier les codes »).
- France entière, toutes les pages de résultats ; Île-de-France d'abord (75, 92, 93, 94, 91, 78, 95, 77), puis
  département par département ; potentiel d'embauche décroissant ; dédoublonnage par SIRET.
- Seulement les entreprises joignables par le formulaire du site. Exclues : toute entreprise déjà dans la base
  (ancien module, opportunités, lot du jour, campagne) et celles que le site marque « déjà candidaté ».
- Recherche autour de 14 villes d'Île-de-France, des 232 principales villes des autres départements et de 139 petites
  villes éloignées (`app/villes_campagne.py`) : 99,5 % de la population est à moins de 30 km d'un centre.
- L'inventaire tourne en arrière-plan (2 appels par seconde au plus) et reprend où il s'est arrêté. Un appel en
  erreur passagère (HTTP 5xx, 429, délai, réseau) est réessayé après 10 s, puis après 1 min ; ensuite, arrêt.
- Villes ou phase B ajoutées : « Lancer l'inventaire » fait seulement les recherches nouvelles. Inventaire à jour :
  « Nouvel inventaire » refait toutes les recherches (la liste de La Bonne Boîte change avec le temps) ; les
  entreprises déjà en base ne sont jamais reprises ni modifiées, seules les nouvelles sont ajoutées.

### Message

`modele_message_spontane.txt` (non versionné, exemple : `modele_message_spontane.exemple.txt`), un seul champ :
`{entreprise}`. Le nom est nettoyé (« UBER EATS FRANCE SAS » devient « Uber Eats France » : casse corrigée, sigles de
4 lettres ou moins gardés, forme juridique retirée) ; vide, de plus de 50 caractères, avec un chiffre ou un
« & » (modifié par La Bonne Boîte dans la lettre), il devient « vous ». Pas de « & » ni de lien dans le modèle
(écrivez « et »). Emojis, % et # sont retirés ; 1 500 caractères au plus. Si `modele_message_spontane_B.txt` existe,
A et B alternent, et l'historique note la version. Jamais de modèle de langage pour ces candidatures.

### Premier passage (obligatoire, dans l'ordre)

1. Accès à l'API, inventaire et plan (métiers, départements, entreprises éligibles), liste des métiers validée.
2. Essai sans envoi sur une entreprise de votre choix : jusqu'à la Synthèse, capture de la page entière, boutons listés.
3. Calibrage : vous cliquez vous-même sur le bouton d'envoi ; le robot observe la page qui suit et propose la règle de
   reconnaissance (bouton, adresse, texte), que vous validez.
4. Cinq envois automatiques devant vous, à comparer aux accusés de réception.
5. La campagne.

### Envoi d'une candidature

Une à la fois, dans un seul onglet : formulaire, CV désigné dans les réglages coché seul (nom exact, sinon nom
contenu ; ambigu ou introuvable : arrêt ; liste de vos documents attendue jusqu'à 30 s, formulaire rechargé une fois,
toujours vide : arrêt sans alerte, l'entreprise reste à faire), message collé à la place du message par défaut puis relu, compteur vérifié,
« Continuer », Synthèse vérifiée (entreprise, pièce jointe, lettre ; vos coordonnées ne sont jamais lues). L'entreprise
est marquée « tentée » avant le clic d'envoi ; la page de confirmation doit correspondre à la règle validée.
Statuts : envoyée, incertaine (jamais de nouvel essai ; contrôle sur « Gérer vos candidatures » en fin de session),
erreur, ignorée (sans e-mail, déjà candidaté, formulaire inactif, introuvable sur le site).

### Lancement et sécurités

- Vous choisissez le nombre de candidatures à envoyer (1 à 1000), puis « Démarrer » : le robot les enchaîne sans pause
  et s'arrête une fois ce nombre envoyé (confirmé), ou quand il n'y a plus d'entreprise à faire. Pas de plafond du
  jour ni de plage horaire. Le Mac ne se met pas en veille pendant l'exécution (fermer le capot l'y met quand même).
- À la fin, tout est déjà enregistré ; la fenêtre du robot se ferme toute seule 1 min après une campagne (10 min
  après un essai ou un calibrage, le temps de relire la Synthèse). Vous pouvez la fermer avant.
- Pages de résultats retenues pour la journée, par métier et département : le robot va droit à la page de chaque
  entreprise. La fiche est toujours relue juste avant le formulaire. Départements 01 à 09 : la recherche du site les
  attend sans zéro (« 1 » pour l'Ain ; « 01 » n'y trouve rien).
- Le robot ne s'arrête pas pour une entreprise. En échec (page non reconnue, écart, documents absents, erreur du
  site…), elle est mise de côté et il passe à la suivante. Rien de créé sur le site : elle redevient « à faire » ;
  brouillon : « brouillon ». Elle est reprise lors d'une prochaine exécution, une seule fois si l'échec venait d'elle
  (2e échec, ou écart : « erreur »). Envoi tenté sans confirmation : « incertaine », jamais reprise, contrôlée en fin
  d'exécution dans « Gérer vos candidatures ».
- « Continuer » sans effet (comme VIVERIS le 07/11) : un 2e clic (un brouillon n'est jamais envoyé), puis mise de côté.
- Fiche introuvable dans les résultats : l'entreprise est ignorée (« introuvable sur le site »). 10 de suite : la
  recherche du site est peut-être en panne ; elles restent à faire et le robot fait une pause.
- Page de résultats en erreur 5xx ou trop lente : deux nouveaux essais (après 30 s, puis 2 min). Jamais de nouvel
  essai d'un envoi.
- 3 échecs de suite, ou 2 erreurs 5xx de suite : pause (10 min, puis 20, puis 30), puis reprise. Après 5 pauses sans
  aucun succès (environ 2 h de panne) : arrêt sans alerte, relance possible.
- Code 429 (trop de requêtes) : pause de 15 min, puis 30 min, puis 1 h, et reprise ; une 4e fois de suite : arrêt.
- Captcha : jamais résolu ni contourné par le robot. Il attend 20 min que vous le résolviez dans sa fenêtre, puis
  reprend.
- Arrêt pour la journée seulement si le site s'y oppose : captcha non résolu, message de limite ou de blocage, code
  403, code 429 répété.
- Session perdue : le robot attend votre reconnexion (20 min au plus), puis reprend. Aucun mot de passe ni session
  conservés (navigateur neuf à chaque lancement).
- État écrit à chaque étape : après un plantage, une entreprise tentée n'est jamais reprise.
- Le navigateur du robot ne joint que La Bonne Boîte et France Travail ; tout le reste est bloqué et noté au journal.
- Journal lisible, sans texte de page ni coordonnées (code HTTP et premiers mots des réponses en erreur, pages
  chargées) ; captures des erreurs seulement en campagne, de chaque étape pour l'essai et le calibrage
  (`output/campagne/<date-heure>/`). Elles peuvent montrer vos coordonnées : `output/` n'est pas versionné.

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
  est acceptée. En mode test, le navigateur du robot ne peut joindre que la machine locale ;
  `JOBHUNTER_ROBOT_ACCELERE=1000` divise les pauses du robot (tests uniquement).

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -t .
```

Les tests utilisent le faux modèle, un CV maître fictif et des dossiers temporaires : ils ne font aucun appel
réseau et ne touchent ni à la vraie base ni à `output/`. Les tests du robot le lancent, sans fenêtre et en temps
accéléré, contre le faux site La Bonne Boîte local (entreprises et candidat fictifs) : essai, calibrage, cinq envois,
campagne (nombre choisi, versions A et B, captures), captcha, limite, 429, page non reconnue, envoi
incertain, 3 erreurs de suite, session perdue, plantage sans doublon, pause, arrêt, CV ambigu.

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
| `app/spontanees.py` | Client de l'API La Bonne Boîte v2, nettoyage, anciennes candidatures spontanées (suivi) |
| `app/campagne.py` | Campagne : cibles, nom et message, nombre à envoyer, alertes, statuts, historique, export |
| `app/robot_campagne.py` | Robot de la campagne (Playwright, fenêtre visible), lancé dans un processus séparé |
| `app/api_campagne.py` | Routes de la campagne et du robot |
| `app/opportunities.py` | Opportunités La Bonne Boîte de l'ancien module, en lecture seule |
| `app/test_mode.py` | Faux modèle, offres et entreprises fictives |
| `tests/faux_site_lbb.py` | Faux site La Bonne Boîte pour tester le robot |
| `frontend/index.html` | Interface (Vue, sans étape de compilation) |
