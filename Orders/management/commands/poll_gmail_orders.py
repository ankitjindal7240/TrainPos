import email
import imaplib
import os
import time
from datetime import timedelta
from email.utils import parseaddr

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from Orders.models import IncomingEmail, Vendor
from Orders.parsers.homebytes import parse_homebytes_email
from Orders.parsers.railrecipe import parse_railrecipe_email
from Orders.parsers.railrestro import parse_railrestro_email
from Orders.parsers.rajbhog_khana import parse_rajbhog_khana_email
from Orders.services.gmail_email import decode_subject, extract_body, get_received_at
from Orders.services.order_creation import create_order_from_incoming_email


GMAIL_HOST = "imap.gmail.com"
GMAIL_PORT = 993
POLL_INTERVAL_SECONDS = 30
PARSERS = {
    "RailRestro": parse_railrestro_email,
    "HomeBytes": parse_homebytes_email,
    "Rajbhog Khana": parse_rajbhog_khana_email,
    "RailRecipe": parse_railrecipe_email,
}


class Command(BaseCommand):
    help = "Poll Gmail for new vendor orders and create normalized TrainPOS orders."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Run one Gmail polling cycle, then exit.",
        )

    def handle(self, *args, **options):
        email_address = os.getenv("GMAIL_EMAIL")
        app_password = os.getenv("GMAIL_APP_PASSWORD")
        if not email_address or not app_password:
            raise CommandError(
                "Set GMAIL_EMAIL and GMAIL_APP_PASSWORD in .env before polling Gmail."
            )

        try:
            while True:
                self._poll_once(email_address, app_password)
                if options["once"]:
                    return

                self.stdout.write(
                    f"[TrainPOS] Next check in {POLL_INTERVAL_SECONDS} seconds..."
                )
                time.sleep(POLL_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("[TrainPOS] Gmail polling stopped."))

    def _poll_once(self, email_address, app_password):
        stats = {"checked": 0, "new": 0, "orders": 0, "skipped": 0, "failures": 0}
        mail = None
        today = timezone.localdate()
        self.stdout.write(f"[TrainPOS] Checking today's Gmail orders ({today.isoformat()})...")

        try:
            mail = imaplib.IMAP4_SSL(GMAIL_HOST, GMAIL_PORT)
            mail.login(email_address, app_password)
            mail.select("INBOX")

            message_uids = self._today_message_uids(mail, today)
            stats["checked"] = len(message_uids)

            for message_uid in message_uids:
                self._process_message(mail, message_uid, stats)
        except (imaplib.IMAP4.error, OSError) as error:
            stats["failures"] += 1
            self.stderr.write(self.style.ERROR(f"[ERROR] IMAP: {error}"))
        finally:
            if mail is not None:
                try:
                    mail.logout()
                except (imaplib.IMAP4.error, OSError):
                    pass

        self.stdout.write(
            "[TrainPOS] Cycle complete: "
            f"checked={stats['checked']} new={stats['new']} "
            f"orders={stats['orders']} skipped={stats['skipped']} "
            f"failures={stats['failures']}"
        )
        return stats

    def _today_message_uids(self, mail, today):
        tomorrow = today + timedelta(days=1)
        status, messages = mail.uid(
            "search",
            None,
            "SINCE",
            today.strftime("%d-%b-%Y"),
            "BEFORE",
            tomorrow.strftime("%d-%b-%Y"),
        )
        if status != "OK":
            raise imaplib.IMAP4.error("Unable to search the INBOX.")
        return messages[0].split()

    def _process_message(self, mail, message_uid, stats):
        message_id = message_uid.decode()
        status, message_data = mail.uid(
            "fetch",
            message_uid,
            "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])",
        )
        if status != "OK" or not message_data or not message_data[0]:
            stats["failures"] += 1
            self.stderr.write(self.style.ERROR(f"[ERROR] Could not read message {message_id}"))
            return

        header_message = email.message_from_bytes(message_data[0][1])
        if timezone.localtime(get_received_at(header_message)).date() != timezone.localdate():
            self.stdout.write(f"[SKIP] Outside current local date - {message_id}")
            return

        sender_email = parseaddr(header_message.get("From", ""))[1].lower()
        vendor = Vendor.objects.filter(email_address__iexact=sender_email).first()
        if vendor is None:
            return

        if IncomingEmail.objects.filter(message_id=message_id).exists():
            stats["skipped"] += 1
            self.stdout.write(f"[SKIP] Already ingested - {message_id}")
            return

        status, message_data = mail.uid("fetch", message_uid, "(RFC822)")
        if status != "OK" or not message_data or not message_data[0]:
            stats["failures"] += 1
            self.stderr.write(self.style.ERROR(f"[ERROR] Could not fetch message {message_id}"))
            return

        message = email.message_from_bytes(message_data[0][1])
        incoming_email, created = IncomingEmail.objects.get_or_create(
            message_id=message_id,
            defaults={
                "vendor": vendor,
                "subject": decode_subject(message.get("Subject"))[:500],
                "body": extract_body(message),
                "received_at": get_received_at(message),
                "processing_status": IncomingEmail.ProcessingStatus.RECEIVED,
                "error_message": "",
                "order": None,
            },
        )
        if not created:
            stats["skipped"] += 1
            self.stdout.write(f"[SKIP] Already ingested - {message_id}")
            return

        stats["new"] += 1
        self.stdout.write(f"[NEW] {vendor.name} - message {message_id}")

        try:
            parser = PARSERS[vendor.name]
            order = create_order_from_incoming_email(incoming_email, parser(incoming_email.body))
        except Exception as error:
            IncomingEmail.objects.filter(pk=incoming_email.pk, order__isnull=True).update(
                processing_status=IncomingEmail.ProcessingStatus.FAILED,
                error_message=str(error),
            )
            stats["failures"] += 1
            self.stderr.write(self.style.ERROR(f"[ERROR] {vendor.name} - {message_id}: {error}"))
            return

        stats["orders"] += 1
        self.stdout.write(f"[ORDER CREATED] {order.order_number}")
