# Render WebSocket HTTPS Proxy

A small Python project that lets you run a local HTTPS `CONNECT` proxy while using a Render Web Service as a WebSocket TCP relay.

Render receives normal WebSocket traffic at `/relay`; it does **not** need to support inbound HTTP `CONNECT`.

## Architecture

```text
Browser / curl / app
  -> local HTTPS proxy: 127.0.0.1:8080
  -> WebSocket: wss://your-service.onrender.com/relay
  -> outbound TCP: target.example:443
```

## Files

- `server.py` — deploy this on Render. It exposes:
  - `/` static status page
  - `/healthz` health check
  - `/relay` WebSocket tunnel endpoint
- `client.py` — run locally. It accepts HTTP `CONNECT` and forwards tunnel bytes over WebSocket.
- `render.yaml` — Render Blueprint for one-click-ish setup from GitHub.
- `requirements.txt` — Python dependencies.

## Deploy to Render from GitHub

1. Create a new GitHub repo and push these files.
2. In Render, create a new **Web Service** from that repo.
3. Use these commands if Render does not read `render.yaml` automatically:

```bash
Build Command: pip install -r requirements.txt
Start Command: python server.py
```

4. Set an environment variable:

```bash
PROXY_TOKEN=use-a-long-random-secret
```

The server listens on `0.0.0.0:$PORT`, which Render provides automatically.

## Run the local client

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python client.py \
  --remote wss://YOUR-SERVICE.onrender.com/relay \
  --token use-a-long-random-secret \
  --listen 127.0.0.1:8080
```

Configure your browser or OS HTTPS proxy:

```text
HTTPS proxy host: 127.0.0.1
HTTPS proxy port: 8080
```

Test with curl:

```bash
curl -x http://127.0.0.1:8080 https://example.com/
```

## Security notes

This can become an open proxy if exposed incorrectly. Keep these protections:

- Use a long, private `PROXY_TOKEN`.
- Keep `ALLOWED_PORTS=443` unless you intentionally need more.
- The server blocks private, loopback, link-local, multicast, reserved, and unspecified IP ranges after DNS resolution.
- Do not publish your token in GitHub.

## Environment variables

| Variable | Used by | Default | Description |
|---|---:|---:|---|
| `PROXY_TOKEN` | server + client | required | Shared secret required for `/relay`. |
| `PORT` | server | `10000` | Render web service port. |
| `ALLOWED_PORTS` | server | `443` | Comma-separated outbound ports. |
| `REMOTE_RELAY` | client | none | Example: `wss://app.onrender.com/relay`. |
| `LOCAL_LISTEN` | client | `127.0.0.1:8080` | Local proxy bind address. |
| `READ_LIMIT` | server | `65536` | TCP read chunk size. |

## Limitations

- This is for HTTPS `CONNECT` tunneling, not plain HTTP proxying.
- Render Free instances can spin down when idle, causing cold starts.
- Some clients may open many parallel tunnels; free-tier limits can affect performance.
