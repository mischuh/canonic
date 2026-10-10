WITH "_leaf_0" AS (
  SELECT
    COUNT(DISTINCT "order_items"."order_id") AS "order_count"
  FROM "order_items" AS "order_items"
  LEFT JOIN "orders" AS "orders"
    ON "order_items"."order_id" = "orders"."order_id"
  LEFT JOIN "customers" AS "customers"
    ON "orders"."customer_id" = "customers"."customer_id"
  WHERE
    "orders"."status" = 'completed' AND "customers"."is_test" = FALSE
), "_leaf_1" AS (
  SELECT
    SUM("order_items"."amount") AS "revenue"
  FROM "order_items" AS "order_items"
  LEFT JOIN "orders" AS "orders"
    ON "order_items"."order_id" = "orders"."order_id"
  LEFT JOIN "customers" AS "customers"
    ON "orders"."customer_id" = "customers"."customer_id"
  WHERE
    "orders"."status" = 'completed' AND "customers"."is_test" = FALSE
)
SELECT
  "_leaf_1"."revenue" / NULLIF("_leaf_0"."order_count", 0) AS "average_order_value"
FROM "_leaf_0"
CROSS JOIN "_leaf_1"
