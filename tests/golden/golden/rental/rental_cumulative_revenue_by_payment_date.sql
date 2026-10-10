WITH "_leaf_0" AS (
  SELECT
    "payments"."payment_date" AS "payment_date",
    SUM(CASE WHEN "payments"."status" = 'settled' THEN "payments"."amount" ELSE 0 END) AS "total_paid"
  FROM "payments" AS "payments"
  GROUP BY
    "payments"."payment_date"
), "_grain" AS (
  SELECT
    "_leaf_0"."payment_date" AS "payment_date"
  FROM "_leaf_0"
)
SELECT
  "_grain"."payment_date" AS "payment_date",
  CASE
    WHEN COUNT("_leaf_0"."total_paid") OVER (
      ORDER BY CASE WHEN "_grain"."payment_date" IS NULL THEN 1 ELSE 0 END ASC, "_grain"."payment_date" ASC
      ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) = 0
    THEN NULL
    ELSE SUM(COALESCE("_leaf_0"."total_paid", 0)) OVER (
      ORDER BY CASE WHEN "_grain"."payment_date" IS NULL THEN 1 ELSE 0 END ASC, "_grain"."payment_date" ASC
      ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    )
  END AS "cumulative_rental_revenue"
FROM "_grain"
LEFT JOIN "_leaf_0"
  ON (
    "_grain"."payment_date" = "_leaf_0"."payment_date"
    OR "_grain"."payment_date" IS NULL
    AND "_leaf_0"."payment_date" IS NULL
  )
