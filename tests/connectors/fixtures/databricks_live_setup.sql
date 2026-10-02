-- Fixture for tests/connectors/test_databricks_live.py. Run once in a Databricks SQL editor
-- as a user that can create catalogs and grant privileges, after adjusting the placeholder:
--   `canonic-sp` -> the service principal canonic will connect as
CREATE CATALOG IF NOT EXISTS canonic_test;
CREATE SCHEMA IF NOT EXISTS canonic_test.shop;

CREATE OR REPLACE TABLE canonic_test.shop.customers (
  id BIGINT NOT NULL, name STRING, tier STRING,
  CONSTRAINT pk_customers PRIMARY KEY (id));
CREATE OR REPLACE TABLE canonic_test.shop.orders (
  id BIGINT NOT NULL, customer_id BIGINT, amount DECIMAL(18, 2), created_at TIMESTAMP,
  CONSTRAINT pk_orders PRIMARY KEY (id),
  CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id) REFERENCES canonic_test.shop.customers (id));
CREATE OR REPLACE VIEW canonic_test.shop.v_orders AS
  SELECT id, customer_id, amount FROM canonic_test.shop.orders;

INSERT INTO canonic_test.shop.customers VALUES (1, 'a', 'pro'), (2, 'b', 'free');
INSERT INTO canonic_test.shop.orders VALUES
  (1, 1, 10.50, TIMESTAMP '2026-01-02 03:04:05'),
  (2, 2, 20.00, TIMESTAMP '2026-02-03 00:00:00'),
  (3, 1, 30.00, TIMESTAMP '2026-02-10 12:00:00');

-- Read-only access for the service principal
GRANT USE CATALOG ON CATALOG canonic_test TO `canonic-sp`;
GRANT USE SCHEMA ON SCHEMA canonic_test.shop TO `canonic-sp`;
GRANT SELECT ON SCHEMA canonic_test.shop TO `canonic-sp`;

-- Cleanup when done:
--   DROP CATALOG canonic_test CASCADE;
