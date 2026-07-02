-- =====================================================================
-- Canonic SaaS-Analytics demo warehouse (DuckDB)
--
-- A small but realistic SaaS subscription warehouse modelled Kimball-style:
--   Business Vault : 8 dimensions + 10 fact tables (incl. a monthly snapshot fact)
--   Data Marts     : 4 pre-aggregated tables for condensed reporting
--
-- The seed is fully deterministic so the contract assertions resolve to
-- exact, hand-checkable values:
--   gross_revenue 2025-Q1 (paid, non-trial, non-refunded) = 17814.00
--   active_subscribers 2025-03                              = 12
--
-- Usage:  duckdb saas.duckdb < setup.sql        (idempotent, safe to re-run)
-- =====================================================================

-- Drop in reverse-dependency order (marts → facts → dims).
DROP TABLE IF EXISTS mart_rep_quota;
DROP TABLE IF EXISTS mart_account_health;
DROP TABLE IF EXISTS mart_cohort_retention;
DROP TABLE IF EXISTS mart_monthly_mrr;
DROP TABLE IF EXISTS fct_payments;
DROP TABLE IF EXISTS fct_nps_responses;
DROP TABLE IF EXISTS fct_opportunities;
DROP TABLE IF EXISTS fct_support_tickets;
DROP TABLE IF EXISTS fct_feature_usage;
DROP TABLE IF EXISTS fct_usage;
DROP TABLE IF EXISTS fct_invoices_rt;
DROP TABLE IF EXISTS fct_invoices;
DROP TABLE IF EXISTS fct_mrr_snapshot;
DROP TABLE IF EXISTS fct_subscription_events;
DROP TABLE IF EXISTS dim_feature;
DROP TABLE IF EXISTS dim_campaign;
DROP TABLE IF EXISTS dim_sales_rep;
DROP TABLE IF EXISTS dim_industry;
DROP TABLE IF EXISTS dim_date;
DROP TABLE IF EXISTS dim_geo;
DROP TABLE IF EXISTS dim_plan;
DROP TABLE IF EXISTS dim_customer;

-- =====================================================================
-- DIMENSIONS (8)
-- =====================================================================

CREATE TABLE dim_geo (
    geo_id     INTEGER PRIMARY KEY,
    country    VARCHAR NOT NULL,
    region     VARCHAR NOT NULL,
    continent  VARCHAR NOT NULL
);
INSERT INTO dim_geo VALUES
    (1, 'US', 'North America',   'Americas'),
    (2, 'DE', 'DACH',            'EMEA'),
    (3, 'UK', 'Northern Europe', 'EMEA'),
    (4, 'FR', 'Western Europe',  'EMEA'),
    (5, 'SG', 'Southeast Asia',  'APAC');

CREATE TABLE dim_industry (
    industry_id    INTEGER PRIMARY KEY,
    industry_name  VARCHAR NOT NULL,
    sector         VARCHAR NOT NULL
);
INSERT INTO dim_industry VALUES
    (1, 'SaaS',          'Technology'),
    (2, 'Retail',        'Commerce'),
    (3, 'Healthcare',    'Life Sciences'),
    (4, 'Finance',       'Financial Services'),
    (5, 'Manufacturing', 'Industrial');

CREATE TABLE dim_sales_rep (
    rep_id     INTEGER PRIMARY KEY,
    rep_name   VARCHAR NOT NULL,
    team       VARCHAR NOT NULL,
    region     VARCHAR NOT NULL,
    hire_date  DATE    NOT NULL
);
INSERT INTO dim_sales_rep VALUES
    (1, 'Alice Anderson', 'Enterprise',  'Americas', DATE '2022-01-15'),
    (2, 'Bob Brown',      'Enterprise',  'EMEA',     DATE '2022-03-01'),
    (3, 'Carol Clark',    'Mid-Market',  'Americas', DATE '2023-02-10'),
    (4, 'Dan Davis',      'Mid-Market',  'EMEA',     DATE '2023-05-20'),
    (5, 'Eve Evans',      'SMB',         'APAC',     DATE '2024-01-05'),
    (6, 'Frank Foster',   'SMB',         'Americas', DATE '2024-06-01');

