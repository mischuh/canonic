SELECT
  COUNT("fact_services"."service_sk") AS "service_count"
FROM "fact_services" AS "fact_services"
WHERE
  "fact_services"."service_arrival_cancelled" = FALSE
