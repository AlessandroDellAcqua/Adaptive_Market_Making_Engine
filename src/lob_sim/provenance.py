"""Machine-readable experiment provenance and source hashing."""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def source_tree_digest(root: str | Path) -> str:
    """Hash source files by relative name and bytes, independent of path."""

    base = Path(root).resolve()
    digest = hashlib.sha256()
    excluded = {".git", ".venv", "__pycache__", ".pytest_cache", "htmlcov", "artifacts"}
    generated_names = {".coverage", ".DS_Store", "coverage.xml"}
    for path in sorted(base.rglob("*")):
        relative = path.relative_to(base)
        if (
            not path.is_file()
            or path.name in generated_names
            or any(part in excluded for part in relative.parts)
        ):
            continue
        relative_name = relative.as_posix()
        digest.update(relative_name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def write_experiment_manifest(
    output: str | Path,
    *,
    experiment_name: str,
    execution_mode: str,
    configuration: dict[str, Any],
    strategy_names: list[str],
    seeds: list[int],
    repository_root: str | Path,
    external_root: str | Path | None = None,
    input_digests: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Write resolved configuration, environment, and path-independent hashes."""

    manifest = {
        "format": "lob_sim.experiment_manifest.v1",
        "experiment_name": experiment_name,
        "execution_mode": execution_mode,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "configuration": configuration,
        "strategies": strategy_names,
        "seeds": seeds,
        "environment": {
            "python_version": sys.version,
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
        "source_digests": {
            "repository": source_tree_digest(repository_root),
            "external_simulator": (
                source_tree_digest(external_root) if external_root is not None else None
            ),
        },
        "input_digests": input_digests or {},
        "git": _git_metadata(repository_root),
    }
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def _git_metadata(root: str | Path) -> dict[str, Any]:
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(root),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=Path(root),
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        return {"revision": revision, "dirty": bool(status.strip())}
    except (OSError, subprocess.CalledProcessError):
        return {"revision": None, "dirty": None}
