#!/bin/sh
# Install this checkout's launchers and systemd --user units on this machine.
#
#   deploy/install.sh            write ~/.local/bin/agentic-trading-* and the units, reload systemd
#   deploy/install.sh --enable   ...then enable and start the services and the three timers
#
# The files in deploy/ name the checkout that wrote them (/home/doczeus/Projects/Agnetic TraDING);
# this rewrites that path to wherever this checkout lives, so the same files serve any user and path.
# Run one host at a time: two hosts would both push to `live` and share one broker login.
set -eu

repo=$(cd "$(dirname "$0")/.." && pwd)
bin="$HOME/.local/bin"
units="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
origin_repo="/home/doczeus/Projects/Agnetic TraDING"
origin_home="/home/doczeus"
user=${USER:-$(id -un)}  # a cleared environment (cron, a sandbox) has no $USER
services="agentic-trading agentic-trading-dashboard agentic-trading-venues"
timers="agentic-trading-swarm.timer agentic-trading-upgrade.timer agentic-trading-upgrade-watch.timer"

[ -x "$repo/.venv/bin/agentic-trading" ] || { echo "no $repo/.venv/bin/agentic-trading: create the venv first" >&2; exit 2; }
[ -f "$repo/config/agentic.toml" ] || { echo "no $repo/config/agentic.toml: import or create it first" >&2; exit 2; }

escape() { printf '%s' "$1" | sed 's/[&|\\]/\\&/g'; }
to_repo=$(escape "$repo")
to_home=$(escape "$HOME")
render() {
    sed -e "s|$origin_repo|$to_repo|g" -e "s|$origin_home|$to_home|g" "$1" > "$2"
}

mkdir -p "$bin" "$units"
for f in "$repo"/deploy/agentic-trading*; do
    name=$(basename "$f")
    case "$name" in
        *.service|*.timer) render "$f" "$units/$name" ;;
        *) render "$f" "$bin/$name"; chmod 755 "$bin/$name" ;;
    esac
done
systemctl --user daemon-reload
echo "installed launchers in $bin and units in $units for $repo"

branch=$(git -C "$repo" symbolic-ref --short -q HEAD || echo detached)
[ "$branch" = live ] || echo "warning: checkout is on '$branch'; the self-upgrader ships to 'live'" >&2
command -v bwrap >/dev/null || echo "warning: bwrap missing; the self-upgrader refuses to run without its sandbox" >&2
[ "$(loginctl show-user "$user" -p Linger --value 2>/dev/null)" = yes ] \
    || echo "warning: linger is off; services stop when you log out (sudo loginctl enable-linger $user)" >&2

if [ "${1:-}" = --enable ]; then
    # shellcheck disable=SC2086
    systemctl --user enable --now $services $timers
    echo "enabled and started: $services $timers"
else
    echo "start with: deploy/install.sh --enable"
fi
