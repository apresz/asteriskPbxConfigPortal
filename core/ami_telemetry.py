import asyncio
import csv
from datetime import datetime, timezone as datetime_timezone
from pathlib import Path
import secrets
from typing import Any


DEFAULT_CDR_CSV_PATH = "/var/log/asterisk/cdr-csv/Master.csv"
DEFAULT_CEL_CSV_PATH = "/var/log/asterisk/cel-custom/Master.csv"
DEFAULT_RECORDING_ROOT = "/var/spool/asterisk/monitor"


class AMIError(ConnectionError):
    pass


class AsteriskAMIClient:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        secret: str,
        timeout_seconds: float = 5.0,
    ):
        self.host = host
        self.port = port
        self.username = username
        self.secret = secret
        self.timeout_seconds = timeout_seconds
        self.reader = None
        self.writer = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        await self.close()

    async def connect(self) -> None:
        if not self.username or not self.secret:
            raise AMIError("Asterisk AMI username and secret are required.")
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port),
            timeout=self.timeout_seconds,
        )
        await self._read_message()
        responses = await self.action(
            "Login",
            {
                "Username": self.username,
                "Secret": self.secret,
                "Events": "off",
            },
        )
        response = _first_message(responses)
        if _value(response, "Response").lower() != "success":
            message = _value(response, "Message") or "AMI login failed."
            raise AMIError(message)

    async def close(self) -> None:
        if self.writer is None:
            return
        try:
            await self.action("Logoff")
        except Exception:
            pass
        self.writer.close()
        await self.writer.wait_closed()
        self.reader = None
        self.writer = None

    async def action(
        self,
        action: str,
        parameters: dict[str, Any] | None = None,
        *,
        complete_event: str | None = None,
    ) -> list[dict[str, Any]]:
        if self.reader is None or self.writer is None:
            raise AMIError("AMI connection is not open.")

        action_id = secrets.token_hex(8)
        lines = [f"Action: {action}", f"ActionID: {action_id}"]
        for name, value in (parameters or {}).items():
            lines.append(f"{name}: {value}")
        self.writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8"))
        await self.writer.drain()

        messages = []
        while True:
            message = await asyncio.wait_for(self._read_message(), timeout=self.timeout_seconds)
            if _value(message, "ActionID") not in {"", action_id}:
                continue
            messages.append(message)
            if _value(message, "Response").lower() == "error":
                raise AMIError(_value(message, "Message") or f"AMI action {action} failed.")
            if complete_event and _value(message, "Event") == complete_event:
                break
            if complete_event is None and _value(message, "Response"):
                break
        return messages

    async def _read_message(self) -> dict[str, Any]:
        if self.reader is None:
            raise AMIError("AMI connection is not open.")
        raw = await self.reader.readuntil(b"\r\n\r\n")
        messages = parse_ami_messages(raw.decode("utf-8", errors="replace"))
        return _first_message(messages)


