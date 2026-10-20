WITH "_daily" AS (
  SELECT
    "payments"."method" AS "method",
    DATE("payments"."payment_date") AS "_day",
    SUM(CASE WHEN "payments"."status" = 'settled' THEN "payments"."amount" ELSE 0 END) AS "_value"
  FROM "payments" AS "payments"
  GROUP BY
    "payments"."method",
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
      WHEN DATE(
        MIN(
          DATE(
            DATE(DATE(MAX("_daily"."_day"), 'start of month')),
            'start of month',
            '+1 months',
            CAST(CAST(STRFTIME('%d', DATE(DATE(MAX("_daily"."_day"), 'start of month'))) AS INTEGER) - 1 AS TEXT) || ' days'
          ),
          DATE(
            DATE(DATE(MAX("_daily"."_day"), 'start of month')),
            'start of month',
            '+2 months',
            '-1 days'
          )
        ),
        '-1 days'
      ) > DATE('2024-12-31')
      THEN DATE('2024-12-31')
      ELSE DATE(
        MIN(
          DATE(
            DATE(DATE(MAX("_daily"."_day"), 'start of month')),
            'start of month',
            '+1 months',
            CAST(CAST(STRFTIME('%d', DATE(DATE(MAX("_daily"."_day"), 'start of month'))) AS INTEGER) - 1 AS TEXT) || ' days'
          ),
          DATE(
            DATE(DATE(MAX("_daily"."_day"), 'start of month')),
            'start of month',
            '+2 months',
            '-1 days'
          )
        ),
        '-1 days'
      )
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
  WHERE
    "_spine"."_day" >= '2024-10-25'
  GROUP BY
    "_spine"."_day"
)
SELECT
  "t"."_bucket" AS "payment_date",
  "d"."method" AS "method",
  SUM("d"."_value") AS "rental_revenue_month_to_date"
FROM "_targets" AS "t"
INNER JOIN "_daily" AS "d"
  ON "d"."_day" <= "t"."_day"
  AND "d"."_day" >= DATE(DATE("t"."_day", 'start of month'))
GROUP BY
  "t"."_bucket",
  "d"."method"
