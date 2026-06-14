<!-- @format -->

# Plan D — visibilité des fichiers & regroupement (séries, redondances)

> **Statut : validé sur le principe (2026-06-11), à implémenter.** Ce document est le
> cahier des charges pour l'implémenteur. Relire « Décisions » (dont 2 points à confirmer)
> avant de coder. Une seule PR. Cocher la checklist au fur et à mesure.

## Problème observé (deux runs sandbox réels)

Les fichiers déposés **en vrac à la racine** de l'Inbox (« singletons ») produisent des
incohérences que les dossiers-ensembles (plan B) n'ont plus :

- **Deux relevés de compteur d'eau** quasi identiques → l'un posé nu dans
  `Personal/Administrative/Housing/`, l'autre nu dans `Personal/Administrative/` —
  jamais réunis, aucun dossier-série créé.
- **Diplômes OpenClassrooms** → incohérences Personal/Work selon le fichier.
- Aucun dossier-série (`Releves-eau`, `CAF`, …) n'est jamais créé pour les genres
  récurrents arrivant seuls.

## Diagnostic (vérifié dans le code — ne pas re-deviner)

1. **L'arbre fourni à l'IA ne contient QUE des noms de dossiers.**
   `taxonomy.existing_category_paths()` renvoie des chemins de dossiers ; aucun nom de
   FICHIER n'est jamais montré. L'IA ne peut donc pas détecter « un fichier semblable
   existe déjà là » → pas de détection de redondance possible.
2. **La règle dossier-série n'existe que dans le prompt de l'ORGANISEUR**
   (`ai_organize._build_organize_prompt`, la règle « Exception — a genuinely RECURRING
   kind … even from a single instance »). Or les **singletons racine sautent
   l'organiseur** (dans `pipeline.process_all_inbox_files`, l'appel `organize_set` est
   conditionné à `set_top` non vide). Donc pour un fichier racine, la règle n'existe
   nulle part → le 1er compteur ne crée pas de dossier-série, et le 2e n'a rien à
   rejoindre.
3. **Constat positif (ne PAS « réparer » ça, ça marche)** : l'arbre **est** relu en
   direct à chaque fichier (`existing_category_paths(paths.library_root)` appelé au
   moment de chaque analyse). Preuve en run réel : le diplôme OC `.png` a rejoint
   `Work/Education` créé quelques secondes avant par le `.pdf`. Aucun bug de
   rafraîchissement d'arbre.

## Décisions

**Validées par l'utilisateur (2026-06-11) :**

- **M1** — ajouter la règle dossier-série au prompt d'**analyse par-fichier** (elle ne
  vit aujourd'hui que chez l'organiseur).
