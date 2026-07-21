# Wiki-Visualization Pipeline

Scripts for downloading, parsing, and processing Wikipedia archives for visualization.

Currently this package downloads [Wikimedia `mediawiki_content_current`](https://dumps.wikimedia.org/other/mediawiki_content_current/) dumps (XML bzip2 shards), verifies SHA-256 checksums, and can resume interrupted downloads.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.14+.

```bash
cd pipeline
uv sync
```

## Downloading data

Data can be downloaded interactively:

```bash
uv run download
```

You will be prompted for:

1. **Archive** (default: `enwiki`): The wikipedia archive to download.
2. **Dump date** (default: most recent available): The date of the archive to download.
3. **Target directory** (default: `../data/downloads/<archive>-<date>/`): Where to download the archive to.

The script creates the target directory, writes `wikiviz-download-info.txt` with the chosen options, downloads all files from the dump’s `xml/bzip2/` listing, then verifies checksums against `SHA256SUMS`.

If a download fails or you delete some shards, re-run against the same target directory:

```bash
uv run download ../data/downloads/enwiki-2026-07-01
```

This loads options from `wikiviz-download-info.txt` in that directory. Only missing and incomplete files are downloaded; complete files are skipped. Checksums are verified again at the end.
