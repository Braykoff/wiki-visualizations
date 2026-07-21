"""Shared helpers."""

from __future__ import annotations

from pathlib import Path


def prompt_choice(prompt: str, default: str, valid: set[str] | None = None) -> str:
    """Ask until blank (use default) or a valid value is entered."""
    while True:
        raw = input(f"{prompt} [{default}]: ").strip()
        value = default if raw == "" else raw
        if valid is not None and value not in valid:
            options = ", ".join(sorted(valid))
            print(f"Invalid input: {value!r}. Available options: {options}")
            continue
        return value


def prompt_path(prompt: str, default: Path) -> Path:
    """Ask for a filesystem path; blank keeps the default."""
    while True:
        raw = input(f"{prompt} [{default}]: ").strip()
        if raw == "":
            return default
        try:
            return Path(raw).expanduser().resolve()
        except (OSError, RuntimeError) as exc:
            print(f"Invalid path: {exc}. Please try again.")


def find_project_root() -> Path:
    """Locate the wiki-visualizations repo root (parent of pipeline/).

    Do not derive this from ``Path(__file__).parents[N]`` alone: when the
    package is installed into the venv, ``__file__`` lives under site-packages.
    """
    starts: list[Path] = [Path.cwd().resolve(), Path(__file__).resolve()]

    seen: set[Path] = set()
    for start in starts:
        for path in [start, *start.parents]:
            if path in seen:
                continue
            seen.add(path)

            # Installed into pipeline/.venv/.../site-packages/ → climb to repo root
            if path.name == ".venv":
                pipeline_dir = path.parent
                if (pipeline_dir / "pyproject.toml").is_file():
                    return pipeline_dir.parent

            # Repo root contains pipeline/pyproject.toml
            if (path / "pipeline" / "pyproject.toml").is_file():
                return path

            # Invoked from inside pipeline/
            if path.name == "pipeline" and (path / "pyproject.toml").is_file():
                return path.parent

    raise RuntimeError(
        "Could not locate project root. Run from the wiki-visualizations "
        "repo (or its pipeline/ directory)."
    )
