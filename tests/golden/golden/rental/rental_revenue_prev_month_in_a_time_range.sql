WITH "_leaf_0" AS (
  SELECT
    "payments"."payment_date" AS "payment_date",
    SUM(CASE WHEN "payments"."status" = 'settled' THEN "payments"."amount" ELSE 0 END) AS "rental_revenue"
  FROM "payments" AS "payments"
  WHERE
    "payments"."payment_date" >= '2024-10-01'
    AND "payments"."payment_date" < '2024-11-01'
  GROUP BY
    "payments"."payment_date"
), "_leaf_1" AS (
  SELECT
    MIN(
      DATE(
        "payments"."payment_date",
        'start of month',
        '+1 months',
        CAST(CAST(STRFTIME('%d', "payments"."payment_date") AS INTEGER) - 1 AS TEXT) || ' days'
      ),
      DATE("payments"."payment_date", 'start of month', '+2 months', '-1 days')
    ) AS "payment_date",
    SUM(CASE WHEN "payments"."status" = 'settled' THEN "payments"."amount" ELSE 0 END) AS "rental_revenue"
  FROM "payments" AS "payments"
  WHERE
    (
      MIN(
        DATE(
          "payments"."payment_date",
          'start of month',
          '+1 months',
          CAST(CAST(STRFTIME('%d', "payments"."payment_date") AS INTEGER) - 1 AS TEXT) || ' days'
        ),
        DATE("payments"."payment_date", 'start of month', '+2 months', '-1 days')
      ) >= '2024-10-01'
      AND MIN(
        DATE(
          "payments"."payment_date",
          'start of month',
          '+1 months',
          CAST(CAST(STRFTIME('%d', "payments"."payment_date") AS INTEGER) - 1 AS TEXT) || ' days'
        ),
        DATE("payments"."payment_date", 'start of month', '+2 months', '-1 days')
      ) < '2024-11-01'
    )
    AND "payments"."payment_date" >= '2024-08-29'
    AND "payments"."payment_date" < '2024-10-04'
  GROUP BY
    MIN(
      DATE(
        "payments"."payment_date",
        'start of month',
        '+1 months',
        CAST(CAST(STRFTIME('%d', "payments"."payment_date") AS INTEGER) - 1 AS TEXT) || ' days'
      ),
      DATE("payments"."payment_date", 'start of month', '+2 months', '-1 days')
    )
), "_grain" AS (
  SELECT
    "_leaf_0"."payment_date" AS "payment_date"
  FROM "_leaf_0"
  UNION
  SELECT
    "_leaf_1"."payment_date" AS "payment_date"
  FROM "_leaf_1"
)
SELECT
  "_grain"."payment_date" AS "payment_date",
  "_leaf_0"."rental_revenue" AS "rental_revenue",
  "_leaf_1"."rental_revenue" AS "rental_revenue_prev_month"
FROM "_grain"
LEFT JOIN "_leaf_0"
  ON (
    "_grain"."payment_date" = "_leaf_0"."payment_date"
    OR "_grain"."payment_date" IS NULL
    AND "_leaf_0"."payment_date" IS NULL
  )
LEFT JOIN "_leaf_1"
  ON (
    "_grain"."payment_date" = "_leaf_1"."payment_date"
    OR "_grain"."payment_date" IS NULL
    AND "_leaf_1"."payment_date" IS NULL
  )
