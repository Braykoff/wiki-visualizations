#!/usr/bin/env python3
"""Interactive CLI to embed Wikipedia articles into a SQLite database.

Two ways to run:
  uv run embeddings
      Interactive prompts; creates (or resumes) an embeddings database.
  uv run embeddings /path/to/embeddings.sqlite
      Reads metadata from the database and resumes that job.

Architecture:
  - N parser worker *processes* (sized from hardware concurrency) stream
    pages out of the .xml.bz2 shards and push cleaned article text onto a
    bounded queue. Processes are used instead of threads because wikitext
    parsing is CPU-bound and Python threads serialize on the GIL.
  - The main process is the only one that touches the model and the
    database: it batches articles, encodes them on the best available
    device (CUDA/MPS/CPU), and commits each batch transactionally.
    A crash therefore loses at most one batch of articles.
  - Articles longer than the model's context window are split into chunks;
    chunk embeddings are mean-pooled into one normalized vector per article.
"""

from __future__ import annotations

import argparse
import bz2
import multiprocessing as mp
import os
import queue as queue_module
import re
import sqlite3
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tqdm import tqdm

from util import find_project_root, prompt

PROJECT_ROOT = find_project_root()
ARCHIVES_DIR = PROJECT_ROOT / "data" / "archives"
MODELS_DIR = PROJECT_ROOT / "data" / "models"
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"

SCHEMA_VERSION = "2"
ARTICLE_NAMESPACE = "0"
TOKEN_SAFETY_MARGIN = 16  # reserve room for model-added special tokens
MAX_CHUNKS_PER_ARTICLE = 120  # caps pathological pages
ARTICLES_PER_FLUSH = 32  # encode+commit granularity; a crash loses at most this many
MAX_ENCODE_BATCH_SIZE = 32
QUEUE_MAX_MESSAGES = 100  # backpressure so parsers don't run away from the encoder
PROGRESS_BYTES_STEP = 4 * 1024 * 1024
BASE_URL_FALLBACK = "https://en.wikipedia.org"


@dataclass(frozen=True)
class JobConfig:
    """Resolved options for an embeddings job."""

    archive: Path
    model: Path
    database: Path


@dataclass(frozen=True)
class PageRecord:
    """One parsed article waiting to be embedded."""

    shard: str
    page_id: int
    rev_id: int
    title: str
    url: str
    text: str


@dataclass(frozen=True)
class EmbeddingSettings:
    """Model-specific settings that must remain stable when resuming."""

    max_tokens: int
    document_prefix: str
    encode_batch_size: int


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