async def collect_agent_telemetry(config: Any) -> dict[str, Any]:
    timestamp = _utc_now()
    errors = []
    phone_registrations = []
    trunk_status = []
    active_calls = []
    queue_status = []
    ami_health: dict[str, Any] = {"ami_connected": False}

    try:
        async with AsteriskAMIClient(
            host=getattr(config, "ami_host", "127.0.0.1"),
            port=int(getattr(config, "ami_port", 5038)),
            username=getattr(config, "ami_username", ""),
            secret=getattr(config, "ami_secret", ""),
            timeout_seconds=float(getattr(config, "ami_timeout_seconds", 5.0)),
        ) as ami:
            contact_messages = await ami.action("PJSIPShowContacts", complete_event="ContactListComplete")
            endpoint_messages = await ami.action("PJSIPShowEndpoints", complete_event="EndpointListComplete")
            channel_messages = await ami.action("CoreShowChannels", complete_event="CoreShowChannelsComplete")
            queue_messages = await ami.action("QueueStatus", complete_event="QueueStatusComplete")
            health_messages = await ami.action("CoreStatus")

        phone_registrations = parse_phone_registrations(contact_messages)
        trunk_status = parse_trunk_status(endpoint_messages)
        active_calls = parse_active_calls(channel_messages)
        queue_status = parse_queue_status(queue_messages)
        ami_health = parse_location_health(health_messages)
        ami_health["ami_connected"] = True
    except Exception as exc:
        errors.append({"category": "ami", "message": str(exc)})

    recent_calls = _read_csv_path(
        getattr(config, "cdr_csv_path", DEFAULT_CDR_CSV_PATH),
        parser=parse_cdr_csv,
        category="recent_calls",
        errors=errors,
    )
    call_events = _read_csv_path(
        getattr(config, "cel_csv_path", DEFAULT_CEL_CSV_PATH),
        parser=parse_cel_csv,
        category="call_events",
        errors=errors,
    )
    recording_metadata = scan_recording_metadata(getattr(config, "recording_root", DEFAULT_RECORDING_ROOT), errors=errors)

    return {
        "type": "telemetry",
        "timestamp": timestamp,
        "location_health": {
            "location_slug": getattr(config, "location_slug", ""),
            "lan_ip": getattr(config, "lan_ip", ""),
            "warp_ip": getattr(config, "warp_ip", ""),
            "collected_at": timestamp,
            **ami_health,
        },
        "phone_registrations": phone_registrations,
        "trunk_status": trunk_status,
        "active_calls": active_calls,
        "queue_status": queue_status,
        "recent_calls": recent_calls,
        "call_events": call_events,
        "recording_metadata": recording_metadata,
        "telemetry_errors": errors,
    }


def parse_ami_messages(raw: str) -> list[dict[str, Any]]:
    messages = []
    current: dict[str, Any] = {}
    for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not line:
            if current:
                messages.append(current)
                current = {}
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key in current:
            existing = current[key]
            if isinstance(existing, list):
                existing.append(value)
            else:
                current[key] = [existing, value]
        else:
            current[key] = value
    if current:
        messages.append(current)
    return messages


