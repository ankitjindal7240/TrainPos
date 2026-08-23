from datetime import date, datetime, time, timedelta
from email.message import EmailMessage
from decimal import Decimal
from io import BytesIO, StringIO
import json
import os
import socket
from types import SimpleNamespace
from urllib.error import HTTPError
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
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
from Orders.services.train_status import (
    TrainStatusError,
    get_dashboard_status,
    get_live_status_for_order,
    get_train_status,
    refresh_live_status_for_order,
)


class RailRadarResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class TrainStatusServiceTests(SimpleTestCase):
    journey_date = date(2026, 8, 23)

    def _payload(self, **target_overrides):
        target = {
            "stationCode": "GGC",
            "stationName": "Gangapur City Junction",
            "scheduledArrival": "2026-08-23T19:15:00+05:30",
            "expectedArrival": "2026-08-23T19:42:00+05:30",
            "delayArrival": 27,
            "status": "upcoming",
        }
        target.update(target_overrides)
        return {
            "success": True,
            "data": {
                "trainNumber": "12963",
                "status": "running",
                "delayMinutes": 27,
                "currentLocation": {"stationCode": "SWM"},
                "nextHalt": {"stationCode": "GGC", "stationName": "Gangapur City Junction"},
                "route": [target],
            },
        }

    def _get_status(self, mocked_urlopen):
        return get_train_status("12963", self.journey_date, "GGC")

    @patch.dict(os.environ, {"RAILRADAR_API_KEY": "test-key"}, clear=False)
    @patch("Orders.services.train_status.urlopen")
    def test_successfully_normalizes_target_station_response(self, mocked_urlopen):
        mocked_urlopen.return_value = RailRadarResponse(self._payload())

        status = self._get_status(mocked_urlopen)

        self.assertEqual(status["train_number"], "12963")
        self.assertEqual(status["target_station"], "GGC")
        self.assertEqual(status["journey_date"], "2026-08-23")
        self.assertEqual(status["scheduled_arrival"], "2026-08-23T19:15:00+05:30")
        self.assertEqual(status["expected_arrival"], "2026-08-23T19:42:00+05:30")
        self.assertEqual(status["delay_minutes"], 27)
        self.assertEqual(status["current_location"], "SWM")
        self.assertEqual(status["next_station"], "Gangapur City Junction")
        self.assertEqual(status["provider"], "RailRadar")
        self.assertTrue(status["raw_available"])
        request = mocked_urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        self.assertIn("date=2026-08-23", request.full_url)
        self.assertIn("haltsOnly=true", request.full_url)
        self.assertEqual(mocked_urlopen.call_args.kwargs["timeout"], 10)

    @patch.dict(os.environ, {"RAILRADAR_API_KEY": "test-key"}, clear=False)
    @patch("Orders.services.train_status.urlopen")
    def test_missing_eta_is_returned_as_none(self, mocked_urlopen):
        mocked_urlopen.return_value = RailRadarResponse(
            self._payload(expectedArrival=None, actualArrival=None)
        )

        status = self._get_status(mocked_urlopen)

        self.assertIsNone(status["expected_arrival"])
        self.assertEqual(status["scheduled_arrival"], "2026-08-23T19:15:00+05:30")

    @patch.dict(os.environ, {"RAILRADAR_API_KEY": "test-key"}, clear=False)
    @patch("Orders.services.train_status.urlopen")
    def test_401_is_reported_as_invalid_api_key(self, mocked_urlopen):
        mocked_urlopen.side_effect = _http_error(401)

        self._assert_error("INVALID_API_KEY")

    @patch.dict(os.environ, {"RAILRADAR_API_KEY": "test-key"}, clear=False)
    @patch("Orders.services.train_status.urlopen")
    def test_404_is_reported_as_train_not_available(self, mocked_urlopen):
        mocked_urlopen.side_effect = _http_error(404)

        self._assert_error("TRAIN_NOT_AVAILABLE")

    @patch.dict(os.environ, {"RAILRADAR_API_KEY": "test-key"}, clear=False)
    @patch("Orders.services.train_status.urlopen")
    def test_429_is_reported_as_rate_limited(self, mocked_urlopen):
        mocked_urlopen.side_effect = _http_error(429)

        self._assert_error("RATE_LIMITED")

    @patch.dict(os.environ, {"RAILRADAR_API_KEY": "test-key"}, clear=False)
    @patch("Orders.services.train_status.urlopen")
    def test_503_is_reported_as_unavailable(self, mocked_urlopen):
        mocked_urlopen.side_effect = _http_error(503)

        self._assert_error("UNAVAILABLE")

    @patch.dict(os.environ, {"RAILRADAR_API_KEY": "test-key"}, clear=False)
    @patch("Orders.services.train_status.urlopen")
    def test_timeout_is_reported_without_leaking_transport_details(self, mocked_urlopen):
        mocked_urlopen.side_effect = socket.timeout()

        self._assert_error("TIMEOUT")

    @patch.dict(os.environ, {"RAILRADAR_API_KEY": "test-key"}, clear=False)
    @patch("Orders.services.train_status.urlopen")
    def test_malformed_response_is_controlled(self, mocked_urlopen):
        mocked_urlopen.return_value = RailRadarResponse({"success": True, "data": {}})

        self._assert_error("MALFORMED_RESPONSE")

    def _assert_error(self, expected_code):
        with self.assertRaises(TrainStatusError) as context:
            get_train_status("12963", self.journey_date, "GGC")
        self.assertEqual(context.exception.code, expected_code)


