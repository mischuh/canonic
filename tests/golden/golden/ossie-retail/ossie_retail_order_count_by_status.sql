SELECT
  "orders"."status" AS "status",
  COUNT("orders"."order_id") AS "order_count"
FROM "main"."orders" AS "orders"
GROUP BY
  "orders"."status"
