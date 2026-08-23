import re
import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser

from Orders.services.payment_modes import normalize_payment_mode


logger = logging.getLogger(__name__)


class _RailRecipeHtmlParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text_parts = []
        self.rows = []
        self.current_row = None
        self.current_cell = None
        self.ignored_tag = None

    def handle_starttag(self, tag, attrs):
        if tag in {"style", "script"}:
            self.ignored_tag = tag
            return
        if self.ignored_tag:
            return
        if tag == "tr":
            self.current_row = []
        elif tag in {"td", "th"} and self.current_row is not None:
            self.current_cell = []
        elif tag == "br":
            self.text_parts.append(" ")
            if self.current_cell is not None:
                self.current_cell.append("\n")

    def handle_endtag(self, tag):
        if tag == self.ignored_tag:
            self.ignored_tag = None
            return
        if self.ignored_tag:
            return
        if tag in {"td", "th"} and self.current_cell is not None:
            cell_lines = "".join(self.current_cell).splitlines()
            self.current_row.append(
                "\n".join(" ".join(line.split()) for line in cell_lines).strip()
            )
            self.current_cell = None
        elif tag == "tr" and self.current_row is not None:
            self.rows.append(self.current_row)
            self.current_row = None

    def handle_data(self, data):
        if self.ignored_tag:
            return
        self.text_parts.append(data)
        if self.current_cell is not None:
            self.current_cell.append(data)


def _find(pattern, text, group=1):
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if not match:
        return None
    return match.group(group).strip() or None


def _amount(value):
    if not value:
        return None

    try:
        return Decimal(re.sub(r"[^0-9.-]", "", value))
    except (InvalidOperation, ValueError):
        return None


def _find_amount(label, text):
    return _amount(
        _find(
            rf"(?<![A-Za-z]){re.escape(label)}(?![A-Za-z])\s*(?:₹|Rs\.?)?\s*([0-9]+(?:\.[0-9]+)?)",
            text,
        )
    )


def _extract_items(rows):
    items = []

    for row in rows:
        if len(row) != 4:
            continue

        item_name, price, quantity, amount = row
        if not item_name or not re.fullmatch(r"₹\s*[0-9]+(?:\.[0-9]+)?", price):
            continue
        quantity_match = re.fullmatch(r"x\s*([0-9]+)", quantity, flags=re.IGNORECASE)
        if not quantity_match or not re.fullmatch(r"₹\s*[0-9]+(?:\.[0-9]+)?", amount):
            continue

        items.append(
            {
                "item_name": item_name.split("\n", maxsplit=1)[0],
                "quantity": int(quantity_match.group(1)),
            }
        )

    return items


def _normalized_order_datetime(date_value, time_value):
    if not date_value or not time_value:
        return None
    return datetime.strptime(
        f"{date_value} {time_value}", "%b %d, %Y %H:%M"
    ).strftime("%Y-%m-%d %H:%M:%S")


def parse_railrecipe_email(body):
    """Return the V1 receipt fields from one saved RailRecipe email body."""
    parser = _RailRecipeHtmlParser()
    parser.feed(body)
    parser.close()
    text = " ".join("".join(parser.text_parts).split())

    payment_status = _find(
        r"PAYMENT\s+STATUS\s*:?\s*(CASH(?:[_\s]+ON[_\s]+DELIVERY)?|COD|PRE[_\s-]*PAID)",
        text,
    )
    total = _find_amount("Grand Total", text)
    payment_mode = normalize_payment_mode(payment_status)
    logger.debug(
        "RailRecipe payment normalization raw=%r normalized=%r",
        payment_status,
        payment_mode,
    )

    if payment_mode == "PRE_PAID":
        advance = total
        amount_to_collect = Decimal("0")
    elif payment_mode == "CASH_ON_DELIVERY":
        advance = Decimal("0")
        amount_to_collect = total
    else:
        payment_mode = None
        advance = None
        amount_to_collect = None

    order_date = _find(r"Order Date\s*([A-Za-z]{3}\s+\d{1,2},\s*\d{4})", text)
    order_time = _find(r"Delivery Time\s*\(ETA\).*?\s+(\d{1,2}:\d{2})\s+Journey Date", text)
    journey_date = _find(r"Journey Date\s*(\d{4}-\d{2}-\d{2})", text)

    return {
        "order_number": _find(r"Order No\.\s*([A-Za-z0-9-]+)", text),
        "customer_name": None,
        "customer_phone": _find(r"Mobile No\.\s*([0-9]+)", text),
        "train_number": _find(r"Train No\.\s*([A-Za-z0-9]+)", text),
        "coach": _find(r"Coach/Seat\s*([A-Za-z0-9]+)\s*/", text),
        "berth": _find(r"Coach/Seat\s*[A-Za-z0-9]+\s*/\s*([A-Za-z0-9]+)", text),
        "order_date": _normalized_order_datetime(order_date, order_time),
        "train_journey_date": journey_date,
        "payment_mode": payment_mode,
        "advance": advance,
        "gst": _find_amount("GST", text),
        "tax": None,
        "discount": _find_amount("Discount", text),
        "total": total,
        "amount_to_collect": amount_to_collect,
        "remarks": _find(r"Comment\s*(.*?)\s+PAYMENT STATUS", text),
        "order_items": _extract_items(parser.rows),
    }
