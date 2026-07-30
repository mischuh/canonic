SELECT
  DATE_TRUNC('DAY', "fct_invoices"."invoice_date") AS "invoice_date",
  SUM("fct_invoices"."amount") AS "total_amount",
  TRUE AS "is_final"
FROM "fct_invoices" AS "fct_invoices"
WHERE
  "fct_invoices"."status" <> 'refunded'
  AND "fct_invoices"."is_trial" = FALSE
  AND "fct_invoices"."invoice_date" <= CAST('2025-03-13T23:59:59-04:00' AS TIMESTAMPTZ)
GROUP BY
  DATE_TRUNC('DAY', "fct_invoices"."invoice_date")
UNION ALL
SELECT
  DATE_TRUNC('DAY', "fct_invoices_rt"."invoice_date") AS "invoice_date",
  SUM("fct_invoices_rt"."amount") AS "total_amount",
  FALSE AS "is_final"
FROM "fct_invoices_rt" AS "fct_invoices_rt"
WHERE
  "fct_invoices_rt"."status" <> 'refunded'
  AND "fct_invoices_rt"."is_trial" = FALSE
  AND "fct_invoices_rt"."invoice_date" > CAST('2025-03-13T23:59:59-04:00' AS TIMESTAMPTZ)
GROUP BY
  DATE_TRUNC('DAY', "fct_invoices_rt"."invoice_date")
