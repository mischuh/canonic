SELECT
  "stores"."region" AS "region",
  SUM("order_items"."amount") AS "revenue"
FROM "main"."order_items" AS "order_items"
LEFT JOIN "main"."orders" AS "orders"
  ON "order_items"."order_id" = "orders"."order_id"
LEFT JOIN "main"."stores" AS "stores"
  ON "orders"."store_id" = "stores"."store_id"
GROUP BY
  "stores"."region"
