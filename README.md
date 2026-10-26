# JobHunter AI — V1

Une API Python qui recherche des offres France Travail, les enregistre en SQLite
sans doublons et les classe par similarité lexicale avec un CV PDF local.
Le matching utilise TF-IDF et la similarité cosinus, gratuitement, sans LLM.

Une interface Streamlit permet également d'effectuer ces recherches sans Swagger.

Cette V1 ne réalise aucun envoi de candidature, e-mail ou formulaire web.

## Architecture

```text
jobhunter-ai/
├── app/
│   ├── __init__.py
│   ├── config.py          # Variables .env et chemins locaux
│   ├── main.py            # Routes FastAPI et orchestration
│   ├── models.py          # Schémas JSON et documentation Swagger
│   ├── ui.py              # Entrée Streamlit et navigation
│   ├── interface/         # Pages, cartes, filtres et design system
│   ├── ui_helpers.py      # Filtres et présentation des offres
│   ├── global_search.py   # Pagination multi-métiers, bilan et classement global
│   ├── france_travail.py  # OAuth2, recherche et normalisation des offres
│   ├── database.py        # SQLite et déduplication
│   ├── applications.py    # Statuts, notes, dates et statistiques des candidatures
│   ├── applications_ui.py # Fiches détaillées et vue Mes candidatures
│   └── matching.py        # Lecture PDF et similarité TF-IDF/cosinus
├── tests/                 # Tests hors ligne, données temporaires
├── scripts/validate_global_live.py # Validation volontaire avec la vraie API
├── cv/                    # CV personnel, exclu de Git
├── data/jobhunter.db       # Créé automatiquement au démarrage, exclu de Git
├── search_profile.json    # Métiers et filtres proposés par défaut
├── .env                   # Configuration personnelle, exclue de Git
├── .env.example           # Exemple sans secret
└── requirements.txt
```

`/jobs` : France Travail → normalisation → SQLite → JSON.

`/jobs/match` : extraction du CV → recherche et sauvegarde des offres → comparaison
CV/description → classement décroissant → JSON. Le texte du CV n'est ni envoyé
à France Travail ni enregistré dans SQLite. Le score est recalculé à chaque appel.

## Installation

Prérequis : Python **3.11 ou supérieur** et un accès Internet pour installer les
dépendances et interroger France Travail. SQLite est inclus dans Python.

Depuis un terminal macOS/Linux :

```bash
cd /Users/dst/Desktop/CODE/jobhunter-ai
python3 --version
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Sur une autre machine, adaptez uniquement le chemin du projet. Sous Windows,
activez l'environnement avec `.venv\Scripts\Activate.ps1` dans PowerShell.

Streamlit fournit l'interface locale. FastAPI expose l'API, Uvicorn la sert, HTTPX effectue les appels HTTP,
python-dotenv charge la configuration, PyMuPDF lit le PDF et scikit-learn calcule
la similarité. Pandas prépare le tableau d'historique de l'interface.

## Configuration

Le `.env` local est déjà créé. Pour une nouvelle installation seulement :

```bash
cp -n .env.example .env
```

Créez une application sur le [portail développeur France Travail](https://francetravail.io/produits-partages/catalogue/offres-emploi)
et activez son accès à l'API Offres d'emploi. Renseignez les identifiants obtenus
dans `.env` :

```dotenv
FRANCE_TRAVAIL_CLIENT_ID=
FRANCE_TRAVAIL_CLIENT_SECRET=
FRANCE_TRAVAIL_SCOPE="api_offresdemploiv2 o2dsoffre"
CV_PATH="cv/mon_cv.pdf"
DATABASE_PATH="data/jobhunter.db"
HTTP_TIMEOUT=20
```

Les champs d'identifiants ci-dessus sont volontairement vides. Ne mettez jamais
vos secrets dans le code, les commandes partagées ou Git. Le serveur peut démarrer
sans identifiants ; les recherches signalent alors la configuration manquante.

Déposez un PDF contenant du texte sélectionnable dans `cv/mon_cv.pdf`, ou adaptez
`CV_PATH`. **Dans votre dossier actuel, `.env` pointe déjà sur le PDF existant**,
dont le nom est conservé. Les espaces dans le chemin sont acceptés entre guillemets.
Un PDF scanné sans couche texte nécessite un OCR préalable ; la V1 n'effectue pas
d'OCR. Les PDF chiffrés nécessitant un mot de passe sont refusés.

Les chemins relatifs sont résolus depuis la racine du projet, indépendamment du
répertoire de lancement. Les variables système sont prioritaires sur `.env`.
Redémarrez le serveur après toute modification de configuration.

`search_profile.json` rassemble les intitulés Data/ML/AI recherchés, les niveaux
junior/entry-level/graduate, l'expérience cible de 0 à 3 ans et les termes à
exclure. L'interface lit `future_excluded_title_terms` pour initialiser le filtre
des titres (Senior, Lead, Principal, Manager, Head of). Ces termes restent
modifiables dans l'interface. `preferred_titles` initialise les 14 métiers de la
recherche globale. Les préférences d'expérience restent préparatoires : aucune
offre n'est exclue selon le nombre d'années demandé.
Les routes FastAPI n'appliquent pas ce filtre : il agit uniquement sur l'affichage Streamlit.

## Lancer l'interface Streamlit

Depuis la racine du projet :

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m streamlit run app/ui.py --server.address 127.0.0.1 --server.port 8501
```

