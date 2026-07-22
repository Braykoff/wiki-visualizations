"""Shared helpers."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path


def prompt_choice(
    prompt: str,
    default: str | None = None,
    options: Sequence[str] | None = None,
    *,
    allow_other: bool = True,
    show_options: bool = False,
    allow_index: bool = False,
) -> str:
    """Ask until the user enters a valid value.

    Modes (combinable where sensible):
      - Open-ended: ``options=None`` accepts any non-blank input.
      - Constrained: ``options`` set; invalid values re-prompt unless ``allow_other``.
      - Indexed: ``allow_index=True`` lets the user pick ``1..N`` from ``options``.
      - Listed: ``show_options=True`` prints ``options`` before prompting.

    ``allow_other=False`` requires ``options``. ``show_options=True`` and
    ``allow_index=True`` also require ``options``. When ``options`` is given,
    order is preserved for display and indexing.
    """
    if not allow_other and options is None:
        raise ValueError("allow_other=False requires options")
    if show_options and options is None:
        raise ValueError("show_options=True requires options")
    if allow_index and options is None:
        raise ValueError("allow_index=True requires options")

    option_set = set(options) if options is not None else None

    if show_options:
        print("Available options:")
        if allow_index:
            for index, option in enumerate(options, start=1):
                marker = " (default)" if option == default else ""
                print(f"  {index}. {option}{marker}")
        else:
            for option in options:
                marker = " (default)" if option == default else ""
                print(f"  - {option}{marker}")

    if default is not None and allow_index and options and default == options[0]:
        default_hint = f"1 ({default})"
    elif default is not None:
        default_hint = default
    else:
        default_hint = None

    while True:
        if default_hint is not None:
            raw = input(f"{prompt} [{default_hint}]: ").strip()
        else:
            raw = input(f"{prompt}: ").strip()

        if raw == "":
            if default is not None:
                return default
            print("Input required.")
            continue

        if allow_index and raw.isdigit() and options is not None:
            index = int(raw)
            if 1 <= index <= len(options):
                return options[index - 1]
            if allow_other:
                print(
                    f"Invalid number: {index}. "
                    f"Choose 1-{len(options)}, or enter another value."
                )
            else:
                print(f"Invalid number: {index}. Choose 1-{len(options)}.")
            continue

        if option_set is not None and raw not in option_set:
            if allow_other:
                return raw
            listed = ", ".join(options) if options is not None else ""
            print(f"Invalid input: {raw!r}. Available options: {listed}")
            continue

        return raw


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
