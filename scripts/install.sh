#!/usr/bin/env bash
# Install ditto-linux for the current user (no root needed).
#   scripts/install.sh              install launcher, desktop entry, autostart
#   scripts/install.sh --shortcut   additionally bind Ctrl+` to "ditto-linux hotkey" (GNOME)
#   scripts/install.sh --uninstall  remove everything this script installed (keeps your history)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="$HOME/.local/bin/ditto-linux"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications/ditto-linux.desktop"
AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/ditto-linux.desktop"
KEY_PATH="/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/ditto-linux/"

if [[ "${1:-}" == "--uninstall" ]]; then
    ditto-linux quit 2>/dev/null || true
    rm -f "$BIN" "$APPS" "$AUTOSTART"
    if command -v gsettings >/dev/null; then
        current=$(gsettings get org.gnome.settings-daemon.plugins.media-keys custom-keybindings)
        new=$(python3 -c "import ast,sys; l=ast.literal_eval(sys.argv[1].replace('@as ','')); print([x for x in l if x!=sys.argv[2]])" "$current" "$KEY_PATH")
        gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings "$new"
        # let the next Ditto start (e.g. from the .deb) register Ctrl+` again
        python3 - "${XDG_CONFIG_HOME:-$HOME/.config}/ditto-linux/settings.json" <<'PY' || true
import json, sys
p = sys.argv[1]
try:
    s = json.load(open(p))
except (OSError, ValueError):
    sys.exit(0)
s["gnome_shortcut_done"] = False
json.dump(s, open(p, "w"), indent=2)
PY
    fi
    echo "Uninstalled. History kept in ${XDG_DATA_HOME:-$HOME/.local/share}/ditto-linux/"
    exit 0
fi

python3 -c "import PyQt6.QtWidgets" 2>/dev/null || {
    echo "PyQt6 is required: sudo apt install python3-pyqt6   (or: pip install --user PyQt6)" >&2
    exit 1
}

mkdir -p "$(dirname "$BIN")" "$(dirname "$APPS")" "$(dirname "$AUTOSTART")"
cat > "$BIN" <<EOF
#!/usr/bin/env bash
exec python3 -c 'import sys; sys.path.insert(0, sys.argv.pop(1)); from ditto_linux.cli import main; sys.exit(main())' "$ROOT" "\$@"
EOF
chmod +x "$BIN"
sed "s#Exec=ditto-linux#Exec=$BIN#" "$ROOT/data/ditto-linux.desktop" > "$APPS"
cp "$APPS" "$AUTOSTART"
echo "Installed $BIN, desktop entry and autostart entry."

if [[ "${1:-}" == "--shortcut" ]] && command -v gsettings >/dev/null; then
    current=$(gsettings get org.gnome.settings-daemon.plugins.media-keys custom-keybindings)
    new=$(python3 -c "import ast,sys; l=ast.literal_eval(sys.argv[1].replace('@as ','')); l+=[sys.argv[2]] if sys.argv[2] not in l else []; print(l)" "$current" "$KEY_PATH")
    gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings "$new"
    schema="org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$KEY_PATH"
    gsettings set "$schema" name "Ditto"
    gsettings set "$schema" command "$BIN hotkey"
    gsettings set "$schema" binding "<Control>grave"
    echo "Bound Ctrl+\` to '$BIN hotkey'."
fi
echo "Start now with: ditto-linux daemon &"
