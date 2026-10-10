WITH "_leaf_0" AS (
  SELECT
    "customers"."customer_type" AS "customer_type",
    COUNT(*) AS "num_customers"
  FROM "main"."customers" AS "customers"
  GROUP BY
    "customers"."customer_type"
), "_leaf_1" AS (
  SELECT
    "customers"."customer_type" AS "customer_type",
    SUM("orders"."amount") AS "revenue"
  FROM "main"."orders" AS "orders"
  LEFT JOIN "main"."customers" AS "customers"
    ON "orders"."customer_id" = "customers"."customer_id"
  GROUP BY
    "customers"."customer_type"
), "_grain" AS (
  SELECT
    "_leaf_0"."customer_type" AS "customer_type"
  FROM "_leaf_0"
  UNION
  SELECT
    "_leaf_1"."customer_type" AS "customer_type"
  FROM "_leaf_1"
)
SELECT
  "_grain"."customer_type" AS "customer_type",
  "_leaf_1"."revenue" AS "revenue",
  "_leaf_0"."num_customers" AS "num_customers"
FROM "_grain"
LEFT JOIN "_leaf_0"
  ON (
    "_grain"."customer_type" = "_leaf_0"."customer_type"
    OR "_grain"."customer_type" IS NULL
    AND "_leaf_0"."customer_type" IS NULL
  )
LEFT JOIN "_leaf_1"
  ON (
    "_grain"."customer_type" = "_leaf_1"."customer_type"
    OR "_grain"."customer_type" IS NULL
    AND "_leaf_1"."customer_type" IS NULL
  )