CREATE TABLE dim_campaign (
    campaign_id    INTEGER PRIMARY KEY,
    campaign_name  VARCHAR NOT NULL,
    channel        VARCHAR NOT NULL,   -- paid_search | content | events | referral | outbound
    start_date     DATE    NOT NULL,
    budget         DECIMAL(12,2) NOT NULL
);
INSERT INTO dim_campaign VALUES
    (1, 'Q4 Paid Search',   'paid_search', DATE '2024-10-01', 50000.00),
    (2, 'Content Hub',      'content',     DATE '2024-01-01', 30000.00),
    (3, 'SaaStr Events',    'events',      DATE '2024-09-01', 80000.00),
    (4, 'Referral Program', 'referral',    DATE '2024-01-01', 20000.00),
    (5, 'Outbound Blitz',   'outbound',    DATE '2025-01-01', 40000.00);

CREATE TABLE dim_plan (
    plan_id            INTEGER PRIMARY KEY,
    plan_name          VARCHAR NOT NULL,   -- free | starter | pro | enterprise
    tier               INTEGER NOT NULL,   -- 0..3 ordering
    list_price_monthly DECIMAL(10,2) NOT NULL,
    billing_interval   VARCHAR NOT NULL
);
INSERT INTO dim_plan VALUES
    (1, 'free',       0,   0.00, 'monthly'),
    (2, 'starter',    1,  49.00, 'monthly'),
    (3, 'pro',        2, 199.00, 'monthly'),
    (4, 'enterprise', 3, 999.00, 'monthly');

CREATE TABLE dim_feature (
    feature_id     INTEGER PRIMARY KEY,
    feature_name   VARCHAR NOT NULL,
    module         VARCHAR NOT NULL,
    tier_required  VARCHAR NOT NULL    -- minimum plan required (matches dim_plan.plan_name)
);
INSERT INTO dim_feature VALUES
    (1, 'Dashboards',     'analytics',   'starter'),
    (2, 'API Access',     'integration', 'pro'),
    (3, 'SSO',            'security',    'enterprise'),
    (4, 'Custom Reports', 'analytics',   'pro'),
    (5, 'Alerts',         'monitoring',  'starter'),
    (6, 'Data Export',    'integration', 'starter'),
    (7, 'Audit Log',      'security',    'enterprise'),
    (8, 'Webhooks',       'integration', 'pro');

-- Conformed date dimension (daily, 2024-2025).
CREATE TABLE dim_date (
    date_id      INTEGER PRIMARY KEY,
    date         DATE    NOT NULL,
    year         INTEGER NOT NULL,
    quarter      INTEGER NOT NULL,
    month        INTEGER NOT NULL,
    month_name   VARCHAR NOT NULL,
    day          INTEGER NOT NULL,
    is_month_end BOOLEAN NOT NULL
);
INSERT INTO dim_date
SELECT
    CAST(strftime(d, '%Y%m%d') AS INTEGER)            AS date_id,
    d                                                 AS date,
    year(d)                                           AS year,
    quarter(d)                                        AS quarter,
    month(d)                                          AS month,
    strftime(d, '%B')                                 AS month_name,
    day(d)                                            AS day,
    (d = last_day(d))                                 AS is_month_end
FROM range(DATE '2024-01-01', DATE '2026-01-01', INTERVAL 1 DAY) AS t(d);

CREATE TABLE dim_customer (
    customer_id   INTEGER PRIMARY KEY,
    company_name  VARCHAR NOT NULL,
    geo_id        INTEGER NOT NULL,
    industry_id   INTEGER NOT NULL,
    campaign_id   INTEGER NOT NULL,
    rep_id        INTEGER NOT NULL,
    segment       VARCHAR NOT NULL,   -- smb | mid_market | enterprise
    signup_date   DATE    NOT NULL
);
INSERT INTO dim_customer VALUES
    ( 1, 'Acme Corp',         1, 1, 1, 1, 'enterprise', DATE '2024-01-10'),
    ( 2, 'Globex',            2, 5, 3, 2, 'enterprise', DATE '2024-02-15'),
    ( 3, 'Initech',           1, 4, 2, 3, 'mid_market', DATE '2024-02-20'),
    ( 4, 'Umbrella',          3, 3, 4, 4, 'mid_market', DATE '2024-03-05'),
    ( 5, 'Stark Industries',  1, 5, 1, 1, 'enterprise', DATE '2024-03-12'),
    ( 6, 'Wayne Enterprises', 1, 4, 3, 3, 'enterprise', DATE '2024-04-01'),
    ( 7, 'Hooli',             5, 1, 5, 5, 'smb',        DATE '2024-05-18'),
    ( 8, 'Pied Piper',        1, 1, 2, 6, 'smb',        DATE '2024-06-22'),
    ( 9, 'Soylent',           4, 2, 4, 4, 'mid_market', DATE '2024-07-09'),
    (10, 'Cyberdyne',         1, 1, 1, 1, 'enterprise', DATE '2024-08-14'),
    (11, 'Massive Dynamic',   3, 3, 3, 2, 'mid_market', DATE '2024-09-30'),
    (12, 'Vandelay',          1, 2, 5, 6, 'smb',        DATE '2024-11-11');

