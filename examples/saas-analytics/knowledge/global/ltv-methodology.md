---
summary: "How customer_ltv is computed and why it is an opaque, customer-grain-only metric."
tags: [ltv, lifetime-value, opaque, methodology, policy]
sl_refs:
  - saas_duckdb.mart_account_health.ltv_value
usage_mode: policy
meta:
  provenance: human_curated
  last_validated_at: "2026-06-28T00:00:00Z"
---

## Definition

`customer_ltv` is a customer's estimated lifetime value: the latest active MRR multiplied by an
expected lifetime that varies by segment (enterprise 48 months, mid-market 36, SMB 24). It is
pre-computed per customer in the `mart_account_health` data mart and is `0` once a customer churns.

## Why it is an opaque metric

LTV is only meaningful at **customer grain**. Re-aggregating a pre-computed lifetime value across
customers (summing it, averaging it over arbitrary dimensions) produces numbers that do not
correspond to any real cohort calculation. To prevent silent misuse, `customer_ltv` is bound as an
**opaque** metric with `native_grain: [customer_id]`.

Canon serves it as a direct lookup at customer grain and refuses any other grain:

> Querying `customer_ltv` grouped by `segment` returns `UNSUPPORTED_MEASURE` — "opaque and can only
> be served at its native grain (customer_id)".

To analyse LTV by segment, aggregate deliberately in a downstream model rather than asking the
semantic layer to re-aggregate the opaque value. See [[vault-vs-mart]] for the mart's role.
