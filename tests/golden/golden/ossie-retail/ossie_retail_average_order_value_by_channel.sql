WITH "_leaf_0" AS (
  SELECT
    "orders"."channel" AS "channel",
    SUM("order_items"."amount") AS "revenue"
  FROM "main"."order_items" AS "order_items"
  LEFT JOIN "main"."orders" AS "orders"
    ON "order_items"."order_id" = "orders"."order_id"
  GROUP BY
    "orders"."channel"
), "_leaf_1" AS (
  SELECT
    "orders"."channel" AS "channel",
    COUNT("orders"."order_id") AS "order_count"
  FROM "main"."orders" AS "orders"
  GROUP BY
    "orders"."channel"
), "_grain" AS (
  SELECT
    "_leaf_0"."channel" AS "channel"
  FROM "_leaf_0"
  UNION
  SELECT
    "_leaf_1"."channel" AS "channel"
  FROM "_leaf_1"
)
SELECT
  "_grain"."channel" AS "channel",
  CAST("_leaf_0"."revenue" AS REAL) / NULLIF("_leaf_1"."order_count", 0) AS "average_order_value"
FROM "_grain"
LEFT JOIN "_leaf_0"
  ON (
    "_grain"."channel" = "_leaf_0"."channel"
    OR "_grain"."channel" IS NULL
    AND "_leaf_0"."channel" IS NULL
  )
LEFT JOIN "_leaf_1"
  ON (
    "_grain"."channel" = "_leaf_1"."channel"
    OR "_grain"."channel" IS NULL
    AND "_leaf_1"."channel" IS NULL
  )