-- =====================================================================
-- SNAPSHOT FACT — monthly MRR position (semi-additive over snapshot_month)
-- Full customer x month grid for 2025; is_active=false (mrr 0) after churn.
-- Customer 3 (Initech) expands pro -> enterprise from 2025-06.
-- Churn: Hooli after 2025-04, Vandelay after 2025-07, Pied Piper after 2025-09.
-- =====================================================================
CREATE TABLE fct_mrr_snapshot (
    snapshot_id    INTEGER PRIMARY KEY,
    snapshot_month DATE    NOT NULL,
    customer_id    INTEGER NOT NULL,
    plan_id        INTEGER NOT NULL,
    mrr            DECIMAL(10,2) NOT NULL,
    is_active      BOOLEAN NOT NULL
);
INSERT INTO fct_mrr_snapshot
WITH months AS (
    SELECT m AS snapshot_month
    FROM range(DATE '2025-01-01', DATE '2026-01-01', INTERVAL 1 MONTH) AS t(m)
),
cust AS (
    SELECT * FROM (VALUES
        ( 1, 4, 999.00, NULL),
        ( 2, 4, 999.00, NULL),
        ( 3, 3, 199.00, NULL),   -- expands to enterprise in June (handled below)
        ( 4, 3, 199.00, NULL),
        ( 5, 4, 999.00, NULL),
        ( 6, 4, 999.00, NULL),
        ( 7, 2,  49.00, DATE '2025-04-01'),
        ( 8, 2,  49.00, DATE '2025-09-01'),
        ( 9, 3, 199.00, NULL),
        (10, 4, 999.00, NULL),
        (11, 3, 199.00, NULL),
        (12, 2,  49.00, DATE '2025-07-01')
    ) AS t(customer_id, plan_id, base_mrr, churn_after)
)
SELECT
    CAST(strftime(m.snapshot_month, '%Y%m') AS INTEGER) * 100 + c.customer_id AS snapshot_id,
    m.snapshot_month,
    c.customer_id,
    CASE WHEN c.customer_id = 3 AND m.snapshot_month >= DATE '2025-06-01' THEN 4 ELSE c.plan_id END AS plan_id,
    CASE
        WHEN c.churn_after IS NOT NULL AND m.snapshot_month > c.churn_after THEN 0.00
        WHEN c.customer_id = 3 AND m.snapshot_month >= DATE '2025-06-01' THEN 999.00
        ELSE c.base_mrr
    END AS mrr,
    (c.churn_after IS NULL OR m.snapshot_month <= c.churn_after) AS is_active
FROM months m CROSS JOIN cust c;

