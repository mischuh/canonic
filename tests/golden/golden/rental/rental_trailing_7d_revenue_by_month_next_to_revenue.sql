WITH "_leaf_0__daily" AS (
  SELECT
    DATE("payments"."payment_date") AS "_day",
    SUM(CASE WHEN "payments"."status" = 'settled' THEN "payments"."amount" ELSE 0 END) AS "_value"
  FROM "payments" AS "payments"
  GROUP BY
    DATE("payments"."payment_date")
), "_leaf_0__digits" AS (
  SELECT
    0 AS "n"
  UNION ALL
  SELECT
    1 AS "n"
  UNION ALL
  SELECT
    2 AS "n"
  UNION ALL
  SELECT
    3 AS "n"
  UNION ALL
  SELECT
    4 AS "n"
  UNION ALL
  SELECT
    5 AS "n"
  UNION ALL
  SELECT
    6 AS "n"
  UNION ALL
  SELECT
    7 AS "n"
  UNION ALL
  SELECT
    8 AS "n"
  UNION ALL
  SELECT
    9 AS "n"
), "_leaf_0__bounds" AS (
  SELECT
    MIN("_leaf_0__daily"."_day") AS "_lo",
    CASE
      WHEN DATE(DATE(MAX("_leaf_0__daily"."_day"), '+7 days'), '-1 days') > DATE('2024-12-31')
      THEN DATE('2024-12-31')
      ELSE DATE(DATE(MAX("_leaf_0__daily"."_day"), '+7 days'), '-1 days')
    END AS "_hi"
  FROM "_leaf_0__daily"
), "_leaf_0__spine" AS (
  SELECT
    DATE("r"."_lo", CAST("s"."n" AS TEXT) || ' days') AS "_day"
  FROM (
    SELECT
      CASE
        WHEN "_leaf_0__bounds"."_lo" < DATE("_leaf_0__bounds"."_hi", '-99999 days')
        THEN DATE("_leaf_0__bounds"."_hi", '-99999 days')
        ELSE "_leaf_0__bounds"."_lo"
      END AS "_lo",
      "_leaf_0__bounds"."_hi" AS "_hi"
    FROM "_leaf_0__bounds"
  ) AS "r"
  CROSS JOIN (
    SELECT
      "d0"."n" + 10 * "d1"."n" + 100 * "d2"."n" + 1000 * "d3"."n" + 10000 * "d4"."n" AS "n"
    FROM "_leaf_0__digits" AS "d0"
    CROSS JOIN "_leaf_0__digits" AS "d1"
    CROSS JOIN "_leaf_0__digits" AS "d2"
    CROSS JOIN "_leaf_0__digits" AS "d3"
    CROSS JOIN "_leaf_0__digits" AS "d4"
  ) AS "s"
  WHERE
    DATE("r"."_lo", CAST("s"."n" AS TEXT) || ' days') <= "r"."_hi"
), "_leaf_0__targets" AS (
  SELECT
    DATE("_leaf_0__spine"."_day", 'start of month') AS "_bucket",
    MAX("_leaf_0__spine"."_day") AS "_day"
  FROM "_leaf_0__spine"
  GROUP BY
    DATE("_leaf_0__spine"."_day", 'start of month')
), "_leaf_0" AS (
  SELECT
    "t"."_bucket" AS "payment_month",
    SUM("d"."_value") AS "trailing_7d_rental_revenue"
  FROM "_leaf_0__targets" AS "t"
  INNER JOIN "_leaf_0__daily" AS "d"
    ON "d"."_day" <= "t"."_day" AND "d"."_day" > DATE("t"."_day", '-7 days')
  GROUP BY
    "t"."_bucket"
), "_leaf_1" AS (
  SELECT
    DATE("payments"."payment_date", 'start of month') AS "payment_month",
    SUM(CASE WHEN "payments"."status" = 'settled' THEN "payments"."amount" ELSE 0 END) AS "rental_revenue"
  FROM "payments" AS "payments"
  GROUP BY
    DATE("payments"."payment_date", 'start of month')
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
  "_leaf_0"."trailing_7d_rental_revenue" AS "trailing_7d_rental_revenue",
  "_leaf_1"."rental_revenue" AS "rental_revenue"
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