Ouvrez <http://127.0.0.1:8501>. Streamlit réutilise directement les fonctions
du backend ; il peut fonctionner sans serveur FastAPI démarré. Les deux
applications lisent le même `.env`, le même CV et la même base SQLite.

La navigation latérale regroupe six espaces : **Dashboard**, **Recherche**,
**Mes candidatures**, **Préparations**, **Historique** et **Paramètres**.

Les sept compteurs du Dashboard sont des boutons cliquables sur toute leur
surface, même à zéro, et utilisables au clavier. Ils ouvrent le groupe annoncé
avec son filtre : toutes les offres, premières détections des sept derniers
jours (pas les dates de publication), étapes du pipeline ou préparations.
Les raccourcis de compteurs réinitialisent les filtres de la vue cible afin que
la liste corresponde au chiffre affiché. Une navigation ordinaire par la sidebar
conserve les filtres, la pagination et les résultats dans la session.

**Recherche → Offres enregistrées** permet de parcourir le catalogue sans appel
API. Les meilleures opportunités ouvrent leur fiche ; les activités récentes
ouvrent l'offre, la candidature ou la préparation concernée, sans génération
supplémentaire. Le bouton **Retour** des fiches indique la page d'origine.
Les actions secondaires des cartes sont réunies dans le menu **•••**.
La navigation est centralisée dans `app/interface/navigation.py` avec
`navigate_to()`. Les retours sont internes à Streamlit : l'URL n'identifie pas
chaque filtre et une nouvelle session de navigateur peut réinitialiser ces valeurs.

- Le **Dashboard** affiche les compteurs, les nouvelles offres des sept derniers
  jours, les cinq meilleurs scores disponibles et les dernières évolutions.
  Les scores du catalogue sont calculés localement au besoin avec le matching
  existant, gardés en session et invalidés si le CV change. Cela ne modifie pas
  les scores conservés avec les candidatures.
- La **Recherche rapide** démarre avec Data Scientist, France entière et 20 offres.
  La limite est dans Filtres avancés. Les métiers de la **Recherche globale**
  sont des tags : saisir un métier puis Entrée pour l'ajouter, × pour le retirer.
- Les listes disposent d'une recherche textuelle, d'un tri, de filtres par score,
  contrat, localisation, statut, date et entreprise, et de pages de **20 cartes**.
  L'historique couvre toutes les offres enregistrées. Une valeur de score absente
  reste distinguée d'un score nul. Aucun filtre ne supprime des offres de SQLite.
- **Détails** ouvre une fenêtre modale avec trois onglets : À propos du poste,
  Analyse CV / Offre, Notes & suivi. Les statuts et les notes restent modifiables.
- **Mes candidatures** présente les étapes du pipeline et les cartes suivies.
  **Préparations** donne accès aux textes conservés, dans un espace à deux zones :
  contexte de l'offre à gauche, message et lettre éditables à droite.
- **Paramètres** affiche l'état de France Travail et du CV sans exposer de secret.
  Les métiers et filtres par défaut peuvent être enregistrés dans
  `search_profile.json`. La recherche globale conserve tous les niveaux par défaut.

Le thème clair et son accent vert sont définis dans `.streamlit/config.toml`.
Le CSS partagé se trouve dans `app/interface/styles.css` ; les pages sont dans
`app/interface/`, tandis que `app/ui.py` reste un point d'entrée léger.
Les composants récents utilisés nécessitent **Streamlit 1.64 ou supérieur**.
Les colonnes se replient sur les petites fenêtres ; le thème clair est la
référence visuelle de cette version. Les fenêtres modales, les widgets et leur
comportement responsive restent ceux de Streamlit.

Les identifiants restent lus dans `.env`. « France Travail configuré » signifie
qu'ils sont présents ; leur validité est vérifiée lors d'une recherche. Le CV
est vérifié localement, sans afficher son contenu complet. Une configuration
absente désactive les recherches ; le suivi et les contenus enregistrés restent
accessibles. Redémarrez Streamlit après une modification du `.env` ou une mise
à jour de l'interface.

La refonte UI a été vérifiée par imports, démarrage et parcours dans le navigateur,
sans exécuter la suite complète. Les anciens tests de widgets décrivent l'ancienne
mise en page et devront être adaptés avant une prochaine validation UI automatisée.
Les tests et modules métier n'ont pas été réécrits pour cette refonte.

## Recherche globale sur toute la France

Sélectionnez **Recherche globale** dans Streamlit. La zone par défaut est
**France entière**, sans Paris imposé. Le pays France est transmis sous le code
`99100`, vérifié dans le référentiel `/referentiel/pays` de France Travail.
Une localisation précise peut être ajoutée (commune ou code INSEE).

La liste des 14 métiers est modifiable sous forme de tags, jusqu'à 50 métiers.
Les doublons de mots-clés, les entrées vides et les différences de casse sont
normalisés. La limite globale proposée est 50, 100, 200 ou 500 **offres uniques**,
200 par défaut. Les offres demandant 5 ou 6 ans d'expérience sont conservées.
Le filtre optionnel de titres Senior/Lead/etc. est désactivé par défaut dans ce mode.

La recherche passe à tour de rôle sur chaque métier, puis continue leurs pages
restantes jusqu'à la limite globale ou à l'épuisement des résultats. Le compteur
« Offres récupérées » inclut toutes les occurrences reçues. Les doublons sont
fusionnés par `(source, external_id)` et tous les mots-clés ayant trouvé une offre
sont conservés. Le CV est extrait une seule fois par recherche ; chaque offre
unique reçoit un score, puis l'ensemble est classé par score décroissant.