-- =====================================================================
-- TRANSACTION FACT — subscription lifecycle events
-- =====================================================================
CREATE TABLE fct_subscription_events (
    event_id      INTEGER PRIMARY KEY,
    customer_id   INTEGER NOT NULL,
    plan_id       INTEGER NOT NULL,
    event_date    DATE    NOT NULL,
    event_type    VARCHAR NOT NULL,   -- new | expansion | contraction | churn | reactivation
    mrr_delta     DECIMAL(10,2) NOT NULL,
    contract_value DECIMAL(12,2) NOT NULL,
    discount_pct  DECIMAL(5,4) NOT NULL
);
INSERT INTO fct_subscription_events VALUES
    -- new (acquisition in 2024) — contract_value = annualised MRR
    ( 1,  1, 4, DATE '2024-01-10', 'new',          999.00, 11988.00, 0.1500),
    ( 2,  2, 4, DATE '2024-02-15', 'new',          999.00, 11988.00, 0.1200),
    ( 3,  3, 3, DATE '2024-02-20', 'new',          199.00,  2388.00, 0.1000),
    ( 4,  4, 3, DATE '2024-03-05', 'new',          199.00,  2388.00, 0.0800),
    ( 5,  5, 4, DATE '2024-03-12', 'new',          999.00, 11988.00, 0.2000),
    ( 6,  6, 4, DATE '2024-04-01', 'new',          999.00, 11988.00, 0.1000),
    ( 7,  7, 2, DATE '2024-05-18', 'new',           49.00,   588.00, 0.0000),
    ( 8,  8, 2, DATE '2024-06-22', 'new',           49.00,   588.00, 0.0500),
    ( 9,  9, 3, DATE '2024-07-09', 'new',          199.00,  2388.00, 0.0800),
    (10, 10, 4, DATE '2024-08-14', 'new',          999.00, 11988.00, 0.1800),
    (11, 11, 3, DATE '2024-09-30', 'new',          199.00,  2388.00, 0.0500),
    (12, 12, 2, DATE '2024-11-11', 'new',           49.00,   588.00, 0.0000),
    -- expansion
    (13,  3, 4, DATE '2025-06-01', 'expansion',    800.00,  9600.00, 0.0500),
    (14, 10, 4, DATE '2025-03-15', 'expansion',    200.00,  2400.00, 0.0000),
    -- contraction
    (15,  9, 3, DATE '2025-08-01', 'contraction',  -50.00,     0.00, 0.0000),
    -- churn
    (16,  7, 2, DATE '2025-05-01', 'churn',        -49.00,     0.00, 0.0000),
    (17, 12, 2, DATE '2025-08-01', 'churn',        -49.00,     0.00, 0.0000),
    (18,  8, 2, DATE '2025-10-01', 'churn',        -49.00,     0.00, 0.0000);

-- =====================================================================
-- BILLING FACTS — final invoices + provisional (real-time) variant
-- Regular monthly invoices mirror the active snapshot rows (paid, non-trial).
-- A handful of refund/trial/pending rows exercise the revenue guardrails.
-- =====================================================================
CREATE TABLE fct_invoices (
    invoice_id   INTEGER PRIMARY KEY,
    customer_id  INTEGER NOT NULL,
    invoice_date DATE    NOT NULL,
    amount       DECIMAL(10,2) NOT NULL,
    status       VARCHAR NOT NULL,   -- paid | refunded | pending
    is_trial     BOOLEAN NOT NULL
);
INSERT INTO fct_invoices
SELECT
    CAST(strftime(snapshot_month, '%Y%m') AS INTEGER) * 100 + customer_id AS invoice_id,
    customer_id,
    snapshot_month AS invoice_date,
    mrr AS amount,
    'paid' AS status,
    false  AS is_trial
FROM fct_mrr_snapshot
WHERE is_active;
-- Special rows (high ids) — all excluded from clean revenue by guardrails.
INSERT INTO fct_invoices VALUES
    (90001,  7, DATE '2025-02-15',  49.00, 'refunded', false),  -- refund: excluded
    (90002,  8, DATE '2025-01-20',  49.00, 'paid',     true),   -- trial:  excluded
    (90003,  1, DATE '2025-05-01', 999.00, 'pending',  false);  -- pending, outside Q1

-- Provisional intraday estimates for the latest open period.
CREATE TABLE fct_invoices_rt (
    invoice_id   INTEGER PRIMARY KEY,
    customer_id  INTEGER NOT NULL,
    invoice_date DATE    NOT NULL,
    amount       DECIMAL(10,2) NOT NULL,
    status       VARCHAR NOT NULL,
    is_trial     BOOLEAN NOT NULL
);
INSERT INTO fct_invoices_rt VALUES
    (1,  1, DATE '2025-12-31', 999.00, 'paid', false),
    (2,  2, DATE '2025-12-31', 999.00, 'paid', false),
    (3,  5, DATE '2025-12-31', 999.00, 'paid', false);

-- =====================================================================
-- USAGE FACTS — account rollup + per-feature usage
-- =====================================================================
CREATE TABLE fct_usage (
    usage_id    INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL,
    usage_date  DATE    NOT NULL,
    api_calls   INTEGER NOT NULL,
    seats_used  INTEGER NOT NULL
);
INSERT INTO fct_usage
SELECT
    CAST(strftime(s.snapshot_month, '%Y%m') AS INTEGER) * 100 + s.customer_id AS usage_id,
    s.customer_id,
    s.snapshot_month AS usage_date,
    (CASE c.segment WHEN 'enterprise' THEN 50 WHEN 'mid_market' THEN 15 ELSE 3 END) * 1000
        + month(s.snapshot_month) * 17 + s.customer_id AS api_calls,
    CASE c.segment WHEN 'enterprise' THEN 50 WHEN 'mid_market' THEN 15 ELSE 3 END AS seats_used
