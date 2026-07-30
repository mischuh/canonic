SELECT
  DATE_TRUNC('MONTH', "fct_mrr_snapshot"."snapshot_month") AS "snapshot_month",
  SUM("fct_mrr_snapshot"."mrr") AS "mrr_sum"
FROM "fct_mrr_snapshot" AS "fct_mrr_snapshot"
GROUP BY
  DATE_TRUNC('MONTH', "fct_mrr_snapshot"."snapshot_month")
