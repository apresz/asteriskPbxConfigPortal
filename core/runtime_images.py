from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable, Mapping


ASTERISK_22_LTS_IMAGE = "ghcr.io/apresz/asterisk:22-lts"
TFTP_SERVICE_IMAGE = "ghcr.io/apresz/tftp:1.0.0"
HTTP_STATIC_SERVICE_IMAGE = "docker.io/nginx:1.27-alpine"
PBX_AGENT_IMAGE = "ghcr.io/apresz/pbx-agent:0.1.0"

RUNTIME_IMAGE_TAG_POLICY_WARN = "warn"
RUNTIME_IMAGE_TAG_POLICY_BLOCK = "block"
RUNTIME_IMAGE_TAG_POLICIES = {
    RUNTIME_IMAGE_TAG_POLICY_WARN,
    RUNTIME_IMAGE_TAG_POLICY_BLOCK,
}

_DIGEST_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_+.-]*:[0-9a-fA-F]{32,}$")
_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-fA-F]{64}$")
_WHITESPACE_RE = re.compile(r"\s")


@dataclass(frozen=True)
class ImageReference:
    reference: str
    repository: str
    registry: str | None
    tag: str | None
    digest: str | None

    @property
    def digest_pinned(self) -> bool:
        return self.digest is not None

    @property
    def tag_only(self) -> bool:
        return self.tag is not None and self.digest is None


@dataclass(frozen=True)
class RuntimeImage:
    service: str
    env_var: str
    reference: str
    custom: bool
    resolved_digest: str | None = None
    digest_source: str | None = None

    @property
    def parsed_reference(self) -> ImageReference:
        return parse_image_reference(self.reference)

    @property
    def digest(self) -> str | None:
        parsed = self.parsed_reference
        return normalize_digest(parsed.digest or self.resolved_digest)

    @property
    def immutable(self) -> bool:
        return self.digest is not None

    @property
    def compose_reference(self) -> str:
        parsed = self.parsed_reference
        digest = self.digest
        if not digest:
            return self.reference
        if parsed.digest == digest:
            return self.reference
        return f"{strip_digest(self.reference)}@{digest}"

    @property
    def compose_default(self) -> str | None:
        if self.custom and not self.immutable:
            return None
        return self.compose_reference


DEFAULT_RUNTIME_IMAGES = (
    RuntimeImage(
        service="asterisk",
        env_var="PBX_ASTERISK_IMAGE",
        reference=ASTERISK_22_LTS_IMAGE,
        custom=True,
    ),
    RuntimeImage(
        service="tftp",
        env_var="PBX_TFTP_IMAGE",
        reference=TFTP_SERVICE_IMAGE,
        custom=True,
    ),
    RuntimeImage(
        service="provisioning-http",
        env_var="PBX_HTTP_IMAGE",
        reference=HTTP_STATIC_SERVICE_IMAGE,
        custom=False,
    ),
    RuntimeImage(
        service="pbx-agent",
        env_var="PBX_AGENT_IMAGE",
        reference=PBX_AGENT_IMAGE,
        custom=True,
    ),
)


def parse_image_reference(reference: str) -> ImageReference:
    reference = str(reference or "").strip()
    if not reference:
        raise ValueError("Image reference is required.")
    if _WHITESPACE_RE.search(reference):
        raise ValueError(f"Image reference contains whitespace: {reference!r}")

    name_part, digest = _split_digest(reference)
    repository, tag = _split_tag(name_part)
    if not repository:
        raise ValueError(f"Image reference is missing a repository: {reference!r}")
    if digest:
        normalize_digest(digest)

    return ImageReference(
        reference=reference,
        repository=repository,
        registry=_registry_for(repository),
        tag=tag,
        digest=normalize_digest(digest),
    )


def normalize_digest(digest: str | None) -> str | None:
    if digest is None:
        return None
    digest = str(digest).strip()
    if not digest:
        return None
    if not _DIGEST_RE.match(digest):
        raise ValueError(f"Image digest must use '<algorithm>:<hex>' syntax: {digest!r}")
    if digest.lower().startswith("sha256:") and not _SHA256_DIGEST_RE.match(digest):
        raise ValueError(f"Image sha256 digest must contain exactly 64 hex characters: {digest!r}")
    return digest.lower()


def strip_digest(reference: str) -> str:
    return _split_digest(str(reference))[0]


