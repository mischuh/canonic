WITH "num" AS (
  SELECT
    "damages"."severity" AS "severity",
    SUM("damages"."repair_cost") AS "n"
  FROM "damages" AS "damages"
  WHERE
    (
      "damages"."severity" IN ('major', 'moderate')
    )
  GROUP BY
    "damages"."severity"
), "den" AS (
  SELECT
    "damages"."severity" AS "severity",
    COUNT("damages"."damage_id") AS "d"
  FROM "damages" AS "damages"
  WHERE
    (
      "damages"."severity" IN ('major', 'moderate')
    )
  GROUP BY
    "damages"."severity"
)
SELECT
  "severity" AS "severity",
  CAST("n" AS REAL) / NULLIF("d", 0) AS "avg_repair_costs"
FROM "num"
FULL JOIN "den"
  USING ("severity")
