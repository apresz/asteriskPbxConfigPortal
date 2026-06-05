from __future__ import annotations

from dataclasses import dataclass
import re
import secrets
from typing import Any, Callable


AMI_USERNAME_MAX_LENGTH = 120
AMI_USERNAME_TOKEN_HEX_BYTES = 4
AMI_SECRET_BYTES = 32


@dataclass(frozen=True)
class AMICredentialUpdate:
    event: str
    username_before: str
    username_after: str
    secret_before_set: bool
    secret_after_set: bool

    @property
    def username_changed(self) -> bool:
        return self.username_before != self.username_after

    @property
    def sensitive_value_changed(self) -> bool:
        return self.secret_before_set != self.secret_after_set or self.event == "rotated"

    @property
    def changed_fields(self) -> list[str]:
        fields = []
        if self.username_changed:
            fields.append("ami_username")
        if self.sensitive_value_changed:
            fields.append("ami_secret")
        return fields

    @property
    def changed(self) -> bool:
        return bool(self.changed_fields)


def generate_ami_username(
    seed: str,
    *,
    token_factory: Callable[[], str] | None = None,
    max_length: int = AMI_USERNAME_MAX_LENGTH,
) -> str:
    location_slug = _slug(seed)
    token = _username_token(token_factory)
    max_length = max(len("ami-x"), int(max_length))
    token = token[: max(1, max_length - len("ami-") - 2)]
    suffix_length = len(token) + 1
    prefix_max_length = max(len("ami"), max_length - suffix_length)
    prefix = f"ami-{location_slug}"[:prefix_max_length].rstrip("-_") or "ami"
    return f"{prefix}-{token}"


def generate_ami_secret(*, token_factory: Callable[[], str] | None = None) -> str:
    if token_factory is not None:
        secret = str(token_factory())
        if not secret:
            raise ValueError("AMI secret token factory returned an empty value.")
        return secret
    return secrets.token_urlsafe(AMI_SECRET_BYTES)


def ensure_location_ami_credentials(
    location: Any,
    *,
    rotate: bool = False,
    username_factory: Callable[[str], str] | None = None,
    secret_factory: Callable[[], str] | None = None,
) -> AMICredentialUpdate:
    before_username = str(getattr(location, "ami_username", "") or "")
    before_secret = str(getattr(location, "ami_secret", "") or "")
    seed = str(getattr(location, "slug", "") or getattr(location, "name", "") or "location")

    after_username = before_username
    after_secret = before_secret

    if rotate or not before_username:
        after_username = str(username_factory(seed) if username_factory else generate_ami_username(seed))
        if not after_username:
            raise ValueError("AMI username factory returned an empty value.")
        setattr(location, "ami_username", after_username)

    if rotate or not before_secret:
        after_secret = str(secret_factory() if secret_factory else generate_ami_secret())
        if not after_secret:
            raise ValueError("AMI secret factory returned an empty value.")
        setattr(location, "ami_secret", after_secret)

    event = "preserved"
    if rotate:
        event = "rotated"
    elif before_username != after_username or before_secret != after_secret:
        event = "generated"

    return AMICredentialUpdate(
        event=event,
        username_before=before_username,
        username_after=after_username,
        secret_before_set=bool(before_secret),
        secret_after_set=bool(after_secret),
    )


def ami_credentials_audit_payload(update: AMICredentialUpdate) -> dict[str, Any]:
    return {
        "event": update.event,
        "fields_changed": update.changed_fields,
        "username_changed": update.username_changed,
        "sensitive_value_changed": update.sensitive_value_changed,
        "had_username_before": bool(update.username_before),
        "has_username_after": bool(update.username_after),
        "had_sensitive_value_before": update.secret_before_set,
        "has_sensitive_value_after": update.secret_after_set,
    }


def render_manager_conf(location: Any, *, header_lines: list[str] | None = None) -> str:
    username = getattr(location, "ami_username", "") or f"ami-{_slug(getattr(location, 'slug', 'location'))}"
    lines = list(header_lines or [])
    lines.extend(
        [
            "[general]",
            "enabled=yes",
            f"port={getattr(location, 'ami_port', 5038)}",
            f"bindaddr={getattr(location, 'ami_host', '127.0.0.1')}",
            "webenabled=no",
            "",
            f"[{username}]",
            f"secret={getattr(location, 'ami_secret', '')}",
            "read=system,call,log,verbose,command,agent,user,config,dtmf,reporting,cdr,dialplan",
            "write=system,call,agent,user,config,command,reporting,originate",
            "permit=127.0.0.1/255.255.255.255",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def _username_token(token_factory: Callable[[], str] | None) -> str:
    raw_token = token_factory() if token_factory else secrets.token_hex(AMI_USERNAME_TOKEN_HEX_BYTES)
    token = re.sub(r"[^a-zA-Z0-9]+", "", str(raw_token).lower())
    if not token:
        raise ValueError("AMI username token factory returned no alphanumeric characters.")
    return token


def _slug(value: Any) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(value or "").strip().lower()).strip("-_")
    return normalized or "location"
