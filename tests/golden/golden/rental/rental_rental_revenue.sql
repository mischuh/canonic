SELECT
  SUM(CASE WHEN "payments"."status" = 'settled' THEN "payments"."amount" ELSE 0 END) AS "rental_revenue"
FROM "payments" AS "payments"
