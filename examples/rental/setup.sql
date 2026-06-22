-- Rental Service demo database for Canon
-- 5 dimensions · 3 fact tables · 40 rentals · 32 payments · 8 damage records
--
-- Usage: sqlite3 rental.db < setup.sql

PRAGMA foreign_keys = ON;

-- ============================================================
-- DIMENSIONS
-- ============================================================

CREATE TABLE vehicle_categories (
    category_id  INTEGER PRIMARY KEY,
    name         TEXT    NOT NULL,
    daily_rate   NUMERIC(8,2) NOT NULL,
    description  TEXT
);

CREATE TABLE locations (
    location_id  INTEGER PRIMARY KEY,
    code         TEXT NOT NULL UNIQUE,
    city         TEXT NOT NULL,
    country      TEXT NOT NULL,
    airport_code TEXT
);

CREATE TABLE employees (
    employee_id  INTEGER PRIMARY KEY,
    first_name   TEXT NOT NULL,
    last_name    TEXT NOT NULL,
    title        TEXT NOT NULL,
    location_id  INTEGER NOT NULL REFERENCES locations(location_id),
    hire_date    TEXT NOT NULL
);

CREATE TABLE customers (
    customer_id     INTEGER PRIMARY KEY,
    first_name      TEXT NOT NULL,
    last_name       TEXT NOT NULL,
    email           TEXT NOT NULL UNIQUE,
    country         TEXT NOT NULL,
    membership_tier TEXT NOT NULL CHECK(membership_tier IN ('standard','silver','gold','platinum')),
    created_at      TEXT NOT NULL
);

CREATE TABLE vehicles (
    vehicle_id    INTEGER PRIMARY KEY,
    make          TEXT NOT NULL,
    model         TEXT NOT NULL,
    year          INTEGER NOT NULL,
    category_id   INTEGER NOT NULL REFERENCES vehicle_categories(category_id),
    license_plate TEXT NOT NULL UNIQUE,
    color         TEXT
);

-- ============================================================
-- FACTS
-- ============================================================

CREATE TABLE rentals (
    rental_id            INTEGER PRIMARY KEY,
    customer_id          INTEGER NOT NULL REFERENCES customers(customer_id),
    vehicle_id           INTEGER NOT NULL REFERENCES vehicles(vehicle_id),
    pickup_location_id   INTEGER NOT NULL REFERENCES locations(location_id),
    dropoff_location_id  INTEGER NOT NULL REFERENCES locations(location_id),
    employee_id          INTEGER NOT NULL REFERENCES employees(employee_id),
    pickup_date          TEXT NOT NULL,
    dropoff_date         TEXT,
    planned_days         INTEGER NOT NULL,
    actual_days          INTEGER,
    rate_per_day         NUMERIC(8,2)  NOT NULL,
    total_amount         NUMERIC(10,2),
    status               TEXT NOT NULL CHECK(status IN ('confirmed','active','completed','cancelled','no_show'))
);

CREATE TABLE payments (
    payment_id   INTEGER PRIMARY KEY,
    rental_id    INTEGER NOT NULL REFERENCES rentals(rental_id),
    payment_date TEXT NOT NULL,
    amount       NUMERIC(10,2) NOT NULL,
    method       TEXT NOT NULL CHECK(method IN ('credit_card','debit_card','cash','corporate')),
    status       TEXT NOT NULL CHECK(status IN ('pending','settled','refunded','failed'))
);

CREATE TABLE damages (
    damage_id         INTEGER PRIMARY KEY,
    rental_id         INTEGER NOT NULL REFERENCES rentals(rental_id),
    vehicle_id        INTEGER NOT NULL REFERENCES vehicles(vehicle_id),
    reported_date     TEXT NOT NULL,
    severity          TEXT NOT NULL CHECK(severity IN ('minor','moderate','major')),
    description       TEXT,
    repair_cost       NUMERIC(10,2),
    at_fault_customer INTEGER NOT NULL DEFAULT 1  -- 1 = customer liable, 0 = no-fault
);