FROM fct_mrr_snapshot s
JOIN dim_customer c ON c.customer_id = s.customer_id
WHERE s.is_active;

CREATE TABLE fct_feature_usage (
    feature_usage_id INTEGER PRIMARY KEY,
    customer_id      INTEGER NOT NULL,
    feature_id       INTEGER NOT NULL,
    usage_date       DATE    NOT NULL,
    event_count      INTEGER NOT NULL
);
INSERT INTO fct_feature_usage
SELECT
    row_number() OVER (ORDER BY s.snapshot_month, s.customer_id, f.feature_id) AS feature_usage_id,
    s.customer_id,
    f.feature_id,
    s.snapshot_month AS usage_date,
    (s.customer_id * 7 + f.feature_id * 3 + month(s.snapshot_month)) AS event_count
FROM fct_mrr_snapshot s
JOIN dim_plan p ON p.plan_id = s.plan_id
JOIN dim_feature f
  ON (CASE f.tier_required WHEN 'free' THEN 0 WHEN 'starter' THEN 1 WHEN 'pro' THEN 2 WHEN 'enterprise' THEN 3 END)
   <= p.tier
WHERE s.is_active;

-- =====================================================================
-- SUPPORT FACT — one row per ticket (CSAT, resolution time)
-- =====================================================================
CREATE TABLE fct_support_tickets (
    ticket_id        INTEGER PRIMARY KEY,
    customer_id      INTEGER NOT NULL,
    rep_id           INTEGER NOT NULL,
    opened_date      DATE    NOT NULL,
    closed_date      DATE,
    priority         VARCHAR NOT NULL,   -- low | medium | high | urgent
    status           VARCHAR NOT NULL,   -- open | closed
    resolution_hours DECIMAL(8,2),
    csat_score       INTEGER             -- 1..5, null while open
);
INSERT INTO fct_support_tickets VALUES
    ( 1,  1, 1, DATE '2025-01-05', DATE '2025-01-05', 'low',    'closed',   2.50, 5),
    ( 2,  1, 1, DATE '2025-02-11', DATE '2025-02-13', 'high',   'closed',  44.00, 4),
    ( 3,  2, 2, DATE '2025-01-20', DATE '2025-01-21', 'medium', 'closed',  18.00, 5),
    ( 4,  3, 3, DATE '2025-02-02', DATE '2025-02-02', 'low',    'closed',   1.00, 5),
    ( 5,  3, 3, DATE '2025-03-15', DATE '2025-03-18', 'urgent', 'closed',  72.00, 2),
    ( 6,  4, 4, DATE '2025-01-30', DATE '2025-02-01', 'medium', 'closed',  40.00, 3),
    ( 7,  5, 1, DATE '2025-02-14', DATE '2025-02-14', 'low',    'closed',   3.50, 5),
    ( 8,  5, 1, DATE '2025-04-09', DATE '2025-04-12', 'high',   'closed',  70.00, 3),
    ( 9,  6, 3, DATE '2025-03-01', DATE '2025-03-02', 'medium', 'closed',  20.00, 4),
    (10,  7, 5, DATE '2025-02-22', DATE '2025-02-25', 'urgent', 'closed',  80.00, 1),
    (11,  8, 6, DATE '2025-03-10', DATE '2025-03-10', 'low',    'closed',   4.00, 4),
    (12,  9, 4, DATE '2025-01-18', DATE '2025-01-19', 'medium', 'closed',  22.00, 4),
    (13, 10, 1, DATE '2025-02-27', DATE '2025-02-28', 'high',   'closed',  30.00, 5),
    (14, 10, 1, DATE '2025-05-05', DATE '2025-05-08', 'urgent', 'closed',  68.00, 3),
    (15, 11, 2, DATE '2025-03-21', DATE '2025-03-22', 'medium', 'closed',  16.00, 5),
    (16, 12, 6, DATE '2025-02-08', DATE '2025-02-09', 'low',    'closed',   6.00, 4),
    (17,  1, 1, DATE '2025-06-12', DATE '2025-06-14', 'high',   'closed',  48.00, 4),
    (18,  2, 2, DATE '2025-05-19', DATE '2025-05-20', 'medium', 'closed',  19.00, 5),
    (19,  4, 4, DATE '2025-06-25', DATE '2025-06-30', 'urgent', 'closed', 110.00, 2),
    (20,  6, 3, DATE '2025-07-02', DATE '2025-07-02', 'low',    'closed',   2.00, 5),
    (21,  9, 4, DATE '2025-07-15', DATE '2025-07-17', 'high',   'closed',  50.00, 3),
    (22,  3, 3, DATE '2025-08-01', NULL,              'medium', 'open',     NULL, NULL),
    (23, 10, 1, DATE '2025-08-20', NULL,              'high',   'open',     NULL, NULL),
    (24, 11, 2, DATE '2025-09-05', DATE '2025-09-06', 'medium', 'closed',  21.00, 4);

