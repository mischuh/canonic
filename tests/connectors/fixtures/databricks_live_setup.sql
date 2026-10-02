-- Fixture for tests/connectors/test_databricks_live.py. Run once in a Databricks SQL editor.
--
-- It creates the workspace.canonic_test schema these tests assert on. A new workspace has a
-- default `workspace` catalog, so the fixture creates a schema there instead of a catalog. To use
-- another catalog or schema, change the statements below and set
-- CANONIC_TEST_DATABRICKS_CATALOG and CANONIC_TEST_DATABRICKS_SCHEMA to match.
CREATE SCHEMA IF NOT EXISTS workspace.canonic_test;
USE CATALOG workspace;
USE SCHEMA canonic_test;

CREATE OR REPLACE TABLE customers (
  id BIGINT NOT NULL, name STRING, tier STRING,
  CONSTRAINT pk_customers PRIMARY KEY (id));
CREATE OR REPLACE TABLE orders (
  id BIGINT NOT NULL, customer_id BIGINT, amount DECIMAL(18, 2), created_at TIMESTAMP,
  CONSTRAINT pk_orders PRIMARY KEY (id),
  CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id) REFERENCES customers (id));
CREATE OR REPLACE VIEW v_orders AS SELECT id, customer_id, amount FROM orders;

INSERT INTO customers VALUES (1, 'a', 'pro'), (2, 'b', 'free');
INSERT INTO orders VALUES
  (1, 1, 10.50, TIMESTAMP '2026-01-02 03:04:05'),
  (2, 2, 20.00, TIMESTAMP '2026-02-03 00:00:00'),
  (3, 1, 30.00, TIMESTAMP '2026-02-10 12:00:00');

-- Optional, where service principals exist: read-only access for the principal canonic uses.
--   GRANT USE CATALOG ON CATALOG workspace TO `canonic-sp`;
--   GRANT USE SCHEMA ON SCHEMA workspace.canonic_test TO `canonic-sp`;
--   GRANT SELECT ON SCHEMA workspace.canonic_test TO `canonic-sp`;

-- Cleanup when done:
--   DROP SCHEMA workspace.canonic_test CASCADE;
