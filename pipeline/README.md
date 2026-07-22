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
uv run download-archive
```

You will be prompted for:

1. **Archive** (default: `enwiki`): The Wikipedia archive to download.
2. **Dump date** (default: most recent available): The date of the archive to download.
3. **Target directory** (default: `../data/archives/<archive>-<date>/`): Where to download the archive to.

The script creates the target directory, writes `wikiviz-download-info.txt` with the chosen options, downloads all files from the dump’s `xml/bzip2/` listing, then verifies checksums against `SHA256SUMS`.

If a download fails or you delete some shards, re-run against the same target directory:

```bash
uv run download-archive ../data/archives/enwiki-2026-07-01
```

This loads options from `wikiviz-download-info.txt` in that directory. Only missing and incomplete files are downloaded; complete files are skipped. Checksums are verified again at the end.

## Downloading models

Embedding models can be downloaded from [Hugging Face](https://huggingface.co/) using the `download-model` command:

```bash
uv run download-model
```

You will be prompted for:
1. **Model** (default: `BAAI/bge-base-en-v1.5`): The model to download.
2. **Target directory** (default: `../data/models/<model-name>`): Where to download the model to. This directory must be empty.

After downloading the model, it will be loaded and a test sentence will be embedded to verify the download was successful.
