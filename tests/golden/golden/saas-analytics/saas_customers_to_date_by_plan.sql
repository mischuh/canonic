WITH "_leaf_0" AS (
  SELECT
    DATE_TRUNC('DAY', "fct_subscription_events"."event_date") AS "event_date",
    "dim_plan"."plan_name" AS "plan_name",
    COUNT(
      CASE
        WHEN "fct_subscription_events"."event_type" = 'new'
        THEN "fct_subscription_events"."event_id"
      END
    ) AS "new_customer_count"
  FROM "fct_subscription_events" AS "fct_subscription_events"
  LEFT JOIN "dim_plan" AS "dim_plan"
    ON "fct_subscription_events"."plan_id" = "dim_plan"."plan_id"
  GROUP BY
    DATE_TRUNC('DAY', "fct_subscription_events"."event_date"),
    "dim_plan"."plan_name"
), "_grain" AS (
  SELECT
    "_leaf_0"."event_date" AS "event_date",
    "_leaf_0"."plan_name" AS "plan_name"
  FROM "_leaf_0"
)
SELECT
  "_grain"."event_date" AS "event_date",
  "_grain"."plan_name" AS "plan_name",
  CASE
    WHEN COUNT("_leaf_0"."new_customer_count") OVER (
      PARTITION BY "_grain"."plan_name"
      ORDER BY CASE WHEN "_grain"."event_date" IS NULL THEN 1 ELSE 0 END ASC, "_grain"."event_date" ASC
      ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) = 0
    THEN NULL
    ELSE SUM(COALESCE("_leaf_0"."new_customer_count", 0)) OVER (
      PARTITION BY "_grain"."plan_name"
      ORDER BY CASE WHEN "_grain"."event_date" IS NULL THEN 1 ELSE 0 END ASC, "_grain"."event_date" ASC
      ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    )
  END AS "customers_to_date"
FROM "_grain"
LEFT JOIN "_leaf_0"
  ON (
    "_grain"."event_date" = "_leaf_0"."event_date"
    OR "_grain"."event_date" IS NULL
    AND "_leaf_0"."event_date" IS NULL
  )
  AND (
    "_grain"."plan_name" = "_leaf_0"."plan_name"
    OR "_grain"."plan_name" IS NULL
    AND "_leaf_0"."plan_name" IS NULL
  )
