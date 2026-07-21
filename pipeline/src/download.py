#!/usr/bin/env python3
"""Interactive CLI to download Wikimedia mediawiki_content_current dumps."""

from __future__ import annotations

import hashlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

from tqdm import tqdm

from cli import prompt_choice, prompt_path

BASE_URL = "https://dumps.wikimedia.org/other/mediawiki_content_current"
USER_AGENT = "wiki-visualizations-pipeline/1.0"
CHUNK_SIZE = 1024 * 1024  # 1 MiB
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Pull file size from Apache index lines: "...href... date time size"
LISTING_SIZE_RE = re.compile(
    r'href="([^"]+)"[^>]*>.*?</a>\s+\S+\s+\S+\s+(\d+|-)\s*$',
    re.IGNORECASE | re.MULTILINE,
)


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


# Resolved at import time; safe because we can climb out of the venv path.
PROJECT_ROOT = find_project_root()


@dataclass(frozen=True)
class ListingEntry:
    """One row from a dumps directory index (name + optional byte size)."""

    name: str
    size: int | None  # None when the index shows "-" (directory) or omits size


class DirectoryListingParser(HTMLParser):
    """Extract href names from an Apache-style directory index."""

    def __init__(self) -> None:
        super().__init__()
        self.entries: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # Collect only real link targets; skip parent/self navigation.
        if tag != "a":
            return
        href = dict(attrs).get("href")
        if not href or href in ("./", "../"):
            return
        name = urllib.parse.unquote(href.rstrip("/"))
        if name and name not in (".", ".."):
            self.entries.append(name)


def fetch_bytes(url: str, timeout: int = 60) -> bytes:
    """GET a URL and return the raw response body, or raise RuntimeError."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} fetching {url}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Failed to fetch {url}: {exc.reason}") from exc


def fetch_text(url: str) -> str:
    """GET a URL and decode it as UTF-8 text."""
    return fetch_bytes(url).decode("utf-8", errors="replace")


def url_exists(url: str) -> bool:
    """Return True if the URL responds successfully (HEAD, with GET fallback)."""
    request = urllib.request.Request(
        url, method="HEAD", headers={"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return 200 <= response.status < 400
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 405):
            # Some dumps hosts reject HEAD; fall back to a tiny GET.
            try:
                fetch_text(url)
                return True
            except RuntimeError:
                return False
        return False
    except urllib.error.URLError:
        return False


def parse_directory_listing(html: str) -> list[ListingEntry]:
    """Parse an Apache index page into unique ListingEntry rows with sizes."""
    # Names come from <a href>; sizes from the trailing column on each line.
    parser = DirectoryListingParser()
    parser.feed(html)

    sizes: dict[str, int | None] = {}
    for match in LISTING_SIZE_RE.finditer(html):
        raw_href, raw_size = match.group(1), match.group(2)
        if raw_href in ("./", "../"):
            continue
        name = urllib.parse.unquote(raw_href.rstrip("/"))
        sizes[name] = None if raw_size == "-" else int(raw_size)

    # Deduplicate while preserving listing order.
    seen: set[str] = set()
    unique: list[ListingEntry] = []
    for name in parser.entries:
        if name in seen:
            continue
        seen.add(name)
        unique.append(ListingEntry(name=name, size=sizes.get(name)))
    return unique


def list_directory(url: str) -> list[ListingEntry]:
    """Fetch and parse a remote directory index URL."""
    html = fetch_text(url if url.endswith("/") else f"{url}/")
    return parse_directory_listing(html)


def format_bytes(num_bytes: int) -> str:
    """Pretty-print a byte count using binary units (KiB, MiB, ...)."""
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    size = float(num_bytes)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{num_bytes} B"


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


def parse_sha256sums(text: str) -> dict[str, str]:
    """Parse a SHA256SUMS file into {filename: hex_digest}."""
    checksums: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # GNU coreutils format: "<hash>  <filename>" or "<hash> *<filename>"
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        digest, name = parts
        name = name[1:] if name.startswith("*") else name
        checksums[name] = digest.lower()
    return checksums


def download_file(url: str, dest: Path, overall: tqdm) -> int:
    """Stream one file to disk, updating per-file and overall progress bars."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=300) as response:
        # Prefer Content-Length so tqdm can show ETA for this file.
        total = response.length
        if total is None:
            content_length = response.headers.get("Content-Length")
            total = int(content_length) if content_length else None

        written = 0
        with (
            open(dest, "wb") as out,
            tqdm(
                total=total,
                unit="B",
                unit_scale=True,
                unit_divisor=1024,
                desc=dest.name[:40],
                leave=False,
                miniters=1,
                dynamic_ncols=True,
            ) as bar,
        ):
            while True:
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                out.write(chunk)
                written += len(chunk)
                bar.update(len(chunk))
                overall.update(len(chunk))
    return written


