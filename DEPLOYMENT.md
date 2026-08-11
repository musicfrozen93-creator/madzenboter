# Deployment & the "analysis service unreachable" (503) fix

The website (Vercel) reaches the analysis engine like this:

```
Browser ──HTTPS──▶ Vercel /api/analyze (serverless)
                      │  server-to-server fetch(ANALYSIS_API_URL + '/api/analyze')
                      ▼
             FastAPI analysis API (Docker/Portainer on your VPS)
                      ▼  provider → analysis pipeline
```

A `503 "The analysis service is unreachable."` means the **Vercel function could
not connect** to the FastAPI service at all. There are exactly two causes, and in
this incident **both** are present:

1. **The FastAPI container is not running** because host port 8000 was already
   allocated (`Bind for 0.0.0.0:8000 failed: port is already allocated`). A stale
   container still owns 8000, so the new one can't start → nothing is listening.
2. **`ANALYSIS_API_URL` on Vercel is wrong.** Vercel is external — it cannot reach
   `localhost:8000` (its own loopback) or a Docker service name
   (`zentry-analysis-api:8000`). It needs a **public** endpoint.

The backend code itself is healthy (verified: `/health` OK, `/api/analyze` returns
real signals for BTCUSDT 15m/1h/4h). This is purely a deployment/connectivity fix.

---

## Recommended architecture (fixes BOTH causes at once)

Put the FastAPI container behind your existing reverse proxy on the shared Docker
network, exposed at a subdomain over HTTPS. The proxy reaches the container
**internally**, so no host port is published and the 8000 conflict disappears.

```
Vercel ──▶ https://api.zentryai.site ──▶ reverse proxy ──▶ http://zentry-analysis-api:8000
```

Then set on **Vercel**: `ANALYSIS_API_URL=https://api.zentryai.site`

---

## Step-by-step

### A. On the VPS — find what owns port 8000

```bash
docker ps -a --format '{{.Names}}\t{{.Status}}\t{{.Ports}}'
sudo lsof -i :8000        # or: sudo ss -ltnp 'sport = :8000'
```

If a **stale/duplicate** `zentry-analysis-api` (or an old bot container) holds
8000 and is not otherwise needed, remove it — then redeploy:

```bash
docker rm -f <stale_container_name>     # ONLY if it is the obsolete analysis/bot container
```

> Do NOT remove postgres, the reverse proxy, or any unrelated production service.

### B. Choose ONE deployment mode

**Mode 1 — Reverse proxy (recommended).** In `docker-compose.yml`, delete the
`ports:` block from `analysis-api` (it already has `expose: ["8000"]`), and put it
on your proxy network (uncomment the `networks:` lines and set the external
network name). Add a proxy route for `api.zentryai.site → zentry-analysis-api:8000`
(nginx `proxy_pass`, Caddy reverse_proxy, or Traefik labels). No host port is
published, so the 8000 conflict cannot recur.

**Mode 2 — Raw host port (quick).** Keep the `ports:` block but pick a free host
port to avoid the conflict:

```bash
# .env next to docker-compose.yml
ANALYSIS_HOST_PORT=8010
```

Open/firewall that port and set `ANALYSIS_API_URL=http://<VPS_PUBLIC_IP>:8010` on
Vercel. (HTTP server-to-server is functional but HTTPS via Mode 1 is preferred.)

### C. Redeploy the analysis service

```bash
docker compose up -d --build analysis-api
docker compose logs -f analysis-api        # expect: "ZENTRY MARKET ANALYSIS API ... Listening on http://0.0.0.0:8000"
docker inspect --format '{{.State.Health.Status}}' zentry-analysis-api   # → healthy
curl -fsS http://localhost:8000/health     # from the VPS → {"status":"ok",...}
```

### D. On Vercel — set the env var and redeploy

- Project → Settings → Environment Variables →
  `ANALYSIS_API_URL = https://api.zentryai.site` (Mode 1) or `http://<VPS_IP>:8010`
  (Mode 2). **Not** a `NEXT_PUBLIC_` variable.
- Redeploy so the serverless functions pick it up.

### E. Verify end to end (from the deployed browser)

- Log in, open the built-in diagnostic:
  `GET https://<your-site>/api/analyze/health`
  → `{ "reachable": true, "providers": ["binance"], ... }` means Vercel can reach
  FastAPI. `reachable: false` prints the real reason (and Vercel function logs now
  carry a loud "ANALYSIS_API_URL is not set / not reachable" line when misconfigured).
- Then run Analyze for BTCUSDT on 15m, 1h, 4h.

---

## Notes

- **Timeout:** a normal analysis is ~1s; a cold one ~1.5s. Keep
  `ANALYSIS_API_TIMEOUT_MS` (default 20000) below your Vercel function max
  duration so a slow upstream returns a clean error rather than a platform 504.
- **CORS is irrelevant here:** the browser never calls FastAPI directly — only the
  Vercel function does (server-to-server). `ALLOWED_ORIGINS` on FastAPI should
  still be your site origin as defence in depth.
- **Security:** no exchange/trading credentials exist in this stack; do not expose
  the raw port publicly if you can use the reverse proxy instead.
