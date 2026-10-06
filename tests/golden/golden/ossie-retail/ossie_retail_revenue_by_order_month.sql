SELECT
  SUBSTRING("orders"."order_date", 1, 7) AS "order_month",
  SUM("order_items"."amount") AS "revenue"
FROM "main"."order_items" AS "order_items"
LEFT JOIN "main"."orders" AS "orders"
  ON "order_items"."order_id" = "orders"."order_id"
GROUP BY
  SUBSTRING("orders"."order_date", 1, 7)
