# Canonic local-model baseline

_Generated 2026-06-18T17:27:58+00:00 — re-run with `canonic eval baseline` (SPEC-E10 §7, GH-66)._

Measures the LLM-in-loop **drafting** that feeds compilable semantics — not literal
compiler quality, since the E5 compiler is deterministic and LLM-free. Per task and
model: grain accuracy on a labeled set, structured (JSON-schema) output behavior, and
latency. Recommended = most accurate model clearing 90%
structured-output adherence.

## Task: `draft` (grain inference)

| Model | Accuracy | Structured output | p50 latency | Median tokens | Recommended |
| --- | --- | --- | --- | --- | --- |
| gemma-4-12B-it-qat-4bit (local, Ollama) (`gemma-4-12B-it-qat-4bit`) | 60% (3/5) | honored 5/5 | 849 ms | — |  |
| gemma-4-e2b-it-4bit (local, Ollama) (`gemma-4-e2b-it-4bit`) | 100% (5/5) | honored 5/5 | 231 ms | — | ✅ |

**Recommended for `draft`:** gemma-4-e2b-it-4bit (local, Ollama).

## Task: `reconcile`

Pending E4 reconciliation drafting — `reconcile` has no live call site yet, so there is
no real behavior to score. The harness is generic and will cover it once E4 wires the
reconcile path (SPEC-E10 §7, GH-66).

## How to re-run

Regenerate before tagging a release so the baseline tracks reality as models churn:

```bash
canonic eval baseline --candidates candidates.yaml --out docs/baseline-models.md
```

`--candidates` is a YAML list of `openai_compatible` models (see
`examples/eval/candidates.example.yaml`); `--dataset` defaults to the shipped labeled set.
