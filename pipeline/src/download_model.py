#!/usr/bin/env python3
"""Interactive CLI to download sentence-transformers embedding models.

Run with: uv run download-model
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sentence_transformers import SentenceTransformer

from util import find_project_root, prompt

PROJECT_ROOT = find_project_root()

# Curated suggestions shown in the prompt (name → embedding dimensions).
# Arbitrary Hugging Face / sentence-transformers ids are also accepted.
MODEL_OPTIONS: tuple[tuple[str, int], ...] = (
    ("BAAI/bge-base-en-v1.5", 768),
    ("BAAI/bge-small-en-v1.5", 384),
    ("BAAI/bge-large-en-v1.5", 1024),
    ("sentence-transformers/all-MiniLM-L6-v2", 384),
    ("nomic-ai/nomic-embed-text-v1.5", 768),
    ("Snowflake/snowflake-arctic-embed-m-v2.0", 768),
)
DEFAULT_MODEL = MODEL_OPTIONS[0][0]
KNOWN_DIMENSIONS = dict(MODEL_OPTIONS)


@dataclass(frozen=True)
class ModelDownloadConfig:
    """Resolved options for a model download."""

    model_id: str
    target: Path


def print_model_options() -> None:
    """Print curated model suggestions with embedding dimensions."""
    print("Suggested embedding models:")
    for index, (name, dims) in enumerate(MODEL_OPTIONS, start=1):
        default_marker = " (default)" if name == DEFAULT_MODEL else ""
        print(f"  {index}. {name} — {dims} dimensions{default_marker}")
    print(
        "Enter a number from the list, a model id, or leave blank for the default."
    )


def select_model() -> str:
    """Ask which model to download: blank, list number, or arbitrary model id."""
    print_model_options()
    return prompt(
        "Which model do you want to download?",
        default=DEFAULT_MODEL,
        options=[name for name, _ in MODEL_OPTIONS],
        allow_index=True,
        allow_other=True,
    )


def model_directory_name(model_id: str) -> str:
    """Filesystem-safe directory name for a Hugging Face model id."""
    return model_id.replace("/", "--")


def default_model_target(model_id: str) -> Path:
    """Default save path: data/models/<model-name> under the repo root."""
    return PROJECT_ROOT / "data" / "models" / model_directory_name(model_id)


def ensure_empty_directory(path: Path) -> None:
    """Create path (and parents); raise if it already contains anything."""
    path.mkdir(parents=True, exist_ok=True)
    contents = list(path.iterdir())
    if contents:
        names = ", ".join(sorted(p.name for p in contents[:10]))
        more = " ..." if len(contents) > 10 else ""
        raise RuntimeError(
            f"Target directory is not empty: {path} (contains {names}{more})"
        )


def select_target(model_id: str) -> Path:
    """Ask where to save the model; default is data/models/<model-name>."""
    default_target = default_model_target(model_id)
    return Path(
        prompt(
            "Target directory",
            default=str(default_target),
            validate_path=True,
        )
    )


def format_duration(seconds: float) -> str:
    """Pretty-print a duration as h/m/s."""
    seconds = max(0, int(round(seconds)))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def verify_model(target: Path, model_id: str) -> int:
    """Load the saved model, run a test encode, and return embedding dimension."""
    print("\nVerifying model...")
    model = SentenceTransformer(str(target), trust_remote_code=True)
    embedding_dim = model.get_embedding_dimension()
    if embedding_dim is None or embedding_dim <= 0:
        raise RuntimeError(f"Invalid embedding dimension: {embedding_dim}")

    expected = KNOWN_DIMENSIONS.get(model_id)
    if expected is not None and embedding_dim != expected:
        raise RuntimeError(
            f"Dimension mismatch for {model_id}: expected {expected}, got {embedding_dim}"
        )

    sample = model.encode("verification test", convert_to_numpy=True)
    if sample.shape[-1] != embedding_dim:
        raise RuntimeError(
            f"Test encode returned shape {sample.shape}, expected last dim {embedding_dim}"
        )

    print(f"  OK  loads from {target}")
    print(f"  OK  embedding dimension: {embedding_dim}")
    print("  OK  test encode succeeded")
    return embedding_dim


def download_model(config: ModelDownloadConfig) -> None:
    """Download model weights via sentence-transformers and save to target."""
    ensure_empty_directory(config.target)

    dims = KNOWN_DIMENSIONS.get(config.model_id)
    dims_note = f" ({dims} dimensions)" if dims is not None else ""
    print(f"Downloading {config.model_id}{dims_note}")
    print(f"Saving to {config.target}")
    print("(This may take a while on first download.)")

    start = time.perf_counter()
    # trust_remote_code: required by some models (e.g. nomic-embed-text).
    model = SentenceTransformer(config.model_id, trust_remote_code=True)
    model.save(str(config.target))
    duration = time.perf_counter() - start

    embedding_dim = verify_model(config.target, config.model_id)

    print("\nDownload complete.")
    print(f"  model:      {config.model_id}")
    print(f"  dimensions: {embedding_dim}")
    print(f"  target:     {config.target}")
    print(f"  duration:   {format_duration(duration)}")
    print(f"  finished:   {datetime.now().isoformat(timespec='seconds')}")


def configure_interactively() -> ModelDownloadConfig:
    """Prompt for model id and target directory."""
    model_id = select_model()
    target = select_target(model_id)
    return ModelDownloadConfig(model_id=model_id, target=target)


def main() -> int:
    """Run the interactive model download flow; return a process exit code."""
    try:
        config = configure_interactively()
        download_model(config)
        return 0
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
