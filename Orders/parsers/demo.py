"""Parser for the deliberately simple, compose-friendly TrainPOS demo format."""

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from Orders.models import Order


def _field(body, label):
    match = re.search(
        rf"^\s*{re.escape(label)}\s*:\s*(.*?)\s*$",
        body,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    return match.group(1).strip() if match else None


def _required_field(body, label):
    value = _field(body, label)
    if not value:
        raise ValueError(f"Demo email requires {label}.")
    return value


def _decimal(value, label):
    try:
        return Decimal(value.replace(",", "").strip())
    except (AttributeError, InvalidOperation) as error:
        raise ValueError(f"Demo email has an invalid {label} amount.") from error


def _payment_mode(value):
    normalized = re.sub(r"[\s_-]+", "", value).upper()
    if normalized in {"COD", "CASHONDELIVERY"}:
        return Order.PaymentMode.CASH_ON_DELIVERY
    if normalized in {"PREPAID", "ONLINE"}:
        return Order.PaymentMode.PRE_PAID
    raise ValueError("Demo email PAYMENT must be COD or PREPAID.")


def _order_date(value):
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError as error:
        raise ValueError(
            "Demo email DELIVERY TIME must use YYYY-MM-DD HH:MM."
        ) from error


def _items(body):
    items = []
    for line in body.splitlines():
        match = re.match(r"^\s*ITEM\s*:\s*(.*?)\s*$", line, flags=re.IGNORECASE)
        if not match:
            continue
        parts = [part.strip() for part in match.group(1).split("|")]
        if len(parts) != 3 or not all(parts):
            raise ValueError(
                "Demo email ITEM must use: ITEM: name | quantity | unit price."
            )
        item_name, quantity_value, price_value = parts
        try:
            quantity = int(quantity_value)
        except ValueError as error:
            raise ValueError("Demo email item quantity must be a whole number.") from error
        if quantity < 1:
            raise ValueError("Demo email item quantity must be at least 1.")
        price = _decimal(price_value, "item price")
        if price < 0:
            raise ValueError("Demo email item price cannot be negative.")
        items.append(
            {
                "item_name": item_name,
                "quantity": quantity,
                "price": price,
                "amount": price * quantity,
            }
        )
    if not items:
        raise ValueError("Demo email requires at least one ITEM line.")
    return items


def parse_demo_email(body):
    """Return normalized order data from a simple text email composed by staff."""
    if not isinstance(body, str) or not body.strip():
        raise ValueError("Demo email body is empty.")

    order_number = _required_field(body, "ORDER")
    customer_name = _required_field(body, "CUSTOMER")
    customer_phone = _required_field(body, "PHONE")
    train_number = _required_field(body, "TRAIN")
    order_date = _order_date(_required_field(body, "DELIVERY TIME"))
    coach = _required_field(body, "COACH")
    berth = _required_field(body, "SEAT")
    payment_mode = _payment_mode(_required_field(body, "PAYMENT"))
    subtotal = _decimal(_required_field(body, "SUBTOTAL"), "SUBTOTAL")
    gst = _decimal(_required_field(body, "GST"), "GST")
    total = _decimal(_required_field(body, "TOTAL"), "TOTAL")
    amount_to_collect = _decimal(
        _required_field(body, "AMOUNT TO COLLECT"), "AMOUNT TO COLLECT"
    )

    return {
        "order_number": order_number,
        "customer_name": customer_name,
        "customer_phone": customer_phone,
        "train_number": train_number,
        "train_name": _field(body, "TRAIN NAME"),
        "coach": coach,
        "berth": berth,
        "order_date": order_date,
        "train_journey_date": order_date[:10],
        "payment_mode": payment_mode,
        "advance": total if payment_mode == Order.PaymentMode.PRE_PAID else Decimal("0"),
        "subtotal": subtotal,
        "gst": gst,
        "tax": None,
        "discount": Decimal("0"),
        "total": total,
        "amount_to_collect": amount_to_collect,
        "remarks": _field(body, "REMARKS"),
        "order_items": _items(body),
    }
