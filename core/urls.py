from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("extensions/", views.portal_area, {"slug": "extensions"}, name="extensions"),
    path("trunks/", views.portal_area, {"slug": "trunks"}, name="trunks"),
    path("dial-plan/", views.portal_area, {"slug": "dial-plan"}, name="dial-plan"),
    path("settings/", views.portal_area, {"slug": "settings"}, name="settings"),
    path("health/", views.health, name="health"),
]

