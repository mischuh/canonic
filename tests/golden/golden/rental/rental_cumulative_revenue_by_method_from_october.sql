WITH "_leaf_0__visible" AS (
  SELECT DISTINCT
    "payments"."payment_date" AS "payment_date"
  FROM "payments" AS "payments"
  WHERE
    "payments"."payment_date" >= '2024-10-01'
), "_leaf_0" AS (
  SELECT
    "payments"."payment_date" AS "payment_date",
    "payments"."method" AS "method",
    SUM(CASE WHEN "payments"."status" = 'settled' THEN "payments"."amount" ELSE 0 END) AS "total_paid"
  FROM "payments" AS "payments"
  GROUP BY
    "payments"."payment_date",
    "payments"."method"
), "_leaf_0__order_rank" AS (
  SELECT
    "_leaf_0"."payment_date" AS "payment_date",
    "_leaf_0"."method" AS "method",
    DENSE_RANK() OVER (
      ORDER BY CASE WHEN "_leaf_0"."payment_date" IS NULL THEN 1 ELSE 0 END ASC, "_leaf_0"."payment_date" ASC
    ) AS "_order_rank"
  FROM "_leaf_0"
), "_leaf_0__fill" AS (
  SELECT
    "t"."payment_date" AS "payment_date",
    "p"."method" AS "method"
  FROM (
    SELECT DISTINCT
      "_leaf_0__order_rank"."payment_date",
      "_leaf_0__order_rank"."_order_rank"
    FROM "_leaf_0__order_rank"
  ) AS "t"
  CROSS JOIN (
    SELECT
      "_leaf_0__order_rank"."method",
      MIN("_leaf_0__order_rank"."_order_rank") AS "_first_rank"
    FROM "_leaf_0__order_rank"
    GROUP BY
      "_leaf_0__order_rank"."method"
  ) AS "p"
  WHERE
    "t"."_order_rank" >= "p"."_first_rank"
), "_grain" AS (
  SELECT
    "_leaf_0"."payment_date" AS "payment_date",
    "_leaf_0"."method" AS "method"
  FROM "_leaf_0"
  UNION
  SELECT
    "_leaf_0__fill"."payment_date" AS "payment_date",
    "_leaf_0__fill"."method" AS "method"
  FROM "_leaf_0__fill"
), "_accumulated" AS (
  SELECT
    "_grain"."payment_date" AS "payment_date",
    "_grain"."method" AS "method",
    CASE
      WHEN COUNT("_leaf_0"."total_paid") OVER (
        PARTITION BY "_grain"."method"
        ORDER BY CASE WHEN "_grain"."payment_date" IS NULL THEN 1 ELSE 0 END ASC, "_grain"."payment_date" ASC
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
      ) = 0
      THEN NULL
      ELSE SUM(COALESCE("_leaf_0"."total_paid", 0)) OVER (
        PARTITION BY "_grain"."method"
        ORDER BY CASE WHEN "_grain"."payment_date" IS NULL THEN 1 ELSE 0 END ASC, "_grain"."payment_date" ASC
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
      )
    END AS "_m0"
  FROM "_grain"
  LEFT JOIN "_leaf_0"
    ON (
      "_grain"."payment_date" = "_leaf_0"."payment_date"
      OR "_grain"."payment_date" IS NULL
      AND "_leaf_0"."payment_date" IS NULL
    )
    AND (
      "_grain"."method" = "_leaf_0"."method"
      OR "_grain"."method" IS NULL
      AND "_leaf_0"."method" IS NULL
    )
), "_visible_grain" AS (
  SELECT
    "_leaf_0"."payment_date" AS "payment_date",
    "_leaf_0"."method" AS "method"
  FROM "_leaf_0"
  INNER JOIN "_leaf_0__visible"
    ON (
      "_leaf_0"."payment_date" = "_leaf_0__visible"."payment_date"
      OR "_leaf_0"."payment_date" IS NULL
      AND "_leaf_0__visible"."payment_date" IS NULL
    )
  UNION
  SELECT
    "_leaf_0__fill"."payment_date" AS "payment_date",
    "_leaf_0__fill"."method" AS "method"
  FROM "_leaf_0__fill"
  INNER JOIN "_leaf_0__visible"
    ON (
      "_leaf_0__fill"."payment_date" = "_leaf_0__visible"."payment_date"
      OR "_leaf_0__fill"."payment_date" IS NULL
      AND "_leaf_0__visible"."payment_date" IS NULL
    )
)
SELECT
  "_accumulated"."payment_date" AS "payment_date",
  "_accumulated"."method" AS "method",
  "_accumulated"."_m0" AS "cumulative_rental_revenue"
FROM "_accumulated"
INNER JOIN "_visible_grain"
  ON (
    "_accumulated"."payment_date" = "_visible_grain"."payment_date"
    OR "_accumulated"."payment_date" IS NULL
    AND "_visible_grain"."payment_date" IS NULL
  )
  AND (
    "_accumulated"."method" = "_visible_grain"."method"
    OR "_accumulated"."method" IS NULL
    AND "_visible_grain"."method" IS NULL
  )
