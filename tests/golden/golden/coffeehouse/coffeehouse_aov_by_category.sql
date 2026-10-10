WITH "_leaf_0" AS (
  SELECT
    "products"."category" AS "category",
    COUNT(DISTINCT "order_items"."order_id") AS "order_count"
  FROM "order_items" AS "order_items"
  LEFT JOIN "orders" AS "orders"
    ON "order_items"."order_id" = "orders"."order_id"
  LEFT JOIN "customers" AS "customers"
    ON "orders"."customer_id" = "customers"."customer_id"
  LEFT JOIN "products" AS "products"
    ON "order_items"."product_id" = "products"."product_id"
  WHERE
    "orders"."status" = 'completed' AND "customers"."is_test" = FALSE
  GROUP BY
    "products"."category"
), "_leaf_1" AS (
  SELECT
    "products"."category" AS "category",
    SUM("order_items"."amount") AS "revenue"
  FROM "order_items" AS "order_items"
  LEFT JOIN "orders" AS "orders"
    ON "order_items"."order_id" = "orders"."order_id"
  LEFT JOIN "customers" AS "customers"
    ON "orders"."customer_id" = "customers"."customer_id"
  LEFT JOIN "products" AS "products"
    ON "order_items"."product_id" = "products"."product_id"
  WHERE
    "orders"."status" = 'completed' AND "customers"."is_test" = FALSE
  GROUP BY
    "products"."category"
), "_grain" AS (
  SELECT
    "_leaf_0"."category" AS "category"
  FROM "_leaf_0"
  UNION
  SELECT
    "_leaf_1"."category" AS "category"
  FROM "_leaf_1"
)
SELECT
  "_grain"."category" AS "category",
  "_leaf_1"."revenue" / NULLIF("_leaf_0"."order_count", 0) AS "average_order_value"
FROM "_grain"
LEFT JOIN "_leaf_0"
  ON (
    "_grain"."category" = "_leaf_0"."category"
    OR "_grain"."category" IS NULL
    AND "_leaf_0"."category" IS NULL
  )
LEFT JOIN "_leaf_1"
  ON (
    "_grain"."category" = "_leaf_1"."category"
    OR "_grain"."category" IS NULL
    AND "_leaf_1"."category" IS NULL
  )
