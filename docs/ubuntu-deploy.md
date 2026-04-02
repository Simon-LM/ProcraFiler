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

Minimum required when using Mistral naming:

```dotenv
PROCRAFILER_AI_NAMING_PRIMARY=mistral:mistral-small-2506
PROCRAFILER_AI_NAMING_FALLBACK=
MISTRAL_API_KEY=<YOUR_KEY>
PROCRAFILER_AI_TIMEOUT=60
PROCRAFILER_AI_RETRIES=2
```

You can check which env file was loaded with:

```bash
procrafiler status
```

## 4) Update after new GitHub push

```bash
cd ProcraFiler
git pull --ff-only
sudo ./scripts/update.sh --mode system
```

## 5) Uninstall

```bash
sudo ./scripts/uninstall.sh --mode system
```

## Notes

- No direct file deletion policy should remain enforced at application level.
- Duplicate files should be moved to a manual review queue only.
- Keep `CHANGELOG.md` updated before each tag release.
