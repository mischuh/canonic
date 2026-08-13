WITH "_leaf_0" AS (
  SELECT
    SUM("damages"."repair_cost") AS "total_repair_cost",
    COUNT("damages"."damage_id") AS "damage_count"
  FROM "damages" AS "damages"
  WHERE
    (
      "damages"."severity" IN ('major', 'moderate')
    )
)
SELECT
  CAST("_leaf_0"."total_repair_cost" AS REAL) / NULLIF("_leaf_0"."damage_count", 0) AS "avg_repair_costs"
FROM "_leaf_0"
