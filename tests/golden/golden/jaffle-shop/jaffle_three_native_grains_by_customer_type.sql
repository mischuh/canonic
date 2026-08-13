WITH "_leaf_0" AS (
  SELECT
    "customers"."customer_type" AS "customer_type",
    COUNT(*) AS "row_count"
  FROM "main"."customers" AS "customers"
  GROUP BY
    "customers"."customer_type"
), "_leaf_1" AS (
  SELECT
    "customers"."customer_type" AS "customer_type",
    SUM("order_items"."quantity") AS "units_sold"
  FROM "main"."order_items" AS "order_items"
  LEFT JOIN "main"."orders" AS "orders"
    ON "order_items"."order_id" = "orders"."order_id"
  LEFT JOIN "main"."customers" AS "customers"
    ON "orders"."customer_id" = "customers"."customer_id"
  GROUP BY
    "customers"."customer_type"
), "_leaf_2" AS (
  SELECT
    "customers"."customer_type" AS "customer_type",
    SUM("orders"."amount") AS "revenue"
  FROM "main"."orders" AS "orders"
  LEFT JOIN "main"."customers" AS "customers"
    ON "orders"."customer_id" = "customers"."customer_id"
  GROUP BY
    "customers"."customer_type"
), "_grain" AS (
  SELECT
    "_leaf_0"."customer_type" AS "customer_type"
  FROM "_leaf_0"
  UNION
  SELECT
    "_leaf_1"."customer_type" AS "customer_type"
  FROM "_leaf_1"
  UNION
  SELECT
    "_leaf_2"."customer_type" AS "customer_type"
  FROM "_leaf_2"
)
SELECT
  "_grain"."customer_type" AS "customer_type",
  "_leaf_2"."revenue" AS "revenue",
  "_leaf_0"."row_count" AS "row_count",
  "_leaf_1"."units_sold" AS "units_sold"
FROM "_grain"
LEFT JOIN "_leaf_0"
  ON (
    "_grain"."customer_type" = "_leaf_0"."customer_type"
    OR "_grain"."customer_type" IS NULL
    AND "_leaf_0"."customer_type" IS NULL
  )
LEFT JOIN "_leaf_1"
  ON (
    "_grain"."customer_type" = "_leaf_1"."customer_type"
    OR "_grain"."customer_type" IS NULL
    AND "_leaf_1"."customer_type" IS NULL
  )
LEFT JOIN "_leaf_2"
  ON (
    "_grain"."customer_type" = "_leaf_2"."customer_type"
    OR "_grain"."customer_type" IS NULL
    AND "_leaf_2"."customer_type" IS NULL
  )
