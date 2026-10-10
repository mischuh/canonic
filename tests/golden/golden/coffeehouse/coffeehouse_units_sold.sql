SELECT
  SUM("order_items"."quantity") AS "units_sold"
FROM "order_items" AS "order_items"
LEFT JOIN "orders" AS "orders"
  ON "order_items"."order_id" = "orders"."order_id"
LEFT JOIN "customers" AS "customers"
  ON "orders"."customer_id" = "customers"."customer_id"
WHERE
  "orders"."status" = 'completed' AND "customers"."is_test" = FALSE
