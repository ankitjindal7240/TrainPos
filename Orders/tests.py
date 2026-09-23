from datetime import date, datetime, time, timedelta
from email.message import EmailMessage
from decimal import Decimal
from io import BytesIO, StringIO
import json
import imaplib
import os
import socket
from types import SimpleNamespace
from urllib.error import HTTPError
from unittest.mock import patch

from cryptography.fernet import Fernet

from django.core.management import call_command
from django.core.management.base import CommandError
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.template.loader import get_template
from django.urls import reverse
from django.utils import timezone

from Orders.models import (
    Customer,
    IncomingEmail,
    Order,
    Restaurant,
    RestaurantEmailConnection,
    RestaurantMembership,
    Train,
    Vendor,
    add_calendar_year,
)
from Orders.management.commands.poll_gmail_orders import Command
from Orders.management.commands.backfill_gmail_orders import Command as BackfillCommand
from Orders.parsers.homebytes import parse_homebytes_email
from Orders.parsers.demo import parse_demo_email
from Orders.parsers.railrecipe import parse_railrecipe_email
from Orders.parsers.railrestro import parse_railrestro_email
from Orders.parsers.rajbhog_khana import parse_rajbhog_khana_email
from Orders.services.order_creation import create_order_from_incoming_email
from Orders.services.credentials import encrypt_app_password
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


class RestaurantCredentialSettingsTests(SimpleTestCase):
    @override_settings(RESTAURANT_CREDENTIAL_ENCRYPTION_KEY="")
    def test_missing_credential_encryption_key_fails_safely(self):
        with self.assertRaises(ImproperlyConfigured):
            encrypt_app_password("app-password")


