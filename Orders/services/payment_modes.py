import re

from Orders.models import Order


def normalize_payment_mode(value):
    if not value:
        return None

    normalized_value = re.sub(r"[\s_-]+", "", value).upper()
    if normalized_value in {"COD", "CASHONDELIVERY"}:
        return Order.PaymentMode.CASH_ON_DELIVERY
    if normalized_value == "PREPAID":
        return Order.PaymentMode.PRE_PAID
    return None
