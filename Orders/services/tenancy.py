from functools import wraps

from django.contrib.auth.decorators import login_required
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.shortcuts import render

from Orders.models import Restaurant


class TrialExpired(PermissionDenied):
    pass


def get_current_restaurant(request):
    """Return the authenticated user's sole active tenant.

    Platform accounts use Django admin for cross-tenant work. A future tenant
    switcher can make the selected membership explicit when users belong to
    more than one restaurant.
    """
    user = request.user
    if not user.is_authenticated:
        raise PermissionDenied("Authentication is required.")
    if user.is_superuser or user.is_staff:
        raise PermissionDenied("Platform accounts access tenant data through Django admin.")

    memberships = list(
        user.restaurant_memberships.select_related("restaurant").filter(
            restaurant__is_active=True
        )[:2]
    )
    if len(memberships) != 1:
        raise PermissionDenied("A single active restaurant membership is required.")

    restaurant = memberships[0].restaurant
    if not restaurant.has_access:
        expiry_kind = restaurant.access_expiry_kind
        restaurant.mark_expired_if_needed()
        error = TrialExpired("This restaurant does not have an active subscription.")
        error.restaurant = restaurant
        error.expiry_kind = expiry_kind
        raise error
    return restaurant


def restaurant_access_required(view):
    """Authenticate, resolve, and attach the authorized tenant to the request."""
    @login_required
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        try:
            request.restaurant = get_current_restaurant(request)
        except TrialExpired as error:
            return render(
                request,
                "Orders/trial_expired.html",
                {
                    "restaurant": getattr(error, "restaurant", None),
                    "expiry_kind": getattr(error, "expiry_kind", None),
                    "trainpos_contact_phone": settings.TRAINPOS_CONTACT_PHONE,
                    "trainpos_contact_email": settings.TRAINPOS_CONTACT_EMAIL,
                    "trainpos_contact_whatsapp": settings.TRAINPOS_CONTACT_WHATSAPP,
                },
                status=403,
            )
        return view(request, *args, **kwargs)

    return wrapped


def owner_required(view):
    """Restrict tenant configuration to the authenticated restaurant owner."""
    @restaurant_access_required
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        membership = request.user.restaurant_memberships.get(
            restaurant=request.restaurant
        )
        if membership.role != "OWNER":
            raise PermissionDenied("Only restaurant owners can manage email settings.")
        return view(request, *args, **kwargs)

    return wrapped


def get_food_costa_restaurant():
    """Temporary compatibility tenant for the existing env-based Gmail worker."""
    return Restaurant.objects.get(slug="food-costa")
