SELECT
  "payments"."payment_date" AS "payment_date",
  SUM(CASE WHEN "payments"."status" = 'settled' THEN "payments"."amount" ELSE 0 END) AS "rental_revenue"
FROM "payments" AS "payments"
WHERE
  "payments"."payment_date" >= '2024-09-02'
  AND "payments"."payment_date" < '2024-12-01'
GROUP BY
  "payments"."payment_date"
