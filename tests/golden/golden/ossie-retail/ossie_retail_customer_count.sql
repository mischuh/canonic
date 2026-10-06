SELECT
  COUNT(DISTINCT "orders"."customer_id") AS "customer_count"
FROM "main"."orders" AS "orders"
