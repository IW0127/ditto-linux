#!/usr/bin/env bash
# Build release artifacts into dist/:
#   ditto-linux_<ver>_all.deb   Debian/Ubuntu package
#   ditto-linux-<ver>.tar.gz    portable archive (any distro; run ./install.sh inside)
#   SHA256SUMS
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
VER=$(python3 -c "import ditto_linux; print(ditto_linux.__version__)")
DIST="$ROOT/dist"
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT
rm -rf "$DIST"
mkdir -p "$DIST"

copy_package() {  # copy the python package without caches
    mkdir -p "$1"
    (cd "$ROOT" && find ditto_linux -name '*.py' -print0 | xargs -0 -I{} install -D -m 0644 {} "$1/{}")
}

# ---------------------------------------------------------------- .deb
PKG="$BUILD/deb"
copy_package "$PKG/usr/lib/ditto-linux"
install -d "$PKG/usr/bin"
cat > "$PKG/usr/bin/ditto-linux" <<'EOF'
#!/bin/sh
# Ditto for Linux launcher
# (not "python3 -m": that would import a ditto_linux folder from the current directory first)
exec /usr/bin/python3 -c 'import sys; sys.path.insert(0, sys.argv.pop(1)); from ditto_linux.cli import main; sys.exit(main())' /usr/lib/ditto-linux "$@"
EOF
chmod 0755 "$PKG/usr/bin/ditto-linux"
install -D -m 0644 data/ditto-linux.desktop "$PKG/usr/share/applications/ditto-linux.desktop"
install -D -m 0644 data/ditto-linux.desktop "$PKG/etc/xdg/autostart/ditto-linux.desktop"
install -D -m 0644 data/ditto-linux.svg "$PKG/usr/share/icons/hicolor/scalable/apps/ditto-linux.svg"
install -D -m 0644 README.md "$PKG/usr/share/doc/ditto-linux/README.md"
cat > "$PKG/usr/share/doc/ditto-linux/copyright" <<EOF
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: ditto-linux
Comment: Linux rewrite of Ditto (https://github.com/sabrogden/Ditto)

Files: *
License: GPL-3.0-or-later
 On Debian systems the full text is in /usr/share/common-licenses/GPL-3.
EOF
chmod 0644 "$PKG/usr/share/doc/ditto-linux/copyright"
SIZE=$(du -sk "$PKG" | cut -f1)
install -d "$PKG/DEBIAN"
cat > "$PKG/DEBIAN/control" <<EOF
Package: ditto-linux
Version: $VER
Section: utils
Priority: optional
Architecture: all
Depends: python3 (>= 3.10), python3-pyqt6
Recommends: ydotool
Suggests: wtype, xdotool
Installed-Size: $SIZE
Maintainer: Ditto for Linux <noreply@localhost>
Homepage: https://github.com/sabrogden/Ditto
Description: clipboard manager compatible with Windows Ditto
 Saves everything copied to the clipboard (text, HTML, images, file lists)
 and lets you search and paste it later from a Quick Paste window (Ctrl+grave).
 Uses the same SQLite database format as Ditto for Windows, supports groups,
 sticky clips, plain-text paste, auto-paste into the previous window and
 Ditto's keyboard shortcuts. Works on X11 and on Wayland (through XWayland).
EOF
cat > "$PKG/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "configure" ]; then
    command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q /usr/share/icons/hicolor || true
    command -v py3compile >/dev/null && py3compile -q /usr/lib/ditto-linux || true
    # restart running instances so they use the new version (their history is untouched)
    for sock in /run/user/*/ditto-linux.sock; do
        [ -S "$sock" ] || continue
        uid=$(stat -c %u "$sock"); user=$(stat -c %U "$sock")
        timeout 5 runuser -u "$user" -- env XDG_RUNTIME_DIR="/run/user/$uid" /usr/bin/ditto-linux restart \
            >/dev/null 2>&1 || true
    done
fi
exit 0
EOF
cat > "$PKG/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
find /usr/lib/ditto-linux -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
exit 0
EOF
chmod 0755 "$PKG/DEBIAN/postinst" "$PKG/DEBIAN/prerm"
echo "/etc/xdg/autostart/ditto-linux.desktop" > "$PKG/DEBIAN/conffiles"
fakeroot dpkg-deb --build -Zxz "$PKG" "$DIST/ditto-linux_${VER}_all.deb" >/dev/null

# ---------------------------------------------------------------- portable tarball
TAR="$BUILD/ditto-linux-$VER"
copy_package "$TAR"
cp -r data scripts README.md LICENSE pyproject.toml "$TAR/"
rm -f "$TAR/scripts/build-release.sh"
cat > "$TAR/install.sh" <<'EOF'
#!/usr/bin/env bash
# Portable install for the current user: see scripts/install.sh --help
exec "$(dirname "$0")/scripts/install.sh" "$@"
EOF
chmod +x "$TAR/install.sh" "$TAR/scripts/install.sh"
tar -C "$BUILD" --owner=0 --group=0 -czf "$DIST/ditto-linux-$VER.tar.gz" "ditto-linux-$VER"

(cd "$DIST" && sha256sum ./*.deb ./*.tar.gz > SHA256SUMS)
ls -l "$DIST"
