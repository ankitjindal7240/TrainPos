from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

from django.utils import timezone


def decode_subject(subject):
    if not subject:
        return "(no subject)"
    return str(make_header(decode_header(subject)))


def extract_body(message):
    if message.is_multipart():
        for part in message.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", ""))

            if content_type == "text/plain" and "attachment" not in content_disposition:
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(
                        part.get_content_charset() or "utf-8",
                        errors="replace",
                    )
        return ""

    payload = message.get_payload(decode=True)
    if not payload:
        return ""
    return payload.decode(message.get_content_charset() or "utf-8", errors="replace")


def get_received_at(message):
    try:
        received_at = parsedate_to_datetime(message["Date"])
        if timezone.is_naive(received_at):
            return timezone.make_aware(received_at)
        return received_at
    except (TypeError, ValueError, IndexError):
        return timezone.now()
