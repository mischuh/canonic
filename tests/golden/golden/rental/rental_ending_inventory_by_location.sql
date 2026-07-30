WITH "ranked" AS (
  SELECT
    "vehicle_inventory"."location_id" AS "location_id",
    "vehicle_inventory"."vehicle_id" AS "vehicle_id",
    "vehicle_inventory"."inventory_level" AS "inventory_level",
    ROW_NUMBER() OVER (
      PARTITION BY "vehicle_inventory"."vehicle_id"
      ORDER BY "vehicle_inventory"."snapshot_date" DESC
    ) AS "rn"
  FROM "vehicle_inventory_snapshots" AS "vehicle_inventory"
)
SELECT
  "ranked"."location_id" AS "location_id",
  SUM("ranked"."inventory_level") AS "ending_inventory"
FROM "ranked"
WHERE
  "ranked"."rn" = 1
GROUP BY
  "ranked"."location_id"
