# Ditto for Linux

A Linux rewrite of [Ditto](https://github.com/sabrogden/Ditto), the Windows clipboard manager, in Python + PyQt6.
It saves everything you copy (text, HTML, images, file lists) and lets you search and paste any of it later.

It uses **the same SQLite schema as Windows Ditto** (`Main`, `Data`, `Types`, `CopyBuffers`, `MainDeletes`,
same triggers and format names such as `CF_UNICODETEXT`, `HTML Format`, `PNG`), so a `Ditto.db` from Windows
can be opened here. Older databases are upgraded in place.

## Install

**Ubuntu / Debian (recommended):**

```bash
sudo apt install ./ditto-linux_1.1.0_all.deb
```

It starts automatically at your next login, or right away from the app menu ("Ditto"). On GNOME the first
start registers **Ctrl+`** to open Quick Paste (unless that key is already taken). `ydotool` is installed as a
recommended package for auto-paste.

**Other distributions:** unpack `ditto-linux-1.1.0.tar.gz` and run `./install.sh --shortcut`.

**Building the release files:** `scripts/build-release.sh` writes the `.deb`, the tarball and `SHA256SUMS` to `dist/`.

### From a source checkout

```bash
sudo apt install python3-pyqt6        # or: pip install --user PyQt6
scripts/install.sh                    # launcher in ~/.local/bin, menu entry, autostart
scripts/install.sh --shortcut         # also bind Ctrl+` to the Quick Paste window (GNOME)
ditto-linux daemon &                  # start now
```

Or run it from the checkout: `python3 -m ditto_linux daemon`. Remove it with `scripts/install.sh --uninstall`
(your history is kept).

## Usage

Copy things as usual. Press Ctrl+` (or click the tray icon, or run `ditto-linux show`) to open Quick Paste.

| Key | Action |
|---|---|
| **Ctrl+`** (global) | open Quick Paste |
| **hold Ctrl, press ` again** | move to the next clip (press again to go further); **release Ctrl to paste it** |
| type | search (all words must match; `"quoted phrase"` supported) |
| Enter / Double-click | Paste selected clip into the previous window |
| Shift+Enter | Paste as plain text |
| Ctrl+1 … Ctrl+0 (also numpad) | Paste clip at position 1–10 |
| Ctrl+C | Copy selected clip to the clipboard (no paste) |
| Esc | Clear search → leave group → close |
| Shift+Esc | Close window |
| Alt+C | Cancel search filter |
| F3 | Show / hide description (preview) |
| N / P | Next / previous clip while the description is shown |
| W | Toggle word wrap in the description |
| Ctrl+N | New clip |
| Ctrl+E | Edit clip |
| Alt+Enter | Clip properties |
| Ctrl+F2 | Compare two selected clips |
| Ctrl+F7 | New group |
| F7 | New group from selected clips |
| Ctrl+G | Show groups only |
| Ctrl+Space | Keep window open when it loses focus |
| F5 | Refresh |
| Delete | Delete selected clips |
| F2 | Set title (description) / rename group |
| Ctrl+M | Move selected clips to a group |
| Ctrl+S | Pin / unpin (always first) |
| Ctrl+P | Pin with a title, optionally hiding the content |
| Ctrl+H | Hide / show content (passwords: the list shows only the title) |
| Ctrl+Up / Ctrl+Down | Move a pinned clip up / down |
| Ctrl+Shift+Up | Move a pinned clip to the top |
| Ctrl+D | Toggle never auto delete |
| Backspace (empty search) | Back to parent group |
| Right-click | Menu |

These match Windows Ditto's defaults; Ctrl+M, Ctrl+S, Ctrl+D, Ctrl+P, Ctrl+H, Ctrl+Up/Down and F2 are extras. Like Ditto, the window hides when it loses focus unless kept open with Ctrl+Space.

**Quick paste of the previous clip:** hold Ctrl, tap ` twice, release Ctrl: the clip before the current one is
pasted. Each extra tap of ` moves one clip further down.

**Pins:** pinned clips stay at the top, in the order you choose (Ctrl+Up/Down), so Ctrl+1, Ctrl+2… always paste
the same ones. Give a pin a title with Ctrl+P (or F2). For passwords and tokens tick *Hide content*: the list,
tooltip and preview show only the title (🔒), searching matches only the title, and Enter still pastes the
real content.

The right-click menu also has **Pin**, **Pin with Title**, **Hide Content**, **Move Pin Up/Down/To Top** and
**Never Auto Delete** (🛡). The tray menu has
*Disconnect from Clipboard* (pause), *Options* and *Delete Clipboard History*.

### Pasting into the previous window

Choosing a clip puts it on the clipboard, hides the window and sends the paste keystroke to the window
you were in, just like Ditto. It uses `wtype` (wlroots), `ydotool` (any session, including GNOME/KDE Wayland) or
`xdotool` (X11), whichever works first. For `ydotool`, Ditto starts its own private `ydotoold` helper and stops it
on exit; this needs read/write access to `/dev/uinput` (usually the `input` group). The keystroke (`ctrl+v`,
`ctrl+shift+v` for terminals, `shift+insert`) and the delay are set in Options.

### Command line

```
ditto-linux list [-n 20] [-s search] [-g GROUP] [--json]
ditto-linux get ID [-f text/html|image/png|...]     # raw data to stdout
ditto-linux add [TEXT]                              # or from stdin
ditto-linux copy ID [--plain]  /  paste ID [--plain]
ditto-linux delete ID...  |  clear  |  cleanup [--max N] [--days N] [--vacuum]
ditto-linux group create NAME | list | move GROUP ID...
ditto-linux pin ID [--title T] [--hide] [--up N] [--off]   # pin, title, hide content, reorder
ditto-linux sticky ID [--off]  |  never-delete ID [--off]  |  mask ID [--off]
ditto-linux export [FILE]  |  import FILE  |  stats
ditto-linux show | toggle | hotkey | hide | pause | resume | status | quit
```

## Wayland notes

Under Wayland, only the focused window may read the clipboard, and GNOME/KDE do not implement the
data-control protocol that clipboard managers need. So in a Wayland session the daemon runs through
**XWayland** (`QT_QPA_PLATFORM=xcb`): the compositor mirrors the clipboard to X11, where Qt is notified of
every change. Global hotkeys can't be grabbed under Wayland, so bind `ditto-linux hotkey` in your desktop's
keyboard settings to Ctrl+` (Ditto does this itself on GNOME). `hotkey` opens the window, or, when pressed again
while Ctrl is still held, moves to the next clip and pastes it when Ctrl is released.

Clips marked by password managers (`x-kde-passwordManagerHint: secret`) are never saved.

## Files

| What | Where (override) |
|---|---|
| Database | `~/.local/share/ditto-linux/Ditto.db` (`--db` or `DITTO_DB`) |
| Settings | `~/.config/ditto-linux/settings.json` (`DITTO_CONFIG`) |
| Control socket | `$XDG_RUNTIME_DIR/ditto-linux.sock` (`DITTO_SOCKET`) |

## Tests

```bash
./run_tests.sh
```

There are 63 tests:

- **Unit tests:** the DB schema, Windows format conversion, search, groups, auto-delete, import/export and legacy-DB upgrade.
- **CLI tests:** every command and its error handling.
- **UI tests:** real key events sent to the Quick Paste window on Qt's offscreen platform.
- **End-to-end tests:** these start a throw-away X server (rootless `Xwayland`, or `Xvfb`) and run the real daemon on it. Separate helper processes act as other apps and copy text, HTML, images and password-manager secrets. The tests then check capture, de-duplication, paste back to the clipboard (rich and plain), pause/resume, the window, single-instance behaviour, and that history survives a restart. Your own clipboard and history are never touched.

## Scope compared with Windows Ditto

Included: history, search, groups, sticky clips, descriptions, editing, plain-text paste, quick paste with
Ctrl+number, image thumbnails and preview, auto-delete by count or age, pause, import/export, and the same database format.

Not ported (Windows-specific or out of scope for now): network sync between computers ("friends"), ChaiScript
add-ins, multiple copy buffers, per-clip global hotkeys, themes, and Windows-only clipboard formats (RTF, CF_HDROP).
