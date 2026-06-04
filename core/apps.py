from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self):
        from django.conf import settings

        from .file_permissions import harden_runtime_storage_permissions

        from . import signals  # noqa: F401

        harden_runtime_storage_permissions(settings)
