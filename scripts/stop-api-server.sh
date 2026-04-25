#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
CODEX_PROXY_SCRIPT="${PROJECT_DIR}/scripts/codex-passthrough-server.py"
PID_FILE="${API_SERVER_PID_FILE:-${PROJECT_DIR}/tmp/api-server.pid}"
ACTIVE_HERMES_HOME="${HERMES_HOME:-${HOME}/.hermes}"
GATEWAY_PID_FILE="${ACTIVE_HERMES_HOME}/gateway.pid"
PYTHON_BIN="${PROJECT_DIR}/venv/bin/python"

is_running() {
    local pid="$1"

    if [[ -z "${pid}" ]] || ! kill -0 "${pid}" 2>/dev/null; then
        return 1
    fi

    local args
    args="$(ps -p "${pid}" -o args= 2>/dev/null || true)"
    [[ "${args}" == *"${PROJECT_DIR}/venv/bin/hermes"* && "${args}" == *" gateway"* ]] || \
        [[ "${args}" == *"${CODEX_PROXY_SCRIPT}"* ]]
}

read_gateway_pid() {
    if [[ ! -f "${GATEWAY_PID_FILE}" ]]; then
        return 1
    fi

    local python_bin="python3"
    if [[ -x "${PYTHON_BIN}" ]]; then
        python_bin="${PYTHON_BIN}"
    fi

    "${python_bin}" - "${GATEWAY_PID_FILE}" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
raw = path.read_text().strip()
if not raw:
    raise SystemExit(1)
try:
    payload = json.loads(raw)
except json.JSONDecodeError:
    print(raw)
    raise SystemExit(0)
if isinstance(payload, dict) and "pid" in payload:
    print(payload["pid"])
    raise SystemExit(0)
if isinstance(payload, int):
    print(payload)
    raise SystemExit(0)
raise SystemExit(1)
PY
}

pid=""
if [[ -f "${PID_FILE}" ]]; then
    pid="$(tr -d '[:space:]' < "${PID_FILE}")"
fi

if ! is_running "${pid}"; then
    if [[ -n "${pid}" ]]; then
        echo "Removing stale pidfile for PID ${pid}"
        rm -f "${PID_FILE}"
    fi
    pid="$(read_gateway_pid 2>/dev/null || true)"
fi

if ! is_running "${pid}"; then
    echo "API server is not running"
    rm -f "${PID_FILE}"
    exit 0
fi

kill "${pid}"

for _ in {1..30}; do
    if ! is_running "${pid}"; then
        rm -f "${PID_FILE}"
        rm -f "${GATEWAY_PID_FILE}"
        echo "API server stopped"
        exit 0
    fi
    sleep 1
done

echo "Process ${pid} did not exit after 30 seconds; sending SIGKILL"
kill -9 "${pid}"
rm -f "${PID_FILE}"
rm -f "${GATEWAY_PID_FILE}"
echo "API server stopped"
