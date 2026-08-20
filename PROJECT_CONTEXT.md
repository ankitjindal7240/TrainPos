# TrainPOS — Project Context

## Project Overview
TrainPOS is a Django web application for managing railway food orders received by a restaurant. Orders currently arrive through Gmail from multiple railway food vendors. TrainPOS will read the emails, identify the vendor, parse vendor-specific formats, normalize the data, store it in a database, and display it on a restaurant dashboard.

This is intended to become a real working restaurant tool and eventually a scalable product.

## Current Goal: V1
V1 must:
1. Fetch/read railway order emails from Gmail.
2. Identify the vendor.
3. Parse vendor-specific email formats.
4. Normalize orders into a common structure.
5. Store orders using Django ORM and PostgreSQL.
6. Show orders on a web dashboard.
7. Show complete order details.
8. Support order status changes.
9. Generate/print KOT.
10. Generate/print bill.
11. Be practical for one-computer restaurant use.

Do NOT implement V2-V5 unless explicitly requested.

## Roadmap
### V1 — Train Order Management
- Gmail order ingestion
- Multiple vendor parsers
- Normalized orders
- Dashboard
- Order details/status
- KOT and bill printing

### V2 — Live Train Status
Live/current train information, estimated arrival timing, preparation deadline alerts, and an alert if an order is not ready before the configured threshold.

### V3 — Monthly Business Reports
Vendor-wise monthly total orders, total order value, COD orders, prepaid/online orders, and settlement summaries. Commission calculation is intentionally deferred.

### V4 — AI Forecasting
Forecast order volume, busy periods, vendor trends, and preparation requirements using historical data.

### V5 — Scalable Product
Multi-restaurant SaaS, multi-user support, multi-tenancy, cloud deployment, production database, authentication/authorization, monitoring, reporting, and scalable infrastructure.

## Current Tech Stack
- Python 3.11
- Django
- Django ORM
- PostgreSQL 14.20 (Homebrew)
- psycopg 3.3.4
- Django templates + HTML/CSS/JavaScript initially
- Gmail as email source

Do NOT introduce React unless explicitly decided later.

## Database
Database: `trainpos`
Host: `localhost`
Port: `5432`
Local PostgreSQL user: `ankitjindal`

Django migrations have already run successfully. PostgreSQL contains the standard Django tables.

## Current Django Structure
```text
TrainPOS/
├── manage.py
├── TrainPOS/
├── Orders/
└── Home/
```

`Orders` is the main V1 application.

## Architecture Principle
Different vendors have different email formats, but TrainPOS must use one normalized internal Order model.

Do NOT create separate order systems such as `HomeBytesOrder`, `RajBhogOrder`, and `RailRestroOrder`.

Preferred architecture:
```text
Gmail
  ↓
Email
  ↓
Vendor Detection
  ↓
Vendor-specific Parser
  ├── HomeBytes
  ├── RajBhog
  └── RailRestro
  ↓
Normalized Order
  ↓
Django ORM
  ↓
PostgreSQL
  ↓
TrainPOS Dashboard
```

Vendor-specific parsing should be isolated so another vendor can be added later without redesigning the core order system.

## Common Order Data
### Order
- vendor
- order number
- booking date/time
- delivery date/time
- customer
- train
- PNR
- coach
- berth/seat
- delivery station
- payment mode
- subtotal
- GST
- discount
- delivery charge
- total
- status
- created_at
- updated_at

### Customer
- name
- phone
- email

### Train
- train number
- train name

### OrderItem
- order
- item name
- description
- quantity
- price
- GST
- amount

## Order Status
Initial statuses:
```text
NEW
ACCEPTED
PREPARING
READY
DELIVERED
CANCELLED
```

## Payment Modes
Normalize vendor terminology into:
```text
PRE_PAID
CASH_ON_DELIVERY
```

Examples:
- HomeBytes: `PRE_PAID`
- RajBhog: `CASH_ON_DELIVERY`
- RailRestro: uses payable/amount-to-collect wording and may map to COD where appropriate.

## Real Vendor Email Examples
### HomeBytes
```text
Booking Date: 11 Aug 2026, 21:04
Delivery Date: 11 Aug 2026, 22:13
Customer Name : Radha Krishna
Customer Contact : 9462623238
Invoice HB001256181 / 2474581989
Payment: PRE_PAID
Coach / Berth: B2 / 36
Train: 12963 / MEWAR EXPRESS
Delivery Station: GGC / GANGAPUR CITY JUNCTION
1 Veg Cheese Pizza 1 300.00 15.00 300.00
Subtotal: 300.00
GST (5%): 15.00
Discount: 0.00
Delivery: 0
Total: 315.00
```

### RajBhog Khana
```text
Booking Date: 11 Aug 2026, 20:13
Delivery Date: 11 Aug 2026, 20:55
Customer Name : Krishna singh
Customer Contact : 8169907916
Invoice RBK001754505 / 2474561605
Payment: CASH_ON_DELIVERY
Coach / Berth: M2 / 72
Train: 19019 / BDTS HW EXP
Delivery Station: GGC / GANGAPUR CITY JUNCTION
1 PANEER COMBO - Paneer 150 gram, 3 butter roti - 1 - 160.00 - 7.60 - 160.00
2 CHOLE RICE COMBO - Chole 150 gram, rice 150 gram - 1 - 160.00 - 7.60 - 160.00
Subtotal: 320.00
GST (5%): 15.20
Discount: 16.00
Delivery: 0
Total: 319.00
```

