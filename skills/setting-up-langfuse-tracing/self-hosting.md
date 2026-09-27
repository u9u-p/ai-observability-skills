# Self-hosting Langfuse (v4) with Docker Compose

These are the steps and the pitfalls, in the order you'll hit them.

## 1. Check out upstream and leave it clean

```bash
git clone https://github.com/langfuse/langfuse.git ../langfuse
```

Don't edit `../langfuse/docker-compose.yml`. Put your changes in an **override file**
in your own repo so upstream stays pullable:

```bash
docker compose -p langfuse --env-file deploy/.env.langfuse \
  -f ../langfuse/docker-compose.yml -f deploy/docker-compose.langfuse.yml up -d
```

## 2. Generate secrets in `deploy/.env.langfuse` (gitignored, `chmod 600`)

```bash
cat > deploy/.env.langfuse <<EOF
NEXTAUTH_SECRET=$(openssl rand -base64 32)
SALT=$(openssl rand -base64 32)
ENCRYPTION_KEY=$(openssl rand -hex 32)
POSTGRES_PASSWORD=$(openssl rand -hex 16)
CLICKHOUSE_PASSWORD=$(openssl rand -hex 16)
MINIO_ROOT_PASSWORD=$(openssl rand -hex 16)
REDIS_AUTH=$(openssl rand -hex 16)
NEXTAUTH_URL=http://localhost:3100
TELEMETRY_ENABLED=false
# Headless bootstrap: org, project, login and a FIXED key pair on first boot.
LANGFUSE_INIT_ORG_ID=my-org
LANGFUSE_INIT_ORG_NAME=My Org
LANGFUSE_INIT_PROJECT_ID=my-project
LANGFUSE_INIT_PROJECT_NAME=my-project
LANGFUSE_INIT_PROJECT_PUBLIC_KEY=pk-lf-$(openssl rand -hex 16)
LANGFUSE_INIT_PROJECT_SECRET_KEY=sk-lf-$(openssl rand -hex 16)
LANGFUSE_INIT_USER_EMAIL=admin@example.local
LANGFUSE_INIT_USER_NAME=admin
LANGFUSE_INIT_USER_PASSWORD=$(openssl rand -hex 12)
EOF
chmod 600 deploy/.env.langfuse
```

With `LANGFUSE_INIT_*` set, nobody has to click through the UI to get keys, and the
keys stay the same across rebuilds. Check upstream's compose to confirm which variables
it passes through. A variable the compose file doesn't forward is **silently ignored**.

## 3. Override file: ports, networking, pass-throughs

```yaml
# deploy/docker-compose.langfuse.yml
services:
  langfuse-web:
    ports: !override ["3100:3000"]      # !override, see pitfall A
    networks: [default, app_trace]      # only the web container joins the app's network
    environment:
      HOSTNAME: 0.0.0.0                 # see pitfall B
  langfuse-worker:
    ports: !override ["127.0.0.1:3030:3030"]
  clickhouse:
    ports: !override ["127.0.0.1:8123:8123", "127.0.0.1:9100:9000"]
  postgres:
    ports: !override ["127.0.0.1:5532:5432"]
  redis:
    ports: !override ["127.0.0.1:6479:6379"]
  minio:
    ports: !override ["9190:9000", "127.0.0.1:9191:9001"]
networks:
  default: { name: langfuse_default }
  app_trace: { external: true }         # docker network create app_trace
```

Everything except the web UI and MinIO's S3 port is bound to loopback. The app gets an
**ingestion endpoint, not a database**.

## Pitfalls (each of these was found against a real deployment)

**A. Compose merges `ports` lists.** Without `!override`, your remapped ports are
published *in addition to* upstream's defaults, and those can collide with Grafana on
3000 or Postgres on 5432. Run `docker ps --format '{{.Names}} {{.Ports}}'` first to see
what's already taken.

**B. Next.js binds to `$HOSTNAME`.** Once `langfuse-web` joins a second network, it
listens on only one interface, and either the app's route or the published port stops
working. Setting `HOSTNAME=0.0.0.0` fixes both.

**C. v4 runs in `events_only` mode.** The legacy `/api/public/ingestion` write API and
the `GET /api/public/traces` read API are both disabled. Ingestion goes through
OpenTelemetry OTLP (`/api/public/otel/v1/traces`), and rows land in the ClickHouse
table `events_full`. The legacy `traces` and `observations` tables stay empty. Use
Python SDK `langfuse>=4`. Most tutorials predate this.

**D. The app is on a different host.** `http://langfuse-web:3000` resolves only on a
shared Docker network. Set `LANGFUSE_HOST=http://<langfuse-private-ip>:3100` and open
that port **only** to the app's security group. Keep `.env.langfuse` off the app host.

**E. Set `NEXTAUTH_URL` to the URL people will actually open.** If it's wrong, login
redirects break.

## Health check

```bash
curl -s http://localhost:3100/api/public/health   # {"status":"OK","version":"4.x"}
docker compose -p langfuse logs --tail=50 langfuse-worker
```
