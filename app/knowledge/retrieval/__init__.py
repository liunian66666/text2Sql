"""Phase 2/3 knowledge retrieval: hybrid index, bidirectional schema recall,
GraphRAG subgraph expansion, join-path search and structured SchemaContext.

All modules are offline-capable. A dense backend is injected; if none is
available the retriever degrades to deterministic keyword recall and records
that fact in ``retrieval_trace`` instead of pretending to have run a model.
"""
