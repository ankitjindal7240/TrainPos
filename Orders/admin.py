from django.conf import settings
from django.contrib import admin, messages
from django.utils import timezone

from Orders.models import (
    Customer,
    IncomingEmail,
    Order,
    Restaurant,
    RestaurantEmailConnection,
    RestaurantMembership,
    Vendor,
)
from Orders.models import add_calendar_year


@admin.register(Restaurant)
class RestaurantAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "owner_contact",
        "created_at",
        "subscription_status",
        "trial_ends_at",
        "subscription_started_at",
        "subscription_ends_at",
        "days_remaining",
        "gmail_connection_status",
        "is_active",
    )
    search_fields = ("name", "slug", "email", "owner_name", "phone")
    list_filter = ("subscription_status", "is_active")
    actions = ("create_trainpos_demo_vendor", "activate_or_extend_subscription_one_year")

    @admin.display(description="Owner / contact")
    def owner_contact(self, restaurant):
        return restaurant.owner_name or restaurant.email or restaurant.phone or "—"

    @admin.display(description="Days remaining")
    def days_remaining(self, restaurant):
        if restaurant.subscription_status == Restaurant.SubscriptionStatus.TRIAL:
            return restaurant.trial_days_remaining
        if restaurant.subscription_status == Restaurant.SubscriptionStatus.ACTIVE:
            return restaurant.subscription_days_remaining
        return 0

    @admin.display(description="Gmail")
    def gmail_connection_status(self, restaurant):
        connection = restaurant.email_connections.order_by("id").first()
        return connection.get_connection_status_display() if connection else "Not configured"

    def get_actions(self, request):
        if not request.user.is_superuser:
            return {}
        return super().get_actions(request)

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    @admin.action(description="Activate / Extend subscription for 1 year")
    def activate_or_extend_subscription_one_year(self, request, queryset):
        now = timezone.now()
        activated = 0
        renewed = 0
        for restaurant in queryset:
            is_early_renewal = (
                restaurant.subscription_status == Restaurant.SubscriptionStatus.ACTIVE
                and restaurant.subscription_ends_at
                and restaurant.subscription_ends_at > now
            )
            activation_base = restaurant.subscription_ends_at if is_early_renewal else now
            if not is_early_renewal:
                restaurant.subscription_started_at = now
                activated += 1
            else:
                renewed += 1
            restaurant.subscription_ends_at = add_calendar_year(activation_base)
            restaurant.subscription_status = Restaurant.SubscriptionStatus.ACTIVE
            restaurant.is_active = True
            restaurant.save(
                update_fields=[
                    "subscription_status",
                    "subscription_started_at",
                    "subscription_ends_at",
                    "is_active",
                    "updated_at",
                ]
            )
        self.message_user(
            request,
            f"Subscription activated for {activated} restaurant(s); extended for {renewed} restaurant(s).",
            level=messages.SUCCESS,
        )

    @admin.action(description="Create TrainPOS Demo Vendor")
    def create_trainpos_demo_vendor(self, request, queryset):
        sender_email = settings.TRAINPOS_DEMO_SENDER_EMAIL
        if not sender_email:
            self.message_user(
                request,
                "Set TRAINPOS_DEMO_SENDER_EMAIL before creating a Demo Vendor.",
                level=messages.ERROR,
            )
            return

        created = 0
        updated = 0
        conflicts = 0
        for restaurant in queryset:
            demo_vendor = Vendor.objects.filter(
                restaurant=restaurant,
                name="TrainPOS Demo Vendor",
            ).first()
            sender_owner = Vendor.objects.filter(
                restaurant=restaurant,
                email_address__iexact=sender_email,
            ).first()
            if sender_owner and sender_owner != demo_vendor:
                conflicts += 1
                continue

            if demo_vendor is None:
                Vendor.objects.create(
                    restaurant=restaurant,
                    name="TrainPOS Demo Vendor",
                    email_address=sender_email,
                    parser_type=Vendor.ParserType.DEMO,
                    is_demo=True,
                    is_active=True,
                )
                created += 1
            else:
                demo_vendor.email_address = sender_email
                demo_vendor.parser_type = Vendor.ParserType.DEMO
                demo_vendor.is_demo = True
                demo_vendor.is_active = True
                demo_vendor.save(
                    update_fields=[
                        "email_address",
                        "parser_type",
                        "is_demo",
                        "is_active",
                    ]
                )
                updated += 1

        if created or updated:
            self.message_user(
                request,
                f"Demo Vendor configured for {created} restaurant(s); {updated} existing vendor(s) updated.",
                level=messages.SUCCESS,
            )
        if conflicts:
            self.message_user(
                request,
                f"Demo Vendor was not changed for {conflicts} restaurant(s) because the configured sender is already assigned to another Vendor.",
                level=messages.ERROR,
            )


@admin.register(RestaurantMembership)
class RestaurantMembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "restaurant", "role", "created_at")
    list_filter = ("role", "restaurant")
    search_fields = ("user__username", "restaurant__name")


@admin.register(RestaurantEmailConnection)
class RestaurantEmailConnectionAdmin(admin.ModelAdmin):
    list_display = ("restaurant", "email_address", "is_active", "connection_status", "last_checked_at")
    list_filter = ("is_active", "connection_status", "restaurant")
    search_fields = ("restaurant__name", "email_address")
    exclude = ("encrypted_app_password",)


@admin.register(Vendor)
class VendorAdmin(admin.ModelAdmin):
    list_display = ("name", "restaurant", "email_address", "parser_type", "is_demo", "is_active")
    list_filter = ("restaurant", "parser_type", "is_demo", "is_active")
    search_fields = ("name", "email_address")


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("name", "restaurant", "phone")
    list_filter = ("restaurant",)
    search_fields = ("name", "phone")


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("order_number", "restaurant", "vendor", "is_demo", "status", "total", "order_date")
    list_filter = ("restaurant", "vendor", "is_demo", "status", "payment_mode")
    search_fields = ("order_number", "customer__name", "customer__phone")


@admin.register(IncomingEmail)
class IncomingEmailAdmin(admin.ModelAdmin):
    list_display = ("message_id", "restaurant", "vendor", "processing_status", "received_at")
    list_filter = ("restaurant", "vendor", "processing_status")
    search_fields = ("message_id", "subject")
