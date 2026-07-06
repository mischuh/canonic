# Canonic local-model baseline

_Generated 2026-06-22T14:01:36+00:00: re-run with `canonic eval baseline` (SPEC-E10 §7, GH-66)._

Measures the LLM-in-loop **drafting** that feeds compilable semantics: not literal
compiler quality, since the E5 compiler is deterministic and LLM-free. Per task and
model: accuracy on a labeled set, structured (JSON-schema) output behavior, and
latency. Recommended = most accurate model clearing 90%
structured-output adherence.

## Task: `reconcile` (grain inference)

| Model | Accuracy | Structured output | p50 latency | Median tokens | Recommended |
| --- | --- | --- | --- | --- | --- |
| gemma-4-e2b-it-4bit (small, draft) (`gemma-4-e2b-it-4bit`) | 100% (8/8) | honored 8/8 | 250 ms |: | ✅ |
| DeepSeek-R1-0528-Qwen3-8B-MLX-4bit (mid) (`DeepSeek-R1-0528-Qwen3-8B-MLX-4bit`) | 88% (7/8) | honored 8/8 | 416 ms |: |  |

**Recommended for `reconcile`:** gemma-4-e2b-it-4bit (small, draft).

## Task: `reconcile` (contradiction resolution)

| Model | Accuracy | Structured output | p50 latency | Median tokens | Recommended |
| --- | --- | --- | --- | --- | --- |
| gemma-4-e2b-it-4bit (small, draft) (`gemma-4-e2b-it-4bit`) | 100% (8/8) | honored 8/8 | 250 ms |: | ✅ |
| DeepSeek-R1-0528-Qwen3-8B-MLX-4bit (mid) (`DeepSeek-R1-0528-Qwen3-8B-MLX-4bit`) | 88% (7/8) | honored 8/8 | 416 ms |: |  |

**Recommended for `reconcile`:** gemma-4-e2b-it-4bit (small, draft).

## How to re-run

Regenerate before tagging a release so the baseline tracks reality as models churn:

```bash
canonic eval baseline --candidates candidates.yaml --out docs/baseline-models.md
```

`--candidates` is a YAML list of `openai_compatible` models (see
`examples/eval/candidates.example.yaml`); `--dataset` defaults to the shipped labeled set.
