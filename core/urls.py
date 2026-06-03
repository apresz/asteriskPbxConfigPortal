from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("extensions/", views.portal_area, {"slug": "extensions"}, name="extensions"),
    path("trunks/", views.portal_area, {"slug": "trunks"}, name="trunks"),
    path("dial-plan/", views.portal_area, {"slug": "dial-plan"}, name="dial-plan"),
    path("settings/", views.portal_area, {"slug": "settings"}, name="settings"),
    path("health/", views.health, name="health"),
    path("api/admin/roles/", views.admin_roles, name="admin-roles"),
    path("api/admin/users/", views.admin_users, name="admin-users"),
    path("api/admin/users/<int:user_id>/", views.admin_user_detail, name="admin-user-detail"),
    path("api/admin/service-identities/", views.admin_service_identities, name="service-identity-list"),
    path(
        "api/admin/service-identities/<int:service_identity_id>/",
        views.admin_service_identity_detail,
        name="service-identity-detail",
    ),
    path("api/admin/api-keys/", views.admin_api_keys, name="api-key-create"),
    path("api/admin/api-keys/<int:api_key_id>/rotate/", views.admin_api_key_rotate, name="api-key-rotate"),
    path("api/admin/api-keys/<int:api_key_id>/revoke/", views.admin_api_key_revoke, name="api-key-revoke"),
]
