#!/usr/bin/env bash
# Update Ditto for Linux to the latest release (for copies older than 1.2.0, which cannot update
# themselves; newer versions update automatically).
#
#   curl -fsSL https://raw.githubusercontent.com/IW0127/ditto-linux/main/scripts/update-ditto.sh | bash
#   ./update-ditto.sh [--force]     # --force reinstalls even if already up to date
#
# Your clip history, pins and settings are kept.
set -euo pipefail

REPO="IW0127/ditto-linux"
FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
fetch() {  # fetch URL [OUTFILE]
    if command -v curl >/dev/null; then curl -fsSL --retry 3 ${2:+-o "$2"} "$1"
    elif command -v wget >/dev/null; then
        if [[ -n "${2:-}" ]]; then wget -qO "$2" "$1"; else wget -qO- "$1"; fi
    else die "install curl or wget first"; fi
}

command -v python3 >/dev/null || die "python3 is required"
[[ $EUID -eq 0 ]] && die "run this as your normal user (it asks for sudo when needed)"

# ------------------------------------------------------------------ what is installed / latest
CURRENT=$(ditto-linux --version 2>/dev/null | awk '{print $2}' || true)
say "Installed: ${CURRENT:-none}"

JSON=$(fetch "https://api.github.com/repos/$REPO/releases/latest") || die "cannot reach GitHub"
read -r TAG DEB_URL TAR_URL SUMS_URL < <(python3 -c '
import json, sys
r = json.load(sys.stdin)
a = {x["name"]: x["browser_download_url"] for x in r.get("assets", [])}
pick = lambda end: next((u for n, u in a.items() if n.endswith(end)), "-")
print(r["tag_name"], pick(".deb"), pick(".tar.gz"), a.get("SHA256SUMS", "-"))' <<<"$JSON")
LATEST=${TAG#v}
say "Latest:    $LATEST"

newer=$(python3 -c 'import sys
v = lambda s: tuple(int(x) for x in s.split(".") if x.isdigit())
print(int(v(sys.argv[1]) > v(sys.argv[2] or "0")))' "$LATEST" "${CURRENT:-0}")
if [[ "$newer" != 1 && $FORCE -eq 0 ]]; then
    say "Ditto is already up to date."
    exit 0
fi

# ------------------------------------------------------------------ download + verify
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
if command -v apt-get >/dev/null && command -v dpkg >/dev/null; then KIND=deb; URL=$DEB_URL
else KIND=tarball; URL=$TAR_URL; fi
[[ "$URL" != "-" && "$SUMS_URL" != "-" ]] || die "release $TAG has no $KIND file or checksums"
FILE="$TMP/$(basename "$URL")"
say "Downloading $(basename "$URL")"
fetch "$URL" "$FILE"
fetch "$SUMS_URL" "$TMP/SHA256SUMS"
WANT=$(awk -v n="$(basename "$FILE")" '{f=$2; sub(/^\*?\.\//, "", f)} f==n {print $1}' "$TMP/SHA256SUMS")
GOT=$(sha256sum "$FILE" | awk '{print $1}')
[[ -n "$WANT" && "$WANT" == "$GOT" ]] || die "checksum mismatch for $(basename "$FILE"); not installing"
say "Checksum OK"

# ------------------------------------------------------------------ stop the running copy
WAS_RUNNING=0
if ditto-linux status >/dev/null 2>&1; then
    WAS_RUNNING=1
    say "Stopping the running Ditto"
    ditto-linux quit >/dev/null 2>&1 || true
    for _ in $(seq 20); do ditto-linux status >/dev/null 2>&1 || break; sleep 0.3; done
fi

# ------------------------------------------------------------------ install
CONF="${XDG_CONFIG_HOME:-$HOME/.config}/ditto-linux/settings.json"
if [[ $KIND == deb ]]; then
    # an old per-user launcher in ~/.local/bin would hide the system package: remove it (history stays)
    USER_BIN="$HOME/.local/bin/ditto-linux"
    if [[ -e "$USER_BIN" ]]; then
        say "Removing the old per-user launcher ($USER_BIN)"
        rm -f "$USER_BIN" "${XDG_DATA_HOME:-$HOME/.local/share}/applications/ditto-linux.desktop" \
              "${XDG_CONFIG_HOME:-$HOME/.config}/autostart/ditto-linux.desktop"
    fi
    say "Installing (sudo may ask for your password)"
    sudo apt-get install -y "$FILE"
    hash -r
else
    DEST="${XDG_DATA_HOME:-$HOME/.local/share}/ditto-linux/app"
    mkdir -p "$DEST"
    tar -xzf "$FILE" -C "$DEST"
    say "Installing for this user"
    bash "$DEST/ditto-linux-$LATEST/install.sh"
    export PATH="$HOME/.local/bin:$PATH"
    hash -r
fi

# let the new version re-check and repair the Ctrl+` shortcut on its first start
if [[ -f "$CONF" ]]; then
    python3 - "$CONF" <<'PY' || true
import json, sys
p = sys.argv[1]
s = json.load(open(p))
s["gnome_shortcut_done"] = False
json.dump(s, open(p, "w"), indent=2)
PY
fi

NEW=$(ditto-linux --version 2>/dev/null | awk '{print $2}')
[[ "$NEW" == "$LATEST" ]] || die "installed, but 'ditto-linux --version' reports '${NEW:-nothing}' (check your PATH)"
say "Installed Ditto $NEW"

# ------------------------------------------------------------------ start it again
if [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
    if [[ $WAS_RUNNING -eq 1 ]] || ! ditto-linux status >/dev/null 2>&1; then
        say "Starting Ditto"
        if command -v systemd-run >/dev/null && systemctl --user show-environment >/dev/null 2>&1; then
            envs=()
            for v in DISPLAY WAYLAND_DISPLAY XAUTHORITY XDG_RUNTIME_DIR XDG_CURRENT_DESKTOP XDG_SESSION_TYPE \
                     DBUS_SESSION_BUS_ADDRESS; do
                [[ -n "${!v:-}" ]] && envs+=("--setenv=$v=${!v}")
            done
            systemd-run --user --collect --quiet --unit="ditto-linux-$(date +%s)" "${envs[@]}" \
                "$(command -v ditto-linux)" daemon || nohup setsid ditto-linux daemon >/dev/null 2>&1 &
        else
            nohup setsid ditto-linux daemon >/dev/null 2>&1 &
        fi
        for _ in $(seq 30); do ditto-linux status >/dev/null 2>&1 && break; sleep 0.3; done
    fi
else
    say "No graphical session here: Ditto starts at your next login."
fi

echo
ditto-linux doctor 2>/dev/null || true
echo
say "Done. From now on Ditto updates itself (tray menu -> Check for Updates…)."
