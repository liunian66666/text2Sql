from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha1, sha256
import json
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Tuple

import numpy as np
import torch


LOCAL_INDEX_DIRNAME = "local_index"
LOCAL_INDEX_MANIFEST_FILENAME = "manifest.json"
LOCAL_INDEX_COLUMNS_DIRNAME = "columns"

# Fields that decide whether two embeddings are interchangeable. They are the
# same six values ``build-index`` has always hashed, so an index built before
# Phase 4B keeps its original signature valid.
SIGNATURE_FIELDS = (
    "api_type", "embedding_model_name_or_path", "use_qwen3_embedding",
    "normalize_embeddings", "max_value_length", "lower_meta_data",
)
MANIFEST_FORMAT_VERSION = 1


def _signature_payload(embedding_config: Any, *, canonical_path: bool) -> Dict[str, Any]:
    if hasattr(embedding_config, "model_dump"):
        config = embedding_config.model_dump()
    elif isinstance(embedding_config, dict):
        config = dict(embedding_config)
    else:
        config = {key: getattr(embedding_config, key, None) for key in SIGNATURE_FIELDS}
    payload = {key: config.get(key) for key in SIGNATURE_FIELDS}
    if canonical_path:
        # Relative (config file) vs absolute (resolved at runtime) spellings of
        # the same local model directory must not look like a different model.
        model = payload.get("embedding_model_name_or_path")
        if model:
            payload["embedding_model_name_or_path"] = Path(str(model)).name
    return payload


def embedding_signature(embedding_config: Any) -> str:
    """Signature v1 — the algorithm every existing index was built with."""
    return sha256(json.dumps(_signature_payload(embedding_config, canonical_path=False),
                             sort_keys=True).encode()).hexdigest()


def canonical_embedding_signature(embedding_config: Any) -> str:
    """Signature v2 — same fields, but the model path is reduced to its name."""
    return sha256(json.dumps(_signature_payload(embedding_config, canonical_path=True),
                             sort_keys=True).encode()).hexdigest()


def get_local_index_path(vector_db_path: str | Path) -> Path:
    return Path(vector_db_path) / LOCAL_INDEX_DIRNAME


def _column_key(table_name: str, column_name: str) -> str:
    return f"{table_name}\t{column_name}"


def _normalize_embeddings(embeddings: np.ndarray) -> np.ndarray:
    if embeddings.size == 0:
        return embeddings.astype(np.float32, copy=False)

    embeddings = embeddings.astype(np.float32, copy=False)
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    norms = np.clip(norms, a_min=1e-12, a_max=None)
    return embeddings / norms


def write_local_index_column(
    local_index_path: str | Path,
    table_name: str,
    column_name: str,
    documents: List[str],
    embeddings: List[List[float]],
) -> Dict[str, Any] | None:
    if not documents:
        return None

    local_index_path = Path(local_index_path)
    columns_dir = local_index_path / LOCAL_INDEX_COLUMNS_DIRNAME
    columns_dir.mkdir(parents=True, exist_ok=True)

    column_key = _column_key(table_name, column_name)
    file_stem = sha1(column_key.encode("utf-8")).hexdigest()
    embeddings_filename = f"{file_stem}.embeddings.npy"
    documents_filename = f"{file_stem}.documents.json"

    normalized_embeddings = _normalize_embeddings(np.asarray(embeddings, dtype=np.float32))

    np.save(columns_dir / embeddings_filename, normalized_embeddings)
    with (columns_dir / documents_filename).open("w", encoding="utf-8") as file_obj:
        json.dump(documents, file_obj, ensure_ascii=True)

    return {
        "table_name": table_name,
        "column_name": column_name,
        "column_key": column_key,
        "embeddings_file": str(Path(LOCAL_INDEX_COLUMNS_DIRNAME) / embeddings_filename),
        "documents_file": str(Path(LOCAL_INDEX_COLUMNS_DIRNAME) / documents_filename),
        "count": len(documents),
        "embedding_dim": int(normalized_embeddings.shape[1]) if normalized_embeddings.ndim == 2 else 0,
    }


