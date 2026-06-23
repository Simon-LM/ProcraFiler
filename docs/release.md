<!-- @format -->

# Release Process

## Goal

Maintain a clean release discipline with:

- changelog updates;
- semantic version tags on GitHub;
- reproducible update flow on Ubuntu.

> **The git tag IS the version.** The package version is derived from the latest
> tag by **setuptools-scm** (`procrafiler --version` reads it from the installed
> metadata) — there is **no version number to edit** in `pyproject.toml` or the
> code. Creating the tag below is what sets the version, and `update.sh` checks
> out that tag, so installs always match a published version.

## Steps

1. Finalize pending work under `[Unreleased]` in [CHANGELOG.md](../CHANGELOG.md).
2. Pick the next version (`X.Y.Z`) using SemVer.
3. Move entries from `[Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`; recreate an empty `[Unreleased]`.
4. Commit + merge the release PR.
5. On `main`, create the annotated tag (this sets the version) and the GitHub Release:

```bash
git tag -a vX.Y.Z -m "vX.Y.Z — <theme>: <one-line summary>"
git push origin vX.Y.Z
gh release create vX.Y.Z --title "vX.Y.Z — <theme>" --notes-file <changelog section>
```

## Tag Naming

Use `vX.Y.Z` (example: `v0.1.0`).

## Recommended Commit Prefixes

- `feat:` new feature
- `fix:` bug fix
- `refactor:` internal restructuring
- `docs:` documentation changes
- `chore:` maintenance/build/CI
