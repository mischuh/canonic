WITH "_leaf_0__base" AS (
  SELECT
    "rentals"."rental_id" AS "_entity",
    "rentals"."pickup_date" AS "_event_time"
  FROM "rentals" AS "rentals"
  WHERE
    "rentals"."pickup_date" >= '2024-01-01'
    AND "rentals"."pickup_date" < '2024-03-22'
  GROUP BY
    "rentals"."rental_id",
    "rentals"."pickup_date"
), "_leaf_0__conversions" AS (
  SELECT
    "payments".*
  FROM "payments" AS "payments"
), "_leaf_0__attributed" AS (
  SELECT
    "payments"."payment_id" AS "_conversion_key_0",
    ROW_NUMBER() OVER (PARTITION BY "payments"."payment_id" ORDER BY "_base_event"."_event_time" DESC) AS "_attribution"
  FROM "_leaf_0__conversions" AS "payments"
  INNER JOIN "_leaf_0__base" AS "_base_event"
    ON "_base_event"."_entity" = "payments"."rental_id"
    AND "_base_event"."_event_time" <= "payments"."payment_date"
    AND "payments"."payment_date" <= DATE("_base_event"."_event_time", '+5 days')
), "_leaf_0" AS (
  SELECT
    COUNT(DISTINCT "payments"."rental_id") AS "payment_rate_5d"
  FROM "_leaf_0__conversions" AS "payments"
  JOIN "_leaf_0__attributed" AS "_attributed"
    ON "_attributed"."_conversion_key_0" = "payments"."payment_id"
  WHERE
    "_attributed"."_attribution" = 1
), "_leaf_1" AS (
  SELECT
    COUNT(CASE WHEN "rentals"."status" = 'completed' THEN "rentals"."rental_id" END) AS "rental_count"
  FROM "rentals" AS "rentals"
  WHERE
    "rentals"."pickup_date" >= '2024-01-01'
    AND "rentals"."pickup_date" < '2024-03-22'
)
SELECT
  CAST(COALESCE((
    "_leaf_0"."payment_rate_5d"
  ), 0) AS REAL) / NULLIF((
    "_leaf_1"."rental_count"
  ), 0) AS "payment_rate_5d",
  "_leaf_1"."rental_count" AS "rental_count"
FROM "_leaf_0"
CROSS JOIN "_leaf_1"