Le bilan indique les métiers interrogés (y compris ceux en erreur), les offres
reçues, les doublons retirés, les offres uniques et les erreurs par métier. La
progression affiche le métier courant et les compteurs en cours. Le détail par
métier distingue les offres reçues du total annoncé par l'API.

Les résultats peuvent être triés par score, date de publication décroissante ou
entreprise. Les valeurs absentes sont placées à la fin pour la date et l'entreprise.
Les fiches détaillées incluent salaire, expérience, identifiant France Travail et mots-clés
d'origine. L'affichage est paginé par 20 cartes ; changer de page, de tri ou de
filtre ne relance aucun appel API et ne retire aucune offre de SQLite.

### Pagination, limites et erreurs

- Les requêtes utilisent `range=debut-fin`, avec au plus 150 offres par page.
  `Content-Range` détermine la position suivante et le total ; sans cet en-tête,
  une page pleine est suivie d'une nouvelle requête bornée.
- Une réponse vide, HTTP 204 ou HTTP 416 arrête la pagination du métier.
  Une page répétée ou un index sans progression provoque un arrêt explicite.
- La recherche est bornée à 500 offres uniques, 200 requêtes de pages et une
  **fenêtre de prudence applicative de 1 000 offres par métier**. Cette dernière
  borne ne prétend pas représenter la limite maximale du fournisseur.
- Les appels sont séquentiels avec au moins 250 ms entre les requêtes de pages
  d'une recherche globale. Ce rythme n'est pas un quota garanti par le fournisseur
  et ne coordonne pas plusieurs recherches lancées simultanément.
- Une erreur sur un métier laisse les autres continuer et conserve les pages
  déjà reçues. Sur HTTP 429, `Retry-After` est respecté avant les appels suivants.
  Au-delà de 30 secondes d'attente demandée, la recherche s'arrête avec un bilan
  partiel afin de ne pas insister sur une API limitée par son quota.
- Atteindre la limite choisie **ne signifie pas avoir récupéré toutes les offres**.
  Le classement compare uniquement les offres reçues. Les pages de l'API portent
  sur un catalogue évolutif, sans garantie d'instantané entre deux requêtes.

La recherche globale est accessible via Streamlit et
`app.global_search.search_global`. Les routes FastAPI existantes conservent leur
contrat de recherche simple (une page, 150 offres maximum).

### Historique des détections

La migration SQLite s'effectue à l'initialisation sans suppression de données.
`first_seen_at` reprend `created_at` pour les anciennes offres ; `last_seen_at`
est actualisé à chaque nouvelle observation. Pour les anciennes lignes, la date
d'enregistrement sert de valeur initiale faute d'historique plus détaillé.
La colonne JSON `keywords` cumule les mots-clés trouvés par la recherche globale.
Les identifiants et la date de première détection restent stables entre recherches.
Comme en V1, le contenu d'une offre déjà connue garde sa première version en base ;
la recherche affichée utilise la version actuelle reçue de l'API.

## Suivre les candidatures

Dans Streamlit, la navigation latérale propose notamment **Recherche** et
**Mes candidatures**. Une offre nouvellement récupérée reste sans candidature
jusqu'à ce que vous lui attribuiez un statut. Les neuf statuts disponibles sont :
Nouvelle, À étudier, À postuler, Candidature préparée, Prête à envoyer, Candidature envoyée,
Entretien, Refus et Archivée.

Sur chaque carte, utilisez **À étudier**, **À postuler** ou **Ignorer / Archiver**.
Ces actions écrivent uniquement dans SQLite : elles ne relancent pas la recherche.
Les cartes affichent le statut actuel. Archiver conserve l'offre et ses notes.

Les cases **Sélectionner** permettent les changements de statut groupés.
**Tout sélectionner** couvre toutes les pages des résultats filtrés, pas seulement
les 20 cartes visibles. **À étudier** et **À postuler** dans la barre de sélection
s'appliquent uniquement aux offres sélectionnées qui correspondent encore aux
filtres actuels. Une nouvelle recherche remet la sélection à zéro.

**Détails** ouvre une fiche dans une fenêtre modale depuis les cartes. Elle
présente la description complète, les compétences, le score, les mots-clés et le
lien de l'offre. Vous pouvez y choisir n'importe quel statut et enregistrer vos
**Notes personnelles**. Les notes seules ne créent pas de candidature. Les offres
déjà présentes en base peuvent aussi être ouvertes depuis les cartes de l'historique,
sans appeler France Travail.

**Mes candidatures** affiche uniquement les offres avec un statut attribué,
y compris Nouvelle et Archivée. Les filtres de statut peuvent être combinés ;
une sélection vide affiche tous les statuts. Les cartes contiennent le poste,
l'entreprise, la localisation, le score CV, la date de publication, le statut,
la dernière modification du statut et un lien cliquable. La première détection
reste disponible dans la fiche. Cliquez sur **Détails** pour modifier
son statut ou ses notes. Cette vue fonctionne sans identifiants API et même si le
CV n'est plus accessible.