def sha256_file(path: Path) -> str:
    """Compute the SHA-256 hex digest of a local file."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


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


def select_archive() -> str:
    """Prompt for which wiki dump to download (default: enwiki)."""
    print(f"Fetching archives from {BASE_URL}/ ...")
    entries = list_directory(f"{BASE_URL}/")
    archives = sorted(
        e.name for e in entries if e.name != "readme.html" and not e.name.endswith(".html")
    )
    if not archives:
        raise RuntimeError(f"No archives found at {BASE_URL}/")
    if "enwiki" not in archives:
        raise RuntimeError("Default archive 'enwiki' was not found in the listing.")

    print(f"Found {len(archives)} archives (default: enwiki).")
    return prompt_choice("Which archive do you want to download?", "enwiki", set(archives))


def select_date(archive: str) -> str:
    """Prompt for dump date under an archive (default: most recent)."""
    archive_url = f"{BASE_URL}/{archive}/"
    print(f"Fetching dump dates from {archive_url} ...")
    entries = list_directory(archive_url)
    dates = sorted(e.name for e in entries if DATE_RE.match(e.name))
    if not dates:
        raise RuntimeError(f"No dump dates found at {archive_url}")

    default = dates[-1]  # most recent YYYY-MM-DD
    print("Available dates:")
    for date in dates:
        marker = " (most recent)" if date == default else ""
        print(f"  - {date}{marker}")
    return prompt_choice("Which dump date?", default, set(dates))


def main() -> int:
    """Run the interactive download flow; return a process exit code."""
    try:
        # --- Choose archive + dump date ---
        archive = select_archive()
        dump_date = select_date(archive)

        # --- Confirm the xml/bzip2 dump route exists ---
        dump_url = f"{BASE_URL}/{archive}/{dump_date}/xml/bzip2/"
        print(f"Checking dump route: {dump_url}")
        if not url_exists(dump_url):
            raise RuntimeError(f"Dump route does not exist: {dump_url}")

        # --- Resolve an empty target directory ---
        default_target = PROJECT_ROOT / "data" / "downloads" / f"{archive}-{dump_date}"
        target = prompt_path("Target directory", default_target)
        ensure_empty_directory(target)

        # --- Discover files to download ---
        print(f"Listing files at {dump_url} ...")
        entries = [e for e in list_directory(dump_url) if e.name]
        if not entries:
            raise RuntimeError(f"No files listed at {dump_url}")

        # Prefer downloading checksums first so verification can fail early if missing.
        entries = sorted(entries, key=lambda e: (e.name != "SHA256SUMS", e.name))
        known_total = sum(e.size or 0 for e in entries)
        unknown = sum(1 for e in entries if e.size is None)

        print(f"Downloading {len(entries)} files to {target}")
        if known_total:
            approx = format_bytes(known_total)
            suffix = f" (+ {unknown} files of unknown size)" if unknown else ""
            print(f"Approximate total size from listing: {approx}{suffix}")

        # --- Download every listed file with progress ---
        start = time.perf_counter()
        sizes: dict[str, int] = {}

        with tqdm(
            total=known_total or None,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            desc="Overall",
            dynamic_ncols=True,
            bar_format=(
                "{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]"
                if known_total
                else "{l_bar}{bar}| {n_fmt} [{elapsed}, {rate_fmt}]"
            ),
        ) as overall:
            for entry in entries:
                url = urllib.parse.urljoin(dump_url, urllib.parse.quote(entry.name))
                dest = target / entry.name
                overall.set_postfix_str(entry.name[:48], refresh=False)
                sizes[entry.name] = download_file(url, dest, overall)

        # --- Verify SHA-256 checksums ---
        checksum_path = target / "SHA256SUMS"
        if not checksum_path.is_file():
            raise RuntimeError(f"Checksum file does not exist: {checksum_path}")

        expected = parse_sha256sums(checksum_path.read_text(encoding="utf-8"))
        if not expected:
            raise RuntimeError(f"Checksum file is empty or unreadable: {checksum_path}")

        print("\nVerifying SHA-256 checksums...")
        for name, expected_digest in expected.items():
            path = target / name
            if not path.is_file():
                raise RuntimeError(f"Missing file required by SHA256SUMS: {name}")
            actual = sha256_file(path)
            if actual != expected_digest:
                raise RuntimeError(
                    f"Checksum mismatch for {name}: expected {expected_digest}, got {actual}"
                )
            print(f"  OK  {name}")

        # --- Success summary ---
        duration = time.perf_counter() - start
        total_size = sum(sizes.values())
        downloaded = sorted(sizes)

        print("\nDownload complete.")
        print(f"Files ({len(downloaded)}):")
        for name in downloaded:
            print(f"  - {name} ({format_bytes(sizes[name])})")
        print(f"Total size: {format_bytes(total_size)}")
        print(f"Total duration: {format_duration(duration)}")
        print(f"Finished at: {datetime.now().isoformat(timespec='seconds')}")
        return 0
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
