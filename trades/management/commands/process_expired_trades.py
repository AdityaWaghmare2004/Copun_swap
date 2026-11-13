from django.core.management.base import BaseCommand

from trades.services import process_expired_trades


class Command(BaseCommand):
    help = "Resolve trades whose 15-minute verification windows have expired."

    def handle(self, *args, **options):
        processed = process_expired_trades()
        self.stdout.write(
            self.style.SUCCESS(f"Resolved {processed} expired trade(s).")
        )
