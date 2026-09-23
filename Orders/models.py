from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone


def default_trial_end():
    return timezone.now() + timedelta(days=15)


def add_calendar_year(value):
    """Return the same local calendar date next year, handling 29 February."""
    try:
        return value.replace(year=value.year + 1)
    except ValueError:
        return value.replace(year=value.year + 1, month=2, day=28)


class Restaurant(models.Model):
    class SubscriptionStatus(models.TextChoices):
        TRIAL = "TRIAL", "Trial"
        ACTIVE = "ACTIVE", "Active"
        EXPIRED = "EXPIRED", "Expired"
        SUSPENDED = "SUSPENDED", "Suspended"

    name = models.CharField(max_length=255)
    slug = models.SlugField(unique=True)
    owner_name = models.CharField(max_length=255, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    is_active = models.BooleanField(default=True)
    trial_started_at = models.DateTimeField(default=timezone.now)
    trial_ends_at = models.DateTimeField(default=default_trial_end)
    subscription_started_at = models.DateTimeField(null=True, blank=True)
    subscription_ends_at = models.DateTimeField(null=True, blank=True)
    subscription_status = models.CharField(
        max_length=20,
        choices=SubscriptionStatus.choices,
        default=SubscriptionStatus.TRIAL,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def is_trial_active(self):
        return (
            self.is_active
            and self.subscription_status == self.SubscriptionStatus.TRIAL
            and timezone.now() <= self.trial_ends_at
        )

    @property
    def trial_days_remaining(self):
        if not self.is_trial_active:
            return 0
        return max(0, (self.trial_ends_at.date() - timezone.localdate()).days)

    @property
    def has_access(self):
        if not self.is_active or self.subscription_status == self.SubscriptionStatus.SUSPENDED:
            return False
        if self.subscription_status == self.SubscriptionStatus.TRIAL:
            return self.is_trial_active
        if self.subscription_status == self.SubscriptionStatus.ACTIVE:
            return bool(
                self.subscription_ends_at
                and timezone.now() <= self.subscription_ends_at
            )
        return False

    @property
    def subscription_days_remaining(self):
        if (
            self.subscription_status != self.SubscriptionStatus.ACTIVE
            or not self.subscription_ends_at
            or not self.has_access
        ):
            return 0
        return max(0, (self.subscription_ends_at.date() - timezone.localdate()).days)

    @property
    def access_expiry_kind(self):
        if self.subscription_status == self.SubscriptionStatus.TRIAL and not self.is_trial_active:
            return "trial"
        if (
            self.subscription_status == self.SubscriptionStatus.ACTIVE
            and (not self.subscription_ends_at or timezone.now() > self.subscription_ends_at)
        ):
            return "subscription"
        if self.subscription_status == self.SubscriptionStatus.EXPIRED:
            return "subscription" if self.subscription_started_at else "trial"
        return None

    def mark_expired_if_needed(self):
        if self.access_expiry_kind and self.subscription_status != self.SubscriptionStatus.EXPIRED:
            self.subscription_status = self.SubscriptionStatus.EXPIRED
            self.save(update_fields=["subscription_status", "updated_at"])

    def __str__(self):
        return self.name


class RestaurantMembership(models.Model):
    class Role(models.TextChoices):
        OWNER = "OWNER", "Owner"
        STAFF = "STAFF", "Staff"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="restaurant_memberships",
    )
    restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.STAFF)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "restaurant"],
                name="unique_restaurant_membership",
            )
        ]

    def __str__(self):
        return f"{self.user} - {self.restaurant} ({self.role})"


class RestaurantEmailConnection(models.Model):
    class ConnectionStatus(models.TextChoices):
        NOT_CONFIGURED = "NOT_CONFIGURED", "Not configured"
        CONNECTED = "CONNECTED", "Connected"
        ERROR = "ERROR", "Error"

    restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.CASCADE,
        related_name="email_connections",
    )
    email_address = models.EmailField()
    encrypted_app_password = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    connection_status = models.CharField(
        max_length=20,
        choices=ConnectionStatus.choices,
        default=ConnectionStatus.NOT_CONFIGURED,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["restaurant", "email_address"],
                name="unique_restaurant_email_connection",
            )
        ]

    def set_app_password(self, app_password):
        from Orders.services.credentials import encrypt_app_password

        self.encrypted_app_password = encrypt_app_password(app_password)

    def get_app_password(self):
        from Orders.services.credentials import decrypt_app_password

        return decrypt_app_password(self.encrypted_app_password)

    def __str__(self):
        return f"{self.restaurant} - {self.email_address}"


class Vendor(models.Model):
    class ParserType(models.TextChoices):
        DEMO = "DEMO", "TrainPOS Demo"
        RAILRESTRO = "RAILRESTRO", "RailRestro"
        HOMEBYTES = "HOMEBYTES", "HomeBytes"
        RAJBHOG = "RAJBHOG", "Rajbhog Khana"
        RAILRECIPE = "RAILRECIPE", "RailRecipe"

    restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.PROTECT,
        related_name="vendors",
    )
    name = models.CharField(max_length=100)
    email_address = models.EmailField()
    parser_type = models.CharField(
        max_length=20,
        choices=ParserType.choices,
        default=ParserType.RAILRESTRO,
    )
    is_demo = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    identifier = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["restaurant", "name"], name="unique_vendor_name_per_restaurant"
            ),
            models.UniqueConstraint(
                fields=["restaurant", "email_address"],
                name="unique_vendor_email_per_restaurant",
            ),
        ]

    def __str__(self):
        return self.name


class Customer(models.Model):
    restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.PROTECT,
        related_name="customers",
    )
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

    restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.PROTECT,
        related_name="orders",
    )
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
    is_demo = models.BooleanField(default=False)
    bill_printed = models.BooleanField(default=False)
    bill_printed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.NEW,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["vendor", "order_number"],
                name="unique_order_per_vendor_order_number",
            )
        ]

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
        INVALID = "INVALID", "Invalid (terminal parser/validation failure)"
        SKIPPED = "SKIPPED", "Skipped (non-order email)"

    message_id = models.CharField(max_length=255)
    restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.PROTECT,
        related_name="incoming_emails",
    )
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

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["restaurant", "message_id"],
                name="unique_incoming_email_message_per_restaurant",
            )
        ]

    def __str__(self):
        return self.subject or self.message_id
