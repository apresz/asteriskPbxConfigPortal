from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import PortalUserProfile


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_portal_user_profile(sender, instance, created, **kwargs):
    if created:
        PortalUserProfile.objects.get_or_create(user=instance)
