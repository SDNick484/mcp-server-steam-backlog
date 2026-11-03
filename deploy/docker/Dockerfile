# Optional: the MCP server as a non-root container. The primary deploy is deploy/alpine/ (OpenRC).
#   docker build -f deploy/docker/Dockerfile -t mcp-server-steam-backlog .
#   docker run --rm -p 127.0.0.1:8486:8486 -e BACKLOG_URL=http://host.docker.internal:8484 \
#     -e MCP_HTTP_BIND=0.0.0.0 -e CF_ACCESS_TEAM_DOMAIN=... -e CF_ACCESS_AUD=... mcp-server-steam-backlog
# Binding 0.0.0.0 inside the container requires Cloudflare Access settings (remote.py refuses otherwise).
FROM python:3.12-alpine AS builder
WORKDIR /build
COPY . .
RUN pip wheel --no-cache-dir --wheel-dir=/wheels .

FROM python:3.12-alpine
RUN addgroup -S mcp && adduser -S -G mcp -u 10001 mcp
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels
USER 10001
WORKDIR /home/mcp
EXPOSE 8486
HEALTHCHECK --interval=30s --timeout=3s CMD wget -qO- http://127.0.0.1:8486/healthz || exit 1
ENTRYPOINT ["mcp-server-steam-backlog", "serve", "--http"]
