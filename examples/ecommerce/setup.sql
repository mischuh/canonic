-- Canon ecommerce demo — run once against your Postgres database:
--   psql "$CANON_PG_DSN" < setup.sql

CREATE SCHEMA IF NOT EXISTS analytics;

-- ------------------------------------------------------------
-- dim_customers
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS analytics.dim_customers (
    customer_id  bigint PRIMARY KEY,
    email        text   NOT NULL,
    country      text   NOT NULL
);

TRUNCATE analytics.dim_customers;

INSERT INTO analytics.dim_customers (customer_id, email, country) VALUES
    (1, 'alice@example.com', 'DE'),
    (2, 'bob@example.com',   'US'),
    (3, 'carol@example.com', 'DE'),
    (4, 'dave@example.com',  'FR'),
    (5, 'eve@example.com',   'US');

-- ------------------------------------------------------------
-- fct_orders
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS analytics.fct_orders (
    order_id     bigint          PRIMARY KEY,
    customer_id  bigint          NOT NULL REFERENCES analytics.dim_customers,
    amount       numeric(12, 2)  NOT NULL,
    status       text            NOT NULL,  -- 'completed' | 'refunded' | 'pending'
    created_at   timestamp       NOT NULL
);

TRUNCATE analytics.fct_orders;

INSERT INTO analytics.fct_orders (order_id, customer_id, amount, status, created_at) VALUES
-- completed orders (included in revenue)
    (1,  1, 500.00,  'completed', '2025-01-10 09:15:00'),
    (3,  1, 350.00,  'completed', '2025-01-12 14:30:00'),
    (4,  3, 125.50,  'completed', '2025-01-13 11:00:00'),
    (5,  4, 780.00,  'completed', '2025-01-14 16:45:00'),
    (7,  2, 430.00,  'completed', '2025-01-16 08:20:00'),
    (9,  4, 1200.00, 'completed', '2025-01-18 13:10:00'),
    (10, 5, 310.00,  'completed', '2025-01-19 17:55:00'),
-- refunded orders (excluded by the revenue-excludes-refunds guardrail)
    (2,  2, 200.00,  'refunded',  '2025-01-11 10:00:00'),
    (8,  3,  60.00,  'refunded',  '2025-01-17 09:30:00'),
-- pending order (not yet confirmed, included until explicitly excluded)
    (6,  5,  95.00,  'pending',   '2025-01-15 12:00:00');

-- Revenue after guardrail (status != 'refunded'):
--   500 + 350 + 125.50 + 780 + 430 + 1200 + 310 + 95 = 3790.50
