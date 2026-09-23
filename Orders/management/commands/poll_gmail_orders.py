import email
import imaplib
import os
import time
from datetime import timedelta
from email.utils import parseaddr

from django.core.management.base import BaseCommand
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone

from Orders.models import IncomingEmail, RestaurantEmailConnection, Vendor
from Orders.services.gmail_email import decode_subject, extract_body, get_received_at
from Orders.services.email_classification import ORDER, classify_vendor_email
from Orders.services.email_failures import (
    is_terminal_parser_or_validation_error,
    sanitized_error_message,
)
from Orders.services.order_creation import create_order_from_incoming_email
from Orders.services.tenancy import get_food_costa_restaurant
from Orders.services.vendor_parsers import get_vendor_parser


GMAIL_HOST = "imap.gmail.com"
GMAIL_PORT = 993
POLL_INTERVAL_SECONDS = 30
class Command(BaseCommand):
    help = "Poll Gmail for new vendor orders and create normalized TrainPOS orders."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Run one Gmail polling cycle, then exit.",
        )

    def handle(self, *args, **options):
        try:
            while True:
                self._poll_all_restaurants()
                if options["once"]:
                    return

                self.stdout.write(
                    f"[TrainPOS] Next check in {POLL_INTERVAL_SECONDS} seconds..."
                )
                time.sleep(POLL_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("[TrainPOS] Gmail polling stopped."))

    def _eligible_connections(self):
        return [
            connection
            for connection in RestaurantEmailConnection.objects.select_related(
                "restaurant"
            ).filter(is_active=True).order_by("id")
            if connection.encrypted_app_password and connection.restaurant.has_access
        ]

    def _legacy_food_costa_mailbox(self, configured_restaurant_ids):
        """Keep the old environment mailbox alive until Food Costa is configured."""
        restaurant = get_food_costa_restaurant()
        if restaurant.id in configured_restaurant_ids:
            return None
        email_address = os.getenv("GMAIL_EMAIL")
        app_password = os.getenv("GMAIL_APP_PASSWORD")
        if not email_address or not app_password:
            return None
        return restaurant, email_address, app_password

    def _poll_all_restaurants(self):
        connections = self._eligible_connections()
        configured_restaurant_ids = {connection.restaurant_id for connection in connections}
        mailboxes = [(connection, connection.restaurant) for connection in connections]
        legacy_mailbox = self._legacy_food_costa_mailbox(configured_restaurant_ids)
        if legacy_mailbox:
            restaurant, email_address, app_password = legacy_mailbox
            mailboxes.append(((email_address, app_password), restaurant))

        if not mailboxes:
            self.stdout.write(self.style.WARNING("[TrainPOS] No eligible Gmail connections."))
            return []

        results = []
        for mailbox, restaurant in mailboxes:
            self.stdout.write(f"[TrainPOS] Restaurant: {restaurant.name}")
            try:
                if isinstance(mailbox, RestaurantEmailConnection):
                    results.append(self._poll_connection(mailbox))
                else:
                    email_address, app_password = mailbox
                    results.append(self._poll_once(email_address, app_password, restaurant))
            except Exception:
                if isinstance(mailbox, RestaurantEmailConnection):
                    mailbox.connection_status = (
                        RestaurantEmailConnection.ConnectionStatus.ERROR
                    )
                    mailbox.last_checked_at = timezone.now()
                    mailbox.save(
                        update_fields=[
                            "connection_status",
                            "last_checked_at",
                            "updated_at",
                        ]
                    )
                self.stderr.write(
                    self.style.ERROR(
                        f"[ERROR] {restaurant.name}: Gmail polling cycle failed."
                    )
                )
                results.append(None)
        return results

    def _poll_connection(self, connection):
        connection.last_checked_at = timezone.now()
        try:
            app_password = connection.get_app_password()
        except (ImproperlyConfigured, ValueError):
            connection.connection_status = RestaurantEmailConnection.ConnectionStatus.ERROR
            connection.save(update_fields=["connection_status", "last_checked_at", "updated_at"])
            self.stderr.write(
                self.style.ERROR(
                    f"[ERROR] {connection.restaurant.name}: Gmail credential could not be used."
                )
            )
            return None

        stats = self._poll_once(
            connection.email_address,
            app_password,
            connection.restaurant,
            connection,
        )
        return stats

    def _poll_once(self, email_address, app_password, restaurant=None, connection=None):
        restaurant = restaurant or get_food_costa_restaurant()
        stats = {
            "checked": 0,
            "new": 0,
            "existing_retried": 0,
            "orders": 0,
            "skipped": 0,
            "failures": 0,
        }
        mail = None
        today = timezone.localdate()
        self.stdout.write(f"[TrainPOS] Checking today's Gmail orders ({today.isoformat()})...")

        imap_connected = False
        try:
            mail = imaplib.IMAP4_SSL(GMAIL_HOST, GMAIL_PORT)
            mail.login(email_address, app_password)
            imap_connected = True
            mail.select("INBOX")

            message_uids = self._today_message_uids(mail, today)
            stats["checked"] = len(message_uids)

            for message_uid in message_uids:
                try:
                    self._process_message(mail, message_uid, stats, restaurant)
                except Exception:
                    stats["failures"] += 1
                    self.stderr.write(
                        self.style.ERROR(
                            f"[ERROR] Could not process message {message_uid.decode(errors='replace')}"
                        )
                    )
        except (imaplib.IMAP4.error, OSError):
            stats["failures"] += 1
            self.stderr.write(self.style.ERROR("[ERROR] IMAP connection failed."))
        finally:
            if mail is not None:
                try:
                    mail.logout()
                except (imaplib.IMAP4.error, OSError):
                    pass

        if connection is not None:
            connection.last_checked_at = timezone.now()
            connection.connection_status = (
                RestaurantEmailConnection.ConnectionStatus.CONNECTED
                if imap_connected
                else RestaurantEmailConnection.ConnectionStatus.ERROR
            )
            connection.save(update_fields=["connection_status", "last_checked_at", "updated_at"])

        self.stdout.write(
            "[TrainPOS] Cycle complete: "
            f"checked={stats['checked']} new={stats['new']} "
            f"orders={stats['orders']} retried={stats['existing_retried']} "
            f"skipped={stats['skipped']} "
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

    def _process_message(self, mail, message_uid, stats, restaurant=None):
        restaurant = restaurant or get_food_costa_restaurant()
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
        vendor = Vendor.objects.filter(
            restaurant=restaurant,
            email_address__iexact=sender_email,
            is_active=True,
        ).first()
        if vendor is None:
            return
        subject = decode_subject(header_message.get("Subject"))

        incoming_email = (
            IncomingEmail.objects.filter(restaurant=restaurant, message_id=message_id)
            .select_related("vendor")
            .first()
        )
        if classify_vendor_email(vendor, subject) != ORDER:
            self._store_non_order_email(
                mail, message_uid, message_id, vendor, subject, incoming_email, stats, restaurant
            )
            return
        if incoming_email is not None:
            if incoming_email.order_id:
                stats["skipped"] += 1
                self.stdout.write(f"[SKIP] Already successfully ingested - {message_id}")
                return
            if incoming_email.processing_status == IncomingEmail.ProcessingStatus.INVALID:
                self._skip_invalid_email(incoming_email, stats)
                return
            stats["existing_retried"] += 1
            if classify_vendor_email(
                incoming_email.vendor, incoming_email.subject, incoming_email.body
            ) != ORDER:
                self._mark_non_order(incoming_email, stats)
                return
            self.stdout.write(f"[RETRY] {incoming_email.vendor.name} - message {message_id}")
            self._create_order_from_incoming_email(incoming_email, stats)
            return

        status, message_data = mail.uid("fetch", message_uid, "(RFC822)")
        if status != "OK" or not message_data or not message_data[0]:
            stats["failures"] += 1
            self.stderr.write(self.style.ERROR(f"[ERROR] Could not fetch message {message_id}"))
            return

        message = email.message_from_bytes(message_data[0][1])
        incoming_email, created = IncomingEmail.objects.get_or_create(
            restaurant=restaurant,
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
            if incoming_email.order_id:
                stats["skipped"] += 1
                self.stdout.write(f"[SKIP] Already successfully ingested - {message_id}")
                return
            if incoming_email.processing_status == IncomingEmail.ProcessingStatus.INVALID:
                self._skip_invalid_email(incoming_email, stats)
                return
            stats["existing_retried"] += 1
            if classify_vendor_email(
                incoming_email.vendor, incoming_email.subject, incoming_email.body
            ) != ORDER:
                self._mark_non_order(incoming_email, stats)
                return
            self.stdout.write(f"[RETRY] {incoming_email.vendor.name} - message {message_id}")
            self._create_order_from_incoming_email(incoming_email, stats)
            return

        stats["new"] += 1
        self.stdout.write(f"[NEW] {vendor.name} - message {message_id}")
        if classify_vendor_email(vendor, incoming_email.subject, incoming_email.body) != ORDER:
            self._mark_non_order(incoming_email, stats)
            return
        self._create_order_from_incoming_email(incoming_email, stats)

    def _store_non_order_email(
        self, mail, message_uid, message_id, vendor, subject, incoming_email, stats, restaurant
    ):
        if incoming_email is None:
            status, message_data = mail.uid("fetch", message_uid, "(RFC822)")
            if status != "OK" or not message_data or not message_data[0]:
                stats["failures"] += 1
                self.stderr.write(
                    self.style.ERROR(f"[ERROR] Could not fetch message {message_id}")
                )
                return
            message = email.message_from_bytes(message_data[0][1])
            incoming_email, _ = IncomingEmail.objects.get_or_create(
                restaurant=restaurant,
                message_id=message_id,
                defaults={
                    "vendor": vendor,
                    "subject": subject[:500],
                    "body": extract_body(message),
                    "received_at": get_received_at(message),
                    "processing_status": IncomingEmail.ProcessingStatus.SKIPPED,
                    "error_message": "",
                    "order": None,
                },
            )
        self._mark_non_order(incoming_email, stats)

    def _mark_non_order(self, incoming_email, stats):
        if incoming_email.order_id:
            stats["skipped"] += 1
            self.stdout.write(
                f"[SKIP] Already successfully ingested - {incoming_email.message_id}"
            )
            return
        IncomingEmail.objects.filter(pk=incoming_email.pk).update(
            processing_status=IncomingEmail.ProcessingStatus.SKIPPED,
            error_message="",
        )
        stats["skipped"] += 1
        self.stdout.write(f"[SKIP] Non-order status update - {incoming_email.message_id}")

    def _skip_invalid_email(self, incoming_email, stats):
        stats["skipped"] += 1
        self.stdout.write(f"[SKIP] Invalid email - {incoming_email.message_id}")

    def _create_order_from_incoming_email(self, incoming_email, stats):
        try:
            parser = get_vendor_parser(incoming_email.vendor)
            order = create_order_from_incoming_email(incoming_email, parser(incoming_email.body))
        except Exception as error:
            processing_status = (
                IncomingEmail.ProcessingStatus.INVALID
                if is_terminal_parser_or_validation_error(error)
                else IncomingEmail.ProcessingStatus.FAILED
            )
            IncomingEmail.objects.filter(pk=incoming_email.pk, order__isnull=True).update(
                processing_status=processing_status,
                error_message=sanitized_error_message(error),
            )
            stats["failures"] += 1
            self.stderr.write(
                self.style.ERROR(
                    f"[ERROR] {incoming_email.vendor.name} - {incoming_email.message_id}: {error}"
                )
            )
            return None

        stats["orders"] += 1
        self.stdout.write(f"[ORDER CREATED] {order.order_number}")
        return order
