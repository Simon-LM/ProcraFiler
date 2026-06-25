<!-- @format -->

# File durability — design (draft / not yet implemented)

> Status: **design to validate, then implement step by step** (see the checklist at the
> end). It captures the architecture so the early phases ship small while later ones
> (LAN, cloud, encrypted backups) plug in without a rewrite. Open questions: **[?]**.

ProcraFiler is an **archive**: most files are written once and rarely touched. Two risks
matter: **silent corruption** (bit rot on SSD/HDD over years) and **loss of a whole
disk/location**. Durability answers both: detect & repair corruption, and keep enough
redundant, self-describing copies to fully restart after a loss.

## Principles (professional baseline)

- **3-2-1-1-0**: ≥3 copies · on ≥2 different media · ≥1 offsite · +1 offline/immutable
  (encrypted cold backup) · 0 *unverified* backups (we scrub, we don't assume).
- **Checksums + scrub + heal** (ZFS/Btrfs idea, at the file level so it works on any
  filesystem): re-hash files, compare to the catalog's `sha256`, and **repair a bad copy
  from a good one**. Detection without a repair source is not durability.
- **No mandatory single master** (see below): every replica is editable; the system
  **reconciles**. We *recommend* a primary library to keep things simple, we don't require it.
- **Every location is self-contained** — documents **+ catalog + snapshot + manifest** —
  so any one location can fully bootstrap a restore.
- **Security first.** Personal documents never go to a cloud in clear text (client-side
  encryption); **keys / `.env` are never replicated** anywhere shared.
- **Never destroy the only good copy, never lose an edit.** Repair = write new → verify
  hash → swap. A true conflict keeps **both** versions.

## Reconciliation model — no single master (the backbone)

The truth is **not** one privileged node; it is the **reconciled union of all replicas**,
computed from three things ProcraFiler already has: the per-file **`sha256`** (content
identity), the stable **`doc_id`** (document identity, survives edits), and the
**`updated_at` timeline** + **deletion tombstones**.

**Terminology (kept on purpose):** the main copy is the **library**, the others are
**mirrors**. The naming is there to **encourage** editing in the library (the simplest
path, fewest conflicts) — but editing a mirror, or dropping into any inbox, must **never
break anything**.

What makes this conflict-free in practice:

- **Adding from any inbox, on any replica, never conflicts.** It is an **append-mostly**
  archive: two adds = two documents; the *same* file added in two places = **deduplicated**
  to one document (same `sha256`). At worst a recognised duplicate, never a conflict.
- **Moves / renames / deletes on any replica are reconciled**, not lost. Each replica is a
  full unit (files + catalog/manifest), so a **reconcile** merges them: a newer move wins
  by **timeline**; a delete is a **tombstone** that propagates without resurrecting the
  file. This is today's `rescan` (which already lets the user's hand-edits win)
  **generalised to N locations**.
- **The one genuinely hard case** (exists in *every* distributed system): the **same
  `doc_id` edited into different content at two replicas** before they meet. ProcraFiler
  **never loses data** → it keeps **both** as **conflict copies** (e.g.
  `…(conflict 2026-06-24, from Proton).pdf`) and surfaces them in **`review`** for the
  user to resolve (possibly a manual edit). Rare for write-once documents. **[?]** exact
  resolution UX (auto-pick newest? always ask? manual merge?) — to finalise.

> Guidance surfaced to users: **prefer editing in the library**, or add through **any
> inbox** (always safe). Editing a mirror works too and is reconciled — it just *may*
> create a conflict copy if the same file was also changed elsewhere.

## Existing building blocks (already in the repo)

- `documents.doc_id` (stable id) + `documents.sha256` (content hash, indexed) → identity
  for reconcile and the scrub's source of truth.
- Deletion **tombstones** (id + hash + date) + the **action log** → safe delete propagation
  and audit trail.
- `catalog_snapshot.json` — atomic (tmp+rename) JSON export of the catalog → a
  corruption-resistant fallback if the SQLite `.db` is damaged.
- `rescan` — already follows hand moves/renames/deletes into the catalog (the seed of
  reconcile). Runtime **lock** → safe cold copies of the DB.
- The **mirror** (`mirror_sync`, `mirror.versions_keep`, `Mirror_Trash`) → a versioned
  second copy and first repair source. `setup` advises it on a **different disk**.

## Core model: destinations

Generalise the single mirror into an ordered list of **destinations**. A destination is
four independent choices — this is what lets every future case fit without a rewrite:

