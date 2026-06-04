from __future__ import annotations

from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
import queue
import threading
from typing import Any
from uuid import uuid4


DEFAULT_LIVE_COMMAND_TIMEOUT_SECONDS = 15.0
DEFAULT_RECORDING_PLAYBACK_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class LiveCommandSpec:
    name: str
    label: str
    ami_action: str
    ami_parameters: dict[str, str]
    description: str
    complete_event: str | None = None


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
)
SUPPORTED_LIVE_COMMANDS_BY_NAME = {command.name: command for command in SUPPORTED_LIVE_COMMANDS}


def supported_live_commands() -> list[dict[str, str]]:
    return [
        {
            "name": command.name,
            "label": command.label,
            "description": command.description,
        }
        for command in SUPPORTED_LIVE_COMMANDS
    ]


def ami_action_for_live_command(command_name: str, parameters: dict[str, Any] | None = None) -> LiveAMIAction:
    command_name = str(command_name or "").strip()
    try:
        command = SUPPORTED_LIVE_COMMANDS_BY_NAME[command_name]
    except KeyError as exc:
        raise UnsupportedLiveCommandError(f"Unsupported live PBX command: {command_name or 'missing'}.") from exc

    parameters = parameters or {}
    if parameters:
        raise UnsupportedLiveCommandError(f"Live PBX command {command_name} does not accept parameters.")

    return LiveAMIAction(
        command_name=command.name,
        ami_action=command.ami_action,
        ami_parameters=dict(command.ami_parameters),
        complete_event=command.complete_event,
    )


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
