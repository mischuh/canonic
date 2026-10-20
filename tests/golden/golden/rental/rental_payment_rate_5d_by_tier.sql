WITH "_leaf_0__base" AS (
  SELECT
    "customers"."membership_tier" AS "membership_tier",
    "rentals"."rental_id" AS "_entity",
    "rentals"."pickup_date" AS "_event_time"
  FROM "rentals" AS "rentals"
  LEFT JOIN "customers" AS "customers"
    ON "rentals"."customer_id" = "customers"."customer_id"
  GROUP BY
    "customers"."membership_tier",
    "rentals"."rental_id",
    "rentals"."pickup_date"
), "_leaf_0__conversions" AS (
  SELECT
    "payments".*
  FROM "payments" AS "payments"
), "_leaf_0__attributed" AS (
  SELECT
    "payments"."payment_id" AS "_conversion_key_0",
    "_base_event"."membership_tier" AS "membership_tier",
    ROW_NUMBER() OVER (
      PARTITION BY "payments"."payment_id"
      ORDER BY "_base_event"."_event_time" DESC, "_base_event"."membership_tier" NULLS LAST
    ) AS "_attribution"
  FROM "_leaf_0__conversions" AS "payments"
  INNER JOIN "_leaf_0__base" AS "_base_event"
    ON "_base_event"."_entity" = "payments"."rental_id"
    AND "_base_event"."_event_time" <= "payments"."payment_date"
    AND "payments"."payment_date" <= DATE("_base_event"."_event_time", '+5 days')
), "_leaf_0" AS (
  SELECT
    "_attributed"."membership_tier" AS "membership_tier",
    COUNT(DISTINCT "payments"."rental_id") AS "payment_rate_5d"
  FROM "_leaf_0__conversions" AS "payments"
  JOIN "_leaf_0__attributed" AS "_attributed"
    ON "_attributed"."_conversion_key_0" = "payments"."payment_id"
  WHERE
    "_attributed"."_attribution" = 1
  GROUP BY
    "_attributed"."membership_tier"
), "_leaf_1__base" AS (
  SELECT
    "customers"."membership_tier" AS "membership_tier",
    "rentals"."rental_id" AS "_entity",
    "rentals"."pickup_date" AS "_event_time"
  FROM "rentals" AS "rentals"
  LEFT JOIN "customers" AS "customers"
    ON "rentals"."customer_id" = "customers"."customer_id"
  GROUP BY
    "customers"."membership_tier",
    "rentals"."rental_id",
    "rentals"."pickup_date"
), "_leaf_1__conversions" AS (
  SELECT
    "payments".*
  FROM "payments" AS "payments"
), "_leaf_1__attributed" AS (
  SELECT
    "payments"."payment_id" AS "_conversion_key_0",
    "_base_event"."membership_tier" AS "membership_tier",
    ROW_NUMBER() OVER (
      PARTITION BY "payments"."payment_id"
      ORDER BY "_base_event"."_event_time" DESC, "_base_event"."membership_tier" NULLS LAST
    ) AS "_attribution"
  FROM "_leaf_1__conversions" AS "payments"
  INNER JOIN "_leaf_1__base" AS "_base_event"
    ON "_base_event"."_entity" = "payments"."rental_id"
    AND "_base_event"."_event_time" <= "payments"."payment_date"
    AND "payments"."payment_date" <= DATE("_base_event"."_event_time", '+5 days')
), "_leaf_1" AS (
  SELECT
    "_attributed"."membership_tier" AS "membership_tier",
    SUM(CASE WHEN "payments"."status" = 'settled' THEN "payments"."amount" ELSE 0 END) AS "revenue_paid_within_5d"
  FROM "_leaf_1__conversions" AS "payments"
  JOIN "_leaf_1__attributed" AS "_attributed"
    ON "_attributed"."_conversion_key_0" = "payments"."payment_id"
  WHERE
    "_attributed"."_attribution" = 1
  GROUP BY
    "_attributed"."membership_tier"
), "_leaf_2" AS (
  SELECT
    "customers"."membership_tier" AS "membership_tier",
    COUNT(CASE WHEN "rentals"."status" = 'completed' THEN "rentals"."rental_id" END) AS "rental_count"
  FROM "rentals" AS "rentals"
  LEFT JOIN "customers" AS "customers"
    ON "rentals"."customer_id" = "customers"."customer_id"
  GROUP BY
    "customers"."membership_tier"
), "_grain" AS (
  SELECT
    "_leaf_0"."membership_tier" AS "membership_tier"
  FROM "_leaf_0"
  UNION
  SELECT
    "_leaf_1"."membership_tier" AS "membership_tier"
  FROM "_leaf_1"
  UNION
  SELECT
    "_leaf_2"."membership_tier" AS "membership_tier"
  FROM "_leaf_2"
)
SELECT
  "_grain"."membership_tier" AS "membership_tier",
  CAST(COALESCE((
    "_leaf_0"."payment_rate_5d"
  ), 0) AS REAL) / NULLIF((
    "_leaf_2"."rental_count"
  ), 0) AS "payment_rate_5d",
  COALESCE(
    (
      "_leaf_1"."revenue_paid_within_5d"
    ),
    CASE WHEN NOT (
      "_leaf_2"."rental_count"
    ) IS NULL THEN 0 END
  ) AS "revenue_paid_within_5d",
  "_leaf_2"."rental_count" AS "rental_count",
  CASE WHEN (
    "_leaf_2"."rental_count"
  ) = 0 THEN 1 ELSE 0 END AS "_canonic_zero_denominator_payment_rate_5d"
FROM "_grain"
LEFT JOIN "_leaf_0"
  ON (
    "_grain"."membership_tier" = "_leaf_0"."membership_tier"
    OR "_grain"."membership_tier" IS NULL
    AND "_leaf_0"."membership_tier" IS NULL
  )
LEFT JOIN "_leaf_1"
  ON (
    "_grain"."membership_tier" = "_leaf_1"."membership_tier"
    OR "_grain"."membership_tier" IS NULL
    AND "_leaf_1"."membership_tier" IS NULL
  )
LEFT JOIN "_leaf_2"
  ON (
    "_grain"."membership_tier" = "_leaf_2"."membership_tier"
    OR "_grain"."membership_tier" IS NULL
    AND "_leaf_2"."membership_tier" IS NULL
  )
