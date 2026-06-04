import asyncio
import hmac
import json
from datetime import timezone as datetime_timezone
from urllib.parse import parse_qs

from asgiref.sync import sync_to_async
from django.utils import dateparse, timezone

from .live_operations import AgentSession, agent_connection_registry
from .models import Location


AGENT_WEBSOCKET_PATH = "/api/agent/ws/"


class AgentAuthenticationError(Exception):
    pass


class ActiveConfigReportError(ValueError):
    pass


class AgentTelemetryReportError(ValueError):
    pass


def authenticate_agent(token: str | None, secret: str | None) -> Location:
    if not token or not secret:
        raise AgentAuthenticationError("Missing agent token or secret.")

    location = Location.objects.filter(agent_token=token, is_active=True).first()
    if location is None or not location.agent_secret:
        raise AgentAuthenticationError("Invalid agent token or secret.")
    if not hmac.compare_digest(location.agent_secret, secret):
        raise AgentAuthenticationError("Invalid agent token or secret.")
    return location


TELEMETRY_LIST_FIELDS = (
    "phone_registrations",
    "trunk_status",
    "active_calls",
    "queue_status",
    "recent_calls",
    "recording_metadata",
)


def update_active_config_report(location_id: int, payload: dict) -> Location:
    version = payload.get("version", payload.get("version_number"))
    checksum = str(payload.get("checksum") or "").strip().lower()
    timestamp = str(payload.get("timestamp") or "").strip()

    try:
        version_number = int(version)
    except (TypeError, ValueError) as exc:
        raise ActiveConfigReportError("Active config version must be an integer.") from exc
    if version_number <= 0:
        raise ActiveConfigReportError("Active config version must be positive.")
    if len(checksum) != 64 or any(char not in "0123456789abcdef" for char in checksum):
        raise ActiveConfigReportError("Active config checksum must be a SHA-256 hex digest.")

    parsed_timestamp = dateparse.parse_datetime(timestamp)
    if parsed_timestamp is None:
        raise ActiveConfigReportError("Active config timestamp must be ISO-8601.")
    if timezone.is_naive(parsed_timestamp):
        parsed_timestamp = timezone.make_aware(parsed_timestamp, datetime_timezone.utc)

    location = Location.objects.get(pk=location_id)
    location.active_config_version_number = version_number
    location.active_config_checksum = checksum
    location.active_config_timestamp = parsed_timestamp
    location.active_config_reported_at = timezone.now()
    location.save(
        update_fields=[
            "active_config_version_number",
            "active_config_checksum",
            "active_config_timestamp",
            "active_config_reported_at",
            "updated_at",
        ]
    )
    return location


def update_agent_telemetry_report(location_id: int, payload: dict) -> Location:
    timestamp = str(payload.get("timestamp") or "").strip()
    parsed_timestamp = dateparse.parse_datetime(timestamp)
    if parsed_timestamp is None:
        raise AgentTelemetryReportError("Telemetry timestamp must be ISO-8601.")
    if timezone.is_naive(parsed_timestamp):
        parsed_timestamp = timezone.make_aware(parsed_timestamp, datetime_timezone.utc)

    telemetry: dict[str, object] = {
        "timestamp": parsed_timestamp.isoformat(),
    }
    for field_name in TELEMETRY_LIST_FIELDS:
        value = payload.get(field_name)
        if not isinstance(value, list):
            raise AgentTelemetryReportError(f"Telemetry field {field_name} must be a list.")
        telemetry[field_name] = value

    location_health = payload.get("location_health")
    if not isinstance(location_health, dict):
        raise AgentTelemetryReportError("Telemetry field location_health must be an object.")
    telemetry["location_health"] = location_health

    call_events = payload.get("call_events", [])
    if not isinstance(call_events, list):
        raise AgentTelemetryReportError("Telemetry field call_events must be a list.")
    telemetry["call_events"] = call_events

    telemetry_errors = payload.get("telemetry_errors", [])
    if not isinstance(telemetry_errors, list):
        raise AgentTelemetryReportError("Telemetry field telemetry_errors must be a list.")
    telemetry["telemetry_errors"] = telemetry_errors

    location = Location.objects.get(pk=location_id)
    location.agent_telemetry = telemetry
    location.agent_telemetry_errors = telemetry_errors
    location.agent_telemetry_reported_at = timezone.now()
    location.save(
        update_fields=[
            "agent_telemetry",
            "agent_telemetry_errors",
            "agent_telemetry_reported_at",
            "updated_at",
        ]
    )
    return location


