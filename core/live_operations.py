from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
import queue
import re
import threading
from typing import Any
from uuid import uuid4

from .audit_helpers import redact_audit_mapping
from .service_principals import is_service_principal, service_identity_audit_details, service_identity_audit_label


DEFAULT_LIVE_COMMAND_TIMEOUT_SECONDS = 15.0
DEFAULT_RECORDING_PLAYBACK_TIMEOUT_SECONDS = 30.0
DEFAULT_CHANNEL_ORIGINATE_ASYNC = True


@dataclass(frozen=True)
class LiveCommandSpec:
    name: str
    label: str
    ami_action: str
    ami_parameters: dict[str, str]
    description: str
    complete_event: str | None = None
    requires_parameters: bool = False


@dataclass(frozen=True)
class LiveAMIAction:
    command_name: str
    ami_action: str
    ami_parameters: dict[str, str]
    complete_event: str | None = None


class LiveOperationError(Exception):
    pass


class UnsupportedLiveCommandError(LiveOperationError, ValueError):
    pass


class AgentUnavailableError(LiveOperationError):
    pass


class AgentCommandTimeoutError(LiveOperationError):
    pass


CHANNEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@;+-]{0,159}$")
CONTEXT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
EXTENSION_PATTERN = re.compile(r"^[A-Za-z0-9*#_+!.:-]{1,80}$")
CALLER_ID_PATTERN = re.compile(r"^[A-Za-z0-9 _+*#().<>@:-]{1,120}$")

CHANNEL_PARAMETER_NAMES = ("channel_id", "channel")
EXTENSION_PARAMETER_NAMES = ("exten", "extension")

SUPPORTED_LIVE_COMMANDS = (
    LiveCommandSpec(
        name="core_reload",
        label="Core reload",
        ami_action="Command",
        ami_parameters={"Command": "core reload"},
        description="Reload Asterisk core configuration.",
    ),
    LiveCommandSpec(
        name="pjsip_reload",
        label="PJSIP reload",
        ami_action="Command",
        ami_parameters={"Command": "pjsip reload"},
        description="Reload PJSIP endpoints and transports.",
    ),
    LiveCommandSpec(
        name="queue_reload",
        label="Queue reload",
        ami_action="QueueReload",
        ami_parameters={},
        description="Reload queue configuration.",
    ),
    LiveCommandSpec(
        name="channel_status",
        label="Channel status",
        ami_action="Status",
        ami_parameters={},
        description="Inspect status for an active channel.",
        complete_event="StatusComplete",
        requires_parameters=True,
    ),
    LiveCommandSpec(
        name="channel_hangup",
        label="Hang up channel",
        ami_action="Hangup",
        ami_parameters={},
        description="Hang up an active channel.",
        requires_parameters=True,
    ),
    LiveCommandSpec(
        name="channel_redirect",
        label="Redirect channel",
        ami_action="Redirect",
        ami_parameters={},
        description="Redirect an active channel into a configured dialplan target.",
        requires_parameters=True,
    ),
    LiveCommandSpec(
        name="channel_originate",
        label="Originate channel",
        ami_action="Originate",
        ami_parameters={},
        description="Originate a call into a configured dialplan target.",
        requires_parameters=True,
    ),
)
SUPPORTED_LIVE_COMMANDS_BY_NAME = {command.name: command for command in SUPPORTED_LIVE_COMMANDS}
LIVE_COMMAND_ALIASES = {
    "status": "channel_status",
    "hangup": "channel_hangup",
    "redirect": "channel_redirect",
    "originate": "channel_originate",
}


def supported_live_commands(*, include_parameterized: bool = False) -> list[dict[str, str]]:
    return [
        {
            "name": command.name,
            "label": command.label,
            "description": command.description,
        }
        for command in SUPPORTED_LIVE_COMMANDS
        if include_parameterized or not command.requires_parameters
    ]


def canonical_live_command_name(command_name: str) -> str:
    normalized = str(command_name or "").strip()
    canonical_name = LIVE_COMMAND_ALIASES.get(normalized, normalized)
    if canonical_name not in SUPPORTED_LIVE_COMMANDS_BY_NAME:
        raise UnsupportedLiveCommandError(f"Unsupported live PBX command: {normalized or 'missing'}.")
    return canonical_name


