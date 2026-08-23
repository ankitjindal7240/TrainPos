from decimal import Decimal
from datetime import date, datetime, time, timedelta
from email.message import EmailMessage

from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from Orders.models import Customer, IncomingEmail, Order, Train, Vendor
from Orders.management.commands.poll_gmail_orders import Command
from Orders.management.commands.backfill_gmail_orders import Command as BackfillCommand
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

    def test_railrecipe_normalizes_payment_status_variants(self):
        for raw_payment_status, expected_payment_mode in (
            ("CASH_ON_DELIVERY", Order.PaymentMode.CASH_ON_DELIVERY),
            ("CASH ON DELIVERY", Order.PaymentMode.CASH_ON_DELIVERY),
            ("COD", Order.PaymentMode.CASH_ON_DELIVERY),
            ("PREPAID", Order.PaymentMode.PRE_PAID),
            ("PRE_PAID", Order.PaymentMode.PRE_PAID),
        ):
            body = f"""
                Order No. RR-100 Mobile No. 9462623238 Train No. 12963
                Coach/Seat B2 / 36 Order Date Aug 20, 2026
                Delivery Time (ETA) Kota 15:00 Journey Date
                PAYMENT STATUS: {raw_payment_status}
            """
            self.assertEqual(
                parse_railrecipe_email(body)["payment_mode"], expected_payment_mode
            )

    def test_railrestro_paid_total_with_zero_collection_is_prepaid(self):
        body = """
            ORDER #: 5726441 Customer: Test Customer M. 9000000000
            TRAIN: 12963 / MEWAR EXPRESS
            Delivery Time: 2026-08-20 20:05:00
            Coact/Seat: B2-36
            Final Total: Rs. 382.10
            Paid Total: Rs. 382.10
            (Amount to collect) Rs. 0/-
        """

        parsed = parse_railrestro_email(body)

        self.assertEqual(parsed["payment_mode"], Order.PaymentMode.PRE_PAID)

    def test_gmail_poll_recovers_offline_email_and_skips_it_on_next_cycle(self):
        class FakeMail:
            def __init__(self, message):
                self.message = message

            def uid(self, command, message_uid, query):
                if command == "fetch":
                    return "OK", [(b"message", self.message)]
                raise AssertionError(f"Unexpected IMAP command: {command}")

        today = timezone.localdate()
        body = f"""
            Booking Date: {today:%d %b %Y}, 14:17<br>
            Delivery Date: {today:%d %b %Y}, 15:00<br>
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
        message["Date"] = today.strftime("%a, %d %b %Y 14:17:00 +0000")
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

    def test_gmail_poll_retries_existing_received_email_without_an_order(self):
        class FakeMail:
            def __init__(self, message):
                self.message = message

            def uid(self, command, message_uid, query):
                if command == "fetch":
                    return "OK", [(b"message", self.message)]
                raise AssertionError(f"Unexpected IMAP command: {command}")

        today = timezone.localdate()
        body = f"""
            Booking Date: {today:%d %b %Y}, 14:17<br>
            Delivery Date: {today:%d %b %Y}, 15:00<br>
            Customer Name : Radha Krishna<br>
            Customer Contact : 9462623238<br>
            Invoice HB-LIVE-RETRY / 2470000000<br>
            Payment: PRE_PAID<br>
            Coach / Berth: B2 / 36<br>
            Train: 12963 / MEWAR EXPRESS<br>
            GST (5%) 15.00 Discount 0.00 Total: 315.00
            <table><tr><td>1</td><td>Veg Cheese Pizza</td><td></td><td>1</td><td>300.00</td><td>15.00</td><td>300.00</td></tr></table>
        """
        IncomingEmail.objects.create(
            message_id="9002",
            vendor=self.vendors["HomeBytes"],
            subject="Existing received order",
            body=body,
            received_at=timezone.now(),
            processing_status=IncomingEmail.ProcessingStatus.RECEIVED,
        )
        message = EmailMessage()
        message["From"] = "HomeBytes <info@homebytes.co.in>"
        message["Subject"] = "HomeBytes retry"
        message["Date"] = today.strftime("%a, %d %b %Y 14:17:00 +0000")
        message.set_content(body, subtype="html")
        stats = {
            "checked": 1,
            "new": 0,
            "existing_retried": 0,
            "orders": 0,
            "skipped": 0,
            "failures": 0,
        }

        Command()._process_message(FakeMail(message.as_bytes()), b"9002", stats)

        incoming_email = IncomingEmail.objects.get(message_id="9002")
        self.assertEqual(stats["existing_retried"], 1)
        self.assertEqual(stats["orders"], 1)
        self.assertEqual(incoming_email.processing_status, IncomingEmail.ProcessingStatus.PROCESSED)
        self.assertTrue(Order.objects.filter(order_number="HB-LIVE-RETRY").exists())

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


class ReportsViewTests(TestCase):
    def setUp(self):
        self.homebytes = Vendor.objects.create(
            name="HomeBytes", email_address="info@homebytes.co.in"
        )
        self.railrestro = Vendor.objects.create(
            name="RailRestro", email_address="no-reply@railrestro.com"
        )
        self.customer = Customer.objects.create(name="Report Customer", phone="9000000000")
        self.train = Train.objects.create(train_number="12963", train_name="MEWAR EXPRESS")

    def _order(self, number, order_date, total, payment_mode, vendor=None, status=None):
        return Order.objects.create(
            vendor=vendor or self.homebytes,
            order_number=number,
            customer=self.customer,
            train=self.train,
            order_date=timezone.make_aware(datetime.combine(order_date, time(12, 0))),
            payment_mode=payment_mode,
            total=Decimal(total),
            status=status or Order.Status.NEW,
        )

    def test_today_report_filters_using_order_date(self):
        today = timezone.localdate()
        self._order("TODAY", today, "100.00", Order.PaymentMode.PRE_PAID)
        self._order("YESTERDAY", today - timedelta(days=1), "200.00", Order.PaymentMode.PRE_PAID)

        response = self.client.get(reverse("reports"))

        self.assertContains(response, "Today")
        self.assertEqual(response.context["summary"]["total_orders"], 1)

    def test_yesterday_report_filters_using_order_date(self):
        today = timezone.localdate()
        self._order("TODAY", today, "100.00", Order.PaymentMode.PRE_PAID)
        self._order("YESTERDAY", today - timedelta(days=1), "200.00", Order.PaymentMode.CASH_ON_DELIVERY)

        response = self.client.get(reverse("reports"), {"period": "yesterday"})

        self.assertEqual(response.context["summary"]["total_orders"], 1)
        self.assertEqual(response.context["summary"]["cod_value"], Decimal("200.00"))

    def test_this_week_starts_on_monday(self):
        today = timezone.localdate()
        monday = today - timedelta(days=today.weekday())
        self._order("MONDAY", monday, "100.00", Order.PaymentMode.PRE_PAID)
        self._order("PREVIOUS-SUNDAY", monday - timedelta(days=1), "200.00", Order.PaymentMode.PRE_PAID)

        response = self.client.get(reverse("reports"), {"period": "week"})

        self.assertEqual(response.context["start_date"], monday)
        self.assertEqual(response.context["summary"]["total_orders"], 1)

    def test_this_month_starts_on_first_day_and_ends_today(self):
        today = timezone.localdate()
        first_day = today.replace(day=1)
        previous_month_day = first_day - timedelta(days=1)
        self._order("FIRST-DAY", first_day, "100.00", Order.PaymentMode.PRE_PAID)
        self._order("PREVIOUS-MONTH", previous_month_day, "200.00", Order.PaymentMode.PRE_PAID)

        response = self.client.get(reverse("reports"), {"period": "month"})

        self.assertEqual(response.context["start_date"], first_day)
        self.assertEqual(response.context["end_date"], today)
        self.assertEqual(response.context["summary"]["total_orders"], 1)

    def test_custom_range_is_inclusive(self):
        today = timezone.localdate()
        start_date = today - timedelta(days=3)
        end_date = today - timedelta(days=1)
        self._order("START", start_date, "100.00", Order.PaymentMode.PRE_PAID)
        self._order("MIDDLE", start_date + timedelta(days=1), "200.00", Order.PaymentMode.PRE_PAID)
        self._order("END", end_date, "300.00", Order.PaymentMode.PRE_PAID)
        self._order("OUTSIDE", today, "400.00", Order.PaymentMode.PRE_PAID)

        response = self.client.get(
            reverse("reports"),
            {"period": "custom", "from_date": start_date, "to_date": end_date},
        )

        self.assertEqual(response.context["summary"]["total_orders"], 3)
        self.assertEqual(response.context["summary"]["online_value"], Decimal("600.00"))

    def test_invalid_custom_range_shows_validation_message(self):
        today = timezone.localdate()

        response = self.client.get(
            reverse("reports"),
            {"period": "custom", "from_date": today, "to_date": today - timedelta(days=1)},
        )

        self.assertContains(response, "From Date cannot be after To Date.")
        self.assertEqual(response.context["summary"]["total_orders"], 0)

    def test_report_aggregates_cod_online_and_net_sales(self):
        today = timezone.localdate()
        self._order("COD", today, "125.50", Order.PaymentMode.CASH_ON_DELIVERY)
        self._order("ONLINE", today, "200.25", Order.PaymentMode.PRE_PAID)

        response = self.client.get(reverse("reports"))
        summary = response.context["summary"]

        self.assertEqual(summary["total_orders"], 2)
        self.assertEqual(summary["cod_value"], Decimal("125.50"))
        self.assertEqual(summary["online_value"], Decimal("200.25"))
        self.assertEqual(summary["net_sales"], Decimal("325.75"))

    def test_vendor_totals_reconcile_with_overall_totals(self):
        today = timezone.localdate()
        self._order("HOME-COD", today, "125.00", Order.PaymentMode.CASH_ON_DELIVERY)
        self._order(
            "RAIL-ONLINE",
            today,
            "200.00",
            Order.PaymentMode.PRE_PAID,
            vendor=self.railrestro,
        )

        response = self.client.get(reverse("reports"))
        vendor_rows = list(response.context["vendor_breakdown"])

        self.assertEqual(sum(row["total_orders"] for row in vendor_rows), 2)
        self.assertEqual(
            sum(row["cod_value"] for row in vendor_rows),
            response.context["summary"]["cod_value"],
        )
        self.assertEqual(
            sum(row["online_value"] for row in vendor_rows),
            response.context["summary"]["online_value"],
        )
        self.assertEqual(
            sum(row["net_sales"] for row in vendor_rows),
            response.context["summary"]["net_sales"],
        )

    def test_reports_include_all_order_statuses(self):
        today = timezone.localdate()
        self._order(
            "CANCELLED-STATUS",
            today,
            "100.00",
            Order.PaymentMode.CASH_ON_DELIVERY,
            status=Order.Status.CANCELLED,
        )
        self._order(
            "DELIVERED-STATUS",
            today,
            "200.00",
            Order.PaymentMode.PRE_PAID,
            status=Order.Status.DELIVERED,
        )

        response = self.client.get(reverse("reports"))

        self.assertEqual(response.context["summary"]["total_orders"], 2)


class GmailBackfillCommandTests(TestCase):
    def setUp(self):
        self.vendor = Vendor.objects.create(
            name="HomeBytes", email_address="info@homebytes.co.in"
        )

    def _body(self, order_number, order_day):
        return f"""
            Booking Date: {order_day:%d %b %Y}, 09:30<br>
            Delivery Date: {order_day:%d %b %Y}, 10:00<br>
            Customer Name : Backfill Customer<br>
            Customer Contact : 9000000000<br>
            Invoice {order_number} / 2470000000<br>
            Payment: PRE_PAID<br>
            Coach / Berth: B2 / 36<br>
            Train: 12963 / MEWAR EXPRESS<br>
            GST (5%) 15.00 Discount 0.00 Total: 315.00
            <table><tr><td>1</td><td>Veg Cheese Pizza</td><td></td><td>1</td><td>300.00</td><td>15.00</td><td>300.00</td></tr></table>
        """

    def _message(self, order_number, order_day, body=None):
        message = EmailMessage()
        message["From"] = "HomeBytes <info@homebytes.co.in>"
        message["Subject"] = f"HomeBytes {order_number}"
        message["Date"] = order_day.strftime("%a, %d %b %Y 10:00:00 +0000")
        message.set_content(
            body
            or self._body(order_number, order_day),
            subtype="html",
        )
        return message.as_bytes()

    def _mail(self, messages):
        class FakeMail:
            def __init__(self, values):
                self.messages = values
                self.search_arguments = None

            def uid(self, command, *arguments):
                if command == "search":
                    self.search_arguments = (command, *arguments)
                    return "OK", [b" ".join(self.messages)]
                if command == "fetch":
                    return "OK", [(b"message", self.messages[arguments[0]])]
                raise AssertionError(f"Unexpected IMAP command: {command}")

        return FakeMail(messages)

    def _stats(self):
        return {
            "checked": 0,
            "vendor_emails": 0,
            "new": 0,
            "existing_retried": 0,
            "orders": 0,
            "skipped": 0,
            "failures": 0,
        }

    def test_search_range_is_inclusive_and_before_uses_next_day(self):
        command = BackfillCommand()
        mail = self._mail({b"8001": self._message("HB-SEARCH", date(2026, 8, 1))})

        message_uids = command._message_uids_for_range(
            mail, date(2026, 8, 1), date(2026, 8, 22)
        )

        self.assertEqual(message_uids, [b"8001"])
        self.assertEqual(
            mail.search_arguments,
            ("search", None, "SINCE", "01-Aug-2026", "BEFORE", "23-Aug-2026"),
        )

    def test_invalid_date_arguments_are_rejected(self):
        command = BackfillCommand()

        with self.assertRaises(CommandError):
            command._date_range(None, None)
        with self.assertRaises(CommandError):
            command._date_range("not-a-date", "2026-08-22")
        with self.assertRaises(CommandError):
            command._date_range("2026-08-23", "2026-08-22")

    def test_existing_received_email_without_order_is_retried(self):
        historical_day = date(2026, 8, 5)
        IncomingEmail.objects.create(
            message_id="8002",
            vendor=self.vendor,
            subject="Existing",
            body=self._body("HB-RECEIVED", historical_day),
            received_at=timezone.now(),
            processing_status=IncomingEmail.ProcessingStatus.RECEIVED,
        )
        command = BackfillCommand()
        stats = self._stats()

        command._process_message(
            self._mail({b"8002": self._message("HB-RECEIVED", historical_day)}),
            b"8002",
            stats,
            historical_day,
            historical_day,
            False,
        )

        incoming_email = IncomingEmail.objects.get(message_id="8002")
        self.assertEqual(stats["existing_retried"], 1)
        self.assertEqual(stats["orders"], 1)
        self.assertEqual(incoming_email.processing_status, IncomingEmail.ProcessingStatus.PROCESSED)
        self.assertIsNotNone(incoming_email.order_id)

        command._process_message(
            self._mail({b"8002": self._message("HB-RECEIVED", historical_day)}),
            b"8002",
            stats,
            historical_day,
            historical_day,
            False,
        )
        self.assertEqual(Order.objects.filter(order_number="HB-RECEIVED").count(), 1)
        self.assertEqual(stats["skipped"], 1)

    def test_existing_failed_email_without_order_is_retried(self):
        historical_day = date(2026, 8, 5)
        IncomingEmail.objects.create(
            message_id="8007",
            vendor=self.vendor,
            subject="Failed",
            body=self._body("HB-FAILED", historical_day),
            received_at=timezone.now(),
            processing_status=IncomingEmail.ProcessingStatus.FAILED,
            error_message="Earlier parser error",
        )
        stats = self._stats()

        BackfillCommand()._process_message(
            self._mail({b"8007": self._message("HB-FAILED", historical_day)}),
            b"8007",
            stats,
            historical_day,
            historical_day,
            False,
        )

        incoming_email = IncomingEmail.objects.get(message_id="8007")
        self.assertEqual(stats["existing_retried"], 1)
        self.assertEqual(incoming_email.processing_status, IncomingEmail.ProcessingStatus.PROCESSED)
        self.assertEqual(incoming_email.error_message, "")

    def test_existing_processed_email_with_order_is_skipped(self):
        historical_day = date(2026, 8, 5)
        body = self._body("HB-PROCESSED", historical_day)
        incoming_email = IncomingEmail.objects.create(
            message_id="8008",
            vendor=self.vendor,
            subject="Processed",
            body=body,
            received_at=timezone.now(),
        )
        create_order_from_incoming_email(incoming_email, parse_homebytes_email(body))
        stats = self._stats()

        BackfillCommand()._process_message(
            self._mail({b"8008": self._message("HB-PROCESSED", historical_day)}),
            b"8008",
            stats,
            historical_day,
            historical_day,
            False,
        )

        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(Order.objects.filter(order_number="HB-PROCESSED").count(), 1)

    def test_new_historical_email_creates_order_with_historical_order_date(self):
        historical_day = date(2026, 8, 5)
        command = BackfillCommand()
        stats = self._stats()
        mail = self._mail({b"8003": self._message("HB-HISTORICAL", historical_day)})

        command._process_message(
            mail, b"8003", stats, historical_day, historical_day, False
        )
        order = Order.objects.get(order_number="HB-HISTORICAL")

        self.assertEqual(stats["new"], 1)
        self.assertEqual(stats["orders"], 1)
        self.assertEqual(timezone.localtime(order.order_date).date(), historical_day)

        command._process_message(
            mail, b"8003", stats, historical_day, historical_day, False
        )
        self.assertEqual(Order.objects.filter(order_number="HB-HISTORICAL").count(), 1)
        self.assertEqual(IncomingEmail.objects.filter(message_id="8003").count(), 1)
        self.assertEqual(stats["skipped"], 1)

    def test_failed_email_does_not_stop_a_later_email(self):
        historical_day = date(2026, 8, 5)
        command = BackfillCommand()
        stats = self._stats()
        mail = self._mail(
            {
                b"8004": self._message("HB-BROKEN", historical_day, "Malformed order"),
                b"8005": self._message("HB-GOOD", historical_day),
            }
        )

        command._process_message(
            mail, b"8004", stats, historical_day, historical_day, False
        )
        command._process_message(
            mail, b"8005", stats, historical_day, historical_day, False
        )

        self.assertEqual(stats["failures"], 1)
        self.assertEqual(stats["orders"], 1)
        self.assertEqual(
            IncomingEmail.objects.get(message_id="8004").processing_status,
            IncomingEmail.ProcessingStatus.FAILED,
        )
        self.assertTrue(Order.objects.filter(order_number="HB-GOOD").exists())

    def test_dry_run_does_not_write_data(self):
        historical_day = date(2026, 8, 5)
        command = BackfillCommand()
        stats = self._stats()

        command._process_message(
            self._mail({b"8006": self._message("HB-DRY-RUN", historical_day)}),
            b"8006",
            stats,
            historical_day,
            historical_day,
            True,
        )

        self.assertEqual(stats["new"], 1)
        self.assertEqual(stats["orders"], 0)
        self.assertEqual(IncomingEmail.objects.count(), 0)
        self.assertEqual(Order.objects.count(), 0)

    def test_retry_selected_failed_message_uses_saved_body_without_duplicates(self):
        historical_day = date(2026, 8, 5)
        body = f"""
            Booking Date: {historical_day:%d %b %Y}, 09:30<br>
            Delivery Date: {historical_day:%d %b %Y}, 10:00<br>
            Customer Name : Retry Customer<br>
            Customer Contact : 9000000000<br>
            Invoice HB-RETRY / 2470000000<br>
            Payment: PRE_PAID<br>
            Coach / Berth: B2 / 36<br>
            Train: 12963 / MEWAR EXPRESS
            <table><tr><td>1</td><td>Veg Cheese Pizza</td><td></td><td>1</td><td>300.00</td><td>15.00</td><td>300.00</td></tr></table>
        """
        IncomingEmail.objects.create(
            message_id="8010",
            vendor=self.vendor,
            subject="Failed historical order",
            body=body,
            received_at=timezone.now(),
            processing_status=IncomingEmail.ProcessingStatus.FAILED,
            error_message="A valid payment mode is required to create an order.",
        )
        command = BackfillCommand()

        stats = command._retry_failed_messages(["8010"], False)
        repeat_stats = command._retry_failed_messages(["8010"], False)

        self.assertEqual(stats["orders"], 1)
        self.assertEqual(repeat_stats["orders"], 0)
        self.assertEqual(repeat_stats["skipped"], 1)
        self.assertEqual(Order.objects.filter(order_number="HB-RETRY").count(), 1)
        self.assertEqual(
            IncomingEmail.objects.get(message_id="8010").processing_status,
            IncomingEmail.ProcessingStatus.PROCESSED,
        )


class EmailClassificationCommandTests(TestCase):
    def setUp(self):
        self.vendor = Vendor.objects.create(
            name="RailRestro", email_address="no-reply@railrestro.com"
        )

    def _mail(self, message):
        class FakeMail:
            def uid(self, command, message_uid, query):
                if command == "fetch":
                    return "OK", [(b"message", message)]
                raise AssertionError(f"Unexpected IMAP command: {command}")

        return FakeMail()

    def _message(self, subject, message_day, body):
        message = EmailMessage()
        message["From"] = "RailRestro <no-reply@railrestro.com>"
        message["Subject"] = subject
        message["Date"] = message_day.strftime("%a, %d %b %Y 10:00:00 +0000")
        message.set_content(body, subtype="html")
        return message.as_bytes()

    def _backfill_stats(self):
        return {
            "checked": 1,
            "vendor_emails": 0,
            "new": 0,
            "existing_retried": 0,
            "orders": 0,
            "skipped": 0,
            "failures": 0,
        }

    def _live_stats(self):
        return {
            "checked": 1,
            "new": 0,
            "existing_retried": 0,
            "orders": 0,
            "skipped": 0,
            "failures": 0,
        }

    def test_backfill_skips_railrestro_status_update_without_creating_an_order(self):
        historical_day = date(2026, 8, 8)
        message = self._message(
            "Order Status Update for Order #5710417",
            historical_day,
            "Current Status: CANCELED",
        )
        stats = self._backfill_stats()

        BackfillCommand()._process_message(
            self._mail(message),
            b"9101",
            stats,
            historical_day,
            historical_day,
            False,
        )

        incoming_email = IncomingEmail.objects.get(message_id="9101")
        self.assertEqual(incoming_email.processing_status, IncomingEmail.ProcessingStatus.SKIPPED)
        self.assertEqual(incoming_email.error_message, "")
        self.assertIsNone(incoming_email.order_id)
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(stats["failures"], 0)

    def test_railrestro_new_order_subject_still_creates_an_order(self):
        historical_day = date(2026, 8, 8)
        body = """
            ORDER #: 5760540 Customer: Test Customer M. 9000000000
            TRAIN: 12963 / MEWAR EXPRESS
            Delivery Time: 2026-08-08 20:05:00
            Coact/Seat: B2-36
            Prepaid: Rs. 100
            Final Total: Rs. 100
            <table><tr><td>Test Meal</td><td>Rs. 100</td><td>1</td><td>Rs. 100</td></tr></table>
        """
        stats = self._backfill_stats()

        BackfillCommand()._process_message(
            self._mail(self._message("New Order #5760540 Received", historical_day, body)),
            b"9102",
            stats,
            historical_day,
            historical_day,
            False,
        )

        self.assertEqual(stats["orders"], 1)
        self.assertTrue(Order.objects.filter(order_number="5760540").exists())

    def test_live_poll_skips_railrestro_status_update_without_creating_an_order(self):
        today = timezone.localdate()
        message = self._message(
            "Order Status Update for Order #5710417",
            today,
            "Current Status: CANCELED",
        )
        stats = self._live_stats()

        Command()._process_message(self._mail(message), b"9103", stats)

        incoming_email = IncomingEmail.objects.get(message_id="9103")
        self.assertEqual(incoming_email.processing_status, IncomingEmail.ProcessingStatus.SKIPPED)
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(stats["failures"], 0)

    def test_railrestro_cancelled_body_is_classified_as_a_non_order(self):
        historical_day = date(2026, 8, 8)
        message = self._message(
            "Order Status Update for Order #5710417",
            historical_day,
            "Current Status: CANCELLED",
        )
        stats = self._backfill_stats()

        BackfillCommand()._process_message(
            self._mail(message),
            b"9104",
            stats,
            historical_day,
            historical_day,
            False,
        )

        self.assertEqual(
            IncomingEmail.objects.get(message_id="9104").processing_status,
            IncomingEmail.ProcessingStatus.SKIPPED,
        )
        self.assertEqual(Order.objects.count(), 0)
