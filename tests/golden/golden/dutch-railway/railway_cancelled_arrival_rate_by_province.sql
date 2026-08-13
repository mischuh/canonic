WITH "_leaf_0" AS (
  SELECT
    "dim_nl_provinces"."province_name" AS "province_name",
    COUNT("fact_services"."service_sk") FILTER(WHERE
      "fact_services"."service_arrival_cancelled") AS "cancelled_arrival_count",
    COUNT("fact_services"."service_sk") AS "scheduled_stop_count"
  FROM "fact_services" AS "fact_services"
  LEFT JOIN "dim_nl_train_stations" AS "dim_nl_train_stations"
    ON "fact_services"."station_sk" = "dim_nl_train_stations"."station_sk"
  LEFT JOIN "dim_nl_municipalities" AS "dim_nl_municipalities"
    ON "dim_nl_train_stations"."municipality_sk" = "dim_nl_municipalities"."municipality_sk"
  LEFT JOIN "dim_nl_provinces" AS "dim_nl_provinces"
    ON "dim_nl_municipalities"."province_sk" = "dim_nl_provinces"."province_sk"
  GROUP BY
    "dim_nl_provinces"."province_name"
)
SELECT
  "_leaf_0"."province_name" AS "province_name",
  "_leaf_0"."cancelled_arrival_count" / NULLIF("_leaf_0"."scheduled_stop_count", 0) AS "cancelled_arrival_rate"
FROM "_leaf_0"
