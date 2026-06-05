from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

RING_GROUP_STRATEGY_RING_ALL = "ring_all"
RING_GROUP_STRATEGY_HUNT = "hunt"


class RingGroupDialplanValidationError(ValueError):
    """Raised when a ring group payload cannot be rendered safely."""


def render_ring_group_dialplan_lines(
    ring_group: Mapping[str, Any],
    *,
    slugify: Callable[[str], str],
) -> list[str]:
    name, strategy, timeout_seconds, members = validate_ring_group_dialplan_payload(ring_group)

    lines = [f"exten => {slugify(name)},1,NoOp(Ring group {name})"]
    if strategy == RING_GROUP_STRATEGY_RING_ALL:
        targets = "&".join(_member_target(member) for member in members)
        lines.append(f" same => n,Dial({targets},{timeout_seconds})")
        lines.append(" same => n,Hangup()")
    elif strategy == RING_GROUP_STRATEGY_HUNT:
        for index, member in enumerate(members):
            lines.append(f" same => n,Dial({_member_target(member)},{timeout_seconds})")
            if index < len(members) - 1:
                lines.append(' same => n,GotoIf($["${DIALSTATUS}" = "ANSWER"]?done)')
        lines.append(" same => n(done),Hangup()")
    lines.append("")
    return lines


def validate_ring_group_dialplan_payload(
    ring_group: Mapping[str, Any],
) -> tuple[str, str, int, list[dict[str, Any]]]:
    name = _non_empty_string(ring_group.get("name"), "name")
    strategy = ring_group.get("strategy") or RING_GROUP_STRATEGY_RING_ALL
    timeout_seconds = _positive_int(ring_group.get("timeout_seconds"), "timeout_seconds", name)
    members = _validated_members(ring_group.get("members"), name)

    if strategy not in {RING_GROUP_STRATEGY_RING_ALL, RING_GROUP_STRATEGY_HUNT}:
        raise RingGroupDialplanValidationError(
            f"Ring group {name!r} has unsupported strategy {strategy!r}."
        )
    return name, strategy, timeout_seconds, members


def _validated_members(value: Any, ring_group_name: str) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise RingGroupDialplanValidationError(
            f"Ring group {ring_group_name!r} members must be a sequence."
        )
    if not value:
        raise RingGroupDialplanValidationError(
            f"Ring group {ring_group_name!r} must have at least one member."
        )

    members: list[dict[str, Any]] = []
    previous_sort_key: tuple[int, str] | None = None
    for index, member in enumerate(value, start=1):
        if not isinstance(member, Mapping):
            raise RingGroupDialplanValidationError(
                f"Ring group {ring_group_name!r} member {index} must be a mapping."
            )
        extension = _non_empty_string(member.get("extension"), f"member {index} extension")
        priority = _positive_int(member.get("priority"), f"member {index} priority", ring_group_name)
        sort_key = (priority, extension)
        if previous_sort_key is not None and sort_key < previous_sort_key:
            raise RingGroupDialplanValidationError(
                f"Ring group {ring_group_name!r} members must be ordered by priority then extension."
            )
        previous_sort_key = sort_key
        members.append({"extension": extension, "priority": priority})
    return members


def _member_target(member: Mapping[str, Any]) -> str:
    return f"PJSIP/{member['extension']}"


def _non_empty_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RingGroupDialplanValidationError(f"Ring group {field_name} is required.")
    return value.strip()


def _positive_int(value: Any, field_name: str, ring_group_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RingGroupDialplanValidationError(
            f"Ring group {ring_group_name!r} {field_name} must be a positive integer."
        )
    return value
