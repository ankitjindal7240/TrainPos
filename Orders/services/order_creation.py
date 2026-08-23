from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from Orders.models import Customer, IncomingEmail, Order, OrderItem, Train


def _to_decimal(value):
    if value is None or value == "":
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"Invalid monetary value: {value}") from error


def _parse_order_datetime(order_date):
    if not order_date:
        return None
    if isinstance(order_date, datetime):
        return timezone.make_aware(order_date) if timezone.is_naive(order_date) else order_date
    try:
        return timezone.make_aware(datetime.fromisoformat(str(order_date)))
    except ValueError as error:
        raise ValueError(f"Unsupported order date/time: {order_date}") from error


def _parse_train_journey_date(train_journey_date):
    if not train_journey_date:
        return None
    if isinstance(train_journey_date, datetime):
        return train_journey_date.date()
    if isinstance(train_journey_date, date):
        return train_journey_date
    try:
        return date.fromisoformat(str(train_journey_date))
    except ValueError as error:
        raise ValueError(
            f"Unsupported train journey date: {train_journey_date}"
        ) from error


def _get_customer(data):
    customer_name = (data.get("customer_name") or "").strip()
    customer_phone = (data.get("customer_phone") or "").strip()

    if customer_phone:
        customer = Customer.objects.filter(phone=customer_phone).first()
        if customer:
            if customer_name and customer.name in {"", "Unknown Customer"}:
                customer.name = customer_name
                customer.save(update_fields=["name"])
            return customer

    return Customer.objects.create(
        name=customer_name or "Unknown Customer",
        phone=customer_phone,
    )


def _get_train(data):
    train_number = (data.get("train_number") or "").strip()
    if not train_number:
        raise ValueError("A train number is required to create an order.")

    train_name = (data.get("train_name") or "").strip()
    train, created = Train.objects.get_or_create(
        train_number=train_number,
        defaults={"train_name": train_name},
    )
    if not created and train_name and not train.train_name:
        train.train_name = train_name
        train.save(update_fields=["train_name"])
    return train


def _create_order_items(order, items):
    if not items:
        raise ValueError("At least one order item is required to create an order.")

    for item in items:
        item_name = (item.get("item_name") or "").strip()
        quantity = item.get("quantity")
        if not item_name or not quantity:
            raise ValueError("Every order item requires an item name and quantity.")
        try:
            quantity = int(quantity)
        except (TypeError, ValueError) as error:
            raise ValueError(f"Invalid item quantity: {quantity}") from error
        if quantity < 1:
            raise ValueError("Item quantity must be at least 1.")

        OrderItem.objects.create(
            order=order,
            item_name=item_name,
            description=item.get("description", ""),
            quantity=quantity,
            price=_to_decimal(item.get("price")),
            gst=_to_decimal(item.get("gst")),
            amount=_to_decimal(item.get("amount")),
        )


def create_order_from_incoming_email(incoming_email, data):
    """Create an order from normalized parser data and mark its email processed.

    Reprocessing an email already linked to an order returns that same order.
    """
    try:
        with transaction.atomic():
            incoming_email = (
                IncomingEmail.objects.select_for_update()
                .select_related("vendor")
                .get(pk=incoming_email.pk)
            )

            if incoming_email.order_id:
                return incoming_email.order

            order_number = (data.get("order_number") or "").strip()
            payment_mode = data.get("payment_mode")
            if not order_number:
                raise ValueError("An order number is required to create an order.")
            if payment_mode not in Order.PaymentMode.values:
                raise ValueError("A valid payment mode is required to create an order.")

            customer = _get_customer(data)
            train = _get_train(data)
            order = Order.objects.create(
                vendor=incoming_email.vendor,
                order_number=order_number,
                customer=customer,
                train=train,
                pnr=(data.get("pnr") or "").strip(),
                coach=(data.get("coach") or "").strip(),
                berth=(data.get("berth") or "").strip(),
                delivery_station=(data.get("delivery_station") or "").strip(),
                order_date=_parse_order_datetime(data.get("order_date")),
                train_journey_date=_parse_train_journey_date(
                    data.get("train_journey_date")
                ),
                payment_mode=payment_mode,
                subtotal=_to_decimal(data.get("subtotal")),
                gst=_to_decimal(data.get("gst")),
                discount=_to_decimal(data.get("discount")),
                delivery_charge=_to_decimal(data.get("delivery_charge")),
                total=_to_decimal(data.get("total")),
                status=Order.Status.NEW,
            )
            _create_order_items(order, data.get("order_items"))

            incoming_email.order = order
            incoming_email.processing_status = IncomingEmail.ProcessingStatus.PROCESSED
            incoming_email.error_message = ""
            incoming_email.save(
                update_fields=["order", "processing_status", "error_message"]
            )
            return order
    except Exception as error:
        IncomingEmail.objects.filter(
            pk=incoming_email.pk,
            order__isnull=True,
        ).update(
            processing_status=IncomingEmail.ProcessingStatus.FAILED,
            error_message=str(error),
        )
        raise
