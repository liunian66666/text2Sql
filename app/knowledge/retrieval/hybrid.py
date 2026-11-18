"""Multi-source hybrid retrieval index (metric / table / column).

Keyword recall understands exact column names, dotted SQL Server schema
prefixes, CamelCase splitting and Chinese business aliases. Dense recall is
*injected*: when no embedding backend is supplied, the index runs keyword-only
and reports ``mode_used=keyword``. No mock vectors are ever scored.

Fusion defaults to the same normalized weighted sum used by the existing
``EnterpriseKnowledgeBase``. RRF is implemented as an opt-in alternative and is
only selected after a dev-set comparison (see ``evaluate-graphrag``).
"""
from __future__ import annotations

import math
import re
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..models import UNKNOWN, DatabaseSchema, MetricDefinition
from ..schema_dictionary import SchemaDictionary, searchable_text

KnowledgeType = Literal["metric", "table", "column"]

DOMAIN_BY_PREFIX = {
    "Sales.": "sales", "Production.": "product", "Purchasing.": "purchasing",
    "HumanResources.": "hr", "Person.": "person", "dbo.": "system",
}

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_ASCII = re.compile(r"^[A-Za-z0-9_.]+$")


class KnowledgeDocument(BaseModel):
    doc_id: str
    database_id: str
    knowledge_type: KnowledgeType
    domain: str
    version: str
    node_id: str
    name: str
    table_name: str = ""
    column_name: str = ""
    text: str = ""
    keywords: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    source_ref: str = ""
    review_status: str = "unreviewed"
    trusted: bool = False


def split_identifier(text: str) -> list[str]:
    """CamelCase + dotted schema prefix -> searchable lowercase fragments."""
    fragments: list[str] = []
    for chunk in re.findall(r"[A-Za-z0-9_]+", text):
        fragments.append(chunk.casefold())
        pieces = _CAMEL_BOUNDARY.sub(" ", chunk).split()
        fragments.extend(piece.casefold() for piece in pieces if len(piece) > 1)
        if "." in chunk:
            fragments.extend(part.casefold() for part in chunk.split(".") if part)
    return list(dict.fromkeys(fragments))


def chinese_ngrams(text: str) -> list[str]:
    """Bigrams + trigrams for Chinese queries, plus the whole CJK run."""
    runs = re.findall(r"[\u4e00-\u9fff]+", text)
    grams: list[str] = []
    for run in runs:
        if len(run) >= 2:
            grams.append(run)
        grams.extend(run[i:i + 2] for i in range(len(run) - 1))
        grams.extend(run[i:i + 3] for i in range(len(run) - 2))
    return list(dict.fromkeys(grams))


def query_terms(question: str) -> set[str]:
    normalized = question.casefold().strip()
    terms = set(split_identifier(normalized))
    terms.update(chinese_ngrams(normalized))
    # Exact dotted identifier / quoted column name as a single strong term.
    terms.update(m.strip('"').casefold() for m in re.findall(r'"([A-Za-z0-9_.]+)"', question))
    return {term for term in terms if term}


def _domain_for_table(table_name: str) -> str:
    for prefix, domain in DOMAIN_BY_PREFIX.items():
        if table_name.startswith(prefix):
            return domain
    return "other"


