#!/usr/bin/env bash
# Make Ditto's auto-paste work on this system.
#
#   curl -fsSL https://raw.githubusercontent.com/IW0127/ditto-linux/main/scripts/fix-autopaste.sh | bash
#
# What it does (asks for sudo once):
#   1. updates Ditto to the latest release (older versions lack fixes for some systems)
#   2. installs the keystroke tool: ydotool (any session) and xdotool on X11
#   3. lets the logged-in user use /dev/uinput (needed by ydotool): loads the uinput module now and at
#      boot, and adds a udev rule giving the active desktop user access (no re-login needed)
#   4. restarts Ditto and shows the auto-paste check
set -euo pipefail

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
[[ $EUID -eq 0 ]] && die "run this as your normal user (it asks for sudo when needed)"

RAW="https://raw.githubusercontent.com/IW0127/ditto-linux/main/scripts"
SESSION="${XDG_SESSION_TYPE:-}"
[[ -z "$SESSION" ]] && { [[ -n "${WAYLAND_DISPLAY:-}" ]] && SESSION=wayland || SESSION=x11; }
say "Session: $SESSION (${XDG_CURRENT_DESKTOP:-unknown desktop})"

# ------------------------------------------------------------------ 1. latest Ditto
if command -v curl >/dev/null; then
    say "Checking for a newer Ditto"
    curl -fsSL "$RAW/update-ditto.sh" | bash || warn "could not update Ditto (continuing)"
    hash -r
fi
command -v ditto-linux >/dev/null || die "Ditto is not installed"

# ------------------------------------------------------------------ 2. keystroke tools
pkgs=(ydotool)
[[ "$SESSION" == x11 ]] && pkgs+=(xdotool)
command -v setfacl >/dev/null || pkgs+=(acl)
missing=()
for p in "${pkgs[@]}"; do
    case $p in acl) command -v setfacl >/dev/null || missing+=(acl) ;; *) command -v "$p" >/dev/null || missing+=("$p") ;; esac
done
if ((${#missing[@]})); then
    say "Installing: ${missing[*]}"
    if command -v apt-get >/dev/null; then sudo apt-get install -y "${missing[@]}"
    elif command -v dnf >/dev/null; then sudo dnf install -y "${missing[@]}"
    elif command -v pacman >/dev/null; then sudo pacman -S --needed --noconfirm "${missing[@]}"
    elif command -v zypper >/dev/null; then sudo zypper install -y "${missing[@]}"
    else die "install these with your package manager: ${missing[*]}"; fi
else
    say "Keystroke tools already installed: ${pkgs[*]/acl/}"
fi

# ------------------------------------------------------------------ 3. /dev/uinput access
RULE=/etc/udev/rules.d/60-ditto-uinput.rules
if [[ ! -f $RULE ]]; then
    say "Allowing the desktop user to use /dev/uinput ($RULE)"
    printf '%s\n' '# Ditto for Linux: let the logged-in desktop user send key presses (ydotool / auto-paste)' \
        'KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"' \
        | sudo tee "$RULE" >/dev/null
fi
echo uinput | sudo tee /etc/modules-load.d/ditto-uinput.conf >/dev/null
sudo modprobe uinput 2>/dev/null || true
sudo udevadm control --reload-rules 2>/dev/null || true
sudo udevadm trigger --subsystem-match=misc --sysname-match=uinput 2>/dev/null || true
if [[ ! -w /dev/uinput ]] && command -v setfacl >/dev/null; then
    sudo setfacl -m "u:$USER:rw" /dev/uinput          # immediate access for this boot
fi
if [[ -w /dev/uinput ]]; then
    say "/dev/uinput is writable"
else
    warn "/dev/uinput is still not writable; adding you to the 'input' group (log out and in afterwards)"
    sudo usermod -aG input "$USER"
fi

# ------------------------------------------------------------------ 4. restart + check
if ditto-linux status >/dev/null 2>&1; then
    say "Restarting Ditto"
    ditto-linux restart >/dev/null 2>&1 || true
    for _ in $(seq 30); do sleep 0.3; ditto-linux status >/dev/null 2>&1 && break; done
fi
echo
ditto-linux doctor 2>/dev/null | sed -n '/^auto-paste:/,/^shortcut:/p' | sed '$d' || true
echo
say "Done. Try it: press Ctrl+\` in a text editor and choose a clip with Enter."
say "Or run:  ditto-linux paste-test"
say "Pasting into a terminal? Set the paste keystroke to ctrl+shift+v in Ditto's Options."
