"""RailRadar provider adapter for local TrainPOS V3 train-status checks.

This module deliberately has no database or dashboard dependencies.  It turns
the RailRadar live-status response into the small, provider-neutral structure
that later V3 work can consume.
"""

import json
import os
import socket
from datetime import date, datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.core.cache import cache
from django.utils import timezone


RAILRADAR_LIVE_STATUS_URL = "https://api.railradar.in/v1/trains/{train_number}/live"
DEFAULT_TARGET_STATION = "GGC"
REQUEST_TIMEOUT_SECONDS = 10
CACHE_TTL_SECONDS = 5 * 60


class TrainStatusError(Exception):
    """A controlled, safe-to-display failure from the train-status provider."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def get_train_status(train_number, journey_date, target_station=DEFAULT_TARGET_STATION):
    """Return RailRadar live status normalized for one station on a journey date.

    ``journey_date`` may be a ``datetime.date`` or an ISO-8601 date string.
    The upstream response itself is intentionally not returned.
    """
    normalized_train_number = str(train_number).strip()
    if not normalized_train_number.isdigit() or len(normalized_train_number) != 5:
        raise TrainStatusError(
            "INVALID_TRAIN_NUMBER", "Train number must contain exactly five digits."
        )

    normalized_date = _normalize_journey_date(journey_date)
    normalized_station = str(target_station).strip().upper()
    if not normalized_station:
        raise TrainStatusError("INVALID_STATION", "A target station code is required.")

    api_key = os.getenv("RAILRADAR_API_KEY")
    if not api_key:
        raise TrainStatusError(
            "CONFIGURATION", "Set RAILRADAR_API_KEY in the local environment before querying RailRadar."
        )

    query = urlencode({"date": normalized_date.isoformat(), "haltsOnly": "true"})
    endpoint = RAILRADAR_LIVE_STATUS_URL.format(train_number=normalized_train_number)
    request = Request(
        f"{endpoint}?{query}",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="GET",
    )

    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise _http_error(error.code) from error
    except (TimeoutError, socket.timeout) as error:
        raise TrainStatusError(
            "TIMEOUT", "RailRadar did not respond before the request timed out."
        ) from error
    except URLError as error:
        raise TrainStatusError(
            "UNAVAILABLE", "RailRadar is temporarily unavailable. Please try again later."
        ) from error
    except (AttributeError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TrainStatusError(
            "MALFORMED_RESPONSE", "RailRadar returned an unreadable response."
        ) from error

    return {
        **_normalize_response(payload, normalized_train_number, normalized_station),
        "journey_date": normalized_date.isoformat(),
        "fetched_at": timezone.now().isoformat(),
    }


def _normalize_journey_date(journey_date):
    if isinstance(journey_date, date):
        return journey_date
    if isinstance(journey_date, str):
        try:
            return date.fromisoformat(journey_date)
        except ValueError as error:
            raise TrainStatusError(
                "INVALID_DATE", "Journey date must use YYYY-MM-DD format."
            ) from error
    raise TrainStatusError("INVALID_DATE", "Journey date must use YYYY-MM-DD format.")


def _http_error(status_code):
    messages = {
        401: ("INVALID_API_KEY", "RailRadar rejected the configured API key."),
        404: (
            "TRAIN_NOT_AVAILABLE",
            "The train was not found or is not running on the selected journey date.",
        ),
        429: ("RATE_LIMITED", "RailRadar rate limit reached. Please try again later."),
        503: ("UNAVAILABLE", "RailRadar is temporarily unavailable. Please try again later."),
    }
    code, message = messages.get(
        status_code,
        ("UPSTREAM_ERROR", "RailRadar returned an unexpected service error."),
    )
    return TrainStatusError(code, message)


def _normalize_response(payload, train_number, target_station):
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise TrainStatusError(
            "MALFORMED_RESPONSE", "RailRadar returned an incomplete response."
        )

    data = payload.get("data")
    route = data.get("route") if isinstance(data, dict) else None
    if not isinstance(route, list):
        raise TrainStatusError(
            "MALFORMED_RESPONSE", "RailRadar returned an incomplete response."
        )

    target_stop = next(
        (
            stop
            for stop in route
            if isinstance(stop, dict)
            and str(stop.get("stationCode", "")).upper() == target_station
        ),
        None,
    )
    if target_stop is None:
        raise TrainStatusError(
            "TARGET_STATION_NOT_FOUND",
            f"Train {train_number} does not have a {target_station} stop in this journey response.",
        )

    current_location = _station_label(data.get("currentLocation"))
    next_station = _station_label(data.get("nextHalt"))
    expected_arrival = (
        target_stop.get("expectedArrival")
        or target_stop.get("expectedArrivalTime")
        or target_stop.get("actualArrival")
    )

    return {
        "train_number": str(data.get("trainNumber") or train_number),
        "target_station": target_station,
        "journey_date": None,
        "scheduled_arrival": target_stop.get("scheduledArrival"),
        "expected_arrival": expected_arrival,
        "delay_minutes": target_stop.get("delayArrival", data.get("delayMinutes")),
        "current_location": current_location,
        "next_station": next_station,
        "status": data.get("status") or target_stop.get("status"),
        "target_status": target_stop.get("status"),
        "provider": "RailRadar",
        "available": True,
        "raw_available": True,
    }


def _station_label(location):
    if not isinstance(location, dict):
        return None
    return location.get("stationName") or location.get("stationCode")


def get_live_status_for_order(
    order, target_station=DEFAULT_TARGET_STATION, provider=None, force_refresh=False
):
    """Resolve the correct train run for an order and return a safe status value.

    Provider failures intentionally become an unavailable result so a live-data
    issue can never prevent the operational Orders page from rendering.
    """
    provider = provider or get_train_status
    target_station = target_station.upper()
    operational_date = _operational_date(order)
    if operational_date is None:
        return _unavailable_status(order.train.train_number, target_station, "MISSING_ORDER_DATE")

    explicit_journey_date = order.train_journey_date
    if explicit_journey_date:
        status = _fetch_cached_status(
            order.train.train_number,
            explicit_journey_date,
            target_station,
            provider,
            force_refresh,
        )
        if isinstance(status, TrainStatusError):
            return _unavailable_status(
                order.train.train_number, target_station, status.code
            )
        if _matches_operational_date(status, operational_date):
            return _resolved_status(status, explicit_journey_date)
        return _unavailable_status(
            order.train.train_number, target_station, "RUN_DATE_MISMATCH"
        )

    candidates = (operational_date, operational_date - timedelta(days=1))
    matches = _matching_candidates(
        order.train.train_number,
        candidates,
        operational_date,
        target_station,
        provider,
        force_refresh,
    )
    if len(matches) > 1:
        return _unavailable_status(order.train.train_number, target_station, "AMBIGUOUS_RUN")
    if len(matches) == 1:
        journey_date, status = matches[0]
        return _resolved_status(status, journey_date)

    d_minus_two = operational_date - timedelta(days=2)
    matches = _matching_candidates(
        order.train.train_number,
        (d_minus_two,),
        operational_date,
        target_station,
        provider,
        force_refresh,
    )
    if len(matches) == 1:
        journey_date, status = matches[0]
        return _resolved_status(status, journey_date)
    return _unavailable_status(order.train.train_number, target_station, "RUN_NOT_RESOLVED")


def get_dashboard_status(status, now=None):
    """Add staff-facing state and urgency without leaking provider structures."""
    if not status.get("available"):
        return {
            **status,
            "display_state": "UNAVAILABLE",
            "urgency": "UNKNOWN",
            "arriving_in": None,
            "scheduled_arrival_display": None,
            "expected_arrival_display": None,
            "updated_label": None,
        }

    provider_status = str(status.get("status") or "").lower()
    target_status = str(status.get("target_status") or "").lower()
    scheduled_arrival_display = _time_display(status.get("scheduled_arrival"))
    expected_arrival_display = _time_display(status.get("expected_arrival"))
    updated_label = _updated_label(status.get("fetched_at"), now)
    if target_status in {"arrived", "departed", "passed", "reached"} or provider_status in {
        "arrived",
        "completed",
        "terminated",
    }:
        return {
            **status,
            "display_state": "ARRIVED",
            "urgency": "ARRIVED",
            "arriving_in": None,
            "scheduled_arrival_display": scheduled_arrival_display,
            "expected_arrival_display": expected_arrival_display,
            "updated_label": updated_label,
        }
    if provider_status in {"not-started", "not_started", "scheduled"}:
        return {
            **status,
            "display_state": "NOT_STARTED",
            "urgency": "UNKNOWN",
            "arriving_in": None,
            "scheduled_arrival_display": scheduled_arrival_display,
            "expected_arrival_display": expected_arrival_display,
            "updated_label": updated_label,
        }

    expected_arrival = _parse_provider_datetime(status.get("expected_arrival"))
    if expected_arrival is None:
        return {
            **status,
            "display_state": "LIVE",
            "urgency": "UNKNOWN",
            "arriving_in": None,
            "scheduled_arrival_display": scheduled_arrival_display,
            "expected_arrival_display": expected_arrival_display,
            "updated_label": updated_label,
        }

    now = now or timezone.localtime()
    now = timezone.localtime(now) if timezone.is_aware(now) else timezone.make_aware(now)
    remaining = expected_arrival - now
    if remaining.total_seconds() <= 0:
        return {
            **status,
            "display_state": "LIVE",
            "urgency": "URGENT",
            "arriving_in": None,
            "scheduled_arrival_display": scheduled_arrival_display,
            "expected_arrival_display": expected_arrival_display,
            "updated_label": updated_label,
        }

    minutes = int(remaining.total_seconds() // 60)
    if minutes <= 30:
        urgency = "URGENT"
    elif minutes <= 60:
        urgency = "APPROACHING"
    else:
        urgency = "NORMAL"
    return {
        **status,
        "display_state": "LIVE",
        "urgency": urgency,
        "arriving_in": _duration_label(minutes),
        "scheduled_arrival_display": scheduled_arrival_display,
        "expected_arrival_display": expected_arrival_display,
        "updated_label": updated_label,
    }


def refresh_live_status_for_order(order, journey_date, target_station=DEFAULT_TARGET_STATION, provider=None):
    """Bypass the page-load cache for one already-resolved train run."""
    provider = provider or get_train_status
    try:
        resolved_journey_date = _normalize_journey_date(journey_date)
    except TrainStatusError as error:
        return _unavailable_status(order.train.train_number, target_station, error.code)

    status = _fetch_cached_status(
        order.train.train_number,
        resolved_journey_date,
        target_station.upper(),
        provider,
        force_refresh=True,
    )
    if isinstance(status, TrainStatusError):
        return _unavailable_status(order.train.train_number, target_station, status.code)
    if not _matches_operational_date(status, _operational_date(order)):
        return _unavailable_status(
            order.train.train_number, target_station, "RUN_DATE_MISMATCH"
        )
    return _resolved_status(status, resolved_journey_date)


def _matching_candidates(
    train_number, candidates, operational_date, target_station, provider, force_refresh
):
    matches = []
    for journey_date in candidates:
        status = _fetch_cached_status(
            train_number, journey_date, target_station, provider, force_refresh
        )
        if isinstance(status, TrainStatusError):
            continue
        if _matches_operational_date(status, operational_date):
            matches.append((journey_date, status))
    return matches


def _fetch_cached_status(
    train_number, journey_date, target_station, provider, force_refresh=False
):
    cache_key = _cache_key(train_number, journey_date, target_station)
    cached_status = cache.get(cache_key)
    if cached_status is not None and not force_refresh:
        return cached_status
    try:
        status = provider(train_number, journey_date, target_station)
    except TrainStatusError as error:
        return error
    status = {**status, "journey_date": journey_date.isoformat()}
    cache.set(cache_key, status, CACHE_TTL_SECONDS)
    return status


def _cache_key(train_number, journey_date, target_station):
    return f"trainpos:live-status:{train_number}:{journey_date.isoformat()}:{target_station}"


def _operational_date(order):
    if order.order_date is None:
        return None
    return timezone.localtime(order.order_date).date()


def _matches_operational_date(status, operational_date):
    arrival = _parse_provider_datetime(
        status.get("expected_arrival") or status.get("scheduled_arrival")
    )
    return arrival is not None and timezone.localtime(arrival).date() == operational_date


def _parse_provider_datetime(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return timezone.make_aware(parsed) if timezone.is_naive(parsed) else parsed


def _resolved_status(status, journey_date):
    return {**status, "journey_date": journey_date.isoformat(), "available": True}


def _unavailable_status(train_number, target_station, reason):
    return {
        "train_number": str(train_number),
        "target_station": target_station,
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
        "fetched_at": None,
        "reason": reason,
    }


def _duration_label(total_minutes):
    hours, minutes = divmod(total_minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def _time_display(value):
    parsed = _parse_provider_datetime(value)
    if parsed is None:
        return None
    return timezone.localtime(parsed).strftime("%I:%M %p").lstrip("0")


def _updated_label(value, now=None):
    fetched_at = _parse_provider_datetime(value)
    if fetched_at is None:
        return None
    current_time = now or timezone.localtime()
    current_time = (
        timezone.localtime(current_time)
        if timezone.is_aware(current_time)
        else timezone.make_aware(current_time)
    )
    minutes = max(0, int((current_time - fetched_at).total_seconds() // 60))
    if minutes == 0:
        return "Updated just now"
    return f"Updated {minutes} min ago"