Le score et le nom du CV sont enregistrés lors de l'attribution d'un statut.
Si l'offre provient de l'historique sans score, le matching local est calculé
avec le CV disponible. En l'absence d'un CV lisible, le statut peut être enregistré
sans score. Ces valeurs sont des instantanés : elles ne sont pas recalculées
automatiquement lorsque le CV change. Aucun contenu complet du CV n'est stocké.

**Préparer ma candidature** génère et enregistre un premier brouillon avec le mode sélectionné :
message court, lettre de motivation, analyse CV/offre et instantané de l'offre,
du nom du CV et du score. Un CV PDF lisible est nécessaire à la génération.
Rouvrir une préparation existante conserve les textes, même si le CV a été déplacé.

Les deux textes sont éditables. **Enregistrer** conserve les modifications dans SQLite.
**Régénérer** remplace les champs affichés par une nouvelle proposition, sans écraser
le contenu en base avant enregistrement. Les modifications non enregistrées dans
les champs sont alors remplacées. **Marquer comme prête** enregistre les deux champs
et attribue **Prête à envoyer**. Une modification ultérieure enregistrée remet un
contenu prêt au statut **Candidature préparée**. Aucun message n'est envoyé.
Le statut **Candidature envoyée** reste une déclaration manuelle d'un envoi effectué ailleurs.

L'analyse est lexicale et non exhaustive : elle montre les compétences mentionnées,
les termes communs, quelques extraits exacts du CV et les termes non détectés dans le CV.
Une occurrence ne démontre pas la maîtrise d'une compétence, et une absence n'est
pas une preuve de lacune. Le catalogue `SKILLS` dans `app/application_generator.py`
est modifiable. Les templates n'inventent ni diplôme, ni responsabilité, ni durée
d'expérience. Relisez et personnalisez ces brouillons avant utilisation ; si le nom
ne peut pas être identifié prudemment, la signature contient `[Votre nom]`.

### Génération locale ou IA

Le choix **Génération → Automatique / IA / Locale** figure dans la fiche et l'éditeur.
Automatique respecte `LLM_PROVIDER`, notamment `local` ; Locale interdit tout appel LLM.
IA tente le fournisseur configuré, sinon revient aux templates.

Dans `.env` (jamais dans l'interface) :

```dotenv
LLM_PROVIDER=local
LLM_MODEL=
LLM_API_KEY=
```

Providers pris en charge : `local`, `mistral`, `openai`, `auto`. Pour Mistral,
utilisez par exemple `LLM_PROVIDER=mistral` et `LLM_MODEL=ministral-8b-latest` ;
pour OpenAI, `LLM_PROVIDER=openai` et `LLM_MODEL=gpt-4o-mini`, avec une clé du fournisseur
correspondant. Le modèle doit être accessible à votre compte et accepter les sorties JSON structurées.
En mode `auto`, indiquez de préférence `mistral/ministral-8b-latest` ou
`openai/gpt-4o-mini` dans `LLM_MODEL` ; le préfixe est retiré avant l'appel.
Les noms commençant par `gpt-`, `o1`, `o3`, `o4`, `mistral-`, `ministral-`,
`open-mistral-` ou `magistral-` sont aussi reconnus. Une configuration ambiguë ou
incomplète reste locale ; la clé n'est jamais utilisée pour deviner le fournisseur.
Une seule configuration distante est active à la fois. Redémarrez Streamlit après
modification de `.env`. La clé LLM est lue exclusivement dans ce fichier, masquée
dans la représentation des paramètres, et n'est pas stockée dans SQLite.

Un clic de génération déclenche au plus une requête HTTP, sans retry automatique.
L'ouverture d'une fiche ou d'un contenu existant ne génère rien. Les échecs HTTP,
quota, timeout, refus et réponses invalides déclenchent un repli local avec une
information discrète. L'attente réseau utilise `HTTP_TIMEOUT` (20 secondes par défaut,
plafonnée à 60 secondes pour le LLM ; connexion au plus 10 secondes).

Seuls l'offre sélectionnée (titre, entreprise, lieu, description, expérience et libellés
de compétences), le texte extrait du CV, le score et les compétences communes/absentes
sont transmis au provider. Aucun historique, note personnelle ni identifiant SQLite.
Le secret sert uniquement à l'authentification HTTP. Aucun corps de réponse ou erreur
brute n'est journalisé par l'adaptateur. Les sources dépassant 100 000 caractères restent
locales. Le mode local ne transmet rien.

Le schéma Pydantic impose `fit_summary`, `matching_skills`, `relevant_cv_points`,
`missing_skills`, `short_message` et `cover_letter`. Les compétences sont comparées à
l'analyse locale et les extraits du CV vérifiés dans le texte source. Le prompt interdit
explicitement toute invention et toute instruction intégrée dans l'offre ou le CV.
Ces contrôles ne constituent pas une preuve sémantique de chaque phrase libre :
relisez les textes, notamment les durées, responsabilités et résultats. Le catalogue
lexical est non exhaustif ; le garde-fou conservateur peut revenir au mode local
si le texte mentionne une technologie absente du CV, même comme besoin de l'offre.

**Régénérer** refait l'analyse, le message et la lettre dans un brouillon de session.
Les textes et métadonnées SQLite précédents restent intacts jusqu'à **Enregistrer**
ou **Marquer comme prête**. Le provider et le modèle affichés correspondent au contenu,
pas au prochain choix de génération. Un brouillon non enregistré peut être perdu lors
d'un rechargement complet. Les retouches manuelles conservent la provenance de la génération.