-- ============================================================
-- SEED DATA — DIMENSIONS
-- ============================================================

INSERT INTO vehicle_categories VALUES
  (1, 'compact',  49.99, 'Small fuel-efficient cars'),
  (2, 'midsize',  69.99, 'Comfortable mid-range sedans'),
  (3, 'suv',      89.99, 'Sport utility vehicles'),
  (4, 'luxury',  149.99, 'Premium luxury vehicles'),
  (5, 'van',      99.99, 'Passenger and cargo vans');

INSERT INTO locations VALUES
  (1, 'JFK-LOC', 'New York',    'US', 'JFK'),
  (2, 'LAX-LOC', 'Los Angeles', 'US', 'LAX'),
  (3, 'ORD-LOC', 'Chicago',     'US', 'ORD'),
  (4, 'LHR-LOC', 'London',      'UK', 'LHR'),
  (5, 'CDG-LOC', 'Paris',       'FR', 'CDG');

INSERT INTO employees VALUES
  (1, 'Alice',  'Morgan',  'Location Manager', 1, '2021-03-15'),
  (2, 'James',  'Park',    'Rental Agent',     1, '2022-07-01'),
  (3, 'Sofia',  'Reyes',   'Location Manager', 2, '2020-11-20'),
  (4, 'Derek',  'Collins', 'Rental Agent',     2, '2023-01-10'),
  (5, 'Priya',  'Sharma',  'Location Manager', 3, '2021-06-01'),
  (6, 'Liam',   'Novak',   'Rental Agent',     3, '2023-05-15'),
  (7, 'Hannah', 'Fischer', 'Location Manager', 4, '2020-09-01'),
  (8, 'Ethan',  'Dubois',  'Location Manager', 5, '2022-02-28');

INSERT INTO customers VALUES
  (1,  'Emily',    'Chen',      'emily.chen@email.com',     'US', 'gold',     '2023-01-15'),
  (2,  'Michael',  'Johnson',   'mjohnson@business.com',    'US', 'platinum', '2022-06-20'),
  (3,  'Sarah',    'Williams',  'swilliams@personal.net',   'US', 'silver',   '2023-08-05'),
  (4,  'James',    'Brown',     'jbrown@email.com',         'UK', 'standard', '2024-01-10'),
  (5,  'Marie',    'Dupont',    'mdupont@mail.fr',          'FR', 'gold',     '2023-03-22'),
  (6,  'Carlos',   'Rodriguez', 'crodriguez@email.es',      'ES', 'standard', '2024-02-14'),
  (7,  'Anna',     'Schneider', 'anna.s@email.de',          'DE', 'silver',   '2023-11-30'),
  (8,  'David',    'Kim',       'david.kim@tech.com',       'US', 'platinum', '2022-09-18'),
  (9,  'Sophie',   'Martin',    'sophie.m@email.fr',        'FR', 'silver',   '2024-03-07'),
  (10, 'Robert',   'Taylor',    'rtaylor@corp.com',         'US', 'gold',     '2023-05-12'),
  (11, 'Yuki',     'Tanaka',    'ytanaka@email.jp',         'JP', 'standard', '2024-04-01'),
  (12, 'Isabella', 'Ferrari',   'iferrari@email.it',        'IT', 'silver',   '2023-07-19'),
  (13, 'William',  'Davis',     'wdavis@consulting.com',    'US', 'platinum', '2022-11-05'),
  (14, 'Olivia',   'Wilson',    'owilson@email.com',        'US', 'gold',     '2023-09-23'),
  (15, 'Lucas',    'Bernard',   'lbernard@email.fr',        'FR', 'standard', '2024-05-18'),
  (16, 'Emma',     'Thompson',  'ethompson@uk.email.com',   'UK', 'gold',     '2023-04-11'),
  (17, 'Noah',     'Martinez',  'nmartinez@email.mx',       'MX', 'standard', '2024-06-02'),
  (18, 'Ava',      'Anderson',  'aanderson@startup.io',     'US', 'silver',   '2023-12-15'),
  (19, 'Liam',     'White',     'lwhite@enterprise.com',    'US', 'platinum', '2022-07-30'),
  (20, 'Charlotte','Jackson',   'cjackson@email.com',       'US', 'standard', '2024-02-28');