### RailRestro
```text
ORDER #: 5721373
Customer: yashvant sinh ghariya
M. 8128899384
TRAIN: 19019 / BDTS HW EXP
Delivery Time: 2026-08-11 20:50:00
PNR No.: 8151776577
Coact/Seat: M1-5
Pizza Combo - Rs. 299 - Qty 1 - Rs. 299
Stuffed Garlic Bread - Rs. 221 - Qty 1 - Rs. 221
Total: Rs. 520
GST: Rs. 26
Subtotal: Rs. 546
Extra Charges: Rs. 0
Cashback: Rs. 0.00
Payable Total: Rs. 546
(Amount to collect) Rs. 546
```

These are representative examples. Vendor fields may have different names/order/format, so parsers must be resilient.

## Proposed Django Models
Initial conceptual models:
- Vendor
- Customer
- Train
- Order
- OrderItem

Do not add unnecessary models/fields until requirements justify them.

### Vendor
Possible fields: name, email/domain or identifier, created_at.

### Customer
Possible fields: name, phone, email.

### Train
Possible fields: train_number, train_name.

### Order
Possible fields: vendor, order_number, customer, train, pnr, coach, berth, delivery_station, booking_date, delivery_date, payment_mode, subtotal, gst, discount, delivery_charge, total, status, created_at, updated_at.

### OrderItem
Possible fields: order, item_name, description, quantity, price, gst, amount.

## Development Philosophy
This is a learning project as well as a real application. The developer is revising Django while building TrainPOS and must understand the implementation rather than blindly accepting generated code.

Rules:
1. Prefer simple Django solutions.
2. Explain important architectural decisions briefly.
3. Build incrementally.
4. Do not generate the entire application at once.
5. Do not hide complexity behind unnecessary abstractions.
6. Prefer Django ORM over raw SQL where practical.
7. Use migrations for schema changes.
8. Keep vendor-specific parsing isolated.
9. Avoid premature optimization.
10. Avoid unnecessary packages.
11. Keep V1 focused.
12. Do not implement V2-V5 unless explicitly requested.

## Codex Working Rules
Before changing code:
- Inspect the existing project structure.
- Inspect relevant existing files.
- Understand the current implementation.
- Do not overwrite existing work unnecessarily.

When implementing a feature:
1. Explain briefly what will change.
2. Make the smallest reasonable change.
3. Run appropriate checks/tests.
4. Report what changed.
5. Explain important Django concepts involved.

Database changes should use:
```bash
python manage.py makemigrations
python manage.py migrate
```

Do not manually create application tables in PostgreSQL.

Never commit Gmail credentials, OAuth tokens, API keys, passwords, or other secrets. Use environment variables for credentials when they are introduced.

## Current Project Stage
Database setup is COMPLETE.

Environment:
```text
Python 3.11
Django
psycopg 3.3.4
PostgreSQL 14.20
Database: trainpos
```

The next step is implementing the domain models in `Orders`.

## Immediate Build Order
### Phase 1 — Data Model
1. Register Orders app.
2. Create Vendor model.
3. Create Customer model.
4. Create Train model.
5. Create Order model.
6. Create OrderItem model.
7. Create migrations.
8. Test models through Django shell/admin.

### Phase 2 — Dashboard
1. Order list page.
2. Important order information.
3. Filters/status indicators.
4. Order detail page.

### Phase 3 — Vendor Email Parsing
1. Email ingestion service.
2. Vendor detection.
3. HomeBytes parser.
4. RajBhog parser.
5. RailRestro parser.
6. Normalize data.
7. Save with Django ORM.
8. Prevent duplicate orders.

### Phase 4 — Restaurant Workflow
1. Accept order.
2. Preparing status.
3. Ready status.
4. Delivered status.
5. KOT generation/printing.
6. Bill generation/printing.

### Phase 5 — Testing/Polish
Test multiple vendors, malformed emails, duplicate emails, missing fields, multiple items, COD/prepaid, printing, and dashboard usability.

## V1 Non-Goals
Do NOT build yet:
- Live train tracking
- AI forecasting
- Monthly vendor accounting
- Commission calculation
- Multi-restaurant SaaS
- Complex notification infrastructure
- Kafka
- Celery unless a concrete V1 requirement appears
- Redis unless a concrete requirement appears
- React unless explicitly decided
- Microservices

## Success Criteria
V1 is successful when a restaurant employee can:
1. Open TrainPOS.
2. See incoming railway orders.
3. Clearly identify train, coach/berth, customer, and items.
4. Know payment mode.
5. Open complete order details.
6. Prepare the order.
7. Mark it ready.
8. Print KOT.
9. Print the bill.
10. Mark it delivered.

## Guiding Principle
Build TrainPOS like a real application, but learn while building it.

**Do not over-engineer V1.**

**Do not sacrifice understanding for speed.**

Use Codex as a coding assistant, not as a replacement for understanding Django concepts or architecture.