def open_connection(path: Path) -> sqlite3.Connection:
    """Open SQLite with WAL so batch commits are durable but fast."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def create_database(path: Path, archive: Path, model: Path) -> None:
    """Create schema and store job metadata for later resume/validation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = open_connection(path)
    try:
        with conn:
            conn.execute(
                "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            conn.execute(
                """
                CREATE TABLE embeddings (
                    page_id INTEGER PRIMARY KEY,
                    rev_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    embedding BLOB NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE shard_progress (
                    shard TEXT PRIMARY KEY,
                    completed INTEGER NOT NULL DEFAULT 0,
                    last_page_id INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.executemany(
                "INSERT INTO metadata (key, value) VALUES (?, ?)",
                [
                    ("schema_version", SCHEMA_VERSION),
                    ("archive_path", str(archive)),
                    ("archive_name", archive.name),
                    ("model_path", str(model)),
                    ("model_name", model.name),
                    ("created_at", created_at),
                ],
            )
    finally:
        conn.close()


def read_metadata(conn: sqlite3.Connection) -> dict[str, str]:
    return dict(conn.execute("SELECT key, value FROM metadata"))


def validate_database(path: Path) -> dict[str, str]:
    """Check the file is one of our embeddings databases; return its metadata."""
    conn = open_connection(path)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        missing_tables = {"metadata", "embeddings", "shard_progress"} - tables
        if missing_tables:
            raise RuntimeError(
                f"Not a wikiviz embeddings database (missing tables: "
                f"{', '.join(sorted(missing_tables))}): {path}"
            )

        columns = {row[1] for row in conn.execute("PRAGMA table_info(embeddings)")}
        required = {"page_id", "rev_id", "title", "url", "embedding"}
        if not required <= columns:
            raise RuntimeError(
                f"embeddings table is missing columns "
                f"{', '.join(sorted(required - columns))}: {path}"
            )

        meta = read_metadata(conn)
        for key in ("archive_path", "archive_name", "model_path", "model_name"):
            if not meta.get(key):
                raise RuntimeError(f"Database metadata is missing {key!r}: {path}")
        return meta
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Wikitext extraction (runs in worker processes)
# --------------------------------------------------------------------------

def _local_name(tag: str) -> str:
    """Strip the XML namespace from a tag name."""
    return tag.rsplit("}", 1)[-1]


def _child(elem: ET.Element, name: str) -> ET.Element | None:
    for candidate in elem:
        if _local_name(candidate.tag) == name:
            return candidate
    return None


def detect_base_url(shard: Path) -> str:
    """Read <base> from the dump header to build article URLs."""
    with bz2.open(shard, "rt", encoding="utf-8", errors="replace") as stream:
        head = stream.read(8192)
    match = re.search(r"<base>(https?://[^/<]+)", head)
    return match.group(1) if match else BASE_URL_FALLBACK


def article_url(base_url: str, title: str) -> str:
    return f"{base_url}/wiki/{urllib.parse.quote(title.replace(' ', '_'), safe='/:()')}"


def clean_wikitext(wikitext: str) -> str:
    """Convert wikitext to plain text (keeps headings/paragraph text)."""
    import mwparserfromhell

    return mwparserfromhell.parse(wikitext).strip_code(
        normalize=True, collapse=True
    ).strip()


def parse_shard(shard_str: str, skip_up_to_page_id: int, out_queue: mp.Queue) -> None:
    """Worker process: stream articles from one shard onto the queue.

    Message protocol: ("page", PageRecord fields...), ("progress", shard, bytes),
    ("done", shard), ("error", shard, message).
    """
    shard = Path(shard_str)
    try:
        base_url = detect_base_url(shard)
        raw = open(shard, "rb")
        try:
            stream = bz2.BZ2File(raw)
            reported_bytes = 0

            # iterparse + root.clear() keeps memory flat on multi-GB shards.
            context = ET.iterparse(stream, events=("start", "end"))
            _, root = next(context)
            for event, elem in context:
                if event != "end" or _local_name(elem.tag) != "page":
                    continue

                ns_elem = _child(elem, "ns")
                title_elem = _child(elem, "title")
                id_elem = _child(elem, "id")
                revision = _child(elem, "revision")
                is_redirect = _child(elem, "redirect") is not None

                # Only real articles: namespace 0, not a redirect stub.
                if (
                    ns_elem is None
                    or (ns_elem.text or "").strip() != ARTICLE_NAMESPACE
                    or is_redirect
                    or title_elem is None
                    or id_elem is None
                    or revision is None
                ):
                    root.clear()
                    continue

                page_id = int((id_elem.text or "0").strip())
                # Resume: shards are ordered by page id, so committed pages
                # form a prefix and can be skipped by id.
                if page_id <= skip_up_to_page_id:
                    root.clear()
                    continue

                rev_id_elem = _child(revision, "id")
                text_elem = _child(revision, "text")
                title = (title_elem.text or "").strip()
                wikitext = text_elem.text if text_elem is not None else None
                root.clear()

                if not title or not wikitext:
                    continue
                text = clean_wikitext(wikitext)
                if not text:
                    continue

                rev_id = int((rev_id_elem.text or "0").strip()) if rev_id_elem is not None else 0
                out_queue.put(
                    (
                        "page",
                        shard.name,
                        page_id,
                        rev_id,
                        title,
                        article_url(base_url, title),
                        text,
                    )
                )

                # Report compressed bytes consumed for the overall progress bar.
                position = raw.tell()
                if position - reported_bytes >= PROGRESS_BYTES_STEP:
                    out_queue.put(("progress", shard.name, position - reported_bytes))
                    reported_bytes = position
        finally:
            raw.close()
        out_queue.put(("done", shard.name))
    except Exception as exc:  # noqa: BLE001 - forwarded to the main process
        out_queue.put(("error", shard.name, f"{type(exc).__name__}: {exc}"))


# --------------------------------------------------------------------------
# Embedding (runs in the main process)
# --------------------------------------------------------------------------

def document_prefix_for_model(model_path: Path) -> str:
    """Return the documented retrieval-document prefix for known models."""
    if "nomic-embed-text" in model_path.name.lower():
        return "search_document: "
    return ""


def encode_batch_size_for(max_tokens: int) -> int:
    """Scale batches down for long-context models to fit accelerator memory."""
    # Attention memory grows approximately with sequence_length². This keeps
    # the normal 32-item batches for 512-token models but avoids asking MPS
    # to hold 32 full 8k-token Nomic inputs simultaneously.
    return max(1, min(MAX_ENCODE_BATCH_SIZE, 16_384 // max_tokens))


def chunk_text(
    tokenizer, text: str, max_tokens: int, prefix: str = ""
) -> list[str]:
    """Split text into tokenizer-exact chunks without exceeding max_tokens."""
    prefix_tokens = len(tokenizer.encode(prefix, add_special_tokens=False))
    content_limit = max_tokens - prefix_tokens
    if content_limit < 1:
        raise RuntimeError("Embedding prefix leaves no room for article content")

    # `verbose=False` avoids a misleading warning from Transformers: we
    # deliberately tokenize over-length articles here so we can split them
    # ourselves before they ever reach the model.
    token_ids = tokenizer(
        text,
        add_special_tokens=False,
        truncation=False,
        verbose=False,
    )["input_ids"]
    if len(token_ids) <= content_limit:
        return [f"{prefix}{text}"]

    # Decode full token windows rather than estimating from characters. This
    # preserves Nomic's large context window and works with every tokenizer.
    # Apply retrieval instructions to every chunk, not only the first one.
    chunks = [
        prefix
        + tokenizer.decode(
            token_ids[start : start + content_limit], skip_special_tokens=True
        )
        for start in range(0, len(token_ids), content_limit)
    ]
    return chunks[:MAX_CHUNKS_PER_ARTICLE]


def load_model(model_path: Path):
    """Load the sentence-transformer on the best device and report how."""
    # Imported lazily so parser worker processes never pay for torch imports.
    import torch
    from sentence_transformers import SentenceTransformer

    if torch.cuda.is_available():
        device = "cuda"
    elif torch.backends.mps.is_available():
        device = "mps"
    else:
        device = "cpu"

    model = SentenceTransformer(str(model_path), device=device, trust_remote_code=True)
    dim = model.get_embedding_dimension()
    print(f"Model: {model_path.name}")
    print(f"  device:         {device}")
    print(f"  dimensions:     {dim}")
    max_tokens = model.max_seq_length - TOKEN_SAFETY_MARGIN
    if max_tokens < 1:
        raise RuntimeError(f"Invalid model max sequence length: {model.max_seq_length}")

    settings = EmbeddingSettings(
        max_tokens=max_tokens,
        document_prefix=document_prefix_for_model(model_path),
        encode_batch_size=encode_batch_size_for(max_tokens),
    )
    print(f"  max seq length: {model.max_seq_length} tokens")
    print(f"  chunk limit:    {settings.max_tokens} content tokens")
    print(f"  encode batch:   {settings.encode_batch_size} chunk(s)")
    if settings.document_prefix:
        print(f"  document prefix: {settings.document_prefix!r}")
    return model, device, dim, settings


def embed_articles(
    model, settings: EmbeddingSettings, records: list[PageRecord]
) -> list[bytes]:
    """Encode a batch of articles; mean-pool chunk vectors per article."""
    import numpy as np

    chunks: list[str] = []
    spans: list[tuple[int, int]] = []
    for record in records:
        # Title is prepended once so it contributes to the article vector.
        article_chunks = chunk_text(
            model.tokenizer,
            f"{record.title}\n\n{record.text}",
            settings.max_tokens,
            settings.document_prefix,
        )
        spans.append((len(chunks), len(article_chunks)))
        chunks.extend(article_chunks)

    vectors = model.encode(
        chunks,
        batch_size=settings.encode_batch_size,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )

    blobs: list[bytes] = []
    for start, count in spans:
        pooled = vectors[start : start + count].mean(axis=0)
        norm = float(np.linalg.norm(pooled))
        if norm > 0:
            pooled = pooled / norm
        blobs.append(pooled.astype(np.float32).tobytes())
    return blobs


def flush_batch(
    conn: sqlite3.Connection,
    model,
    settings: EmbeddingSettings,
    batch: list[PageRecord],
) -> None:
    """Encode a batch and commit rows + per-shard progress in one transaction."""
    if not batch:
        return
    blobs = embed_articles(model, settings, batch)

    per_shard_max: dict[str, int] = {}
    for record in batch:
        per_shard_max[record.shard] = max(
            per_shard_max.get(record.shard, 0), record.page_id
        )

    with conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO embeddings
                (page_id, rev_id, title, url, embedding)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (r.page_id, r.rev_id, r.title, r.url, blob)
                for r, blob in zip(batch, blobs)
            ],
        )
        # last_page_id advances atomically with the rows it describes.
        for shard, max_page_id in per_shard_max.items():
            conn.execute(
                "UPDATE shard_progress SET last_page_id = MAX(last_page_id, ?) "
                "WHERE shard = ?",
                (max_page_id, shard),
            )


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def pick_directory(base: Path, question: str) -> Path:
    """Prompt with base's subdirectories as indexable options; any path allowed."""
    subdirs = sorted(p for p in base.iterdir() if p.is_dir()) if base.is_dir() else []
    if subdirs:
        chosen = prompt(
            question,
            default=str(subdirs[0]),
            options=[str(p) for p in subdirs],
            allow_index=True,
            show_options=True,
            validate_path=True,
        )
    else:
        chosen = prompt(question, validate_path=True)
    path = Path(chosen)
    if not path.is_dir():
        raise RuntimeError(f"Not a directory: {path}")
    return path


def configure_interactively() -> JobConfig:
    """Prompt for archive, model, and output database; create/validate the DB."""
    archive = pick_directory(ARCHIVES_DIR, "Which wiki archive?")
    model = pick_directory(MODELS_DIR, "Which embedding model?")

    default_db = PROCESSED_DIR / f"embeddings-{archive.name}-{model.name}.sqlite"
    database = Path(
        prompt("Output database", default=str(default_db), validate_path=True)
    )

    if database.exists():
        meta = validate_database(database)
        # Guard against accidentally mixing two different jobs in one file.
        if Path(meta["archive_path"]) != archive or Path(meta["model_path"]) != model:
            raise RuntimeError(
                f"Existing database {database} was created for "
                f"archive={meta['archive_path']} model={meta['model_path']}; "
                "choose a different output file or matching inputs."
            )
        print(f"Resuming existing database: {database}")
    else:
        create_database(database, archive, model)
        print(f"Created database: {database}")

    return JobConfig(archive=archive, model=model, database=database)


def load_job(database: Path) -> JobConfig:
    """Build a JobConfig from an existing database's metadata (resume mode)."""
    database = database.expanduser().resolve()
    if not database.is_file():
        raise RuntimeError(f"Database does not exist: {database}")
    meta = validate_database(database)

    archive = Path(meta["archive_path"])
    model = Path(meta["model_path"])
    if not archive.is_dir():
        raise RuntimeError(f"Archive directory from metadata not found: {archive}")
    if not model.is_dir():
        raise RuntimeError(f"Model directory from metadata not found: {model}")

    print(f"Loaded job from {database}")
    print(f"  archive: {archive}")
    print(f"  model:   {model}")
    return JobConfig(archive=archive, model=model, database=database)


# --------------------------------------------------------------------------
# Job runner
# --------------------------------------------------------------------------

def format_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def update_postfix(bar: tqdm, articles_written: int, last_title: str) -> None:
    """Show article count, absolute completion ETA, and the latest title."""
    parts = [f"{articles_written} articles"]
    # tqdm already shows time remaining; add a wall-clock finish estimate,
    # which is easier to reason about for multi-day jobs.
    rate = bar.format_dict.get("rate")
    if rate and bar.total:
        finish = datetime.now() + timedelta(seconds=(bar.total - bar.n) / rate)
        parts.append(f"eta {finish.strftime('%a %b %d %H:%M')}")
    if last_title:
        parts.append(f"last: {last_title[:40]}")
    bar.set_postfix_str(", ".join(parts))


def run_job(config: JobConfig) -> None:
    """Parse shards in worker processes, embed and store in the main process."""
    shards = sorted(config.archive.glob("*.xml.bz2"))
    if not shards:
        raise RuntimeError(f"No .xml.bz2 shards found in {config.archive}")

    conn = open_connection(config.database)
    try:
        model, device, dim, settings = load_model(config.model)

        # Persist/verify all vector-affecting settings before processing.
        # Resuming with a changed chunk limit or instruction prefix would
        # silently create incompatible vectors in the same database.
        meta = read_metadata(conn)
        stored_dim = meta.get("embedding_dim")
        if stored_dim is not None and int(stored_dim) != dim:
            raise RuntimeError(
                f"Database stores {stored_dim}-dim embeddings but model produces {dim}"
            )
        embedding_settings = {
            "embedding_strategy": "token-chunks-mean-pool-v2",
            "embedding_dim": str(dim),
            "max_content_tokens": str(settings.max_tokens),
            "document_prefix": settings.document_prefix,
        }
        existing_rows = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        incompatible = {
            key: (meta[key], value)
            for key, value in embedding_settings.items()
            if key in meta and meta[key] != value
        }
        missing = set(embedding_settings) - set(meta)
        if incompatible or (existing_rows and missing):
            details = ", ".join(
                f"{key}={old!r} (current {new!r})"
                for key, (old, new) in incompatible.items()
            )
            if missing:
                details = f"{details}; missing metadata: {', '.join(sorted(missing))}"
            raise RuntimeError(
                "Database uses a different or legacy embedding strategy; "
                f"start a new database to avoid mixing incompatible vectors ({details})."
            )
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
                embedding_settings.items(),
            )
            conn.executemany(
                "INSERT OR IGNORE INTO shard_progress (shard) VALUES (?)",
                [(shard.name,) for shard in shards],
            )

        # Work out what is left to do.
        progress = {
            shard: (bool(completed), last_page_id)
            for shard, completed, last_page_id in conn.execute(
                "SELECT shard, completed, last_page_id FROM shard_progress"
            )
        }
        pending = [s for s in shards if not progress[s.name][0]]
        if not pending:
            total_rows = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
            print(f"All {len(shards)} shard(s) already completed ({total_rows} articles).")
            return

        cpu_count = os.process_cpu_count() or os.cpu_count() or 1
        workers = max(1, min(len(pending), cpu_count - 2))
        print(f"Hardware concurrency: {cpu_count} CPUs")
        print(f"Parser processes:     {workers} (1 per shard, main process encodes/writes)")
        print(f"Shards: {len(pending)} pending of {len(shards)} total")

        ctx = mp.get_context("spawn")
        out_queue: mp.Queue = ctx.Queue(maxsize=QUEUE_MAX_MESSAGES)
        total_bytes = sum(s.stat().st_size for s in pending)

        start = time.perf_counter()
        articles_written = 0
        last_title = ""
        batch: list[PageRecord] = []
        active: dict[str, Path] = {s.name: s for s in pending}
        procs: dict[str, mp.process.BaseProcess] = {}
        reported: dict[str, int] = {s.name: 0 for s in pending}
        waiting = list(pending)

        def start_next_workers() -> None:
            while waiting and len(procs) < workers:
                shard = waiting.pop(0)
                proc = ctx.Process(
                    target=parse_shard,
                    args=(str(shard), progress[shard.name][1], out_queue),
                    daemon=True,
                )
                proc.start()
                procs[shard.name] = proc

        start_next_workers()

        with tqdm(
            total=total_bytes,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            desc="Embedding",
            dynamic_ncols=True,
        ) as bar:
            try:
                while active:
                    try:
                        message = out_queue.get(timeout=5)
                    except queue_module.Empty:
                        # Detect workers that died without reporting.
                        for shard_name, proc in list(procs.items()):
                            if shard_name in active and proc.exitcode not in (None, 0):
                                raise RuntimeError(
                                    f"Worker for {shard_name} died with exit code "
                                    f"{proc.exitcode}"
                                )
                        continue

                    kind = message[0]
                    if kind == "page":
                        batch.append(PageRecord(*message[1:]))
                        if len(batch) >= ARTICLES_PER_FLUSH:
                            flush_batch(conn, model, settings, batch)
                            articles_written += len(batch)
                            last_title = batch[-1].title
                            update_postfix(bar, articles_written, last_title)
                            batch = []
                    elif kind == "progress":
                        _, shard_name, delta = message
                        reported[shard_name] += delta
                        bar.update(delta)
                        update_postfix(bar, articles_written, last_title)
                    elif kind == "done":
                        _, shard_name = message
                        # Flush before marking complete so the flag is truthful.
                        flush_batch(conn, model, settings, batch)
                        articles_written += len(batch)
                        batch = []
                        with conn:
                            conn.execute(
                                "UPDATE shard_progress SET completed = 1 WHERE shard = ?",
                                (shard_name,),
                            )
                        shard_path = active.pop(shard_name)
                        bar.update(shard_path.stat().st_size - reported[shard_name])
                        proc = procs.pop(shard_name)
                        proc.join()
                        start_next_workers()
                        tqdm.write(f"Completed shard {shard_name}")
                    elif kind == "error":
                        _, shard_name, error = message
                        raise RuntimeError(f"Worker for {shard_name} failed: {error}")

                flush_batch(conn, model, settings, batch)
                articles_written += len(batch)
                batch = []
            finally:
                for proc in procs.values():
                    if proc.is_alive():
                        proc.terminate()
                    proc.join()

        duration = time.perf_counter() - start
        total_rows = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        rate = articles_written / duration if duration > 0 else 0.0

        print("\nEmbedding run complete.")
        print(f"  articles embedded this run: {articles_written}")
        print(f"  total articles in database: {total_rows}")
        print(f"  device: {device}, dimensions: {dim}")
        print(f"  duration: {format_duration(duration)} ({rate:.1f} articles/s)")
        print(f"  database: {config.database}")
        print(f"  finished: {datetime.now().isoformat(timespec='seconds')}")
    finally:
        conn.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Embed Wikipedia articles from a downloaded archive into a SQLite "
            "database. With no arguments, prompts interactively. With a database "
            "path argument, resumes that job from its stored metadata."
        )
    )
    parser.add_argument(
        "database",
        nargs="?",
        type=Path,
        help="Existing embeddings .sqlite file to resume (skips prompts)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the embeddings job; return a process exit code."""
    try:
        args = parse_args(argv)
        config = load_job(args.database) if args.database else configure_interactively()
        run_job(config)
        return 0
    except KeyboardInterrupt:
        print("\nInterrupted. Re-run against the same database to resume.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
