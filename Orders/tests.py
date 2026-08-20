from decimal import Decimal
from datetime import datetime, timedelta
from email.message import EmailMessage

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from Orders.models import Customer, IncomingEmail, Order, Train, Vendor
from Orders.management.commands.poll_gmail_orders import Command
from Orders.parsers.homebytes import parse_homebytes_email
from Orders.parsers.railrecipe import parse_railrecipe_email
from Orders.parsers.railrestro import parse_railrestro_email
from Orders.parsers.rajbhog_khana import parse_rajbhog_khana_email
from Orders.services.order_creation import create_order_from_incoming_email


class OrderCreationServiceTests(TestCase):
    def setUp(self):
        self.vendors = {
            "RailRestro": Vendor.objects.create(
                name="RailRestro", email_address="no-reply@railrestro.com"
            ),
            "HomeBytes": Vendor.objects.create(
                name="HomeBytes", email_address="info@homebytes.co.in"
            ),
            "Rajbhog Khana": Vendor.objects.create(
                name="Rajbhog Khana", email_address="orders@rajbhogkhana.com"
            ),
            "RailRecipe": Vendor.objects.create(
                name="RailRecipe", email_address="no-reply@railrecipe.com"
            ),
        }

    def _email(self, vendor_name, message_id):
        return IncomingEmail.objects.create(
            message_id=message_id,
            vendor=self.vendors[vendor_name],
            subject="Test order",
            body="Raw vendor email body",
            received_at=timezone.now(),
        )

    def _data(self, order_number, payment_mode="PRE_PAID"):
        return {
            "order_number": order_number,
            "customer_name": "Radha Krishna",
            "customer_phone": "9462623238",
            "train_number": "12963",
            "train_name": "MEWAR EXPRESS",
            "coach": "B2",
            "berth": "36",
            "order_date": "2026-08-11 22:13:00",
            "payment_mode": payment_mode,
            "gst": Decimal("15.00"),
            "discount": Decimal("0.00"),
            "total": Decimal("315.00"),
            "order_items": [
                {
                    "item_name": "Veg Cheese Pizza",
                    "quantity": 1,
                    "price": Decimal("300.00"),
                    "gst": Decimal("15.00"),
                    "amount": Decimal("300.00"),
                }
            ],
        }

    def _assert_successful_creation(self, vendor_name, payment_mode):
        incoming_email = self._email(vendor_name, f"{vendor_name}-1")
        order = create_order_from_incoming_email(
            incoming_email,
            self._data(f"{vendor_name}-ORDER", payment_mode),
        )

        incoming_email.refresh_from_db()
        self.assertEqual(order.vendor, self.vendors[vendor_name])
        self.assertEqual(order.status, Order.Status.NEW)
        self.assertEqual(order.items.count(), 1)
        self.assertEqual(order.items.first().price, Decimal("300.00"))
        self.assertEqual(incoming_email.order, order)
        self.assertEqual(
            incoming_email.processing_status,
            IncomingEmail.ProcessingStatus.PROCESSED,
        )

    def test_successful_railrestro_order_creation(self):
        self._assert_successful_creation("RailRestro", "CASH_ON_DELIVERY")

    def test_successful_homebytes_order_creation(self):
        self._assert_successful_creation("HomeBytes", "PRE_PAID")

    def test_successful_rajbhog_order_creation(self):
        self._assert_successful_creation("Rajbhog Khana", "CASH_ON_DELIVERY")

    def test_successful_railrecipe_order_creation(self):
        self._assert_successful_creation("RailRecipe", "PRE_PAID")

    def test_duplicate_processing_returns_the_existing_order(self):
        incoming_email = self._email("HomeBytes", "duplicate-email")
        data = self._data("HB-DUPLICATE")

        first_order = create_order_from_incoming_email(incoming_email, data)
        second_order = create_order_from_incoming_email(incoming_email, data)

        self.assertEqual(first_order.pk, second_order.pk)
        self.assertEqual(Order.objects.count(), 1)

    def test_failed_creation_rolls_back_and_marks_email_failed(self):
        incoming_email = self._email("RailRestro", "failed-email")
        data = self._data("RR-FAILED")
        data["order_items"].append({"item_name": "Broken item", "quantity": "bad"})

        with self.assertRaises(ValueError):
            create_order_from_incoming_email(incoming_email, data)

        incoming_email.refresh_from_db()
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(Customer.objects.count(), 0)
        self.assertEqual(Train.objects.count(), 0)
        self.assertEqual(
            incoming_email.processing_status,
            IncomingEmail.ProcessingStatus.FAILED,
        )
        self.assertIn("Invalid item quantity", incoming_email.error_message)

    def test_order_list_contains_only_todays_orders(self):
        today_email = self._email("HomeBytes", "today-order")
        today_order = create_order_from_incoming_email(
            today_email,
            self._data("TODAY-ORDER"),
        )
        today_order.order_date = timezone.now()
        today_order.save(update_fields=["order_date"])

        older_email = self._email("RailRestro", "older-order")
        older_order = create_order_from_incoming_email(
            older_email,
            self._data("OLDER-ORDER", "CASH_ON_DELIVERY"),
        )
        older_order.order_date = timezone.now() - timedelta(days=1)
        older_order.save(update_fields=["order_date"])

        response = self.client.get(reverse("order_list"))

        self.assertContains(response, "TODAY-ORDER")
        self.assertNotContains(response, "OLDER-ORDER")
        self.assertEqual(response.context["summary"]["total_orders"], 1)

    def test_bill_route_marks_an_order_as_printed(self):
        incoming_email = self._email("RailRecipe", "bill-order")
        order = create_order_from_incoming_email(
            incoming_email,
            self._data("BILL-ORDER"),
        )

        response = self.client.get(reverse("order_bill", args=[order.pk]))

        order.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(order.bill_printed)
        self.assertIsNotNone(order.bill_printed_at)

    def test_homebytes_operational_datetime_is_saved_to_order(self):
        body = """
            Booking Date: 20 Aug 2026, 14:17<br>
            Delivery Date: 20 Aug 2026, 15:00<br>
            Customer Name : Radha Krishna<br>
            Customer Contact : 9462623238<br>
            Invoice HB001260544 / 2470000000<br>
            Payment: PRE_PAID<br>
            Coach / Berth: B2 / 36<br>
            Train: 12963 / MEWAR EXPRESS<br>
            GST (5%) 15.00 Discount 0.00 Total: 315.00
            <table><tr><td>1</td><td>Veg Cheese Pizza</td><td></td><td>1</td><td>300.00</td><td>15.00</td><td>300.00</td></tr></table>
        """
        parsed = parse_homebytes_email(body)
        incoming_email = self._email("HomeBytes", "homebytes-delivery-date")

        order = create_order_from_incoming_email(incoming_email, parsed)

        self.assertEqual(parsed["order_date"], "2026-08-20 15:00:00")
        self.assertEqual(
            order.order_date,
            timezone.make_aware(datetime(2026, 8, 20, 15, 0)),
        )

    def test_all_vendor_parsers_return_one_normalized_order_date(self):
        invoice_body = """
            Booking Date: 20 Aug 2026, 14:17<br>
            Delivery Date: 20 Aug 2026, 15:00<br>
            Customer Name : Radha Krishna<br>
            Customer Contact : 9462623238<br>
            Invoice HB001260544 / 2470000000<br>
            Payment: PRE_PAID<br>
            Coach / Berth: B2 / 36<br>
            Train: 12963 / MEWAR EXPRESS
        """
        railrestro_body = """
            ORDER #: 5759908 Customer: Mamta M. 6364385972
            TRAIN: 22975 / SAURASHTRA JANTA EXP
            Delivery Time: 2026-08-20 20:05:00
            Coact/Seat: S1-37
        """
        railrecipe_body = """
            Order No. RR-100 Mobile No. 9462623238 Train No. 12963
            Coach/Seat B2 / 36 Order Date Aug 20, 2026
            Delivery Time (ETA) Kota 15:00 Journey Date
            PAYMENT STATUS PREPAID
        """

        self.assertEqual(
            parse_homebytes_email(invoice_body)["order_date"], "2026-08-20 15:00:00"
        )
        self.assertEqual(
            parse_rajbhog_khana_email(invoice_body)["order_date"],
            "2026-08-20 15:00:00",
        )
        self.assertEqual(
            parse_railrestro_email(railrestro_body)["order_date"],
            "2026-08-20 20:05:00",
        )
        self.assertEqual(
            parse_railrecipe_email(railrecipe_body)["order_date"],
            "2026-08-20 15:00:00",
        )

    def test_gmail_poll_recovers_offline_email_and_skips_it_on_next_cycle(self):
        class FakeMail:
            def __init__(self, message):
                self.message = message

            def uid(self, command, message_uid, query):
                if command == "fetch":
                    return "OK", [(b"message", self.message)]
                raise AssertionError(f"Unexpected IMAP command: {command}")

        body = """
            Booking Date: 20 Aug 2026, 14:17<br>
            Delivery Date: 20 Aug 2026, 15:00<br>
            Customer Name : Radha Krishna<br>
            Customer Contact : 9462623238<br>
            Invoice HB-OFFLINE-1 / 2470000000<br>
            Payment: PRE_PAID<br>
            Coach / Berth: B2 / 36<br>
            Train: 12963 / MEWAR EXPRESS<br>
            GST (5%) 15.00 Discount 0.00 Total: 315.00
            <table><tr><td>1</td><td>Veg Cheese Pizza</td><td></td><td>1</td><td>300.00</td><td>15.00</td><td>300.00</td></tr></table>
        """
        message = EmailMessage()
        message["From"] = "HomeBytes <info@homebytes.co.in>"
        message["Subject"] = "HomeBytes offline order"
        message["Date"] = "Thu, 20 Aug 2026 14:17:00 +0000"
        message.set_content(body, subtype="html")

        command = Command()
        stats = {"checked": 1, "new": 0, "orders": 0, "skipped": 0, "failures": 0}
        fake_mail = FakeMail(message.as_bytes())

        command._process_message(fake_mail, b"9001", stats)
        command._process_message(fake_mail, b"9001", stats)

        self.assertEqual(stats["new"], 1)
        self.assertEqual(stats["orders"], 1)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(IncomingEmail.objects.filter(message_id="9001").count(), 1)
        self.assertEqual(Order.objects.filter(order_number="HB-OFFLINE-1").count(), 1)

    def test_gmail_poll_searches_only_the_current_local_date(self):
        class FakeMail:
            def __init__(self):
                self.search_arguments = None

            def uid(self, command, *arguments):
                self.search_arguments = (command, *arguments)
                return "OK", [b"801 900 1000"]

        fake_mail = FakeMail()
        today = timezone.localdate()

        message_uids = Command()._today_message_uids(fake_mail, today)

        self.assertEqual(message_uids, [b"801", b"900", b"1000"])
        self.assertEqual(
            fake_mail.search_arguments,
            (
                "search",
                None,
                "SINCE",
                today.strftime("%d-%b-%Y"),
                "BEFORE",
                (today + timedelta(days=1)).strftime("%d-%b-%Y"),
            ),
        )