class TrainRunResolutionTests(SimpleTestCase):
    operational_date = date(2026, 8, 23)

    def setUp(self):
        cache.clear()

    def _order(self, train_number="19037", journey_date=None):
        return SimpleNamespace(
            order_date=timezone.make_aware(datetime(2026, 8, 23, 12, 0)),
            train_journey_date=journey_date,
            train=SimpleNamespace(train_number=train_number),
        )

    def _status(self, journey_date, **overrides):
        status = {
            "train_number": "19037",
            "target_station": "GGC",
            "scheduled_arrival": f"{journey_date.isoformat()}T15:00:00+05:30",
            "expected_arrival": f"{journey_date.isoformat()}T15:34:00+05:30",
            "delay_minutes": 34,
            "current_location": "SWM",
            "next_station": "GGC",
            "status": "running",
            "target_status": "upcoming",
            "provider": "RailRadar",
            "available": True,
            "raw_available": True,
        }
        status.update(overrides)
        return status

    def test_same_day_run_is_selected_when_ggc_passage_is_today(self):
        calls = []

        def provider(train_number, journey_date, target_station):
            calls.append(journey_date)
            return self._status(journey_date)

        status = get_live_status_for_order(self._order(), provider=provider)

        self.assertTrue(status["available"])
        self.assertEqual(status["journey_date"], "2026-08-23")
        self.assertEqual(calls, [date(2026, 8, 23), date(2026, 8, 22)])

    def test_19037_previous_day_regression_selects_22_aug_run(self):
        def provider(train_number, journey_date, target_station):
            if journey_date == date(2026, 8, 23):
                return self._status(
                    date(2026, 8, 24),
                    status="not-started",
                    expected_arrival=None,
                )
            return self._status(date(2026, 8, 23))

        status = get_live_status_for_order(self._order("19037"), provider=provider)

        self.assertTrue(status["available"])
        self.assertEqual(status["journey_date"], "2026-08-22")
        self.assertEqual(status["expected_arrival"], "2026-08-23T15:34:00+05:30")

    def test_explicit_vendor_journey_date_is_preferred(self):
        calls = []

        def provider(train_number, journey_date, target_station):
            calls.append(journey_date)
            return self._status(date(2026, 8, 23))

        status = get_live_status_for_order(
            self._order(journey_date=date(2026, 8, 22)), provider=provider
        )

        self.assertTrue(status["available"])
        self.assertEqual(status["journey_date"], "2026-08-22")
        self.assertEqual(calls, [date(2026, 8, 22)])

    def test_d_minus_two_is_used_only_when_the_first_two_do_not_match(self):
        def provider(train_number, journey_date, target_station):
            passage_date = (
                date(2026, 8, 23)
                if journey_date == date(2026, 8, 21)
                else date(2026, 8, 24)
            )
            return self._status(passage_date)

        status = get_live_status_for_order(self._order(), provider=provider)

        self.assertTrue(status["available"])
        self.assertEqual(status["journey_date"], "2026-08-21")

    def test_ambiguous_runs_are_not_shown(self):
        def provider(train_number, journey_date, target_station):
            return self._status(date(2026, 8, 23))

        status = get_live_status_for_order(self._order(), provider=provider)

        self.assertFalse(status["available"])
        self.assertEqual(status["reason"], "AMBIGUOUS_RUN")

    def test_identical_train_run_uses_one_cached_provider_lookup(self):
        calls = []

        def provider(train_number, journey_date, target_station):
            calls.append((train_number, journey_date, target_station))
            return self._status(date(2026, 8, 23))

        first = self._order(journey_date=date(2026, 8, 22))
        second = self._order(journey_date=date(2026, 8, 22))
        get_live_status_for_order(first, provider=provider)
        get_live_status_for_order(second, provider=provider)

        self.assertEqual(calls, [("19037", date(2026, 8, 22), "GGC")])

    def test_manual_refresh_forces_lookup_and_replaces_cached_eta_for_same_run(self):
        calls = []

        def initial_provider(train_number, journey_date, target_station):
            calls.append("initial")
            return self._status(
                date(2026, 8, 23), expected_arrival="2026-08-23T15:00:00+05:30"
            )

        def refreshed_provider(train_number, journey_date, target_station):
            calls.append("refresh")
            return self._status(
                date(2026, 8, 23), expected_arrival="2026-08-23T15:34:00+05:30"
            )

        first = self._order(journey_date=date(2026, 8, 22))
        second = self._order(journey_date=date(2026, 8, 22))
        get_live_status_for_order(first, provider=initial_provider)
        refreshed = refresh_live_status_for_order(
            first, date(2026, 8, 22), provider=refreshed_provider
        )
        shared = get_live_status_for_order(second, provider=initial_provider)

        self.assertEqual(calls, ["initial", "refresh"])
        self.assertEqual(refreshed["expected_arrival"], "2026-08-23T15:34:00+05:30")
        self.assertEqual(shared["expected_arrival"], "2026-08-23T15:34:00+05:30")

    def test_failed_manual_refresh_keeps_last_known_cached_status(self):
        def working_provider(train_number, journey_date, target_station):
            return self._status(
                date(2026, 8, 23), expected_arrival="2026-08-23T15:00:00+05:30"
            )

        def failing_provider(train_number, journey_date, target_station):
            raise TrainStatusError("TIMEOUT", "Timed out")

        order = self._order(journey_date=date(2026, 8, 22))
        original = get_live_status_for_order(order, provider=working_provider)
        failed = refresh_live_status_for_order(
            order, date(2026, 8, 22), provider=failing_provider
        )
        cached = get_live_status_for_order(order, provider=failing_provider)

        self.assertFalse(failed["available"])
        self.assertEqual(original["expected_arrival"], cached["expected_arrival"])
        self.assertEqual(cached["expected_arrival"], "2026-08-23T15:00:00+05:30")

    def test_manual_refresh_does_not_change_a_different_train_run(self):
        def provider(train_number, journey_date, target_station):
            eta = "2026-08-23T15:00:00+05:30" if train_number == "19037" else "2026-08-23T16:00:00+05:30"
            return self._status(date(2026, 8, 23), train_number=train_number, expected_arrival=eta)

        first = self._order("19037", date(2026, 8, 22))
        second = self._order("12963", date(2026, 8, 22))
        get_live_status_for_order(first, provider=provider)
        second_original = get_live_status_for_order(second, provider=provider)

        def refreshed_first_provider(train_number, journey_date, target_station):
            return self._status(
                date(2026, 8, 23),
                train_number=train_number,
                expected_arrival="2026-08-23T15:34:00+05:30",
            )

        refresh_live_status_for_order(
            first, date(2026, 8, 22), provider=refreshed_first_provider
        )
        second_after_refresh = get_live_status_for_order(second, provider=provider)

        self.assertEqual(second_original["expected_arrival"], "2026-08-23T16:00:00+05:30")
        self.assertEqual(second_after_refresh["expected_arrival"], "2026-08-23T16:00:00+05:30")

    def test_different_trains_use_separate_cache_entries(self):
        calls = []

        def provider(train_number, journey_date, target_station):
            calls.append(train_number)
            return self._status(date(2026, 8, 23), train_number=train_number)

        get_live_status_for_order(
            self._order("19037", date(2026, 8, 22)), provider=provider
        )
        get_live_status_for_order(
            self._order("12963", date(2026, 8, 22)), provider=provider
        )

        self.assertEqual(calls, ["19037", "12963"])

    def test_provider_timeout_becomes_an_unavailable_result(self):
        def provider(train_number, journey_date, target_station):
            raise TrainStatusError("TIMEOUT", "Timed out")

        status = get_live_status_for_order(self._order(), provider=provider)

        self.assertFalse(status["available"])
        self.assertEqual(status["reason"], "RUN_NOT_RESOLVED")

    def test_dashboard_urgency_states_and_arrived_state(self):
        now = timezone.make_aware(datetime(2026, 8, 23, 14, 10))
        status = self._status(
            date(2026, 8, 23), expected_arrival="2026-08-23T15:30:00+05:30"
        )
        self.assertEqual(get_dashboard_status(status, now)["urgency"], "NORMAL")

        status["expected_arrival"] = "2026-08-23T15:00:00+05:30"
        self.assertEqual(get_dashboard_status(status, now)["urgency"], "APPROACHING")

        status["expected_arrival"] = "2026-08-23T14:30:00+05:30"
        self.assertEqual(get_dashboard_status(status, now)["urgency"], "URGENT")

        status["target_status"] = "departed"
        self.assertEqual(get_dashboard_status(status, now)["display_state"], "ARRIVED")

    def test_running_and_not_started_dashboard_states_include_the_right_eta(self):
        running = get_dashboard_status(
            self._status(
                date(2026, 8, 23), expected_arrival="2026-08-23T15:34:00+05:30"
            ),
            timezone.make_aware(datetime(2026, 8, 23, 14, 10)),
        )
        self.assertEqual(running["display_state"], "LIVE")
        self.assertEqual(running["expected_arrival_display"], "3:34 PM")

        not_started = get_dashboard_status(
            self._status(
                date(2026, 8, 23), status="not-started", expected_arrival=None
            )
        )
        self.assertEqual(not_started["display_state"], "NOT_STARTED")
        self.assertEqual(not_started["scheduled_arrival_display"], "3:00 PM")


