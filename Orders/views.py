from django.db.models import Count, F, Q
from django.conf import settings
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from Orders.models import Order


def order_list(request):
    orders = (
        Order.objects.select_related("vendor", "customer", "train")
        .prefetch_related("items")
        .filter(delivery_date__date=timezone.localdate())
        .order_by(
            F("delivery_date").desc(nulls_last=True),
            F("booking_date").desc(nulls_last=True),
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
    schedule = order.delivery_date or order.booking_date

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
