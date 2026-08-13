WITH "_leaf_0" AS (
  SELECT
    "fact_services"."service_type" AS "service_type",
    COUNT("fact_services"."service_sk") AS "scheduled_stop_count"
  FROM "fact_services" AS "fact_services"
  GROUP BY
    "fact_services"."service_type"
), "_leaf_1" AS (
  SELECT
    "fact_services"."service_type" AS "service_type",
    COUNT("fact_services"."service_sk") AS "service_count"
  FROM "fact_services" AS "fact_services"
  WHERE
    "fact_services"."service_arrival_cancelled" = FALSE
  GROUP BY
    "fact_services"."service_type"
), "_grain" AS (
  SELECT
    "_leaf_0"."service_type" AS "service_type"
  FROM "_leaf_0"
  UNION
  SELECT
    "_leaf_1"."service_type" AS "service_type"
  FROM "_leaf_1"
)
SELECT
  "_grain"."service_type" AS "service_type",
  "_leaf_1"."service_count" AS "service_count",
  "_leaf_0"."scheduled_stop_count" AS "scheduled_stop_count"
FROM "_grain"
LEFT JOIN "_leaf_0"
  ON (
    "_grain"."service_type" = "_leaf_0"."service_type"
    OR "_grain"."service_type" IS NULL
    AND "_leaf_0"."service_type" IS NULL
  )
LEFT JOIN "_leaf_1"
  ON (
    "_grain"."service_type" = "_leaf_1"."service_type"
    OR "_grain"."service_type" IS NULL
    AND "_leaf_1"."service_type" IS NULL
  )
