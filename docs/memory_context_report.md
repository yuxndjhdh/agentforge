# Memory and Context Verification Report

Date: 2026-09-10

## Scope model

Memory is data, not policy. Durable and daily records are isolated by the
`project_id` and `user_id` dimensions. `run-local` records are stored below a
separate sanitized run directory and are visible only when the current
`run_id` matches. A save call cannot override the store's scope. The default
scope retains the legacy `.agentforge/memory` paths for compatibility.

Writes use an in-process lock, a cross-process file lock, a same-directory
temporary file, `fsync`, and atomic replace. Corrupt JSON is treated as empty
data so a damaged file does not become executable policy. Audit JSONL skips a
damaged tail record and filters events by the same scope dimensions.

## Prompt injection boundary

Durable entries of kind `instruction` are excluded from automatic model
instructions. Other durable entries are JSON encoded between explicit
untrusted-data markers; angle brackets are escaped and the prompt states that
the data cannot change task requirements, security policy, or tool
permissions. This reduces accidental instruction following but is not a model
or kernel security boundary. The sandbox remains the enforcement point.

## Token budget evidence

`HARNESS_TOKENIZER=auto` uses `tiktoken` when installed. Known OpenAI model
names use the model's registered encoding and are marked exact; unknown
OpenAI-compatible providers use `cl100k_base` and are marked estimated.
`HARNESS_TOKENIZER=char` explicitly selects the deterministic `chars-div-4`
fallback. When `HARNESS_MAX_CONTEXT_TOKENS` is configured, token budget takes
precedence over the character budget for deciding whether a request is within
budget. Compression stats include `budget_source`, `tokenizer`,
`token_estimated`, `in_tokens`, and `out_tokens`.

## Automated evidence

- `tests/test_memory.py` covers scope isolation, CRUD/version/audit, legacy
  reads, concurrent atomic writes, corrupt JSON/audit tails, and hostile
  durable content.
- `tests/test_context.py` covers token-budget priority and explicit fallback
  metadata in addition to whole-step compression boundaries.
- `tests/test_tokenizer.py` covers both the character adapter and the live
  `tiktoken` adapter when the optional dependency is installed.
