<!-- @format -->

# Plan B — organiser un dossier déposé comme un ENSEMBLE cohérent

> **Statut : BROUILLON à valider par l'utilisateur. Ne PAS implémenter le durcissement
> (section « Règles à appliquer ») tant que ce document n'est pas validé.**
> Le but de ce doc : se mettre d'accord, noir sur blanc, sur ce qu'EST le plan B et ce
> qu'il reste à faire — pour qu'il soit respecté.

## L'esprit (la règle d'or)

Les documents déposés **dans un même dossier** sont **a priori bien regroupés** : l'utilisateur
les a réunis exprès. Donc **par défaut ils restent ensemble**, dans **un seul** dossier (ou
sous-dossier) de destination — **on ne les disperse pas**.

On ne sort un fichier de l'ensemble **que** si son contenu est **flagramment** étranger à
l'affaire (barre **haute**, pas une nuance). Une photo de dégât et le constat d'assurance de
**la même** affaire = **le même** dossier, pas deux.

Le nom du dossier déposé est un **indice FORT**, pas une certitude à 100 % : il se peut qu'un
document s'y trouve par erreur. Mais le doute doit être **évident** pour justifier de le sortir.

**Fiabilité des images :** les photos sont décrites par une IA de vision qui **peut halluciner**
(mauvaise date, mauvais lieu, mauvais sujet). Les descriptions de photos **ne sont donc pas
fiables à 100 %**. → On ne disperse **jamais** un dossier sur la seule foi de la description
d'une photo ; pour un ensemble surtout composé d'images, on s'appuie **davantage sur l'indice
« dossier »** que sur les descriptions individuelles.

## Définitions

- **Ensemble (set)** = un sous-dossier de premier niveau de l'Inbox, **avec tout son arbre**
  (les sous-sous-dossiers imbriqués sont inclus dans le même ensemble). Les noms des
  sous-dossiers imbriqués sont des indices plus fins.
- **Singleton** = un fichier seul à la racine de l'Inbox (traité à part — voir « À discuter »).
- **Affaire / série** = le dossier de destination final, **daté EN TÊTE** et nommé. La date va
  TOUJOURS au **début** du nom de dossier (jamais à la fin), comme pour les fichiers — ex.
  `Personal/Administrative/Insurance/2025-08_Degats-eaux-Annoville`.

## Le mécanisme (déjà implémenté — PR #34)

> **Périmètre.** Tout ce Plan B décrit le flux **Inbox** (`run` / `process-all`). Ranger ou
> réorganiser la **librairie déjà constituée** (quand l'utilisateur y ajoute / modifie des
> fichiers à la main) est un mode **séparé et futur** (`rescan` / `reorganize`) où l'organiseur
> pourra, lui, regarder plus large — avec un vrai problème d'**échelle** à concevoir (voir
> « À discuter »). La règle « un dossier à la fois » ci-dessous est donc propre au `run`, pas une
> interdiction générale d'appeler l'organiseur sur la librairie plus tard.

**On traite UN dossier à la fois, séquentiellement** (dossier par dossier). On n'attend
**jamais** d'avoir analysé tout l'Inbox, et on **n'envoie jamais plusieurs dossiers d'un coup à
l'organiseur**. Pour **un** ensemble (= un sous-dossier de 1er niveau, son arbre compris), en
trois phases :

1. **CATALOGUE** — analyser **individuellement** chaque fichier **de CE dossier** en sa fiche
   (nom, date, résumé, catégorie *proposée* par-fichier…). **Rien n'est rangé** ; les fichiers
   attendent dans `Queue`. _(L'analyse par-fichier sert UNIQUEMENT à fabriquer les fiches — ce
   n'est pas un rangement par-fichier.)_
2. **ORGANISE** — une fois **tous les fichiers de CE dossier** analysés, **un seul** appel
   `organize_set` reçoit les fiches **de ce dossier** et décide la place de chacune.
3. **RANGE** — chaque fichier de ce dossier est déposé à sa place finale (déplacement + miroir +
   catalogue). Puis on passe au **dossier suivant**.

➡️ **Le mécanisme par-dossier est fait et testé.** Le problème de dispersion n'est PAS là.
**Réserve (R7, à faire) :** si un dossier contient ÉNORMÉMENT de fichiers, un seul appel
`organize_set` devient trop gros et risque des erreurs → il faudra **découper ce dossier en
lots** (voir R7). On ne charge jamais tout l'Inbox d'un coup.

## Règles à appliquer (LE travail restant — à valider avant de coder)

Ces règles doivent être encodées dans les **consignes de l'organiseur** (et au besoin dans sa
structure de sortie). C'est ce qui manque aujourd'hui.

- **R1 — Une destination par défaut.** Pour un ensemble issu d'un dossier déposé, choisir **UN
  seul** dossier de destination pour **tous** ses fichiers (une base + un nom d'affaire +
  **une** période). Ne pas répartir l'ensemble entre plusieurs dossiers par défaut.