def build_documents(registry: Any, schemas: dict[str, DatabaseSchema],
                    dictionary: SchemaDictionary | None = None) -> list[KnowledgeDocument]:
    """One document per metric / table / column. Cross-database names never mix."""
    docs: list[KnowledgeDocument] = []
    for metric in registry.metrics:  # type: ignore[attr-defined]
        db = metric.database_id
        terms = [*metric.aliases, *metric.keywords, metric.name]
        docs.append(KnowledgeDocument(
            doc_id=f"metric:{metric.metric_id}", database_id=db, knowledge_type="metric",
            domain=metric.domain, version=metric.version, node_id=metric.metric_id,
            name=metric.name, text=" ".join(filter(None, [
                metric.name, metric.description, metric.expression, metric.domain,
                " ".join(metric.aliases), " ".join(metric.keywords),
                " ".join(metric.source_tables)])),
            keywords=list(dict.fromkeys(terms)), aliases=list(metric.aliases),
            source_ref=metric.source_ref,
            review_status="approved" if metric.reviewed else "unreviewed",
            trusted=metric.trusted,
        ))

    for database_id, schema in schemas.items():
        dictionary_table = None
        for table in schema.tables:
            if dictionary is not None:
                dictionary_table = dictionary.table(table.table_name)
            en, zh, table_aliases, _ = (
                dictionary.describe(table.table_name) if dictionary is not None
                else (UNKNOWN, UNKNOWN, [], "unknown"))
            table_text = " ".join(p for p in (*filter(None, [en, zh]),
                                              *table_aliases) if p != UNKNOWN)
            table_terms = [*split_identifier(table.table_name), *table_aliases,
                           *split_identifier(en), *chinese_ngrams(zh)]
            docs.append(KnowledgeDocument(
                doc_id=f"table:{table.table_id}", database_id=database_id,
                knowledge_type="table", domain=_domain_for_table(table.table_name),
                version=table.version, node_id=table.table_id,
                name=table.table_name, table_name=table.table_name,
                text=" ".join([table.table_name, table_text]),
                keywords=list(dict.fromkeys(table_terms)), aliases=table_aliases,
                source_ref=(dictionary_table.source_ref if dictionary_table else "sqlite:PRAGMA"),
                review_status=(dictionary_table.review_status if dictionary_table else "unreviewed"),
            ))
            for column in table.columns:
                col_desc = dictionary_table.column(column.column_name) if dictionary_table else None
                c_en = col_desc.description_en if col_desc else UNKNOWN
                c_zh = col_desc.description_zh if col_desc else UNKNOWN
                c_aliases = list(col_desc.aliases) if col_desc else []
                column_text = " ".join(
                    p for p in (c_en, c_zh, *c_aliases) if p and p != UNKNOWN)
                column_terms = [
                    *split_identifier(column.column_name), *c_aliases,
                    *split_identifier(c_en), *chinese_ngrams(c_zh),
                    *split_identifier(table.table_name),
                ]
                docs.append(KnowledgeDocument(
                    doc_id=f"column:{column.column_id}", database_id=database_id,
                    knowledge_type="column", domain=_domain_for_table(table.table_name),
                    version=column.version, node_id=column.column_id,
                    name=f"{table.table_name}.{column.column_name}",
                    table_name=table.table_name, column_name=column.column_name,
                    text=" ".join([column.column_name, table.table_name, column_text]),
                    keywords=list(dict.fromkeys(column_terms)), aliases=c_aliases,
                    source_ref=(col_desc.source_ref if col_desc else "sqlite:PRAGMA"),
                    review_status=(col_desc.review_status if col_desc else "unreviewed"),
                ))
    return sorted(docs, key=lambda item: item.doc_id)


class Hit(BaseModel):
    doc_id: str
    score: float
    matched: list[str] = Field(default_factory=list)
    rank: int = 0
    lexical_score: float = 0.0
    semantic_score: float = 0.0


class RetrievalFilters(BaseModel):
    database_id: str
    knowledge_types: list[KnowledgeType] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    version: str | None = None

    def accepts(self, doc: KnowledgeDocument) -> bool:
        # Hard isolation: a query can never see another database's objects.
        if doc.database_id != self.database_id:
            return False
        if self.knowledge_types and doc.knowledge_type not in self.knowledge_types:
            return False
        if self.domains and doc.domain not in self.domains:
            return False
        if self.version and doc.version != self.version:
            return False
        return True


def keyword_score(question: str, doc: KnowledgeDocument) -> tuple[float, list[str]]:
    """Deterministic lexical recall. Higher weight = more specific evidence."""
    terms = query_terms(question)
    qnorm = question.casefold().strip()
    score = 0.0
    matched: list[str] = []

    if doc.column_name and doc.column_name.casefold() in qnorm:
        score += 3.0
        matched.append(doc.column_name)
    if doc.table_name and doc.table_name.casefold() in qnorm:
        score += 2.5
        matched.append(doc.table_name)
    if doc.name.casefold() in qnorm and doc.knowledge_type == "metric":
        score += 2.0
        matched.append(doc.name)
    for alias in doc.aliases:
        needle = alias.casefold()
        if needle and (needle in qnorm or qnorm in needle):
            score += 2.2
            matched.append(alias)
    for keyword in doc.keywords:
        needle = keyword.casefold()
        if not needle:
            continue
        if needle in terms or (len(needle) > 3 and needle in qnorm):
            score += 1.0
            matched.append(keyword)
        elif needle in qnorm:
            score += 0.6
            matched.append(keyword)
    description_terms = set(split_identifier(doc.text)) | set(chinese_ngrams(doc.text))
    overlap = {term for term in description_terms & terms if len(term) > 1}
    score += 0.25 * min(len(overlap), 6)
    matched.extend(sorted(overlap)[:6])
    return min(score, 12.0), list(dict.fromkeys(matched))[:12]


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    numerator = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if not left_norm or not right_norm:
        return 0.0
    return max(0.0, min(1.0, numerator / (left_norm * right_norm)))


