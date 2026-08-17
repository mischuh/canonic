#!/usr/bin/env python3
"""Deterministic generator for examples/marketplace/setup.sql.

Produces a large, realistic-looking SQLite seed dataset for the marketplace E12
(tenant scoping / RBAC) example: 5 merchants sharing one warehouse, ~24 months of
order history per merchant with seasonality, per-merchant growth/decline trends,
promotion windows, and hundreds of customers per merchant with fake PII for the
masking demo.

Usage (from the repo root)::

    .venv/bin/python scripts/generate_marketplace_data.py > examples/marketplace/setup.sql

Everything here is seeded (``SEED`` below); re-running produces byte-identical
output, so the generated file is diffable in review.

``END_DATE`` is pinned to a fixed date rather than computed from ``datetime.now()``,
so a re-run without editing the constant reproduces the exact same file (required for
the diffable-review property above). Bump ``END_DATE`` by hand every so often to keep
"this month" / "last 3 months" queries meaningful, and update every place that quotes
the resulting date range or example query output (``examples/marketplace/README.md``,
``docs/guides/marketplace.mdx``, ``semantics/marketplace_db/dim_date.yaml``) to match.
"""

from __future__ import annotations

import datetime as dt
import sys
from dataclasses import dataclass, field
from random import Random

SEED = 20260817
# Ends "today" (kept in step with the repo's current date) so the most recent month
# and the trailing-3-months window are never empty — regenerate periodically to keep
# it current; see the module docstring.
END_DATE = dt.date(2026, 8, 17)
START_DATE = END_DATE - dt.timedelta(days=730)  # ~24 months of history

_FIRST_NAMES = [
    "Olivia",
    "Liam",
    "Emma",
    "Noah",
    "Ava",
    "Elijah",
    "Sophia",
    "James",
    "Isabella",
    "William",
    "Mia",
    "Benjamin",
    "Charlotte",
    "Lucas",
    "Amelia",
    "Henry",
    "Harper",
    "Alexander",
    "Evelyn",
    "Michael",
    "Abigail",
    "Daniel",
    "Emily",
    "Jacob",
    "Elizabeth",
    "Logan",
    "Sofia",
    "Jackson",
    "Avery",
    "Sebastian",
    "Ella",
    "Aiden",
    "Scarlett",
    "Matthew",
    "Grace",
    "Samuel",
    "Chloe",
    "David",
    "Victoria",
    "Joseph",
    "Riley",
    "Carter",
    "Aria",
    "Owen",
    "Lily",
    "Wyatt",
    "Zoey",
    "John",
    "Nora",
    "Luke",
    "Hannah",
]
_LAST_NAMES = [
    "Smith",
    "Johnson",
    "Williams",
    "Brown",
    "Jones",
    "Garcia",
    "Miller",
    "Davis",
    "Rodriguez",
    "Martinez",
    "Hernandez",
    "Lopez",
    "Gonzalez",
    "Wilson",
    "Anderson",
    "Thomas",
    "Taylor",
    "Moore",
    "Jackson",
    "Martin",
    "Lee",
    "Perez",
    "Thompson",
    "White",
    "Harris",
    "Sanchez",
    "Clark",
    "Ramirez",
    "Lewis",
    "Robinson",
    "Walker",
    "Young",
    "Allen",
    "King",
    "Wright",
    "Scott",
    "Torres",
    "Nguyen",
    "Hill",
    "Flores",
    "Green",
    "Adams",
    "Nelson",
    "Baker",
    "Hall",
    "Rivera",
    "Campbell",
    "Mitchell",
]
_EMAIL_DOMAINS = [
    "mailbox.com",
    "inbox.net",
    "webmail.io",
    "example.org",
    "freemail.co",
    "postbox.net",
]

_PHONE_FORMATS: dict[str, str] = {
    "US": "+1-{a:03d}-{b:03d}-{c:04d}",
    "GB": "+44-7{a:03d}-{b:03d}{c:03d}",
    "DE": "+49-15{a:02d}-{b:03d}{c:03d}",
    "CA": "+1-{a:03d}-{b:03d}-{c:04d}",
}


@dataclass(frozen=True)
class Promo:
    promo_id: str
    merchant_id: str
    promo_name: str
    discount_pct: float
    start_date: dt.date
    end_date: dt.date


