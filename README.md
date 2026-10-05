# Mobile POS Desktop

A cross-platform desktop point-of-sale app with inventory and double-entry
accounting. It's built with Python and Qt (PySide6) and runs on Windows,
macOS and Linux. All data is kept in one local SQLite file.

## Features

**Register**
- Barcode/SKU scanning (USB scanners work as a keyboard), product search and category filter
- Line discounts (%), order discounts (amount or %), price override
- Split payments: cash (with change), card, bank transfer, and on account for customers
- Held (parked) orders, cash-drawer shifts with over/short, and receipts you can print or save as PDF

**Sales**
- History with search and receipt reprint
- Partial or full refunds, with optional restocking

**Inventory**
- Products with SKU, barcode, cost, reorder level, and taxable / track-stock flags
- Purchases from suppliers, paid now or on credit, using weighted-average cost
- Stock counts and adjustments, booked to shrinkage
- Movement log, low-stock alerts, CSV import and export

**Customers and suppliers**
- Account balances and statements
- Receiving payments from customers and paying suppliers

**Expenses**
- Paid from cash or bank, or recorded as bills on credit
- Voiding posts a reversing entry

**Accounting**
- Editable chart of accounts
- Journal with automatic postings from all of the above
- Manual journal entries, fund transfers, reversals, general ledger

**Reports**
- Sales by day, product, category or payment method, with margins
- Profit & Loss, Balance Sheet, Trial Balance, tax, inventory valuation
- Every report exports to CSV and can be printed or saved as PDF

**Dashboard**
- Today, this month and balances, a 14-day sales chart, top products, low stock and recent sales

**Branches**
- Add, edit, deactivate and delete branches. There's always one head branch, and the tills run as it for now.
- Staff belong to a branch. Sales, cash shifts and held orders are recorded against the running branch.
- Each branch can have an API key for the order API. Only a hash of the key is stored.

**Customer orders (delivery or pickup)**
- Staff with **Handles customer orders** see their branch's orders under **Orders**, with a count of new ones in the menu.
- Take an order, then mark it delivered or collected, release it, or cancel it with a reason. Owners/admins see every branch, and can enter, edit and delete orders.
- Online shops send orders to the web app's order API (see [`web/`](../web/README.md)). A desktop app can't receive requests from the internet, but both apps share the data file, so those orders appear here. The list refreshes every 30 seconds.

## Staff sign-in and roles

On first launch you create the **owner** account. The owner adds staff
under **Staff**.

| Role | Can do |
| --- | --- |
| Cashier | Sell, hold orders, open and close cash shifts, view sales and reprint receipts |
| Manager | Everything a cashier can, plus discounts and price changes, refunds, dashboard, products, inventory, customers, suppliers, expenses and reports |
| Owner / Admin | Everything, including accounting, settings, backup/restore/sample data, staff management, branches, and customer orders at every branch |

Any role can also be given **Handles customer orders**, per person, to work on their branch's orders.

- **Manager approval:** a cashier can still give a discount, change a
  price or process a refund if a manager enters their own username and
  password on the spot. Approval covers one order (discounts) or one refund.
  The approver is recorded on the sale or refund.
- **Who did what:** sales, refunds, shifts, stock movements, purchases,
  expenses and journal entries record the staff member. Receipts show
  "Served by". The Sales report can group by **Staff**.
- **Audit log** (Staff → Audit log) records:
  - sign-ins, failed sign-ins, sign-outs and screen locks
  - manager approvals
  - staff and password changes
  - backups, restores and settings changes
- **Passwords:** stored as salted PBKDF2-SHA256 hashes, never in plain text.
  - Minimum length: 4 characters (a PIN) for cashiers, 6 for managers and admins.
  - After 5 wrong attempts the account locks for 5 minutes. An admin can
    reset the password to unlock it sooner.
- **Locking:**
  - **Lock** (Ctrl+L) hides the app until someone signs back in.
  - **Switch user** (Ctrl+Shift+L) signs the current person out.
  - The screen also locks after 10 minutes without keyboard or mouse input.
    You can change this under Settings → Security.
- **Admin safeguards:** there must always be at least one active owner/admin,
  and nobody can deactivate their own account.

Staff sign-in controls what people can do *in the app*. It doesn't encrypt the
data file, so keep the computer's own user account and backups secure.

## Run from source

Requires Python 3.10 or newer.

```bash
cd desktop
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
pos-desktop                 # or: python -m pos_desktop
```

On first launch you create the owner account, and the app then offers to load sample data. Options:

- `--db PATH` uses a specific data file. You can also set the `POS_DB` environment variable.
- `--sample` loads sample data into a new file without asking.

