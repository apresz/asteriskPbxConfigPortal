from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("locations/", views.location_list, name="locations"),
    path("locations/new/", views.location_create, name="location-create"),
    path("locations/<slug:slug>/", views.location_detail, name="location-detail"),
    path("locations/<slug:slug>/edit/", views.location_update, name="location-edit"),
    path("locations/<slug:slug>/delete/", views.location_delete, name="location-delete"),
    path("extensions/", views.portal_area, {"slug": "extensions"}, name="extensions"),
    path("trunks/", views.portal_area, {"slug": "trunks"}, name="trunks"),
    path("dial-plan/", views.portal_area, {"slug": "dial-plan"}, name="dial-plan"),
    path("settings/", views.portal_area, {"slug": "settings"}, name="settings"),
    path("health/", views.health, name="health"),
]
