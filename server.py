#!/usr/bin/env python3
"""
Render WebSocket TCP relay for a local HTTPS CONNECT proxy.

Routes:
  GET /          Static status/instructions page
  GET /healthz   Health check
  GET /relay     WebSocket TCP tunnel endpoint

The relay only accepts targets that resolve to public IP addresses and, by

default, only allows port 443. Keep PROXY_TOKEN secret.
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import os
import socket
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import unquote

from aiohttp import WSMsgType, web

DEFAULT_ALLOWED_PORTS = "443"
READ_LIMIT = int(os.getenv("READ_LIMIT", "65536"))


@dataclass(frozen=True)
class Target:
    host: str
    port: int


def get_allowed_ports() -> set[int]:
    raw = os.getenv("ALLOWED_PORTS", DEFAULT_ALLOWED_PORTS)
    ports: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        port = int(part)
        if port < 1 or port > 65535:
            raise ValueError(f"Invalid port in ALLOWED_PORTS: {port}")
        ports.add(port)
    return ports or {443}


def parse_target(value: str | None) -> Target:
    if not value:
        raise ValueError("missing target")

    value = unquote(value).strip()

    # IPv6 authority form: [2001:db8::1]:443
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            raise ValueError("bad IPv6 target")
        host = value[1:end]
        rest = value[end + 1 :]
        port = 443
        if rest:
            if not rest.startswith(":"):
                raise ValueError("bad IPv6 target")
            port = int(rest[1:])
        return Target(host=host, port=port)

    # Regular host:port. If there are multiple colons without brackets, treat as
    # an IPv6 literal with implicit 443.
    if value.count(":") == 1:
        host, port_s = value.rsplit(":", 1)
        return Target(host=host, port=int(port_s or "443"))

    return Target(host=value, port=443)


def is_public_ip(addr: str) -> bool:
    ip = ipaddress.ip_address(addr)
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def resolve_public_addresses(host: str, port: int) -> list[tuple[int, str]]:
    # Handle IP literals directly.
    try:
        ip = ipaddress.ip_address(host)
        if not is_public_ip(str(ip)):
            raise ValueError("target resolves to a blocked address")
        family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
        return [(family, str(ip))]
    except ValueError as exc:
        if "blocked" in str(exc):
            raise

    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(
        host,
        port,
        type=socket.SOCK_STREAM,
        proto=socket.IPPROTO_TCP,
    )

    addresses: list[tuple[int, str]] = []
    seen: set[str] = set()
    for family, _type, _proto, _canon, sockaddr in infos:
        addr = sockaddr[0]
        if addr in seen:
            continue
        seen.add(addr)
        if not is_public_ip(addr):
            raise ValueError("target resolves to a blocked address")
        addresses.append((family, addr))

    if not addresses:
        raise ValueError("target did not resolve")
    return addresses


async def open_validated_connection(target: Target):
    allowed_ports = get_allowed_ports()
    if target.port not in allowed_ports:
        allowed = ", ".join(str(p) for p in sorted(allowed_ports))
        raise ValueError(f"port {target.port} not allowed; allowed: {allowed}")

    addresses = await resolve_public_addresses(target.host, target.port)
    last_error: Exception | None = None

    for family, addr in addresses:
        try:
            return await asyncio.open_connection(
                host=addr,
                port=target.port,
                family=family,
            )
        except OSError as exc:
            last_error = exc

    raise OSError(f"could not connect to target: {last_error}")


def require_token(request: web.Request) -> None:
    expected = os.getenv("PROXY_TOKEN")
    if not expected:
        raise web.HTTPServiceUnavailable(text="PROXY_TOKEN is not configured\n")

    supplied = request.query.get("token") or request.headers.get("x-proxy-token")
    if supplied != expected:
        raise web.HTTPUnauthorized(text="bad token\n")


async def index(request: web.Request) -> web.Response:
    host = request.host
    token_status = "configured" if os.getenv("PROXY_TOKEN") else "missing"
    allowed_ports = ", ".join(str(p) for p in sorted(get_allowed_ports()))
    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>WebSocket HTTPS Proxy Relay</title>
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, sans-serif; margin: 2rem; line-height: 1.5; max-width: 820px; }}
    code, pre {{ background: #f4f4f5; border-radius: 8px; padding: .15rem .35rem; }}
    pre {{ padding: 1rem; overflow: auto; }}
    .ok {{ color: #166534; font-weight: 700; }}
    .warn {{ color: #92400e; font-weight: 700; }}
  </style>
</head>
<body>
  <h1>WebSocket HTTPS Proxy Relay</h1>
  <p class="ok">Server is running.</p>
  <p>This app is a WebSocket TCP relay for a local HTTPS <code>CONNECT</code> proxy client.</p>
  <ul>
    <li>WebSocket endpoint: <code>wss://{html.escape(host)}/relay</code></li>
    <li>Token status: <span class="{'ok' if os.getenv('PROXY_TOKEN') else 'warn'}">{token_status}</span></li>
    <li>Allowed outbound ports: <code>{html.escape(allowed_ports)}</code></li>
  </ul>
  <h2>Local client example</h2>
  <pre>python client.py --remote wss://{html.escape(host)}/relay --token YOUR_TOKEN --listen 127.0.0.1:8080</pre>
  <p>Then configure your browser HTTPS proxy as <code>127.0.0.1:8080</code>.</p>
</body>
</html>"""
    return web.Response(text=page, content_type="text/html")


async def healthz(request: web.Request) -> web.Response:
    return web.Response(text="ok\n")


async def websocket_relay(request: web.Request) -> web.StreamResponse:
    require_token(request)
    target = parse_target(request.query.get("target"))

    ws = web.WebSocketResponse(heartbeat=25, max_msg_size=0)
    await ws.prepare(request)

    reader: asyncio.StreamReader | None = None
    writer: asyncio.StreamWriter | None = None
    tcp_to_ws_task: asyncio.Task | None = None

    try:
        reader, writer = await open_validated_connection(target)
        await ws.send_str("OK")

        async def tcp_to_ws() -> None:
            assert reader is not None
            while not reader.at_eof():
                data = await reader.read(READ_LIMIT)
                if not data:
                    break
                await ws.send_bytes(data)

        tcp_to_ws_task = asyncio.create_task(tcp_to_ws())

        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                writer.write(msg.data)
                await writer.drain()
            elif msg.type == WSMsgType.TEXT:
                # Control messages are not currently used after initial OK.
                if msg.data == "PING":
                    await ws.send_str("PONG")
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                break

    except Exception as exc:
        if not ws.closed:
            await ws.send_str(f"ERR {exc}")
    finally:
        if tcp_to_ws_task:
            tcp_to_ws_task.cancel()
        if writer:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
        if not ws.closed:
            await ws.close()

    return ws


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/relay", websocket_relay)
    return app


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    web.run_app(create_app(), host="0.0.0.0", port=port)
