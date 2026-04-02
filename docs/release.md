<!-- @format -->

# Release Process

## Goal

Maintain a clean release discipline with:

- changelog updates;
- semantic version tags on GitHub;
- reproducible update flow on Ubuntu.

## Steps

1. Finalize pending work under `[Unreleased]` in [CHANGELOG.md](../CHANGELOG.md).
2. Pick next version (`X.Y.Z`) using SemVer.
3. Move entries from `[Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`.
4. Recreate an empty `[Unreleased]` section at the top.
5. Commit release files.
6. Create annotated tag:

```bash
git tag -a vX.Y.Z -m "Release vX.Y.Z"
git push origin main --tags
```

## Tag Naming

Use `vX.Y.Z` (example: `v0.1.0`).

## Recommended Commit Prefixes

- `feat:` new feature
- `fix:` bug fix
- `refactor:` internal restructuring
- `docs:` documentation changes
- `chore:` maintenance/build/CI
