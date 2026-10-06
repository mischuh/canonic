-- Ossie retail demo: a small coffee retailer, small enough to check every number by hand.
-- Build with: sqlite3 retail.db < setup.sql

DROP TABLE IF EXISTS order_items;
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS customer_profiles;
DROP TABLE IF EXISTS customers;
DROP TABLE IF EXISTS products;
DROP TABLE IF EXISTS stores;

CREATE TABLE stores (
    store_id INTEGER PRIMARY KEY,
    name     TEXT NOT NULL,
    region   TEXT NOT NULL
);

CREATE TABLE customers (
    customer_id INTEGER PRIMARY KEY,
    email       TEXT NOT NULL UNIQUE,
    country     TEXT NOT NULL,
    segment     TEXT NOT NULL
);

CREATE TABLE customer_profiles (
    customer_id  INTEGER PRIMARY KEY,
    loyalty_tier TEXT NOT NULL
);

CREATE TABLE products (
    product_id INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    category   TEXT NOT NULL,
    unit_price NUMERIC NOT NULL
);

CREATE TABLE orders (
    order_id    INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL,
    store_id    INTEGER NOT NULL,
    order_date  DATE NOT NULL,
    status      TEXT NOT NULL,
    channel     TEXT NOT NULL
);

CREATE TABLE order_items (
    order_id    INTEGER NOT NULL,
    line_number INTEGER NOT NULL,
    product_id  INTEGER NOT NULL,
    quantity    INTEGER NOT NULL,
    amount      NUMERIC NOT NULL,
    PRIMARY KEY (order_id, line_number)
);

INSERT INTO stores VALUES
    (1, 'Berlin Mitte', 'north'),
    (2, 'Hamburg', 'north'),
    (3, 'Munich', 'south');

INSERT INTO customers VALUES
    (1, 'anna@example.com', 'DE', 'consumer'),
    (2, 'ben@example.com', 'DE', 'business'),
    (3, 'clara@example.com', 'AT', 'consumer'),
    (4, 'dario@example.com', 'CH', 'business');

INSERT INTO customer_profiles VALUES
    (1, 'gold'),
    (2, 'silver'),
    (3, 'bronze');

INSERT INTO products VALUES
    (1, 'Espresso Beans', 'coffee', 12.50),
    (2, 'Filter Paper', 'accessories', 4.00),
    (3, 'Grinder', 'equipment', 80.00);

INSERT INTO orders VALUES
    (1, 1, 1, '2026-01-05', 'shipped', 'online'),
    (2, 2, 1, '2026-01-12', 'shipped', 'store'),
    (3, 1, 2, '2026-01-20', 'cancelled', 'online'),
    (4, 3, 3, '2026-02-02', 'shipped', 'store'),
    (5, 4, 3, '2026-02-14', 'placed', 'online'),
    (6, 2, 2, '2026-02-21', 'shipped', 'online');

INSERT INTO order_items VALUES
    (1, 1, 1, 2, 25.00),
    (1, 2, 2, 1, 4.00),
    (2, 1, 3, 1, 80.00),
    (3, 1, 1, 1, 12.50),
    (4, 1, 1, 4, 50.00),
    (4, 2, 2, 2, 8.00),
    (5, 1, 3, 1, 80.00),
    (5, 2, 1, 1, 12.50),
    (6, 1, 2, 3, 12.00);
