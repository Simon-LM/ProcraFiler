#!/usr/bin/env python3
"""Run a mutation campaign: sabotage one line, check a test notices, restore.

Mutation testing is this project's primary quality signal — a green suite says the
code runs, not that the tests would object if it stopped being correct. Each mutant
is a single deliberate defect; a mutant that SURVIVES names a guarantee nobody
asserts.

Usage:

    python scripts/mutate.py mutants.json            # the whole campaign
    python scripts/mutate.py mutants.json --start 0 --stop 4   # one batch

Run it in **batches**. Every mutant starts a fresh interpreter and re-runs a whole
test module, so a long campaign keeps a core busy for minutes — on a laptop that is
heat and fan noise for no added information over four batches of five.

`mutants.json` is a list of objects:

    [
      {
        "label": "the guard never refuses",
        "file": "src/procrafiler/state_version.py",
        "old": "if written <= running:",
        "new": "if True:",
        "test": "tests.test_state_version"
      }
    ]

`old` must appear EXACTLY ONCE in the file: an anchor matching twice would mutate
an arbitrary one of them, and matching zero times would silently score a mutant
that was never applied. Both are reported as failures rather than skipped.

**The bytecode trap this script exists to prevent.** Python validates a cached
`.pyc` on the source's size and mtime. A mutation of the SAME BYTE LENGTH —
`30` -> `90`, `<=` -> `>=`, `True` -> `else` — restored within the same second
leaves the file with exactly the size and mtime the cached bytecode was built
from. Python then keeps serving the MUTATED bytecode after the restore, silently,
for every later run in that directory. It cost this project a real debugging
session: a passing test began failing with the source visibly correct on disk.

Two protections, both needed, and `tests/test_mutate_script.py` pins them:

1. every write drops the module's cached `.pyc`, so nothing stale can be served;
2. the test subprocess runs with `PYTHONDONTWRITEBYTECODE=1`, so a mutated module
   never produces a cache file in the first place.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


def repo_root(start: Path | None = None) -> Path:
    """The repository root, walked up from the CWD.

    From the CWD and not from `__file__`, so the script still works when it is run
    from a copy outside the repository — and with a stop condition, because walking
    up from a path that has no `.git` above it otherwise spins for ever at `/`.
    """
    root = (start or Path.cwd()).resolve()
    while not (root / ".git").exists():
        if root.parent == root:
            raise SystemExit("not inside a git repository")
        root = root.parent
    return root


@dataclass(frozen=True)
class Mutant:
    label: str
    file: str
    old: str
    new: str
    test: str


def load_mutants(path: Path) -> list[Mutant]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise SystemExit(f"{path}: expected a list of mutants")
    mutants: list[Mutant] = []
    for index, entry in enumerate(payload):
        missing = [k for k in ("label", "file", "old", "new", "test") if k not in entry]
        if missing:
            raise SystemExit(f"{path}: mutant {index} is missing {', '.join(missing)}")
        mutants.append(Mutant(**{k: entry[k] for k in ("label", "file", "old", "new", "test")}))
    return mutants


def write_source(path: Path, text: str) -> None:
    """Write the file, then make any cached bytecode for it impossible to serve.

    See the bytecode trap in this module's docstring: without the second half, a
    same-length mutation restored within the same second leaves Python serving the
    mutated bytecode from cache.
    """
    path.write_text(text, encoding="utf-8")
    cache = path.parent / "__pycache__"
    if cache.is_dir():
        for stale in cache.glob(f"{path.stem}.*.pyc"):
            stale.unlink(missing_ok=True)


def run_test(module: str, *, cwd: Path) -> bool:
    """True when the module passes. Bytecode writing is off: a mutated module must
    not leave a cache file behind for the next run to pick up."""
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", module],
        cwd=cwd, env=env, capture_output=True, text=True,
    )
    return proc.returncode == 0


def run_campaign(mutants: list[Mutant], *, root: Path) -> tuple[int, int]:
    caught = survived = 0
    for mutant in mutants:
        path = root / mutant.file
        try:
            original = path.read_text(encoding="utf-8")
        except OSError as err:
            print(f"  ??  NOT APPLIED  {mutant.label}  ({err})")
            survived += 1
            continue

        occurrences = original.count(mutant.old)
        if occurrences != 1:
            # Never scored as caught: a mutant that was not applied proves nothing,
            # and counting it as a pass is how a campaign flatters itself.
            print(f"  ??  NOT APPLIED  {mutant.label}  (anchor found {occurrences}x)")
            survived += 1
            continue

        write_source(path, original.replace(mutant.old, mutant.new, 1))
        try:
            passed = run_test(mutant.test, cwd=root)
        finally:
            write_source(path, original)

        if passed:
            print(f"  !!  SURVIVED    {mutant.label}")
            survived += 1
        else:
            print(f"  ok  caught      {mutant.label}")
            caught += 1
    return caught, survived


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mutants", help="JSON file describing the mutants")
    parser.add_argument("--start", type=int, default=0, help="First mutant to run (default: 0)")
    parser.add_argument("--stop", type=int, default=None, help="Stop before this one (default: all)")
    args = parser.parse_args(argv)

    root = repo_root()
    mutants = load_mutants(Path(args.mutants))
    batch = mutants[args.start:args.stop]
    if not batch:
        print("No mutants in that range.")
        return 0

    caught, survived = run_campaign(batch, root=root)
    print(f"\n{caught} caught, {survived} survived, out of {len(batch)} in this batch")
    return 1 if survived else 0


if __name__ == "__main__":
    raise SystemExit(main())
