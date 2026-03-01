from django.core.management.base import BaseCommand

from accounts.models import User
from analytics.services import calculate_seller_risk_batch


class Command(BaseCommand):
    help = 'Run seller fraud verification and persist fresh risk snapshots.'

    def handle(self, *args, **options):
        sellers = User.objects.filter(role=User.UserRole.SELLER)
        snapshots = calculate_seller_risk_batch(sellers=sellers)
        self.stdout.write(
            self.style.SUCCESS(f'Generated {len(snapshots)} seller verification snapshot(s).')
        )