@override_settings(TRAINPOS_SITE_URL="https://trainpos.in")
class PublicSeoTests(SimpleTestCase):
    def test_landing_page_has_required_content_and_ctas(self):
        response = self.client.get(reverse("landing"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Train Food Order Management Software for Railway Restaurants",
        )
        self.assertContains(response, "₹5,999/year")
        self.assertContains(response, "15-day free trial")
        self.assertContains(response, reverse("signup"))
        self.assertContains(response, reverse("login"))
        self.assertContains(response, 'rel="canonical" href="https://trainpos.in/"')
        self.assertContains(response, 'application/ld+json')

    def test_robots_and_sitemap_expose_only_public_homepage(self):
        robots = self.client.get(reverse("robots_txt"))
        sitemap = self.client.get(reverse("sitemap_xml"))

        self.assertEqual(robots.status_code, 200)
        self.assertContains(robots, "Disallow: /orders/")
        self.assertContains(robots, "Sitemap: https://trainpos.in/sitemap.xml")
        self.assertEqual(sitemap.status_code, 200)
        self.assertContains(sitemap, "https://trainpos.in/")

    def test_login_is_not_indexable(self):
        response = self.client.get(reverse("login"))

        self.assertContains(response, "<title>Log in to TrainPOS</title>")
        self.assertContains(self.client.get(reverse("robots_txt")), "Disallow: /login/")


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
        self.restaurant = Restaurant.objects.get(slug="food-costa")
        self.user = get_user_model().objects.create_user(username="food-costa-owner")
        RestaurantMembership.objects.create(
            user=self.user,
            restaurant=self.restaurant,
            role=RestaurantMembership.Role.OWNER,
        )
        self.client.force_login(self.user)
        self.vendors = {
            "RailRestro": Vendor.objects.create(
                restaurant=self.restaurant,
                name="RailRestro", email_address="no-reply@railrestro.com",
                parser_type=Vendor.ParserType.RAILRESTRO,
            ),
            "HomeBytes": Vendor.objects.create(
                restaurant=self.restaurant,
                name="HomeBytes", email_address="info@homebytes.co.in",
                parser_type=Vendor.ParserType.HOMEBYTES,
            ),
            "Rajbhog Khana": Vendor.objects.create(
                restaurant=self.restaurant,
                name="Rajbhog Khana", email_address="orders@rajbhogkhana.com",
                parser_type=Vendor.ParserType.RAJBHOG,
            ),
            "RailRecipe": Vendor.objects.create(
                restaurant=self.restaurant,
                name="RailRecipe", email_address="no-reply@railrecipe.com",
                parser_type=Vendor.ParserType.RAILRECIPE,
            ),
        }

    def _email(self, vendor_name, message_id):
        return IncomingEmail.objects.create(
            restaurant=self.restaurant,
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

    def test_duplicate_business_order_links_both_emails_without_duplicate_items(self):
        first_email = self._email("Rajbhog Khana", "rajbhog-duplicate-one")
        second_email = self._email("Rajbhog Khana", "rajbhog-duplicate-two")
        data = self._data("RBK001733571", "CASH_ON_DELIVERY")

        first_order = create_order_from_incoming_email(first_email, data)
        second_order = create_order_from_incoming_email(second_email, data)
        repeated_order = create_order_from_incoming_email(second_email, data)

        first_email.refresh_from_db()
        second_email.refresh_from_db()
        self.assertEqual(first_order.pk, second_order.pk)
        self.assertEqual(second_order.pk, repeated_order.pk)
        self.assertEqual(
            Order.objects.filter(
                vendor=self.vendors["Rajbhog Khana"], order_number="RBK001733571"
            ).count(),
            1,
        )
        self.assertEqual(first_order.items.count(), 1)
        self.assertEqual(first_email.order_id, first_order.id)
        self.assertEqual(second_email.order_id, first_order.id)
        self.assertEqual(first_email.processing_status, IncomingEmail.ProcessingStatus.PROCESSED)
        self.assertEqual(second_email.processing_status, IncomingEmail.ProcessingStatus.PROCESSED)

    def test_same_order_number_is_allowed_for_different_vendors(self):
        railrestro_email = self._email("RailRestro", "cross-vendor-railrestro")
        homebytes_email = self._email("HomeBytes", "cross-vendor-homebytes")

        railrestro_order = create_order_from_incoming_email(
            railrestro_email, self._data("SHARED-ORDER", "CASH_ON_DELIVERY")
        )
        homebytes_order = create_order_from_incoming_email(
            homebytes_email, self._data("SHARED-ORDER", "PRE_PAID")
        )

        self.assertNotEqual(railrestro_order.pk, homebytes_order.pk)
        self.assertEqual(Order.objects.filter(order_number="SHARED-ORDER").count(), 2)

    def test_vendor_order_number_unique_constraint_prevents_direct_duplicates(self):
        incoming_email = self._email("RailRestro", "unique-order-first")
        order = create_order_from_incoming_email(
            incoming_email, self._data("UNIQUE-ORDER", "CASH_ON_DELIVERY")
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Order.objects.create(
                    restaurant=order.restaurant,
                    vendor=order.vendor,
                    order_number=order.order_number,
                    customer=order.customer,
                    train=order.train,
                    payment_mode=Order.PaymentMode.CASH_ON_DELIVERY,
                )

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

    def test_dashboard_version_uses_djangos_current_local_date(self):
        with patch("Orders.views.get_live_status_for_order", return_value={
            "available": False,
            "train_number": "12963",
            "target_station": "GGC",
            "journey_date": None,
        }):
            response = self.client.get(reverse("order_list"))

        self.assertContains(response, timezone.localdate().isoformat())
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
            restaurant=self.restaurant,
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
        self.restaurant = Restaurant.objects.get(slug="food-costa")
        self.user = get_user_model().objects.create_user(username="report-owner")
        RestaurantMembership.objects.create(
            user=self.user,
            restaurant=self.restaurant,
            role=RestaurantMembership.Role.OWNER,
        )
        self.client.force_login(self.user)
        self.homebytes = Vendor.objects.create(
            restaurant=self.restaurant,
            name="HomeBytes", email_address="info@homebytes.co.in"
        )
        self.railrestro = Vendor.objects.create(
            restaurant=self.restaurant,
            name="RailRestro", email_address="no-reply@railrestro.com"
        )
        self.customer = Customer.objects.create(
            restaurant=self.restaurant,
            name="Report Customer",
            phone="9000000000",
        )
        self.train = Train.objects.create(train_number="12963", train_name="MEWAR EXPRESS")

    def _order(self, number, order_date, total, payment_mode, vendor=None, status=None):
        return Order.objects.create(
            restaurant=self.restaurant,
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


class DemoVendorTests(TestCase):
    def setUp(self):
        self.restaurant_a = Restaurant.objects.create(
            name="Demo Restaurant A",
            slug="demo-restaurant-a",
            subscription_status=Restaurant.SubscriptionStatus.ACTIVE,
            subscription_ends_at=timezone.now() + timedelta(days=366),
        )
        self.restaurant_b = Restaurant.objects.create(
            name="Demo Restaurant B",
            slug="demo-restaurant-b",
            subscription_status=Restaurant.SubscriptionStatus.ACTIVE,
            subscription_ends_at=timezone.now() + timedelta(days=366),
        )
        self.user = get_user_model().objects.create_user(username="demo-owner")
        RestaurantMembership.objects.create(
            user=self.user,
            restaurant=self.restaurant_a,
            role=RestaurantMembership.Role.OWNER,
        )
        self.demo_vendor = Vendor.objects.create(
            restaurant=self.restaurant_a,
            name="TrainPOS Demo Vendor",
            email_address="platform-demo@example.com",
            parser_type=Vendor.ParserType.DEMO,
            is_demo=True,
        )
        self.real_vendor = Vendor.objects.create(
            restaurant=self.restaurant_a,
            name="RailRestro",
            email_address="no-reply@railrestro.com",
            parser_type=Vendor.ParserType.RAILRESTRO,
        )

    def _body(self, order_number, payment="COD", amount_to_collect="304.50"):
        return f"""
            ORDER: {order_number}
            CUSTOMER: Demo Passenger
            PHONE: 9000000000
            TRAIN: 12963
            TRAIN NAME: MEWAR EXPRESS
            DELIVERY TIME: {timezone.localdate():%Y-%m-%d} 19:30
            COACH: B2
            SEAT: 36
            PAYMENT: {payment}

            ITEM: Demo Veg Thali | 1 | 250.00
            ITEM: Demo Water Bottle | 2 | 20.00

            GST: 14.50
            SUBTOTAL: 304.50
            TOTAL: 304.50
            AMOUNT TO COLLECT: {amount_to_collect}
            REMARKS: TrainPOS demonstration order
        """

    def _railrestro_body(self, order_number):
        return f"""
            ORDER #: {order_number} Customer: Real Passenger M. 9000000000
            TRAIN: 12963 / MEWAR EXPRESS
            Delivery Time: {timezone.localdate():%Y-%m-%d} 19:30:00
            Coact/Seat: B2-36
            <table><tr><td>Real Veg Thali</td><td>Rs. 304.50</td><td>1</td><td>Rs. 304.50</td></tr></table>
            GST: Rs. 14.50 Subtotal: Rs. 304.50
            Final Total: Rs. 304.50 (Amount to collect) Rs. 304.50
            Remarks: real order Best Regards
        """

    def _message(self, order_number, body=None):
        message = EmailMessage()
        message["From"] = "TrainPOS Demo <platform-demo@example.com>"
        message["Subject"] = f"New Order #{order_number} Received"
        message["Date"] = timezone.localdate().strftime("%a, %d %b %Y 10:00:00 +0000")
        message.set_content(body or self._body(order_number), subtype="plain")
        return message.as_bytes()

    def _mail(self, message):
        class FakeMail:
            def uid(self, command, *arguments):
                if command == "fetch":
                    return "OK", [(b"message", message)]
                raise AssertionError(f"Unexpected IMAP command: {command}")

        return FakeMail()

    def _stats(self):
        return {
            "checked": 1,
            "new": 0,
            "existing_retried": 0,
            "orders": 0,
            "skipped": 0,
            "failures": 0,
        }

    def test_demo_sender_is_accepted_only_for_its_explicit_tenant_and_uses_normal_pipeline(self):
        message = self._message("DEMO-TENANT-001")
        command = Command()
        tenant_a_stats = self._stats()
        tenant_b_stats = self._stats()

        command._process_message(
            self._mail(message), b"demo-message-1", tenant_a_stats, self.restaurant_a
        )
        command._process_message(
            self._mail(message), b"demo-message-1", tenant_b_stats, self.restaurant_b
        )

        order = Order.objects.get(restaurant=self.restaurant_a, order_number="DEMO-TENANT-001")
        self.assertTrue(order.is_demo)
        self.assertEqual(order.vendor, self.demo_vendor)
        self.assertEqual(order.items.count(), 2)
        self.assertEqual(tenant_a_stats["orders"], 1)
        self.assertEqual(tenant_b_stats["orders"], 0)
        self.assertFalse(IncomingEmail.objects.filter(restaurant=self.restaurant_b).exists())
        self.assertFalse(Order.objects.filter(restaurant=self.restaurant_b).exists())

    def test_missing_demo_order_is_terminal_and_not_retried(self):
        body = self._body("DEMO-MISSING-ORDER").replace("ORDER:", "ORDER NUMBER:")
        message = self._message("DEMO-MISSING-ORDER", body)
        command = Command()

        first_stats = self._stats()
        command._process_message(
            self._mail(message), b"demo-missing-order", first_stats, self.restaurant_a
        )
        incoming_email = IncomingEmail.objects.get(message_id="demo-missing-order")
        self.assertEqual(incoming_email.processing_status, IncomingEmail.ProcessingStatus.INVALID)
        self.assertEqual(incoming_email.error_message, "Demo email requires ORDER.")
        self.assertIsNone(incoming_email.order_id)
        self.assertFalse(Order.objects.filter(order_number="DEMO-MISSING-ORDER").exists())

        repeat_stats = self._stats()
        command._process_message(
            self._mail(message), b"demo-missing-order", repeat_stats, self.restaurant_a
        )
        self.assertEqual(repeat_stats["existing_retried"], 0)
        self.assertEqual(repeat_stats["skipped"], 1)
        self.assertEqual(Order.objects.filter(order_number="DEMO-MISSING-ORDER").count(), 0)

    def test_missing_demo_item_is_terminal(self):
        body = "\n".join(
            line
            for line in self._body("DEMO-MISSING-ITEM").splitlines()
            if not line.strip().upper().startswith("ITEM:")
        )
        stats = self._stats()
        Command()._process_message(
            self._mail(self._message("DEMO-MISSING-ITEM", body)),
            b"demo-missing-item",
            stats,
            self.restaurant_a,
        )

        incoming_email = IncomingEmail.objects.get(message_id="demo-missing-item")
        self.assertEqual(incoming_email.processing_status, IncomingEmail.ProcessingStatus.INVALID)
        self.assertIn("requires at least one ITEM line", incoming_email.error_message)
        self.assertEqual(Order.objects.filter(order_number="DEMO-MISSING-ITEM").count(), 0)

    def test_unexpected_demo_processing_error_remains_retryable(self):
        message = self._message("DEMO-TRANSIENT-001")
        command = Command()
        first_stats = self._stats()

        with patch(
            "Orders.management.commands.poll_gmail_orders.get_vendor_parser",
            side_effect=RuntimeError("temporary processing error"),
        ):
            command._process_message(
                self._mail(message), b"demo-transient", first_stats, self.restaurant_a
            )

        incoming_email = IncomingEmail.objects.get(message_id="demo-transient")
        self.assertEqual(incoming_email.processing_status, IncomingEmail.ProcessingStatus.FAILED)
        self.assertIsNone(incoming_email.order_id)

        retry_stats = self._stats()
        command._process_message(
            self._mail(message), b"demo-transient", retry_stats, self.restaurant_a
        )
        incoming_email.refresh_from_db()
        order = Order.objects.get(order_number="DEMO-TRANSIENT-001")
        self.assertEqual(retry_stats["existing_retried"], 1)
        self.assertEqual(incoming_email.processing_status, IncomingEmail.ProcessingStatus.PROCESSED)
        self.assertEqual(incoming_email.order, order)
        self.assertEqual(order.items.count(), 2)

    def test_valid_demo_email_is_idempotently_skipped_after_processing(self):
        message = self._message("DEMO-IDEMPOTENT-001")
        command = Command()
        command._process_message(
            self._mail(message), b"demo-idempotent", self._stats(), self.restaurant_a
        )
        repeat_stats = self._stats()
        command._process_message(
            self._mail(message), b"demo-idempotent", repeat_stats, self.restaurant_a
        )

        order = Order.objects.get(order_number="DEMO-IDEMPOTENT-001")
        self.assertEqual(repeat_stats["skipped"], 1)
        self.assertEqual(Order.objects.filter(order_number="DEMO-IDEMPOTENT-001").count(), 1)
        self.assertEqual(order.items.count(), 2)

    def test_demo_orders_appear_on_dashboard_with_a_badge_but_are_excluded_from_reports(self):
        demo_email = IncomingEmail.objects.create(
            restaurant=self.restaurant_a,
            vendor=self.demo_vendor,
            message_id="demo-dashboard-email",
            subject="New demo order",
            body=self._body("DEMO-DASHBOARD-001"),
            received_at=timezone.now(),
        )
        demo_order = create_order_from_incoming_email(
            demo_email, parse_demo_email(demo_email.body)
        )
        real_email = IncomingEmail.objects.create(
            restaurant=self.restaurant_a,
            vendor=self.real_vendor,
            message_id="real-dashboard-email",
            subject="New real order",
            body=self._railrestro_body("REAL-DASHBOARD-001"),
            received_at=timezone.now(),
        )
        real_order = create_order_from_incoming_email(
            real_email, parse_railrestro_email(real_email.body)
        )

        self.assertTrue(demo_order.is_demo)
        self.assertFalse(real_order.is_demo)
        self.client.force_login(self.user)
        unavailable_status = {
            "available": False,
            "train_number": "12963",
            "target_station": "GGC",
            "journey_date": None,
        }
        with patch("Orders.views.get_live_status_for_order", return_value=unavailable_status):
            dashboard = self.client.get(reverse("order_list"))
        report = self.client.get(reverse("reports"))

        self.assertContains(dashboard, "DEMO-DASHBOARD-001")
        self.assertContains(dashboard, "DEMO")
        self.assertContains(dashboard, "REAL-DASHBOARD-001")
        self.assertEqual(report.context["summary"]["total_orders"], 1)
        self.assertEqual(report.context["summary"]["cod_value"], real_order.total)
        self.assertEqual(
            [row["vendor__name"] for row in report.context["vendor_breakdown"]],
            ["RailRestro"],
        )

    def test_demo_parser_normalizes_cod_items_financials_and_whitespace(self):
        body = self._body("DEMO-COD-001").replace("PAYMENT: COD", " payment : cash on delivery ")

        parsed = parse_demo_email(body)

        self.assertEqual(parsed["order_number"], "DEMO-COD-001")
        self.assertEqual(parsed["payment_mode"], Order.PaymentMode.CASH_ON_DELIVERY)
        self.assertEqual(parsed["order_date"], f"{timezone.localdate():%Y-%m-%d} 19:30:00")
        self.assertEqual(parsed["coach"], "B2")
        self.assertEqual(parsed["berth"], "36")
        self.assertEqual(parsed["subtotal"], Decimal("304.50"))
        self.assertEqual(parsed["gst"], Decimal("14.50"))
        self.assertEqual(parsed["total"], Decimal("304.50"))
        self.assertEqual(parsed["amount_to_collect"], Decimal("304.50"))
        self.assertEqual(parsed["remarks"], "TrainPOS demonstration order")
        self.assertEqual(parsed["order_items"], [
            {"item_name": "Demo Veg Thali", "quantity": 1, "price": Decimal("250.00"), "amount": Decimal("250.00")},
            {"item_name": "Demo Water Bottle", "quantity": 2, "price": Decimal("20.00"), "amount": Decimal("40.00")},
        ])

    def test_demo_parser_normalizes_prepaid(self):
        parsed = parse_demo_email(self._body("DEMO-PREPAID-001", "pre_paid", "0.00"))

        self.assertEqual(parsed["payment_mode"], Order.PaymentMode.PRE_PAID)
        self.assertEqual(parsed["amount_to_collect"], Decimal("0.00"))
        self.assertEqual(parsed["advance"], Decimal("304.50"))

    def test_malformed_demo_email_fails_cleanly(self):
        malformed = "\n".join(
            line
            for line in self._body("DEMO-BAD").splitlines()
            if not line.strip().upper().startswith("ITEM:")
        )
        with self.assertRaisesMessage(ValueError, "requires at least one ITEM line"):
            parse_demo_email(malformed)

    def test_real_railrestro_parser_remains_independent_of_demo_parser(self):
        parsed = parse_railrestro_email(self._railrestro_body("REAL-RAILRESTRO-001"))

        self.assertEqual(parsed["order_number"], "REAL-RAILRESTRO-001")
        self.assertEqual(parsed["payment_mode"], Order.PaymentMode.CASH_ON_DELIVERY)
        self.assertEqual(parsed["order_items"][0]["item_name"], "Real Veg Thali")


class OrderDashboardVersionTests(TestCase):
    def setUp(self):
        self.restaurant = Restaurant.objects.get(slug="food-costa")
        self.user = get_user_model().objects.create_user(username="dashboard-owner")
        RestaurantMembership.objects.create(
            user=self.user,
            restaurant=self.restaurant,
            role=RestaurantMembership.Role.OWNER,
        )
        self.client.force_login(self.user)
        self.vendor = Vendor.objects.create(
            restaurant=self.restaurant,
            name="Dashboard Vendor", email_address="dashboard@example.com"
        )
        self.customer = Customer.objects.create(
            restaurant=self.restaurant,
            name="Dashboard Customer",
            phone="9000000000",
        )
        self.train = Train.objects.create(train_number="12963", train_name="MEWAR EXPRESS")

    def _order(self, number, order_date=None):
        return Order.objects.create(
            restaurant=self.restaurant,
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
            restaurant=self.restaurant,
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


class TenantIsolationTests(TestCase):
    def setUp(self):
        self.restaurant_a = Restaurant.objects.create(
            name="Restaurant A",
            slug="restaurant-a",
            subscription_status=Restaurant.SubscriptionStatus.ACTIVE,
            subscription_ends_at=timezone.now() + timedelta(days=366),
        )
        self.restaurant_b = Restaurant.objects.create(
            name="Restaurant B",
            slug="restaurant-b",
            subscription_status=Restaurant.SubscriptionStatus.ACTIVE,
            subscription_ends_at=timezone.now() + timedelta(days=366),
        )
        self.user_a = get_user_model().objects.create_user(username="tenant-a")
        self.user_b = get_user_model().objects.create_user(username="tenant-b")
        RestaurantMembership.objects.create(
            user=self.user_a,
            restaurant=self.restaurant_a,
            role=RestaurantMembership.Role.OWNER,
        )
        RestaurantMembership.objects.create(
            user=self.user_b,
            restaurant=self.restaurant_b,
            role=RestaurantMembership.Role.STAFF,
        )
        self.superuser = get_user_model().objects.create_superuser(
            username="platform-admin",
            email="platform@example.com",
            password="test-password",
        )
        self.train = Train.objects.create(train_number="12963", train_name="MEWAR EXPRESS")
        self.vendor_a = Vendor.objects.create(
            restaurant=self.restaurant_a,
            name="RailRestro",
            email_address="no-reply@railrestro.com",
        )
        self.vendor_b = Vendor.objects.create(
            restaurant=self.restaurant_b,
            name="RailRestro",
            email_address="no-reply@railrestro.com",
        )
        self.customer_a = Customer.objects.create(
            restaurant=self.restaurant_a, name="Customer A", phone="9000000001"
        )
        self.customer_b = Customer.objects.create(
            restaurant=self.restaurant_b, name="Customer B", phone="9000000002"
        )
        self.order_a = self._order(
            self.restaurant_a, self.vendor_a, self.customer_a, "TENANT-ORDER"
        )
        self.order_b = self._order(
            self.restaurant_b, self.vendor_b, self.customer_b, "TENANT-ORDER"
        )

    def _order(self, restaurant, vendor, customer, order_number):
        return Order.objects.create(
            restaurant=restaurant,
            vendor=vendor,
            order_number=order_number,
            customer=customer,
            train=self.train,
            order_date=timezone.now(),
            payment_mode=Order.PaymentMode.PRE_PAID,
            total=Decimal("100.00"),
        )

    def test_vendor_and_message_id_can_repeat_across_restaurants(self):
        email_a = IncomingEmail.objects.create(
            restaurant=self.restaurant_a,
            vendor=self.vendor_a,
            message_id="same-mailbox-uid",
            subject="A",
            body="A",
            received_at=timezone.now(),
        )
        email_b = IncomingEmail.objects.create(
            restaurant=self.restaurant_b,
            vendor=self.vendor_b,
            message_id="same-mailbox-uid",
            subject="B",
            body="B",
            received_at=timezone.now(),
        )

        self.assertNotEqual(self.vendor_a.pk, self.vendor_b.pk)
        self.assertNotEqual(email_a.pk, email_b.pk)
        self.assertEqual(Order.objects.filter(order_number="TENANT-ORDER").count(), 2)

    def test_restaurant_a_only_sees_its_dashboard_and_reports(self):
        self.client.force_login(self.user_a)
        unavailable_status = {
            "available": False,
            "train_number": "12963",
            "target_station": "GGC",
            "journey_date": None,
        }

        with patch("Orders.views.get_live_status_for_order", return_value=unavailable_status):
            dashboard = self.client.get(reverse("order_list"))
        reports_response = self.client.get(reverse("reports"))
        version = self.client.get(reverse("order_dashboard_version"))

        self.assertContains(dashboard, "TENANT-ORDER")
        self.assertNotContains(dashboard, "Customer B")
        self.assertEqual(reports_response.context["summary"]["total_orders"], 1)
        self.assertEqual(version.json()["order_count"], 1)

    def test_restaurant_a_cannot_access_restaurant_b_order_endpoints(self):
        self.client.force_login(self.user_a)

        bill_response = self.client.get(reverse("order_bill", args=[self.order_b.pk]))
        with patch("Orders.views.refresh_live_status_for_order") as refresh:
            refresh_response = self.client.post(
                reverse("order_train_status_refresh", args=[self.order_b.pk]),
                {"journey_date": timezone.localdate().isoformat()},
            )

        self.assertEqual(bill_response.status_code, 404)
        self.assertEqual(refresh_response.status_code, 404)
        refresh.assert_not_called()

    def test_user_without_membership_is_denied_operational_dashboard(self):
        user = get_user_model().objects.create_user(username="no-membership")
        self.client.force_login(user)

        response = self.client.get(reverse("order_list"))

        self.assertEqual(response.status_code, 403)

    @override_settings(RESTAURANT_CREDENTIAL_ENCRYPTION_KEY=Fernet.generate_key().decode())
    def test_restaurant_email_credentials_are_encrypted_and_trial_defaults_apply(self):
        trial_restaurant = Restaurant.objects.create(name="Trial", slug="trial")
        connection = RestaurantEmailConnection(
            restaurant=trial_restaurant,
            email_address="orders@trial.example.com",
        )
        connection.set_app_password("app-password")
        connection.save()

        self.assertEqual(trial_restaurant.subscription_status, Restaurant.SubscriptionStatus.TRIAL)
        self.assertTrue(trial_restaurant.is_trial_active)
        self.assertTrue(trial_restaurant.has_access)
        self.assertNotEqual(connection.encrypted_app_password, "app-password")
        self.assertEqual(connection.get_app_password(), "app-password")

    def test_platform_superuser_can_inspect_tenants_in_admin(self):
        self.client.force_login(self.superuser)

        restaurant_admin = self.client.get(reverse("admin:Orders_restaurant_changelist"))
        order_admin = self.client.get(reverse("admin:Orders_order_changelist"))

        self.assertEqual(restaurant_admin.status_code, 200)
        self.assertEqual(order_admin.status_code, 200)
        self.assertContains(restaurant_admin, "Restaurant A")
        self.assertContains(restaurant_admin, "Restaurant B")
        self.assertContains(order_admin, "TENANT-ORDER")


class SaaSOnboardingTests(TestCase):
    password = "Strong-pass-123!"

    def _user_with_membership(self, restaurant, username, role="OWNER"):
        user = get_user_model().objects.create_user(
            username=username,
            email=f"{username}@example.com",
            password=self.password,
        )
        RestaurantMembership.objects.create(
            user=user,
            restaurant=restaurant,
            role=role,
        )
        return user

    def _restaurant(self, name, slug):
        return Restaurant.objects.create(
            name=name,
            slug=slug,
            subscription_status=Restaurant.SubscriptionStatus.ACTIVE,
            subscription_ends_at=timezone.now() + timedelta(days=366),
        )

    def test_public_routes_render(self):
        self.assertEqual(self.client.get(reverse("landing")).status_code, 200)
        self.assertEqual(self.client.get(reverse("signup")).status_code, 200)
        self.assertEqual(self.client.get(reverse("login")).status_code, 200)
        self.assertEqual(self.client.get(reverse("onboarding_email")).status_code, 302)

    def test_successful_signup_creates_trial_restaurant_owner_and_logs_in(self):
        response = self.client.post(
            reverse("signup"),
            {
                "restaurant_name": "New Railway Foods",
                "owner_name": "New Owner",
                "email": "owner@newrailway.example",
                "phone": "9000000000",
                "password": self.password,
                "confirm_password": self.password,
            },
        )

        user = get_user_model().objects.get(email="owner@newrailway.example")
        restaurant = Restaurant.objects.get(email="owner@newrailway.example")
        membership = RestaurantMembership.objects.get(user=user, restaurant=restaurant)
        self.assertRedirects(response, reverse("onboarding_email"))
        self.assertEqual(membership.role, RestaurantMembership.Role.OWNER)
        self.assertEqual(restaurant.subscription_status, Restaurant.SubscriptionStatus.TRIAL)
        self.assertTrue(restaurant.is_trial_active)
        self.assertEqual((restaurant.trial_ends_at - restaurant.trial_started_at).days, 15)
        self.assertEqual(str(self.client.session["_auth_user_id"]), str(user.pk))

    def test_signup_rolls_back_every_record_when_membership_creation_fails(self):
        with patch(
            "Orders.views.RestaurantMembership.objects.create",
            side_effect=IntegrityError("membership failure"),
        ):
            response = self.client.post(
                reverse("signup"),
                {
                    "restaurant_name": "Rollback Foods",
                    "owner_name": "Rollback Owner",
                    "email": "rollback@example.com",
                    "phone": "9000000000",
                    "password": self.password,
                    "confirm_password": self.password,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(get_user_model().objects.filter(email="rollback@example.com").exists())
        self.assertFalse(Restaurant.objects.filter(name="Rollback Foods").exists())

    def test_duplicate_and_invalid_signup_are_rejected(self):
        get_user_model().objects.create_user(
            username="taken@example.com",
            email="taken@example.com",
            password=self.password,
        )
        response = self.client.post(
            reverse("signup"),
            {
                "restaurant_name": "Duplicate Foods",
                "owner_name": "Owner",
                "email": "taken@example.com",
                "phone": "",
                "password": self.password,
                "confirm_password": "different-password",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "An account with this email already exists.")
        self.assertContains(response, "Phone is required.")
        self.assertContains(response, "Passwords do not match.")

    def test_login_and_logout_flow(self):
        restaurant = self._restaurant("Login Foods", "login-foods")
        user = self._user_with_membership(restaurant, "login-owner")

        login_response = self.client.post(
            reverse("login"),
            {"username": user.username, "password": self.password},
        )
        self.assertRedirects(login_response, reverse("order_list"))
        logout_response = self.client.post(reverse("logout"))

        self.assertRedirects(logout_response, reverse("landing"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_owner_can_manage_email_but_staff_cannot(self):
        restaurant = self._restaurant("Email Foods", "email-foods")
        owner = self._user_with_membership(restaurant, "email-owner")
        staff = self._user_with_membership(
            restaurant, "email-staff", RestaurantMembership.Role.STAFF
        )

        self.client.force_login(owner)
        self.assertEqual(self.client.get(reverse("onboarding_email")).status_code, 200)
        self.client.force_login(staff)
        self.assertEqual(self.client.get(reverse("onboarding_email")).status_code, 403)

    @override_settings(RESTAURANT_CREDENTIAL_ENCRYPTION_KEY=Fernet.generate_key().decode())
    def test_owner_saves_encrypted_credential_and_successful_connection(self):
        restaurant = self._restaurant("Connection Foods", "connection-foods")
        owner = self._user_with_membership(restaurant, "connection-owner")
        self.client.force_login(owner)

        with patch("Orders.views.test_gmail_connection", return_value=True) as connection_test:
            response = self.client.post(
                reverse("onboarding_email"),
                {
                    "email_address": "orders@connection.example.com",
                    "app_password": "test-app-password",
                    "action": "test",
                },
                follow=True,
            )

        connection = RestaurantEmailConnection.objects.get(restaurant=restaurant)
        self.assertTrue(connection.is_active)
        self.assertEqual(
            connection.connection_status,
            RestaurantEmailConnection.ConnectionStatus.CONNECTED,
        )
        self.assertNotEqual(connection.encrypted_app_password, "test-app-password")
        self.assertEqual(connection.get_app_password(), "test-app-password")
        self.assertContains(response, "Email connected successfully.")
        connection_test.assert_called_once_with(
            "orders@connection.example.com", "test-app-password"
        )

    @override_settings(RESTAURANT_CREDENTIAL_ENCRYPTION_KEY=Fernet.generate_key().decode())
    def test_failed_connection_does_not_expose_app_password_or_cross_tenant_data(self):
        restaurant_a = self._restaurant("Connection A", "connection-a")
        restaurant_b = self._restaurant("Connection B", "connection-b")
        owner_a = self._user_with_membership(restaurant_a, "connection-a-owner")
        connection_b = RestaurantEmailConnection(
            restaurant=restaurant_b,
            email_address="orders@b.example.com",
        )
        connection_b.set_app_password("restaurant-b-secret")
        connection_b.save()
        encrypted_b = connection_b.encrypted_app_password
        self.client.force_login(owner_a)

        with patch("Orders.views.test_gmail_connection", return_value=False):
            response = self.client.post(
                reverse("onboarding_email"),
                {
                    "email_address": "orders@a.example.com",
                    "app_password": "restaurant-a-secret",
                    "action": "test",
                },
                follow=True,
            )

        connection_b.refresh_from_db()
        connection_a = RestaurantEmailConnection.objects.get(restaurant=restaurant_a)
        self.assertFalse(connection_a.is_active)
        self.assertEqual(
            connection_a.connection_status,
            RestaurantEmailConnection.ConnectionStatus.ERROR,
        )
        self.assertEqual(connection_b.encrypted_app_password, encrypted_b)
        self.assertContains(
            response,
            "Could not connect to Gmail. Please verify the email and App Password.",
        )
        self.assertNotContains(response, "restaurant-a-secret")
        self.assertNotContains(response, "restaurant-b-secret")

    def test_imap_connection_helper_only_logs_in_and_logs_out(self):
        class FakeMail:
            def login(self, email_address, app_password):
                self.credentials = (email_address, app_password)

            def logout(self):
                self.logged_out = True

        fake_mail = FakeMail()
        with patch(
            "Orders.services.gmail_connection.imaplib.IMAP4_SSL",
            return_value=fake_mail,
        ):
            from Orders.services.gmail_connection import test_gmail_connection

            self.assertTrue(test_gmail_connection("orders@example.com", "app-password"))

        self.assertEqual(fake_mail.credentials, ("orders@example.com", "app-password"))
        self.assertTrue(fake_mail.logged_out)
        self.assertFalse(hasattr(fake_mail, "select"))

    def test_active_trial_access_expired_trial_block_and_food_costa_active_access(self):
        trial = Restaurant.objects.create(name="Active Trial", slug="active-trial")
        trial_user = self._user_with_membership(trial, "trial-owner")
        self.client.force_login(trial_user)
        self.assertEqual(self.client.get(reverse("order_list")).status_code, 200)

        trial.trial_ends_at = timezone.now() - timedelta(minutes=1)
        trial.save(update_fields=["trial_ends_at"])
        expired_response = self.client.get(reverse("order_list"))
        self.assertEqual(expired_response.status_code, 403)
        self.assertContains(expired_response, "trial has ended", status_code=403)

        food_costa = Restaurant.objects.get(slug="food-costa")
        food_costa_user = self._user_with_membership(food_costa, "food-costa-owner")
        self.client.force_login(food_costa_user)
        self.assertEqual(food_costa.subscription_status, Restaurant.SubscriptionStatus.ACTIVE)
        self.assertEqual(self.client.get(reverse("order_list")).status_code, 200)


class AuthenticatedNavigationTests(TestCase):
    def setUp(self):
        self.restaurant = Restaurant.objects.get(slug="food-costa")
        self.user = get_user_model().objects.create_user(
            username="nav-owner",
            first_name="Ankit",
            password="test-password",
        )
        RestaurantMembership.objects.create(
            user=self.user,
            restaurant=self.restaurant,
            role=RestaurantMembership.Role.OWNER,
        )
        self.client.force_login(self.user)

    def test_owner_sees_only_current_mvp_navigation_and_account_controls(self):
        response = self.client.get(reverse("order_list"))

        self.assertContains(response, '<meta name="robots" content="noindex,nofollow">')
        self.assertContains(response, "Orders")
        self.assertContains(response, "Reports")
        self.assertContains(response, "Gmail Settings")
        self.assertContains(response, reverse("onboarding_email"))
        self.assertContains(response, "Manage Gmail")
        self.assertContains(response, "Log out")
        self.assertContains(response, "Ankit")
        self.assertNotContains(response, ">Dashboard<", html=False)
        self.assertNotContains(response, ">KOT<", html=False)
        self.assertNotContains(response, ">Customers<", html=False)
        self.assertNotContains(response, ">Trains<", html=False)
        self.assertNotContains(response, ">Vendors<", html=False)
        self.assertNotContains(response, "Search orders, PNR, phone, train...")
        self.assertNotContains(response, "Platform Admin")

    def test_logout_remains_post_only(self):
        self.assertEqual(self.client.get(reverse("logout")).status_code, 405)
        response = self.client.post(reverse("logout"))
        self.assertRedirects(response, reverse("landing"))

    def test_superuser_account_dropdown_includes_platform_admin(self):
        superuser = get_user_model().objects.create_superuser(
            username="platform-nav-admin",
            email="platform-nav-admin@example.com",
            password="admin-password",
        )
        request = RequestFactory().get(reverse("order_list"))
        request.user = superuser
        html = get_template("Orders/order_list.html").render(
            {
                "restaurant": self.restaurant,
                "orders": [],
                "summary": {},
                "dashboard_version": SimpleNamespace(token="navigation-test"),
                "dashboard_date": timezone.localdate(),
                "can_manage_email": False,
                "has_active_email_connection": True,
            },
            request,
        )

        self.assertIn("Platform Admin", html)
        self.assertIn(reverse("admin:index"), html)


class SubscriptionAccessTests(TestCase):
    def _restaurant(self, slug, status=Restaurant.SubscriptionStatus.TRIAL, **values):
        return Restaurant.objects.create(
            name=slug.replace("-", " ").title(),
            slug=slug,
            subscription_status=status,
            **values,
        )

    def _user_for(self, restaurant, username):
        user = get_user_model().objects.create_user(username=username, password="test-password")
        RestaurantMembership.objects.create(
            user=user,
            restaurant=restaurant,
            role=RestaurantMembership.Role.OWNER,
        )
        self.client.force_login(user)
        return user

    def test_fresh_trial_and_active_future_subscription_have_access(self):
        trial = self._restaurant("fresh-trial")
        self._user_for(trial, "fresh-trial-owner")
        self.assertEqual(self.client.get(reverse("order_list")).status_code, 200)

        active = self._restaurant(
            "paid-active",
            Restaurant.SubscriptionStatus.ACTIVE,
            subscription_started_at=timezone.now(),
            subscription_ends_at=timezone.now() + timedelta(days=30),
        )
        self._user_for(active, "paid-active-owner")
        self.assertEqual(self.client.get(reverse("order_list")).status_code, 200)

    @override_settings(
        TRAINPOS_CONTACT_PHONE="+91 90000 00000",
        TRAINPOS_CONTACT_EMAIL="team@example.com",
        TRAINPOS_CONTACT_WHATSAPP="+91 91111 11111",
    )
    def test_expired_trial_is_blocked_and_shows_configured_contact_details(self):
        restaurant = self._restaurant(
            "expired-trial",
            trial_ends_at=timezone.now() - timedelta(seconds=1),
        )
        self._user_for(restaurant, "expired-trial-owner")

        response = self.client.get(reverse("order_list"))

        restaurant.refresh_from_db()
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "Your 15-day TrainPOS trial has ended.", status_code=403)
        self.assertContains(response, "+91 90000 00000", status_code=403)
        self.assertContains(response, "team@example.com", status_code=403)
        self.assertContains(response, "+91 91111 11111", status_code=403)
        self.assertEqual(restaurant.subscription_status, Restaurant.SubscriptionStatus.EXPIRED)

    def test_expired_active_and_expired_or_suspended_statuses_are_blocked(self):
        expired_active = self._restaurant(
            "expired-active",
            Restaurant.SubscriptionStatus.ACTIVE,
            subscription_started_at=timezone.now() - timedelta(days=365),
            subscription_ends_at=timezone.now() - timedelta(seconds=1),
        )
        self._user_for(expired_active, "expired-active-owner")
        response = self.client.get(reverse("order_list"))
        expired_active.refresh_from_db()
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "Your TrainPOS subscription has expired.", status_code=403)
        self.assertEqual(expired_active.subscription_status, Restaurant.SubscriptionStatus.EXPIRED)

        explicitly_expired = self._restaurant(
            "explicitly-expired", Restaurant.SubscriptionStatus.EXPIRED
        )
        self._user_for(explicitly_expired, "explicitly-expired-owner")
        self.assertEqual(self.client.get(reverse("order_list")).status_code, 403)

        suspended = self._restaurant(
            "suspended", Restaurant.SubscriptionStatus.SUSPENDED
        )
        self._user_for(suspended, "suspended-owner")
        self.assertEqual(self.client.get(reverse("order_list")).status_code, 403)

    def test_food_costa_compatibility_subscription_is_active(self):
        food_costa = Restaurant.objects.get(slug="food-costa")
        self.assertEqual(food_costa.subscription_status, Restaurant.SubscriptionStatus.ACTIVE)
        self.assertIsNotNone(food_costa.subscription_ends_at)
        self.assertTrue(food_costa.has_access)

    def test_restaurant_owner_cannot_change_subscription_through_admin_url(self):
        restaurant = self._restaurant("owner-cannot-bill")
        self._user_for(restaurant, "owner-cannot-bill-user")
        original_status = restaurant.subscription_status

        response = self.client.post(
            reverse("admin:Orders_restaurant_change", args=[restaurant.pk]),
            {"subscription_status": Restaurant.SubscriptionStatus.ACTIVE},
        )

        restaurant.refresh_from_db()
        self.assertIn(response.status_code, {302, 403})
        self.assertEqual(restaurant.subscription_status, original_status)


class SubscriptionAdminTests(TestCase):
    def setUp(self):
        self.superuser = get_user_model().objects.create_superuser(
            username="subscription-admin",
            email="subscription-admin@example.com",
            password="admin-password",
        )
        self.client.force_login(self.superuser)

    def _restaurant(self, slug, **values):
        return Restaurant.objects.create(name=slug, slug=slug, **values)

    def _activate(self, *restaurants):
        return self.client.post(
            reverse("admin:Orders_restaurant_changelist"),
            {
                "action": "activate_or_extend_subscription_one_year",
                "_selected_action": [str(restaurant.pk) for restaurant in restaurants],
            },
            follow=True,
        )

    def test_activate_one_year_and_early_or_expired_renewal(self):
        now = timezone.make_aware(datetime(2026, 8, 28, 10, 0))
        trial = self._restaurant("subscription-trial")
        future_end = timezone.make_aware(datetime(2027, 1, 15, 10, 0))
        early = self._restaurant(
            "subscription-early",
            subscription_status=Restaurant.SubscriptionStatus.ACTIVE,
            subscription_started_at=timezone.make_aware(datetime(2026, 1, 15, 10, 0)),
            subscription_ends_at=future_end,
        )
        expired = self._restaurant(
            "subscription-expired",
            subscription_status=Restaurant.SubscriptionStatus.ACTIVE,
            subscription_started_at=timezone.make_aware(datetime(2025, 8, 28, 10, 0)),
            subscription_ends_at=now - timedelta(seconds=1),
        )

        with patch("Orders.admin.timezone.now", return_value=now):
            response = self._activate(trial, early, expired)

        trial.refresh_from_db()
        early.refresh_from_db()
        expired.refresh_from_db()
        self.assertEqual(trial.subscription_status, Restaurant.SubscriptionStatus.ACTIVE)
        self.assertEqual(trial.subscription_started_at, now)
        self.assertEqual(trial.subscription_ends_at, add_calendar_year(now))
        self.assertEqual(early.subscription_started_at, timezone.make_aware(datetime(2026, 1, 15, 10, 0)))
        self.assertEqual(early.subscription_ends_at, add_calendar_year(future_end))
        self.assertEqual(expired.subscription_started_at, now)
        self.assertEqual(expired.subscription_ends_at, add_calendar_year(now))
        self.assertTrue(trial.is_active)
        self.assertContains(response, "Subscription activated for 2 restaurant")
        self.assertContains(response, "extended for 1 restaurant")

    def test_admin_form_allows_custom_subscription_dates(self):
        restaurant = self._restaurant("custom-subscription")
        model_admin = admin.site._registry[Restaurant]
        request = RequestFactory().get("/admin/")
        request.user = self.superuser
        form_class = model_admin.get_form(request, restaurant)
        self.assertIn("subscription_started_at", form_class.base_fields)
        self.assertIn("subscription_ends_at", form_class.base_fields)
        self.assertIn("subscription_status", form_class.base_fields)

        custom_start = timezone.make_aware(datetime(2026, 9, 1, 9, 0))
        custom_end = timezone.make_aware(datetime(2028, 2, 29, 9, 0))
        form = form_class(
            data={
                "name": restaurant.name,
                "slug": restaurant.slug,
                "owner_name": "",
                "phone": "",
                "email": "",
                "is_active": "on",
                "trial_started_at_0": restaurant.trial_started_at.strftime("%Y-%m-%d"),
                "trial_started_at_1": restaurant.trial_started_at.strftime("%H:%M:%S"),
                "trial_ends_at_0": restaurant.trial_ends_at.strftime("%Y-%m-%d"),
                "trial_ends_at_1": restaurant.trial_ends_at.strftime("%H:%M:%S"),
                "subscription_status": Restaurant.SubscriptionStatus.ACTIVE,
                "subscription_started_at_0": custom_start.strftime("%Y-%m-%d"),
                "subscription_started_at_1": custom_start.strftime("%H:%M:%S"),
                "subscription_ends_at_0": custom_end.strftime("%Y-%m-%d"),
                "subscription_ends_at_1": custom_end.strftime("%H:%M:%S"),
            },
            instance=restaurant,
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        restaurant.refresh_from_db()
        self.assertEqual(restaurant.subscription_ends_at, custom_end)


class DemoVendorAdminTests(TestCase):
    def setUp(self):
        self.superuser = get_user_model().objects.create_superuser(
            username="platform-admin",
            email="platform-admin@example.com",
            password="admin-password",
        )
        self.restaurant_a = Restaurant.objects.create(
            name="Admin Demo A",
            slug="admin-demo-a",
            subscription_status=Restaurant.SubscriptionStatus.ACTIVE,
            subscription_ends_at=timezone.now() + timedelta(days=366),
        )
        self.restaurant_b = Restaurant.objects.create(
            name="Admin Demo B",
            slug="admin-demo-b",
            subscription_status=Restaurant.SubscriptionStatus.ACTIVE,
            subscription_ends_at=timezone.now() + timedelta(days=366),
        )
        self.client.force_login(self.superuser)

    def _run_demo_action(self, *restaurants):
        return self.client.post(
            reverse("admin:Orders_restaurant_changelist"),
            {
                "action": "create_trainpos_demo_vendor",
                "_selected_action": [str(restaurant.pk) for restaurant in restaurants],
            },
            follow=True,
        )

    @override_settings(TRAINPOS_DEMO_SENDER_EMAIL="platform-demo@example.com")
    def test_admin_action_creates_and_idempotently_reenables_tenant_demo_vendor(self):
        response = self._run_demo_action(self.restaurant_a)

        demo_vendor = Vendor.objects.get(restaurant=self.restaurant_a)
        self.assertEqual(demo_vendor.name, "TrainPOS Demo Vendor")
        self.assertEqual(demo_vendor.email_address, "platform-demo@example.com")
        self.assertEqual(demo_vendor.parser_type, Vendor.ParserType.DEMO)
        self.assertTrue(demo_vendor.is_demo)
        self.assertTrue(demo_vendor.is_active)
        self.assertFalse(Vendor.objects.filter(restaurant=self.restaurant_b).exists())
        self.assertContains(response, "Demo Vendor configured for 1 restaurant")

        demo_vendor.is_active = False
        demo_vendor.parser_type = Vendor.ParserType.RAILRESTRO
        demo_vendor.save(update_fields=["is_active", "parser_type"])
        self._run_demo_action(self.restaurant_a)

        self.assertEqual(Vendor.objects.filter(restaurant=self.restaurant_a).count(), 1)
        demo_vendor.refresh_from_db()
        self.assertTrue(demo_vendor.is_active)
        self.assertEqual(demo_vendor.parser_type, Vendor.ParserType.DEMO)

    @override_settings(TRAINPOS_DEMO_SENDER_EMAIL="")
    def test_admin_action_handles_missing_demo_sender_without_creating_vendor(self):
        response = self._run_demo_action(self.restaurant_a)

        self.assertFalse(Vendor.objects.filter(restaurant=self.restaurant_a).exists())
        self.assertContains(response, "Set TRAINPOS_DEMO_SENDER_EMAIL")


@override_settings(RESTAURANT_CREDENTIAL_ENCRYPTION_KEY=Fernet.generate_key().decode())
class MultiRestaurantPollingTests(TestCase):
    class FakeMail:
        def __init__(self, message, login_error=None):
            self.message = message
            self.login_error = login_error
            self.logged_out = False

        def login(self, email_address, app_password):
            self.credentials = (email_address, app_password)
            if self.login_error:
                raise self.login_error

        def select(self, mailbox):
            self.selected_mailbox = mailbox
            return "OK", [b""]

        def uid(self, command, *arguments):
            if command == "search":
                return "OK", [b"100"]
            if command == "fetch":
                return "OK", [(b"message", self.message)]
            raise AssertionError(f"Unexpected IMAP command: {command}")

        def logout(self):
            self.logged_out = True

    def _restaurant(self, name, slug, status=Restaurant.SubscriptionStatus.ACTIVE):
        values = {"name": name, "slug": slug, "subscription_status": status}
        if status == Restaurant.SubscriptionStatus.ACTIVE:
            values["subscription_ends_at"] = timezone.now() + timedelta(days=366)
        return Restaurant.objects.create(**values)

    def _vendor(self, restaurant):
        return Vendor.objects.create(
            restaurant=restaurant,
            name="HomeBytes",
            email_address="info@homebytes.co.in",
            parser_type=Vendor.ParserType.HOMEBYTES,
        )

    def _connection(self, restaurant, address):
        connection = RestaurantEmailConnection(restaurant=restaurant, email_address=address)
        connection.set_app_password("tenant-app-password")
        connection.save()
        return connection

    def _message(self, order_number):
        today = timezone.localdate()
        body = f"""
            Booking Date: {today:%d %b %Y}, 09:30<br>
            Delivery Date: {today:%d %b %Y}, 10:00<br>
            Customer Name : Tenant Customer<br>
            Customer Contact : 9000000000<br>
            Invoice {order_number} / 2470000000<br>
            Payment: PRE_PAID<br>
            Coach / Berth: B2 / 36<br>
            Train: 12963 / MEWAR EXPRESS<br>
            GST (5%) 15.00 Discount 0.00 Total: 315.00
            <table><tr><td>1</td><td>Veg Cheese Pizza</td><td></td><td>1</td><td>300.00</td><td>15.00</td><td>300.00</td></tr></table>
        """
        message = EmailMessage()
        message["From"] = "HomeBytes <info@homebytes.co.in>"
        message["Subject"] = f"HomeBytes {order_number}"
        message["Date"] = today.strftime("%a, %d %b %Y 10:00:00 +0000")
        message.set_content(body, subtype="html")
        return message.as_bytes()

    def test_two_restaurants_process_same_mail_id_and_order_number_independently(self):
        restaurant_a = self._restaurant("Polling A", "polling-a")
        restaurant_b = self._restaurant("Polling B", "polling-b")
        self._vendor(restaurant_a)
        self._vendor(restaurant_b)
        connection_a = self._connection(restaurant_a, "a@example.com")
        connection_b = self._connection(restaurant_b, "b@example.com")
        command = Command()
        mail_a = self.FakeMail(self._message("HB-SHARED"))
        mail_b = self.FakeMail(self._message("HB-SHARED"))

        with patch.object(command, "_legacy_food_costa_mailbox", return_value=None), patch(
            "Orders.management.commands.poll_gmail_orders.imaplib.IMAP4_SSL",
            side_effect=[mail_a, mail_b],
        ):
            command._poll_all_restaurants()

        self.assertEqual(Order.objects.filter(restaurant=restaurant_a).count(), 1)
        self.assertEqual(Order.objects.filter(restaurant=restaurant_b).count(), 1)
        self.assertEqual(
            IncomingEmail.objects.filter(message_id="100", restaurant=restaurant_a).count(),
            1,
        )
        self.assertEqual(
            IncomingEmail.objects.filter(message_id="100", restaurant=restaurant_b).count(),
            1,
        )
        self.assertEqual(Order.objects.filter(order_number="HB-SHARED").count(), 2)
        self.assertEqual(mail_a.credentials[0], "a@example.com")
        self.assertEqual(mail_b.credentials[0], "b@example.com")
        self.assertEqual(mail_a.credentials[1], "tenant-app-password")
        self.assertEqual(mail_b.credentials[1], "tenant-app-password")
        connection_a.refresh_from_db()
        connection_b.refresh_from_db()
        self.assertEqual(connection_a.connection_status, RestaurantEmailConnection.ConnectionStatus.CONNECTED)
        self.assertEqual(connection_b.connection_status, RestaurantEmailConnection.ConnectionStatus.CONNECTED)
        self.assertIsNotNone(connection_a.last_checked_at)
        self.assertTrue(mail_a.logged_out)
        self.assertTrue(mail_b.logged_out)

    def test_duplicate_within_one_tenant_is_idempotent(self):
        restaurant = self._restaurant("Polling Duplicate", "polling-duplicate")
        self._vendor(restaurant)
        self._connection(restaurant, "duplicate@example.com")
        command = Command()

        with patch.object(command, "_legacy_food_costa_mailbox", return_value=None), patch(
            "Orders.management.commands.poll_gmail_orders.imaplib.IMAP4_SSL",
            side_effect=[
                self.FakeMail(self._message("HB-DUPLICATE")),
                self.FakeMail(self._message("HB-DUPLICATE")),
            ],
        ):
            command._poll_all_restaurants()
            command._poll_all_restaurants()

        self.assertEqual(Order.objects.filter(restaurant=restaurant).count(), 1)
        self.assertEqual(IncomingEmail.objects.filter(restaurant=restaurant).count(), 1)
        self.assertEqual(Order.objects.get(restaurant=restaurant).items.count(), 1)

    def test_failure_in_one_restaurant_does_not_block_the_next(self):
        restaurant_a = self._restaurant("Failure A", "failure-a")
        restaurant_b = self._restaurant("Failure B", "failure-b")
        self._vendor(restaurant_a)
        self._vendor(restaurant_b)
        connection_a = self._connection(restaurant_a, "failure-a@example.com")
        connection_b = self._connection(restaurant_b, "failure-b@example.com")
        command = Command()

        with patch.object(command, "_legacy_food_costa_mailbox", return_value=None), patch(
            "Orders.management.commands.poll_gmail_orders.imaplib.IMAP4_SSL",
            side_effect=[
                self.FakeMail(None, imaplib.IMAP4.error("invalid credentials")),
                self.FakeMail(self._message("HB-SECOND-SUCCEEDS")),
            ],
        ):
            command._poll_all_restaurants()

        connection_a.refresh_from_db()
        connection_b.refresh_from_db()
        self.assertEqual(connection_a.connection_status, RestaurantEmailConnection.ConnectionStatus.ERROR)
        self.assertEqual(connection_b.connection_status, RestaurantEmailConnection.ConnectionStatus.CONNECTED)
        self.assertEqual(Order.objects.filter(restaurant=restaurant_a).count(), 0)
        self.assertEqual(Order.objects.filter(restaurant=restaurant_b).count(), 1)

    def test_malformed_message_in_one_restaurant_does_not_block_the_next(self):
        restaurant_a = self._restaurant("Malformed A", "malformed-a")
        restaurant_b = self._restaurant("Malformed B", "malformed-b")
        self._vendor(restaurant_a)
        self._vendor(restaurant_b)
        self._connection(restaurant_a, "malformed-a@example.com")
        self._connection(restaurant_b, "malformed-b@example.com")
        command = Command()
        original_process_message = command._process_message
        calls = 0

        def fail_first_message(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ValueError("malformed vendor email")
            return original_process_message(*args, **kwargs)

        with patch.object(command, "_legacy_food_costa_mailbox", return_value=None), patch(
            "Orders.management.commands.poll_gmail_orders.imaplib.IMAP4_SSL",
            side_effect=[
                self.FakeMail(self._message("HB-MALFORMED")),
                self.FakeMail(self._message("HB-AFTER-MALFORMED")),
            ],
        ), patch.object(command, "_process_message", side_effect=fail_first_message):
            command._poll_all_restaurants()

        self.assertEqual(Order.objects.filter(restaurant=restaurant_a).count(), 0)
        self.assertEqual(Order.objects.filter(restaurant=restaurant_b).count(), 1)

    def test_only_access_eligible_restaurants_are_polled(self):
        active = self._restaurant("Eligible Active", "eligible-active")
        trial = Restaurant.objects.create(name="Eligible Trial", slug="eligible-trial")
        expired = Restaurant.objects.create(
            name="Expired", slug="expired", trial_ends_at=timezone.now() - timedelta(minutes=1)
        )
        suspended = self._restaurant(
            "Suspended", "suspended", Restaurant.SubscriptionStatus.SUSPENDED
        )
        for restaurant in (active, trial, expired, suspended):
            self._connection(restaurant, f"{restaurant.slug}@example.com")
        command = Command()

        with patch.object(command, "_legacy_food_costa_mailbox", return_value=None), patch.object(
            command, "_poll_connection", return_value={"checked": 0}
        ) as poll_connection:
            command._poll_all_restaurants()

        polled_ids = {call.args[0].restaurant_id for call in poll_connection.call_args_list}
        self.assertEqual(polled_ids, {active.id, trial.id})

    def test_suspended_and_expired_paid_restaurants_are_excluded_until_reactivated(self):
        restaurant = self._restaurant("Renewable", "renewable")
        connection = self._connection(restaurant, "renewable@example.com")
        command = Command()

        self.assertEqual(
            [item.pk for item in command._eligible_connections()], [connection.pk]
        )

        restaurant.subscription_status = Restaurant.SubscriptionStatus.SUSPENDED
        restaurant.save(update_fields=["subscription_status"])
        self.assertEqual(command._eligible_connections(), [])

        restaurant.subscription_status = Restaurant.SubscriptionStatus.ACTIVE
        restaurant.subscription_ends_at = timezone.now() - timedelta(seconds=1)
        restaurant.save(update_fields=["subscription_status", "subscription_ends_at"])
        self.assertEqual(command._eligible_connections(), [])

        restaurant.subscription_ends_at = timezone.now() + timedelta(days=30)
        restaurant.save(update_fields=["subscription_ends_at"])
        self.assertEqual(
            [item.pk for item in command._eligible_connections()], [connection.pk]
        )

    def test_food_costa_legacy_fallback_and_backfill_are_tenant_scoped(self):
        food_costa = Restaurant.objects.get(slug="food-costa")
        command = Command()
        with patch(
            "Orders.management.commands.poll_gmail_orders.os.getenv",
            side_effect=lambda key: {"GMAIL_EMAIL": "legacy@example.com", "GMAIL_APP_PASSWORD": "legacy-password"}.get(key),
        ):
            legacy = command._legacy_food_costa_mailbox(set())
        self.assertEqual(legacy[0], food_costa)
        self.assertEqual(legacy[1], "legacy@example.com")

        restaurant_a = self._restaurant("Backfill A", "backfill-a")
        restaurant_b = self._restaurant("Backfill B", "backfill-b")
        vendor_a = self._vendor(restaurant_a)
        vendor_b = self._vendor(restaurant_b)
        IncomingEmail.objects.create(
            restaurant=restaurant_a,
            vendor=vendor_a,
            message_id="same-backfill-id",
            subject="A",
            body="A",
            received_at=timezone.now(),
            processing_status=IncomingEmail.ProcessingStatus.FAILED,
        )
        IncomingEmail.objects.create(
            restaurant=restaurant_b,
            vendor=vendor_b,
            message_id="same-backfill-id",
            subject="B",
            body="B",
            received_at=timezone.now(),
            processing_status=IncomingEmail.ProcessingStatus.FAILED,
        )

        stats = BackfillCommand()._retry_failed_messages(
            ["same-backfill-id"], dry_run=True, restaurant=restaurant_a
        )

        self.assertEqual(stats["checked"], 1)
        self.assertEqual(
            IncomingEmail.objects.filter(
                restaurant=restaurant_b,
                message_id="same-backfill-id",
                processing_status=IncomingEmail.ProcessingStatus.FAILED,
            ).count(),
            1,
        )


class GmailBackfillCommandTests(TestCase):
    def setUp(self):
        self.restaurant = Restaurant.objects.get(slug="food-costa")
        self.vendor = Vendor.objects.create(
            restaurant=self.restaurant,
            name="HomeBytes", email_address="info@homebytes.co.in",
            parser_type=Vendor.ParserType.HOMEBYTES,
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
            restaurant=self.restaurant,
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
            restaurant=self.restaurant,
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
            restaurant=self.restaurant,
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
            IncomingEmail.ProcessingStatus.INVALID,
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
            restaurant=self.restaurant,
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
        self.restaurant = Restaurant.objects.get(slug="food-costa")
        self.vendor = Vendor.objects.create(
            restaurant=self.restaurant,
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
