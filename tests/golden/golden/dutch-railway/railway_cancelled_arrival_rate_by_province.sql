WITH "num" AS (
  SELECT
    "dim_nl_provinces"."province_name" AS "province_name",
    COUNT("fact_services"."service_sk") FILTER(WHERE
      "fact_services"."service_arrival_cancelled") AS "n"
  FROM "fact_services" AS "fact_services"
  LEFT JOIN "dim_nl_train_stations" AS "dim_nl_train_stations"
    ON "fact_services"."station_sk" = "dim_nl_train_stations"."station_sk"
  LEFT JOIN "dim_nl_municipalities" AS "dim_nl_municipalities"
    ON "dim_nl_train_stations"."municipality_sk" = "dim_nl_municipalities"."municipality_sk"
  LEFT JOIN "dim_nl_provinces" AS "dim_nl_provinces"
    ON "dim_nl_municipalities"."province_sk" = "dim_nl_provinces"."province_sk"
  GROUP BY
    "dim_nl_provinces"."province_name"
), "den" AS (
  SELECT
    "dim_nl_provinces"."province_name" AS "province_name",
    COUNT("fact_services"."service_sk") AS "d"
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
  "province_name" AS "province_name",
  "n" / NULLIF("d", 0) AS "cancelled_arrival_rate"
FROM "num"
FULL JOIN "den"
  USING ("province_name")
