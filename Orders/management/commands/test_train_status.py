from datetime import date, datetime

from django.core.management.base import BaseCommand, CommandError

from Orders.services.train_status import TrainStatusError, get_train_status


class Command(BaseCommand):
    help = "Locally query RailRadar for one train's status at Gangapur City Junction (GGC)."

    def add_arguments(self, parser):
        parser.add_argument("train_number", help="Five-digit Indian Railways train number.")
        parser.add_argument(
            "--date",
            required=True,
            dest="journey_date",
            help="Journey start date in YYYY-MM-DD format.",
        )

    def handle(self, *args, **options):
        try:
            journey_date = date.fromisoformat(options["journey_date"])
        except ValueError as error:
            raise CommandError("--date must use YYYY-MM-DD format.") from error

        try:
            status = get_train_status(options["train_number"], journey_date, "GGC")
        except TrainStatusError as error:
            raise CommandError(f"RailRadar [{error.code}]: {error}") from error

        self.stdout.write(f"Train: {status['train_number']}")
        self.stdout.write(f"Station: {status['target_station']}")
        self.stdout.write(f"Scheduled arrival: {_arrival_display(status['scheduled_arrival'])}")
        self.stdout.write(f"Expected arrival: {_arrival_display(status['expected_arrival'])}")
        self.stdout.write(f"Delay: {_delay_display(status['delay_minutes'])}")
        self.stdout.write(
            "Current/next location: "
            f"{_display(status['current_location'])} / {_display(status['next_station'])}"
        )
        self.stdout.write(f"Status: {_display(status['status'])}")
        self.stdout.write(f"Provider: {status['provider']}")


def _display(value):
    return str(value) if value is not None else "Not available"


def _delay_display(value):
    return f"{value} minutes" if value is not None else "Not available"


def _arrival_display(value):
    if value is None:
        return "Not available"
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).strftime("%H:%M")
        except ValueError:
            pass
    return str(value)
