from django.core.management.base import BaseCommand, CommandError

from Orders.models import Order
from Orders.services.train_status import get_dashboard_status, get_live_status_for_order


class Command(BaseCommand):
    help = "Locally resolve and display RailRadar status for one TrainPOS order without writing data."

    def add_arguments(self, parser):
        parser.add_argument("order_id", type=int, help="Existing TrainPOS Order primary key.")

    def handle(self, *args, **options):
        try:
            order = Order.objects.select_related("train").get(pk=options["order_id"])
        except Order.DoesNotExist as error:
            raise CommandError("Order does not exist.") from error

        status = get_dashboard_status(get_live_status_for_order(order))
        self.stdout.write(f"Order: {order.order_number}")
        self.stdout.write(f"Train: {status['train_number']}")
        self.stdout.write(f"Station: {status['target_station']}")
        if not status["available"]:
            self.stdout.write("Live status: unavailable")
            return

        self.stdout.write(f"Resolved journey date: {status['journey_date']}")
        self.stdout.write(f"Status: {status['display_state'].replace('_', ' ').title()}")
        self.stdout.write(
            f"Scheduled arrival: {status['scheduled_arrival_display'] or 'Not available'}"
        )
        self.stdout.write(
            f"Expected arrival: {status['expected_arrival_display'] or 'Not available'}"
        )
        self.stdout.write(
            f"Delay: {status['delay_minutes']} minutes"
            if status["delay_minutes"] is not None
            else "Delay: Not available"
        )
        self.stdout.write(f"Urgency: {status['urgency'].title()}")
