"""Demo data so a new user can explore every screen."""

from __future__ import annotations

import random
from datetime import date, timedelta

from . import accounting as acc
from . import catalog, expenses, parties, sales
from . import settings as st
from .accounting import Line
from .catalog import Product
from .db import Database
from .parties import Party

# (name, category, price, cost, stock, reorder) in major currency units.
PRODUCTS = [
    ("Espresso", "Coffee", 3.00, 0.60, 0, 0),
    ("Americano", "Coffee", 3.50, 0.70, 0, 0),
    ("Cappuccino", "Coffee", 4.25, 0.95, 0, 0),
    ("Latte", "Coffee", 4.50, 1.05, 0, 0),
    ("Green Tea", "Tea", 3.00, 0.40, 0, 0),
    ("Chai Latte", "Tea", 4.75, 1.10, 0, 0),
    ("Croissant", "Bakery", 3.25, 1.20, 40, 10),
    ("Blueberry Muffin", "Bakery", 3.50, 1.30, 36, 10),
    ("Bagel", "Bakery", 2.75, 0.90, 30, 8),
    ("Club Sandwich", "Food", 7.95, 3.40, 20, 5),
    ("Garden Salad", "Food", 8.50, 3.10, 15, 5),
    ("Bottled Water", "Drinks", 1.50, 0.45, 60, 12),
    ("Orange Juice", "Drinks", 3.95, 1.60, 24, 6),
    ("Coffee Beans 250g", "Retail", 12.00, 6.50, 12, 4),
    ("Travel Mug", "Retail", 15.00, 7.00, 3, 4),
]


def load_sample_data(db: Database, days: int = 21, seed: int = 7) -> None:
    fmt = st.money_format(db)
    # Rough scaling so zero-decimal currencies (e.g. IDR) get realistic prices.
    scale = 10**fmt.decimals * (1000 if fmt.decimals == 0 else 1)

    def money(v: float) -> int:
        return int(round(v * scale))

    rng = random.Random(seed)
    today = date.today()
    start = today - timedelta(days=days)
    with db.transaction():
        acc.post(db, "Owner capital contribution", [
            Line(acc.account_id(db, "bank"), debit=money(5000)),
            Line(acc.account_id(db, "equity"), credit=money(5000)),
        ], source="manual", date=start.isoformat())

        ids = []
        for name, cat, price, cost, stock, reorder in PRODUCTS:
            ids.append(catalog.create_product(db, Product(
                sku=catalog.next_sku(db), name=name, category=cat, price=money(price),
                cost=money(cost), track_stock=stock > 0 or cat not in ("Coffee", "Tea"),
                reorder_level=reorder, taxable=cat != "Drinks" or name != "Bottled Water",
            ), opening_stock=stock, date=start.isoformat()))

        bakery = parties.save_party(db, "suppliers", Party(name="Sunrise Bakery Co.", phone="555-0101"))
        parties.save_party(db, "suppliers", Party(name="Metro Beverage Supply", phone="555-0133"))
        parties.save_party(db, "customers", Party(name="Walk-in regular: Sam", phone="555-0199"))
        office = parties.save_party(db, "customers", Party(
            name="Northside Office Ltd", email="accounts@example.com", notes="Monthly invoice"))

        catalog.record_purchase(db, [
            catalog.PurchaseLine(ids[6], 30, money(1.15)),
            catalog.PurchaseLine(ids[7], 30, money(1.25)),
        ], acc.account_id(db, "ap"), supplier_id=bakery, reference="INV-2041",
            date=(start + timedelta(days=3)).isoformat())

        rent = db.scalar("SELECT id FROM accounts WHERE code = '6000'")
        utilities = db.scalar("SELECT id FROM accounts WHERE code = '6100'")
        expenses.record_expense(db, rent, acc.account_id(db, "bank"), money(1200),
                                payee="City Properties", memo="Monthly rent",
                                date=(start + timedelta(days=1)).isoformat())
        expenses.record_expense(db, utilities, acc.account_id(db, "bank"), money(180),
                                payee="Power & Water", date=(start + timedelta(days=10)).isoformat())

        for offset in range(days + 1):
            day = (start + timedelta(days=offset)).isoformat()
            for _ in range(rng.randint(4, 10)):
                cart = sales.Cart()
                for pid in rng.sample(ids, rng.randint(1, 3)):
                    p = catalog.get_product(db, pid)
                    if p.track_stock and p.stock < 2:
                        continue
                    cart.add(p, rng.randint(1, 2))
                if not cart.lines:
                    continue
                total = sales.price_cart(cart, st.get_int(db, "tax_rate_bps")).total
                method = rng.choice(["cash", "cash", "card", "card", "card"])
                if rng.random() < 0.08:
                    cart.customer_id = office
                    method = "account"
                sales.checkout(db, cart, {method: total}, date=day)