-- =====================================================================
-- SALES PIPELINE FACT — opportunities (won/lost deals)
-- =====================================================================
CREATE TABLE fct_opportunities (
    opp_id       INTEGER PRIMARY KEY,
    customer_id  INTEGER NOT NULL,
    rep_id       INTEGER NOT NULL,
    campaign_id  INTEGER NOT NULL,
    created_date DATE    NOT NULL,
    closed_date  DATE,
    stage        VARCHAR NOT NULL,   -- prospecting | negotiation | closed_won | closed_lost
    amount       DECIMAL(12,2) NOT NULL,
    is_won       BOOLEAN NOT NULL
);
INSERT INTO fct_opportunities VALUES
    ( 1,  1, 1, 1, DATE '2024-01-02', DATE '2024-01-10', 'closed_won',  11988.00, true),
    ( 2,  2, 2, 3, DATE '2024-02-01', DATE '2024-02-15', 'closed_won',  11988.00, true),
    ( 3,  3, 3, 2, DATE '2024-02-05', DATE '2024-02-20', 'closed_won',   2388.00, true),
    ( 4,  4, 4, 4, DATE '2024-02-20', DATE '2024-03-05', 'closed_won',   2388.00, true),
    ( 5,  5, 1, 1, DATE '2024-02-28', DATE '2024-03-12', 'closed_won',  11988.00, true),
    ( 6,  6, 3, 3, DATE '2024-03-15', DATE '2024-04-01', 'closed_won',  11988.00, true),
    ( 7,  7, 5, 5, DATE '2024-05-01', DATE '2024-05-18', 'closed_won',    588.00, true),
    ( 8,  8, 6, 2, DATE '2024-06-10', DATE '2024-06-22', 'closed_won',    588.00, true),
    ( 9,  9, 4, 4, DATE '2024-06-25', DATE '2024-07-09', 'closed_won',   2388.00, true),
    (10, 10, 1, 1, DATE '2024-07-30', DATE '2024-08-14', 'closed_won',  11988.00, true),
    (11, 11, 2, 3, DATE '2024-09-10', DATE '2024-09-30', 'closed_won',   2388.00, true),
    (12, 12, 6, 5, DATE '2024-10-25', DATE '2024-11-11', 'closed_won',    588.00, true),
    -- lost deals (no resulting customer)
    (13,  1, 1, 1, DATE '2024-04-01', DATE '2024-05-01', 'closed_lost', 24000.00, false),
    (14,  2, 2, 3, DATE '2024-05-01', DATE '2024-06-15', 'closed_lost',  6000.00, false),
    (15,  5, 1, 1, DATE '2025-01-10', DATE '2025-02-20', 'closed_lost', 18000.00, false),
    (16,  6, 3, 3, DATE '2025-02-01', DATE '2025-03-10', 'closed_lost',  9000.00, false),
    (17, 10, 1, 1, DATE '2025-02-15', DATE '2025-03-15', 'closed_won',   2400.00, true),  -- expansion deal
    (18,  3, 3, 2, DATE '2025-05-01', DATE '2025-06-01', 'closed_won',   9600.00, true),  -- expansion deal
    -- still open
    (19,  9, 4, 4, DATE '2025-08-01', NULL,              'negotiation', 12000.00, false),
    (20, 11, 2, 3, DATE '2025-09-01', NULL,              'prospecting',  4800.00, false);

