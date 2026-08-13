WITH "_leaf_0" AS (
  SELECT
    COUNT("fact_services"."service_sk") AS "scheduled_stop_count"
  FROM "fact_services" AS "fact_services"
), "_leaf_1" AS (
  SELECT
    COUNT("fact_services"."service_sk") AS "service_count"
  FROM "fact_services" AS "fact_services"
  WHERE
    "fact_services"."service_arrival_cancelled" = FALSE
)
SELECT
  "_leaf_1"."service_count" AS "service_count",
  "_leaf_0"."scheduled_stop_count" AS "scheduled_stop_count"
FROM "_leaf_0"
CROSS JOIN "_leaf_1"
