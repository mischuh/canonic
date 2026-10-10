WITH "_leaf_0" AS (
  SELECT
    SUM(CASE WHEN "payments"."status" = 'settled' THEN "payments"."amount" ELSE 0 END) AS "rental_revenue"
  FROM "payments" AS "payments"
), "_leaf_1__ranked" AS (
  SELECT
    "vehicle_inventory"."vehicle_id" AS "vehicle_id",
    "vehicle_inventory"."inventory_level" AS "inventory_level",
    ROW_NUMBER() OVER (
      PARTITION BY "vehicle_inventory"."vehicle_id"
      ORDER BY "vehicle_inventory"."snapshot_date" DESC
    ) AS "rn"
  FROM "vehicle_inventory_snapshots" AS "vehicle_inventory"
), "_leaf_1" AS (
  SELECT
    SUM("_leaf_1__ranked"."inventory_level") AS "ending_inventory"
  FROM "_leaf_1__ranked"
  WHERE
    "_leaf_1__ranked"."rn" = 1
)
SELECT
  "_leaf_1"."ending_inventory" AS "ending_inventory",
  "_leaf_0"."rental_revenue" AS "rental_revenue"
FROM "_leaf_0"
CROSS JOIN "_leaf_1"
