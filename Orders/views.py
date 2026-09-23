from datetime import date, datetime, time, timedelta
from decimal import Decimal
import json

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.forms import AuthenticationForm
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction
from django.db.models import Count, DecimalField, F, Max, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.defaultfilters import slugify
from django.utils import timezone
from django.views.decorators.http import require_POST

from Orders.forms import RestaurantEmailConnectionForm, SignupForm
from Orders.models import Order, Restaurant, RestaurantEmailConnection, RestaurantMembership
from Orders.services.gmail_connection import test_gmail_connection
from Orders.services.train_status import (
    get_dashboard_status,
    get_live_status_for_order,
    refresh_live_status_for_order,
)
from Orders.services.tenancy import owner_required, restaurant_access_required


MONEY_FIELD = DecimalField(max_digits=10, decimal_places=2)
ZERO_MONEY = Value(Decimal("0.00"), output_field=MONEY_FIELD)


def landing(request):
    site_url = settings.TRAINPOS_SITE_URL or request.build_absolute_uri("/").rstrip("/")
    organization = {
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": "TrainPOS",
        "url": site_url,
        "logo": f"{site_url}/static/Orders/img/trainpos-mark.svg",
    }
    if settings.TRAINPOS_CONTACT_EMAIL:
        organization["email"] = settings.TRAINPOS_CONTACT_EMAIL
    if settings.TRAINPOS_CONTACT_PHONE:
        organization["telephone"] = settings.TRAINPOS_CONTACT_PHONE
    application = {
        "@context": "https://schema.org",
        "@type": "SoftwareApplication",
        "name": "TrainPOS",
        "applicationCategory": "BusinessApplication",
        "operatingSystem": "Web",
        "url": site_url,
        "description": (
            "Train food order management software for railway and eCatering restaurants, "
            "combining automatic order capture, live train ETA, billing and sales reports."
        ),
        "offers": {
            "@type": "Offer",
            "price": "5999",
            "priceCurrency": "INR",
            "description": "TrainPOS annual plan",
        },
    }
    return render(
        request,
        "Orders/landing.html",
        {
            "site_url": site_url,
            "organization_json": json.dumps(organization),
            "application_json": json.dumps(application),
            "trainpos_contact_phone": settings.TRAINPOS_CONTACT_PHONE,
            "trainpos_contact_email": settings.TRAINPOS_CONTACT_EMAIL,
            "trainpos_contact_whatsapp": settings.TRAINPOS_CONTACT_WHATSAPP,
        },
    )


def robots_txt(request):
    site_url = settings.TRAINPOS_SITE_URL or request.build_absolute_uri("/").rstrip("/")
    content = "\n".join(
        [
            "User-agent: *",
            "Allow: /",
            "Disallow: /orders/",
            "Disallow: /reports/",
            "Disallow: /onboarding/",
            "Disallow: /admin/",
            "Disallow: /login/",
            "Disallow: /signup/",
            f"Sitemap: {site_url}/sitemap.xml",
            "",
        ]
    )
    return HttpResponse(content, content_type="text/plain")


def sitemap_xml(request):
    site_url = settings.TRAINPOS_SITE_URL or request.build_absolute_uri("/").rstrip("/")
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"  <url><loc>{site_url}/</loc></url>\n"
        "</urlset>\n"
    )
    return HttpResponse(content, content_type="application/xml")


def _restaurant_slug(name):
    base_slug = slugify(name) or "restaurant"
    slug = base_slug
    suffix = 2
    while Restaurant.objects.filter(slug=slug).exists():
        slug = f"{base_slug}-{suffix}"
        suffix += 1
    return slug


