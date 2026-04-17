#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PID_FILE="${API_SERVER_PID_FILE:-${PROJECT_DIR}/tmp/api-server.pid}"

is_running() {
    local pid="$1"

    if [[ -z "${pid}" ]] || ! kill -0 "${pid}" 2>/dev/null; then
        return 1
    fi

    local args
    args="$(ps -p "${pid}" -o args= 2>/dev/null || true)"
    [[ "${args}" == *"${PROJECT_DIR}/venv/bin/hermes"* && "${args}" == *" gateway"* ]]
}

if [[ ! -f "${PID_FILE}" ]]; then
    echo "API server is not running (no pidfile at ${PID_FILE})"
    exit 0
fi

pid="$(tr -d '[:space:]' < "${PID_FILE}")"

if ! is_running "${pid}"; then
    echo "Removing stale pidfile for PID ${pid}"
    rm -f "${PID_FILE}"
    exit 0
fi

kill "${pid}"

for _ in {1..30}; do
    if ! is_running "${pid}"; then
        rm -f "${PID_FILE}"
        echo "API server stopped"
        exit 0
    fi
    sleep 1
done

echo "Process ${pid} did not exit after 30 seconds; sending SIGKILL"
kill -9 "${pid}"
rm -f "${PID_FILE}"
echo "API server stopped"
