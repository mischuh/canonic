WITH "num" AS (
  SELECT
    SUM("damages"."repair_cost") AS "n"
  FROM "damages" AS "damages"
  WHERE
    (
      "damages"."severity" IN ('major', 'moderate')
    )
), "den" AS (
  SELECT
    COUNT("damages"."damage_id") AS "d"
  FROM "damages" AS "damages"
  WHERE
    (
      "damages"."severity" IN ('major', 'moderate')
    )
)
SELECT
  CAST("n" AS REAL) / NULLIF("d", 0) AS "avg_repair_costs"
FROM "num"
CROSS JOIN "den"
