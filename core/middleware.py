from ipaddress import ip_address, ip_network

from django.conf import settings
from django.http import HttpResponseForbidden


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

