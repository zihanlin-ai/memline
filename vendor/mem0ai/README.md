# Vendored mem0ai (official 2.0.12 + workspace modifications)

Base: byte-identical official PyPI `mem0ai==2.0.12` wheel content.

Local modifications (all in `mem0/memory/main.py` unless noted):
- Concurrency: `_ENTITY_LINK_LOCK` around entity read-modify-write sections
  (daemon runs requests concurrently; spaCy calls are locked daemon-side).
- Extraction parse failures raise `LLMError` instead of returning a silent
  empty result.
- Raw (`infer=False`) adds: exact-hash dedup within user scope, semantic
  `near_duplicate_of` annotation (>=0.95, never skips), entity linking.
- Keyword-only retrieval: `Memory.search(keyword=True)` /
  `_search_keyword_store` (pure BM25); `vector_stores/qdrant.py`
  `keyword_search()` passes `with_payload=True`.
- Entity dedup is user-scoped (upstream scopes by user+agent+run, which
  recreates every entity once per session).

Upgrading: fetch the new official wheel, overlay these modifications (git
history of this directory is the authoritative record), bump the local
version suffix, then `pip install -e` into the store venv.
