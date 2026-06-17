-- Canon ecommerce demo — idempotent setup, safe to re-run against an existing database:
--   psql "$CANON_PG_DSN" < setup.sql
--
-- Tables are dropped in reverse FK order and recreated from scratch each run,
-- so schema changes (e.g. new columns) are always applied cleanly.

CREATE SCHEMA IF NOT EXISTS analytics;

-- Drop in reverse FK order so constraints never block the drops.
DROP TABLE IF EXISTS analytics.fct_order_items CASCADE;
DROP TABLE IF EXISTS analytics.fct_orders      CASCADE;
DROP TABLE IF EXISTS analytics.dim_channels    CASCADE;
DROP TABLE IF EXISTS analytics.dim_products    CASCADE;
DROP TABLE IF EXISTS analytics.dim_customers   CASCADE;

-- ------------------------------------------------------------
-- dim_customers
-- ------------------------------------------------------------
CREATE TABLE analytics.dim_customers (
    customer_id  bigint PRIMARY KEY,
    email        text   NOT NULL,
    country      text   NOT NULL
);

INSERT INTO analytics.dim_customers (customer_id, email, country) VALUES
    (1, 'alice@example.com', 'DE'),
    (2, 'bob@example.com',   'US'),
    (3, 'carol@example.com', 'DE'),
    (4, 'dave@example.com',  'FR'),
    (5, 'eve@example.com',   'US');

-- ------------------------------------------------------------
-- dim_products
-- ------------------------------------------------------------
CREATE TABLE analytics.dim_products (
    product_id  bigint          PRIMARY KEY,
    name        text            NOT NULL,
    category    text            NOT NULL,
    unit_price  numeric(12, 2)  NOT NULL
);

INSERT INTO analytics.dim_products (product_id, name, category, unit_price) VALUES
    (1, 'Wireless Mouse',      'Accessories', 25.00),
    (2, 'Mechanical Keyboard', 'Accessories', 75.00),
    (3, '27" Monitor',         'Displays',    250.00),
    (4, 'USB-C Hub',           'Accessories', 60.00),
    (5, 'Laptop Stand',        'Furniture',   45.00);

-- ------------------------------------------------------------
-- dim_channels  (sales channel an order was placed through)
-- ------------------------------------------------------------
CREATE TABLE analytics.dim_channels (
    channel_id  bigint PRIMARY KEY,
    name        text   NOT NULL  -- 'web' | 'mobile' | 'in_store'
);

INSERT INTO analytics.dim_channels (channel_id, name) VALUES
    (1, 'web'),
    (2, 'mobile'),
    (3, 'in_store');

-- ------------------------------------------------------------
-- fct_orders  (one row per order; channel_id joins to dim_channels)
-- ------------------------------------------------------------
CREATE TABLE analytics.fct_orders (
    order_id     bigint          PRIMARY KEY,
    customer_id  bigint          NOT NULL REFERENCES analytics.dim_customers,
    channel_id   bigint          NOT NULL REFERENCES analytics.dim_channels,
    amount       numeric(12, 2)  NOT NULL,
    status       text            NOT NULL,  -- 'completed' | 'refunded' | 'pending'
    created_at   timestamp       NOT NULL
);

INSERT INTO analytics.fct_orders (order_id, customer_id, channel_id, amount, status, created_at) VALUES
-- completed orders (included in revenue)
    (1,  1, 1, 500.00,  'completed', '2025-01-10 09:15:00'),
    (3,  1, 2, 350.00,  'completed', '2025-01-12 14:30:00'),
    (4,  3, 1, 125.50,  'completed', '2025-01-13 11:00:00'),
    (5,  4, 3, 780.00,  'completed', '2025-01-14 16:45:00'),
    (7,  2, 2, 430.00,  'completed', '2025-01-16 08:20:00'),
    (9,  4, 1, 1200.00, 'completed', '2025-01-18 13:10:00'),
    (10, 5, 3, 310.00,  'completed', '2025-01-19 17:55:00'),
-- refunded orders (excluded by the revenue-excludes-refunds guardrail)
    (2,  2, 1, 200.00,  'refunded',  '2025-01-11 10:00:00'),
    (8,  3, 2,  60.00,  'refunded',  '2025-01-17 09:30:00'),
-- pending order (not yet confirmed, included until explicitly excluded)
    (6,  5, 1,  95.00,  'pending',   '2025-01-15 12:00:00');

-- Revenue after guardrail (status != 'refunded'):
--   500 + 350 + 125.50 + 780 + 430 + 1200 + 310 + 95 = 3790.50

-- ------------------------------------------------------------
-- fct_order_items  (one row per product line within an order)
-- Per order, sum(line_amount) == fct_orders.amount, so line_revenue
-- reconciles with revenue at the order grain.
-- ------------------------------------------------------------
CREATE TABLE analytics.fct_order_items (
    order_item_id  bigint          PRIMARY KEY,
    order_id       bigint          NOT NULL REFERENCES analytics.fct_orders,
    product_id     bigint          NOT NULL REFERENCES analytics.dim_products,
    quantity       int             NOT NULL,
    line_amount    numeric(12, 2)  NOT NULL
);

INSERT INTO analytics.fct_order_items (order_item_id, order_id, product_id, quantity, line_amount) VALUES
-- order 1 (500.00)
    (1,  1, 3, 1, 250.00),
    (2,  1, 2, 2, 250.00),
-- order 3 (350.00)
    (3,  3, 3, 1, 250.00),
    (4,  3, 5, 2, 100.00),
-- order 4 (125.50)
    (5,  4, 1, 2,  50.00),
    (6,  4, 4, 1,  75.50),
-- order 5 (780.00)
    (7,  5, 3, 3, 750.00),
    (8,  5, 1, 1,  30.00),
-- order 7 (430.00)
    (9,  7, 2, 4, 300.00),
    (10, 7, 4, 2, 130.00),
-- order 9 (1200.00)
    (11, 9, 3, 4, 1000.00),
    (12, 9, 2, 2, 200.00),
-- order 10 (310.00)
    (13, 10, 4, 3, 210.00),
    (14, 10, 1, 4, 100.00),
-- order 2 (200.00, refunded)
    (15, 2, 1, 2, 200.00),
-- order 8 (60.00, refunded)
    (16, 8, 5, 1,  60.00),
-- order 6 (95.00, pending)
    (17, 6, 4, 1,  95.00);

-- Totals for non-refunded orders (status != 'refunded'):
--   line_revenue = 3790.50  (matches revenue above)
--   units_sold   = 3+3+3+4+6+6+7+1 = 33 units
