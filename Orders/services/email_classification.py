import re


ORDER = "ORDER"
STATUS_UPDATE = "STATUS_UPDATE"


def classify_vendor_email(vendor, subject, body=""):
    if vendor.name != "RailRestro":
        return ORDER

    normalized_subject = " ".join((subject or "").split())
    if re.match(r"^Order Status Update for Order #[A-Za-z0-9-]+$", normalized_subject, re.I):
        return STATUS_UPDATE

    normalized_body = " ".join((body or "").split())
    if re.search(r"Current Status\s*:\s*CANCELL?ED\b", normalized_body, re.I):
        return STATUS_UPDATE

    return ORDER
