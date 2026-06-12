# Multi-modal image input: CLI path and Telegram attachment

- STATUS: OPEN
- PRIORITY: 0
- TAGS: multimodal,cli,telegram,backlog

## CLI Side

- `/image <path>` slash command, or natural syntax: the user types a message containing a file path ending in `.png`/`.jpg`/`.webp` and the CLI detects it
- Image is base64-encoded and attached to the next API call as a vision message
- Alternative: watch for clipboard image (if `xclip`/`wl-paste` has image data) when the user types `/paste-image`

## Telegram Side

- Already supported by Telegram's Bot API — photos and documents land in `message.photo` / `message.document`
- `main.py` handler extended: if the message contains an image, download it to a temp file and attach alongside the text caption

## Processing

- Images are passed to the main agent (or routed to knowledge agent) with the vision-capable model
- If the current `SCUFRIS_MODEL` isn't vision-capable, fall back gracefully: "I can't process images with the current model; switch to a vision model"
- A `describe_image_tool` wraps the vision call so sub-agents can request image descriptions too — e.g. the journal agent could describe a food photo and auto-log macros

## Practical Use Cases

- "What's in this photo?" (describe)
- "Extract the text from this screenshot" (OCR-like)
- "This is my lunch — log the macros" (journal integration)
- "Here's an error screenshot from my terminal — what's wrong?" (coding agent)

## Dependencies on User Identity (`20260520-145231`)

**Light** dependency. Images themselves don't care which user sent them, but a few touchpoints exist:

- **Vision model preference**: per-user override in config (`[user.models] vision = "claude-3-5-sonnet"`) so power users can pick a stronger vision model than the global default
- **Storage / cleanup**: downloaded Telegram images live in a temp dir keyed by `user_id` (`/tmp/scufris/<user_id>/<msg_id>.jpg`) so concurrent users don't collide and cleanup is per-user
- **Audit trail**: if image processing is logged (for debugging or RAG indexing of "things the user has shown me"), the log row carries `user_id`
- **Quota** (future, multi-user): per-user image-call counter to prevent one user burning the vision-model budget

Effectively: this task can ship before the identity layer using `user_id=1`, no rework needed once identity lands beyond swapping the constant for a real lookup.

## Storage and Lifecycle

- Telegram uploads are downloaded to a temp file, attached as a base64 data URL or a multipart vision payload, then deleted after the agent turn completes
- CLI path inputs are read-only references — never copied unless the user explicitly says "save this" (which becomes a new tool, out of scope here)
- Image payloads are **not** added to `ChatHistoryManager` raw — store a placeholder `[image attached: <hash>]` plus the agent's description, so history stays text-only and cheap

## Complexity Estimate

Small-to-medium. CLI detection + Telegram download + vision message construction is maybe 2–3 days. Most risk is model-side: ensuring the agent stack and history serialization handle multimodal messages without breaking the text-only assumptions everywhere.

## Open Questions

- **Max image size**: enforce a 5 MB / 2048px cap before sending? Telegram already compresses but CLI paths can be anything.
- **Multi-image messages**: support N images per turn or strictly one?
- **Clipboard paste**: worth building (`/paste-image`)? It's polish; most workflows can just save to `/tmp/x.png` first.
- **Auto-route by content**: should the main agent always handle images, or detect "screenshot of code" vs "photo of food" and route to coding/journal sub-agents respectively? Probably too clever for v1 — just send everything to main and let it dispatch.
- **OCR vs vision**: for screenshot-of-text use cases, is a dedicated OCR path (tesseract) worth having alongside the vision model? Vision is slower/costlier but more flexible.
- **History serialization**: where does the image bytes / URL get stored if we want re-querying? Probably "nowhere for v1" — describe and discard.