- **M2** — après l'analyse d'un singleton, une **étape légère « confirmer ou
  regrouper »** : montrer à l'IA les **noms de fichiers** le long des 2-3 branches
  candidates (le `category_path` + les `alternatives` déjà renvoyés par l'analyse) pour
  détecter une redondance et proposer un dossier commun.
- **M3** — quand un fichier **déjà rangé** doit rejoindre le regroupement (« solution
  4 ») : le **déplacer** dans le dossier commun, laisser un **lien symbolique à son
  emplacement initial** (pointant vers le nouveau chemin), et **signaler** l'opération
  (alerte dans le résumé du run + journal d'actions).

**Confirmées par l'utilisateur (2026-06-11) :**

- [x] **Lien symbolique (M3)** : *déplacer le fichier existant + laisser un symlink à
  son ANCIEN emplacement, pointant vers le nouveau chemin.*
- [x] **Chaîne IA pour M2** : la chaîne **ANALYSIS** (`mistral-small`) — choix « pour
  tester » ; réévaluer (passage à ORGANIZE/medium) si les runs montrent des
  regroupements ratés. M2 lit donc sa chaîne via `task_chain_from_env("ANALYSIS")`,
  aucune nouvelle variable d'environnement à créer.

**Périmètre** : M2/M3 s'appliquent aux **singletons racine uniquement**. Les dossiers
déposés restent gouvernés par le plan B (organiseur, ensemble entier). Étendre M2 aux
ensembles = plus tard, si les runs le justifient.

## Modifications

### M1 — règle dossier-série dans le prompt d'analyse

- **Fichier** : `src/procrafiler/ai_analysis.py`, fonction `_build_analysis_prompt`,
  champ `category_path`.
- Ajouter (en anglais, aligné sur le libellé déjà présent chez l'organiseur) :
  une consigne du type *« If the document is of an obviously RECURRING kind (meter
  reading, bank statement, payslip, bill, tax notice…), propose a series subfolder
  under the right base (e.g. `…/Housing/Releves-eau`), even for a single instance —
  and REUSE that folder from the tree when it already exists. »*
- Effet attendu : le compteur n°1 **crée** `Housing/Releves-eau` dès son premier run ;
  le n°2 le **trouve par le nom** dans l'arbre (relu en direct) et s'y range — souvent
  sans même avoir besoin de M2.
- **Test** : le prompt d'analyse contient la règle (dans `tests/test_ai_analysis.py`,
  même style que les tests de prompt existants).

### M2 — étape « confirmer ou regrouper » (visibilité des noms de fichiers)

- **Nouveau module** `src/procrafiler/ai_grouping.py` (même squelette que
  `ai_organize` : dataclass résultat gelée, `_build_…_prompt` testable, chaîne par
  tâche, retry/failover, fallback propre).
  - `propose_grouping(document, candidate_branches, *, chain=None, …) -> GroupingResult`
    - `document` : résumé du nouveau fichier (name, summary, original_filename).
    - `candidate_branches` : `dict[path_str, list[str]]` — pour chaque dossier candidat,
      la liste des **noms de fichiers** existants (voir plafonds ci-dessous).
    - `GroupingResult` : `path: str | None` (destination confirmée ou nouveau dossier
      commun), `group_with: list[str]` (noms de fichiers existants à regrouper, possiblement
      vide), `provider/model/raw_output/used_fallback/reason`.
  - **Prompt** (anglais), points obligatoires :
    - on classe UN nouveau document ; voici les dossiers candidats **avec les noms des
      fichiers qu'ils contiennent** ;
    - confirmer UN chemin, OU proposer UN sous-dossier commun (série/affaire) si le
      nouveau fichier est manifestement du même genre/de la même affaire que des
      fichiers existants — auquel cas les **nommer** dans `group_with` ;
    - **date EN TÊTE** des noms de dossiers (jamais à la fin) ;
    - barre haute : ne regrouper que l'évident ; les noms de fichiers existants ont été
      générés par IA → **indices, pas vérité absolue** ;
    - JSON strict, pas d'autre clé.
  - **Plafonds (coût borné)** : ≤ 3 branches (le `category_path` + jusqu'à 2
    `alternatives` validées) ; ≤ 30 noms de fichiers par branche (les plus récents
    d'abord) ; ≤ ~2500 caractères de listing au total. Listing = fichiers sous le
    dossier candidat (récursif, profondeur ≤ 2), noms seuls, via le système de
    fichiers.
  - **Skip sans appel IA** (coût zéro) si : pas d'analyse, aucune branche candidate
    existante sur disque, ou toutes les branches vides.
- **Câblage pipeline** (`src/procrafiler/pipeline.py`, chemin singleton uniquement —
  dans la boucle `work_sets`, cas `set_top == ""`) : après `_route_from_analysis`
  (analyse non-None, route confirmée — ne PAS l'appliquer aux décisions pendantes ni au
  Manual_Review), appeler `propose_grouping` ; si `path` proposé et validé par
  `normalize_category_path` → router le nouveau fichier dedans ; sinon comportement
  actuel inchangé. Échec/fallback → comportement actuel inchangé.
- **Tests** : parsing JSON (omissions, index/clés invalides), contenu du prompt
  (noms de fichiers présents, date-en-tête, barre haute), skip-si-vide, et un test
  pipeline : deux « compteurs » singletons → le 2e déclenche le regroupement.

### M3 — regrouper un fichier existant : déplacer + symlink + alerte

- Quand `group_with` nomme des fichiers existants (match par **nom de fichier exact**
  parmi ceux listés dans les branches candidates — ne jamais deviner) :
  1. Retrouver le document au **catalogue** par `current_path`. Introuvable → alerte
     seule, pas de déplacement.
  2. **Garde-fous** : ne déplacer que des documents `status == "LIBRARY_STORED"` ;
     jamais un doc en décision pendante ; jamais un chemin hors `library_root`.
  3. Déplacer le fichier librairie vers le dossier commun (`_ensure_unique_path`),
     mettre à jour le **catalogue** (current_path/current_filename + `category_path`
     de la fiche `content_json`), déplacer la **copie miroir** (ré-introduire un helper
     type `_move_mirror_copy`, supprimé en PR #34 — récupérer le code d'origine via
     `git show 039a8ef^:src/procrafiler/pipeline.py` si utile).
  4. Créer un **symlink relatif** à l'ancien emplacement → nouveau chemin. Si
     `os.symlink` échoue (FS sans support) → log warning, continuer sans symlink.
  5. **Alerte / traçabilité** : événements action-log `library_file_regrouped`
     (path_before/path_after) + `symlink_left` ; ligne de progression
     `regrouped → <nouveau chemin>` ; compteur `regrouped` dans le résumé du batch.
- **Le symlink n'est PAS mirroré** (le miroir ne contient que des fichiers réels) et ne
  doit jamais être traité comme un document (exclure les symlinks de tout scan
  librairie présent et futur ; `_iter_inbox_files` ignore déjà les symlinks côté
  Inbox).
- **Tests** : regroupement même-run (les 2 compteurs finissent ensemble ; symlink
  présent à l'ancien emplacement et pointant juste ; miroir suit ; catalogue à jour ;
  `regrouped` compté) ; `group_with` avec nom inconnu → alerte sans crash ; échec
  symlink simulé → run continue.

## Checklist implémentation

- [x] Décisions confirmées par l'utilisateur (symlink = déplacer + lien à l'ancien emplacement ; chaîne M2 = ANALYSIS)
- [x] M1 : règle-série dans le prompt d'analyse + test (`test_prompt_has_series_folder_rule`)
- [x] M2 : module `ai_grouping` + tests unitaires (`tests/test_ai_grouping.py`, 12 tests)
- [x] M2 : câblage pipeline singletons + test pipeline
- [x] M3 : déplacement + symlink + miroir + catalogue + garde-fous
- [x] M3 : alertes (progress + action log `library_file_regrouped`/`symlink_left` + `regrouped` dans le résumé)
- [x] Suite complète verte (243 tests, `python -m unittest discover -s tests`)
- [x] CHANGELOG : une entrée
- [x] Ce fichier : checklist cochée

## Déviations par rapport au plan initial

- `_files_under` dans les tests existants utilisait `is_file()` sans exclure les symlinks ; corrigé pour exclure les symlinks (comportement correct : les symlinks sont des marqueurs M3, pas des documents).
- `test_batch_cli.test_process_all_batch_summary` : même correction (`is_file() and not is_symlink()`).
- Aucune déviation fonctionnelle ; toutes les règles du plan respectées.

## Révision post-run 3 (validée par l'utilisateur, 2026-06-12)

Le 3e run sandbox réel (librairie vide) a validé M1 et tout l'amont (plan B,
contexte, nommage), mais **M2/M3 a dé-rangé la librairie** : `mistral-small` a
inversé la sémantique (« rapatrier les semblables vers le chemin confirmé »,
même moins profond, au lieu de « creuser un sous-dossier-série ») ; il a arraché
les constats du dossier d'affaire Insurance (2×, chaînes de symlinks), aplati
Banking/Housing/Health vers Administrative nu, et écrasé la route-série des deux
compteurs (M1 correcte) vers Housing nu. Rien dans la mécanique ne l'en
empêchait. De plus, les symlinks laissés étaient TOUS parasites (librairie
partie de vide → aucun repère pré-run à préserver).

### L'invariant du run (consolidation d'architecture)

> **Un `run` ne peut qu'augmenter l'ordre de la librairie, jamais le défaire.**
> Il peut créer des dossiers, placer les nouveaux fichiers, et **approfondir**
> un fichier existant (le descendre dans un sous-dossier strictement plus
> spécifique de là où il est). Il ne peut **jamais** aplatir, remonter, ni
> croiser les branches. La « réorganisation » utile (sous-dossiers série/affaire
> qui attirent les fichiers apparentés) se produit ainsi naturellement au fil
> des runs, par approfondissement.

Décisions d'architecture associées (mêmes échanges) :

- **`reorganize` (commande globale) : SUPPRIMÉ de la roadmap.** L'Inbox est la
  seule porte par laquelle l'IA décide. Une passe corrective de fin de run est
  REJETÉE par défaut (c'est l'option A déguisée — on durcit les étapes du run,
  on ne répare pas derrière) ; réévaluable seulement si les runs réels restent
  insuffisants.
- **`rescan` (futur chantier) : secrétaire pur, automatique avant chaque run.**
  Fichier déposé à la main dans la librairie → LU INTÉGRALEMENT (fiche complète
  au catalogue, pour la recherche) + préfixe horodaté (nom de l'utilisateur
  intouché) ; fichier connu déplacé/renommé (sha256) → mise à jour du chemin,
  zéro IA ; disparu → signalé. L'IA *comprend*, ne *décide* pas.
- **Trou connu (échappatoire future)** : un fichier déjà catalogué ne peut pas
  repasser par l'Inbox (la dédup sha256 le jetterait en doublon). Si le besoin
  émerge : micro-commande `refile <chemin>` (dé-épingler du catalogue +
  ré-ingérer par le pipeline normal). Rien à construire maintenant.

### Corrections G1–G7 (cette PR) — « l'IA voit tout, mais ne peut que ranger »

- **G1 — Listing en chemins relatifs** : chaque branche liste ses fichiers en
  chemins relatifs à la branche (`Releves-eau/2026-01__Releve.pdf`), pas en noms
  à plat — l'IA voit OÙ vit chaque fichier (donc les dossiers-séries existants)
  et `group_with` cite des chemins non ambigus. Plafonds inchangés (≤3 branches,
  ≤30 fichiers, prof. ≤2, ~2500 car.) — la visibilité n'est PAS réduite
  (décision utilisateur : l'information le long des branches est la raison
  d'être du plan D).
- **G2 — Branche ancêtre incluse** : si la route de l'analyse n'existe pas
  encore sur disque (ex. `Housing/Releves-eau` proposé par M1), son ancêtre
  existant le plus proche (`Housing`) devient la branche candidate — c'est là
  que vivent les fichiers à regrouper.
- **G3 — Le `path` de M2 ne peut que creuser** : accepté seulement s'il est un
  STRICT descendant d'une branche candidate ; sinon ignoré (la route de
  l'analyse est conservée). Bloque l'écrasement des routes-série.
- **G4 — Fichier existant : approfondissement uniquement** : M3 ne déplace que
  vers un STRICT descendant du dossier ACTUEL du fichier. Bloque arrachages et
  aplatissements ; rend les cascades intra-run inoffensives.
- **G5 — Symlink = repère d'avant-run uniquement** : le run tient la liste des
  fichiers qu'il a placés ; un regroupement d'un fichier placé pendant CE run se
  fait SANS symlink (librairie vide → zéro symlink). Un symlink créé pendant le
  run est re-ciblé si son fichier bouge encore (jamais de lien pendouillant).
- **G6 — Chaîne M2 → ORGANIZE (`mistral-medium`)** : ce jugement mérite le même
  modèle que l'organiseur d'ensembles. Remplace le choix ANALYSIS « pour
  tester » (sa condition de réévaluation est remplie).
- **G7 — Prompt réécrit autour du contrat** : confirmer, OU proposer UN
  sous-dossier partagé PLUS PROFOND sous une branche candidate ; `group_with` =
  uniquement des chemins relatifs copiés du listing dont le dossier actuel est
  un ANCÊTRE du sous-dossier proposé (les fichiers descendent, jamais ne
  remontent ni ne changent de branche) ; un fichier déjà dans un sous-dossier
  bien nommé est déjà rangé → le laisser ; doute → confirmer, `group_with`
  vide. Le prompt DÉCRIT ce que G3/G4 IMPOSENT — un modèle qui désobéit est
  simplement ignoré.
- Matching `group_with` : chemin relatif exact sous une branche ; sinon match
  par nom de fichier (préfixe horodaté toléré) seulement s'il est UNIQUE —
  jamais deviner.

### Checklist révision

- [x] G1 listing chemins relatifs + G2 branche ancêtre
- [x] G3 verrou « creuser seulement » (nouveau fichier)
- [x] G4 verrou « descendant strict » (fichiers existants) + refus journalisé
- [x] G5 symlinks pré-run only + re-ciblage intra-run
- [x] G6 chaîne ORGANIZE + G7 prompt contrat + matching tolérant unique
- [x] Tests (G2–G5 pipeline, prompt/chaîne ai_grouping, existants ajustés)
- [x] Invariant inscrit dans la spec ; rescan/refile/fin-de-run consignés au backlog
- [x] CHANGELOG ; suite complète verte (250 tests)

### Affinements post-run 4 (validés 2026-06-13)

Le run 4 a validé l'architecture (compteurs réunis, regroupement intra-run propre,
zéro symlink, G3 visible en action). Trois finitions décidées :

- **Série vs affaire (dates)** : un dossier-SÉRIE n'est JAMAIS daté à son niveau
  (il est ouvert) ; une période dans une série = sous-dossier **millésime nu**
  (`Factures-electricite/2026/`), créé seulement quand il apporte quelque chose ;
  seule une AFFAIRE ponctuelle est datée directement, date en tête
  (`2025-08_Degats-eaux-cuisine/`). (Le run 4 avait produit `2026_Releves-eau`
  avec un millésime faux — l'exemple du prompt grouping enseignait à dater les
  séries ; corrigé.)
- **Grammaire des séparateurs** : l'underscore sépare les COMPOSANTS sémantiques
  d'un nom, le tiret lie les MOTS d'un composant, `__` reste réservé au préfixe
  horodaté — `Facture_EDF`, `Releve_BNP-Paribas`, `CV_LOUVEL-Simon_Developpeur-web`,
  dossier `2025-08_Degats-eaux-cuisine-Annoville`. (Corrige la fuite de
  séparateurs mélangés du run 4.)
- **`regrouped` au CLI** : non affiché (journal d'actions suffisant pour les
  regroupements intra-run).
- **Doublons posés à la main dans la librairie** (futur `rescan`) : détecter par
  sha256, cataloguer l'exemplaire en réutilisant la fiche de l'original (zéro IA),
  alerter, ne JAMAIS agir — politique détaillée dans `docs/backlog.md`.

### Affinements post-run 6 (validés 2026-06-14)

Le run 6 a validé l'ancrage Personnel/Travail sur le fichier contexte (PR #43).
Quatre finitions décidées (PR « série = `<Entité>/<Année>/` »), **supersèdent** la
règle post-run-4 « millésime créé seulement quand il apporte quelque chose » :

- **Série = `<Entité>/<Année>/`** : un genre récurrent va dans un dossier nommé
  d'après son **entité** (émetteur/organisme — `Energy/EDF`, `Energy/Enercoop`,
  `Banking/BNP-Paribas` — ou le genre à défaut d'émetteur — `Housing/Releves-eau`),
  jamais daté, puis un sous-dossier **millésime nu** pour l'année du document
  (`Energy/EDF/2026`), **toujours**, même pour une seule occurrence. Deux entités
  différentes = deux séries différentes, **jamais** le même dossier (le run 6 avait
  mis une facture Enercoop dans `Energy/EDF` ; le prompt grouping interdit désormais
  de regrouper entre émetteurs). Règle portée dans les 3 prompts (analyse, organize,
  grouping). Règle aussi la question CV (`…/CV/2026`).
- **Pas de date dans le stem** : `naming.sanitize_filename_stem` retire de façon
  déterministe une date **mois** (`YYYY-MM`/`YYYY-MM-DD`) en TÊTE du stem (le préfixe
  horodaté la porte déjà) ; une année nue est gardée (identité possible,
  `Recensement-population_2026`). La règle de prompt « pas de date dans le nom » reste.
- **Noms de relevés cohérents** : few-shot `relevé de compteur → Releve_<ressource>`
  (`Releve_eau`, `Releve_electricite`) — deux relevés de la même ressource = même nom.
- **3a léger** : quand le grouping place le nouveau fichier dans une série déjà
  peuplée, il peut renvoyer un `name` pour aligner son stem sur ses voisins
  (`override_name` dans `_file_cataloged`). Ne renomme PAS les fichiers déjà classés
  (harmonisation lourde = hors périmètre, toucherait symlinks/catalogue/miroir).

### Affinements post-run 7 (validés 2026-06-14, PR « année déterministe »)

Le run 7 a validé les 4 correctifs du run 6, MAIS révélé que le **sous-dossier
année disparaissait** dès qu'un dossier-série était créé/confirmé par le grouping
ou l'organize (le modèle écrivait l'année — et parfois la mauvaise, `2026` pour des
relevés de 2024/2025). Décision : **l'année passe du modèle au CODE.**