def parse_phone_registrations(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    registrations = []
    for message in messages:
        if _value(message, "Event") != "ContactList":
            continue
        status = _value(message, "Status")
        endpoint = _value(message, "EndpointName") or _value(message, "AOR")
        contact = _value(message, "URI") or _value(message, "ObjectName")
        registrations.append(
            {
                "extension": endpoint or _contact_extension(contact),
                "aor": _value(message, "AOR") or endpoint,
                "contact": contact,
                "status": status.lower() or "unknown",
                "reachable": status.lower() == "reachable",
                "via_address": _value(message, "ViaAddress"),
                "user_agent": _value(message, "UserAgent"),
                "roundtrip_usec": _int_or_none(_value(message, "RoundtripUsec")),
                "expires_seconds": _int_or_none(_value(message, "RegExpire")),
            }
        )
    return sorted(registrations, key=lambda item: (item["extension"], item["contact"]))


def parse_trunk_status(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    trunks = []
    for message in messages:
        if _value(message, "Event") != "EndpointList":
            continue
        name = _value(message, "ObjectName") or _value(message, "EndpointName")
        if not name.startswith("trunk-"):
            continue
        device_state = _value(message, "DeviceState") or _value(message, "Status")
        active_contacts = _int_or_none(_value(message, "ActiveContacts"))
        trunks.append(
            {
                "name": name,
                "technology": "PJSIP",
                "status": device_state.lower() or "unknown",
                "available": device_state.lower() not in {"unavailable", "invalid"} and (active_contacts or 0) >= 0,
                "active_contacts": active_contacts,
                "configured_contacts": _int_or_none(_value(message, "Contacts")),
                "aors": _split_csv(_value(message, "Aors") or _value(message, "AORs")),
                "transport": _value(message, "Transport"),
                "outbound_auths": _split_csv(_value(message, "OutboundAuths")),
            }
        )
    return sorted(trunks, key=lambda item: item["name"])


def parse_active_calls(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    calls = []
    for message in messages:
        if _value(message, "Event") != "CoreShowChannel":
            continue
        calls.append(
            {
                "channel": _value(message, "Channel"),
                "state": _value(message, "ChannelStateDesc") or _value(message, "ChannelState"),
                "caller_id": _value(message, "CallerIDNum"),
                "connected_line": _value(message, "ConnectedLineNum"),
                "context": _value(message, "Context"),
                "extension": _value(message, "Exten"),
                "application": _value(message, "Application"),
                "application_data": _value(message, "ApplicationData"),
                "duration": _value(message, "Duration"),
                "bridge_id": _value(message, "BridgeId"),
                "uniqueid": _value(message, "Uniqueid"),
                "linkedid": _value(message, "Linkedid"),
            }
        )
    return sorted(calls, key=lambda item: (item["uniqueid"], item["channel"]))


def parse_queue_status(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    queues: dict[str, dict[str, Any]] = {}
    for message in messages:
        event = _value(message, "Event")
        queue_name = _value(message, "Queue")
        if not queue_name:
            continue
        queue = queues.setdefault(queue_name, {"name": queue_name, "members": [], "callers": []})
        if event == "QueueParams":
            queue.update(
                {
                    "strategy": _value(message, "Strategy"),
                    "calls_waiting": _int_or_none(_value(message, "Calls")) or 0,
                    "completed_calls": _int_or_none(_value(message, "Completed")) or 0,
                    "abandoned_calls": _int_or_none(_value(message, "Abandoned")) or 0,
                    "hold_time_seconds": _int_or_none(_value(message, "Holdtime")),
                    "talk_time_seconds": _int_or_none(_value(message, "TalkTime")),
                    "service_level_percent": _float_or_none(_value(message, "ServiceLevelPerf")),
                }
            )
        elif event == "QueueMember":
            queue["members"].append(
                {
                    "name": _value(message, "Name"),
                    "location": _value(message, "Location"),
                    "membership": _value(message, "Membership"),
                    "penalty": _int_or_none(_value(message, "Penalty")),
                    "status_code": _int_or_none(_value(message, "Status")),
                    "paused": _truthy(_value(message, "Paused")),
                    "calls_taken": _int_or_none(_value(message, "CallsTaken")) or 0,
                    "last_call_epoch": _int_or_none(_value(message, "LastCall")),
                }
            )
        elif event == "QueueEntry":
            queue["callers"].append(
                {
                    "position": _int_or_none(_value(message, "Position")),
                    "channel": _value(message, "Channel"),
                    "caller_id": _value(message, "CallerIDNum"),
                    "wait_seconds": _int_or_none(_value(message, "Wait")),
                    "uniqueid": _value(message, "Uniqueid"),
                }
            )
    return [queues[name] for name in sorted(queues)]


def parse_location_health(messages: list[dict[str, Any]]) -> dict[str, Any]:
    health: dict[str, Any] = {}
    for message in messages:
        for key in ("CoreStartupTime", "CoreReloadTime", "CoreCurrentCalls", "CoreMaxCalls"):
            value = _value(message, key)
            if value:
                health[_snake_case(key)] = _int_or_none(value) if value.isdigit() else value
        response = _value(message, "Response")
        if response:
            health["ami_response"] = response.lower()
    return health


def parse_cdr_csv(raw: str, *, limit: int = 25) -> list[dict[str, Any]]:
    fields = [
        "accountcode",
        "source",
        "destination",
        "destination_context",
        "caller_id",
        "channel",
        "destination_channel",
        "last_application",
        "last_data",
        "start",
        "answer",
        "end",
        "duration_seconds",
        "billable_seconds",
        "disposition",
        "amaflags",
        "uniqueid",
        "userfield",
    ]
    calls = []
    for row in csv.reader(raw.splitlines()):
        if not row:
            continue
        padded = [*row, *[""] * max(0, len(fields) - len(row))]
        item = {field: padded[index] for index, field in enumerate(fields)}
        item["duration_seconds"] = _int_or_none(item["duration_seconds"]) or 0
        item["billable_seconds"] = _int_or_none(item["billable_seconds"]) or 0
        calls.append(item)
    return calls[-limit:][::-1]


def parse_cel_csv(raw: str, *, limit: int = 50) -> list[dict[str, Any]]:
    fields = [
        "event_time",
        "event_type",
        "user_defined_type",
        "caller_id_name",
        "caller_id_num",
        "caller_id_ani",
        "caller_id_rdnis",
        "caller_id_dnid",
        "extension",
        "context",
        "channel",
        "application",
        "application_data",
        "amaflag",
        "accountcode",
        "peer_account",
        "uniqueid",
        "linkedid",
        "userfield",
        "peer",
    ]
    events = []
    for row in csv.reader(raw.splitlines()):
        if not row:
            continue
        padded = [*row, *[""] * max(0, len(fields) - len(row))]
        events.append({field: padded[index] for index, field in enumerate(fields)})
    return events[-limit:][::-1]


def scan_recording_metadata(
    root: str | Path,
    *,
    limit: int = 100,
    errors: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    root_path = Path(root)
    if not root_path.exists():
        return []
    try:
        paths = [
            path
            for path in root_path.rglob("*")
            if path.is_file() and path.suffix.lower() in {".wav", ".gsm", ".mp3", ".ogg"}
        ]
    except OSError as exc:
        if errors is not None:
            errors.append({"category": "recording_metadata", "message": str(exc)})
        return []

    files = []
    for path in paths:
        try:
            stat = path.stat()
        except OSError as exc:
            if errors is not None:
                errors.append({"category": "recording_metadata", "message": str(exc)})
            continue
        files.append((path, stat))

    files.sort(key=lambda item: item[1].st_mtime, reverse=True)
    recordings = []
    for path, stat in files[:limit]:
        recordings.append(
            {
                "path": str(path),
                "filename": path.name,
                "size_bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=datetime_timezone.utc).isoformat(),
                "uniqueid": path.stem.split("-", 1)[0],
            }
        )
    return recordings


def telemetry_failure_payload(config: Any, exc: Exception) -> dict[str, Any]:
    timestamp = _utc_now()
    return {
        "type": "telemetry",
        "timestamp": timestamp,
        "location_health": {
            "location_slug": getattr(config, "location_slug", ""),
            "lan_ip": getattr(config, "lan_ip", ""),
            "warp_ip": getattr(config, "warp_ip", ""),
            "collected_at": timestamp,
            "ami_connected": False,
        },
        "phone_registrations": [],
        "trunk_status": [],
        "active_calls": [],
        "queue_status": [],
        "recent_calls": [],
        "call_events": [],
        "recording_metadata": [],
        "telemetry_errors": [{"category": "telemetry", "message": str(exc)}],
    }


def _read_csv_path(path: str | Path, *, parser, category: str, errors: list[dict[str, str]]) -> list[dict[str, Any]]:
    csv_path = Path(path)
    if not csv_path.exists():
        return []
    try:
        return parser(csv_path.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:
        errors.append({"category": category, "message": str(exc)})
        return []


def _first_message(messages: list[dict[str, Any]]) -> dict[str, Any]:
    return messages[0] if messages else {}


def _value(message: dict[str, Any], *names: str) -> str:
    lower_map = {key.lower(): value for key, value in message.items()}
    for name in names:
        value = lower_map.get(name.lower())
        if value is None:
            continue
        if isinstance(value, list):
            return str(value[-1])
        return str(value)
    return ""


def _contact_extension(contact: str) -> str:
    if not contact:
        return ""
    contact = contact.split("/", 1)[0]
    contact = contact.split("@", 1)[0]
    return contact


def _int_or_none(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _truthy(value: str) -> bool:
    return str(value).strip().lower() in {"1", "yes", "true", "on"}


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _snake_case(value: str) -> str:
    result = []
    for index, char in enumerate(value):
        if char.isupper() and index:
            result.append("_")
        result.append(char.lower())
    return "".join(result)


def _utc_now() -> str:
    return datetime.now(datetime_timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