def signup(request):
    if request.user.is_authenticated:
        return redirect("admin:index" if request.user.is_staff else "order_list")

    form = SignupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                email = form.cleaned_data["email"]
                user = get_user_model().objects.create_user(
                    username=email,
                    email=email,
                    password=form.cleaned_data["password"],
                    first_name=form.cleaned_data["owner_name"],
                )
                restaurant = Restaurant.objects.create(
                    name=form.cleaned_data["restaurant_name"],
                    slug=_restaurant_slug(form.cleaned_data["restaurant_name"]),
                    owner_name=form.cleaned_data["owner_name"],
                    email=email,
                    phone=form.cleaned_data["phone"],
                )
                RestaurantMembership.objects.create(
                    user=user,
                    restaurant=restaurant,
                    role=RestaurantMembership.Role.OWNER,
                )
        except IntegrityError:
            form.add_error("email", "An account with this email already exists.")
        else:
            login(request, user)
            return redirect("onboarding_email")
    return render(request, "Orders/signup.html", {"form": form})


def login_view(request):
    if request.user.is_authenticated:
        return redirect("admin:index" if request.user.is_staff else "order_list")

    form = AuthenticationForm(request, data=request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.get_user()
        if not (user.is_staff or user.is_superuser) and not user.restaurant_memberships.filter(
            restaurant__is_active=True
        ).exists():
            form.add_error(None, "This account does not have an active restaurant membership.")
        else:
            login(request, user)
            return redirect("admin:index" if user.is_staff or user.is_superuser else "order_list")
    return render(request, "Orders/login.html", {"form": form})


@require_POST
def logout_view(request):
    logout(request)
    return redirect("landing")


@owner_required
def onboarding_email(request):
    restaurant = request.restaurant
    connection = restaurant.email_connections.order_by("id").first()
    form = RestaurantEmailConnectionForm(
        request.POST or None,
        initial={"email_address": connection.email_address} if connection else None,
    )
    if request.method == "POST" and form.is_valid():
        connection = connection or RestaurantEmailConnection(restaurant=restaurant)
        connection.email_address = form.cleaned_data["email_address"]
        try:
            connection.set_app_password(form.cleaned_data["app_password"])
        except ImproperlyConfigured:
            form.add_error(None, "Email connection setup is temporarily unavailable. Please contact TrainPOS.")
        else:
            action = request.POST.get("action", "save")
            if action == "test":
                try:
                    connection_ok = test_gmail_connection(
                        connection.email_address, connection.get_app_password()
                    )
                except ImproperlyConfigured:
                    connection_ok = False
                if connection_ok:
                    connection.connection_status = RestaurantEmailConnection.ConnectionStatus.CONNECTED
                    connection.is_active = True
                    connection.last_checked_at = timezone.now()
                    messages.success(request, "Email connected successfully.")
                else:
                    connection.connection_status = RestaurantEmailConnection.ConnectionStatus.ERROR
                    connection.is_active = False
                    messages.error(
                        request,
                        "Could not connect to Gmail. Please verify the email and App Password.",
                    )
            else:
                connection.connection_status = RestaurantEmailConnection.ConnectionStatus.NOT_CONFIGURED
                connection.is_active = False
                messages.success(request, "Email connection saved. Test it before continuing.")
            connection.save()
            return redirect("onboarding_email")
    return render(
        request,
        "Orders/onboarding_email.html",
        {"form": form, "connection": connection, "restaurant": restaurant},
    )


def _report_period(request):
    today = timezone.localdate()
    period = request.GET.get("period", "today")
    periods = {
        "today": (today, today, "Today"),
        "yesterday": (today - timedelta(days=1), today - timedelta(days=1), "Yesterday"),
        "week": (
            today - timedelta(days=today.weekday()),
            today + timedelta(days=6 - today.weekday()),
            "This Week",
        ),
        "month": (today.replace(day=1), today, "This Month"),
    }
    if period in periods:
        return period, *periods[period], None

    if period != "custom":
        return "today", *periods["today"], "Please select a valid report period."

    from_date = request.GET.get("from_date", "")
    to_date = request.GET.get("to_date", "")
    if not from_date or not to_date:
        return "custom", None, None, "Custom Date Range", "Choose both From Date and To Date."
    try:
        start_date = date.fromisoformat(from_date)
        end_date = date.fromisoformat(to_date)
    except ValueError:
        return "custom", None, None, "Custom Date Range", "Enter valid dates."
    if start_date > end_date:
        return "custom", None, None, "Custom Date Range", "From Date cannot be after To Date."
    return "custom", start_date, end_date, "Custom Date Range", None


def _report_summary(queryset):
    return queryset.aggregate(
        total_orders=Count("id"),
        cod_value=Coalesce(
            Sum("total", filter=Q(payment_mode=Order.PaymentMode.CASH_ON_DELIVERY)),
            ZERO_MONEY,
            output_field=MONEY_FIELD,
        ),
        online_value=Coalesce(
            Sum("total", filter=Q(payment_mode=Order.PaymentMode.PRE_PAID)),
            ZERO_MONEY,
            output_field=MONEY_FIELD,
        ),
    )


@restaurant_access_required
def reports(request):
    restaurant = request.restaurant
    period, start_date, end_date, period_label, validation_error = _report_period(request)
    orders = Order.objects.none()
    if not validation_error:
        orders = Order.objects.filter(
            restaurant=restaurant,
            order_date__date__range=(start_date, end_date),
            is_demo=False,
        )

    summary = _report_summary(orders)
    summary["net_sales"] = summary["cod_value"] + summary["online_value"]
    vendor_breakdown = (
        orders.values("vendor__name")
        .annotate(
            total_orders=Count("id"),
            cod_value=Coalesce(
                Sum("total", filter=Q(payment_mode=Order.PaymentMode.CASH_ON_DELIVERY)),
                ZERO_MONEY,
                output_field=MONEY_FIELD,
            ),
            online_value=Coalesce(
                Sum("total", filter=Q(payment_mode=Order.PaymentMode.PRE_PAID)),
                ZERO_MONEY,
                output_field=MONEY_FIELD,
            ),
        )
        .annotate(net_sales=F("cod_value") + F("online_value"))
        .order_by("vendor__name")
    )
    return render(
        request,
        "Orders/reports.html",
        {
            "restaurant": restaurant,
            "period": period,
            "period_label": period_label,
            "start_date": start_date,
            "end_date": end_date,
            "validation_error": validation_error,
            "summary": summary,
            "vendor_breakdown": vendor_breakdown,
            "can_manage_email": request.user.restaurant_memberships.filter(
                restaurant=restaurant,
                role=RestaurantMembership.Role.OWNER,
            ).exists(),
        },
    )


@restaurant_access_required
def order_list(request):
    restaurant = request.restaurant
    orders = (
        Order.objects.select_related("vendor", "customer", "train")
        .prefetch_related("items")
        .filter(restaurant=restaurant, order_date__date=timezone.localdate())
        .order_by(
            F("order_date").desc(nulls_last=True),
            "-created_at",
        )
    )
    summary = orders.aggregate(
        total_orders=Count("id"),
        new_orders=Count("id", filter=Q(status=Order.Status.NEW)),
        preparing_orders=Count("id", filter=Q(status=Order.Status.PREPARING)),
        ready_orders=Count("id", filter=Q(status=Order.Status.READY)),
        cancelled_orders=Count("id", filter=Q(status=Order.Status.CANCELLED)),
    )
    orders = list(orders)
    _attach_live_train_statuses(orders)
    return render(
        request,
        "Orders/order_list.html",
        {
            "orders": orders,
            "summary": summary,
            "dashboard_version": _dashboard_version(restaurant),
            "dashboard_date": timezone.localdate(),
            "restaurant": restaurant,
            "can_manage_email": request.user.restaurant_memberships.filter(
                restaurant=restaurant,
                role=RestaurantMembership.Role.OWNER,
            ).exists(),
            "has_active_email_connection": restaurant.email_connections.filter(
                is_active=True,
                connection_status=RestaurantEmailConnection.ConnectionStatus.CONNECTED,
            ).exists(),
        },
    )


def _attach_live_train_statuses(orders):
    """Resolve each distinct train/run once for the operational dashboard."""
    statuses = {}
    for order in orders:
        operational_date = timezone.localtime(order.order_date).date()
        key = (
            order.train.train_number,
            order.train_journey_date or operational_date,
        )
        if key not in statuses:
            statuses[key] = get_dashboard_status(get_live_status_for_order(order))
        order.live_train_status = statuses[key]

    for order in orders:
        live_status = order.live_train_status
        journey_date = live_status.get("journey_date") or (
            order.train_journey_date or timezone.localtime(order.order_date).date()
        )
        order.live_train_run_key = f"{order.train.train_number}:{journey_date}:GGC"


@require_POST
@restaurant_access_required
def refresh_order_train_status(request, pk):
    """Manually refresh one resolved run without changing any Order data."""
    restaurant = request.restaurant
    order = get_object_or_404(
        Order.objects.select_related("train"),
        pk=pk,
        restaurant=restaurant,
    )
    journey_date = request.POST.get("journey_date", "")
    if not journey_date:
        return JsonResponse({"ok": False, "error": "Refresh failed."}, status=400)

    run_key = f"{order.train.train_number}:{journey_date}:GGC"
    lock_key = f"trainpos:live-status:refresh:{run_key}"
    if not cache.add(lock_key, True, timeout=30):
        return JsonResponse(
            {"ok": False, "error": "A refresh is already in progress."}, status=429
        )

    try:
        status = get_dashboard_status(refresh_live_status_for_order(order, journey_date))
    finally:
        cache.delete(lock_key)

    if not status["available"]:
        return JsonResponse({"ok": False, "error": "Refresh failed."}, status=503)
    return JsonResponse({"ok": True, "run_key": run_key, "status": _live_status_payload(status)})


def _live_status_payload(status):
    return {
        "display_state": status["display_state"],
        "urgency": status["urgency"],
        "scheduled_arrival": status["scheduled_arrival_display"],
        "expected_arrival": status["expected_arrival_display"],
        "delay_minutes": status["delay_minutes"],
        "arriving_in": status["arriving_in"],
        "updated_label": status["updated_label"],
        "journey_date": status["journey_date"],
        "fetched_at": status.get("fetched_at"),
        "current_location": status.get("current_location"),
        "next_station": status.get("next_station"),
    }


@restaurant_access_required
def order_dashboard_version(request):
    """Return a minimal, database-only token for today's operational orders."""
    return JsonResponse(_dashboard_version(request.restaurant))


def _dashboard_version(restaurant):
    today = timezone.localdate()
    start = timezone.make_aware(datetime.combine(today, time.min))
    end = start + timedelta(days=1)
    version = Order.objects.filter(
        restaurant=restaurant,
        order_date__gte=start,
        order_date__lt=end,
    ).aggregate(order_count=Count("id"), latest_order_id=Max("id"))
    latest_order_id = version["latest_order_id"] or 0
    order_count = version["order_count"]
    return {
        "date": today.isoformat(),
        "order_count": order_count,
        "latest_order_id": latest_order_id,
        "token": f"{today.isoformat()}:{order_count}:{latest_order_id}",
    }


@restaurant_access_required
def bill_print(request, pk):
    restaurant = request.restaurant
    order = get_object_or_404(
        Order.objects.select_related("vendor", "customer", "train").prefetch_related("items"),
        pk=pk,
        restaurant=restaurant,
    )
    schedule = order.order_date

    if not order.bill_printed:
        order.bill_printed = True
        order.bill_printed_at = timezone.now()
        order.save(update_fields=["bill_printed", "bill_printed_at"])

    if order.payment_mode == Order.PaymentMode.CASH_ON_DELIVERY:
        payment_label = "COD"
        advance = 0
        amount_to_collect = order.total
    else:
        payment_label = "ONLINE"
        advance = order.total
        amount_to_collect = 0

    return render(
        request,
        "Orders/bill_print.html",
        {
            "order": order,
            "schedule": schedule,
            "payment_label": payment_label,
            "advance": advance,
            "tax": 0,
            "amount_to_collect": amount_to_collect,
            "restaurant_phone": restaurant.phone
            or getattr(settings, "RESTAURANT_PHONE", "+91 00000 00000"),
        },
    )