def validate_live_command_parameters(command_name: str, parameters: Mapping[str, Any] | None = None) -> dict[str, Any]:
    command_name = canonical_live_command_name(command_name)
    command = SUPPORTED_LIVE_COMMANDS_BY_NAME[command_name]
    raw_parameters = _parameter_mapping(command_name, parameters)

    if not command.requires_parameters:
        if raw_parameters:
            raise UnsupportedLiveCommandError(f"Live PBX command {command_name} does not accept parameters.")
        return {}

    if command_name == "channel_status":
        _reject_unknown_parameters(command_name, raw_parameters, set(CHANNEL_PARAMETER_NAMES))
        return {
            "channel_id": _required_text_parameter(
                command_name,
                raw_parameters,
                CHANNEL_PARAMETER_NAMES,
                "channel_id",
                CHANNEL_ID_PATTERN,
            )
        }

    if command_name == "channel_hangup":
        _reject_unknown_parameters(command_name, raw_parameters, {*CHANNEL_PARAMETER_NAMES, "cause"})
        normalized = {
            "channel_id": _required_text_parameter(
                command_name,
                raw_parameters,
                CHANNEL_PARAMETER_NAMES,
                "channel_id",
                CHANNEL_ID_PATTERN,
            )
        }
        cause = _optional_int_parameter(command_name, raw_parameters, "cause", minimum=0, maximum=127)
        if cause is not None:
            normalized["cause"] = cause
        return normalized

    if command_name == "channel_redirect":
        _reject_unknown_parameters(
            command_name,
            raw_parameters,
            {*CHANNEL_PARAMETER_NAMES, "context", *EXTENSION_PARAMETER_NAMES, "priority"},
        )
        return {
            "channel_id": _required_text_parameter(
                command_name,
                raw_parameters,
                CHANNEL_PARAMETER_NAMES,
                "channel_id",
                CHANNEL_ID_PATTERN,
            ),
            "context": _required_text_parameter(
                command_name,
                raw_parameters,
                ("context",),
                "context",
                CONTEXT_PATTERN,
            ),
            "exten": _required_text_parameter(
                command_name,
                raw_parameters,
                EXTENSION_PARAMETER_NAMES,
                "exten",
                EXTENSION_PATTERN,
            ),
            "priority": _optional_int_parameter(command_name, raw_parameters, "priority", minimum=1, maximum=999999) or 1,
        }

    if command_name == "channel_originate":
        _reject_unknown_parameters(
            command_name,
            raw_parameters,
            {
                *CHANNEL_PARAMETER_NAMES,
                "context",
                *EXTENSION_PARAMETER_NAMES,
                "priority",
                "caller_id",
                "timeout_ms",
                "async",
            },
        )
        normalized = {
            "channel_id": _required_text_parameter(
                command_name,
                raw_parameters,
                CHANNEL_PARAMETER_NAMES,
                "channel_id",
                CHANNEL_ID_PATTERN,
            ),
            "context": _required_text_parameter(
                command_name,
                raw_parameters,
                ("context",),
                "context",
                CONTEXT_PATTERN,
            ),
            "exten": _required_text_parameter(
                command_name,
                raw_parameters,
                EXTENSION_PARAMETER_NAMES,
                "exten",
                EXTENSION_PATTERN,
            ),
            "priority": _optional_int_parameter(command_name, raw_parameters, "priority", minimum=1, maximum=999999) or 1,
            "async": _optional_bool_parameter(
                command_name,
                raw_parameters,
                "async",
                default=DEFAULT_CHANNEL_ORIGINATE_ASYNC,
            ),
        }
        caller_id = _optional_text_parameter(command_name, raw_parameters, "caller_id", CALLER_ID_PATTERN)
        if caller_id is not None:
            normalized["caller_id"] = caller_id
        timeout_ms = _optional_int_parameter(command_name, raw_parameters, "timeout_ms", minimum=1, maximum=3600000)
        if timeout_ms is not None:
            normalized["timeout_ms"] = timeout_ms
        return normalized

    raise UnsupportedLiveCommandError(f"Unsupported live PBX command: {command_name or 'missing'}.")


def ami_action_for_live_command(command_name: str, parameters: dict[str, Any] | None = None) -> LiveAMIAction:
    command_name = canonical_live_command_name(command_name)
    command = SUPPORTED_LIVE_COMMANDS_BY_NAME[command_name]
    parameters = validate_live_command_parameters(command_name, parameters)

    return LiveAMIAction(
        command_name=command.name,
        ami_action=command.ami_action,
        ami_parameters=_ami_parameters_for_live_command(command, parameters),
        complete_event=command.complete_event,
    )


