-- Shaped like Stripe's real-time sync to Postgres (Stripe Data Pipeline, public preview):
-- https://docs.stripe.com/data/data-pipeline/real-time-sync-to-postgres/schema
-- Same table names, column names and Postgres types as the documented schema (API version
-- 2026-03-25.dahlia): Unix seconds as bigint, amounts as bigint in the smallest currency
-- unit, nested objects as jsonb. Only a subset of each table's columns is created, the
-- rest is JSON the pack does not read.
CREATE SCHEMA stripe;

CREATE TABLE stripe.customers (
    id           text PRIMARY KEY,
    _account_id  text,
    created      bigint,
    currency     text,
    delinquent   boolean,
    email        text,
    livemode     boolean,
    metadata     jsonb,
    _updated_at  timestamptz
);

CREATE TABLE stripe.charges (
    id               text PRIMARY KEY,
    _account_id      text,
    amount           bigint,
    amount_captured  bigint,
    amount_refunded  bigint,
    captured         boolean,
    created          bigint,
    currency         text,
    customer         text,
    disputed         boolean,
    livemode         boolean,
    metadata         jsonb,
    paid             boolean,
    refunded         boolean,
    status           text,
    _updated_at      timestamptz
);

-- Note: the real refunds table has no livemode column.
CREATE TABLE stripe.refunds (
    id           text PRIMARY KEY,
    _account_id  text,
    amount       bigint,
    charge       text,
    created      bigint,
    currency     text,
    reason       text,
    status       text,
    metadata     jsonb,
    _updated_at  timestamptz
);

CREATE TABLE stripe.subscriptions (
    id                    text PRIMARY KEY,
    _account_id           text,
    cancel_at_period_end  boolean,
    canceled_at           bigint,
    created               bigint,
    currency              text,
    customer              text,
    ended_at              bigint,
    items                 jsonb,
    livemode              boolean,
    start_date            bigint,
    status                text,
    trial_end             bigint,
    _updated_at           timestamptz
);
