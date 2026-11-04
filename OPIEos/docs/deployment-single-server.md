# Single-server Docker deployment

The production stack is designed to run behind one public reverse-proxy entry
point. PostgreSQL, Redis, Gateway, Crawl4AI, and the sandbox provisioner stay on
the private Compose network.

## One-command setup

On a server with Docker Engine and the Compose plugin installed:

```bash
cp .env.example .env
# Edit .env: set strong passwords, SIDUS_API_KEY, SANDBOX_IMAGE,
# PROVISIONER_API_KEY, CRAWL4AI_TOKEN, and the public host settings.
make up
```

`make up` now seeds `config.yaml` from `config.production.example.yaml`, creates
the runtime directories, generates internal auth secrets when absent, detects
the provisioner sandbox, and automatically enables the Docker sandbox overlay
when the host Docker socket is available. Set `SANDBOX_BACKEND=kubernetes` to
opt into the Kubernetes backend instead.

For a public deployment, put TLS in front of the stack and set `BIND_HOST` to
the interface that should receive the private reverse-proxy traffic. Do not
publish PostgreSQL or Redis ports.

## Required production values

At minimum, `.env` must contain strong values for:

- `POSTGRES_PASSWORD`, `DATABASE_URL`
- `REDIS_PASSWORD`, `DEER_FLOW_STREAM_BRIDGE_REDIS_URL`
- `SIDUS_API_KEY`
- `CRAWL4AI_TOKEN`
- `SANDBOX_IMAGE`, `PROVISIONER_API_KEY`

Keep `.env`, `config.yaml`, and runtime data out of Git and back up the
PostgreSQL volume separately.