@dataclass
class Merchant:
    merchant_id: str
    merchant_name: str
    category: str
    country: str
    currency: str
    n_customers: int
    target_orders: int
    trend: float  # relative change in daily order rate from period start to period end
    weekend_multiplier: float
    price_range: tuple[float, float]
    sku_prefix: str
    n_skus: int
    promo_names: list[str]

    customer_ids: list[int] = field(default_factory=list)
    promos: list[Promo] = field(default_factory=list)


MERCHANTS: list[Merchant] = [
    Merchant(
        merchant_id="byte-gadgets",
        merchant_name="Byte Gadgets",
        category="electronics",
        country="US",
        currency="USD",
        n_customers=420,
        target_orders=1280,
        trend=0.65,  # visibly growing
        weekend_multiplier=1.12,
        price_range=(15.0, 420.0),
        sku_prefix="BG",
        n_skus=40,
        promo_names=["Back to School", "Black Friday Blowout", "Spring Upgrade Sale"],
    ),
    Merchant(
        merchant_id="urban-threads",
        merchant_name="Urban Threads",
        category="apparel",
        country="GB",
        currency="GBP",
        n_customers=260,
        target_orders=640,
        trend=-0.50,  # visibly declining
        weekend_multiplier=1.30,
        price_range=(8.0, 130.0),
        sku_prefix="UT",
        n_skus=35,
        promo_names=["End of Season Clearout", "Boxing Week Sale"],
    ),
    Merchant(
        merchant_id="artisan-coffee",
        merchant_name="Artisan Coffee Co.",
        category="coffee & beverages",
        country="US",
        currency="USD",
        n_customers=240,
        target_orders=600,
        trend=0.35,  # moderate growth
        weekend_multiplier=1.20,
        price_range=(6.0, 38.0),
        sku_prefix="AC",
        n_skus=22,
        promo_names=["Harvest Roast Launch", "Holiday Blend Special"],
    ),
    Merchant(
        merchant_id="green-leaf",
        merchant_name="Green Leaf Botanicals",
        category="home & garden",
        country="DE",
        currency="EUR",
        n_customers=150,
        target_orders=350,
        trend=0.08,  # roughly flat
        weekend_multiplier=1.08,
        price_range=(8.0, 65.0),
        sku_prefix="GL",
        n_skus=28,
        promo_names=["Spring Planting Sale"],
    ),
    Merchant(
        merchant_id="cozy-candles",
        merchant_name="Cozy Candles",
        category="home decor",
        country="CA",
        currency="CAD",
        n_customers=110,
        target_orders=250,
        trend=-0.05,  # roughly flat
        weekend_multiplier=1.22,
        price_range=(5.0, 42.0),
        sku_prefix="CC",
        n_skus=18,
        promo_names=["Winter Glow Sale"],
    ),
]

CURRENCIES = [
    ("USD", "US Dollar", "$"),
    ("GBP", "British Pound", "£"),
    ("EUR", "Euro", "€"),
    ("CAD", "Canadian Dollar", "CA$"),
]

STATUS_WEIGHTS = [("completed", 0.85), ("pending", 0.05), ("cancelled", 0.07), ("refunded", 0.03)]

# Calendar month each named promo is anchored to, so windows land somewhere plausible
# (Black Friday in November, back-to-school in August, ...) rather than a bare even split
# of the 24-month range.
PROMO_ANCHOR_MONTH: dict[str, int] = {
    "Back to School": 8,
    "Black Friday Blowout": 11,
    "Spring Upgrade Sale": 3,
    "End of Season Clearout": 1,
    "Boxing Week Sale": 12,
    "Harvest Roast Launch": 10,
    "Holiday Blend Special": 12,
    "Spring Planting Sale": 4,
    "Winter Glow Sale": 1,
}


def daterange(start: dt.date, end: dt.date):
    days = (end - start).days
    for i in range(days + 1):
        yield start + dt.timedelta(days=i)


def sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def sql_val(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dt.date):
        return sql_str(value.isoformat())
    return sql_str(str(value))


def emit_inserts(
    out: list[str], table: str, columns: list[str], rows: list[tuple], batch: int = 500
) -> None:
    if not rows:
        return
    col_list = ", ".join(columns)
    for start in range(0, len(rows), batch):
        chunk = rows[start : start + batch]
        out.append(f"INSERT INTO {table} ({col_list}) VALUES")
        lines = [f"  ({', '.join(sql_val(v) for v in row)})" for row in chunk]
        out.append(",\n".join(lines) + ";")
        out.append("")