def write_local_index_manifest(
    local_index_path: str | Path,
    column_entries: List[Dict[str, Any]],
    meta: Dict[str, Any] | None = None,
) -> None:
    """Persist the manifest.

    ``meta`` (Phase 4A) records build provenance so a *partial* index can be
    distinguished from a complete one, and a stale/incompatible index can be
    detected without re-reading every column file. Legacy callers that omit
    ``meta`` keep the original manifest shape.
    """
    local_index_path = Path(local_index_path)
    local_index_path.mkdir(parents=True, exist_ok=True)

    manifest: Dict[str, Any] = {
        "version": 1,
        "metric": "cosine",
        "columns": sorted(column_entries, key=lambda entry: entry["column_key"]),
    }
    if meta:
        for key, value in meta.items():
            if key in {"version", "metric", "columns"}:
                continue
            manifest[key] = value
    with (local_index_path / LOCAL_INDEX_MANIFEST_FILENAME).open("w", encoding="utf-8") as file_obj:
        json.dump(manifest, file_obj, ensure_ascii=True, indent=2)


def local_index_exists(vector_db_path: str | Path) -> bool:
    return (get_local_index_path(vector_db_path) / LOCAL_INDEX_MANIFEST_FILENAME).exists()


def read_local_index_manifest(local_index_path: str | Path) -> Dict[str, Any] | None:
    manifest_path = Path(local_index_path) / LOCAL_INDEX_MANIFEST_FILENAME
    if not manifest_path.exists():
        return None
    with manifest_path.open("r", encoding="utf-8") as file_obj:
        return json.load(file_obj)


# Index status values surfaced to preflight / SQLSkill.
INDEX_MISSING = "missing"
INDEX_COMPLETE = "complete"
INDEX_PARTIAL = "partial"
INDEX_INCOMPATIBLE = "incompatible"
INDEX_STALE = "stale"


def classify_index_status(
    local_index_path: str | Path,
    *,
    expected_signature: str | None = None,
    expected_signature_v2: str | None = None,
    database_fingerprint: str | None = None,
) -> Dict[str, Any]:
    """Return a factual status for a local value index.

    Order of precedence: missing -> incompatible (embedding build config
    differs) -> stale (database changed) -> partial / complete. A legacy
    manifest without the Phase 4A fields is treated as ``complete`` when it has
    columns, so existing Sales/HR indexes keep working unchanged.

    ``expected_signature_v2`` is only compared when the manifest recorded it,
    so indexes built before signature v2 existed stay compatible.
    """
    index_path = Path(local_index_path)
    manifest = read_local_index_manifest(index_path)
    if manifest is None:
        return {"status": INDEX_MISSING, "covered_tables": [], "covered_columns": 0,
                "index_kind": None, "embedding_model": None, "column_count": 0,
                "embedding_dim": None, "policy_version": None, "reasons": ["manifest_not_found"]}

    columns = manifest.get("columns") or []
    covered_tables = sorted({entry.get("table_name") for entry in columns if entry.get("table_name")})
    column_count = len(columns)
    embedding_model = manifest.get("embedding_model")
    build_signature = manifest.get("embedding_signature")
    build_signature_v2 = manifest.get("embedding_signature_v2")
    index_kind = manifest.get("index_kind") or (INDEX_COMPLETE if column_count else INDEX_PARTIAL)
    shared = {"covered_tables": covered_tables, "covered_columns": column_count,
              "index_kind": index_kind, "embedding_model": embedding_model,
              "column_count": column_count,
              "embedding_dim": manifest.get("embedding_dim"),
              "policy_version": manifest.get("policy_version"),
              "signature_source": ("v2" if build_signature_v2 else
                                   "v1" if build_signature else None)}

    reasons: List[str] = []
    if build_signature_v2 is not None and expected_signature_v2 is not None:
        if build_signature_v2 != expected_signature_v2:
            reasons.append("embedding_signature_v2_mismatch")
    elif build_signature is not None and expected_signature is not None:
        if build_signature != expected_signature:
            reasons.append("embedding_signature_mismatch")
    if manifest.get("version") != MANIFEST_FORMAT_VERSION:
        reasons.append(f"manifest_format_version:{manifest.get('version')}")
    if reasons:
        return {"status": INDEX_INCOMPATIBLE, **shared, "reasons": reasons}
    if database_fingerprint is not None and manifest.get("database_fingerprint") is not None \
            and manifest["database_fingerprint"] != database_fingerprint:
        return {"status": INDEX_STALE, **shared, "reasons": ["database_fingerprint_mismatch"]}

    return {"status": index_kind, **shared, "reasons": []}


