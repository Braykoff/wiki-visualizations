"""Shared CLI helpers for interactive prompts."""

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