-- =====================================================================
-- NPS FACT — survey responses
-- =====================================================================
CREATE TABLE fct_nps_responses (
    response_id   INTEGER PRIMARY KEY,
    customer_id   INTEGER NOT NULL,
    response_date DATE    NOT NULL,
    score         INTEGER NOT NULL,   -- 0..10
    category      VARCHAR NOT NULL    -- promoter (9-10) | passive (7-8) | detractor (0-6)
);
INSERT INTO fct_nps_responses VALUES
    ( 1,  1, DATE '2025-03-31', 10, 'promoter'),
    ( 2,  2, DATE '2025-03-31',  9, 'promoter'),
    ( 3,  3, DATE '2025-03-31',  6, 'detractor'),
    ( 4,  4, DATE '2025-03-31',  8, 'passive'),
    ( 5,  5, DATE '2025-03-31', 10, 'promoter'),
    ( 6,  6, DATE '2025-03-31',  9, 'promoter'),
    ( 7,  7, DATE '2025-03-31',  3, 'detractor'),
    ( 8,  8, DATE '2025-03-31',  7, 'passive'),
    ( 9,  9, DATE '2025-03-31',  8, 'passive'),
    (10, 10, DATE '2025-03-31', 10, 'promoter'),
    (11, 11, DATE '2025-03-31',  9, 'promoter'),
    (12, 12, DATE '2025-03-31',  5, 'detractor'),
    (13,  1, DATE '2025-06-30', 10, 'promoter'),
    (14,  3, DATE '2025-06-30',  8, 'passive'),
    (15,  5, DATE '2025-06-30',  9, 'promoter'),
    (16,  9, DATE '2025-06-30',  6, 'detractor'),
    (17, 10, DATE '2025-06-30', 10, 'promoter'),
    (18, 11, DATE '2025-06-30',  9, 'promoter');

-- =====================================================================
-- PAYMENTS FACT — settled / failed payment transactions
-- Settled payments mirror paid, non-trial invoices.
-- =====================================================================
CREATE TABLE fct_payments (
    payment_id   INTEGER PRIMARY KEY,
    invoice_id   INTEGER NOT NULL,
    customer_id  INTEGER NOT NULL,
    payment_date DATE    NOT NULL,
    amount       DECIMAL(10,2) NOT NULL,
    method       VARCHAR NOT NULL,   -- card | ach | wire
    status       VARCHAR NOT NULL    -- settled | failed | pending
);
INSERT INTO fct_payments
SELECT
    i.invoice_id AS payment_id,
    i.invoice_id,
    i.customer_id,
    i.invoice_date AS payment_date,
    i.amount,
    CASE c.segment WHEN 'enterprise' THEN 'wire' WHEN 'mid_market' THEN 'ach' ELSE 'card' END AS method,
    'settled' AS status
FROM fct_invoices i
JOIN dim_customer c ON c.customer_id = i.customer_id
WHERE i.status = 'paid' AND i.is_trial = false;
-- A couple of failed payment attempts.
INSERT INTO fct_payments VALUES
    (80001, 90003,  1, DATE '2025-05-01', 999.00, 'wire', 'failed'),
    (80002, 202504 * 100 + 7, 7, DATE '2025-04-01', 49.00, 'card', 'failed');

-- =====================================================================
-- DATA MARTS (4) — pre-aggregated, condensed reporting layer
-- =====================================================================

-- Monthly MRR by segment.
CREATE TABLE mart_monthly_mrr (
    month           DATE    NOT NULL,
    segment         VARCHAR NOT NULL,
    total_mrr       DECIMAL(14,2) NOT NULL,
    active_customers INTEGER NOT NULL,
    new_mrr         DECIMAL(14,2) NOT NULL,
    churned_mrr     DECIMAL(14,2) NOT NULL
);
INSERT INTO mart_monthly_mrr
SELECT
    s.snapshot_month AS month,
    c.segment,
    SUM(s.mrr) AS total_mrr,
    COUNT(*) FILTER (WHERE s.is_active) AS active_customers,
    COALESCE(SUM(e_new.mrr_delta), 0)  AS new_mrr,
    COALESCE(-SUM(e_churn.mrr_delta), 0) AS churned_mrr
FROM fct_mrr_snapshot s
JOIN dim_customer c ON c.customer_id = s.customer_id
LEFT JOIN fct_subscription_events e_new
       ON e_new.customer_id = s.customer_id
      AND e_new.event_type = 'new'
      AND date_trunc('month', e_new.event_date) = s.snapshot_month
