"""``enterprise-agent build-index`` — 增量构建值向量索引，可断点续跑。

Phase 4A adds selective indexing: ``--tables`` / ``--columns`` form a strict
whitelist, ``--exclude-columns`` removes fields, a sensitive-field registry
always wins, ``--max-values`` / ``--max-total-values`` / ``--max-requests`` cap
the work, and ``--dry-run`` reports the plan without embedding anything.

A build that covers only part of the text columns writes a *partial* manifest
(``index_kind="partial"``) with the real covered tables/columns, database
fingerprint, embedding model and policy. It never fabricates an empty manifest
to fake readiness. Legacy full builds keep the original manifest shape plus the
new provenance fields.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha1, sha256
import json
from pathlib import Path
import time

import numpy as np
from app.config import get_config
from app.dataset import load_dataset
from app.db_utils import load_table_names, load_column_names_and_types, execute_sql_without_cache
from app.vector_db.vector_db import get_embedding_function, _is_text_column_type, _is_uuid_column, _is_number_column
from app.vector_db.local_index import (
    canonical_embedding_signature,
    embedding_signature,
    get_local_index_path,
    write_local_index_column,
    write_local_index_manifest,
)
from app.vector_db.value_index_policy import ValueIndexPolicy, split_csv


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=None)
    parser.add_argument("--reuse-legacy-columns", action="store_true")
    parser.add_argument("--database", help="直接构建单个 SQLite 库，而不是读数据集快照")
    parser.add_argument("--tables", default="", help="逗号分隔的表白名单（支持 * 通配，表名可带 Schema 前缀）")
    parser.add_argument("--columns", default="", help="逗号分隔的列白名单，可写 Table.Column 或裸列名")
    parser.add_argument("--exclude-columns", default="", help="逗号分隔的排除列，可写 Table.Column 或裸列名")
    parser.add_argument("--max-values", type=int, default=None, help="单列最大不同值数量（默认 2000）")
    parser.add_argument("--max-total-values", type=int, default=None, help="全部列累计最大值数量（默认 200000）")
    parser.add_argument("--max-requests", type=int, default=None, help="embedding 请求上限（按 batch 估算）")
    parser.add_argument("--include-all", action="store_true", help="显式允许全库构建（默认必须给白名单）")
    parser.add_argument("--dry-run", action="store_true", help="只统计待索引字段与预算，不做任何 embedding")


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _build_policy(args: argparse.Namespace, database_id: str) -> ValueIndexPolicy:
    return ValueIndexPolicy(
        database_id=database_id,
        allowed_tables=split_csv(args.tables),
        allowed_columns=split_csv(args.columns),
        excluded_columns=split_csv(args.exclude_columns),
        max_distinct_values_per_column=args.max_values or 2000,
        max_total_values=args.max_total_values or 200_000,
        max_embedding_requests=args.max_requests or 60_000,
    )


def _candidate_text_columns(db: Path) -> list[tuple[str, str, str]]:
    """Return ``(table, column, dtype)`` for every text column in the database."""
    candidates: list[tuple[str, str, str]] = []
    for table in load_table_names(str(db)):
        for name, dtype in load_column_names_and_types(str(db), table):
            if _is_text_column_type(dtype):
                candidates.append((table, name, dtype))
    return candidates


def _plan(db: Path, policy: ValueIndexPolicy, candidates: list[tuple[str, str, str]]) -> tuple[list[dict], list[dict]]:
    """Split candidates into included and excluded lists with explicit reasons."""
    included: list[dict] = []
    excluded: list[dict] = []
    for table, column, dtype in candidates:
        include, reason = policy.evaluate(table, column)
        record = {"table": table, "column": column, "dtype": dtype}
        if include:
            included.append(record)
        else:
            record["reason"] = reason
            excluded.append(record)
    return included, excluded


def _dry_run_report(db: Path, policy: ValueIndexPolicy, included: list[dict], excluded: list[dict], batch_size: int) -> dict:
    """Count DISTINCT values for included columns without embedding anything."""
    rows: list[dict] = []
    total_values = 0
    for record in included:
        table, column = record["table"], record["column"]
        query = (
            f"SELECT COUNT(DISTINCT {_quote(column)}) FROM {_quote(table)} "
            f"WHERE {_quote(column)} IS NOT NULL "
            f"AND LENGTH(CAST({_quote(column)} AS TEXT)) <= {policy.max_value_length}"
        )
        result = execute_sql_without_cache(str(db), query, timeout=300)
        if result.result_rows is None:
            raise RuntimeError(result.error_message)
        distinct = int(result.result_rows[0][0]) if result.result_rows else 0
        capped = min(distinct, policy.max_distinct_values_per_column)
        total_values += capped
        rows.append({
            "table": table,
            "column": column,
            "distinct_values": distinct,
            "indexed_values_after_cap": capped,
        })
    estimated_requests = policy.estimated_requests(total_values, batch_size)
    return {
        "database": db.stem,
        "database_path": str(db),
        "database_sha256": sha256(db.read_bytes()).hexdigest(),
        "dry_run": True,
        "policy": policy.to_dict(),
        "included_columns": rows,
        "included_column_count": len(rows),
        "excluded_columns": excluded,
        "excluded_column_count": len(excluded),
        "total_values_after_cap": total_values,
        "batch_size": batch_size,
        "estimated_embedding_requests": estimated_requests,
        "within_max_total_values": total_values <= policy.max_total_values,
        "within_max_requests": estimated_requests <= policy.max_embedding_requests,
        "note": "No embedding performed. Counts are read-only DISTINCT scans.",
    }


def run(args: argparse.Namespace) -> int:
    from .common import resolve_config

    resolve_config(args.config)
    config = get_config()
    cfg = config.vector_database_config
    if cfg.build_backend != "local_index":
        raise ValueError("This resumable builder supports build_backend=local_index only")
    embedding_config = {k: getattr(cfg, k) for k in (
        "api_type", "embedding_model_name_or_path", "use_qwen3_embedding",
        "normalize_embeddings", "max_value_length", "lower_meta_data")}
    # Single source of truth: the runtime guard hashes exactly these fields.
    signature = embedding_signature(embedding_config)
    signature_v2 = canonical_embedding_signature(embedding_config)
    batch_size = int(config.run_config.embedding_batch_size)

    databases = [args.database] if args.database else load_dataset(config.dataset_config.save_path).get_all_database_paths()

    # A real build must be explicitly scoped; dry-run may inspect anything.
    if not args.dry_run and not (args.tables or args.columns) and not args.include_all:
        raise ValueError(
            "Refusing a full-database value-index build: pass --tables/--columns, "
            "or --include-all to opt into indexing every non-sensitive text column."
        )

    embedding = None
    if not args.dry_run:
        embedding = get_embedding_function(
            cfg.embedding_model_name_or_path, api_type=cfg.api_type,
            use_qwen3_embedding=cfg.use_qwen3_embedding, local_files_only=cfg.local_files_only,
            normalize_embeddings=cfg.normalize_embeddings, base_url=cfg.base_url,
            api_key=cfg.api_key, embedding_device=cfg.embedding_device)

    for db in databases:
        db = Path(db).resolve()
        policy = _build_policy(args, db.stem)
        candidates = _candidate_text_columns(db)
        included, excluded = _plan(db, policy, candidates)

        if args.dry_run:
            report = _dry_run_report(db, policy, included, excluded, batch_size)
            print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
            continue

        index = get_local_index_path(Path(cfg.store_root_path) / db.stem)
        index.mkdir(parents=True, exist_ok=True)
        provenance = index / "embedding_provenance.json"
        if provenance.exists():
            if json.loads(provenance.read_text())["signature"] != signature:
                raise ValueError("Existing index embedding configuration differs; use a new index directory")
        elif any((index / "columns").glob("*.npy")) and not args.reuse_legacy_columns:
            raise ValueError("Existing columns have no provenance; verify their model before reusing")

        # Collect documents first so the budget is enforced before any embedding.
        collected: list[tuple[str, str, list[str]]] = []
        total_values = 0
        for record in included:
            table, column = record["table"], record["column"]
            query = (
                f"SELECT DISTINCT {_quote(column)} FROM {_quote(table)} "
                f"WHERE {_quote(column)} IS NOT NULL "
                f"AND LENGTH(CAST({_quote(column)} AS TEXT)) <= {policy.max_value_length} "
                f"LIMIT {policy.max_distinct_values_per_column}"
            )
            result = execute_sql_without_cache(str(db), query, timeout=600)
            if result.result_rows is None:
                raise RuntimeError(result.error_message)
            docs = [str(row[0]) for row in result.result_rows]
            if not docs or _is_uuid_column(docs) or _is_number_column(docs):
                continue
            total_values += len(docs)
            if total_values > policy.max_total_values:
                raise ValueError(
                    f"Value budget exceeded for {db.stem}: {total_values} > max_total_values="
                    f"{policy.max_total_values}. Narrow --tables/--columns or raise --max-total-values."
                )
            collected.append((table, column, docs))

        estimated = policy.estimated_requests(total_values, batch_size)
        if estimated > policy.max_embedding_requests:
            raise ValueError(
                f"Embedding request budget exceeded for {db.stem}: {estimated} > max_requests="
                f"{policy.max_embedding_requests}. Narrow the whitelist or raise --max-requests."
            )

        entries = []
        text_cache: dict[str, np.ndarray] = {}
        for table, column, docs in collected:
            t, c = (table.lower(), column.lower()) if cfg.lower_meta_data else (table, column)
            stem = sha1(f"{t}\t{c}".encode()).hexdigest()
            npy = index / "columns" / f"{stem}.embeddings.npy"
            txt = index / "columns" / f"{stem}.documents.json"
            if npy.exists() and txt.exists() and json.loads(txt.read_text(encoding="utf-8")) == docs:
                vectors = np.load(npy, allow_pickle=False)
                if vectors.ndim != 2 or vectors.shape[0] != len(docs) or not np.isfinite(vectors).all():
                    raise ValueError(f"Invalid cached vectors: {npy}")
                text_cache.update(zip(docs, vectors))
                print(f"Reused {t}.{c}: {len(docs)} values", flush=True)
            else:
                missing = [d for d in docs if d not in text_cache]
                started = time.perf_counter()
                for offset in range(0, len(missing), batch_size):
                    batch = missing[offset:offset + batch_size]
                    text_cache.update(zip(batch, embedding(batch)))
                    if offset == 0 or offset % 1024 == 0:
                        print(f"Embedding {t}.{c}: {min(offset + len(batch), len(missing))}/{len(missing)}, {time.perf_counter()-started:.1f}s", flush=True)
                vectors = np.asarray([text_cache[d] for d in docs])
            entries.append(write_local_index_column(index, t, c, docs, vectors))

        db_sha256 = sha256(db.read_bytes()).hexdigest()
        meta = {
            "index_kind": "complete" if not excluded else "partial",
            "covered_tables": sorted({entry["table_name"] for entry in entries}),
            "covered_column_keys": [entry["column_key"] for entry in entries],
            "covered_column_count": len(entries),
            "database_fingerprint": db_sha256,
            "embedding_model": cfg.embedding_model_name_or_path,
            "embedding_signature": signature,
            "embedding_signature_v2": signature_v2,
            "signature_version": 2,
            "embedding_dim": entries[0]["embedding_dim"] if entries else 0,
            "policy_version": policy.policy_version,
            "policy": policy.to_dict(),
            "excluded_columns": excluded,
            "built_at": datetime.now(timezone.utc).isoformat(),
        }
        write_local_index_manifest(index, entries, meta=meta)
        provenance.write_text(json.dumps({
            "signature": signature,
            "config": embedding_config,
            "database_sha256": db_sha256,
        }), encoding="utf-8")
        (index.parent / "success_flag").touch()
        print(f"Complete: {db.stem}, {len(entries)} columns, kind={meta['index_kind']}, "
              f"covered_tables={len(meta['covered_tables'])}, excluded={len(excluded)}", flush=True)
    return 0
