WITH "_leaf_0" AS (
  SELECT
    DATE("payments"."payment_date", 'start of month') AS "payment_month",
    SUM(CASE WHEN "payments"."status" = 'settled' THEN "payments"."amount" ELSE 0 END) AS "rental_revenue"
  FROM "payments" AS "payments"
  GROUP BY
    DATE("payments"."payment_date", 'start of month')
), "_leaf_1" AS (
  SELECT
    DATE(
      MIN(
        DATE(
          "payments"."payment_date",
          'start of month',
          '+1 months',
          CAST(CAST(STRFTIME('%d', "payments"."payment_date") AS INTEGER) - 1 AS TEXT) || ' days'
        ),
        DATE("payments"."payment_date", 'start of month', '+2 months', '-1 days')
      ),
      'start of month'
    ) AS "payment_month",
    SUM(CASE WHEN "payments"."status" = 'settled' THEN "payments"."amount" ELSE 0 END) AS "rental_revenue"
  FROM "payments" AS "payments"
  GROUP BY
    DATE(
      MIN(
        DATE(
          "payments"."payment_date",
          'start of month',
          '+1 months',
          CAST(CAST(STRFTIME('%d', "payments"."payment_date") AS INTEGER) - 1 AS TEXT) || ' days'
        ),
        DATE("payments"."payment_date", 'start of month', '+2 months', '-1 days')
      ),
      'start of month'
    )
), "_grain" AS (
  SELECT
    "_leaf_0"."payment_month" AS "payment_month"
  FROM "_leaf_0"
  UNION
  SELECT
    "_leaf_1"."payment_month" AS "payment_month"
  FROM "_leaf_1"
)
SELECT
  "_grain"."payment_month" AS "payment_month",
  CAST((
    (
      "_leaf_0"."rental_revenue"
    ) - (
      "_leaf_1"."rental_revenue"
    )
  ) AS REAL) / NULLIF((
    "_leaf_1"."rental_revenue"
  ), 0) AS "rental_revenue_growth_mom",
  CASE WHEN (
    "_leaf_1"."rental_revenue"
  ) = 0 THEN 1 ELSE 0 END AS "_canonic_zero_denominator_rental_revenue_growth_mom"
FROM "_grain"
LEFT JOIN "_leaf_0"
  ON (
    "_grain"."payment_month" = "_leaf_0"."payment_month"
    OR "_grain"."payment_month" IS NULL
    AND "_leaf_0"."payment_month" IS NULL
  )
LEFT JOIN "_leaf_1"
  ON (
    "_grain"."payment_month" = "_leaf_1"."payment_month"
    OR "_grain"."payment_month" IS NULL
    AND "_leaf_1"."payment_month" IS NULL
  )