LEFT JOIN fct_subscription_events e_churn
       ON e_churn.customer_id = s.customer_id
      AND e_churn.event_type = 'churn'
      AND date_trunc('month', e_churn.event_date) = s.snapshot_month
GROUP BY s.snapshot_month, c.segment;

-- Cohort retention by signup month.
CREATE TABLE mart_cohort_retention (
    cohort_month      DATE    NOT NULL,
    period_number     INTEGER NOT NULL,
    cohort_size       INTEGER NOT NULL,
    retained_customers INTEGER NOT NULL,
    retention_rate    DECIMAL(6,4) NOT NULL
);
INSERT INTO mart_cohort_retention
WITH cohorts AS (
    SELECT date_trunc('month', signup_date) AS cohort_month, COUNT(*) AS cohort_size
    FROM dim_customer GROUP BY 1
)
SELECT
    co.cohort_month,
    CAST(date_diff('month', co.cohort_month, s.snapshot_month) AS INTEGER) AS period_number,
    co.cohort_size,
    COUNT(*) FILTER (WHERE s.is_active) AS retained_customers,
    ROUND(COUNT(*) FILTER (WHERE s.is_active) * 1.0 / co.cohort_size, 4) AS retention_rate
FROM fct_mrr_snapshot s
JOIN dim_customer c ON c.customer_id = s.customer_id
JOIN cohorts co ON co.cohort_month = date_trunc('month', c.signup_date)
GROUP BY co.cohort_month, period_number, co.cohort_size;

-- Account health — customer-grain rollup (source for the opaque customer_ltv metric).
CREATE TABLE mart_account_health (
    customer_id    INTEGER PRIMARY KEY,
    as_of_month    DATE    NOT NULL,
    health_score   INTEGER NOT NULL,   -- 0..100
    ltv_value      DECIMAL(14,2) NOT NULL,
    last_usage_date DATE,
    open_tickets   INTEGER NOT NULL
);
INSERT INTO mart_account_health
SELECT
    c.customer_id,
    DATE '2025-12-01' AS as_of_month,
    -- simple deterministic health proxy from latest activity & support load
    LEAST(100, GREATEST(0,
        70
        + CASE c.segment WHEN 'enterprise' THEN 20 WHEN 'mid_market' THEN 10 ELSE 0 END
        - 5 * COALESCE(tix.open_tickets, 0)
        - CASE WHEN latest.is_active THEN 0 ELSE 40 END)) AS health_score,
    -- LTV = latest active MRR * expected lifetime (months), 0 once churned
    CASE WHEN latest.is_active
         THEN latest.mrr * (CASE c.segment WHEN 'enterprise' THEN 48 WHEN 'mid_market' THEN 36 ELSE 24 END)
         ELSE 0.00 END AS ltv_value,
    usage.last_usage_date,
    COALESCE(tix.open_tickets, 0) AS open_tickets
FROM dim_customer c
LEFT JOIN (
    SELECT customer_id, mrr, is_active
    FROM fct_mrr_snapshot WHERE snapshot_month = DATE '2025-12-01'
) latest ON latest.customer_id = c.customer_id
LEFT JOIN (
    SELECT customer_id, MAX(usage_date) AS last_usage_date
    FROM fct_usage GROUP BY customer_id
) usage ON usage.customer_id = c.customer_id
LEFT JOIN (
    SELECT customer_id, COUNT(*) AS open_tickets
    FROM fct_support_tickets WHERE status = 'open' GROUP BY customer_id
) tix ON tix.customer_id = c.customer_id;

-- Sales rep quota attainment by quarter.
CREATE TABLE mart_rep_quota (
    rep_id            INTEGER NOT NULL,
    quarter           VARCHAR NOT NULL,   -- e.g. '2024-Q1'
    quota             DECIMAL(14,2) NOT NULL,
    closed_won_amount DECIMAL(14,2) NOT NULL,
    attainment_pct    DECIMAL(6,4) NOT NULL
);
INSERT INTO mart_rep_quota
WITH won AS (
    SELECT
        rep_id,
        year(closed_date) || '-Q' || quarter(closed_date) AS quarter,
        SUM(amount) AS closed_won_amount
    FROM fct_opportunities
    WHERE is_won AND closed_date IS NOT NULL
    GROUP BY 1, 2
)
SELECT
    rep_id,
    quarter,
    25000.00 AS quota,
    closed_won_amount,
    ROUND(closed_won_amount / 25000.00, 4) AS attainment_pct
FROM won;
