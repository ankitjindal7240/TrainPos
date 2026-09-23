# TrainPOS Demo Vendor email

Configure the platform-controlled secondary sender as a tenant-owned Vendor in
Django Admin. The vendor must have `parser_type` set to `DEMO`, `is_demo` set
to true, and a unique sender email for that restaurant.

Send every demonstration as a new Gmail compose message with a unique order
number. Use plain text only; no HTML is required.

Subject:

```text
New Demo Order #DEMO-20260830-001
```

Body:

```text
ORDER: DEMO-20260830-001
CUSTOMER: Demo Passenger
PHONE: 9000000000
TRAIN: 12963
TRAIN NAME: MEWAR EXPRESS
DELIVERY TIME: 2026-08-30 19:30
COACH: B2
SEAT: 36
PAYMENT: COD

ITEM: Demo Veg Thali | 1 | 250.00
ITEM: Demo Water Bottle | 2 | 20.00

GST: 14.50
SUBTOTAL: 304.50
TOTAL: 304.50
AMOUNT TO COLLECT: 304.50
REMARKS: TrainPOS demonstration order
```

For a prepaid demo, replace the payment lines with:

```text
PAYMENT: PREPAID
AMOUNT TO COLLECT: 0.00
```

Only explicitly configured tenant Demo Vendors accept this sender. Demo orders
follow the normal Gmail-to-order flow, appear on the operational dashboard, and
remain excluded from business reports.