| Axis | Values | Notes |
|------|--------|-------|
| **Transport** | `local` · `lan` · `rclone` | a path on another disk · a path/share on another machine · an rclone remote (cloud). |
| **Mode** | `mirror` · `backup` | **mirror** = browsable replica, files **as-is**, reconciled. **backup** = opaque immutable archive (bundled, encrypted), disaster-recovery only. |
| **Selection** | by category / type / size | a small destination gets a subset (e.g. documents only, no large media). Expressed against catalog metadata. |
| **Packaging + encryption** | `as-is` / `zip-bundles` · `none` / `client-side crypt` | cloud: encryption **required**; backups: usually zip + encrypt. |

Examples: local mirror today = `local · mirror · all · as-is · none`; a LAN machine =
`lan · mirror · all · as-is · none`; a usable cloud drive = `rclone · mirror · docs-only ·
as-is · crypt`; a cold offsite backup = `rclone · backup · all · zip-bundles · crypt`.

### Manifest (per destination)

Each destination carries a `manifest.json`: per item `{relative_path, sha256, size,
doc_id, updated_at, last_verified}`. It makes a destination **verifiable without
downloading** (compare hashes, e.g. `rclone check`), **reconcilable** (the unit of merge),
and **bootstrappable** (rebuild the catalog from it).

### Self-contained, restartable units

Every `mirror`-mode destination also receives `catalog.db` + `catalog_snapshot.json` +
the manifest. Losing the primary partition is then recoverable: point ProcraFiler at any
mirror and `restore` rebuilds library **and** catalog. (Config/keys are not in the data,
by design — re-run `setup` after a restore; the **data** comes from the mirror.)

## Cloud specifics

### Capacity → selective replication (not "everything")

Free tiers are small (Google 15 GB shared, Proton ≈2–5 GB, Dropbox 2 GB, OneDrive 5 GB) —
fine for **documents**, not for **media**. A cloud destination therefore uses a
**selection policy**. Default idea **[?]**: documents (small) → cloud OK; photos / audio /
video (large) → local + LAN by default, and to the cloud only as **zip + encrypted cold
backup** in per-type bundles (`photos/`, `audio/`, `video/`) so they are sent/skipped as a
group. The catalog knows category + size, so the rules are expressible; a destination is
**capacity-aware** (reports usage, warns before a configured budget).

### Cloud inbox = the friction-free add path

The cleanest way to add from a phone or another machine: a **cloud inbox** (a drop folder
on the drive). ProcraFiler **pulls** it, processes locally, files into the library, and
the pull **drains** it. Because nothing lingers, an inbox is always conflict-free — this is
the recommended way to feed the system from anywhere. A cloud **mirror** can also be
edited (it reconciles), but the inbox is the zero-friction path.

### Why rclone

One tool covers transport + encryption + verification for dozens of backends (incl. Google
Drive and Proton Drive): `rclone sync`/`bisync`, `rclone crypt` (client-side encryption of
names + contents), `rclone check` (verify by hash against the manifest, no full download).

## Backup trigger & anti-contamination (security-critical)

The cold `backup` (+1/+2) is the insurance against **compromise** (ransomware, a hacked
machine, human error). It must **never be deletion/overwrite-reachable from the primary**,
or malware propagates straight into it. A naive auto-sync to a backup is **forbidden**.
Acceptable triggers, strongest first:

1. **Manual / offline (air-gap)** — produce an encrypted bundle, the user puts it on
   external media and **unplugs**. **Default.**
2. **Immutable / append-only remote** — if automatic, only to a **versioned, immutable**
   target (S3 Object Lock / WORM, B2 versioning) with **write-only, no-delete** creds.
3. **Pull-based** — a trusted machine *pulls* from the primary.

Two app-level safeguards regardless of trigger:

- **Hash-gated replication** — a file is pushed only if its `sha256` **matches the
  catalog**; a silently modified/encrypted file no longer matches → **not propagated**, and
  the **scrub flags** it. The catalog hash is both the bit-rot *and* the tamper detector.
- **Versioned / retained** copies — a bad version can't destroy prior good ones.

## Creating & restoring a cold backup

A cold backup is **immutable**: no re-sync, no conflict. You create a new **dated** one,
keep N, prune the oldest.

- **Create** — `procrafiler backup --to <path|-> [--only documents] [--encrypt]` →
  `procrafiler-backup-YYYY-MM-DD.tar.zst(.age)` (+ a `.sha256`). Value over a plain `zip`:
  **consistency** (takes the lock, writes a *fresh* snapshot, *then* bundles → catalog ↔
  files match at one instant), **self-containment** (docs/subset + catalog + snapshot +
  manifest), **dated name + checksum**.
- **Restore** — `procrafiler restore --from-archive <file>`: decrypt → unpack → verify →
  reuse the mirror restore path.
- **Reminder, not auto-run** — record the last-backup date; `doctor` / `status` / `run`
  **nudge** after a configurable interval ("it's been 3 months — make an offline backup").