class LocalSentenceTransformerBackend:
    """Real local embedding. No API, no quota. Returns a labelled backend."""

    label = "local:sentence-transformers"

    def __init__(self, model_path: str | Path, *, query_prompt: str | None = "query",
                 batch_size: int = 16):
        self.model_path = str(model_path)
        self.query_prompt = query_prompt
        self.batch_size = batch_size
        self._model = None
        self.label = f"local:sentence-transformers:{Path(model_path).name}"

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self.model_path)
            self._model.max_seq_length = 256
        return self._model

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        vectors = model.encode(list(texts), batch_size=self.batch_size,
                               normalize_embeddings=True, convert_to_numpy=True,
                               show_progress_bar=False)
        return [[float(x) for x in vector] for vector in vectors]

    def embed_query(self, text: str) -> list[float]:
        model = self._load()
        try:
            kwargs = {"prompt_name": self.query_prompt} if self.query_prompt else {}
            vector = model.encode([text], normalize_embeddings=True,
                                  convert_to_numpy=True, show_progress_bar=False, **kwargs)[0]
        except Exception:
            vector = model.encode([text], normalize_embeddings=True,
                                  convert_to_numpy=True, show_progress_bar=False)[0]
        return [float(x) for x in vector]


class DenseIndex:
    """Lazy corpus vectors + per-query cache, scoped to one index instance."""

    def __init__(self, documents: list[KnowledgeDocument], backend: Any):
        self.documents = documents
        self.backend = backend
        self._vectors: list[list[float]] | None = None
        self._query_cache: dict[str, list[float]] = {}

    def search(self, question: str, *, limit: int = 40) -> list[tuple[str, float]]:
        if self._vectors is None:
            self._vectors = self.backend.embed_documents(
                [f"{doc.knowledge_type}: {doc.name}. {doc.text}" for doc in self.documents])
        query_vector = self._query_cache.get(question)
        if query_vector is None:
            query_vector = self.backend.embed_query(question)
            self._query_cache[question] = query_vector
        scored = [(doc.doc_id, _cosine(query_vector, vector))
                  for doc, vector in zip(self.documents, self._vectors)]
        scored.sort(key=lambda row: (-row[1], row[0]))
        return scored[:limit]


class HybridResult(BaseModel):
    hits: list[Hit] = Field(default_factory=list)
    requested_mode: str = "keyword"
    mode_used: str = "keyword"
    fusion: str = "weighted"
    backend_label: str | None = None
    fallback_reason: str | None = None
    keyword_hit_count: int = 0
    dense_hit_count: int = 0
    candidate_count: int = 0
    elapsed_seconds: float = 0.0
    trace: dict[str, Any] = Field(default_factory=dict)