def weighted_choice(rng: Random, weights: list[tuple[str, float]]) -> str:
    total = sum(w for _, w in weights)
    r = rng.uniform(0, total)
    upto = 0.0
    for name, w in weights:
        upto += w
        if r <= upto:
            return name
    return weights[-1][0]


def gen_customers(rng: Random, merchant: Merchant, next_id: int) -> list[tuple]:
    rows: list[tuple] = []
    phone_fmt = _PHONE_FORMATS[merchant.country]
    for _ in range(merchant.n_customers):
        first = rng.choice(_FIRST_NAMES)
        last = rng.choice(_LAST_NAMES)
        cid = next_id
        next_id += 1
        domain = rng.choice(_EMAIL_DOMAINS)
        email = f"{first.lower()}.{last.lower()}{cid}@{domain}"
        phone = phone_fmt.format(
            a=rng.randint(1, 899), b=rng.randint(0, 999), c=rng.randint(0, 9999)
        )
        rows.append((cid, merchant.merchant_id, f"{first} {last}", email, phone, merchant.country))
        merchant.customer_ids.append(cid)
    return rows, next_id


def gen_promos(rng: Random, merchant: Merchant, days: list[dt.date]) -> list[Promo]:
    """Place each of the merchant's named promos in a plausible calendar window.

    Each promo name is anchored to a real-world month (Black Friday in November,
    back-to-school in August, ...) via ``PROMO_ANCHOR_MONTH``. Since a merchant's
    promo names never share an anchor month, the resulting windows never overlap.
    """
    promos: list[Promo] = []
    for i, name in enumerate(merchant.promo_names):
        anchor_month = PROMO_ANCHOR_MONTH[name]
        month_days = [d for d in days if d.month == anchor_month]
        years = sorted({d.year for d in month_days})
        year = rng.choice(years)
        candidates = [d for d in month_days if d.year == year]
        window_len = rng.randint(7, 14)
        max_start_idx = max(len(candidates) - window_len, 0)
        offset = rng.randint(0, max_start_idx)
        start = candidates[offset]
        end = start + dt.timedelta(days=window_len - 1)
        discount = round(rng.uniform(10.0, 30.0), 2)
        promo_id = f"promo-{merchant.merchant_id}-{i + 1}"
        promos.append(
            Promo(
                promo_id=promo_id,
                merchant_id=merchant.merchant_id,
                promo_name=name,
                discount_pct=discount,
                start_date=start,
                end_date=end,
            )
        )
    merchant.promos = promos
    return promos


def active_promo(promos: list[Promo], day: dt.date) -> Promo | None:
    for p in promos:
        if p.start_date <= day <= p.end_date:
            return p
    return None


def gen_orders_and_items(
    rng: Random, merchant: Merchant, days: list[dt.date], next_order_id: int, next_item_id: int
) -> tuple[list[tuple], list[tuple], int, int]:
    n_days = len(days)
    raw_weights: list[float] = []
    for i, day in enumerate(days):
        trend_factor = 1.0 + merchant.trend * (i / max(n_days - 1, 1))
        weekday_mult = merchant.weekend_multiplier if day.weekday() >= 5 else 1.0
        promo = active_promo(merchant.promos, day)
        promo_mult = 1.8 if promo is not None else 1.0
        noise = rng.uniform(0.65, 1.35)
        raw_weights.append(trend_factor * weekday_mult * promo_mult * noise)

    total_weight = sum(raw_weights)
    scale = merchant.target_orders / total_weight
    expected = [w * scale for w in raw_weights]

    # Stochastic rounding so the sum lands close to target_orders while each day's
    # count still reflects its underlying expected value.
    daily_counts: list[int] = []
    for e in expected:
        base = int(e)
        frac = e - base
        daily_counts.append(base + 1 if rng.random() < frac else base)

    order_rows: list[tuple] = []
    item_rows: list[tuple] = []
    sku_pool = [f"{merchant.sku_prefix}-{n:03d}" for n in range(1, merchant.n_skus + 1)]
    lo, hi = merchant.price_range

    order_id = next_order_id
    item_id = next_item_id
    for day, count in zip(days, daily_counts, strict=True):
        promo = active_promo(merchant.promos, day)
        for _ in range(count):
            customer_id = rng.choice(merchant.customer_ids)
            status = weighted_choice(rng, STATUS_WEIGHTS)
            n_items = rng.choices([1, 2, 3, 4, 5], weights=[35, 30, 20, 10, 5])[0]
            subtotal = 0.0
            item_rows_for_order: list[tuple] = []
            for _ in range(n_items):
                sku = rng.choice(sku_pool)
                quantity = rng.choices([1, 2, 3], weights=[65, 25, 10])[0]
                unit_price = round(rng.uniform(lo, hi), 2)
                line_amount = round(unit_price * quantity, 2)
                subtotal += line_amount
                item_rows_for_order.append(
                    (
                        item_id,
                        order_id,
                        merchant.merchant_id,
                        sku,
                        quantity,
                        unit_price,
                        line_amount,
                    )
                )
                item_id += 1

            promo_id = None
            if promo is not None and rng.random() < 0.55:
                promo_id = promo.promo_id
                amount = round(subtotal * (1 - promo.discount_pct / 100.0), 2)
            else:
                amount = round(subtotal, 2)

            order_rows.append(
                (
                    order_id,
                    merchant.merchant_id,
                    customer_id,
                    day,
                    merchant.currency,
                    promo_id,
                    status,
                    amount,
                )
            )
            item_rows.extend(item_rows_for_order)
            order_id += 1

    return order_rows, item_rows, order_id, item_id


