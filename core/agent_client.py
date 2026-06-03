import asyncio
import base64
from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
import ssl
from urllib.parse import urlparse


DEFAULT_AGENT_WEBSOCKET_PATH = "/api/agent/ws/"


@dataclass(frozen=True)
class ActiveConfigMarker:
    version: int
    checksum: str
    timestamp: str

    def as_payload(self) -> dict:
        return {
            "type": "active_config",
            "version": self.version,
            "checksum": self.checksum,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True)
class AgentConfig:
    websocket_url: str
    token: str
    secret: str
    marker_path: Path

    @classmethod
    def from_env(cls) -> "AgentConfig":
        websocket_url = os.environ.get("PBX_AGENT_WS_URL") or portal_url_to_websocket_url(
            os.environ.get("PBX_AGENT_PORTAL_URL") or os.environ.get("PBX_PORTAL_URL", "")
        )
        return cls(
            websocket_url=websocket_url,
            token=os.environ.get("PBX_AGENT_TOKEN", ""),
            secret=os.environ.get("PBX_AGENT_SECRET", ""),
            marker_path=Path(os.environ.get("PBX_ACTIVE_CONFIG_MARKER", "/etc/asterisk/pbx-active-config.json")),
        )


def portal_url_to_websocket_url(portal_url: str) -> str:
    portal_url = portal_url.rstrip("/")
    if not portal_url:
        return ""
    if portal_url.startswith("https://"):
        return f"wss://{portal_url[8:]}{DEFAULT_AGENT_WEBSOCKET_PATH}"
    if portal_url.startswith("http://"):
        return f"ws://{portal_url[7:]}{DEFAULT_AGENT_WEBSOCKET_PATH}"
    if portal_url.startswith(("ws://", "wss://")):
        return portal_url
    return f"wss://{portal_url}{DEFAULT_AGENT_WEBSOCKET_PATH}"


def read_active_config_marker(path: str | Path) -> ActiveConfigMarker:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    version = data.get("version", data.get("version_number"))
    checksum = str(data.get("checksum") or "").strip().lower()
    timestamp = str(data.get("timestamp") or "").strip()
    if version is None or not checksum or not timestamp:
        raise ValueError("Active config marker requires version, checksum, and timestamp.")
    return ActiveConfigMarker(version=int(version), checksum=checksum, timestamp=timestamp)


async def report_active_config_once(config: AgentConfig) -> dict:
    if not config.websocket_url:
        raise ValueError("PBX agent WebSocket URL is required.")
    if not config.token or not config.secret:
        raise ValueError("PBX agent token and secret are required.")

    marker = read_active_config_marker(config.marker_path)
    return await websocket_json_exchange(
        config.websocket_url,
        marker.as_payload(),
        {
            "X-PBX-Agent-Token": config.token,
            "X-PBX-Agent-Secret": config.secret,
        },
    )


async def websocket_json_exchange(url: str, payload: dict, headers: dict[str, str]) -> dict:
    parsed = urlparse(url)
    if parsed.scheme not in {"ws", "wss"}:
        raise ValueError("PBX agent WebSocket URL must use ws:// or wss://.")
    host = parsed.hostname
    if not host:
        raise ValueError("PBX agent WebSocket URL requires a hostname.")

    port = parsed.port or (443 if parsed.scheme == "wss" else 80)
    ssl_context = ssl.create_default_context() if parsed.scheme == "wss" else None
    reader, writer = await asyncio.open_connection(host, port, ssl=ssl_context)
    try:
        await _client_handshake(reader, writer, parsed, headers)
        await _write_text_frame(writer, json.dumps(payload))
        response = await _read_text_frame(reader)
        await _write_close_frame(writer)
        return json.loads(response)
    finally:
        writer.close()
        await writer.wait_closed()


async def _client_handshake(reader, writer, parsed, headers: dict[str, str]) -> None:
    key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
    path = parsed.path or DEFAULT_AGENT_WEBSOCKET_PATH
    if parsed.query:
        path = f"{path}?{parsed.query}"
    host_header = parsed.hostname if parsed.port is None else f"{parsed.hostname}:{parsed.port}"
    request_headers = {
        "Host": host_header,
        "Upgrade": "websocket",
        "Connection": "Upgrade",
        "Sec-WebSocket-Key": key,
        "Sec-WebSocket-Version": "13",
        **headers,
    }
    request = [f"GET {path} HTTP/1.1", *[f"{name}: {value}" for name, value in request_headers.items()], "", ""]
    writer.write("\r\n".join(request).encode("ascii"))
    await writer.drain()
    response = await reader.readuntil(b"\r\n\r\n")
    status_line = response.split(b"\r\n", 1)[0]
    if b" 101 " not in status_line:
        raise ConnectionError(status_line.decode("latin1", errors="replace"))


async def _write_text_frame(writer, text: str) -> None:
    await _write_frame(writer, opcode=0x1, payload=text.encode("utf-8"))


async def _write_close_frame(writer) -> None:
    await _write_frame(writer, opcode=0x8, payload=b"")


async def _write_frame(writer, *, opcode: int, payload: bytes) -> None:
    mask = secrets.token_bytes(4)
    length = len(payload)
    header = bytearray([0x80 | opcode])
    if length < 126:
        header.append(0x80 | length)
    elif length <= 0xFFFF:
        header.extend([0x80 | 126, (length >> 8) & 0xFF, length & 0xFF])
    else:
        header.append(0x80 | 127)
        header.extend(length.to_bytes(8, "big"))
    masked_payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    writer.write(bytes(header) + mask + masked_payload)
    await writer.drain()


async def _read_text_frame(reader) -> str:
    first_two = await reader.readexactly(2)
    opcode = first_two[0] & 0x0F
    length = first_two[1] & 0x7F
    if length == 126:
        length = int.from_bytes(await reader.readexactly(2), "big")
    elif length == 127:
        length = int.from_bytes(await reader.readexactly(8), "big")
    payload = await reader.readexactly(length)
    if opcode != 0x1:
        raise ConnectionError("Expected a text frame from portal.")
    return payload.decode("utf-8")