INSERT INTO vehicles VALUES
  (1,  'Toyota',     'Corolla',       2022, 1, 'AAA-001', 'Silver'),
  (2,  'Honda',      'Civic',         2023, 1, 'AAA-002', 'Blue'),
  (3,  'Volkswagen', 'Jetta',         2022, 1, 'AAA-003', 'White'),
  (4,  'Toyota',     'Camry',         2023, 2, 'BBB-001', 'Black'),
  (5,  'Honda',      'Accord',        2022, 2, 'BBB-002', 'Gray'),
  (6,  'Mazda',      'Mazda6',        2023, 2, 'BBB-003', 'Red'),
  (7,  'Ford',       'Explorer',      2023, 3, 'CCC-001', 'White'),
  (8,  'Jeep',       'Grand Cherokee',2022, 3, 'CCC-002', 'Black'),
  (9,  'Toyota',     'RAV4',          2023, 3, 'CCC-003', 'Blue'),
  (10, 'BMW',        '5 Series',      2023, 4, 'DDD-001', 'Black'),
  (11, 'Mercedes',   'E-Class',       2022, 4, 'DDD-002', 'Silver'),
  (12, 'Audi',       'A6',            2023, 4, 'DDD-003', 'White'),
  (13, 'Ford',       'Transit',       2022, 5, 'EEE-001', 'White'),
  (14, 'Mercedes',   'Sprinter',      2023, 5, 'EEE-002', 'Silver'),
  (15, 'Dodge',      'Grand Caravan', 2022, 5, 'EEE-003', 'Gray');

-- ============================================================
-- SEED DATA — FACT: rentals
-- Rates: compact=49.99 midsize=69.99 suv=89.99 luxury=149.99 van=99.99
-- total_amount = actual_days * rate_per_day  (completed only)
-- ============================================================

