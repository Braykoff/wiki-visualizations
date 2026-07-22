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

## Processing

### Generating embeddings

Once an archive and a model are downloaded, generate article embeddings with:

```bash
uv run embeddings
```

You will be prompted for:
1. **Wiki archive** (default: first directory in `../data/archives/`): The downloaded archive to process.
2. **Embedding model** (default: first directory in `../data/models/`): The downloaded model to embed with.
3. **Output database** (default: `../data/processed/embeddings-<archive>-<model>.sqlite`): A SQLite file containing the job metadata, per-shard progress, and an `embeddings` table with `page_id`, `rev_id`, `title`, `url`, and `embedding` (float32 blob) columns.

Articles are parsed from the archive shards by worker processes (sized from hardware concurrency) while the main process encodes batches on the best available device (CUDA/MPS/CPU) and writes them to the database. Each batch commits transactionally, so interrupting the run loses at most one batch. Article chunks are sized from the selected model's tokenizer and context limit, not a fixed character count. Nomic Embed Text uses its documented `search_document:` prefix for every document chunk and automatically uses smaller encode batches for its longer inputs.

To resume an interrupted job, pass the database path:

```bash
uv run embeddings ../data/processed/embeddings-enwiki-2026-07-01-BAAI--bge-base-en-v1.5.sqlite
```

This reads the archive and model from the database metadata and continues from the last committed article in each shard.

### Sharing an embedding run across computers

Each computer must use its own output database; do not place one SQLite database
on a shared drive and have multiple computers write to it. During interactive
setup, enter comma-separated relative compute shares, then select this
computer's worker number:

```text
Work shares across computers (comma-separated percentages or weights) [100]: 70,30
Which worker is this computer? [1]: 1
```

Run the second computer with the same `70,30` shares and select worker `2`.
For a 40% / 30% / 30% split, each computer uses `40,30,30` and selects its
own worker number. The script assigns whole XML shards deterministically by
compressed size, balancing them toward the requested shares without duplicate
work. Worker databases default to names such as
`embeddings-<archive>-<model>-worker-1-of-2.sqlite`; keep them separate until
the results are merged or consumed together by a later processing step.
