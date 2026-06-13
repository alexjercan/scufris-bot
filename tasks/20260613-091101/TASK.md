# Test harness with mocked opencode client

- STATUS: OPEN
- PRIORITY: 45
- TAGS: testing,opencode

Equivalent of v1's "mock at the SDK boundary" approach (the `tests/`
suite covering tools / callbacks / stats / etc.), but for opencode's
API. Lets the v2 server be tested without spinning up opencode +
Ollama.

Spun off from scufris-v2 planning doc (`tasks/20260613-085701/TASK.md`).

