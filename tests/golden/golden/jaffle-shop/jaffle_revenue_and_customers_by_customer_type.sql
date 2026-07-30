WITH "_leaf0" AS (
  SELECT
    "customers"."customer_type" AS "customer_type",
    SUM("orders"."amount") AS "revenue"
  FROM "main"."orders" AS "orders"
  LEFT JOIN "main"."customers" AS "customers"
    ON "orders"."customer_id" = "customers"."customer_id"
  GROUP BY
    "customers"."customer_type"
), "_leaf1" AS (
  SELECT
    "customers"."customer_type" AS "customer_type",
    COUNT(*) AS "row_count"
  FROM "main"."customers" AS "customers"
  GROUP BY
    "customers"."customer_type"
)
SELECT
  "customer_type" AS "customer_type",
  "_leaf0"."revenue" AS "revenue",
  "_leaf1"."row_count" AS "row_count"
FROM "_leaf0"
FULL JOIN "_leaf1"
  USING ("customer_type")
