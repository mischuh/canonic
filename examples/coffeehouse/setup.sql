-- Coffeehouse demo: a small online and in-store coffee shop, Q1 2026.
-- Small enough to check every number by hand. Build with: bash scripts/build.sh
--
-- The data carries the traps a real shop has:
--   - multi-line baskets (order 2, 10) next to single-line orders, so an average taken
--     over order lines instead of orders is badly skewed
--   - customers who buy several times a month (Anna), so summing daily distinct
--     customers overcounts the monthly figure
--   - refunded and cancelled orders, and one QA test customer, none of which is revenue

DROP TABLE IF EXISTS order_items;
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS products;
DROP TABLE IF EXISTS customers;

CREATE TABLE customers (
    customer_id INTEGER PRIMARY KEY,
    name        VARCHAR NOT NULL,
    country     VARCHAR NOT NULL,
    segment     VARCHAR NOT NULL,
    is_test     BOOLEAN NOT NULL
);

CREATE TABLE products (
    product_id INTEGER PRIMARY KEY,
    name       VARCHAR NOT NULL,
    category   VARCHAR NOT NULL,
    unit_price DECIMAL(10, 2) NOT NULL
);

CREATE TABLE orders (
    order_id    INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL,
    order_date  DATE NOT NULL,
    status      VARCHAR NOT NULL,   -- completed, refunded, cancelled
    channel     VARCHAR NOT NULL    -- online, store
);

CREATE TABLE order_items (
    order_id    INTEGER NOT NULL,
    line_number INTEGER NOT NULL,
    product_id  INTEGER NOT NULL,
    quantity    INTEGER NOT NULL,
    amount      DECIMAL(10, 2) NOT NULL,
    PRIMARY KEY (order_id, line_number)
);

INSERT INTO customers VALUES
    (1,  'Anna',    'DE', 'consumer', false),
    (2,  'Ben',     'DE', 'business', false),
    (3,  'Clara',   'AT', 'consumer', false),
    (4,  'Dario',   'CH', 'business', false),
    (5,  'Emma',    'DE', 'consumer', false),
    (99, 'QA Test', 'DE', 'consumer', true);

INSERT INTO products VALUES
    (1, 'House Espresso 250g',  'coffee',      10.00),
    (2, 'Ethiopia Filter 250g', 'coffee',      15.00),
    (3, 'Paper Filters',        'accessories',  5.00),
    (4, 'Hand Grinder',         'equipment',   60.00),
    (5, 'Milk Jug',             'accessories', 20.00);

INSERT INTO orders VALUES
    -- January
    (1,  1,  '2026-01-05', 'completed', 'online'),
    (2,  2,  '2026-01-05', 'completed', 'store'),
    (3,  1,  '2026-01-12', 'completed', 'online'),
    (4,  3,  '2026-01-20', 'refunded',  'online'),
    (5,  99, '2026-01-21', 'completed', 'online'),
    (6,  3,  '2026-01-26', 'completed', 'store'),
    (7,  1,  '2026-01-26', 'completed', 'online'),
    -- February
    (8,  4,  '2026-02-02', 'completed', 'online'),
    (9,  1,  '2026-02-03', 'completed', 'online'),
    (10, 2,  '2026-02-03', 'completed', 'store'),
    (11, 5,  '2026-02-10', 'completed', 'online'),
    (12, 1,  '2026-02-17', 'cancelled', 'online'),
    (13, 1,  '2026-02-24', 'completed', 'store'),
    -- March
    (14, 1,  '2026-03-02', 'completed', 'online'),
    (15, 3,  '2026-03-09', 'completed', 'online'),
    (16, 5,  '2026-03-09', 'completed', 'store'),
    (17, 1,  '2026-03-16', 'completed', 'online'),
    (18, 2,  '2026-03-23', 'refunded',  'store'),
    (19, 1,  '2026-03-30', 'completed', 'online');

-- amount = quantity * unit_price
INSERT INTO order_items VALUES
    (1,  1, 1,  2,  20.00),
    (2,  1, 4,  1,  60.00),
    (2,  2, 2,  2,  30.00),
    (2,  3, 1,  5,  50.00),
    (2,  4, 3, 12,  60.00),
    (3,  1, 2,  2,  30.00),
    (4,  1, 4,  1,  60.00),
    (4,  2, 2,  2,  30.00),
    (5,  1, 4, 10, 600.00),
    (6,  1, 1,  1,  10.00),
    (6,  2, 3,  3,  15.00),
    (7,  1, 1,  2,  20.00),
    (8,  1, 2,  1,  15.00),
    (8,  2, 5,  1,  20.00),
    (9,  1, 1,  2,  20.00),
    (10, 1, 4,  2, 120.00),
    (10, 2, 2,  4,  60.00),
    (10, 3, 1,  4,  40.00),
    (10, 4, 3, 10,  50.00),
    (10, 5, 5,  1,  20.00),
    (11, 1, 1,  1,  10.00),
    (12, 1, 4,  1,  60.00),
    (13, 1, 2,  1,  15.00),
    (14, 1, 1,  2,  20.00),
    (15, 1, 2,  2,  30.00),
    (15, 2, 3,  2,  10.00),
    (16, 1, 1,  1,  10.00),
    (17, 1, 1,  2,  20.00),
    (18, 1, 4,  1,  60.00),
    (19, 1, 1,  2,  20.00),
    (19, 2, 3,  1,   5.00);
