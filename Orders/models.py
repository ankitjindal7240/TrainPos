from django.db import models


class Vendor(models.Model):
    name = models.CharField(max_length=100, unique=True)
    email_address = models.EmailField(unique=True)
    identifier = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class Customer(models.Model):
    name = models.CharField(max_length=255)
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)

    def __str__(self):
        return self.name


class Train(models.Model):
    train_number = models.CharField(max_length=20)
    train_name = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return f"{self.train_number} - {self.train_name}".rstrip(" - ")


class Order(models.Model):
    class Status(models.TextChoices):
        NEW = "NEW", "New"
        ACCEPTED = "ACCEPTED", "Accepted"
        PREPARING = "PREPARING", "Preparing"
        READY = "READY", "Ready"
        DELIVERED = "DELIVERED", "Delivered"
        CANCELLED = "CANCELLED", "Cancelled"

    class PaymentMode(models.TextChoices):
        PRE_PAID = "PRE_PAID", "Pre-paid"
        CASH_ON_DELIVERY = "CASH_ON_DELIVERY", "Cash on delivery"

    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name="orders")
    order_number = models.CharField(max_length=100)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="orders")
    train = models.ForeignKey(Train, on_delete=models.PROTECT, related_name="orders")
    pnr = models.CharField(max_length=20, blank=True)
    coach = models.CharField(max_length=20, blank=True)
    berth = models.CharField(max_length=20, blank=True)
    delivery_station = models.CharField(max_length=255, blank=True)
    order_date = models.DateTimeField(null=True, blank=True)
    train_journey_date = models.DateField(null=True, blank=True)
    payment_mode = models.CharField(max_length=20, choices=PaymentMode.choices)
    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    gst = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    discount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    delivery_charge = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    bill_printed = models.BooleanField(default=False)
    bill_printed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.NEW,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.vendor} - {self.order_number}"


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    item_name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    quantity = models.PositiveIntegerField()
    price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    gst = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    def __str__(self):
        return f"{self.quantity} x {self.item_name}"


class IncomingEmail(models.Model):
    class ProcessingStatus(models.TextChoices):
        RECEIVED = "RECEIVED", "Received"
        PROCESSED = "PROCESSED", "Processed"
        FAILED = "FAILED", "Failed"
        SKIPPED = "SKIPPED", "Skipped (non-order email)"

    message_id = models.CharField(max_length=255, unique=True)
    vendor = models.ForeignKey(
        Vendor,
        on_delete=models.PROTECT,
        related_name="incoming_emails",
    )
    subject = models.CharField(max_length=500, blank=True)
    body = models.TextField()
    received_at = models.DateTimeField()
    processing_status = models.CharField(
        max_length=10,
        choices=ProcessingStatus.choices,
        default=ProcessingStatus.RECEIVED,
    )
    error_message = models.TextField(blank=True)
    order = models.ForeignKey(
        Order,
        on_delete=models.SET_NULL,
        related_name="incoming_emails",
        null=True,
        blank=True,
    )

    def __str__(self):
        return self.subject or self.message_id
