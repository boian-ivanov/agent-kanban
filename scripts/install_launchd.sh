#!/usr/bin/env bash
# Установка/деинсталляция UI как launchd-сервиса (auto-start при логине, macOS).
#
#   bash scripts/install_launchd.sh install      # подставляет путь, копирует, грузит
#   bash scripts/install_launchd.sh uninstall    # выгружает и удаляет
#   bash scripts/install_launchd.sh status
#   bash scripts/install_launchd.sh reload       # после правок plist-template
#
# Логи: ~/Library/Logs/agent-kanban/{stdout,stderr}.log
# UI:   http://localhost:7777/

set -euo pipefail

PLIST_NAME="com.agent-kanban.app"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST_SRC="${REPO_DIR}/${PLIST_NAME}.plist"
PLIST_DST="${HOME}/Library/LaunchAgents/${PLIST_NAME}.plist"
LOG_DIR="${HOME}/Library/Logs/agent-kanban"

mkdir -p "${LOG_DIR}" "${HOME}/Library/LaunchAgents"

# Подставляет __INSTALL_PATH__ и __HOME__ в plist-template и пишет в DST.
materialize_plist() {
    if [[ ! -f "${PLIST_SRC}" ]]; then
        echo "ERROR: plist template not found: ${PLIST_SRC}"
        exit 1
    fi
    if [[ ! -x "${REPO_DIR}/.venv/bin/python" ]]; then
        echo "ERROR: ${REPO_DIR}/.venv/bin/python not found."
        echo "  Создай virtualenv первым:"
        echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
        exit 1
    fi
    sed -e "s|__INSTALL_PATH__|${REPO_DIR}|g" \
        -e "s|__HOME__|${HOME}|g" \
        "${PLIST_SRC}" > "${PLIST_DST}"
}

case "${1:-status}" in
    install)
        launchctl unload "${PLIST_DST}" 2>/dev/null || true
        materialize_plist
        launchctl load "${PLIST_DST}"
        sleep 1
        echo "Loaded ${PLIST_NAME}"
        echo "UI:   http://localhost:7777/"
        echo "Logs: ${LOG_DIR}/stderr.log"
        ;;

    uninstall)
        launchctl unload "${PLIST_DST}" 2>/dev/null || true
        rm -f "${PLIST_DST}"
        echo "Unloaded and removed ${PLIST_NAME}"
        ;;

    status)
        if launchctl list | grep -q "${PLIST_NAME}"; then
            echo "Status: LOADED"
            launchctl list "${PLIST_NAME}" 2>&1 | head -20
        else
            echo "Status: NOT loaded"
        fi
        ;;

    reload)
        launchctl unload "${PLIST_DST}" 2>/dev/null || true
        materialize_plist
        launchctl load "${PLIST_DST}"
        echo "Reloaded ${PLIST_NAME}"
        ;;

    *)
        echo "Usage: $0 {install|uninstall|status|reload}"
        exit 1
        ;;
esac
