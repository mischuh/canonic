WITH "_leaf_0" AS (
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
  WHERE
    MIN(
      DATE(
        "payments"."payment_date",
        'start of month',
        '+1 months',
        CAST(CAST(STRFTIME('%d', "payments"."payment_date") AS INTEGER) - 1 AS TEXT) || ' days'
      ),
      DATE("payments"."payment_date", 'start of month', '+2 months', '-1 days')
    ) >= '2024-10-01'
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
)
SELECT
  "_leaf_0"."payment_month" AS "payment_month",
  "_leaf_0"."rental_revenue" AS "rental_revenue_prev_month"
FROM "_leaf_0"
