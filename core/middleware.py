from ipaddress import ip_address, ip_network

from django.conf import settings
from django.http import HttpResponseForbidden

from .models import APIKey


class LANWarpOnlyMiddleware:
    """Optionally restrict portal access to configured LAN/WARP CIDRs."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not settings.PORTAL_ENFORCE_CLIENT_CIDR:
            return self.get_response(request)

        client_ip = self._client_ip(request)
        if not self._is_allowed(client_ip):
            return HttpResponseForbidden("Portal access is restricted to the configured private network.")

        return self.get_response(request)

    def _client_ip(self, request) -> str:
        remote_addr = request.META.get("REMOTE_ADDR", "")
        forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")

        if forwarded_for and self._is_trusted_proxy(remote_addr):
            return forwarded_for.split(",", 1)[0].strip()

        return remote_addr

    def _is_trusted_proxy(self, raw_ip: str) -> bool:
        return self._ip_in_networks(raw_ip, settings.PORTAL_TRUSTED_PROXY_CIDRS)

    def _is_allowed(self, raw_ip: str) -> bool:
        return self._ip_in_networks(raw_ip, settings.PORTAL_ALLOWED_CLIENT_CIDRS)

    def _ip_in_networks(self, raw_ip: str, cidrs: list[str]) -> bool:
        try:
            address = ip_address(raw_ip)
        except ValueError:
            return False

        networks = []
        for cidr in cidrs:
            try:
                networks.append(ip_network(cidr))
            except ValueError:
                continue

        return any(address in network for network in networks)


class APIKeyAuthenticationMiddleware:
    """Authenticate user-scoped API keys for JSON API endpoints."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.api_key = None
        request.api_user = None
        request.api_key_auth_error = ""
        request.api_key_auth_error_status = 401

        authorization = request.headers.get("Authorization", "").strip()
        if authorization and request.path.startswith("/api/"):
            request._dont_enforce_csrf_checks = True
            self._authenticate(request, authorization)

        return self.get_response(request)

    def _authenticate(self, request, authorization: str) -> None:
        scheme, separator, raw_secret = authorization.partition(" ")
        raw_secret = raw_secret.strip()
        if scheme.lower() != "bearer" or not separator or not raw_secret:
            request.api_key_auth_error = "Authorization header must use Bearer token."
            return

        api_key = APIKey.find_by_secret(raw_secret)
        if api_key is None:
            request.api_key_auth_error = "Invalid API key."
            return

        request.api_key = api_key
        if api_key.user_id:
            request.api_user = api_key.user
        else:
            request.api_key_auth_error = "API key must be scoped to an active user."
            request.api_key_auth_error_status = 403