def _http_error(status_code):
    return HTTPError(
        "https://api.railradar.in/v1/trains/12963/live",
        status_code,
        "Provider error",
        hdrs=None,
        fp=BytesIO(),
    )


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

        unavailable_status = {
            "train_number": "12963",
            "target_station": "GGC",
            "journey_date": None,
            "scheduled_arrival": None,
            "expected_arrival": None,
            "delay_minutes": None,
            "current_location": None,
            "next_station": None,
            "status": None,
            "target_status": None,
            "provider": "RailRadar",
            "available": False,
            "raw_available": False,
            "reason": "TIMEOUT",
        }
        with patch("Orders.views.get_live_status_for_order", return_value=unavailable_status):
            response = self.client.get(reverse("order_list"))

        self.assertContains(response, "TODAY-ORDER")
        self.assertNotContains(response, "OLDER-ORDER")
        self.assertContains(response, "UNKNOWN")
        self.assertEqual(response.context["summary"]["total_orders"], 1)

    def test_dashboard_top_date_uses_djangos_current_local_date(self):
        with patch("Orders.views.get_live_status_for_order", return_value={
            "available": False,
            "train_number": "12963",
            "target_station": "GGC",
            "journey_date": None,
        }):
            response = self.client.get(reverse("order_list"))

        self.assertContains(response, timezone.localdate().strftime("%d %b %Y"))
        self.assertNotContains(response, "12 Aug 2026 - 13 Aug 2026")

    def test_expanded_details_show_items_and_payment_aware_stored_financials_only(self):
        cod_email = self._email("RailRestro", "expanded-cod")
        cod_order = create_order_from_incoming_email(
            cod_email, self._data("EXPANDED-COD", "CASH_ON_DELIVERY")
        )
        cod_order.order_date = timezone.now()
        cod_order.save(update_fields=["order_date"])

        prepaid_email = self._email("HomeBytes", "expanded-prepaid")
        prepaid_order = create_order_from_incoming_email(
            prepaid_email, self._data("EXPANDED-PREPAID", "PRE_PAID")
        )
        prepaid_order.order_date = timezone.now()
        prepaid_order.save(update_fields=["order_date"])

        unavailable_status = {
            "available": False,
            "train_number": "12963",
            "target_station": "GGC",
            "journey_date": None,
            "scheduled_arrival": None,
            "expected_arrival": None,
            "delay_minutes": None,
            "current_location": None,
            "next_station": None,
            "status": None,
            "target_status": None,
            "provider": "RailRadar",
            "raw_available": False,
            "reason": "TIMEOUT",
        }
        with patch("Orders.views.get_live_status_for_order", return_value=unavailable_status):
            response = self.client.get(reverse("order_list"))

        self.assertContains(response, "Veg Cheese Pizza")
        self.assertContains(response, "Amount Summary")
        self.assertContains(response, "Delivery Charge")
        self.assertContains(response, "Amount To Collect")
        self.assertContains(response, "Prepaid / Paid")
        self.assertContains(response, "₹315.00")
        self.assertNotContains(response, '<h2>Customer</h2>', html=True)
        self.assertNotContains(response, '<h2>Journey</h2>', html=True)

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

    def test_manual_train_status_endpoint_returns_refreshed_json_without_writing_order(self):
        incoming_email = self._email("RailRestro", "manual-refresh-order")
        order = create_order_from_incoming_email(
            incoming_email,
            self._data("MANUAL-REFRESH", "CASH_ON_DELIVERY"),
        )
        fresh_status = {
            "train_number": "12963",
            "target_station": "GGC",
            "journey_date": "2026-08-11",
            "scheduled_arrival": "2026-08-11T15:00:00+05:30",
            "expected_arrival": "2026-08-11T15:34:00+05:30",
            "delay_minutes": 34,
            "current_location": "SWM",
            "next_station": "GGC",
            "status": "running",
            "target_status": "upcoming",
            "provider": "RailRadar",
            "available": True,
            "raw_available": True,
            "fetched_at": timezone.now().isoformat(),
        }

        with patch(
            "Orders.views.refresh_live_status_for_order", return_value=fresh_status
        ):
            response = self.client.post(
                reverse("order_train_status_refresh", args=[order.pk]),
                {"journey_date": "2026-08-11"},
            )
            unchanged_response = self.client.post(
                reverse("order_train_status_refresh", args=[order.pk]),
                {"journey_date": "2026-08-11"},
            )

        order.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(unchanged_response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertTrue(unchanged_response.json()["ok"])
        self.assertEqual(
            response.json()["status"]["expected_arrival"],
            unchanged_response.json()["status"]["expected_arrival"],
        )
        self.assertEqual(response.json()["status"]["expected_arrival"], "3:34 PM")
        self.assertEqual(
            set(response.json()["status"]),
            {
                "display_state",
                "urgency",
                "scheduled_arrival",
                "expected_arrival",
                "delay_minutes",
                "arriving_in",
                "updated_label",
                "journey_date",
                "fetched_at",
                "current_location",
                "next_station",
            },
        )
        self.assertEqual(order.order_number, "MANUAL-REFRESH")

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
            2026-08-20 15:00
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
        self.assertEqual(
            parse_railrecipe_email(railrecipe_body)["train_journey_date"],
            "2026-08-20",
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

    def test_railrestro_prepaid_paid_total_becomes_order_total_and_reports_online_value(self):
        body = """
            ORDER #: 5773443 Customer: Test Customer M. 9000000000
            TRAIN: 20178 / TRAIN EXPRESS
            Delivery Time: 2026-08-23 21:05:00
            Coact/Seat: B4-29
            <table>
              <tr><td>Veg Premium Thali</td><td>Rs. 443</td><td>2</td><td>Rs. 886</td></tr>
              <tr><td>Choco Lava Cake</td><td>Rs. 72</td><td>1</td><td>Rs. 72</td></tr>
            </table>
            GST: Rs. 47.9 Subtotal: Rs. 1005.9 Extra Charges: Rs. 0
            Cashback: Rs. 0.00 Prepaid: Rs. 1005.9 Paid Total: Rs. 1005.9
            (Amount to collect) Rs. 0/-
        """
        parsed = parse_railrestro_email(body)
        incoming_email = self._email("RailRestro", "railrestro-prepaid-total")
        incoming_email.body = body
        incoming_email.save(update_fields=["body"])

        order = create_order_from_incoming_email(incoming_email, parsed)
        order.order_date = timezone.now()
        order.save(update_fields=["order_date"])

        self.assertEqual(parsed["payment_mode"], Order.PaymentMode.PRE_PAID)
        self.assertEqual(parsed["amount_to_collect"], Decimal("0"))
        self.assertEqual(parsed["total"], Decimal("1005.9"))
        self.assertEqual(order.total, Decimal("1005.90"))
        self.assertEqual(order.subtotal, Decimal("1005.90"))

        paid_total_only_body = """
            ORDER #: 5773883 Customer: Test Customer M. 9000000001
            TRAIN: 20178 / TRAIN EXPRESS
            Delivery Time: 2026-08-23 22:05:00
            Coact/Seat: B4-30
            <table><tr><td>Paneer Fried Rice</td><td>Rs. 268</td><td>2</td><td>Rs. 536</td></tr></table>
            Total: Rs. 536 GST: Rs. 26.8 Subtotal: Rs. 562.8
            Extra Charges: Rs. 0 Cashback: Rs. 0.00
            Paid Total: Rs. 562.8 (Amount to collect) Rs. 0/-
        """
        paid_total_only = parse_railrestro_email(paid_total_only_body)
        paid_total_email = self._email("RailRestro", "railrestro-paid-total-only")
        paid_total_email.body = paid_total_only_body
        paid_total_email.save(update_fields=["body"])
        paid_total_order = create_order_from_incoming_email(
            paid_total_email, paid_total_only
        )
        paid_total_order.order_date = timezone.now()
        paid_total_order.save(update_fields=["order_date"])

        self.assertEqual(paid_total_only["payment_mode"], Order.PaymentMode.PRE_PAID)
        self.assertIsNone(paid_total_only["advance"])
        self.assertEqual(paid_total_only["amount_to_collect"], Decimal("0"))
        self.assertEqual(paid_total_only["subtotal"], Decimal("562.8"))
        self.assertEqual(paid_total_only["gst"], Decimal("26.8"))
        self.assertEqual(paid_total_only["total"], Decimal("562.8"))
        self.assertEqual(paid_total_order.total, Decimal("562.80"))
        self.assertEqual(paid_total_order.subtotal, Decimal("562.80"))

        response = self.client.get(reverse("reports"))
        self.assertEqual(response.context["summary"]["online_value"], Decimal("1568.70"))

    def test_railrestro_cod_keeps_collection_total_when_no_final_total_label_exists(self):
        body = """
            ORDER #: COD-100 Customer: Test Customer M. 9000000000
            TRAIN: 20178 / TRAIN EXPRESS
            Delivery Time: 2026-08-23 21:05:00
            Coact/Seat: B4-29
            (Amount to collect) Rs. 837.90
        """

        parsed = parse_railrestro_email(body)

        self.assertEqual(parsed["payment_mode"], Order.PaymentMode.CASH_ON_DELIVERY)
        self.assertEqual(parsed["total"], Decimal("837.90"))

    def test_repair_command_updates_only_existing_zero_total_railrestro_prepaid_order(self):
        body = """
            ORDER #: RR-REPAIR-1 Customer: Test Customer M. 9000000000
            TRAIN: 20178 / TRAIN EXPRESS
            Delivery Time: 2026-08-23 21:05:00
            Coact/Seat: B4-29
            <table><tr><td>Veg Premium Thali</td><td>Rs. 443</td><td>2</td><td>Rs. 886</td></tr></table>
            GST: Rs. 47.9 Subtotal: Rs. 1005.9 Prepaid: Rs. 1005.9
            Paid Total: Rs. 1005.9 (Amount to collect) Rs. 0/-
        """
        incoming_email = self._email("RailRestro", "repair-railrestro-prepaid")
        incoming_email.body = body
        incoming_email.save(update_fields=["body"])
        order = create_order_from_incoming_email(
            incoming_email, parse_railrestro_email(body)
        )
        order.total = Decimal("0.00")
        order.subtotal = Decimal("0.00")
        order.save(update_fields=["total", "subtotal"])

        output = StringIO()
        call_command(
            "repair_railrestro_prepaid_totals",
            "--order-number",
            order.order_number,
            "--apply",
            stdout=output,
        )

        order.refresh_from_db()
        incoming_email.refresh_from_db()
        self.assertEqual(order.total, Decimal("1005.90"))
        self.assertEqual(order.subtotal, Decimal("1005.90"))
        self.assertEqual(Order.objects.filter(order_number="RR-REPAIR-1").count(), 1)
        self.assertEqual(incoming_email.order_id, order.id)

    def test_repair_command_dry_run_leaves_correct_total_and_zero_subtotal_unchanged(self):
        body = """
            ORDER #: RR-SUBTOTAL-DRY Customer: Test Customer M. 9000000000
            TRAIN: 20178 / TRAIN EXPRESS
            Delivery Time: 2026-08-23 21:05:00
            Coact/Seat: B4-29
            <table><tr><td>Paneer Fried Rice</td><td>Rs. 268</td><td>2</td><td>Rs. 536</td></tr></table>
            GST: Rs. 26.8 Subtotal: Rs. 562.8
            Paid Total: Rs. 562.8 (Amount to collect) Rs. 0/-
        """
        incoming_email = self._email("RailRestro", "repair-railrestro-subtotal-dry")
        incoming_email.body = body
        incoming_email.save(update_fields=["body"])
        order = create_order_from_incoming_email(
            incoming_email, parse_railrestro_email(body)
        )
        order.subtotal = Decimal("0.00")
        order.save(update_fields=["subtotal"])

        output = StringIO()
        call_command(
            "repair_railrestro_prepaid_totals",
            "--order-number",
            order.order_number,
            stdout=output,
        )

        order.refresh_from_db()
        incoming_email.refresh_from_db()
        self.assertEqual(order.total, Decimal("562.80"))
        self.assertEqual(order.subtotal, Decimal("0.00"))
        self.assertIn(
            "RR-SUBTOTAL-DRY | ₹0.00 | ₹562.80 | ₹562.80 | "
            "eligible: repair subtotal only",
            output.getvalue(),
        )

    def test_repair_command_repairs_only_subtotal_when_total_is_correct(self):
        body = """
            ORDER #: RR-SUBTOTAL-APPLY Customer: Test Customer M. 9000000000
            TRAIN: 20178 / TRAIN EXPRESS
            Delivery Time: 2026-08-23 21:05:00
            Coact/Seat: B4-29
            <table><tr><td>Paneer Fried Rice</td><td>Rs. 268</td><td>2</td><td>Rs. 536</td></tr></table>
            GST: Rs. 26.8 Subtotal: Rs. 562.8
            Paid Total: Rs. 562.8 (Amount to collect) Rs. 0/-
        """
        incoming_email = self._email("RailRestro", "repair-railrestro-subtotal-apply")
        incoming_email.body = body
        incoming_email.save(update_fields=["body"])
        order = create_order_from_incoming_email(
            incoming_email, parse_railrestro_email(body)
        )
        order.subtotal = Decimal("0.00")
        order.gst = Decimal("1.00")
        order.discount = Decimal("2.00")
        order.delivery_charge = Decimal("3.00")
        order.save(update_fields=["subtotal", "gst", "discount", "delivery_charge"])

        call_command(
            "repair_railrestro_prepaid_totals",
            "--order-number",
            order.order_number,
            "--apply",
        )

        order.refresh_from_db()
        incoming_email.refresh_from_db()
        self.assertEqual(order.total, Decimal("562.80"))
        self.assertEqual(order.subtotal, Decimal("562.80"))
        self.assertEqual(order.gst, Decimal("1.00"))
        self.assertEqual(order.discount, Decimal("2.00"))
        self.assertEqual(order.delivery_charge, Decimal("3.00"))
        self.assertEqual(Order.objects.filter(order_number="RR-SUBTOTAL-APPLY").count(), 1)
        self.assertEqual(incoming_email.order_id, order.id)

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


class OrderDashboardVersionTests(TestCase):
    def setUp(self):
        self.vendor = Vendor.objects.create(
            name="Dashboard Vendor", email_address="dashboard@example.com"
        )
        self.customer = Customer.objects.create(name="Dashboard Customer", phone="9000000000")
        self.train = Train.objects.create(train_number="12963", train_name="MEWAR EXPRESS")

    def _order(self, number, order_date=None):
        return Order.objects.create(
            vendor=self.vendor,
            order_number=number,
            customer=self.customer,
            train=self.train,
            order_date=order_date or timezone.now(),
            payment_mode=Order.PaymentMode.PRE_PAID,
            total=Decimal("100.00"),
        )

    def _version(self):
        response = self.client.get(reverse("order_dashboard_version"))
        self.assertEqual(response.status_code, 200)
        return response.json()

    def _live_status(self):
        return {
            "train_number": "12963",
            "target_station": "GGC",
            "journey_date": timezone.localdate().isoformat(),
            "scheduled_arrival": f"{timezone.localdate().isoformat()}T15:00:00+05:30",
            "expected_arrival": f"{timezone.localdate().isoformat()}T15:34:00+05:30",
            "delay_minutes": 34,
            "current_location": "SWM",
            "next_station": "GGC",
            "status": "running",
            "target_status": "upcoming",
            "provider": "RailRadar",
            "available": True,
            "raw_available": True,
            "fetched_at": timezone.now().isoformat(),
        }

    def test_endpoint_returns_the_current_today_scoped_dashboard_version(self):
        order = self._order("VERSION-ONE")

        version = self._version()

        self.assertEqual(version["date"], timezone.localdate().isoformat())
        self.assertEqual(version["order_count"], 1)
        self.assertEqual(version["latest_order_id"], order.id)
        self.assertEqual(version["token"], f"{version['date']}:1:{order.id}")

    def test_new_orders_change_the_token_and_multiple_new_orders_change_it_once(self):
        initial = self._version()
        self._order("VERSION-TWO")
        self._order("VERSION-THREE")

        updated = self._version()

        self.assertEqual(initial["order_count"], 0)
        self.assertEqual(updated["order_count"], 2)
        self.assertNotEqual(initial["token"], updated["token"])

    def test_status_change_does_not_change_the_new_order_token(self):
        order = self._order("STATUS-ONLY")
        before = self._version()

        order.status = Order.Status.PREPARING
        order.save(update_fields=["status"])
        after = self._version()

        self.assertEqual(before["token"], after["token"])

    def test_yesterdays_order_is_excluded_from_todays_version(self):
        self._order("YESTERDAY", timezone.now() - timedelta(days=1))
        today_order = self._order("TODAY")

        version = self._version()

        self.assertEqual(version["order_count"], 1)
        self.assertEqual(version["latest_order_id"], today_order.id)

    def test_version_endpoint_does_not_call_railradar_or_gmail(self):
        with patch("Orders.views.get_live_status_for_order") as live_status, patch(
            "Orders.management.commands.poll_gmail_orders.imaplib.IMAP4_SSL"
        ) as gmail_connection:
            response = self.client.get(reverse("order_dashboard_version"))

        self.assertEqual(response.status_code, 200)
        live_status.assert_not_called()
        gmail_connection.assert_not_called()

    def test_dashboard_remains_available_without_a_version_endpoint_request(self):
        self._order("NORMAL-DASHBOARD")
        unavailable_status = {
            "available": False,
            "train_number": "12963",
            "target_station": "GGC",
            "journey_date": None,
            "scheduled_arrival": None,
            "expected_arrival": None,
            "delay_minutes": None,
            "current_location": None,
            "next_station": None,
            "status": None,
            "target_status": None,
            "provider": "RailRadar",
            "raw_available": False,
            "reason": "TIMEOUT",
        }
        with patch("Orders.views.get_live_status_for_order", return_value=unavailable_status):
            response = self.client.get(reverse("order_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "NORMAL-DASHBOARD")

    def test_same_train_run_shares_live_lookup_while_rendering_eta_columns_per_order(self):
        self._order("SHARED-ONE")
        self._order("SHARED-TWO")
        with patch("Orders.views.get_live_status_for_order", return_value=self._live_status()) as lookup:
            response = self.client.get(reverse("order_list"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(lookup.call_count, 1)
        self.assertEqual(response.content.count(b"data-live-eta"), 2)
        self.assertContains(response, "ETA GGC")
        self.assertNotContains(response, "Live Train Status")

    def test_different_train_runs_render_dedicated_live_columns_and_clean_train_info(self):
        self._order("TRAIN-ONE")
        other_train = Train.objects.create(train_number="19037", train_name="AVADH EXPRESS")
        Order.objects.create(
            vendor=self.vendor,
            order_number="TRAIN-TWO",
            customer=self.customer,
            train=other_train,
            order_date=timezone.now(),
            payment_mode=Order.PaymentMode.PRE_PAID,
            total=Decimal("100.00"),
        )
        with patch("Orders.views.get_live_status_for_order", return_value=self._live_status()):
            response = self.client.get(reverse("order_list"))

        self.assertEqual(response.content.count(b"data-live-eta"), 2)
        self.assertContains(response, "12963 - MEWAR EXPRESS")
        self.assertContains(response, "19037 - AVADH EXPRESS")
        self.assertNotContains(response, "Expected GGC:")
        self.assertContains(response, "Arriving In")


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
