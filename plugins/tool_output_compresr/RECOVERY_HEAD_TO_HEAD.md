# Tool-output recovery: head-to-head

A controlled comparison of two `recover.py` implementations for the
`tool_output_compresr` plugin, measuring **savings**, **precision**, and
**losslessness** of the recovery-reference rewriting.

## What's being compared

| Label | Branch | Anchoring approach |
|---|---|---|
| **feat** | `feat/compresr-context-compression` @ `8f71e788e` (shared integration branch; includes PR #3 marker-recovery + PR #4 contract tests) | line-anchoring with a reorder guard |
| **fix** | `fix/compresr-recover-ambiguous-anchor` | segment/run anchoring + ambiguity separation + economics guard |

Both branches share the **same** recovery-reference format and the same
`rewrite_placeholders(cache_path, original, compressed) -> (rewritten, gaps, ok)`
contract, so any difference comes purely from the anchoring logic — not from
reference length.

## Method

The `toc_latte_v2` tool-output API is **non-deterministic**, so a fair
comparison can't call it once per branch. Instead:

1. **Capture once.** Call the live API on a fixed corpus of 6 real files
   (markdown prose, structured markdown, a task list, two Python sources, one
   JSON results file) and save each `(original, compressed-with-markers)` pair.
2. **Replay.** Feed the *same* captured pairs through each branch's
   `recover.py::rewrite_placeholders` in a fresh process, and measure:
   - **savings** — `1 − out_tokens / base_tokens` (tokens = `(len+3)//4`, the
     estimator both branches use);
   - **losslessness** — count of non-blank original lines that are neither kept
     verbatim in the output nor covered by a recovery gap (must be 0);
   - **precision** — gap count and total gap-lines vs. the minimum that had to
     be dropped, plus an over-cover proxy.

Corpus raw API drop (before recovery rewriting) ranged 38–55% across the six
files, so all inputs were genuinely compressible.

## Results

| | feat (`8f71e788e`) | fix (`...ambiguous-anchor`) |
|---|---|---|
| **Total savings** | **10.2%** (34.3k → 30.8k tok) | **26.8%** (34.3k → 25.1k tok) |
| **Docs compressed** | **1 of 6** | **6 of 6** |
| **Unrecoverable lines (lossy?)** | 0 ✓ | 0 ✓ |
| **Precision where both compress** (`md_handoff`) | — | byte-identical to feat (4443 tok, 13 gaps, 0 over-cover) |

Per-document:

| doc | type | feat save% | fix save% |
|---|---|---|---|
| md_handoff | markdown prose | 44.1 | 44.1 |
| md_arch | structured md | 0.0 (fail-open) | 21.5 |
| md_tasks | task list | 0.0 (fail-open) | 18.6 |
| py_toolsets | Python | 0.0 (fail-open) | 14.4 |
| py_utils | Python | 0.0 (fail-open) | 14.6 |
| json_judge | JSON | 0.0 (fail-open) | 42.5 |

`feat` returns the **original untouched** on every document except the prose-y
`md_handoff`. `fix` compresses all six, losslessly.

## Root cause — why `feat` fails open on 5 of 6 docs

On those 5 documents `feat`'s `_anchor_walk` **does anchor successfully and finds
gaps** (52, 50, 79, 40, 16 of them) — then sets `reordered=True`, and
`rewrite_placeholders` discards the entire result (`return original, [], False`):

```
doc            feat: anchored / reordered      fix: anchored / reordered / ambiguous
md_handoff          True   /  False                 True   /  False  /  False
md_arch             True   /  True                  True   /  False  /  False
md_tasks            True   /  True                  True   /  False  /  True
py_toolsets         True   /  True                  True   /  False  /  True
py_utils            True   /  True                  True   /  False  /  True
json_judge          True   /  True                  True   /  False  /  True
```

PR #3 fixed **marker parsing** (so abstractive `[N tokens dropped]` markers now
anchor at all — real progress), but not the **reorder over-sensitivity**: any
document with incidental repeated lines (blank lines, `},`, code braces, table
rules) trips `reordered=True` and the good anchoring is thrown away. That covers
essentially every structured / code / JSON output.

`fix` separates *true reordering* from *incidental duplicate lines* via a
distinct `ambiguous` flag, so the anchoring is kept (as a conservative,
still-lossless over-cover) instead of discarded.

## Precision caveat (honest)

`fix` shows nonzero over-cover on repetitive content (JSON worst). **That number
is inflated by duplicates** — the proxy flags a dropped line as "over-covered"
whenever an identical line is kept *anywhere* (JSON is full of `},` /
`"score": 1,`). The gap *ranges* themselves are correct (those positions were
genuinely dropped) and recovery stays lossless (0 unrecoverable). The real cost
of the extra gaps is recovery-reference overhead, which already shows up as lower
savings on code (14%) than on JSON/markdown.

## Verdict

`fix` wins decisively: **2.6× the savings**, compresses **all** content types
(vs prose-only), is **byte-identical** to `feat` where `feat` works, and is
**lossless everywhere**.

What `feat` has that `fix` doesn't: the **marker-parsing robustness** (PR #3)
and the **contract tests** (PR #4) — both worth keeping. So the recommended
end-state is not either branch as-is, but **`feat`'s marker work + tests with
`fix`'s reorder/ambiguity separation and economics guard layered on top** — a
smaller, cleaner change than the current `fix` branch (which predates the marker
work).

## Reproducing

The harness (capture + replay scripts) and the captured corpus are kept out of
the repo (session scratchpad). The numbers above are from a single captured
corpus; re-capturing will shift them slightly (non-deterministic API) but not
the conclusion — `feat` fails open on any duplicate-bearing document, `fix` does
not.
