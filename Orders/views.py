from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.db.models import Count, DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from Orders.models import Order


MONEY_FIELD = DecimalField(max_digits=10, decimal_places=2)
ZERO_MONEY = Value(Decimal("0.00"), output_field=MONEY_FIELD)


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


def reports(request):
    period, start_date, end_date, period_label, validation_error = _report_period(request)
    orders = Order.objects.none()
    if not validation_error:
        orders = Order.objects.filter(order_date__date__range=(start_date, end_date))

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
            "period": period,
            "period_label": period_label,
            "start_date": start_date,
            "end_date": end_date,
            "validation_error": validation_error,
            "summary": summary,
            "vendor_breakdown": vendor_breakdown,
        },
    )


def order_list(request):
    orders = (
        Order.objects.select_related("vendor", "customer", "train")
        .prefetch_related("items")
        .filter(order_date__date=timezone.localdate())
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
    return render(
        request,
        "Orders/order_list.html",
        {"orders": orders, "summary": summary},
    )


def bill_print(request, pk):
    order = get_object_or_404(
        Order.objects.select_related("vendor", "customer", "train").prefetch_related("items"),
        pk=pk,
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
            "restaurant_phone": getattr(settings, "RESTAURANT_PHONE", "+91 00000 00000"),
        },
    )
