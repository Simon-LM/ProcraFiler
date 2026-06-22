from __future__ import annotations

import os
import sys
from pathlib import Path


def _running_under_test_runner() -> bool:
    """True when the process is clearly a test run (``python -m unittest`` or
    ``pytest``).

    Safety guard: the developer-convenience ``./.env`` (a real Mistral key +
    chains) must NEVER be auto-loaded during tests, or an "offline" unit test
    silently hits the live API (spending money, leaking data). The test suite's
    own offline guard (`tests/__init__.py`) only runs with the canonical
    ``-t . -s tests`` invocation; this check protects every other invocation too.
    Detects the runner via the process `__main__`, so it is NOT fooled by app
    code that merely imports `unittest.mock`.
    """
    if "pytest" in sys.modules:
        return True
    main_file = getattr(sys.modules.get("__main__"), "__file__", "") or ""
    return main_file.replace("\\", "/").endswith("unittest/__main__.py")


def _parse_env_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if "=" not in stripped:
        return None

    key, value = stripped.split("=", 1)
    key = key.strip()
    value = value.strip()
    if not key:
        return None

    if value and ((value[0] == '"' and value[-1:] == '"') or (value[0] == "'" and value[-1:] == "'")):
        value = value[1:-1]

    return key, value


def default_env_candidates() -> list[Path]:
    home = Path.home()
    config_home = Path(os.environ.get("PROCRAFILER_CONFIG_HOME", str(home / ".config" / "procrafiler")))
    explicit = os.environ.get("PROCRAFILER_ENV_FILE")

    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))

    # The cwd `./.env` is a developer convenience — never load it under a test
    # runner, so a test can't pick up the real key/chains and reach the live API.
    if not _running_under_test_runner():
        candidates.append(Path.cwd() / ".env")
    candidates.extend(
        [
            config_home / "procrafiler.env",
            Path("/etc/procrafiler/procrafiler.env"),
        ]
    )

    seen: set[str] = set()
    unique_candidates: list[Path] = []
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique_candidates.append(path)
    return unique_candidates


def load_runtime_env(candidates: list[Path] | None = None) -> Path | None:
    for env_file in candidates or default_env_candidates():
        if not env_file.exists() or not env_file.is_file():
            continue

        for raw_line in env_file.read_text(encoding="utf-8").splitlines():
            parsed = _parse_env_line(raw_line)
            if parsed is None:
                continue
            key, value = parsed
            if key not in os.environ:
                os.environ[key] = value

        os.environ["PROCRAFILER_ENV_LOADED_FROM"] = str(env_file)
        return env_file

    return None