def build_dim_date(days: list[dt.date]) -> list[tuple]:
    weekday_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    rows = []
    for day in days:
        rows.append(
            (
                day,
                day.year,
                day.month,
                day.day,
                weekday_names[day.weekday()],
                1 if day.weekday() >= 5 else 0,
                (day.month - 1) // 3 + 1,
            )
        )
    return rows


DDL = """\
-- Marketplace demo database for Canonic (SPEC-E12: tenant scoping / RBAC)
--
-- GENERATED FILE — do not hand-edit. Regenerate with:
--   .venv/bin/python scripts/generate_marketplace_data.py > examples/marketplace/setup.sql
--
-- 5 merchants · ~24 months of order history ({start} .. {end}) with per-merchant
-- growth/decline trends, weekday/weekend seasonality, and promotion-window volume bumps.
--
-- Usage: sqlite3 marketplace.db < setup.sql

PRAGMA foreign_keys = ON;

-- ============================================================
-- SHARED DIMENSIONS (never tenant-scoped)
-- ============================================================

CREATE TABLE merchants (
    merchant_id   TEXT PRIMARY KEY,
    merchant_name TEXT NOT NULL,
    category      TEXT NOT NULL,
    country       TEXT NOT NULL
);

CREATE TABLE dim_date (
    date_id      TEXT PRIMARY KEY,
    year         INTEGER NOT NULL,
    month        INTEGER NOT NULL,
    day          INTEGER NOT NULL,
    weekday_name TEXT NOT NULL,
    is_weekend   INTEGER NOT NULL,
    quarter      INTEGER NOT NULL
);

CREATE TABLE dim_currency (
    currency_code TEXT PRIMARY KEY,
    currency_name TEXT NOT NULL,
    symbol        TEXT NOT NULL
);

-- ============================================================
-- TENANT-SCOPED SOURCES (row-level, keyed on merchant_id)
-- ============================================================

CREATE TABLE customers (
    customer_id    INTEGER PRIMARY KEY,
    merchant_id    TEXT NOT NULL REFERENCES merchants(merchant_id),
    customer_name  TEXT NOT NULL,
    customer_email TEXT NOT NULL,
    customer_phone TEXT NOT NULL,
    country        TEXT NOT NULL
);

CREATE TABLE orders (
    order_id      INTEGER PRIMARY KEY,
    merchant_id   TEXT NOT NULL REFERENCES merchants(merchant_id),
    customer_id   INTEGER NOT NULL REFERENCES customers(customer_id),
    order_date    TEXT NOT NULL REFERENCES dim_date(date_id),
    currency_code TEXT NOT NULL REFERENCES dim_currency(currency_code),
    promo_id      TEXT,
    status        TEXT NOT NULL CHECK(status IN ('completed','pending','cancelled','refunded')),
    amount        NUMERIC(10,2) NOT NULL
);

CREATE TABLE order_items (
    order_item_id INTEGER PRIMARY KEY,
    order_id      INTEGER NOT NULL REFERENCES orders(order_id),
    merchant_id   TEXT NOT NULL REFERENCES merchants(merchant_id),
    sku           TEXT NOT NULL,
    quantity      INTEGER NOT NULL,
    unit_price    NUMERIC(10,2) NOT NULL,
    line_amount   NUMERIC(10,2) NOT NULL
);

-- promotions is deliberately declared in NEITHER scoped_sources NOR shared_sources in
-- contracts/policies/tenancy.yaml — a policy hole that TENANT_SCOPE_MISSING catches.
CREATE TABLE promotions (
    promo_id      TEXT PRIMARY KEY,
    merchant_id   TEXT NOT NULL REFERENCES merchants(merchant_id),
    promo_name    TEXT NOT NULL,
    discount_pct  NUMERIC(5,2) NOT NULL,
    start_date    TEXT NOT NULL,
    end_date      TEXT NOT NULL
);

CREATE INDEX idx_orders_merchant ON orders(merchant_id);
CREATE INDEX idx_orders_customer ON orders(customer_id);
CREATE INDEX idx_orders_date ON orders(order_date);
CREATE INDEX idx_order_items_order ON order_items(order_id);
CREATE INDEX idx_order_items_merchant ON order_items(merchant_id);
CREATE INDEX idx_customers_merchant ON customers(merchant_id);
CREATE INDEX idx_promotions_merchant ON promotions(merchant_id);
"""


