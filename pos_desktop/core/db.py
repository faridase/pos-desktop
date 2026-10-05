"""SQLite storage with versioned migrations.

Backward compatibility: the schema version lives in `PRAGMA user_version`.
Every released migration in MIGRATIONS is append-only: never edit one that
has shipped. Add a new entry instead, so older databases upgrade in place.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

SCHEMA_V1 = """
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE accounts (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    type TEXT NOT NULL
        CHECK (type IN ('asset', 'liability', 'equity', 'income', 'expense')),
    system_key TEXT UNIQUE,
    is_active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE journal_entries (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,
    memo TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,
    source_id INTEGER,
    reversed_by INTEGER REFERENCES journal_entries(id),
    created_at TEXT NOT NULL
);
CREATE INDEX idx_entries_date ON journal_entries(date);

CREATE TABLE journal_lines (
    id INTEGER PRIMARY KEY,
    entry_id INTEGER NOT NULL REFERENCES journal_entries(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    debit INTEGER NOT NULL DEFAULT 0 CHECK (debit >= 0),
    credit INTEGER NOT NULL DEFAULT 0 CHECK (credit >= 0),
    memo TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_lines_entry ON journal_lines(entry_id);
CREATE INDEX idx_lines_account ON journal_lines(account_id);

CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    phone TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE suppliers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    phone TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE products (
    id INTEGER PRIMARY KEY,
    sku TEXT NOT NULL UNIQUE,
    barcode TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'General',
    price INTEGER NOT NULL DEFAULT 0,
    cost INTEGER NOT NULL DEFAULT 0,
    stock INTEGER NOT NULL DEFAULT 0,
    track_stock INTEGER NOT NULL DEFAULT 1,
    reorder_level INTEGER NOT NULL DEFAULT 0,
    taxable INTEGER NOT NULL DEFAULT 1,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_products_barcode ON products(barcode);

CREATE TABLE stock_movements (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    created_at TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    unit_cost INTEGER NOT NULL,
    reason TEXT NOT NULL,
    reference TEXT NOT NULL DEFAULT '',
    journal_entry_id INTEGER REFERENCES journal_entries(id)
);
CREATE INDEX idx_movements_product ON stock_movements(product_id);

CREATE TABLE purchases (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,
    created_at TEXT NOT NULL,
    supplier_id INTEGER REFERENCES suppliers(id),
    reference TEXT NOT NULL DEFAULT '',
    total INTEGER NOT NULL,
    pay_account_id INTEGER NOT NULL REFERENCES accounts(id),
    journal_entry_id INTEGER REFERENCES journal_entries(id)
);

CREATE TABLE purchase_items (
    id INTEGER PRIMARY KEY,
    purchase_id INTEGER NOT NULL REFERENCES purchases(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL,
    unit_cost INTEGER NOT NULL
);

CREATE TABLE sales (
    id INTEGER PRIMARY KEY,
    receipt_no INTEGER NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    date TEXT NOT NULL,
    customer_id INTEGER REFERENCES customers(id),
    subtotal INTEGER NOT NULL,
    discount INTEGER NOT NULL,
    tax INTEGER NOT NULL,
    total INTEGER NOT NULL,
    tendered INTEGER NOT NULL,
    change_given INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'completed',
    note TEXT NOT NULL DEFAULT '',
    journal_entry_id INTEGER REFERENCES journal_entries(id)
);
CREATE INDEX idx_sales_date ON sales(date);

CREATE TABLE sale_items (
    id INTEGER PRIMARY KEY,
    sale_id INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
    product_id INTEGER REFERENCES products(id),
    sku TEXT NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    quantity INTEGER NOT NULL,
    unit_price INTEGER NOT NULL,
    unit_cost INTEGER NOT NULL,
    discount INTEGER NOT NULL,
    net INTEGER NOT NULL,
    tax INTEGER NOT NULL,
    track_stock INTEGER NOT NULL
);

CREATE TABLE sale_payments (
    id INTEGER PRIMARY KEY,
    sale_id INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
    method TEXT NOT NULL,
    amount INTEGER NOT NULL
);

CREATE TABLE refunds (
    id INTEGER PRIMARY KEY,
    sale_id INTEGER NOT NULL REFERENCES sales(id),
    created_at TEXT NOT NULL,
    date TEXT NOT NULL,
    method TEXT NOT NULL,
    net INTEGER NOT NULL,
    tax INTEGER NOT NULL,
    restock INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    journal_entry_id INTEGER REFERENCES journal_entries(id)
);

CREATE TABLE refund_items (
    id INTEGER PRIMARY KEY,
    refund_id INTEGER NOT NULL REFERENCES refunds(id) ON DELETE CASCADE,
    sale_item_id INTEGER NOT NULL REFERENCES sale_items(id),
    quantity INTEGER NOT NULL,
    net INTEGER NOT NULL,
    tax INTEGER NOT NULL
);

CREATE TABLE customer_payments (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    date TEXT NOT NULL,
    created_at TEXT NOT NULL,
    amount INTEGER NOT NULL,
    method TEXT NOT NULL,
    memo TEXT NOT NULL DEFAULT '',
    journal_entry_id INTEGER REFERENCES journal_entries(id)
);

CREATE TABLE supplier_payments (
    id INTEGER PRIMARY KEY,
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
    date TEXT NOT NULL,
    created_at TEXT NOT NULL,
    amount INTEGER NOT NULL,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    memo TEXT NOT NULL DEFAULT '',
    journal_entry_id INTEGER REFERENCES journal_entries(id)
);

CREATE TABLE expenses (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,
    created_at TEXT NOT NULL,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    paid_from_account_id INTEGER NOT NULL REFERENCES accounts(id),
    supplier_id INTEGER REFERENCES suppliers(id),
    payee TEXT NOT NULL DEFAULT '',
    amount INTEGER NOT NULL,
    memo TEXT NOT NULL DEFAULT '',
    voided INTEGER NOT NULL DEFAULT 0,
    journal_entry_id INTEGER REFERENCES journal_entries(id)
);

CREATE TABLE held_carts (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    data TEXT NOT NULL
);

CREATE TABLE shifts (
    id INTEGER PRIMARY KEY,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    opening_float INTEGER NOT NULL,
    expected_cash INTEGER,
    counted_cash INTEGER,
    difference INTEGER,
    notes TEXT NOT NULL DEFAULT '',
    journal_entry_id INTEGER REFERENCES journal_entries(id)
);

INSERT INTO accounts (code, name, type, system_key) VALUES
    ('1000', 'Cash on Hand', 'asset', 'cash'),
    ('1010', 'Bank Account', 'asset', 'bank'),
    ('1020', 'Card Payments Clearing', 'asset', 'card'),
    ('1100', 'Accounts Receivable', 'asset', 'ar'),
    ('1200', 'Inventory', 'asset', 'inventory'),
    ('2000', 'Accounts Payable', 'liability', 'ap'),
    ('2100', 'Sales Tax Payable', 'liability', 'tax'),
    ('3000', 'Owner''s Equity', 'equity', 'equity'),
    ('3100', 'Opening Balance Equity', 'equity', 'opening'),
    ('3200', 'Owner''s Drawings', 'equity', NULL),
    ('4000', 'Sales Revenue', 'income', 'sales'),
    ('4100', 'Sales Returns', 'income', 'returns'),
    ('4900', 'Other Income', 'income', NULL),
    ('5000', 'Cost of Goods Sold', 'expense', 'cogs'),
    ('5100', 'Inventory Shrinkage', 'expense', 'shrinkage'),
    ('5200', 'Cash Over/Short', 'expense', 'over_short'),
    ('6000', 'Rent', 'expense', NULL),
    ('6100', 'Utilities', 'expense', NULL),
    ('6200', 'Wages & Salaries', 'expense', NULL),
    ('6300', 'Supplies', 'expense', NULL),
    ('6400', 'Marketing', 'expense', NULL),
    ('6500', 'Bank & Card Fees', 'expense', NULL),
    ('6900', 'Other Expenses', 'expense', NULL);
"""

# v2: staff accounts, roles and audit trail. Existing rows keep NULL user
# columns ("before staff logins").
SCHEMA_V2 = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    full_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'manager', 'cashier')),
    password_hash TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    last_login_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    user_id INTEGER REFERENCES users(id),
    action TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_audit_created ON audit_log(created_at);

ALTER TABLE sales ADD COLUMN user_id INTEGER REFERENCES users(id);
ALTER TABLE sales ADD COLUMN approved_by INTEGER REFERENCES users(id);
ALTER TABLE refunds ADD COLUMN user_id INTEGER REFERENCES users(id);
ALTER TABLE refunds ADD COLUMN approved_by INTEGER REFERENCES users(id);
ALTER TABLE journal_entries ADD COLUMN user_id INTEGER REFERENCES users(id);
ALTER TABLE stock_movements ADD COLUMN user_id INTEGER REFERENCES users(id);
ALTER TABLE purchases ADD COLUMN user_id INTEGER REFERENCES users(id);
ALTER TABLE expenses ADD COLUMN user_id INTEGER REFERENCES users(id);
ALTER TABLE shifts ADD COLUMN opened_by INTEGER REFERENCES users(id);
ALTER TABLE shifts ADD COLUMN closed_by INTEGER REFERENCES users(id);
"""

# Append-only. Index i upgrades the database from version i to i + 1.
# v3: branches (a head branch is created and existing records assigned to it),
# and delivery orders received through the web app's order API. The desktop
# app doesn't use these yet; it leaves branch_id NULL, which means the head branch.
SCHEMA_V3 = """
CREATE TABLE branches (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name TEXT NOT NULL,
    address TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    is_head INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    api_key_hash TEXT UNIQUE,
    api_key_hint TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
INSERT INTO branches (code, name, is_head, created_at)
    VALUES ('HQ', 'Head Branch', 1, datetime('now', 'localtime'));

ALTER TABLE users ADD COLUMN branch_id INTEGER REFERENCES branches(id);
ALTER TABLE users ADD COLUMN handles_orders INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sales ADD COLUMN branch_id INTEGER REFERENCES branches(id);
ALTER TABLE shifts ADD COLUMN branch_id INTEGER REFERENCES branches(id);
ALTER TABLE held_carts ADD COLUMN branch_id INTEGER REFERENCES branches(id);
UPDATE users SET branch_id = (SELECT id FROM branches WHERE is_head = 1);
UPDATE sales SET branch_id = (SELECT id FROM branches WHERE is_head = 1);
UPDATE shifts SET branch_id = (SELECT id FROM branches WHERE is_head = 1);
UPDATE held_carts SET branch_id = (SELECT id FROM branches WHERE is_head = 1);

CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    branch_id INTEGER NOT NULL REFERENCES branches(id),
    reference TEXT,
    customer_name TEXT NOT NULL,
    address TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    scheduled_date TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    ordered_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'api',
    status TEXT NOT NULL DEFAULT 'new'
        CHECK (status IN ('new', 'picked_up', 'completed', 'cancelled')),
    picked_by INTEGER REFERENCES users(id),
    picked_at TEXT,
    closed_by INTEGER REFERENCES users(id),
    closed_at TEXT,
    status_note TEXT NOT NULL DEFAULT '',
    created_by INTEGER REFERENCES users(id)
);
CREATE UNIQUE INDEX idx_orders_reference ON orders(branch_id, reference)
    WHERE reference IS NOT NULL;
CREATE INDEX idx_orders_status ON orders(status, scheduled_date);

CREATE TABLE order_items (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id INTEGER REFERENCES products(id),
    sku TEXT NOT NULL,
    name TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price INTEGER NOT NULL
);
CREATE INDEX idx_order_items_order ON order_items(order_id);
"""

# v4: an order is either delivered to the customer or picked up by them at the
# branch. Existing orders are deliveries.
SCHEMA_V4 = """
ALTER TABLE orders ADD COLUMN fulfillment TEXT NOT NULL DEFAULT 'delivery'
    CHECK (fulfillment IN ('delivery', 'pickup'));
"""

MIGRATIONS: list[str] = [SCHEMA_V1, SCHEMA_V2, SCHEMA_V3, SCHEMA_V4]
SCHEMA_VERSION = len(MIGRATIONS)


class NewerDatabaseError(RuntimeError):
    """The file was written by a newer version of the app."""


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_text() -> str:
    return datetime.now().strftime("%Y-%m-%d")


class Database:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self._depth = 0
        # The signed-in staff member; stamped on records as they're created.
        self.user_id: int | None = None
        self.migrate()

    # ---- migrations --------------------------------------------------------

    @property
    def version(self) -> int:
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def migrate(self) -> None:
        version = self.version
        if version > SCHEMA_VERSION:
            raise NewerDatabaseError(
                f"This data file uses schema v{version}, but this app only "
                f"understands up to v{SCHEMA_VERSION}. Please update the app."
            )
        for index in range(version, SCHEMA_VERSION):
            # executescript commits implicitly, so wrap the whole step.
            self.conn.executescript(
                f"BEGIN;\n{MIGRATIONS[index]}\nPRAGMA user_version = {index + 1};\nCOMMIT;"
            )

    # ---- transactions ------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Atomic block. Nested calls use savepoints."""
        name = f"sp{self._depth}"
        if self._depth == 0:
            self.conn.execute("BEGIN IMMEDIATE")
        else:
            self.conn.execute(f"SAVEPOINT {name}")
        self._depth += 1
        try:
            yield
        except BaseException:
            self._depth -= 1
            if self._depth == 0:
                self.conn.execute("ROLLBACK")
            else:
                self.conn.execute(f"ROLLBACK TO {name}")
                self.conn.execute(f"RELEASE {name}")
            raise
        else:
            self._depth -= 1
            if self._depth == 0:
                self.conn.execute("COMMIT")
            else:
                self.conn.execute(f"RELEASE {name}")

    # ---- queries -----------------------------------------------------------

    def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def insert(self, sql: str, params: Any = ()) -> int:
        return int(self.conn.execute(sql, params).lastrowid)

    def all(self, sql: str, params: Any = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: Any = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: Any = (), default: Any = None) -> Any:
        row = self.conn.execute(sql, params).fetchone()
        return default if row is None or row[0] is None else row[0]

    # ---- maintenance -------------------------------------------------------

    def backup_to(self, dest: str | Path) -> None:
        target = sqlite3.connect(str(dest))
        try:
            self.conn.backup(target)
        finally:
            target.close()

    def close(self) -> None:
        self.conn.close()
