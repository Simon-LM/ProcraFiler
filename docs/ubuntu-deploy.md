<!-- @format -->

# Ubuntu Deployment Guide

This guide matches the expected flow:

1. develop locally;
2. push to GitHub;
3. pull/clone on Ubuntu target machine;
4. install and update from that clone.

## 1) Clone on target Ubuntu machine

```bash
git clone <REPO_URL_A_COMPLETER>
cd ProcraFiler
```

GitHub owner profile: [github.com/Simon-LM](https://github.com/Simon-LM)

## 2) System install

Recommended path for custom binaries is `/usr/local/bin`:

```bash
sudo ./scripts/install.sh --mode system
```

If you explicitly want `/usr/bin`:

```bash
sudo ./scripts/install.sh --mode system --prefix /usr
```

## 3) Verify installation

```bash
procrafiler status
procrafiler init-layout
```

## 3.1) Configure runtime environment

System install mode creates `/etc/procrafiler/procrafiler.env` automatically on first install.

Edit it and fill your API keys/settings:

```bash
sudo nano /etc/procrafiler/procrafiler.env
```

Minimum required when using Mistral for the analysis call:

```dotenv
PROCRAFILER_AI_ANALYSIS_PRIMARY=mistral:mistral-small-2506
PROCRAFILER_AI_ANALYSIS_FALLBACK=
MISTRAL_API_KEY=<YOUR_KEY>
PROCRAFILER_AI_TIMEOUT=60
PROCRAFILER_AI_RETRIES=2
```

You can check which env file was loaded with:

```bash
procrafiler status
```

## 3.2) Guided first run

Run the guided setup to choose where your files live and tell the app who you are:

```bash
procrafiler setup
```

It asks where your **Inbox**, **Library** and an optional **Mirror** (a backup copy of the library — ideally on a **different disk** than the Library, e.g. Library on SSD and Mirror on HDD, so it survives a disk failure) should live — press Enter to accept each default or type your own — writes those paths to the env file (keeping your API key + AI chains), creates **only** the folders you chose, then runs the short "who you are" questionnaire. The mirror is optional: decline it and no mirror folder is created (`mirror_sync` is turned off). If you put the mirror on the same disk as the library, `setup` warns you. Re-run `procrafiler setup` any time.

## 4) Update to the latest release

```bash
sudo /opt/procrafiler/app/update.sh --mode system
# from a clone instead: sudo ./scripts/update.sh --mode system
```

`update.sh` fetches the tags and checks out the **latest release tag** (`vX.Y.Z`) — never a branch HEAD — then reinstalls. All of it happens inside the installation's **own** source copy (`<app>/src`), so the clone you installed from is never fetched into, checked out or moved, and updating keeps working after you delete it. It prints the old → new version and refuses to run if that source copy has local changes. Your library, catalog, settings and env file are never touched. The reported version is derived from the tag itself (setuptools-scm), so `procrafiler --version` always matches the installed release.

## 5) Uninstall

```bash
sudo procrafiler-uninstall --mode system
# from a clone instead: sudo ./scripts/uninstall.sh --mode system
```

This removes the app (both launchers + venv + source) and **keeps everything else** — your library, the catalog/state, and your config (incl. the env file with your API key). Each target is reported as *removed* or *already absent*, and finding nothing to remove is an error, not a tick. **Your organized files are never deleted.**

To also remove the app's config and regenerable state (env file, settings, policy, catalog, logs, search index) — but **never** your library — add `--purge` (it lists the files and asks for confirmation; `--yes` skips the prompt). It **refuses** while any `PROCRAFILER_*` path variable is set in your shell, since those would redirect it away from the installation:

```bash
./scripts/uninstall.sh --mode user --purge
```

A purge also removes **your context file**, after offering to copy it out first (default: no copy; `--keep-context` / `--drop-context` answer up front). See the README for why.

## Notes

- No direct file deletion policy should remain enforced at application level.
- Duplicate files should be moved to a manual review queue only.
- Keep `CHANGELOG.md` updated before each tag release.
