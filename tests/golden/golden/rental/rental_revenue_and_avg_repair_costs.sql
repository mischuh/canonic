WITH "_leaf_0" AS (
  SELECT
    SUM("damages"."repair_cost") AS "total_repair_cost",
    COUNT("damages"."damage_id") AS "damage_count"
  FROM "damages" AS "damages"
  WHERE
    (
      "damages"."severity" IN ('major', 'moderate')
    )
), "_leaf_1" AS (
  SELECT
    SUM(CASE WHEN "payments"."status" = 'settled' THEN "payments"."amount" ELSE 0 END) AS "total_paid"
  FROM "payments" AS "payments"
)
SELECT
  "_leaf_1"."total_paid" AS "total_paid",
  CAST("_leaf_0"."total_repair_cost" AS REAL) / NULLIF("_leaf_0"."damage_count", 0) AS "avg_repair_costs"
FROM "_leaf_0"
CROSS JOIN "_leaf_1"