def redact_live_operation_parameters(parameters: Mapping[str, Any] | None) -> dict[str, Any]:
    if parameters is None:
        return {}
    if not isinstance(parameters, Mapping):
        return {"_invalid_type": parameters.__class__.__name__}
    return redact_audit_mapping(parameters)


def build_live_operation_audit_details(
    *,
    actor,
    location,
    command_name: str,
    parameters: Mapping[str, Any] | None,
    result: dict,
    api_key=None,
) -> dict[str, Any]:
    actor_username = _api_actor_username(actor, api_key)
    details: dict[str, Any] = {
        "command": command_name,
        "actor_username": actor_username,
        "location_id": getattr(location, "id", None),
        "location_slug": getattr(location, "slug", ""),
        "parameters": redact_live_operation_parameters(parameters),
        "result": result,
    }
    if api_key is not None:
        details.update(
            {
                "api_key_id": getattr(api_key, "id", None),
                "api_key_name": getattr(api_key, "name", ""),
                "api_key_prefix": getattr(api_key, "prefix", ""),
                "api_key_scope_type": getattr(api_key, "scope_type", ""),
                "api_key_scope_id": getattr(api_key, "user_id", None)
                or getattr(api_key, "service_identity_id", None),
            }
        )
        service_identity = getattr(api_key, "service_identity", None)
        if service_identity is not None:
            details.update(service_identity_audit_details(service_identity, prefix="api_key_service_identity"))
    return details


def _api_actor_username(actor, api_key=None) -> str:
    if is_service_principal(actor):
        return actor.get_username()
    if getattr(actor, "is_authenticated", False):
        return actor.get_username()

    service_identity = getattr(api_key, "service_identity", None)
    if service_identity is not None:
        return service_identity_audit_label(service_identity)
    return "anonymous"


def _ami_parameters_for_live_command(command: LiveCommandSpec, parameters: dict[str, Any]) -> dict[str, str]:
    if not command.requires_parameters:
        return dict(command.ami_parameters)

    if command.name == "channel_status":
        return {"Channel": parameters["channel_id"]}
    if command.name == "channel_hangup":
        ami_parameters = {"Channel": parameters["channel_id"]}
        if "cause" in parameters:
            ami_parameters["Cause"] = str(parameters["cause"])
        return ami_parameters
    if command.name == "channel_redirect":
        return {
            "Channel": parameters["channel_id"],
            "Context": parameters["context"],
            "Exten": parameters["exten"],
            "Priority": str(parameters["priority"]),
        }
    if command.name == "channel_originate":
        ami_parameters = {
            "Channel": parameters["channel_id"],
            "Context": parameters["context"],
            "Exten": parameters["exten"],
            "Priority": str(parameters["priority"]),
            "Async": "true" if parameters["async"] else "false",
        }
        if "caller_id" in parameters:
            ami_parameters["CallerID"] = parameters["caller_id"]
        if "timeout_ms" in parameters:
            ami_parameters["Timeout"] = str(parameters["timeout_ms"])
        return ami_parameters
    raise UnsupportedLiveCommandError(f"Unsupported live PBX command: {command.name or 'missing'}.")


def _parameter_mapping(command_name: str, parameters: Mapping[str, Any] | None) -> dict[str, Any]:
    if parameters is None:
        return {}
    if not isinstance(parameters, Mapping):
        raise UnsupportedLiveCommandError(f"Live PBX command {command_name} parameters must be a JSON object.")
    return {str(key): value for key, value in parameters.items()}


def _reject_unknown_parameters(command_name: str, parameters: dict[str, Any], allowed_parameters: set[str]) -> None:
    unknown_parameters = sorted(set(parameters) - allowed_parameters)
    if unknown_parameters:
        raise UnsupportedLiveCommandError(
            f"Live PBX command {command_name} does not accept parameter(s): {', '.join(unknown_parameters)}."
        )


def _aliased_parameter(command_name: str, parameters: dict[str, Any], names: tuple[str, ...]):
    present_names = [name for name in names if name in parameters]
    if not present_names:
        return None
    value = parameters[present_names[0]]
    for name in present_names[1:]:
        if parameters[name] != value:
            raise UnsupportedLiveCommandError(
                f"Live PBX command {command_name} received conflicting aliases for {present_names[0]}."
            )
    return value