def covered_column_keys(local_index_path: str | Path) -> set[tuple[str, str]]:
    """``(table, column)`` pairs the built index actually covers."""
    manifest = read_local_index_manifest(local_index_path)
    if not manifest:
        return set()
    keys: set[tuple[str, str]] = set()
    for entry in manifest.get("columns") or []:
        table = entry.get("table_name")
        column = entry.get("column_name")
        if table and column:
            keys.add((str(table), str(column)))
    return keys


def verify_runtime_index(
    local_index_path: str | Path,
    *,
    embedding_config: Any,
    database_fingerprint: str | None = None,
    policy_version: str | None = None,
) -> Dict[str, Any]:
    """Runtime guard executed before any value vector is read.

    ``incompatible`` / ``stale`` must stop retrieval: scoring a query vector
    from a different model against stored vectors silently returns garbage.
    A ``partial`` index is a legitimate, explicitly-declared result and is
    allowed through (per-column misses degrade to "no values").
    """
    status = classify_index_status(
        local_index_path,
        expected_signature=embedding_signature(embedding_config),
        expected_signature_v2=canonical_embedding_signature(embedding_config),
        database_fingerprint=database_fingerprint,
    )
    warnings: List[str] = []
    if status["status"] in {INDEX_COMPLETE, INDEX_PARTIAL}:
        manifest_policy = status.get("policy_version")
        if policy_version and manifest_policy and manifest_policy != policy_version:
            # A policy change alters *coverage*, not vector validity: surface it
            # instead of rejecting a usable index.
            warnings.append(f"policy_version_mismatch:{manifest_policy}!={policy_version}")
        if not status.get("embedding_dim"):
            warnings.append("embedding_dim_unknown")
    status["warnings"] = warnings
    status["usable"] = status["status"] in {INDEX_COMPLETE, INDEX_PARTIAL}
    return status


@dataclass
class _LoadedColumn:
    embeddings: torch.Tensor
    documents: List[str]


