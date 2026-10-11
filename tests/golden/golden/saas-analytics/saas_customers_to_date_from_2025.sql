WITH "_leaf_0__visible" AS (
  SELECT DISTINCT
    DATE_TRUNC('DAY', "fct_subscription_events"."event_date") AS "event_date"
  FROM "fct_subscription_events" AS "fct_subscription_events"
  WHERE
    "fct_subscription_events"."event_date" >= '2025-01-01'
), "_leaf_0" AS (
  SELECT
    DATE_TRUNC('DAY', "fct_subscription_events"."event_date") AS "event_date",
    COUNT(
      CASE
        WHEN "fct_subscription_events"."event_type" = 'new'
        THEN "fct_subscription_events"."event_id"
      END
    ) AS "customers_to_date"
  FROM "fct_subscription_events" AS "fct_subscription_events"
  GROUP BY
    DATE_TRUNC('DAY', "fct_subscription_events"."event_date")
), "_grain" AS (
  SELECT
    "_leaf_0"."event_date" AS "event_date"
  FROM "_leaf_0"
), "_accumulated" AS (
  SELECT
    "_grain"."event_date" AS "event_date",
    CASE
      WHEN COUNT("_leaf_0"."customers_to_date") OVER (
        ORDER BY CASE WHEN "_grain"."event_date" IS NULL THEN 1 ELSE 0 END ASC, "_grain"."event_date" ASC
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
      ) = 0
      THEN NULL
      ELSE SUM(COALESCE("_leaf_0"."customers_to_date", 0)) OVER (
        ORDER BY CASE WHEN "_grain"."event_date" IS NULL THEN 1 ELSE 0 END ASC, "_grain"."event_date" ASC
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
      )
    END AS "_m0"
  FROM "_grain"
  LEFT JOIN "_leaf_0"
    ON (
      "_grain"."event_date" = "_leaf_0"."event_date"
      OR "_grain"."event_date" IS NULL
      AND "_leaf_0"."event_date" IS NULL
    )
), "_visible_grain" AS (
  SELECT
    "_leaf_0"."event_date" AS "event_date"
  FROM "_leaf_0"
  INNER JOIN "_leaf_0__visible"
    ON (
      "_leaf_0"."event_date" = "_leaf_0__visible"."event_date"
      OR "_leaf_0"."event_date" IS NULL
      AND "_leaf_0__visible"."event_date" IS NULL
    )
)
SELECT
  "_accumulated"."event_date" AS "event_date",
  "_accumulated"."_m0" AS "customers_to_date"
FROM "_accumulated"
INNER JOIN "_visible_grain"
  ON (
    "_accumulated"."event_date" = "_visible_grain"."event_date"
    OR "_accumulated"."event_date" IS NULL
    AND "_visible_grain"."event_date" IS NULL
  )
