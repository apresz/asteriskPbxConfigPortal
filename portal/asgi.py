"""ASGI config for the portal project."""
import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "portal.settings")

django_application = get_asgi_application()

from core.agent_ws import AGENT_WEBSOCKET_PATH, AgentWebSocketApplication  # noqa: E402

agent_websocket_application = AgentWebSocketApplication()


async def application(scope, receive, send):
    if scope["type"] == "websocket":
        if scope.get("path") == AGENT_WEBSOCKET_PATH:
            await agent_websocket_application(scope, receive, send)
        else:
            await send({"type": "websocket.close", "code": 4404})
        return

    await django_application(scope, receive, send)
