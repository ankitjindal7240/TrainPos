import email
import imaplib
import os
from datetime import date, timedelta
from email.utils import parseaddr

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from Orders.management.commands.poll_gmail_orders import GMAIL_HOST, GMAIL_PORT, PARSERS
from Orders.models import IncomingEmail, Vendor
from Orders.services.gmail_email import decode_subject, extract_body, get_received_at
from Orders.services.order_creation import create_order_from_incoming_email


class Command(BaseCommand):
    help = "Backfill historical vendor emails from Gmail for an inclusive date range."

    def add_arguments(self, parser):
        parser.add_argument("--from-date", help="Start date in YYYY-MM-DD format.")
        parser.add_argument("--to-date", help="End date in YYYY-MM-DD format.")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Search and count eligible emails without writing database records.",
        )
        parser.add_argument(
            "--retry-failed-message-id",
            action="append",
            default=[],
            help="Retry one saved FAILED IncomingEmail by message ID; may be repeated.",
        )

    def handle(self, *args, **options):
        retry_message_ids = options["retry_failed_message_id"]
        if retry_message_ids:
            stats = self._retry_failed_messages(retry_message_ids, options["dry_run"])
            self._write_retry_summary(retry_message_ids, stats, options["dry_run"])
            return

        start_date, end_date = self._date_range(options["from_date"], options["to_date"])
        email_address = os.getenv("GMAIL_EMAIL")
        app_password = os.getenv("GMAIL_APP_PASSWORD")
        if not email_address or not app_password:
            raise CommandError(
                "Set GMAIL_EMAIL and GMAIL_APP_PASSWORD in .env before backfilling Gmail."
            )

        stats = self._backfill(
            email_address,
            app_password,
            start_date,
            end_date,
            options["dry_run"],
        )
        self._write_summary(start_date, end_date, stats, options["dry_run"])

    def _date_range(self, from_date, to_date):
        if not from_date or not to_date:
            raise CommandError("Both --from-date and --to-date are required.")
        try:
            start_date = date.fromisoformat(from_date)
            end_date = date.fromisoformat(to_date)
        except (TypeError, ValueError) as error:
            raise CommandError("Use YYYY-MM-DD format for --from-date and --to-date.") from error
        if start_date > end_date:
            raise CommandError("--from-date cannot be later than --to-date.")
        return start_date, end_date

    def _retry_failed_messages(self, message_ids, dry_run):
        stats = {
            "checked": 0,
            "vendor_emails": 0,
            "new": 0,
            "orders": 0,
            "skipped": 0,
            "failures": 0,
        }
        failed_messages = IncomingEmail.objects.filter(
            message_id__in=message_ids,
            processing_status=IncomingEmail.ProcessingStatus.FAILED,
            order__isnull=True,
        ).select_related("vendor")
        failed_by_message_id = {item.message_id: item for item in failed_messages}

        for message_id in message_ids:
            incoming_email = failed_by_message_id.get(message_id)
            if incoming_email is None:
                stats["skipped"] += 1
                self.stdout.write(f"[SKIP] Not a retryable failed email - {message_id}")
                continue

            stats["checked"] += 1
            stats["vendor_emails"] += 1
            if dry_run:
                stats["new"] += 1
                self.stdout.write(f"[DRY RUN] Retry {incoming_email.vendor.name} - {message_id}")
                continue

            try:
                order = create_order_from_incoming_email(
                    incoming_email, PARSERS[incoming_email.vendor.name](incoming_email.body)
                )
            except Exception as error:
                stats["failures"] += 1
                self.stderr.write(
                    self.style.ERROR(
                        f"[ERROR] {incoming_email.vendor.name} - {message_id}: {error}"
                    )
                )
                continue

            stats["orders"] += 1
            self.stdout.write(f"[ORDER CREATED] {order.order_number}")
        return stats

    def _backfill(self, email_address, app_password, start_date, end_date, dry_run):
        stats = {
            "checked": 0,
            "vendor_emails": 0,
            "new": 0,
            "orders": 0,
            "skipped": 0,
            "failures": 0,
        }
        mail = None
        self.stdout.write(
            f"[TrainPOS Backfill] Checking Gmail from {start_date} to {end_date}..."
        )
        try:
            mail = imaplib.IMAP4_SSL(GMAIL_HOST, GMAIL_PORT)
            mail.login(email_address, app_password)
            mail.select("INBOX")

            message_uids = self._message_uids_for_range(mail, start_date, end_date)
            stats["checked"] = len(message_uids)
            for message_uid in message_uids:
                self._process_message(
                    mail, message_uid, stats, start_date, end_date, dry_run
                )
        except (imaplib.IMAP4.error, OSError) as error:
            stats["failures"] += 1
            self.stderr.write(self.style.ERROR(f"[ERROR] IMAP: {error}"))
        finally:
            if mail is not None:
                try:
                    mail.logout()
                except (imaplib.IMAP4.error, OSError):
                    pass
        return stats

    def _message_uids_for_range(self, mail, start_date, end_date):
        status, messages = mail.uid(
            "search",
            None,
            "SINCE",
            start_date.strftime("%d-%b-%Y"),
            "BEFORE",
            (end_date + timedelta(days=1)).strftime("%d-%b-%Y"),
        )
        if status != "OK":
            raise imaplib.IMAP4.error("Unable to search the INBOX.")
        return messages[0].split()

    def _process_message(self, mail, message_uid, stats, start_date, end_date, dry_run):
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
        message_date = timezone.localtime(get_received_at(header_message)).date()
        if not start_date <= message_date <= end_date:
            self.stdout.write(f"[SKIP] Outside requested date range - {message_id}")
            return

        sender_email = parseaddr(header_message.get("From", ""))[1].lower()
        vendor = Vendor.objects.filter(email_address__iexact=sender_email).first()
        if vendor is None:
            return
        stats["vendor_emails"] += 1

        if IncomingEmail.objects.filter(message_id=message_id).exists():
            stats["skipped"] += 1
            self.stdout.write(f"[SKIP] Already ingested - {message_id}")
            return

        if dry_run:
            stats["new"] += 1
            self.stdout.write(f"[DRY RUN] New {vendor.name} message - {message_id}")
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
            order = create_order_from_incoming_email(
                incoming_email, PARSERS[vendor.name](incoming_email.body)
            )
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

    def _write_summary(self, start_date, end_date, stats, dry_run):
        label = "[TrainPOS Backfill] DRY RUN" if dry_run else "[TrainPOS Backfill]"
        self.stdout.write(
            f"{label}\n"
            f"Date range: {start_date} -> {end_date}\n"
            f"Checked: {stats['checked']}\n"
            f"Vendor emails: {stats['vendor_emails']}\n"
            f"New: {stats['new']}\n"
            f"Orders created: {stats['orders']}\n"
            f"Already ingested/skipped: {stats['skipped']}\n"
            f"Failures: {stats['failures']}"
        )

    def _write_retry_summary(self, message_ids, stats, dry_run):
        label = "[TrainPOS Backfill Retry] DRY RUN" if dry_run else "[TrainPOS Backfill Retry]"
        self.stdout.write(
            f"{label}\n"
            f"Requested failed messages: {len(message_ids)}\n"
            f"Checked: {stats['checked']}\n"
            f"Orders created: {stats['orders']}\n"
            f"Already ingested/skipped: {stats['skipped']}\n"
            f"Failures: {stats['failures']}"
        )
