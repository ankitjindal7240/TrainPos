import re
import logging
from decimal import Decimal, InvalidOperation
from html import unescape
from html.parser import HTMLParser


logger = logging.getLogger(__name__)


class _EmailHtmlParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text_parts = []
        self.rows = []
        self.current_row = None
        self.current_cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.current_row = []
        elif tag in {"td", "th"} and self.current_row is not None:
            self.current_cell = []
        elif tag == "br":
            self.text_parts.append(" ")
            if self.current_cell is not None:
                self.current_cell.append(" ")

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.current_cell is not None:
            self.current_row.append(" ".join("".join(self.current_cell).split()))
            self.current_cell = None
        elif tag == "tr" and self.current_row is not None:
            self.rows.append(self.current_row)
            self.current_row = None

    def handle_data(self, data):
        self.text_parts.append(data)
        if self.current_cell is not None:
            self.current_cell.append(data)


def _find(pattern, text, group=1):
    match = re.search(pattern, text, flags=re.IGNORECASE)
    return match.group(group).strip() if match else None


def _amount(value):
    if not value:
        return None

    cleaned_value = re.sub(r"[^0-9.-]", "", value)
    try:
        return Decimal(cleaned_value)
    except (InvalidOperation, ValueError):
        return None


def _find_amount(label, text):
    return _amount(
        _find(rf"{re.escape(label)}\s*:?\s*(?:Rs\.?)?\s*([0-9]+(?:\.[0-9]+)?)", text)
    )


def _extract_items(rows):
    items = []

    for row in rows:
        if len(row) != 4:
            continue

        item_name, price, quantity, amount = row
        if not item_name or not re.fullmatch(r"Rs\.?\s*[0-9]+(?:\.[0-9]+)?", price, re.I):
            continue
        if not quantity.isdigit() or not re.fullmatch(r"Rs\.?\s*[0-9]+(?:\.[0-9]+)?", amount, re.I):
            continue

        items.append({"item_name": item_name, "quantity": int(quantity)})

    return items


def parse_railrestro_email(body):
    """Return the V1 receipt fields from one saved RailRestro email body."""
    parser = _EmailHtmlParser()
    parser.feed(body)
    parser.close()
    text = " ".join("".join(parser.text_parts).split())

    delivery_date = _find(r"Delivery Time:\s*(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})", text)
    delivery_time = _find(r"Delivery Time:\s*\d{4}-\d{2}-\d{2}\s+(\d{2}:\d{2}:\d{2})", text)
    train_number = _find(r"TRAIN:\s*([A-Za-z0-9]+)\s*/", text)
    coach = _find(r"Coact/Seat:\s*([A-Za-z0-9]+)\s*-", text)
    berth = _find(r"Coact/Seat:\s*[A-Za-z0-9]+\s*-\s*([A-Za-z0-9]+)", text)
    prepaid_raw = _find(r"Prepaid\s*:?\s*(?:Rs\.?)?\s*([0-9]+(?:\.[0-9]+)?)", text)
    advance = _amount(prepaid_raw)
    amount_to_collect_raw = _find(
        r"\(Amount to collect\)\s*(?:Rs\.?)?\s*([0-9]+(?:\.[0-9]+)?)", text
    )
    amount_to_collect = _amount(amount_to_collect_raw)
    paid_total_raw = _find(r"Paid\s*Total\s*:?\s*(?:Rs\.?)?\s*([0-9]+(?:\.[0-9]+)?)", text)
    paid_total = _amount(paid_total_raw)
    final_total = _find_amount("Final Total", text)
    payable_total = _find_amount("Payable Total", text)
    subtotal = _find_amount("Subtotal", text)

    if advance is not None and advance > 0:
        payment_mode = "PRE_PAID"
    elif amount_to_collect is not None and amount_to_collect > 0:
        payment_mode = "CASH_ON_DELIVERY"
    elif paid_total is not None and paid_total > 0 and amount_to_collect == Decimal("0"):
        payment_mode = "PRE_PAID"
    else:
        payment_mode = None
    logger.debug(
        "RailRestro payment normalization raw=%r normalized=%r",
        {
            "prepaid": prepaid_raw,
            "paid_total": paid_total_raw,
            "amount_to_collect": amount_to_collect_raw,
        },
        payment_mode,
    )

    if payment_mode == "PRE_PAID":
        # RailRestro's prepaid confirmation can use Paid Total / Prepaid rather
        # than the COD-oriented Final Total / Payable Total labels.
        total = paid_total or advance or final_total or payable_total or subtotal
    else:
        # Preserve COD behavior: its collection amount is a valid final sale
        # value only after the explicit final/payable totals have been checked.
        total = final_total or payable_total or amount_to_collect or paid_total or subtotal

    return {
        "order_number": _find(r"ORDER\s*#:\s*([A-Za-z0-9-]+)", text),
        "customer_name": _find(r"Customer:\s*(.*?)\s+M\.\s*", text),
        "customer_phone": _find(r"\bM\.\s*([0-9]+)", text),
        "train_number": train_number,
        "coach": coach,
        "berth": berth,
        "order_date": f"{delivery_date} {delivery_time}" if delivery_date and delivery_time else None,
        "train_journey_date": None,
        "payment_mode": payment_mode,
        "advance": advance,
        "subtotal": subtotal,
        "gst": _find_amount("GST", text),
        "tax": _find_amount("Tax", text),
        "discount": _find_amount("Discount", text),
        "total": total,
        "amount_to_collect": amount_to_collect,
        "remarks": _find(r"Remarks?\s*:\s*(.*?)(?=\s+(?:Best Regards|$))", text),
        "order_items": _extract_items(parser.rows),
    }