def _required_text_parameter(
    command_name: str,
    parameters: dict[str, Any],
    names: tuple[str, ...],
    label: str,
    pattern: re.Pattern,
) -> str:
    value = _aliased_parameter(command_name, parameters, names)
    if value is None:
        raise UnsupportedLiveCommandError(f"Live PBX command {command_name} requires parameter {label}.")
    if not isinstance(value, str):
        raise UnsupportedLiveCommandError(f"Live PBX command {command_name} parameter {label} is invalid.")
    text_value = str(value).strip()
    if not text_value or not pattern.fullmatch(text_value):
        raise UnsupportedLiveCommandError(f"Live PBX command {command_name} parameter {label} is invalid.")
    return text_value


def _optional_text_parameter(
    command_name: str,
    parameters: dict[str, Any],
    name: str,
    pattern: re.Pattern,
) -> str | None:
    if name not in parameters or parameters[name] in (None, ""):
        return None
    if not isinstance(parameters[name], str):
        raise UnsupportedLiveCommandError(f"Live PBX command {command_name} parameter {name} is invalid.")
    text_value = str(parameters[name]).strip()
    if not text_value or not pattern.fullmatch(text_value):
        raise UnsupportedLiveCommandError(f"Live PBX command {command_name} parameter {name} is invalid.")
    return text_value


def _optional_int_parameter(
    command_name: str,
    parameters: dict[str, Any],
    name: str,
    *,
    minimum: int,
    maximum: int,
) -> int | None:
    if name not in parameters or parameters[name] in (None, ""):
        return None
    value = parameters[name]
    if isinstance(value, bool):
        raise UnsupportedLiveCommandError(f"Live PBX command {command_name} parameter {name} must be an integer.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise UnsupportedLiveCommandError(
            f"Live PBX command {command_name} parameter {name} must be an integer."
        ) from exc
    if parsed < minimum or parsed > maximum:
        raise UnsupportedLiveCommandError(
            f"Live PBX command {command_name} parameter {name} must be between {minimum} and {maximum}."
        )
    return parsed


def _optional_bool_parameter(
    command_name: str,
    parameters: dict[str, Any],
    name: str,
    *,
    default: bool,
) -> bool:
    if name not in parameters:
        return default
    value = parameters[name]
    if not isinstance(value, bool):
        raise UnsupportedLiveCommandError(f"Live PBX command {command_name} parameter {name} must be a boolean.")
    return value


