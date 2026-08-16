#!/usr/bin/env python3
"""Search HuggingFace for agent-trace datasets and download bounded samples
with a provenance manifest — the continuous-ingest replacement for the old
AI-Traces/download_hf_traces.py (which sampled the first 10 files in listing
order and recorded nothing).

    python scripts/tracelab/refresh_corpus.py --corpus <dir> [--query "..."]
        [--max-datasets 5] [--max-files 20] [--max-bytes 2000000000] [--dry-run]

Every downloaded dataset gets `<dir>/<name>/MANIFEST.json` recording what was
fetched, when, how it was selected, and how much was skipped — so a later
mining run knows exactly what slice of the dataset it is looking at.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_QUERIES = [
    "agent trajectories",
    "agent traces",
    "tool use trajectories",
    "swe agent trajectories",
    "multi-agent traces",
]

# Only formats the tracelab scan adapters can actually read.
DATA_SUFFIXES = (".jsonl", ".parquet")


def search_datasets(queries: list[str], limit_per_query: int) -> list[Any]:
    from huggingface_hub import HfApi

    api = HfApi()
    seen: dict[str, Any] = {}
    for query in queries:
        for info in api.list_datasets(search=query, sort="last_modified", limit=limit_per_query):
            seen.setdefault(info.id, info)
    return sorted(seen.values(), key=lambda i: str(i.last_modified or ""), reverse=True)


def download_dataset(
    repo_id: str, corpus: Path, max_files: int, max_bytes: int, dry_run: bool
) -> dict[str, Any]:
    from huggingface_hub import HfApi, hf_hub_download

    api = HfApi()
    manifest: dict[str, Any] = {
        "repo_id": repo_id,
        "downloaded_at": datetime.now(UTC).isoformat(),
        "selection_rule": (
            f"data files by size ascending, max {max_files} files, max {max_bytes} bytes total"
        ),
        "files": [],
        "skipped_files": 0,
        "errors": [],
    }
    try:
        infos = api.list_repo_tree(repo_id, repo_type="dataset", recursive=True)
        data_files = [
            f
            for f in infos
            if getattr(f, "size", None) is not None and f.path.endswith(DATA_SUFFIXES)
        ]
    except Exception as exc:  # noqa: BLE001 — gated/missing repos are expected
        manifest["errors"].append(f"list failed: {exc}")
        return manifest

    # Size-ascending keeps the sample diverse under the byte budget instead of
    # whatever alphabetical order happens to yield.
    data_files.sort(key=lambda f: f.size)
    target_dir = corpus / repo_id.split("/")[-1]
    spent = 0
    for file_info in data_files:
        if len(manifest["files"]) >= max_files or spent + file_info.size > max_bytes:
            manifest["skipped_files"] += 1
            continue
        if dry_run:
            manifest["files"].append({"path": file_info.path, "size": file_info.size, "dry": True})
            spent += file_info.size
            continue
        try:
            hf_hub_download(
                repo_id=repo_id,
                filename=file_info.path,
                repo_type="dataset",
                local_dir=target_dir,
            )
            manifest["files"].append({"path": file_info.path, "size": file_info.size})
            spent += file_info.size
        except Exception as exc:  # noqa: BLE001
            manifest["errors"].append(f"{file_info.path}: {exc}")

    manifest["bytes_downloaded"] = spent
    if not dry_run and manifest["files"]:
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "MANIFEST.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True, help="HF-Datasets corpus root")
    parser.add_argument("--query", action="append", default=[], help="search query (repeatable)")
    parser.add_argument("--max-datasets", type=int, default=5)
    parser.add_argument("--max-files", type=int, default=20)
    parser.add_argument("--max-bytes", type=int, default=2_000_000_000)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    queries = args.query or DEFAULT_QUERIES
    already = (
        {p.name for p in args.corpus.iterdir() if p.is_dir()} if args.corpus.exists() else set()
    )
    candidates = [
        info
        for info in search_datasets(queries, limit_per_query=20)
        if info.id.split("/")[-1] not in already
    ]
    print(f"{len(candidates)} new candidate datasets (have {len(already)})")

    results = []
    for info in candidates[: args.max_datasets]:
        print(f"→ {info.id} (modified {info.last_modified})")
        manifest = download_dataset(
            info.id, args.corpus, args.max_files, args.max_bytes, args.dry_run
        )
        print(
            f"   files={len(manifest['files'])} skipped={manifest['skipped_files']} "
            f"errors={len(manifest['errors'])}"
        )
        results.append(manifest)

    summary_path = args.corpus / "refresh-log.jsonl"
    if not args.dry_run:
        with summary_path.open("a") as handle:
            for manifest in results:
                handle.write(json.dumps(manifest) + "\n")
        print(f"appended {len(results)} manifests to {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
