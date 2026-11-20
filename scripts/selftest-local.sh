#!/usr/bin/env bash
# 无 Docker 环境下的本地等价自测：用三个本地进程模拟 compose 的三个容器。
# 端口：权威 127.0.0.1:5301/udp，解析器 127.0.0.1:5354/udp + 8081/http，网页 127.0.0.1:8000
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="${PYTHON:-/tmp/venv/bin/python}"
[ -x "$PY" ] || PY="python3"
RUN_DIR="$(mktemp -d)"
DB="$RUN_DIR/dns.db"
export DB_PATH="$DB"
export DOMAINS="app1.internal,app2.internal"

cleanup() {
  echo ">> 清理本地进程"
  kill "$AUTH_PID" "$RES_PID" "$WEB_PID" 2>/dev/null || true
  wait 2>/dev/null || true
  rm -rf "$RUN_DIR"
}
trap cleanup EXIT

echo ">> 启动权威 DNS (127.0.0.1:5301)"
LISTEN_ADDR=127.0.0.1 LISTEN_PORT=5301 "$PY" -m app.authoritative >"$RUN_DIR/auth.log" 2>&1 &
AUTH_PID=$!

echo ">> 启动缓存解析器 (127.0.0.1:5354, stats 8081)"
LISTEN_ADDR=127.0.0.1 LISTEN_PORT=5354 STATS_HOST=127.0.0.1 STATS_PORT=8081 \
UPSTREAM_HOST=127.0.0.1 UPSTREAM_PORT=5301 UPSTREAM_TIMEOUT=2.0 \
"$PY" -m app.resolver >"$RUN_DIR/resolver.log" 2>&1 &
RES_PID=$!

echo ">> 启动网页 (127.0.0.1:8000)"
LISTEN_ADDR=127.0.0.1 WEB_HOST=127.0.0.1 WEB_PORT=8000 \
RESOLVER_HOST=127.0.0.1 RESOLVER_PORT=5354 RESOLVER_STATS=http://127.0.0.1:8081 \
"$PY" -m app.web >"$RUN_DIR/web.log" 2>&1 &
WEB_PID=$!

# 等待端口就绪
for i in $(seq 1 50); do
  if curl -sf http://127.0.0.1:8000/api/state >/dev/null 2>&1; then break; fi
  sleep 0.2
done
curl -sf http://127.0.0.1:8000/api/state >/dev/null || { echo "网页未就绪，日志："; cat "$RUN_DIR"/*.log; exit 1; }

WEB_BASE=http://127.0.0.1:8000 \
RESOLVER_UDP_HOST=127.0.0.1 RESOLVER_UDP_PORT=5354 \
RESOLVER_STATS_HOST=127.0.0.1 RESOLVER_STATS_PORT=8081 \
"$PY" -m scripts.selftest

echo ">> 重启解析器，验证缓存不持久化"
kill "$RES_PID"; wait "$RES_PID" 2>/dev/null || true
LISTEN_ADDR=127.0.0.1 LISTEN_PORT=5354 STATS_HOST=127.0.0.1 STATS_PORT=8081 \
UPSTREAM_HOST=127.0.0.1 UPSTREAM_PORT=5301 UPSTREAM_TIMEOUT=2.0 \
"$PY" -m app.resolver >"$RUN_DIR/resolver2.log" 2>&1 &
RES_PID=$!
for i in $(seq 1 50); do
  curl -sf http://127.0.0.1:8081/healthz >/dev/null 2>&1 && break
  sleep 0.2
done
WEB_BASE=http://127.0.0.1:8000 \
RESOLVER_UDP_HOST=127.0.0.1 RESOLVER_UDP_PORT=5354 \
RESOLVER_STATS_HOST=127.0.0.1 RESOLVER_STATS_PORT=8081 \
"$PY" -m scripts.selftest --restart-check

echo ">> 全部自测通过"
