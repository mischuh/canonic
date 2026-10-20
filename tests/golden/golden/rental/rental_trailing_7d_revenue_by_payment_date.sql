WITH "_daily" AS (
  SELECT
    DATE("payments"."payment_date") AS "_day",
    SUM(CASE WHEN "payments"."status" = 'settled' THEN "payments"."amount" ELSE 0 END) AS "_value"
  FROM "payments" AS "payments"
  GROUP BY
    DATE("payments"."payment_date")
), "_digits" AS (
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
), "_bounds" AS (
  SELECT
    MIN("_daily"."_day") AS "_lo",
    CASE
      WHEN DATE(DATE(MAX("_daily"."_day"), '+7 days'), '-1 days') > DATE('2024-12-31')
      THEN DATE('2024-12-31')
      ELSE DATE(DATE(MAX("_daily"."_day"), '+7 days'), '-1 days')
    END AS "_hi"
  FROM "_daily"
), "_spine" AS (
  SELECT
    DATE("r"."_lo", CAST("s"."n" AS TEXT) || ' days') AS "_day"
  FROM (
    SELECT
      CASE
        WHEN "_bounds"."_lo" < DATE("_bounds"."_hi", '-99999 days')
        THEN DATE("_bounds"."_hi", '-99999 days')
        ELSE "_bounds"."_lo"
      END AS "_lo",
      "_bounds"."_hi" AS "_hi"
    FROM "_bounds"
  ) AS "r"
  CROSS JOIN (
    SELECT
      "d0"."n" + 10 * "d1"."n" + 100 * "d2"."n" + 1000 * "d3"."n" + 10000 * "d4"."n" AS "n"
    FROM "_digits" AS "d0"
    CROSS JOIN "_digits" AS "d1"
    CROSS JOIN "_digits" AS "d2"
    CROSS JOIN "_digits" AS "d3"
    CROSS JOIN "_digits" AS "d4"
  ) AS "s"
  WHERE
    DATE("r"."_lo", CAST("s"."n" AS TEXT) || ' days') <= "r"."_hi"
), "_targets" AS (
  SELECT
    "_spine"."_day" AS "_bucket",
    MAX("_spine"."_day") AS "_day"
  FROM "_spine"
  GROUP BY
    "_spine"."_day"
)
SELECT
  "t"."_bucket" AS "payment_date",
  SUM("d"."_value") AS "trailing_7d_rental_revenue"
FROM "_targets" AS "t"
INNER JOIN "_daily" AS "d"
  ON "d"."_day" <= "t"."_day" AND "d"."_day" > DATE("t"."_day", '-7 days')
GROUP BY
  "t"."_bucket"