class HybridIndex:
    """Keyword baseline + optional dense recall + weighted / RRF fusion."""

    def __init__(self, documents: list[KnowledgeDocument], *, dense_backend: Any | None = None,
                 lexical_weight: float = 0.6, semantic_weight: float = 0.4,
                 semantic_min_score: float = 0.25):
        self.documents = documents
        self.by_id = {doc.doc_id: doc for doc in documents}
        self.dense = DenseIndex(documents, dense_backend) if dense_backend is not None else None
        total = lexical_weight + semantic_weight
        if total <= 0:
            raise ValueError("retrieval weights must not both be zero")
        self.lexical_weight = lexical_weight / total
        self.semantic_weight = semantic_weight / total
        self.semantic_min_score = semantic_min_score

    def keyword(self, question: str, filters: RetrievalFilters,
                *, limit: int | None = None) -> list[Hit]:
        scored: list[Hit] = []
        for doc in self.documents:
            if not filters.accepts(doc):
                continue
            score, matched = keyword_score(question, doc)
            if score > 0:
                scored.append(Hit(doc_id=doc.doc_id, score=score, matched=matched,
                                  lexical_score=score))
        scored.sort(key=lambda hit: (-hit.score, hit.doc_id))
        for rank, hit in enumerate(scored, start=1):
            hit.rank = rank
        return scored[:limit] if limit else scored

    def search(self, question: str, filters: RetrievalFilters, *, mode: str = "keyword",
               fusion: str = "weighted", limit: int = 40) -> HybridResult:
        started = time.perf_counter()
        keyword_hits = self.keyword(question, filters)
        candidates = sum(1 for doc in self.documents if filters.accepts(doc))
        mode_used = mode if (mode == "hybrid" and self.dense is not None) else "keyword"
        fallback_reason = None
        if mode == "hybrid" and self.dense is None:
            fallback_reason = "no_dense_backend"
        dense_hits: list[tuple[str, float]] = []
        if mode_used == "hybrid":
            try:
                dense_hits = self.dense.search(question, limit=max(limit * 2, 60))  # type: ignore[union-attr]
            except Exception as exc:
                mode_used = "keyword"
                fallback_reason = f"{type(exc).__name__}"
                dense_hits = []

        dense_map = {doc_id: score for doc_id, score in dense_hits}
        dense_ranks = {doc_id: rank for rank, (doc_id, _) in enumerate(dense_hits, start=1)}
        keyword_map = {hit.doc_id: hit for hit in keyword_hits}

        if fusion == "rrf":
            hits = self._fuse_rrf(keyword_map, dense_ranks, dense_map)
        else:
            hits = self._fuse_weighted(keyword_map, dense_map, mode_used)
        hits.sort(key=lambda hit: (-hit.score, hit.doc_id))
        for rank, hit in enumerate(hits[:limit], start=1):
            hit.rank = rank
        result = HybridResult(
            hits=hits[:limit], requested_mode=mode, mode_used=mode_used,
            fusion=fusion if mode_used == "hybrid" else "weighted",
            backend_label=(self.dense.backend.label if self.dense is not None else None),
            fallback_reason=fallback_reason,
            keyword_hit_count=len(keyword_hits), dense_hit_count=len(dense_hits),
            candidate_count=candidates,
            elapsed_seconds=round(time.perf_counter() - started, 6),
            trace={
                "database_id": filters.database_id,
                "knowledge_types": list(filters.knowledge_types),
                "domains": list(filters.domains), "version": filters.version,
                "question_terms": sorted(query_terms(question))[:30],
                "fusion_used": fusion if mode_used == "hybrid" else "weighted",
                "lexical_weight": self.lexical_weight,
                "semantic_weight": self.semantic_weight,
            },
        )
        return result

    def _fuse_weighted(self, keyword_map: dict[str, Hit], dense_map: dict[str, float],
                       mode_used: str) -> list[Hit]:
        hits: list[Hit] = []
        for doc_id in set(keyword_map) | set(dense_map):
            lex_raw = keyword_map[doc_id].score if doc_id in keyword_map else 0.0
            sem = dense_map.get(doc_id, 0.0)
            lex = min(lex_raw / 6.0, 1.0)
            if mode_used == "keyword":
                score, eligible = lex, lex_raw > 0
            else:
                score = self.lexical_weight * lex + self.semantic_weight * sem
                eligible = lex_raw > 0 or sem >= self.semantic_min_score
            if not eligible:
                continue
            hits.append(Hit(doc_id=doc_id, score=round(score, 6),
                            matched=keyword_map[doc_id].matched if doc_id in keyword_map else [],
                            lexical_score=round(lex, 6), semantic_score=round(sem, 6)))
        return hits

    @staticmethod
    def _fuse_rrf(keyword_map: dict[str, Hit], dense_ranks: dict[str, int],
                  dense_map: dict[str, float], k: int = 60) -> list[Hit]:
        hits: list[Hit] = []
        for doc_id in set(keyword_map) | set(dense_ranks):
            score = 0.0
            if doc_id in keyword_map:
                score += 1.0 / (k + keyword_map[doc_id].rank)
            if doc_id in dense_ranks:
                score += 1.0 / (k + dense_ranks[doc_id])
            hits.append(Hit(doc_id=doc_id, score=round(score, 8),
                            matched=keyword_map[doc_id].matched if doc_id in keyword_map else [],
                            lexical_score=keyword_map[doc_id].score if doc_id in keyword_map else 0.0,
                            semantic_score=round(dense_map.get(doc_id, 0.0), 6)))
        return hits
