WITH "_leaf_0" AS (
  SELECT
    SUM("order_items"."amount") AS "revenue"
  FROM "order_items" AS "order_items"
  LEFT JOIN "orders" AS "orders"
    ON "order_items"."order_id" = "orders"."order_id"
  LEFT JOIN "customers" AS "customers"
    ON "orders"."customer_id" = "customers"."customer_id"
  WHERE
    "orders"."status" = 'completed' AND "customers"."is_test" = FALSE
), "_leaf_1" AS (
  SELECT
    COUNT(DISTINCT "orders"."customer_id") AS "active_customers"
  FROM "orders" AS "orders"
  LEFT JOIN "customers" AS "customers"
    ON "orders"."customer_id" = "customers"."customer_id"
  WHERE
    "orders"."status" = 'completed' AND "customers"."is_test" = FALSE
)
SELECT
  "_leaf_0"."revenue" / NULLIF("_leaf_1"."active_customers", 0) AS "revenue_per_customer"
FROM "_leaf_0"
CROSS JOIN "_leaf_1"