- **R2 — Barre haute pour sortir un fichier.** Ne sortir un fichier de l'ensemble que si son
  contenu est **flagramment** d'une autre affaire. Une nuance (photo du dégât vs paperasse
  d'assurance de la **même** affaire) **n'est pas** un motif de séparation.
- **R3 — Pas de fragmentation entre bases.** Interdit d'éclater **une même affaire** entre deux
  catégories de base (ex. la moitié dans `Housing`, l'autre dans `Insurance`).
- **R4 — Un seul nom d'affaire pour l'ensemble.** Décider le nom du dossier d'affaire **une
  fois** pour tout l'ensemble (une seule période), pas par-document → fini les `…-2025-08` ET
  `…-2025-10` pour la même affaire.
- **R5 — Descriptions de photos peu fiables.** Indiquer explicitement que les descriptions
  d'images peuvent être hallucinées ; ne jamais disperser un dossier sur la base d'une seule
  photo douteuse ; pour un ensemble surtout d'images, privilégier l'indice « dossier ».
- **R6 — Réutiliser l'existant.** Préférer un dossier existant de l'arbre à un quasi-doublon.
- **R7 — Échelle / gros dossiers.** Si un dossier a trop de fichiers pour tenir dans un seul
  appel `organize_set` (risque d'erreurs), le **découper en lots** (p. ex. par sous-dossier
  imbriqué, ou par paquets de N) plutôt qu'un appel géant. **Jamais tout l'Inbox d'un coup.**

## Checklist — état réel (à cocher ensemble)

Mécanisme :

- [x] Traitement **dossier par dossier**, séquentiel (jamais tout l'Inbox d'un coup)
- [x] Catalogue tout le dossier d'abord, sans ranger (`_catalog_one_inbox_file`)
- [x] **Un seul** appel `organize_set` par dossier, une fois ses fichiers tous analysés
- [x] Range chaque fichier à la place décidée par l'ensemble (miroir + catalogue suivent)
- [x] Le nom du dossier déposé est fourni comme **indice fort** dans le prompt
- [x] Dédup intra-run préservée

Esprit (= ce qui a fragmenté les dégâts des eaux — **à faire**) :

- [x] **R1** une destination par défaut pour tout l'ensemble — implémenté (prompt)
- [x] **R2** barre haute pour sortir un fichier (FLAGRANT) — implémenté (prompt)
- [x] **R3** pas de fragmentation entre bases — implémenté (prompt)
- [x] **R4** un seul nom d'affaire/période, **date EN TÊTE** — implémenté (prompt)
- [x] **R5** descriptions de photos peu fiables (anti-hallucination) — implémenté (prompt)
- [x] **R6** réutiliser un dossier existant — présent
- [x] **R7** découper les très gros dossiers en lots (`ORGANIZE_MAX_SET`=80) — implémenté

> Implémentés côté prompt organiseur + pipeline — **à RE-TESTER en sandbox** pour confirmer que
> la dispersion (dégâts des eaux éclatés Housing/Insurance/multi-dates) a bien disparu.

## Filet de sécurité (dernier recours, seulement si R1–R5 ne suffisent pas)

Si, malgré les règles, l'organiseur disperse encore les fichiers d'**un même dossier déposé**
dans >1 dossier d'affaire : **consolider** vers le dossier dominant et **émettre une alerte**
sur les fichiers exclus (au lieu de scinder en silence). À n'activer que si le durcissement du
prompt ne donne pas satisfaction.

## À discuter (hors périmètre immédiat de B)

- **Fichiers racine (singletons) :** aujourd'hui non organisés en ensemble → doublons de format
  classés différemment, pas de séries (compteur, CAF). Piste : un **balayage de cohérence en
  fin de run** sur les fichiers de CE run uniquement. **À faire APRÈS** R1–R5.
- **Fichier de contexte utilisateur** (passion/pro, lieux, identité) : aide la classification et
  le nommage — chantier séparé.
- **Réorganiser la librairie entière (futur `reorganize` / `rescan`) :** appeler l'organiseur de
  temps en temps pour (re)classer la librairie déjà constituée — **distinct** du flux Inbox.
  **Gros défi d'ÉCHELLE** : un seul appel sur toute la librairie devient impossible quand il y a
  beaucoup de fichiers. Pistes à creuser (pas urgent) : par dossier / par catégorie ;
  **incrémental** (seulement ce qui a changé depuis le dernier passage) ; donner à l'organiseur
  des **résumés de dossiers** plutôt que chaque fiche ; organisation **hiérarchique** (d'abord
  les grandes catégories, puis l'intérieur de chacune). À réfléchir ensemble le moment venu.

## Validation

- [x] L'utilisateur valide l'esprit + R1–R7 (2026-06-10).
- [x] R1–R5 + R7 implémentés (prompt organiseur + cadrage échelle).
- [ ] RE-TESTER en sandbox : la dispersion des dégâts des eaux doit avoir disparu.
