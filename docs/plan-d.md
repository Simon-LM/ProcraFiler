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
