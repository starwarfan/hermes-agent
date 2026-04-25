#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="${PROJECT_DIR}/venv"
HERMES_BIN="${VENV_DIR}/bin/hermes"
PYTHON_BIN="${VENV_DIR}/bin/python"
CODEX_PROXY_SCRIPT="${PROJECT_DIR}/scripts/codex-passthrough-server.py"
LOG_DIR="${PROJECT_DIR}/logs"
RUN_DIR="${PROJECT_DIR}/tmp"
LOG_FILE="${API_SERVER_LOG_FILE:-${LOG_DIR}/api-server.log}"
PID_FILE="${API_SERVER_PID_FILE:-${RUN_DIR}/api-server.pid}"
ACTIVE_HERMES_HOME="${HERMES_HOME:-${HOME}/.hermes}"
ACTIVE_HERMES_LOG_DIR="${ACTIVE_HERMES_HOME}/logs"
API_SERVER_MODE_VALUE="${API_SERVER_MODE:-integrated}"
API_SERVER_ENABLED_VALUE="${API_SERVER_ENABLED:-true}"
API_SERVER_KEY_VALUE="${API_SERVER_KEY:-replace-with-your-own-key}"
API_SERVER_PASSTHROUGH_ENABLED_VALUE="${API_SERVER_PASSTHROUGH_ENABLED:-true}"
API_SERVER_HOST_VALUE="${API_SERVER_HOST:-127.0.0.1}"
API_SERVER_PORT_VALUE="${API_SERVER_PORT:-8672}"

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

mkdir -p "${LOG_DIR}" "${RUN_DIR}"

case "${API_SERVER_MODE_VALUE}" in
    integrated|codex-standalone)
        ;;
    *)
        echo "Unsupported API_SERVER_MODE: ${API_SERVER_MODE_VALUE}" >&2
        echo "Supported values: integrated, codex-standalone" >&2
        exit 1
        ;;
esac

if [[ "${API_SERVER_MODE_VALUE}" == "integrated" ]]; then
    if ! mkdir -p "${ACTIVE_HERMES_LOG_DIR}" 2>/dev/null; then
        echo "Cannot create Hermes log directory at ${ACTIVE_HERMES_LOG_DIR}" >&2
        echo "Set HERMES_HOME to a writable path before starting the server." >&2
        exit 1
    fi
fi

if [[ ! -x "${HERMES_BIN}" ]]; then
    echo "Hermes executable not found at ${HERMES_BIN}" >&2
    echo "Create the venv first or adjust the script." >&2
    exit 1
fi

if [[ "${API_SERVER_MODE_VALUE}" == "codex-standalone" ]]; then
    if [[ ! -x "${PYTHON_BIN}" ]]; then
        echo "Python executable not found at ${PYTHON_BIN}" >&2
        echo "Create the venv first or adjust the script." >&2
        exit 1
    fi
    if [[ ! -f "${CODEX_PROXY_SCRIPT}" ]]; then
        echo "Codex passthrough script not found at ${CODEX_PROXY_SCRIPT}" >&2
        exit 1
    fi
fi

if [[ -f "${PID_FILE}" ]]; then
    existing_pid="$(tr -d '[:space:]' < "${PID_FILE}")"
    if is_running "${existing_pid}"; then
        echo "API server (${API_SERVER_MODE_VALUE}) is already running with PID ${existing_pid}"
        echo "Log file: ${LOG_FILE}"
        exit 0
    fi
    rm -f "${PID_FILE}"
fi

(
    cd "${PROJECT_DIR}"
    source "${VENV_DIR}/bin/activate"
    if [[ "${API_SERVER_MODE_VALUE}" == "codex-standalone" ]]; then
        nohup env \
            CODEX_PROXY_KEY="${API_SERVER_KEY_VALUE}" \
            CODEX_PROXY_HOST="${API_SERVER_HOST_VALUE}" \
            CODEX_PROXY_PORT="${API_SERVER_PORT_VALUE}" \
            "${PYTHON_BIN}" "${CODEX_PROXY_SCRIPT}" \
            >> "${LOG_FILE}" 2>&1 < /dev/null &
    else
        nohup env \
            HERMES_HOME="${ACTIVE_HERMES_HOME}" \
            API_SERVER_ENABLED="${API_SERVER_ENABLED_VALUE}" \
            API_SERVER_KEY="${API_SERVER_KEY_VALUE}" \
            API_SERVER_PASSTHROUGH_ENABLED="${API_SERVER_PASSTHROUGH_ENABLED_VALUE}" \
            API_SERVER_HOST="${API_SERVER_HOST_VALUE}" \
            API_SERVER_PORT="${API_SERVER_PORT_VALUE}" \
            "${HERMES_BIN}" gateway \
            >> "${LOG_FILE}" 2>&1 < /dev/null &
    fi
    echo $! > "${PID_FILE}"
)

pid="$(tr -d '[:space:]' < "${PID_FILE}")"
sleep 1

if ! is_running "${pid}"; then
    echo "API server failed to stay up. Check ${LOG_FILE}" >&2
    rm -f "${PID_FILE}"
    exit 1
fi

echo "API server (${API_SERVER_MODE_VALUE}) started with PID ${pid}"
echo "Log file: ${LOG_FILE}"
