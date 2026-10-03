# Stripe context-pack demo

A Dockerized walkthrough of the `stripe` context pack. It brings up a real Postgres database
seeded with tables shaped like Stripe's real-time sync to Postgres, installs the pack against it
with `canonic pack add`, and serves the result over MCP. No Stripe account is needed.

## Run it

You need the `canonic-packs` repository checked out next to this one, on the branch that contains
`packs/stripe` (the pack is read from your working tree, so unmerged branches work).

```bash
cd scripts/stripe_pack_demo
docker compose up --build
```

If `canonic-packs` lives somewhere else, set `CANONIC_PACKS_DIR=/path/to/canonic-packs`.

On start the canonic container installs the pack, prints the first answer (net revenue of the last
30 days) and then serves MCP on `http://localhost:7474/mcp`. The bearer token is
`stripe-demo-local-dev`. Point an MCP client at it, or run queries inside the container:

```bash
docker compose exec -w /data/stripe-demo canonic \
  uv run --project /app --no-sync canonic query --metrics net_revenue
```

To change the pack and try again, edit it in `canonic-packs` and run `docker compose restart canonic`.
Installing writes every file again, so this is safe, but it does not delete files a newer pack version
no longer ships. Use `docker compose down` to stop and discard
the database and the installed project.

## What the seed contains

The seed (`postgres/init/`) uses the exact table names, column names and Postgres types of the
[documented schema](https://docs.stripe.com/data/data-pipeline/real-time-sync-to-postgres/schema):
Unix seconds as `bigint`, amounts in cents, `items` as `jsonb`. Timestamps are relative to container
start, so the 30 day window always has data.

| table | live | excluded on purpose |
| --- | --- | --- |
| `charges` | 6 succeeded USD charges | a failed charge, a test mode charge, a EUR charge |
| `refunds` | 3 succeeded USD refunds | a failed refund, a refund of the test mode charge, a EUR refund |
| `customers` | 4 | 1 test mode customer |
| `subscriptions` | 6 (3 active, 1 past due, 1 trialing, 1 canceled) | 1 test mode subscription |

`ch_6` was charged 40 days ago and refunded 2 days ago. It shows the difference between refunds by
charge date (`refunded_amount`) and by refund date (`refunds_issued`).

## Expected values

All amounts in USD. Compare these with what `canonic query` returns.

| metric | value | how |
| --- | --- | --- |
| `gross_revenue` | 540.00 | 100 + 50 + 200 + 30 + 40 + 120 |
| `refunded_amount` | 115.00 | 50 + 25 + 40 |
| `net_revenue` | 425.00 | 540 - 115 |
| `net_revenue`, first answer (last 30 days) | 395.00 | the 30 and 40 day old charges drop out, 470 - 75 |
| `refund_rate` | 0.2130 | 115 / 540 |
| `successful_payments` | 6 | |
| `refunds_issued` | 115.00 | 25 on day 15, 50 on day 8, 40 on day 2 before today |
| `new_customers` | 4 | |
| `new_subscriptions` | 6 | |
| `active_subscriptions` | 4 | 3 active and 1 past due |
| `trialing_subscriptions` | 1 | |
| `canceled_subscriptions` | 1 | |

Every query also returns the guardrail `warnings`. The filters are applied either way. To see
the SQL, call the `compile_query` MCP tool. For `refunds_issued` it contains
`stripe_charges.livemode = TRUE` through a join, because the real `refunds` table has no
`livemode` column.

## MRR

MRR is not part of the pack yet, see the pack README. The seed includes `items` jsonb on every
subscription so you can try the flattened-view workaround described there. Expected MRR on this
data is 105.00 (20 + 20 + 45 + 20, trial and test mode excluded).
