from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping

DEFAULT_IVR_BUSINESS_HOURS_SCHEDULE = {
    "times": "09:00-17:00",
    "weekdays": "mon-fri",
    "monthdays": "*",
    "months": "*",
}
IVR_BUSINESS_HOURS_SCHEDULE_FIELDS = (
    "times",
    "weekdays",
    "monthdays",
    "months",
    "timezone",
)


def default_ivr_business_hours_schedule(timezone: str = "") -> dict[str, str]:
    """Expose the Asterisk GotoIfTime fields used by generated IVR hours branches."""
    return {
        **DEFAULT_IVR_BUSINESS_HOURS_SCHEDULE,
        "timezone": str(timezone or "").strip(),
    }


def incomplete_ivr_hours_destination_errors(ivrs: Iterable[Any]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    for ivr in ivrs:
        business_destination = _value(ivr, "business_hours_destination")
        after_destination = _value(ivr, "after_hours_destination")
        if bool(business_destination) == bool(after_destination):
            continue
        missing = []
        if not business_destination:
            missing.append("business_hours_destination")
        if not after_destination:
            missing.append("after_hours_destination")
        name = _ivr_name(ivr)
        errors.append(
            {
                "code": "ivr_incomplete_hours_destinations",
                "ivr": name,
                "missing": missing,
                "message": f"IVR {name} must define both business-hours and after-hours destinations.",
            }
        )
    return errors


def render_ivr_dialplan_lines(
    ivr: Mapping[str, Any],
    *,
    slugify: Callable[[str], str],
    destination_app: Callable[[dict[str, Any] | None], str],
) -> list[str]:
    name = str(ivr["name"])
    ivr_name = slugify(name)
    context_name = f"ivr-{ivr_name}"
    lines = [
        f"exten => {ivr_name},1,Goto({context_name},s,1)",
        "",
        f"[{context_name}]",
        f"exten => s,1,NoOp(IVR {name})",
    ]

    if _has_hours_routing(ivr):
        schedule = _schedule(ivr.get("business_hours_schedule"))
        lines.extend(
            [
                (
                    " same => n,NoOp(IVR business_hours_schedule "
                    f"times={schedule['times']} weekdays={schedule['weekdays']} "
                    f"monthdays={schedule['monthdays']} months={schedule['months']} "
                    f"timezone={schedule['timezone']})"
                ),
                f" same => n,GotoIfTime({_schedule_time_spec(schedule)}?business-hours,1)",
                " same => n,Goto(after-hours,1)",
                "",
                f"exten => business-hours,1,NoOp(IVR {name} business hours)",
                f" same => n,{destination_app(ivr['business_hours_destination'])}",
                " same => n,Hangup()",
                "",
                f"exten => after-hours,1,NoOp(IVR {name} after hours)",
                f" same => n,{destination_app(ivr['after_hours_destination'])}",
                " same => n,Hangup()",
                "",
            ]
        )
    else:
        prompt = ivr.get("prompt_name") or "silence/1"
        lines.extend(
            [
                f" same => n,Background({prompt})",
                f" same => n,WaitExten({ivr['timeout_seconds']})",
                " same => n,Goto(t,1)",
                "",
                f"exten => t,1,NoOp(IVR {name} timeout)",
                f" same => n,{destination_app(ivr.get('timeout_destination'))}",
                " same => n,Hangup()",
                "",
                f"exten => i,1,NoOp(IVR {name} invalid input)",
                f" same => n,{destination_app(ivr.get('invalid_destination'))}",
                " same => n,Hangup()",
                "",
            ]
        )

    for option in ivr.get("menu_options", []):
        lines.extend(
            [
                f"exten => {option['digit']},1,NoOp(IVR option {option['digit']} {option['label']})",
                f" same => n,{destination_app(option.get('destination'))}",
                " same => n,Hangup()",
                "",
            ]
        )

    return lines


def _has_hours_routing(ivr: Mapping[str, Any]) -> bool:
    return bool(ivr.get("business_hours_destination") and ivr.get("after_hours_destination"))


def _schedule(raw_schedule: Mapping[str, Any] | None) -> dict[str, str]:
    schedule = default_ivr_business_hours_schedule()
    if raw_schedule:
        schedule.update(
            {
                field: str(raw_schedule.get(field) or schedule.get(field) or "").strip()
                for field in IVR_BUSINESS_HOURS_SCHEDULE_FIELDS
            }
        )
    return schedule


def _schedule_time_spec(schedule: Mapping[str, str]) -> str:
    return ",".join(
        [
            schedule["times"],
            schedule["weekdays"],
            schedule["monthdays"],
            schedule["months"],
        ]
    )


def _ivr_name(ivr: Any) -> str:
    return str(_value(ivr, "name") or "unknown IVR")


def _value(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(key, default)
    return getattr(item, key, default)