Implémentation : `app/llm/` utilise `httpx` et `pydantic`, déjà présents. Aucun SDK
supplémentaire requis. Formats vérifiés dans les documentations officielles
[OpenAI](https://developers.openai.com/api/docs/guides/structured-outputs) et
[Mistral](https://docs.mistral.ai/studio/conversations/structured-output/custom).

Vérification ciblée : `.venv/bin/python -m unittest tests.test_llm -v`.
Pour vérifier l'interface : ouvrez une offre, sélectionnez Locale, cliquez sur
Préparer ma candidature, modifiez un texte et enregistrez. Régénérez puis rouvrez la
préparation : le brouillon et son avertissement restent distincts de la version SQLite.
Après configuration d'une clé, sélectionnez IA et cliquez une seule fois sur Régénérer.

Le tableau de bord compte toutes les offres SQLite et les candidatures dans
chaque état principal, indépendamment des filtres de recherche. Les mises à jour
par lot sont transactionnelles : si une offre du lot n'existe plus, aucun statut
du lot n'est changé.

### Migration du suivi

Aucune nouvelle table : la table `jobs` reçoit les colonnes suivantes au démarrage :

| Colonne | Rôle |
| --- | --- |
| `application_status` | Statut manuel, `NULL` pour les offres non suivies |
| `personal_notes` | Notes personnelles, texte vide initialement |
| `status_updated_at` | Dernier changement effectif de statut, UTC |
| `application_prepared_at` | Première attribution du statut préparée, UTC |
| `application_sent_at` | Première déclaration manuelle du statut envoyée, UTC |
| `application_match_score` | Score CV conservé avec le suivi, éventuellement absent |
| `application_cv_name` | Nom du CV associé au score |
| `application_message` | Message éditable enregistré |
| `application_letter` | Lettre éditable enregistrée |
| `application_analysis` | Analyse structurée JSON, avec extraits du CV |
| `application_generated_at` | Date de la génération enregistrée, UTC |
| `application_content_updated_at` | Dernière modification des textes, UTC |
| `application_generation_method` | Méthode utilisée : `templates` ou `llm` |
| `application_generation_provider` | Provider effectif : `local`, `mistral`, `openai` ; NULL pour anciens contenus |
| `application_generation_model` | Modèle utilisé, vide en local |
| `application_preparation_context` | Instantané JSON : offre, CV, empreinte du texte du CV et score |

La migration est additive et peut être rejouée. Une nouvelle recherche ne remet
pas à zéro les statuts, les notes ou ces dates. Réattribuer le même statut ne
change pas sa date de modification ; enregistrer une note ne modifie pas cette date.
Les dates de première préparation et de premier envoi déclaré sont conservées
lors des changements de statut suivants. Il n'y a pas encore de journal complet
des transitions ni de gestion de plusieurs envois pour une même offre.

### Validation avec la vraie API

```bash
.venv/bin/python -m scripts.validate_global_live --limit 500
```

Ce contrôle est volontairement séparé des tests hors ligne : il utilise le vrai
CV et les identifiants du `.env`, actionne le formulaire Streamlit via AppTest,
appelle France Travail et enregistre les offres réelles dans SQLite. Une sauvegarde
de la base est créée avant l'exécution. Le bilan sans secrets est écrit dans
`data/global-search-validation.json`.

Validation du 23 septembre 2026 : **14 métiers interrogés, 567 occurrences reçues,
67 doublons supprimés, 500 offres uniques, 20 requêtes de pages, aucune erreur API**.
La limite de 500 a été atteinte ; `AI/ML Engineer` et `NLP Engineer` n'ont renvoyé
aucune offre lors de ce test. Les résultats réels peuvent varier avec le catalogue.

## Lancer FastAPI

Depuis la racine du projet, environnement virtuel activé :

```bash
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

- État : <http://127.0.0.1:8000/>
- Swagger interactif : <http://127.0.0.1:8000/docs>
- Schéma OpenAPI : <http://127.0.0.1:8000/openapi.json>

La table SQLite est créée au démarrage. Le serveur de développement est destiné
à un usage local. Arrêtez-le avec `Ctrl+C`.

## Rechercher des offres : `GET /jobs`

Tous les paramètres sont facultatifs :

| Paramètre | Rôle | Valeur par défaut |
| --- | --- | --- |
| `keyword` | Métier ou mots-clés, par exemple `Data Scientist` | Aucun filtre |
| `location` | Nom exact de commune ou code INSEE | Aucun filtre |
| `contract_type` | Code France Travail, par exemple `CDI` ou `CDD` | Aucun filtre |
| `limit` | Nombre maximal d'offres, entre 1 et 150 | 50 |

```bash
curl -G 'http://127.0.0.1:8000/jobs' \
  --data-urlencode 'keyword=Data Scientist' \
  --data-urlencode 'location=Paris' \
  --data-urlencode 'contract_type=CDI' \
  --data-urlencode 'limit=20'
```

Un nom de commune est résolu via l'[API publique de découpage administratif](https://geo.api.gouv.fr/decoupage-administratif/communes).
En cas de nom inconnu ou ambigu, une erreur explicite vous invite à préciser
un code INSEE. **Un code INSEE n'est pas un code postal.** La V1 ne prend pas en
charge la saisie libre d'une région ou d'un pays.

La réponse est une liste JSON d'offres contenant `external_id`, `title`,
`company`, `location`, `contract_type`, `description`, `publication_date`,
`salary`, `experience`, `skills`, `url` et `source`. Les informations absentes
restent `null` ; les compétences absentes donnent `[]`. Aucune entreprise,
rémunération ou URL n'est inventée. Le salaire conserve la structure renvoyée
par l'API. S'il n'y a aucun résultat, la réponse est `[]` avec le statut HTTP 200.

Chaque recherche sauvegarde les offres. La contrainte unique sur
`(source, external_id)` évite les doublons, y compris entre deux recherches.
Une offre déjà sauvegardée conserve sa première version ; la réponse HTTP,
elle, utilise les données de la recherche actuelle. Les scores ne sont pas stockés.

## Classer selon le CV : `GET /jobs/match`

```bash
curl -G 'http://127.0.0.1:8000/jobs/match' \
  --data-urlencode 'keyword=Machine Learning Engineer' \
  --data-urlencode 'location=Paris' \
  --data-urlencode 'limit=20'
```

Les mêmes champs sont retournés avec `match_score`, entre 0 et 100, du plus
élevé au plus faible. Le classement porte **uniquement sur les offres récupérées**
dans la limite demandée, pas sur toutes les offres disponibles ni l'historique SQLite.

TF-IDF pondère les mots du CV et de chaque description ; la similarité cosinus
mesure leur proximité. Une description absente ou vide reçoit 0. Ce score
lexical est indicatif : il ne représente pas une probabilité d'embauche,
ne comprend pas les synonymes et ne vérifie pas le niveau d'expérience.

## Consulter les offres enregistrées en Python

```bash
python -c 'from app.database import get_jobs; print(get_jobs(limit=5))'
```

Le module expose `init_db`, `add_job`, `job_exists` et `get_jobs`. Les lignes
enregistrées possèdent un `id` SQLite interne et un `external_id` France Travail,
ainsi que `created_at`, `first_seen_at`, `last_seen_at` en UTC et les mots-clés
d'origine `keywords`. Le module `app.applications` expose `get_offer`,
`get_applications`, `update_status`, `save_notes` et `application_stats` pour le suivi manuel.

## Vérifier le projet

```bash
python -m compileall -q app tests
python -m unittest discover -s tests -v
python -m pip check
```

Les tests utilisent des réponses HTTP simulées, des PDF temporaires et une base
SQLite temporaire. Ils n'ont besoin ni de clés API, ni d'accès réseau, ni de votre
CV personnel. Pour vérifier le CV configuré sans afficher son contenu :

```bash
python -c 'from app.config import get_settings; from app.matching import extract_cv_text; print(len(extract_cv_text(get_settings().cv_path)), "caractères extraits")'
```

Après lancement d'Uvicorn dans un premier terminal, dans un second :

```bash
curl 'http://127.0.0.1:8000/'
curl 'http://127.0.0.1:8000/jobs?keyword=Data%20Scientist&location=Paris&limit=20'
curl 'http://127.0.0.1:8000/jobs/match?keyword=Data%20Scientist&location=Paris&limit=20'
```

Les deux dernières commandes nécessitent des identifiants France Travail valides.

## Erreurs courantes

| Statut | Cause et action |
| --- | --- |
| 422 | Paramètre invalide, commune inconnue/ambiguë ou CV inexploitable : consultez `detail`. |
| 502 | Réponse inattendue ou erreur du fournisseur : réessayez et vérifiez l'accès à l'API. |
| 503 | Identifiants absents/refusés, accès API ou quota indisponible, ou SQLite inaccessible : vérifiez la configuration. |
| 504 | Délai HTTP dépassé : réessayez ou ajustez `HTTP_TIMEOUT` (maximum 120 secondes). |

Les réponses d'erreur ne divulguent ni clés, ni token, ni corps brut des erreurs
d'authentification. Le token est conservé temporairement en mémoire et renouvelé
à expiration. Aucun navigateur automatisé ni contournement des protections des
plateformes n'est utilisé.

Documentation de référence : [France Travail](https://francetravail.io/produits-partages/catalogue/offres-emploi/documentation),
[FastAPI](https://fastapi.tiangolo.com/),
[PyMuPDF](https://pymupdf.readthedocs.io/en/latest/document.html),
[TF-IDF scikit-learn](https://scikit-learn.org/stable/modules/generated/sklearn.feature_extraction.text.TfidfVectorizer.html).

### Gmail : création de brouillons uniquement

La page **Envois** présente les candidatures prêtes, les brouillons créés et les erreurs.
Dans une préparation, la section **Envoi** permet d'enregistrer l'email du recruteur
et l'objet. **Créer le brouillon Gmail** prend exclusivement `application_message`
enregistré et le PDF de `CV_PATH` ; les retouches non enregistrées et la lettre ne
sont pas joints. Le CV est vérifié (présence, PDF lisible non chiffré, au plus 15 Mo).
Aucun appel de génération, aucun envoi, aucune opération en masse.

#### Configuration Google, une seule fois

1. Ouvrir [Google Cloud Console](https://console.cloud.google.com/), créer ou sélectionner
   un projet **JobHunter AI**.
2. Dans **API et services → Bibliothèque**, rechercher **Gmail API** et cliquer sur **Activer**.
3. Dans **Google Auth platform → Branding**, commencer la configuration : nom de
   l'application, email d'assistance et email de contact. Pour un compte Gmail personnel,
   choisir l'audience **External / Externe** et garder le projet en mode **Testing / Test**.
4. Dans **Audience → Test users / Utilisateurs de test**, ajouter l'adresse Gmail à connecter.
5. Dans **Data Access / Accès aux données → Add or Remove Scopes**, ajouter exactement :
   `https://www.googleapis.com/auth/gmail.compose` et
   `https://www.googleapis.com/auth/userinfo.email`, puis enregistrer.
6. Dans **Clients → Create client / Créer un client**, choisir **Desktop app / Application
   de bureau**, lui donner un nom puis le créer. Télécharger son fichier **JSON OAuth**.
   Ne pas créer de clé API, de compte de service ni de mot de passe d'application.
7. Placer ce JSON sous `jobhunter-ai/secrets/client_secret.json`. Aucun autre fichier
   n'est à télécharger depuis Google. Le token sera créé localement après autorisation.

Ces étapes suivent la [configuration officielle du consentement Google](https://developers.google.com/workspace/guides/configure-oauth-consent)
et le [guide Gmail pour une application de bureau](https://developers.google.com/workspace/gmail/api/quickstart/python).
L'autorisation `gmail.compose` permet aussi l'envoi côté Google : Google ne propose
pas de droit REST réservé uniquement à la création des brouillons. JobHunter expose
uniquement `users.drafts.create` ; aucune méthode d'envoi n'est implémentée.

Dans `.env` :

```dotenv
GMAIL_CLIENT_SECRET_FILE=secrets/client_secret.json
GMAIL_TOKEN_FILE=tokens/gmail_token.json
```

Les deux chemins sont relatifs au projet (les chemins absolus sont aussi acceptés).
Les dossiers `secrets/`, `tokens/` et les noms courants de fichiers OAuth sont exclus
par `.gitignore`. Le token est écrit atomiquement avec permissions `0600` ; il contient
les éléments nécessaires à la reconnexion et ne doit jamais être partagé. Aucun secret
OAuth n'est enregistré dans SQLite ou affiché dans l'interface.

#### Connexion et premier brouillon

```bash
cd /Users/dst/Desktop/CODE/jobhunter-ai
.venv/bin/python -m streamlit run app/ui.py
```

Ouvrir http://localhost:8501, puis **Paramètres → Gmail → Connecter Gmail**.
Cliquer sur **Autoriser JobHunter dans Google**, choisir le compte ajouté aux utilisateurs
de test et valider les droits demandés. Revenir dans JobHunter : le compte connecté
s'affiche, et `tokens/gmail_token.json` est créé automatiquement. Le lien expire après
3 minutes ; une annulation ou expiration permet de recommencer. Le navigateur et
Streamlit doivent tourner sur la même machine : callback OAuth sur `127.0.0.1`, port
éphémère. Cette intégration vise une utilisation personnelle locale, pas un hébergement
Streamlit public ou multi-utilisateur.

Dans **Préparations**, enregistrer le message puis le marquer **Prête à envoyer**.
Renseigner **Email du recruteur** et **Objet**, cliquer sur **Enregistrer les coordonnées**,
vérifier le nom du PDF affiché et le **Message enregistré utilisé**, puis cliquer sur
**Créer le brouillon Gmail**. La page **Envois** permet la même action individuellement.
Le statut de candidature reste **Prête à envoyer**, jamais **Candidature envoyée**.

Le lien Gmail ouvre le dossier des brouillons, pas un permalien inventé. Sélectionner
dans Gmail le compte de création indiqué par JobHunter. Un brouillon existant bloque
une seconde création ; aucune recréation ou synchronisation automatique des retouches.
Si la réponse réseau est perdue après la requête, la réservation reste bloquée pour
éviter un doublon. Vérifier alors le dossier Gmail ; cette première version ne réconcilie
pas automatiquement une création incertaine et ne relance jamais la requête.
Une erreur d'accès ou un token révoqué demande une reconnexion dans Paramètres.

#### Stockage et validation ciblée Gmail

Migration additive de `jobs` uniquement, sans modifier les offres ou les candidatures :

| Colonne | Usage |
| --- | --- |
| `application_channel` | `UNKNOWN` par défaut, `EMAIL` lorsque le contact est renseigné |
| `contact_email` | Adresse du recruteur enregistrée |
| `email_subject` | Objet enregistré et modifiable |
| `email_status` | Dernier état : `NOT_READY`, `READY`, `DRAFT_CREATED`, `ERROR` |
| `gmail_draft_id` | Identifiant du brouillon créé |
| `gmail_message_id` | Identifiant du message dans le brouillon |
| `draft_created_at` | Date UTC de création confirmée |
| `send_error` | Erreur utilisateur expurgée, malgré le nom aucune fonction d'envoi |
| `gmail_account_email` | Compte auquel appartient le brouillon |
| `gmail_draft_pending` | Réservation persistante contre les doubles clics/créations incertaines |

La disponibilité affichée est recalculée depuis le statut actuel et le message
sauvegardé ; les préconditions sont revérifiées juste avant la requête. Pas de colonne
`email_sent_at`, de statut `SENT` ni de compteur de retry à cette étape.
Les logs locaux conservent les catégories d'erreurs et codes HTTP, jamais les tokens,
le corps du message ou les réponses OAuth brutes.

Dépendances nouvelles : `google-auth`, `google-auth-oauthlib` (OAuth officiel) ; le REST
Gmail utilise `httpx` déjà installé. Vérification sans compte Google ni réseau :

```bash
.venv/bin/python -m unittest tests.test_gmail_drafts -v
```

### Opportunités spontanées et bouton Envoyer

**Recherche → Sources** propose les offres d'emploi, les candidatures spontanées
et une recherche combinée. Les métiers proviennent de `search_profile.json` et restent
modifiables. France entière est sélectionnée par défaut. La limite (50/100/200/500)
s'applique par source ; une recherche s'arrête quand cette limite est atteinte.
Les entreprises conservées restent consultables après redémarrage et figurent dans
Mes candidatures dès qu'un statut leur est attribué. Le potentiel d'embauche La Bonne
Boîte n'est pas un score de matching CV et n'atteste pas une offre publiée.

L'intégration suit le [contrat OpenAPI officiel La Bonne Boîte v2](https://francetravail.io/api-peio/v2/api/360/openapi) :
`GET /partenaire/labonneboite/v2/recherche`, `job`, `location` facultative,
`page` à partir de 1 et `page_size` au plus 100. Plafond applicatif : 10 pages par métier,
50 métiers, pause entre requêtes, arrêt en cas de pagination répétée. SIRET prioritaire
pour la déduplication, identifiant API sinon. Les champs absents restent vides.

**Accès distinct à activer** dans votre application partenaire France Travail :
La Bonne Boîte v2, scope `api_labonneboitev2`. Les identifiants restent dans `.env` ;
le scope Offres d'emploi existant n'est pas remplacé. Le test réel du 29 septembre 2026
retourne `400 invalid_scope` lors de l'authentification : **0 entreprise récupérée,
recherche réelle non validée**. Cela n'indique pas une absence de résultats.

**Paramètres → Profil candidat** enregistre `candidate_profile.json` localement
(ignoré par Git, permissions 0600). Les champs inconnus restent vides. Le CV de ce
profil, s'il est renseigné, remplace `CV_PATH` ; sinon le CV existant est conservé.

**Un seul essai de candidature :**

1. Ouvrir une offre existante, ou une entreprise une fois l'accès La Bonne Boîte activé.
2. Cliquer sur Préparer, relire/modifier le message et enregistrer « Prête à envoyer ».
3. Vérifier le CV et, pour un email, le destinataire et l'objet. Une correction manuelle
   de l'adresse prime sur les coordonnées automatiques, y compris une suppression.
4. Cliquer sur **Envoyer** : le routeur choisit EMAIL, LABONNEBOITE, FRANCE_TRAVAIL,
   COMPANY_SITE, ATS, MANUAL ou UNKNOWN à partir des coordonnées réelles.
5. Gmail crée/réutilise **un brouillon**, avec le texte enregistré et le CV. Un brouillon
   déjà créé n'est pas synchronisé avec les retouches ultérieures : le vérifier dans Gmail.
   Les autres canaux ouvrent la page de candidature ; terminer manuellement.
6. Consulter **Historique → Journal des tentatives de candidature**. Le statut devient
   Intervention requise ou Échec, jamais Envoyée par simple ouverture d'une page.

La recherche de contact public est déclenchée au clic seulement si le site officiel
est connu et qu'aucun email ou lien de candidature explicite n'est disponible. Trois
pages maximum, respect de robots.txt, erreurs/protections bloquantes, cache SQLite
24 heures. Une seule adresse publiée peut être retenue automatiquement ; plusieurs
adresses exigent un choix manuel. Aucun email n'est déduit du nom de l'entreprise.

**Navigateur :** sans configuration, la page s'ouvre dans le navigateur habituel et
le remplissage reste manuel. Le socle Playwright peut se connecter à une session
Chromium existante via `BROWSER_CDP_URL=http://127.0.0.1:PORT` (CDP local activé par
l'utilisateur, ne pas exposer le port). Aucun navigateur de débogage n'est lancé
silencieusement, aucun profil navigateur ni mot de passe France Travail n'est stocké.
Sur un formulaire simple et unique, il remplit les champs reconnus du profil, joint
le CV et insère le message enregistré. Il ne clique jamais sur la soumission finale.
Connexion, CAPTCHA, redirection d'hôte ou formulaire ambigu renvoient
`MANUAL_REVIEW_REQUIRED`. Le socle a été vérifié par simulation ; aucun parcours ATS
ou La Bonne Boîte connecté n'est certifié de bout en bout.

**Migration additive :** nouvelles tables `opportunities` (identité/SIRET, entreprise,
secteur/NAF/ROME, métier, localisation, site, source, potentiel, mots-clés, dates et
champs communs de candidature/Gmail) et `application_events` (source, identifiant,
canal, événement, détail, URL, date). Dans `jobs` : `application_url`, `website`,
`application_attempted_at`, `application_error`, `contact_email_source`,
`contact_email_manual`, `contact_checked_at`. Migration automatique au démarrage.

Validation limitée à 10 tests ciblés (6 intégration/routage + 4 Gmail simulé), imports,
migration idempotente sur copie (734 lignes anciennes intégralement conservées),
écrans Streamlit et bouton Envoyer simulé via AppTest, démarrage réel et santé HTTP.
Aucune suite complète pytest ni candidature réelle n'a été lancée.

```bash
.venv/bin/python -m unittest tests.test_opportunity_routing tests.test_gmail_drafts -q
.venv/bin/python -m streamlit run app/ui.py
# Navigateur : http://localhost:8501
```
# Jobhunter-ai
