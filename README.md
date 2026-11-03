# Ditto for Linux

A Linux rewrite of [Ditto](https://github.com/sabrogden/Ditto), the Windows clipboard manager, in Python + PyQt6.
It saves everything you copy (text, HTML, images, file lists) and lets you search and paste any of it later.

It uses **the same SQLite schema as Windows Ditto** (`Main`, `Data`, `Types`, `CopyBuffers`, `MainDeletes`,
same triggers and format names such as `CF_UNICODETEXT`, `HTML Format`, `PNG`), so a `Ditto.db` from Windows
can be opened here. Older databases are upgraded in place.

## Install

**Ubuntu / Debian (recommended):**

```bash
sudo apt install ./ditto-linux_1.4.1_all.deb
```

It starts automatically at your next login, or right away from the app menu ("Ditto"). On GNOME the first
start registers **Ctrl+`** to open Quick Paste (unless that key is already taken). `ydotool` is installed as a
recommended package for auto-paste.

**Other distributions:** unpack `ditto-linux-1.4.1.tar.gz` and run `./install.sh --shortcut`.

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

### Updating from 1.1.0 or older

Versions before 1.2.0 cannot update themselves. On those systems run:

```
curl -fsSL https://raw.githubusercontent.com/IW0127/ditto-linux/main/scripts/update-ditto.sh | bash
```

It downloads the latest release, checks its checksum, installs it (with `sudo apt` on Ubuntu/Debian, per-user
elsewhere), keeps your history and settings and restarts Ditto. From then on Ditto updates itself.

## Usage

Copy things as usual. Press Ctrl+` (or click the tray icon, or run `ditto-linux show`) to open Quick Paste.

| Key | Action |
|---|---|
| **Ctrl+`** (global) | open Quick Paste |
| **hold Ctrl, press ` again** | move to the next clip (press again to go further); **release Ctrl to paste it** |
| type | search (all words must match; `"quoted phrase"` supported) |
| Enter / Double-click | Paste selected clip into the previous window |
| Shift+Enter | Paste as plain text |
| Ctrl/Shift+click, then Enter | Paste several clips at once (one per line, in list order) |
| Ctrl+Shift+V | Special paste: UPPERCASE, lowercase, Capitalize, Sentence case, trim, one line, slugify… |
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

**Paste several clips:** select them with Ctrl+click or Shift+arrows and press Enter; they are pasted as one
text, separated by a new line (or space / comma / tab / nothing, in Options).

**Special paste** (Ctrl+Shift+V or right-click → Special Paste) changes the text before pasting:
UPPERCASE, lowercase, Capitalize Each Word, Sentence case, iNVERT cASE, trim spaces, remove line breaks,
remove extra spaces and blank lines, slugify. It also works on several selected clips. From the command line:
`ditto-linux paste 12 15 --transform upper`.

**Ignore apps:** Options → *Don't save copies from* (e.g. `keepassxc, bitwarden`, `*` wildcards). Ditto
identifies the app that copied for X11 apps and apps running through XWayland (many Electron apps); native
Wayland apps cannot be identified and are always saved. Each clip's *Properties* show the app it came from,
and Options lists recently seen apps.

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

### Auto-paste not working?

Run `ditto-linux doctor`: the *auto-paste* section shows what is missing. To fix it in one go:

```
curl -fsSL https://raw.githubusercontent.com/IW0127/ditto-linux/main/scripts/fix-autopaste.sh | bash
```

It updates Ditto, installs `ydotool` (plus `xdotool` on X11), loads the `uinput` module and adds a udev rule
(`TAG+="uaccess"`) that lets the logged-in desktop user use `/dev/uinput`, then restarts Ditto. Check with
`ditto-linux paste-test`. For terminals set the paste keystroke to `ctrl+shift+v` in Options. If auto-paste
fails, Ditto now says so in a notification; the clip is still on the clipboard, so Ctrl+V works.

### Command line

```
ditto-linux list [-n 20] [-s search] [-g GROUP] [--json]
ditto-linux get ID [-f text/html|image/png|...]     # raw data to stdout
ditto-linux add [TEXT]                              # or from stdin
ditto-linux copy ID... [--plain] [-t TRANSFORM]  /  paste ID... [--plain] [-t TRANSFORM]
ditto-linux delete ID...  |  clear  |  cleanup [--max N] [--days N] [--vacuum]
ditto-linux group create NAME | list | move GROUP ID...
ditto-linux pin ID [--title T] [--hide] [--up N] [--off]   # pin, title, hide content, reorder
ditto-linux sticky ID [--off]  |  never-delete ID [--off]  |  mask ID [--off]
ditto-linux export [FILE]  |  import FILE  |  stats
ditto-linux show | toggle | hotkey | hide | pause | resume | status | restart | quit
ditto-linux update [--check] [-y]  |  shortcut [--fix] [--take-over] [--set KEY]  |  logs [-f]  |  doctor
```

## Updates, shortcut check and logs

**Updates.** Once a day Ditto checks the [releases](https://github.com/IW0127/ditto-linux/releases). When a
new version is out it shows a popup (*Install and restart* / *Skip this version* / *Later*). For the `.deb` it
downloads the package, verifies it against `SHA256SUMS`, installs it with `pkexec apt-get` (you are asked for
your password) and restarts itself. A per-user (tarball) install is updated with the bundled `install.sh`; a git
checkout is only notified. Turn it off in Options, or check by hand from the tray / ☰ menu or with
`ditto-linux update` (`--check` to only look).

**Shortcut check.** Every time Ditto starts it checks its global shortcut (GNOME):
a broken entry (e.g. pointing at an uninstalled launcher) is repaired, a key you changed in GNOME Settings is
kept, and if the key is also used by another shortcut, a clipboard extension or CopyQ, a popup offers to
**take the key over** (removes it from the other shortcut) or **use another key**. From a terminal:

```
ditto-linux shortcut                 # report: ok / missing / broken / conflict
ditto-linux shortcut --fix           # repair Ditto's entry
ditto-linux shortcut --take-over     # also remove the key from conflicting shortcuts
ditto-linux shortcut --set 'Ctrl+Alt+V'
```

**Logs.** Ditto logs to `~/.local/state/ditto-linux/ditto.log` (rotated at 1 MB, 3 old copies; clip
contents are never logged, only ids): start/stop, shortcut checks and fixes, updates, pastes and the Ctrl+`
cycling, Qt warnings and any crash with its traceback. `ditto-linux logs` (`-f` to follow, `--path`),
*Open Log File* in the tray menu, and `ditto-linux doctor` for a summary of the setup.

**Problem reports.** When Ditto hits an error (an exception, or a hard crash found on the next start) it
can file a GitHub issue: setting *Report problems on GitHub* = **Ask** (default: a popup first), **Always**
(no popup) or **Never**. It uses the GitHub CLI (`gh auth login`); without it the pre-filled issue page opens
in the browser. The same problem is filed once (repeats add at most one comment a day; if the issue was closed
a new one is opened). Reports contain the error, system info and recent log lines with your user name and home
folder hidden, never clipboard contents. Report something by hand with
`ditto-linux report -t "title" -m "what happened"` (`--dry-run` shows the report).

## Releasing

`scripts/release.sh 1.3.0 "what changed"` bumps the version, runs the tests, commits, tags and pushes.
GitHub Actions then tests again, builds the `.deb` / `.tar.gz` and publishes the release, and installed
copies offer the update within a day. Every push to `main` also runs the tests (CI).

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

There are 161 tests:

- **Unit tests:** the DB schema, Windows format conversion, search, groups, auto-delete, import/export and legacy-DB upgrade.
- **CLI tests:** every command and its error handling.
- **UI tests:** real key events sent to the Quick Paste window on Qt's offscreen platform.
- **End-to-end tests:** these start a throw-away X server (rootless `Xwayland`, or `Xvfb`) and run the real daemon on it. Separate helper processes act as other apps and copy text, HTML, images and password-manager secrets. The tests then check capture, de-duplication, paste back to the clipboard (rich and plain), pause/resume, the window, single-instance behaviour, and that history survives a restart. Your own clipboard and history are never touched.

## Scope compared with Windows Ditto

Included: history, search, groups, pinned clips with titles and hidden content, descriptions, editing,
plain-text paste, pasting several clips at once, special paste (text transforms), ignoring apps, quick paste
with Ctrl+number and Ctrl+` cycling, image thumbnails and preview, comparing clips, auto-delete by count or
age, pause, import/export, and the same database format. Extra: auto-update, shortcut check and repair,
automatic problem reports and logs.

Not ported yet: network sync between computers ("friends"), per-clip global hotkeys, multiple copy buffers,
themes / font / remembered window position, drag and drop, saving a clip to a file or QR code, regex search,
ChaiScript add-ins, RTF clips, and automatic Ctrl+` setup on desktops other than GNOME.
