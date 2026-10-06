SELECT
  "products"."category" AS "category",
  SUM("order_items"."amount") AS "revenue"
FROM "main"."order_items" AS "order_items"
LEFT JOIN "main"."products" AS "products"
  ON "order_items"."product_id" = "products"."product_id"
GROUP BY
  "products"."category"
