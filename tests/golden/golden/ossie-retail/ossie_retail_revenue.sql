SELECT
  SUM("order_items"."amount") AS "revenue"
FROM "main"."order_items" AS "order_items"
