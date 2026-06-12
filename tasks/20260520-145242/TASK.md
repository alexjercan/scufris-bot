# RAG document store: embeddings, sync, and Q&A

- STATUS: OPEN
- PRIORITY: 0
- TAGS: rag,knowledge,tools,backlog

A local vector database — **sqlite-vec** (decided: keeps the stack flat, matches the SQLite-everywhere story of the persistence task) — that the knowledge agent queries before touching the web.

## Indexing / Sync Side

A `rag_sync` CLI command (or a tool the agent can call) that takes a source spec and chunks + embeds documents into the store.

Sources:
- A local directory (journal's daily `.md` files, a notes vault, a book folder)
- A git repo path (re-syncs on pull)
- A remote URL list or an Atom/RSS feed of documents
- A glob pattern like `~/journal/2026-*.md`

Details:
- **Chunking strategy**: paragraph-aware for Markdown (respect headings as chunk boundaries), sliding window with overlap for PDFs
- **Embeddings**: `nomic-embed-text` via Ollama — already local, no API key, reasonable quality
- **Metadata per chunk**: `source_path`, `source_type`, `chunk_index`, `modified_at`, content hash for dedup/incremental sync
- **Incremental sync**: hash the file, skip if unchanged. Watcher mode (`rag_sync --watch ~/journal`) using `watchdog` for live re-indexing as you write

## Query Side

- `rag_search(query, top_k, source_filter?)` tool added to the knowledge agent's toolset
- Knowledge agent prompt updated: "before calling `web_search` for personal/historical questions, try `rag_search` first"
- Results include the source path and a snippet so the agent can cite "from your journal entry on May 3rd…"
- Optional reranking pass (cross-encoder or just LLM-based) if top_k results are noisy

## Journal Integration

- Auto-sync hook: after `today --create` writes a new entry, trigger an incremental sync of `~/journal/` so today's entry is immediately queryable
- Enables questions like "what did I eat last Tuesday?" or "what was my weight trend in April?" answered from actual journal data rather than hallucinations

## Source Registry

- Source list lives in the per-user XDG config (see `20260520-145231`) as `[user.rag.sources]` rather than a separate `rag_sources.toml` — keeps all per-user config in one place
- A fallback dedicated file `$XDG_CONFIG_HOME/scufris/rag_sources.toml` is acceptable if the main config gets too noisy
- `rag_sources_tool` lets the agent list what's indexed and when it was last synced (returns only the current user's sources)

## Dependencies on User Identity (`20260520-145231`)

This task is **strongly coupled** to the identity layer. The RAG store must be partitioned by `user_id` so different users don't see each other's documents:

- **Collection / table keying**: every embedded chunk carries a `user_id` column. sqlite-vec approach: single `rag_chunks` table with `user_id` indexed and `WHERE user_id = ?` on every query. Vector data lives in a `vec0` virtual table joined on chunk ID.
- **Sync paths are per-user**: `~/journal` for `alex` is different from `~/journal` for `bob` — resolve via the user's config entry, not a global
- **`rag_search` tool** receives the current `user_id` from the tool invocation context (same plumbing as facts/scratchpad) — agents cannot accidentally query another user's corpus
- **Shared sources** (optional, future): a `[global.rag.sources]` namespace for things like a shared documentation set; queried with `user_id = NULL` partition. Defer until needed.

If identity work isn't done yet, the RAG store can launch with a hardcoded `user_id = 1` and be migrated later — but the schema should include the column from day one to avoid a painful migration. **Decision: identity ships first; this task does not start until then.**

## Complexity Estimate

Medium-large. Embedding pipeline + chunker + watcher + tool wiring + reranker is ~1 week of focused work. The hardest part is probably making incremental sync robust (file moves, renames, partial reads during write).

## Open Questions

- **Conversation indexing** (from `20260520-145229`): index full turns, or only assistant final replies? Indexing tool calls might be noisy.
- **Reranking**: skip for v1 and rely on top-k=10 with a generous prompt window, or do it from the start?
- **PDF support**: nice-to-have or v1 requirement? Adds `pypdf`/`pdfplumber` dependency and OCR fallback complexity.
- **Eviction / size cap**: what's the policy when the journal hits 10,000 chunks? Probably nothing for v1, but worth noting.

## Decided

- **Store**: sqlite-vec (flat stack, no extra process)
- **Sequencing**: identity layer (`20260520-145231`) must land before this task
