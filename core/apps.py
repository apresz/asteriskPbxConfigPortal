from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self):
        from .file_permissions import harden_local_storage_permissions

        harden_local_storage_permissions()
        from . import signals  # noqa: F401