def configured_runtime_images(overrides: Mapping[str, Any] | None = None) -> tuple[RuntimeImage, ...]:
    if not overrides:
        return DEFAULT_RUNTIME_IMAGES

    configured: list[RuntimeImage] = []
    for default_image in DEFAULT_RUNTIME_IMAGES:
        override = overrides.get(default_image.service) or overrides.get(default_image.env_var) or {}
        if isinstance(override, str):
            override = {"reference": override}
        if not isinstance(override, Mapping):
            raise ValueError(f"Runtime image override for {default_image.service!r} must be a mapping or string.")

        reference = str(override.get("reference") or default_image.reference).strip()
        resolved_digest = normalize_digest(override.get("resolved_digest") or override.get("digest"))
        digest_source = str(override.get("digest_source") or "").strip() or None
        configured.append(
            RuntimeImage(
                service=default_image.service,
                env_var=default_image.env_var,
                reference=reference,
                custom=default_image.custom,
                resolved_digest=resolved_digest,
                digest_source=digest_source,
            )
        )
    return tuple(configured)


def runtime_image_metadata(images: Iterable[RuntimeImage]) -> list[dict[str, Any]]:
    metadata = []
    for image in images:
        parsed = image.parsed_reference
        digest = image.digest
        digest_source = image.digest_source
        if parsed.digest:
            digest_source = digest_source or "image-reference"
        elif image.resolved_digest:
            digest_source = digest_source or "configured-resolved-digest"
        metadata.append(
            {
                "service": image.service,
                "env_var": image.env_var,
                "reference": image.reference,
                "compose_reference": image.compose_default,
                "repository": parsed.repository,
                "registry": parsed.registry,
                "tag": parsed.tag,
                "digest": digest,
                "digest_source": digest_source,
                "custom": image.custom,
                "immutable": image.immutable,
                "requires_env_override": image.compose_default is None,
            }
        )
    return metadata


def runtime_image_validation_issues(
    images: Iterable[RuntimeImage],
    *,
    tag_policy: str = RUNTIME_IMAGE_TAG_POLICY_WARN,
) -> dict[str, list[dict[str, Any]]]:
    tag_policy = normalize_tag_policy(tag_policy)
    warnings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    target = errors if tag_policy == RUNTIME_IMAGE_TAG_POLICY_BLOCK else warnings

    for image in images:
        try:
            parsed = image.parsed_reference
            digest = image.digest
        except ValueError as exc:
            errors.append(
                {
                    "code": "runtime_image_invalid_reference",
                    "service": image.service,
                    "env_var": image.env_var,
                    "reference": image.reference,
                    "message": str(exc),
                }
            )
            continue

        if image.custom and not digest:
            target.append(
                {
                    "code": "runtime_image_tag_only",
                    "service": image.service,
                    "env_var": image.env_var,
                    "reference": image.reference,
                    "repository": parsed.repository,
                    "tag": parsed.tag,
                    "policy": tag_policy,
                    "message": "Custom PBX runtime images must include an immutable digest or resolved digest record.",
                }
            )

    return {"warnings": warnings, "errors": errors}


def normalize_tag_policy(policy: str | None) -> str:
    normalized = str(policy or RUNTIME_IMAGE_TAG_POLICY_WARN).strip().lower()
    if normalized not in RUNTIME_IMAGE_TAG_POLICIES:
        choices = ", ".join(sorted(RUNTIME_IMAGE_TAG_POLICIES))
        raise ValueError(f"Runtime image tag policy must be one of: {choices}.")
    return normalized


def _split_digest(reference: str) -> tuple[str, str | None]:
    if "@" not in reference:
        return reference, None
    name_part, digest = reference.rsplit("@", 1)
    if not name_part or not digest:
        raise ValueError(f"Image reference has an invalid digest separator: {reference!r}")
    return name_part, digest


def _split_tag(name_part: str) -> tuple[str, str | None]:
    last_slash = name_part.rfind("/")
    last_colon = name_part.rfind(":")
    if last_colon > last_slash:
        repository = name_part[:last_colon]
        tag = name_part[last_colon + 1 :]
        if not tag:
            raise ValueError(f"Image reference has an empty tag: {name_part!r}")
        return repository, tag
    return name_part, None


def _registry_for(repository: str) -> str | None:
    first = repository.split("/", 1)[0]
    if "." in first or ":" in first or first == "localhost":
        return first
    return None
