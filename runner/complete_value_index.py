"""Complete a local value index without discarding completed column files.

Legacy files can only be reused with --reuse-legacy-columns, after verifying
that they were generated with the current embedding configuration.
"""
import argparse
from hashlib import sha1, sha256
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from app.config import get_config
from app.dataset import load_dataset
from app.db_utils import load_table_names, load_column_names_and_types, execute_sql_without_cache
from app.vector_db.vector_db import get_embedding_function, _is_text_column_type, _is_uuid_column, _is_number_column
from app.vector_db.local_index import get_local_index_path, write_local_index_column, write_local_index_manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reuse-legacy-columns", action="store_true")
    parser.add_argument("--database", help="Build one SQLite database directly instead of reading the dataset snapshot")
    args = parser.parse_args()
    config = get_config()
    cfg = config.vector_database_config
    if cfg.build_backend != "local_index":
        raise ValueError("This resumable builder supports build_backend=local_index only")
    embedding_config = {k: getattr(cfg, k) for k in (
        "api_type", "embedding_model_name_or_path", "use_qwen3_embedding",
        "normalize_embeddings", "max_value_length", "lower_meta_data")}
    signature = sha256(json.dumps(embedding_config, sort_keys=True).encode()).hexdigest()
    embedding = get_embedding_function(
        cfg.embedding_model_name_or_path, api_type=cfg.api_type,
        use_qwen3_embedding=cfg.use_qwen3_embedding, local_files_only=cfg.local_files_only,
        normalize_embeddings=cfg.normalize_embeddings, base_url=cfg.base_url,
        api_key=cfg.api_key, embedding_device=cfg.embedding_device)
    databases = [args.database] if args.database else load_dataset(config.dataset_config.save_path).get_all_database_paths()
    for db in databases:
        db = Path(db).resolve()
        index = get_local_index_path(Path(cfg.store_root_path) / db.stem)
        index.mkdir(parents=True, exist_ok=True)
        provenance = index / "embedding_provenance.json"
        if provenance.exists():
            if json.loads(provenance.read_text())["signature"] != signature:
                raise ValueError("Existing index embedding configuration differs; use a new index directory")
        elif any((index / "columns").glob("*.npy")) and not args.reuse_legacy_columns:
            raise ValueError("Existing columns have no provenance; verify their model before reusing")
        provenance.write_text(json.dumps({"signature": signature, "config": embedding_config}), encoding="utf-8")
        entries = []
        # Reuse exact text embeddings across columns within this one build.
        text_cache = {}
        for table in load_table_names(str(db)):
            for name, dtype in load_column_names_and_types(str(db), table):
                if not _is_text_column_type(dtype):
                    continue
                quoted_table = '"' + table.replace('"', '""') + '"'
                quoted_name = '"' + name.replace('"', '""') + '"'
                result = execute_sql_without_cache(str(db), f"SELECT DISTINCT {quoted_name} FROM {quoted_table} WHERE {quoted_name} IS NOT NULL AND LENGTH(CAST({quoted_name} AS TEXT)) <= {cfg.max_value_length}", timeout=600)
                if result.result_rows is None:
                    raise RuntimeError(result.error_message)
                docs = [str(r[0]) for r in result.result_rows]
                if not docs or _is_uuid_column(docs) or _is_number_column(docs):
                    continue
                t, c = (table.lower(), name.lower()) if cfg.lower_meta_data else (table, name)
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
                    for offset in range(0, len(missing), config.run_config.embedding_batch_size):
                        batch = missing[offset:offset + config.run_config.embedding_batch_size]
                        text_cache.update(zip(batch, embedding(batch)))
                        if offset == 0 or offset % 1024 == 0:
                            print(f"Embedding {t}.{c}: {min(offset + len(batch),len(missing))}/{len(missing)}, {time.perf_counter()-started:.1f}s", flush=True)
                    vectors = np.asarray([text_cache[d] for d in docs])
                entries.append(write_local_index_column(index, t, c, docs, vectors))
        write_local_index_manifest(index, entries)
        provenance.write_text(json.dumps({"signature": signature, "config": embedding_config,
            "database_sha256": sha256(db.read_bytes()).hexdigest()}), encoding="utf-8")
        (index.parent / "success_flag").touch()
        print(f"Complete: {db.stem}, {len(entries)} columns", flush=True)


if __name__ == "__main__":
    main()
