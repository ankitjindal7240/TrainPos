from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from Orders.models import Order
from Orders.parsers.railrestro import parse_railrestro_email


class Command(BaseCommand):
    help = (
        "Safely repair RailRestro prepaid totals or missing subtotals from stored source emails."
    )

    def add_arguments(self, parser):
        parser.add_argument("--order-number", help="Repair one specific RailRestro order.")
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply the deterministic financial updates. Without this flag the command is dry-run only.",
        )

    def handle(self, *args, **options):
        orders = Order.objects.filter(
            vendor__name="RailRestro",
            payment_mode=Order.PaymentMode.PRE_PAID,
        ).filter(Q(total=0) | Q(subtotal=0)).prefetch_related("incoming_emails")
        if options["order_number"]:
            orders = orders.filter(order_number=options["order_number"])

        eligible = 0
        skipped = 0
        for order in orders:
            incoming_email = order.incoming_emails.first()
            if incoming_email is None:
                skipped += 1
                self.stdout.write(
                    f"{order.order_number} | ₹{order.subtotal:.2f} | — | "
                    f"₹{order.total:.2f} | SKIP: no stored IncomingEmail"
                )
                continue

            parsed = parse_railrestro_email(incoming_email.body)
            recovered_total = parsed.get("total")
            recovered_subtotal = parsed.get("subtotal") or recovered_total
            if parsed.get("payment_mode") != Order.PaymentMode.PRE_PAID:
                skipped += 1
                self.stdout.write(
                    f"{order.order_number} | ₹{order.subtotal:.2f} | — | "
                    f"₹{order.total:.2f} | SKIP: stored email is not prepaid"
                )
                continue

            repair_total = order.total == 0 and recovered_total is not None and recovered_total > 0
            repair_subtotal = (
                order.subtotal == 0
                and recovered_subtotal is not None
                and recovered_subtotal > 0
            )
            if not repair_total and not repair_subtotal:
                skipped += 1
                self.stdout.write(
                    f"{order.order_number} | ₹{order.subtotal:.2f} | — | "
                    f"₹{order.total:.2f} | SKIP: no deterministic value to repair"
                )
                continue

            if repair_total and repair_subtotal:
                action = "repair total and subtotal"
            elif repair_total:
                action = "repair total"
            else:
                action = "repair subtotal only"

            self.stdout.write(
                f"{order.order_number} | ₹{order.subtotal:.2f} | "
                f"₹{recovered_subtotal:.2f} | ₹{order.total:.2f} | "
                f"{action if options['apply'] else f'eligible: {action}'}"
            )
            if options["apply"]:
                update_fields = ["updated_at"]
                if repair_total:
                    order.total = recovered_total
                    update_fields.append("total")
                if repair_subtotal:
                    order.subtotal = recovered_subtotal
                    update_fields.append("subtotal")
                order.save(update_fields=update_fields)
            eligible += 1

        if options["order_number"] and not eligible and not skipped:
            raise CommandError("No matching RailRestro prepaid order needing repair was found.")
        action = "repaired" if options["apply"] else "eligible (dry run)"
        self.stdout.write(self.style.SUCCESS(f"{eligible} order(s) {action}; {skipped} skipped."))