INSERT INTO rentals VALUES
--  id  cust  veh  pick  drop  emp  pickup       dropoff       pdays  adays  rate    total      status
  ( 1,  1,   1,   1,   2,   2, '2024-01-10', '2024-01-17',  7,  7,  49.99,  349.93, 'completed'),
  ( 2,  2,  10,   2,   1,   4, '2024-01-15', '2024-01-22',  7,  7, 149.99, 1049.93, 'completed'),
  ( 3,  3,   4,   1,   4,   2, '2024-01-20', '2024-01-23',  3,  3,  69.99,  209.97, 'completed'),
  ( 4,  4,   7,   4,   2,   7, '2024-02-01', '2024-02-08',  7,  7,  89.99,  629.93, 'completed'),
  ( 5,  5,  11,   5,   3,   8, '2024-02-10', '2024-02-14',  4,  4, 149.99,  599.96, 'completed'),
  ( 6,  6,   2,   2,   5,   4, '2024-02-15', '2024-02-18',  3,  3,  49.99,  149.97, 'completed'),
  ( 7,  7,   8,   3,   1,   6, '2024-03-01', '2024-03-08',  7,  7,  89.99,  629.93, 'completed'),
  ( 8,  8,  12,   1,   2,   2, '2024-03-10', '2024-03-17',  7,  7, 149.99, 1049.93, 'completed'),
  ( 9,  9,   5,   5,   4,   8, '2024-03-20', '2024-03-25',  5,  5,  69.99,  349.95, 'completed'),
  (10, 10,   3,   1,   3,   2, '2024-04-01', '2024-04-04',  3,  3,  49.99,  149.97, 'completed'),
  (11, 11,  13,   2,   4,   3, '2024-04-10', '2024-04-17',  7,  7,  99.99,  699.93, 'completed'),
  (12, 12,   9,   4,   5,   7, '2024-04-15', '2024-04-22',  7,  7,  89.99,  629.93, 'completed'),
  (13, 13,   1,   1,   3,   2, '2024-05-01', '2024-05-05',  4,  4,  49.99,  199.96, 'completed'),
  (14, 14,   6,   3,   5,   5, '2024-05-10', '2024-05-17',  7,  7,  69.99,  489.93, 'completed'),
  (15, 15,  14,   5,   1,   8, '2024-05-20', '2024-05-27',  7,  7,  99.99,  699.93, 'completed'),
  (16, 16,  10,   4,   2,   7, '2024-06-01', '2024-06-08',  7,  7, 149.99, 1049.93, 'completed'),
  (17, 17,   2,   2,   3,   4, '2024-06-10', '2024-06-12',  2,  2,  49.99,   99.98, 'completed'),
  (18, 18,   7,   1,   4,   1, '2024-06-15', '2024-06-22',  7,  7,  89.99,  629.93, 'completed'),
  (19, 19,   4,   3,   1,   5, '2024-07-01', '2024-07-08',  7,  7,  69.99,  489.93, 'completed'),
  (20, 20,  11,   5,   2,   8, '2024-07-10', '2024-07-17',  7,  7, 149.99, 1049.93, 'completed'),
  (21,  1,   8,   1,   5,   1, '2024-07-20', '2024-07-25',  5,  5,  89.99,  449.95, 'completed'),
  (22,  2,  15,   2,   4,   3, '2024-08-01', '2024-08-08',  7,  7,  99.99,  699.93, 'completed'),
  (23,  3,   3,   1,   2,   2, '2024-08-10', '2024-08-12',  2,  2,  49.99,   99.98, 'completed'),
  (24,  4,  12,   4,   3,   7, '2024-08-15', '2024-08-22',  7,  7, 149.99, 1049.93, 'completed'),
  (25,  5,   9,   5,   1,   8, '2024-09-01', '2024-09-05',  4,  4,  89.99,  359.96, 'completed'),
  (26,  6,   5,   2,   5,   4, '2024-09-10', '2024-09-17',  7,  7,  69.99,  489.93, 'completed'),
  (27,  7,  13,   3,   2,   6, '2024-09-20', '2024-09-27',  7,  7,  99.99,  699.93, 'completed'),
  (28,  8,   1,   1,   4,   2, '2024-10-01', '2024-10-08',  7,  7,  49.99,  349.93, 'completed'),
  (29,  9,   7,   4,   1,   7, '2024-10-10', '2024-10-17',  7,  7,  89.99,  629.93, 'completed'),
  (30, 10,   2,   2,   5,   3, '2024-10-20', '2024-10-22',  2,  2,  49.99,   99.98, 'completed'),
  (31, 11,  10,   1,   5,   1, '2024-11-01', '2024-11-08',  7,  7, 149.99, 1049.93, 'completed'),
  (32, 12,   6,   3,   4,   5, '2024-11-10', '2024-11-14',  4,  4,  69.99,  279.96, 'completed'),
  -- active (pickup_date in June 2026, no dropoff yet)
  (33, 13,   8,   2,   5,   4, '2026-06-15', NULL,          7, NULL, 89.99,    NULL, 'active'),
  (34, 14,  11,   4,   1,   7, '2026-06-18', NULL,          5, NULL,149.99,    NULL, 'active'),
  (35, 15,   2,   1,   3,   2, '2026-06-20', NULL,          3, NULL, 49.99,    NULL, 'active'),
  -- confirmed (future)
  (36, 16,   4,   3,   2,   5, '2026-07-01', NULL,          7, NULL, 69.99,    NULL, 'confirmed'),
  (37, 17,  12,   5,   1,   8, '2026-07-10', NULL,          7, NULL,149.99,    NULL, 'confirmed'),
  -- cancelled
  (38, 18,   6,   1,   3,   1, '2026-05-20', NULL,          5, NULL, 69.99,    NULL, 'cancelled'),
  (39, 19,   9,   2,   4,   3, '2026-03-01', NULL,          3, NULL, 89.99,    NULL, 'cancelled'),
  -- no-show
  (40, 20,   3,   3,   5,   6, '2026-04-15', NULL,          2, NULL, 49.99,    NULL, 'no_show');