By default the data file is stored in the per-user app data folder:
- **Windows:** `%APPDATA%\MobilePOS\Mobile POS Desktop\pos.db`
- **macOS:** `~/Library/Application Support/Mobile POS Desktop/pos.db`
- **Linux:** `~/.local/share/MobilePOS/Mobile POS Desktop/pos.db`

The exact path is shown under Settings → Data.

On Linux, Qt needs the usual system libraries. On Debian/Ubuntu, for example:
`sudo apt install libxcb-cursor0 libegl1`.

## Tests

```bash
pytest
```

There are two test files:
- `tests/test_core.py` covers money math, pricing, checkout, refunds, purchases, shifts and migrations. It checks that the trial balance and balance sheet balance, and that the reports agree with the ledger.
- `tests/test_auth.py` covers passwords, lockout, roles, manager approval, staff stamping and upgrading a v1 data file.
- `tests/test_ui.py` runs every screen headless (`QT_QPA_PLATFORM=offscreen`). It also completes a sale, a refund and a journal entry through the real widgets, and checks what each role can see.

## Build a standalone app

Build on each target OS with PyInstaller. Qt doesn't support cross-compiling.

**Windows:** run the build script in PowerShell:

```powershell
cd desktop
powershell -ExecutionPolicy Bypass -File build_windows.ps1
```

The script installs the dependencies, runs the tests and builds the app.
The app is `dist\MobilePOS\MobilePOS.exe`, and a zip to share is saved
next to it. To install on another PC, unzip it and run `MobilePOS.exe`.
Nothing else needs to be installed. The built app uses the same data folder
as running from source, so it opens the existing data.

**macOS / Linux:**

```bash
pip install pyinstaller pillow
pyinstaller --name MobilePOS --windowed --icon pos_desktop/resources/icon.png \
    --add-data "pos_desktop/resources:pos_desktop/resources" packaging/launcher.py
```

## How the accounting works

Money is stored as integers in the currency's minor unit, such as cents, so
there is no floating-point rounding. Every business event posts a balanced
journal entry. Reports are computed only from journal lines, so the books
always reconcile.

| Event | Debit | Credit |
| --- | --- | --- |
| Sale | Cash / Card / Bank / A/R, and COGS | Sales Revenue, Sales Tax Payable, and Inventory |
| Refund | Sales Returns, Tax Payable, and Inventory if restocked | Payment account, and COGS if restocked |
| Purchase | Inventory | Cash / Bank, or Accounts Payable |
| Expense | Expense account | Cash / Bank, or Accounts Payable |
| Customer payment | Cash / Card / Bank | Accounts Receivable |
| Supplier payment | Accounts Payable | Cash / Bank |
| Stock count loss / gain | Shrinkage / Inventory | Inventory / Shrinkage |
| Shift short / over | Cash Over/Short / Cash | Cash / Cash Over/Short |
| Opening stock | Inventory | Opening Balance Equity |

Common manual entries:
- **Owner investment:** debit Bank, credit Owner's Equity.
- **Tax remittance:** debit Sales Tax Payable, credit Bank.
- **Card settlement:** use Transfer funds, from Card Payments Clearing to Bank.

## Backward compatibility

- **Schema version:** the database schema version is stored in `PRAGMA user_version`. Migrations in `core/db.py` are append-only, so opening an older data file upgrades it in place. v3 adds branches and customer orders for the web app, and v4 marks each order as delivery or pickup ([`web/`](../web/README.md)); the desktop app keeps working and treats its records as belonging to the head branch. For example, v2 added staff accounts: a v1 file opens with its history intact, asks you to create the owner account, and older sales show as "(before staff logins)".
- **Newer data files:** a file written by a newer version of the app is refused with a clear message rather than risking damage.
- **Settings:** settings fall back to defaults when a key is missing. Held orders ignore unknown fields.
- **Restore:** the backup is validated and upgraded on a temporary copy first. The current data is kept as `pos.db.before-restore`.
- **Currency decimals:** these lock once money has been recorded, because changing them would reinterpret stored amounts.

## Layout

```
pos_desktop/
  core/        # no Qt imports; fully unit-tested
    db.py            schema + migrations + transactions
    money.py         formatting, parsing, rounding, allocation
    accounting.py    ledger, posting, statements
    catalog.py       products, purchases, stock, CSV
    sales.py         cart pricing, checkout, refunds, held orders
    parties.py       customers (A/R) and suppliers (A/P)
    auth.py          staff, roles, permissions, password hashing, audit log
    expenses.py, shifts.py, reports.py, receipts.py, settings.py, sample_data.py
  ui/          # PySide6 screens and dialogs
  app.py       # entry point
```
