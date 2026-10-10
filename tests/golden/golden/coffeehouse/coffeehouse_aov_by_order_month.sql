WITH "_leaf_0" AS (
  SELECT
    DATE_TRUNC('MONTH', "orders"."order_date") AS "order_month",
    COUNT(DISTINCT "order_items"."order_id") AS "order_count"
  FROM "order_items" AS "order_items"
  LEFT JOIN "orders" AS "orders"
    ON "order_items"."order_id" = "orders"."order_id"
  LEFT JOIN "customers" AS "customers"
    ON "orders"."customer_id" = "customers"."customer_id"
  WHERE
    "orders"."status" = 'completed' AND "customers"."is_test" = FALSE
  GROUP BY
    DATE_TRUNC('MONTH', "orders"."order_date")
), "_leaf_1" AS (
  SELECT
    DATE_TRUNC('MONTH', "orders"."order_date") AS "order_month",
    SUM("order_items"."amount") AS "revenue"
  FROM "order_items" AS "order_items"
  LEFT JOIN "orders" AS "orders"
    ON "order_items"."order_id" = "orders"."order_id"
  LEFT JOIN "customers" AS "customers"
    ON "orders"."customer_id" = "customers"."customer_id"
  WHERE
    "orders"."status" = 'completed' AND "customers"."is_test" = FALSE
  GROUP BY
    DATE_TRUNC('MONTH', "orders"."order_date")
), "_grain" AS (
  SELECT
    "_leaf_0"."order_month" AS "order_month"
  FROM "_leaf_0"
  UNION
  SELECT
    "_leaf_1"."order_month" AS "order_month"
  FROM "_leaf_1"
)
SELECT
  "_grain"."order_month" AS "order_month",
  "_leaf_1"."revenue" / NULLIF("_leaf_0"."order_count", 0) AS "average_order_value"
FROM "_grain"
LEFT JOIN "_leaf_0"
  ON (
    "_grain"."order_month" = "_leaf_0"."order_month"
    OR "_grain"."order_month" IS NULL
    AND "_leaf_0"."order_month" IS NULL
  )
LEFT JOIN "_leaf_1"
  ON (
    "_grain"."order_month" = "_leaf_1"."order_month"
    OR "_grain"."order_month" IS NULL
    AND "_leaf_1"."order_month" IS NULL
  )
