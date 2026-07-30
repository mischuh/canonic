SELECT
  COUNT(
    CASE
      WHEN "fact_services"."service_arrival_cancelled" = FALSE
      THEN "fact_services"."service_sk"
    END
  ) AS "service_count",
  COUNT("fact_services"."service_sk") AS "scheduled_stop_count"
FROM "fact_services" AS "fact_services"
