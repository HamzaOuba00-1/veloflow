#!/usr/bin/env bash
# Install or update the VeloFlow collector on a Debian VM.
#
# Idempotent: the first run installs everything, later runs pull the latest
# code from GitHub, resynchronise dependencies and restart the service.
# Usage on the VM:  sudo bash setup.sh
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/HamzaOuba00-1/veloflow.git}"
BRANCH="${BRANCH:-main}"
APP_USER="veloflow"
APP_HOME="/opt/veloflow"
APP_DIR="${APP_HOME}/app"
DATA_DIR="/var/lib/veloflow"

log() { printf '\n==> %s\n' "$*"; }

if [[ ${EUID} -ne 0 ]]; then
    echo "Run this script with sudo." >&2
    exit 1
fi

log "Swap: 1 GB safety net (the VM has 1 GB of RAM and no swap by default)"
if ! swapon --show=NAME --noheadings | grep -qx /swapfile; then
    fallocate -l 1G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
    swapon /swapfile
fi
grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
# Use swap only under real memory pressure, not to evict the page cache.
echo 'vm.swappiness=10' > /etc/sysctl.d/99-veloflow.conf
sysctl --quiet -p /etc/sysctl.d/99-veloflow.conf

log "System packages"
# No terminal over `gcloud compute ssh --command`: tell apt not to ask anything.
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git curl ca-certificates >/dev/null

log "uv (installed once, system-wide)"
if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh \
        | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
fi

log "Secrets folder (filled by hand, see infra/vm/veloflow.env.example)"
install -d -o root -g root -m 700 /etc/veloflow
if [[ ! -f /etc/veloflow/veloflow.env ]]; then
    echo "    /etc/veloflow/veloflow.env is missing: monitoring stays disabled."
fi

log "Dedicated system user '${APP_USER}' (no login shell, no sudo)"
if ! id -u "${APP_USER}" >/dev/null 2>&1; then
    useradd --system --home-dir "${APP_HOME}" --create-home \
        --shell /usr/sbin/nologin "${APP_USER}"
fi
# Open data, read by the sync job: readable by every local user, writable by the service only.
install -d -o "${APP_USER}" -g "${APP_USER}" -m 755 "${DATA_DIR}"
# The model (copied from the laptop) and the forecast document it writes.
install -d -o "${APP_USER}" -g "${APP_USER}" -m 755 "${DATA_DIR}/models" "${DATA_DIR}/serving"

as_app() { sudo -u "${APP_USER}" -H "$@"; }

log "Code from ${REPO_URL} (${BRANCH})"
if [[ -d "${APP_DIR}/.git" ]]; then
    as_app git -C "${APP_DIR}" fetch --quiet origin "${BRANCH}"
    # Fast-forward only: the VM never has local commits; anything else is an error.
    as_app git -C "${APP_DIR}" merge --ff-only --quiet "origin/${BRANCH}"
else
    as_app git clone --quiet --branch "${BRANCH}" "${REPO_URL}" "${APP_DIR}"
fi
as_app git -C "${APP_DIR}" log -1 --format='Deployed commit: %h %s'

log "Python 3.12 and runtime dependencies, exactly as locked (no dev tools, no web app)"
# Explicit locations: the install must not depend on the user's home directory.
as_app env UV_CACHE_DIR="${APP_HOME}/.cache/uv" UV_PYTHON_INSTALL_DIR="${APP_HOME}/python" \
    uv sync --project "${APP_DIR}" --frozen --no-default-groups --quiet

log "systemd units"
install -m 644 "${APP_DIR}"/infra/vm/veloflow-*.service "${APP_DIR}"/infra/vm/veloflow-*.timer \
    /etc/systemd/system/
systemctl daemon-reload
TIMERS=(veloflow-compact.timer veloflow-weather.timer veloflow-predict.timer)
systemctl enable --quiet veloflow-collector.service "${TIMERS[@]}"
# restart, not start: on an update the running collector must load the new code.
systemctl restart veloflow-collector.service
systemctl start "${TIMERS[@]}"

log "Status"
systemctl --no-pager --lines=5 status veloflow-collector.service || true
systemctl --no-pager list-timers "${TIMERS[@]}" || true
