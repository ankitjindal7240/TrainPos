import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser


class _HomeBytesHtmlParser(HTMLParser):
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

    try:
        return Decimal(re.sub(r"[^0-9.-]", "", value))
    except (InvalidOperation, ValueError):
        return None


def _find_amount(label, text):
    return _amount(
        _find(
            rf"(?<![A-Za-z]){re.escape(label)}(?![A-Za-z])\s*:?\s*([0-9]+(?:\.[0-9]+)?)",
            text,
        )
    )


def _extract_items(rows):
    items = []

    for row in rows:
        if len(row) != 7 or not row[0].isdigit() or not row[3].isdigit():
            continue
        items.append({"item_name": row[1], "quantity": int(row[3])})

    return items


def _normalized_order_datetime(date_value, time_value):
    if not date_value or not time_value:
        return None
    return datetime.strptime(
        f"{date_value} {time_value}", "%d %b %Y %H:%M"
    ).strftime("%Y-%m-%d %H:%M:%S")


def parse_homebytes_email(body):
    """Return the V1 receipt fields from one saved HomeBytes email body."""
    parser = _HomeBytesHtmlParser()
    parser.feed(body)
    parser.close()
    text = " ".join("".join(parser.text_parts).split())

    delivery_date = _find(r"Delivery Date:\s*(\d{1,2}\s+[A-Za-z]{3}\s+\d{4}),", text)
    delivery_time = _find(r"Delivery Date:\s*\d{1,2}\s+[A-Za-z]{3}\s+\d{4},\s*(\d{1,2}:\d{2})", text)
    payment_mode = _find(r"Payment:\s*(PRE_PAID|CASH_ON_DELIVERY)", text)
    total = _find_amount("Total", text)

    if payment_mode == "PRE_PAID":
        advance = total
        amount_to_collect = Decimal("0")
    elif payment_mode == "CASH_ON_DELIVERY":
        advance = Decimal("0")
        amount_to_collect = total
    else:
        advance = None
        amount_to_collect = None

    return {
        "order_number": _find(r"Invoice\s+([A-Za-z0-9-]+)\s*/", text),
        "customer_name": _find(r"Customer Name\s*:\s*(.*?)\s+Customer Contact", text),
        "customer_phone": _find(r"Customer Contact\s*:\s*([0-9]+)", text),
        "train_number": _find(r"Train:\s*([A-Za-z0-9]+)\s*/", text),
        "coach": _find(r"Coach\s*/\s*Berth:\s*(.*?)\s+/\s+", text),
        "berth": _find(r"Coach\s*/\s*Berth:\s*.*?\s*/\s*([A-Za-z0-9]+)\s+Train:", text),
        "order_date": _normalized_order_datetime(delivery_date, delivery_time),
        "payment_mode": payment_mode,
        "advance": advance,
        "gst": _find_amount("GST (5%)", text),
        "tax": None,
        "discount": _find_amount("Discount", text),
        "total": total,
        "amount_to_collect": amount_to_collect,
        "remarks": None,
        "order_items": _extract_items(parser.rows),
    }