class AgentWebSocketApplication:
    async def __call__(self, scope, receive, send):
        connect_message = await receive()
        if connect_message["type"] != "websocket.connect":
            await send({"type": "websocket.close", "code": 4400})
            return

        token, secret = _credentials_from_scope(scope)
        try:
            location = await sync_to_async(authenticate_agent, thread_sensitive=True)(token, secret)
        except AgentAuthenticationError:
            await send({"type": "websocket.close", "code": 4401})
            return

        await send({"type": "websocket.accept"})
        await send(
            {
                "type": "websocket.send",
                "text": json.dumps({"type": "agent_authenticated", "location": location.slug}),
            }
        )

        agent_session = agent_connection_registry.register(location_id=location.id, location_slug=location.slug)
        try:
            await self._message_loop(
                location=location,
                agent_session=agent_session,
                receive=receive,
                send=send,
            )
        finally:
            agent_connection_registry.unregister(agent_session)

    async def _message_loop(
        self,
        *,
        location: Location,
        agent_session: AgentSession,
        receive,
        send,
    ) -> None:
        receive_task = asyncio.create_task(receive())
        outbound_task = asyncio.create_task(asyncio.to_thread(agent_session.wait_for_outbound_message))
        try:
            while True:
                done, _pending = await asyncio.wait(
                    {receive_task, outbound_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if receive_task in done:
                    message = receive_task.result()
                    message_type = message["type"]
                    if message_type == "websocket.disconnect":
                        return
                    if message_type != "websocket.receive":
                        await _send_error(send, "Unsupported WebSocket event.")
                        receive_task = asyncio.create_task(receive())
                        continue

                    payload = _payload_from_message(message)
                    if payload is None:
                        await _send_error(send, "Expected a JSON text message.")
                    else:
                        await _handle_agent_payload(location, agent_session, payload, send)
                    receive_task = asyncio.create_task(receive())

                if outbound_task in done:
                    outbound_payload = outbound_task.result()
                    if outbound_payload is None:
                        return
                    await send(
                        {
                            "type": "websocket.send",
                            "text": json.dumps(outbound_payload),
                        }
                    )
                    outbound_task = asyncio.create_task(asyncio.to_thread(agent_session.wait_for_outbound_message))
        finally:
            agent_session.close()
            for task in (receive_task, outbound_task):
                if not task.done():
                    task.cancel()


async def _handle_agent_payload(location: Location, agent_session: AgentSession, payload: dict, send) -> None:
    if payload.get("type") == "active_config":
        try:
            updated_location = await sync_to_async(update_active_config_report, thread_sensitive=True)(
                location.id,
                payload,
            )
        except ActiveConfigReportError as exc:
            await _send_error(send, str(exc))
            return

        await send(
            {
                "type": "websocket.send",
                "text": json.dumps(
                    {
                        "type": "active_config_ack",
                        "location": updated_location.slug,
                        "version": updated_location.active_config_version_number,
                        "checksum": updated_location.active_config_checksum,
                        "timestamp": updated_location.active_config_timestamp.isoformat(),
                    }
                ),
            }
        )
        return

    if payload.get("type") == "telemetry":
        try:
            updated_location = await sync_to_async(update_agent_telemetry_report, thread_sensitive=True)(
                location.id,
                payload,
            )
        except AgentTelemetryReportError as exc:
            await _send_error(send, str(exc))
            return

        await send(
            {
                "type": "websocket.send",
                "text": json.dumps(
                    {
                        "type": "telemetry_ack",
                        "location": updated_location.slug,
                        "timestamp": updated_location.agent_telemetry["timestamp"],
                        "categories": [*TELEMETRY_LIST_FIELDS, "call_events", "location_health"],
                        "error_count": len(updated_location.agent_telemetry_errors),
                    }
                ),
            }
        )
        return

    if payload.get("type") == "live_command_result":
        command_id = str(payload.get("command_id") or "")
        if not command_id or not agent_connection_registry.resolve_result(agent_session, command_id, payload):
            await _send_error(send, "Unknown live command result.")
            return
        await send(
            {
                "type": "websocket.send",
                "text": json.dumps({"type": "live_command_result_ack", "command_id": command_id}),
            }
        )
        return

    if payload.get("type") == "recording_file_result":
        request_id = str(payload.get("request_id") or "")
        if not request_id or not agent_connection_registry.resolve_result(agent_session, request_id, payload):
            await _send_error(send, "Unknown recording file result.")
            return
        await send(
            {
                "type": "websocket.send",
                "text": json.dumps({"type": "recording_file_result_ack", "request_id": request_id}),
            }
        )
        return

    await _send_error(send, "Unsupported agent message type.")


def _credentials_from_scope(scope) -> tuple[str | None, str | None]:
    query = parse_qs(scope.get("query_string", b"").decode("utf-8"))
    headers = {
        name.decode("latin1").lower(): value.decode("latin1")
        for name, value in scope.get("headers", [])
    }
    token = _first(query.get("token")) or headers.get("x-pbx-agent-token")
    secret = _first(query.get("secret")) or headers.get("x-pbx-agent-secret")
    authorization = headers.get("authorization", "")
    if authorization.lower().startswith("bearer ") and ":" in authorization:
        bearer_token, bearer_secret = authorization[7:].split(":", 1)
        token = token or bearer_token
        secret = secret or bearer_secret
    return token, secret


def _payload_from_message(message) -> dict | None:
    raw_payload = message.get("text")
    if raw_payload is None and message.get("bytes") is not None:
        try:
            raw_payload = message["bytes"].decode("utf-8")
        except UnicodeDecodeError:
            return None
    if raw_payload is None:
        return None
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


async def _send_error(send, message: str) -> None:
    await send(
        {
            "type": "websocket.send",
            "text": json.dumps({"type": "error", "error": message}),
        }
    )


def _first(values: list[str] | None) -> str | None:
    if not values:
        return None
    return values[0]
