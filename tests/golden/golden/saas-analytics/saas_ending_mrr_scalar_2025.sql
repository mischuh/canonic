WITH "ranked" AS (
  SELECT
    "fct_mrr_snapshot"."customer_id" AS "customer_id",
    "fct_mrr_snapshot"."mrr" AS "mrr",
    ROW_NUMBER() OVER (
      PARTITION BY "fct_mrr_snapshot"."customer_id"
      ORDER BY "fct_mrr_snapshot"."snapshot_month" DESC
    ) AS "rn"
  FROM "fct_mrr_snapshot" AS "fct_mrr_snapshot"
  WHERE
    "fct_mrr_snapshot"."snapshot_month" >= '2025-01-01'
    AND "fct_mrr_snapshot"."snapshot_month" < '2026-01-01'
)
SELECT
  SUM("ranked"."mrr") AS "ending_mrr"
FROM "ranked"
WHERE
  "ranked"."rn" = 1