class LocalValueIndex:
    def __init__(self, index_path: str | Path, device: str = "auto"):
        self._index_path = Path(index_path)
        self._manifest_path = self._index_path / LOCAL_INDEX_MANIFEST_FILENAME
        if not self._manifest_path.exists():
            raise FileNotFoundError(f"Local index manifest not found: {self._manifest_path}")

        with self._manifest_path.open("r", encoding="utf-8") as file_obj:
            manifest = json.load(file_obj)

        self._columns = {
            entry["column_key"]: entry
            for entry in manifest.get("columns", [])
        }
        # Recorded at build time; used to reject query vectors produced by a
        # different embedding model instead of scoring mismatched shapes.
        self.embedding_dim = manifest.get("embedding_dim")
        self.manifest = manifest
        self._device = self._resolve_device(device)
        self._column_cache: Dict[str, _LoadedColumn] = {}
        self._cache_lock = Lock()

    @staticmethod
    def _resolve_device(device: str) -> torch.device:
        normalized_device = device.lower()
        if normalized_device == "auto":
            normalized_device = "cuda" if torch.cuda.is_available() else "cpu"

        if normalized_device.startswith("cuda") and not torch.cuda.is_available():
            normalized_device = "cpu"

        return torch.device(normalized_device)

    @property
    def device(self) -> str:
        return self._device.type

    def _load_column(self, table_name: str, column_name: str) -> _LoadedColumn:
        column_key = _column_key(table_name, column_name)
        cached_column = self._column_cache.get(column_key)
        if cached_column is not None:
            return cached_column

        with self._cache_lock:
            cached_column = self._column_cache.get(column_key)
            if cached_column is not None:
                return cached_column

            entry = self._columns.get(column_key)
            if entry is None:
                raise KeyError(f"Local index column not found: {table_name}.{column_name}")

            embeddings = np.load(self._index_path / entry["embeddings_file"])
            with (self._index_path / entry["documents_file"]).open("r", encoding="utf-8") as file_obj:
                documents = json.load(file_obj)

            tensor = torch.from_numpy(embeddings)
            if self._device.type == "cuda":
                tensor = tensor.to(self._device, non_blocking=True)
            cached_column = _LoadedColumn(embeddings=tensor, documents=documents)
            self._column_cache[column_key] = cached_column
            return cached_column

    def retrieve_values_for_column(
        self,
        query_embeddings: List[List[float]],
        table_name: str,
        column_name: str,
        max_values_per_column: int,
        lower_meta_data: bool,
    ) -> Dict[str, Any]:
        lookup_table_name = table_name.lower() if lower_meta_data else table_name
        lookup_column_name = column_name.lower() if lower_meta_data else column_name

        if not query_embeddings:
            return {
                "table_name": lookup_table_name,
                "column_name": lookup_column_name,
                "values": [],
            }

        try:
            loaded_column = self._load_column(lookup_table_name, lookup_column_name)
        except KeyError:
            return {
                "table_name": lookup_table_name,
                "column_name": lookup_column_name,
                "values": [],
            }
        column_embeddings = loaded_column.embeddings
        if column_embeddings.shape[0] == 0:
            return {
                "table_name": lookup_table_name,
                "column_name": lookup_column_name,
                "values": [],
            }

        query_array = np.asarray(query_embeddings, dtype=np.float32)
        if query_array.ndim == 1:
            query_array = query_array.reshape(1, -1)
        if column_embeddings.shape[1] != query_array.shape[1]:
            raise ValueError(
                "Value index embedding dimension mismatch for "
                f"{lookup_table_name}.{lookup_column_name}: index={column_embeddings.shape[1]}, "
                f"query={query_array.shape[1]}. Rebuild the index with the configured model "
                "or run the query with the same embedding model."
            )
        query_tensor = torch.as_tensor(query_array, dtype=torch.float32, device=self._device)
        query_tensor = torch.nn.functional.normalize(query_tensor, p=2, dim=1, eps=1e-12)

        top_k = min(max_values_per_column, column_embeddings.shape[0])
        similarities = query_tensor @ column_embeddings.T
        top_similarities, top_indices = torch.topk(similarities, k=top_k, dim=1)

        similarity_rows = top_similarities.detach().cpu().tolist()
        index_rows = top_indices.detach().cpu().tolist()

        values: List[Tuple[str, float]] = []
        for similarity_row, index_row in zip(similarity_rows, index_rows):
            for similarity, index in zip(similarity_row, index_row):
                values.append((loaded_column.documents[index], float(1.0 - similarity)))

        seen_values = set()
        top_values = []
        for value, distance in sorted(values, key=lambda item: item[1]):
            if value in seen_values:
                continue
            seen_values.add(value)
            top_values.append({"value": value, "distance": distance})
            if len(top_values) >= max_values_per_column:
                break

        return {
            "table_name": lookup_table_name,
            "column_name": lookup_column_name,
            "values": top_values,
        }