-- ============================================================
-- SEED DATA — FACT: payments
-- One settled payment per completed rental (rentals 1-32)
-- ============================================================

INSERT INTO payments VALUES
  ( 1,  1, '2024-01-17',  349.93, 'credit_card', 'settled'),
  ( 2,  2, '2024-01-22', 1049.93, 'corporate',   'settled'),
  ( 3,  3, '2024-01-23',  209.97, 'credit_card', 'settled'),
  ( 4,  4, '2024-02-08',  629.93, 'credit_card', 'settled'),
  ( 5,  5, '2024-02-14',  599.96, 'debit_card',  'settled'),
  ( 6,  6, '2024-02-18',  149.97, 'cash',        'settled'),
  ( 7,  7, '2024-03-08',  629.93, 'credit_card', 'settled'),
  ( 8,  8, '2024-03-17', 1049.93, 'corporate',   'settled'),
  ( 9,  9, '2024-03-25',  349.95, 'credit_card', 'settled'),
  (10, 10, '2024-04-04',  149.97, 'debit_card',  'settled'),
  (11, 11, '2024-04-17',  699.93, 'corporate',   'settled'),
  (12, 12, '2024-04-22',  629.93, 'credit_card', 'settled'),
  (13, 13, '2024-05-05',  199.96, 'credit_card', 'settled'),
  (14, 14, '2024-05-17',  489.93, 'debit_card',  'settled'),
  (15, 15, '2024-05-27',  699.93, 'corporate',   'settled'),
  (16, 16, '2024-06-08', 1049.93, 'corporate',   'settled'),
  (17, 17, '2024-06-12',   99.98, 'credit_card', 'settled'),
  (18, 18, '2024-06-22',  629.93, 'credit_card', 'settled'),
  (19, 19, '2024-07-08',  489.93, 'debit_card',  'settled'),
  (20, 20, '2024-07-17', 1049.93, 'corporate',   'settled'),
  (21, 21, '2024-07-25',  449.95, 'credit_card', 'settled'),
  (22, 22, '2024-08-08',  699.93, 'corporate',   'settled'),
  (23, 23, '2024-08-12',   99.98, 'credit_card', 'settled'),
  (24, 24, '2024-08-22', 1049.93, 'credit_card', 'settled'),
  (25, 25, '2024-09-05',  359.96, 'debit_card',  'settled'),
  (26, 26, '2024-09-17',  489.93, 'credit_card', 'settled'),
  (27, 27, '2024-09-27',  699.93, 'corporate',   'settled'),
  (28, 28, '2024-10-08',  349.93, 'credit_card', 'settled'),
  (29, 29, '2024-10-17',  629.93, 'debit_card',  'settled'),
  (30, 30, '2024-10-22',   99.98, 'cash',        'settled'),
  (31, 31, '2024-11-08', 1049.93, 'corporate',   'settled'),
  (32, 32, '2024-11-14',  279.96, 'credit_card', 'settled');

-- ============================================================
-- SEED DATA — FACT: damages
-- ============================================================

INSERT INTO damages VALUES
  (1,  3,  4, '2024-01-23', 'minor',    'Scratch on front bumper',       250.00, 1),
  (2,  7,  8, '2024-03-08', 'moderate', 'Dented rear door',             1200.00, 1),
  (3, 12,  9, '2024-04-22', 'minor',    'Cracked side mirror',           350.00, 0),
  (4, 15, 14, '2024-05-27', 'major',    'Front-end collision damage',   4500.00, 1),
  (5, 22, 15, '2024-08-08', 'minor',    'Interior upholstery stain',     180.00, 1),
  (6, 24, 12, '2024-08-22', 'minor',    'Small paint chip on hood',      220.00, 0),
  (7, 28,  1, '2024-10-08', 'moderate', 'Damaged windshield',            800.00, 1),
  (8, 31, 10, '2024-11-08', 'minor',    'Scratched alloy wheel',         400.00, 1);