- **Don't reinvent backup** — built-in encrypted bundle for the simple case; wrap
  **restic** / **borg** / **rclone** for serious incremental/immutable needs. ProcraFiler
  produces the **consistent self-contained export**; the backup tool handles storage.

## Operations (future `procrafiler` commands)

1. **replicate** — push new/changed docs + catalog + snapshot + manifest to each
   destination, honouring selection/packaging/encryption (generalises `mirror_sync`).
2. **reconcile** — merge the manifests of all reachable replicas into the union: dedup by
   `sha256`, identity by `doc_id`, order by timeline, deletes via tombstones; emit
   **conflict copies** for true divergences. (Cross-location `rescan`.)
3. **scrub** — re-hash files (incremental, oldest-`last_verified` first), compare to the
   catalog, record `last_verified`, detect mismatches; report.
4. **heal** — restore a diverged copy from a good one (majority vote when ≥3 copies).
5. **verify-catalog** — `PRAGMA integrity_check` + DB↔snapshot agreement; rebuild the DB
   from the snapshot if corrupt.
6. **restore** — `--from <destination>` or `--from-archive <file>`: rebuild from a unit.
7. **backup** — create the dated encrypted bundle (above).
8. **health** — SMART (`smartctl`): warn on real disk wear, not a calendar timer.

## Security rules (hard constraints)

- Keys / `.env` are **never** sent to a mirror, LAN share, or cloud.
- Cloud (and any untrusted location) gets **client-side encrypted** data only.
- A `backup` destination is opaque (encrypted bundles); a trusted `local`/`lan` mirror may
  stay in clear for browsability.

## Implementation checklist (step by step)

### Phase 1 — Integrity, self-healing & backup, with the single local mirror (v1.0.0)

Pure Python, no new deps, no root. Lays the manifest + restore foundations.

- [x] Catalog: add `last_verified_utc` (guarded `ALTER`, like `flow_state`/`content_json`). **Done.**
- [ ] `manifest.json` writer for the library and the mirror (path, sha256, size, doc_id,
      updated_at, last_verified); written atomically (tmp+rename).
- [x] `procrafiler scrub` — incremental re-hash (`--limit N`, least-recently-verified
      first), compare to catalog (library + mirror), update `last_verified`, collect
      mismatches; printed report; non-zero exit on a problem. **Done** (`scrub.py`).
- [x] `heal` (`scrub --repair`) — restore a bad copy from a verified-good one (library ↔
      mirror), atomically + re-verified; never from a non-good source; both-bad =
      unrecoverable; logged to the action log. **Done** (`scrub.py` `_restore`).
- [x] `procrafiler verify-catalog` (`integrity_check`; rebuild the DB from
      `catalog_snapshot.json` when corrupt/lost, old DB kept aside). **Done**
      (`catalog_verify.py`). *Pending:* replicating `catalog.db` + snapshot + manifest
      into the mirror (with the `restore` slice).
- [ ] `procrafiler restore --from <mirror>` — rebuild library + catalog from the mirror unit.
- [ ] `procrafiler backup --to <path> [--only documents] [--encrypt]` — consistent dated
      bundle (+ `.sha256`); `restore --from-archive`.
- [ ] Backup reminder — store last-backup date; nudge in `doctor`/`status`.
- [ ] Offline tests for each; docs (README + this file) updated.

### Phase 2 — Multiple replicas & reconciliation (≥3 copies, LAN)

- [ ] Destinations config (ordered list; `local`/`lan` transport; per-destination selection).
- [ ] `procrafiler reconcile` — cross-location merge (dedup by sha256, identity by doc_id,
      timeline, tombstones); **conflict copies** surfaced in `review`.
- [ ] Per-replica **inbox** ingest (drained), so adds work from any replica.
- [ ] Majority-vote heal with ≥3 copies.

### Phase 3 — Cloud via rclone

- [ ] `rclone` transport + `crypt`; remote hash verification (`rclone check`).
- [ ] Capacity-aware selection policies; cloud **inbox** + reconciled cloud mirror.
- [ ] Immutable cold-backup targets (write-only/versioned).

### Phase 4 — Disk health

- [ ] `procrafiler health` — SMART via `smartctl`; "replace this disk" warnings.

## Open questions [?]

- **Conflict resolution UX** for the same-doc-edited-twice case (auto-newest vs always-ask
  vs manual merge) and how `review` presents it.
- Default **selection policy** per destination (what counts as "documents", media routing,
  size thresholds, budgets).
- **Cold-backup packaging** (one archive vs per-category bundles vs delegating to restic/borg).
- Where **destination config** lives (settings vs env) and how `setup` exposes it without
  overwhelming a first-time user.
