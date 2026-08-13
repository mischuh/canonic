WITH "_leaf_0" AS (
  SELECT
    SUM("order_items"."quantity") AS "units_sold"
  FROM "main"."order_items" AS "order_items"
), "_leaf_1" AS (
  SELECT
    SUM("orders"."amount") AS "revenue"
  FROM "main"."orders" AS "orders"
)
SELECT
  "_leaf_0"."units_sold" AS "units_sold",
  "_leaf_1"."revenue" AS "revenue"
FROM "_leaf_0"
CROSS JOIN "_leaf_1"
