from django.db.models.signals import post_save
from django.dispatch import receiver

from accounts.models import CustomerProfile
from accounts.models import SellerProfile
from accounts.models import User


@receiver(post_save, sender=User)
def create_profiles_for_new_user(sender, instance, created, **kwargs):
    if not created:
        return

    if instance.role == User.UserRole.SELLER:
        SellerProfile.objects.get_or_create(
            user=instance,
            defaults={'store_name': f'{instance.display_name} Store'},
        )
    if instance.role == User.UserRole.CUSTOMER:
        CustomerProfile.objects.get_or_create(user=instance)
