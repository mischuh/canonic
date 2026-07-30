WITH "_ranked" AS (
  SELECT
    "rentals"."total_amount" AS "_val",
    CUME_DIST() OVER (ORDER BY "rentals"."total_amount" NULLS LAST) AS "_cd"
  FROM "rentals" AS "rentals"
  WHERE
    NOT "rentals"."total_amount" IS NULL
)
SELECT
  MIN("_ranked"."_val") AS "median_rental_amount"
FROM "_ranked"
WHERE
  "_ranked"."_cd" >= 0.5
