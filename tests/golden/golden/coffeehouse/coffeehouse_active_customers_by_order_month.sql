SELECT
  DATE_TRUNC('MONTH', "orders"."order_date") AS "order_month",
  COUNT(DISTINCT "orders"."customer_id") AS "active_customers"
FROM "orders" AS "orders"
LEFT JOIN "customers" AS "customers"
  ON "orders"."customer_id" = "customers"."customer_id"
WHERE
  "orders"."status" = 'completed' AND "customers"."is_test" = FALSE
GROUP BY
  DATE_TRUNC('MONTH', "orders"."order_date")
