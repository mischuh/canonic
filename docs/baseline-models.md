# Canon local-model baseline

> **Not yet populated.** This file is the published home of the E10 tested local-model baseline
> (SPEC-E10 §7, GH-66). The harness ships and is re-runnable; the rows below are filled by running
> it against real local models. Regenerate before tagging a release:
>
> ```bash
> canon eval baseline --candidates candidates.yaml --out docs/baseline-models.md
> ```

Measures the LLM-in-loop **drafting** that feeds compilable semantics — **not** literal compiler
quality, since the E5 compiler is deterministic and LLM-free (SPEC-E10 §7). For each task and
candidate model the harness records, over a labeled set:

- **Accuracy** — share of cases whose drafted grain matches the known-correct grain exactly.
- **Structured-output behavior** — how reliably the model honors JSON-schema-constrained output
  (`honored` / `schema-invalid` / `unsupported` / `error`); smaller local models vary most here.
- **p50 latency** and best-effort **median tokens** per call.

**Recommended** = the most accurate candidate that also clears the structured-output adherence
floor (default 90%) — a model that returns unparseable output is unusable for E4 drafting however
accurate its rare parseable answers are.

## Task: `draft` (grain inference)

| Model | Accuracy | Structured output | p50 latency | Median tokens | Recommended |
| --- | --- | --- | --- | --- | --- |
| _populate with `canon eval baseline`_ | | | | | |

## Task: `reconcile`

Pending E4 reconciliation drafting — `reconcile` has no live call site yet, so there is no real
behavior to score. The harness is generic and will cover it once E4 wires the reconcile path
(SPEC-E10 §7, GH-66).

## How to re-run

1. Start your local `openai_compatible` model server(s) — Ollama, vLLM, LM Studio, llama.cpp, or
   TGI. A hosted endpoint differs only by `base_url` and key (SPEC-E10 §2).
2. Copy `examples/eval/candidates.example.yaml` and point each entry at a model you want to test.
3. Run the harness; it writes this file:

   ```bash
   canon eval baseline --candidates candidates.yaml --out docs/baseline-models.md
   ```

`--dataset` defaults to the shipped labeled set (`canon/eval/datasets/draft_grain.jsonl`); pass
your own JSONL to extend coverage. Commit the regenerated doc per release so the baseline tracks
reality as local models churn.
