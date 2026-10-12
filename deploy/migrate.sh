#!/bin/sh
# Move the running system to another machine without losing its record.
#
#   old host:  deploy/migrate.sh export FILE   stops and disables every unit here, then packs the state
#   new host:  deploy/migrate.sh import FILE   unpacks it into this checkout (then deploy/install.sh --enable)
#
# FILE holds config/agentic.toml, config/secrets.toml, .env, data/ and ~/.config/agentic-trading
# (broker tokens, upgrade.env): carry it privately (USB stick, scp), never through a synced folder,
# and delete it once imported. Nothing here prints a secret. The arm latches are left behind on
# purpose, so the new host starts disarmed (README: "Do not copy a populated data/state").
set -eu

repo=$(cd "$(dirname "$0")/.." && pwd)
units="agentic-trading agentic-trading-dashboard agentic-trading-venues
agentic-trading-swarm.timer agentic-trading-upgrade.timer agentic-trading-upgrade-watch.timer"

usage() { echo "usage: deploy/migrate.sh export|import FILE" >&2; exit 2; }
[ $# -eq 2 ] || usage
file=$2

running() {
    for u in agentic-trading-upgrade.service agentic-trading-swarm.service; do
        state=$(systemctl --user is-active "$u" 2>/dev/null || true)
        case "$state" in active|activating|deactivating) echo "$u" ;; esac
    done
}

case "$1" in
export)
    busy=$(running)
    [ -z "$busy" ] || { echo "refusing: $busy is mid-run; try again when it finishes" >&2; exit 1; }
    # shellcheck disable=SC2086
    systemctl --user disable --now $units 2>/dev/null || true
    echo "stopped and disabled on this host: $(echo $units)"
    umask 077
    set -- -C "$repo" config/agentic.toml data
    [ -f "$repo/config/secrets.toml" ] && set -- "$@" config/secrets.toml
    [ -f "$repo/.env" ] && set -- "$@" .env
    [ -d "$HOME/.config/agentic-trading" ] && set -- "$@" -C "$HOME" .config/agentic-trading
    tar -czf "$file" --exclude=data/state/arm.json --exclude=data/state/venues_arm.json \
        --exclude='data/state/*.pid' --transform 's|^\.config/agentic-trading|_home/.config/agentic-trading|' "$@"
    echo "wrote $file: $(tar -tzf "$file" | wc -l) entries, $(du -h "$file" | cut -f1) (mode 600)"
    ;;
import)
    [ -r "$file" ] || { echo "cannot read $file" >&2; exit 2; }
    for u in agentic-trading agentic-trading-venues; do
        if systemctl --user is-active --quiet "$u" 2>/dev/null; then
            echo "refusing: $u is running on this host; stop it first" >&2; exit 1
        fi
    done
    if [ -f "$repo/config/agentic.toml" ]; then
        cp -p "$repo/config/agentic.toml" "$repo/config/agentic.toml.bak-migrate"
        echo "kept the existing config as config/agentic.toml.bak-migrate"
    fi
    umask 077
    tar -xzf "$file" -C "$repo" --exclude='_home/*'
    if tar -tzf "$file" | grep -q '^_home/'; then
        tar -xzf "$file" -C "$HOME" --strip-components=1 --wildcards '_home/*'
    fi
    echo "imported $(tar -tzf "$file" | wc -l) entries into $repo and ~/.config/agentic-trading"
    echo "next: deploy/install.sh --enable, then delete $file"
    ;;
*) usage ;;
esac