def main() -> None:
    rng = Random(SEED)
    days = list(daterange(START_DATE, END_DATE))

    out: list[str] = [DDL.format(start=START_DATE.isoformat(), end=END_DATE.isoformat())]

    out.append("-- ============================================================")
    out.append("-- SEED DATA — SHARED DIMENSIONS")
    out.append("-- ============================================================\n")

    merchant_rows = [(m.merchant_id, m.merchant_name, m.category, m.country) for m in MERCHANTS]
    emit_inserts(
        out, "merchants", ["merchant_id", "merchant_name", "category", "country"], merchant_rows
    )

    emit_inserts(out, "dim_currency", ["currency_code", "currency_name", "symbol"], CURRENCIES)

    dim_date_rows = build_dim_date(days)
    emit_inserts(
        out,
        "dim_date",
        ["date_id", "year", "month", "day", "weekday_name", "is_weekend", "quarter"],
        dim_date_rows,
    )

    out.append("-- ============================================================")
    out.append("-- SEED DATA — TENANT-SCOPED SOURCES")
    out.append("-- ============================================================\n")

    next_customer_id = 1
    next_order_id = 1
    next_item_id = 1

    all_customer_rows: list[tuple] = []
    all_promo_rows: list[tuple] = []
    all_order_rows: list[tuple] = []
    all_item_rows: list[tuple] = []

    for merchant in MERCHANTS:
        cust_rows, next_customer_id = gen_customers(rng, merchant, next_customer_id)
        all_customer_rows.extend(cust_rows)

        promos = gen_promos(rng, merchant, days)
        for p in promos:
            all_promo_rows.append(
                (p.promo_id, p.merchant_id, p.promo_name, p.discount_pct, p.start_date, p.end_date)
            )

        order_rows, item_rows, next_order_id, next_item_id = gen_orders_and_items(
            rng, merchant, days, next_order_id, next_item_id
        )
        all_order_rows.extend(order_rows)
        all_item_rows.extend(item_rows)

    emit_inserts(
        out,
        "customers",
        [
            "customer_id",
            "merchant_id",
            "customer_name",
            "customer_email",
            "customer_phone",
            "country",
        ],
        all_customer_rows,
    )
    emit_inserts(
        out,
        "promotions",
        ["promo_id", "merchant_id", "promo_name", "discount_pct", "start_date", "end_date"],
        all_promo_rows,
    )
    emit_inserts(
        out,
        "orders",
        [
            "order_id",
            "merchant_id",
            "customer_id",
            "order_date",
            "currency_code",
            "promo_id",
            "status",
            "amount",
        ],
        all_order_rows,
    )
    emit_inserts(
        out,
        "order_items",
        [
            "order_item_id",
            "order_id",
            "merchant_id",
            "sku",
            "quantity",
            "unit_price",
            "line_amount",
        ],
        all_item_rows,
    )

    summary = (
        f"-- Row counts: merchants={len(merchant_rows)} dim_currency={len(CURRENCIES)} "
        f"dim_date={len(dim_date_rows)} customers={len(all_customer_rows)} "
        f"promotions={len(all_promo_rows)} orders={len(all_order_rows)} "
        f"order_items={len(all_item_rows)}"
    )
    out.insert(1, summary)

    sys.stdout.write("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