class AgentSession:
    _CLOSE_SENTINEL = object()

    def __init__(self, *, location_id: int, location_slug: str):
        self.location_id = location_id
        self.location_slug = location_slug
        self.session_id = uuid4().hex
        self._outbound_messages: queue.Queue[dict[str, Any] | object] = queue.Queue()
        self._pending_results: dict[str, Future] = {}
        self._lock = threading.RLock()
        self._closed = False

    def dispatch(
        self,
        *,
        command_name: str,
        parameters: dict[str, Any] | None = None,
        timeout_seconds: float = DEFAULT_LIVE_COMMAND_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        command_id = uuid4().hex
        return self._dispatch_pending_result(
            result_id=command_id,
            outbound_payload={
                "type": "live_command",
                "command_id": command_id,
                "command": command_name,
                "parameters": parameters or {},
            },
            timeout_seconds=timeout_seconds,
            timeout_message=f"PBX agent did not answer command {command_name}.",
        )

    def dispatch_recording_file(
        self,
        *,
        path: str,
        retention_days: int | None = None,
        timeout_seconds: float = DEFAULT_RECORDING_PLAYBACK_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        request_id = uuid4().hex
        outbound_payload: dict[str, Any] = {
            "type": "recording_file_request",
            "request_id": request_id,
            "path": path,
        }
        if retention_days is not None:
            outbound_payload["retention_days"] = retention_days
        return self._dispatch_pending_result(
            result_id=request_id,
            outbound_payload=outbound_payload,
            timeout_seconds=timeout_seconds,
            timeout_message="PBX agent did not answer recording playback request.",
        )

    def _dispatch_pending_result(
        self,
        *,
        result_id: str,
        outbound_payload: dict[str, Any],
        timeout_seconds: float,
        timeout_message: str,
    ) -> dict[str, Any]:
        result_future: Future = Future()
        with self._lock:
            if self._closed:
                raise AgentUnavailableError(f"PBX agent for {self.location_slug} is not connected.")
            self._pending_results[result_id] = result_future

        self._outbound_messages.put(outbound_payload)
        try:
            result = result_future.result(timeout=timeout_seconds)
        except FutureTimeoutError as exc:
            with self._lock:
                self._pending_results.pop(result_id, None)
            raise AgentCommandTimeoutError(timeout_message) from exc
        if not isinstance(result, dict):
            raise AgentUnavailableError("PBX agent returned an invalid result.")
        return result

    def wait_for_outbound_message(self) -> dict[str, Any] | None:
        message = self._outbound_messages.get()
        if message is self._CLOSE_SENTINEL:
            return None
        return message

    def resolve_result(self, command_id: str, result: dict[str, Any]) -> bool:
        with self._lock:
            result_future = self._pending_results.pop(command_id, None)
        if result_future is None:
            return False
        result_future.set_result(result)
        return True

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            pending_results = list(self._pending_results.values())
            self._pending_results.clear()

        for result_future in pending_results:
            result_future.set_exception(AgentUnavailableError(f"PBX agent for {self.location_slug} disconnected."))
        self._outbound_messages.put(self._CLOSE_SENTINEL)


class AgentConnectionRegistry:
    def __init__(self):
        self._sessions_by_location_id: dict[int, AgentSession] = {}
        self._lock = threading.RLock()

    def register(self, *, location_id: int, location_slug: str) -> AgentSession:
        session = AgentSession(location_id=location_id, location_slug=location_slug)
        with self._lock:
            existing_session = self._sessions_by_location_id.get(location_id)
            self._sessions_by_location_id[location_id] = session
        if existing_session is not None:
            existing_session.close()
        return session

    def unregister(self, session: AgentSession) -> None:
        with self._lock:
            active_session = self._sessions_by_location_id.get(session.location_id)
            if active_session is session:
                self._sessions_by_location_id.pop(session.location_id, None)
        session.close()

    def dispatch(
        self,
        *,
        location_id: int,
        location_slug: str,
        command_name: str,
        parameters: dict[str, Any] | None = None,
        timeout_seconds: float = DEFAULT_LIVE_COMMAND_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        with self._lock:
            session = self._sessions_by_location_id.get(location_id)
        if session is None:
            raise AgentUnavailableError(f"PBX agent for {location_slug} is not connected.")
        return session.dispatch(
            command_name=command_name,
            parameters=parameters,
            timeout_seconds=timeout_seconds,
        )

    def dispatch_recording_file(
        self,
        *,
        location_id: int,
        location_slug: str,
        path: str,
        retention_days: int | None = None,
        timeout_seconds: float = DEFAULT_RECORDING_PLAYBACK_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        with self._lock:
            session = self._sessions_by_location_id.get(location_id)
        if session is None:
            raise AgentUnavailableError(f"PBX agent for {location_slug} is not connected.")
        return session.dispatch_recording_file(
            path=path,
            retention_days=retention_days,
            timeout_seconds=timeout_seconds,
        )

    def resolve_result(self, session: AgentSession, command_id: str, result: dict[str, Any]) -> bool:
        with self._lock:
            active_session = self._sessions_by_location_id.get(session.location_id)
        if active_session is not session:
            return False
        return session.resolve_result(command_id, result)

    def is_connected(self, location_id: int) -> bool:
        with self._lock:
            return location_id in self._sessions_by_location_id


agent_connection_registry = AgentConnectionRegistry()


def run_location_live_command(
    location,
    command_name: str,
    parameters: dict[str, Any] | None = None,
    *,
    timeout_seconds: float = DEFAULT_LIVE_COMMAND_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    command_name = canonical_live_command_name(command_name)
    parameters = validate_live_command_parameters(command_name, parameters)
    ami_action_for_live_command(command_name, parameters)
    return agent_connection_registry.dispatch(
        location_id=location.id,
        location_slug=location.slug,
        command_name=command_name,
        parameters=parameters,
        timeout_seconds=timeout_seconds,
    )


def run_location_recording_playback(
    location,
    path: str,
    *,
    retention_days: int | None = None,
    timeout_seconds: float = DEFAULT_RECORDING_PLAYBACK_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    return agent_connection_registry.dispatch_recording_file(
        location_id=location.id,
        location_slug=location.slug,
        path=path,
        retention_days=retention_days,
        timeout_seconds=timeout_seconds,
    )
