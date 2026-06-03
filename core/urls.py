from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("extensions/", views.extension_list, name="extensions"),
    path("extensions/new/", views.extension_create, name="extension_create"),
    path("extensions/<int:pk>/edit/", views.extension_edit, name="extension_edit"),
    path("extensions/<int:pk>/delete/", views.extension_delete, name="extension_delete"),
    path("extensions/template.csv", views.extension_csv_template_view, name="extension_csv_template"),
    path("extensions/export.csv", views.extension_export, name="extension_export"),
    path("extensions/import/", views.extension_import, name="extension_import"),
    path("trunks/", views.portal_area, {"slug": "trunks"}, name="trunks"),
    path("dial-plan/", views.portal_area, {"slug": "dial-plan"}, name="dial-plan"),
    path("settings/", views.portal_area, {"slug": "settings"}, name="settings"),
    path("health/", views.health, name="health"),
]