- **L'année est déterministe, dérivée de la date du document.** L'analyse renvoie
  un booléen `series` + le dossier-**entité sans année** ; les prompts (analyse,
  organize, grouping) ne proposent QUE l'entité. Le pipeline ajoute `/<AAAA>` une
  seule fois (`_with_series_year`), après que grouping/organize ont fixé l'entité —
  année = `document_dt` (date EXIF/contenu, jamais l'horodatage de traitement) pour
  le nouveau fichier ; `_fiche_year` (catalogue) pour un fichier regroupé. On
  n'ajoute l'année que s'il y a un dossier-entité **sous une base** (un certif tombé
  à plat dans `Education` n'est pas daté). `series` est stocké dans la fiche.
- **`Energy` → `Utilities`** (parapluie élec/gaz/eau ; les relevés d'eau ne sont
  pas de l'énergie). `Telecom` reste séparé.
- **CV = série datée** : `series: true`, entité `…/Employment/CV`, le nom de la
  personne reste dans le FICHIER (`CV_LOUVEL-Simon`) → `…/CV/2026/`.

## Hors périmètre (ne PAS faire ici)

- Check global de la librairie / re-tri de l'existant (futur `reorganize`/`rescan`,
  voir `docs/plan-b.md` § « À discuter »).
- Étendre M2 aux dossiers-ensembles (plan B les couvre ; à revoir après des runs).
- Chantier (e) : source de la date + liste « dates à vérifier » (chantier séparé).
- Toute modification du mécanisme plan B (catalogue → organise par dossier).

## Rappels projet pour l'implémenteur

- **Horodatage TOUJOURS en tête** des noms (fichiers ET dossiers) — jamais à la fin.
- Généraliste, pas de règles par type codées en dur — les genres cités dans les prompts
  sont des **exemples** (few-shot), pas des `if`.
- Ne jamais lancer l'app hors sandbox (`./sandbox/run.sh` ou overrides `PROCRAFILER_*`).
- Tests : ne pas committer `.env` / `context.txt` / `sandbox/` ; un fichier env vide
  pour la suite hermétique si nécessaire.
- Une PR ; l'utilisateur merge via GitHub UI ; CHANGELOG dans la PR.
