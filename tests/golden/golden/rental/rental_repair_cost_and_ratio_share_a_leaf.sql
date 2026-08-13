WITH "_leaf_0" AS (
  SELECT
    "damages"."severity" AS "severity",
    SUM("damages"."repair_cost") AS "total_repair_cost"
  FROM "damages" AS "damages"
  GROUP BY
    "damages"."severity"
), "_leaf_1" AS (
  SELECT
    "damages"."severity" AS "severity",
    SUM("damages"."repair_cost") AS "total_repair_cost",
    COUNT("damages"."damage_id") AS "damage_count"
  FROM "damages" AS "damages"
  WHERE
    (
      "damages"."severity" IN ('major', 'moderate')
    )
  GROUP BY
    "damages"."severity"
), "_grain" AS (
  SELECT
    "_leaf_0"."severity" AS "severity"
  FROM "_leaf_0"
  UNION
  SELECT
    "_leaf_1"."severity" AS "severity"
  FROM "_leaf_1"
)
SELECT
  "_grain"."severity" AS "severity",
  "_leaf_0"."total_repair_cost" AS "total_repair_cost",
  CAST("_leaf_1"."total_repair_cost" AS REAL) / NULLIF("_leaf_1"."damage_count", 0) AS "avg_repair_costs"
FROM "_grain"
LEFT JOIN "_leaf_0"
  ON (
    "_grain"."severity" = "_leaf_0"."severity"
    OR "_grain"."severity" IS NULL
    AND "_leaf_0"."severity" IS NULL
  )
LEFT JOIN "_leaf_1"
  ON (
    "_grain"."severity" = "_leaf_1"."severity"
    OR "_grain"."severity" IS NULL
    AND "_leaf_1"."severity" IS NULL
  )
