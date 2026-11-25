#!/usr/bin/env bash
# One-time, idempotent install of the SR665 fan controller.
# Usage: sudo bash /opt/fan-control/deploy/install.sh
set -euo pipefail

APP_DIR=/opt/fan-control
CONFIG_DIR=/etc/fanctl
UNIT=/etc/systemd/system/sr665-fanctl.service
UDEV_RULE=/etc/udev/rules.d/99-ipmi-group.rules
WRAPPER=/usr/local/bin/fanctl

[[ ${EUID} -eq 0 ]] || { echo "run me with sudo" >&2; exit 1; }
[[ -f ${APP_DIR}/src/sr665_fanctl/cli.py ]] || { echo "deploy the code to ${APP_DIR} first" >&2; exit 1; }

# Group and service user (idempotent).
getent group ipmi >/dev/null || groupadd --system ipmi
id -u fanctl >/dev/null 2>&1 || useradd --system --gid ipmi --no-create-home --shell /usr/sbin/nologin fanctl

# /dev/ipmi0 for the ipmi group: udev rule for future boots, chmod for this one.
cat > "${UDEV_RULE}" <<'EOF'
# sr665-fanctl: in-band IPMI without sudo (deploy/install.sh in fan-control)
KERNEL=="ipmi*", MODE="0660", GROUP="ipmi"
EOF
udevadm control --reload-rules
if [[ -e /dev/ipmi0 ]]; then
    chgrp ipmi /dev/ipmi0
    chmod 0660 /dev/ipmi0
fi

# Wrapper and unit are always refreshed from the repo.
install -d -m 0755 "${CONFIG_DIR}"
install -m 0755 "${APP_DIR}/deploy/fanctl-wrapper.sh" "${WRAPPER}"
install -m 0644 "${APP_DIR}/deploy/sr665-fanctl.service" "${UNIT}"
# The config is only seeded once; later tuning is an explicit root action.
if [[ ! -f ${CONFIG_DIR}/config.toml ]]; then
    install -m 0644 "${APP_DIR}/deploy/config.toml" "${CONFIG_DIR}/config.toml"
fi

systemctl daemon-reload
udevadm settle
echo "installed; next: sudo usermod -aG ipmi <your-user>, then the Phase 0 probe (docs/probe-results.md)"
