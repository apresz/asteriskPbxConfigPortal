from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .models import FeatureCode, Location


@dataclass(frozen=True)
class DefaultFeatureCode:
    code: str
    name: str
    feature_type: str
    notes: str


DEFAULT_FEATURE_CODES = (
    DefaultFeatureCode(
        code="*98",
        name="Voicemail main",
        feature_type=FeatureCode.FeatureType.VOICEMAIL_MAIN,
        notes="Access the shared voicemail menu.",
    ),
    DefaultFeatureCode(
        code="*8",
        name="Call pickup",
        feature_type=FeatureCode.FeatureType.CALL_PICKUP,
        notes="Pick up a ringing call in the pickup group.",
    ),
    DefaultFeatureCode(
        code="**",
        name="Directed pickup",
        feature_type=FeatureCode.FeatureType.DIRECTED_PICKUP,
        notes="Prefix for directed pickup by extension.",
    ),
    DefaultFeatureCode(
        code="700",
        name="Call park",
        feature_type=FeatureCode.FeatureType.PARK,
        notes="Park the current call.",
    ),
    DefaultFeatureCode(
        code="*80",
        name="Paging prefix",
        feature_type=FeatureCode.FeatureType.PAGING_PREFIX,
        notes="Prefix for paging groups.",
    ),
)


def default_feature_code_specs() -> tuple[DefaultFeatureCode, ...]:
    return DEFAULT_FEATURE_CODES


def ensure_default_feature_codes(location: Location) -> int:
    created_count = 0
    for spec in DEFAULT_FEATURE_CODES:
        _feature_code, created = FeatureCode.objects.get_or_create(
            location=location,
            code=spec.code,
            defaults={
                "name": spec.name,
                "feature_type": spec.feature_type,
                "notes": spec.notes,
                "is_active": True,
            },
        )
        if created:
            created_count += 1
    return created_count


def ensure_default_feature_codes_for_locations(locations: Iterable[Location]) -> int:
    return sum(ensure_default_feature_codes(location) for location in locations)


def locations_missing_default_feature_codes() -> list[Location]:
    default_codes = {spec.code for spec in DEFAULT_FEATURE_CODES}
    missing_locations = []
    for location in Location.objects.prefetch_related("feature_codes").order_by("name"):
        existing_codes = {feature_code.code for feature_code in location.feature_codes.all()}
        if not default_codes.issubset(existing_codes):
            missing_locations.append(location)
    return missing_locations
