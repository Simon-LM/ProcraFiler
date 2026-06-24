<!-- @format -->

# File durability — design (draft / not yet implemented)

> Status: **design to validate before coding.** This captures the architecture so
> the early phases can ship small while later ones (LAN, cloud, encrypted backups)
> plug in without a rewrite. Open questions are marked **[?]**.

ProcraFiler is an **archive**: most files are written once and rarely touched. Two
risks then matter: **silent corruption** (bit rot on SSD/HDD over years) and **loss
of a whole disk/location**. Durability answers both: detect & repair corruption,
and keep enough redundant, self-describing copies to fully restart after a loss.

## Principles (professional baseline)

- **3-2-1-1-0**: ≥3 copies · on ≥2 different media · ≥1 offsite · +1 offline/immutable
  (encrypted cold backup) · 0 *unverified* backups (we scrub, we don't assume).
- **Checksums + scrub + heal** (ZFS/Btrfs idea, applied at the file level so it works
  on any filesystem): re-hash files, compare to the catalog's `sha256`, and **repair a
  bad copy from a good one**. Detection without a repair source is not durability.
- **Every backup location is self-contained** — documents **+ catalog + snapshot +
  manifest** — so any one location can fully bootstrap a restore.
- **Metadata is as precious as the files.** Losing the catalog loses search, dedup
  tombstones and provenance, so it is replicated and recoverable too.
- **Security first.** Personal documents never go to a cloud in clear text
  (client-side encryption), and **keys / `.env` are never replicated** anywhere shared.
- **Never destroy the only good copy.** Repair = write new → verify hash → then swap.

## Existing building blocks (already in the repo)

- `documents.sha256` — a content hash per document, indexed. The scrub's source of truth.
- `catalog_snapshot.json` — an atomic (tmp+rename) JSON export of the catalog →
  a corruption-resistant fallback if the SQLite `.db` is damaged.
- Runtime **lock** → safe cold copies of the DB. **Action log** → audit trail.
- The **mirror** (`mirror_sync`) → today's second copy and first repair source.
- `setup` already advises the mirror on a **different disk** than the library.

## Core model: destinations

Generalise the single mirror into an ordered list of **destinations**. A destination
is four independent choices — this is what lets every future case fit without a rewrite:

| Axis | Values | Notes |
|------|--------|-------|
| **Transport** | `local` · `lan` · `rclone` | where/how: a path on another disk, a path on another machine, or an rclone remote (cloud). |
| **Mode** | `mirror` · `backup` | **mirror** = browsable, files stored **as-is** (you can open them at the destination). **backup** = opaque archive (bundled, encrypted), disaster-recovery only. |
| **Selection** | filter by category / type / size | so a small destination gets a subset (e.g. documents only, no large media). Expressed against catalog metadata. |
| **Packaging + encryption** | `as-is` / `zip-bundles` · `none` / `client-side crypt` | cloud destinations: encryption **required**; backups: usually zip + encrypt. |

Examples this expresses cleanly:

- **Local mirror** (today): `local · mirror · all · as-is · none`.
- **Second machine on the LAN**: `lan · mirror · all · as-is · none`.
- **Usable cloud drive** (Proton/Google): `rclone · mirror · documents-only · as-is · crypt`.
- **Cold offsite backup**: `rclone · backup · all · zip-bundles · crypt`.

### Manifest (per destination)

Each destination carries a `manifest.json`: for every item, `{relative_path, sha256,
size, doc_id, last_verified}`. It makes a destination **verifiable without downloading**
(compare remote hashes, e.g. via `rclone check`) and **bootstrappable** (rebuild the
catalog from it).

### Catalog replication = self-contained, restartable units

Every `mirror`-mode destination also receives `catalog.db` + `catalog_snapshot.json` +
the manifest. So losing the primary partition is recoverable: point ProcraFiler at a
destination and `restore` rebuilds library **and** catalog. (Config/keys are *not* in
the data, by design — re-run `setup` after a restore; the **data** comes from the mirror.)

## Cloud specifics

### Capacity → selective replication (not "everything")

Free tiers are small (Google 15 GB shared, Proton ≈2–5 GB, Dropbox 2 GB, OneDrive 5 GB)
— fine for **documents**, not for **media**. So a cloud destination uses a **selection
policy**. Default idea **[?]**:

- **documents** (small) → cloud OK (fits a free tier);
- **photos / audio / video** (large) → local + LAN by default; to the cloud only if
  capacity allows, and preferably as **zip + encrypted cold backup**, in per-type
  bundles (`photos/`, `audio/`, `video/`) so they can be sent/skipped as a group.

The catalog already knows each document's category and the file knows its size, so these
rules are expressible. A destination is **capacity-aware**: it reports usage and warns
before exceeding a configured budget.

### Cloud inbox + cloud mirror = avoid 2-way conflicts

A true two-way sync (you edit files on the drive *and* locally) creates **conflicts**
(the Dropbox/Drive problem). We sidestep it by separating roles on the cloud:

- **Cloud mirror** = a **read-mostly**, one-way push (ProcraFiler → cloud). You can
  browse it; you don't edit it. No conflict.
- **Cloud inbox** = the single **write** surface. You (or your phone) drop files there;
  ProcraFiler **pulls** them, processes locally, files them into the library — and the
  pull **drains** the inbox (moves the files out). Because nothing lingers there, there
  is no persistent divergent state to conflict over.

> User guidance to surface: *the cloud mirror is a copy to read/recover from — to add
> new files, use the cloud inbox; don't edit the mirror directly.*

If a genuine bidirectional mirror is ever wanted, it becomes `rclone bisync` with
conflict-copies, using the catalog `sha256` + `updated_at` **timeline** as the arbiter
(local catalog is canonical). Flagged **advanced / later** — the inbox+read-mirror split
covers the common need without that complexity. **[?]** conflict policy to finalise.

### Why rclone

One tool covers transport + encryption + verification for dozens of backends (incl.
Google Drive and Proton Drive): `rclone sync` (1-way), `rclone bisync` (2-way),
`rclone crypt` (client-side encryption of names + contents), `rclone check` (verify by
hash against the manifest, no full download).

## Operations (future `procrafiler` commands)

1. **replicate** — push new/changed docs + catalog + snapshot + manifest to each
   destination, honouring its selection/packaging/encryption (generalises `mirror_sync`).
2. **scrub** — re-hash files (primary + each destination), compare to the catalog, record
   `last_verified`. Incremental (N oldest-verified per pass) so it stays light.
3. **heal** — restore a diverged copy from a good one (majority vote when ≥3 copies).
4. **verify-catalog** — `PRAGMA integrity_check` + DB↔snapshot agreement; rebuild the DB
   from the snapshot if corrupt.
5. **restore** — `restore --from <destination>`: rebuild library + catalog from a unit.
6. **health** — SMART (`smartctl`): warn on real disk wear, not a calendar timer.

## Phases (ship small, grow without rewrite)

- **Phase 1 — Integrity & self-healing with the existing local mirror** (candidate for
  v1.0.0; pure Python, no new deps, no root): `scrub` (+ `last_verified` column via a
  guarded `ALTER`), `heal` from the mirror, catalog durability (replicate db + snapshot +
  manifest, `integrity_check`, rebuild from snapshot), `restore --from`, a scrub report
  with an alert on unrecoverable corruption. **Lays the destination + manifest model.**
- **Phase 2 — Multiple destinations (≥3 copies, LAN)**: N destinations, majority-vote heal.
- **Phase 3 — Cloud via rclone**: `rclone` transport, `crypt` encryption, selection
  policies, the **cloud inbox + read mirror** split, remote hash verification.
- **Phase 4 — SMART** disk-health monitoring.

## Security rules (hard constraints)

- Keys / `.env` are **never** sent to a mirror, LAN share, or cloud.
- Cloud (and any untrusted location) gets **client-side encrypted** data only.
- A `backup`-mode destination is opaque (encrypted bundles); a `mirror`-mode local/LAN
  destination on trusted storage may stay in clear for browsability.

## Open questions [?]

- Default **selection policy** per destination type (what's "documents", media routing,
  size thresholds, budgets).
- **Conflict** policy if/when a real 2-way cloud mirror is offered.
- **Packaging granularity** of cold backups (one archive vs per-category bundles vs
  incremental) and how restore reads them.
- Where destination config lives (settings vs env) and how `setup` exposes it without
  overwhelming a first-time user.
