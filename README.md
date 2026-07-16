# skreenshot

A Linux clone of the Windows Shift+Win+S snipping flow, reduced to its core:
press a hotkey, the screen freezes and dims, drag a rectangle, and on release
the region is on the clipboard as a PNG (hold Shift while releasing to also save
it to a file). Esc cancels. Nothing else: no editor, no annotations, no tray icon.

Works on X11 (XFCE, KDE Plasma) and on Wayland (KDE Plasma, GNOME, and
wlroots compositors such as Sway). The session type is detected
automatically; on Wayland the screen is captured through the standard
xdg-desktop-portal, so there is no black-capture failure mode.

## Requirements

- Python 3 with `venv`. `make deps` creates a local `.venv` and pip-installs
  PyQt6 and PyYAML into it; `make deps-dev` adds the dev/test tools (pytest, ruff).
- An X11 or Wayland session.
- On Wayland: `xdg-desktop-portal` plus your desktop's portal backend
  (`-kde`, `-gnome`, `-wlr`, ...). Desktop distributions install these by
  default. On a compositor without a clipboard manager, installing
  `wl-clipboard` is recommended (see Wayland notes below).
- For the end-to-end tests only: the system tools `Xvfb`, `xdotool`, `xclip`
  and ImageMagick (X11 suite), plus `kwin_wayland`, `dbus-daemon` and
  `xwininfo` (Wayland suite), from your OS package manager (pip can't
  provide these).

## Install

```
git clone <this repo>
cd skreenshot
make deps             # create .venv and pip-install PyQt6
make install          # symlinks ~/.local/bin/skreenshot, installs icons + .desktop
make install-hotkey   # binds Shift+Super+S (XFCE or KDE, autodetected)
```

`make uninstall` and `make uninstall-hotkey` reverse both. `make help`
lists every target. Make sure `~/.local/bin` is on your PATH.

The installed `~/.local/bin/skreenshot` is a symlink back to this checkout, and
the launcher re-execs itself under the checkout's `.venv`, so the standalone
command and the hotkey use the same deps `make deps` installed. Keep the
checkout (and its `.venv`) in place.

## Running

One command does the whole flow:

```
skreenshot
```

(or `make run` from the checkout). Drag with the left button; release
copies the selection to the clipboard and the overlay disappears
immediately. Hold **Shift** while releasing to also open a Save-As dialog and
write the PNG to a file (name pre-filled `screenshot-YYYY-MM-DD-HHhMMm.png`, in
the folder from `save_dir`). Cancel with Esc, a right-click, or a click without
a drag; cancel never touches the clipboard.

Exit codes: 0 image copied, 2 cancelled, 1 error. On X11 a detached helper
process may briefly outlive the command; it serves the clipboard until
another application takes the selection over, then exits. That is what
makes the paste survive on sessions without a clipboard manager.

## Wayland notes

- The full-screen grab goes through the `org.freedesktop.portal.Screenshot`
  D-Bus interface. Depending on the desktop, the first run may show a
  one-time "allow this app to take screenshots?" dialog (GNOME, and KDE
  Plasma 6.4 or newer); the answer is remembered. Plasma up to 6.3 captures
  without asking. GNOME plays its screenshot flash/shutter feedback on every
  capture; that comes from GNOME's portal, not from skreenshot.
- Selecting across several monitors in one drag works; start the drag on any
  screen and release on another.
- Clipboard persistence after the command exits is provided by the desktop:
  Klipper on Plasma, GNOME's built-in clipboard manager. On compositors
  without one (e.g. a bare Sway), skreenshot hands the clipboard to
  `wl-copy` when it is installed; otherwise it stays in the foreground
  serving the clipboard until some other application takes the selection
  over, and says so on stderr. Install `wl-clipboard` if you want the
  command to return immediately on such setups.
- If the overlay comes up without keyboard focus (some desktops restrict
  focus stealing), Esc has no effect at first; keyboard focus transfers
  when you press a mouse button, so Esc works once a drag is in progress.
  To cancel without keyboard focus, right-click or left-click without
  dragging — pointer input works regardless.

## Hotkey setup

The app itself registers no global hotkeys; your desktop environment runs
the command. `make install-hotkey` (or `skreenshot --install-hotkey`)
suggests Shift+Super+S and refuses to overwrite an existing binding.

- XFCE: sets one xfconf property on the `xfce4-keyboard-shortcuts`
  channel. Takes effect immediately, no restart. Kali's default Print and
  Shift+Print bindings (xfce4-screenshooter) are left alone.
- KDE Plasma: installs `~/.local/share/kglobalaccel/skreenshot.desktop`
  with `X-KDE-Shortcuts=Meta+Shift+S` and pokes kglobalacceld over D-Bus
  so the binding works without a relogin. Note: some Plasma setups bind
  Meta+Shift+S to Spectacle's region capture; if the key does nothing or
  opens Spectacle, resolve the conflict in System Settings > Keyboard >
  Shortcuts.

To pick a different key, bind the `skreenshot` command manually in your
DE's shortcut settings instead of running `--install-hotkey`.

## Configuration

A YAML file at `~/.config/skreenshot/config.yaml` (honoring `XDG_CONFIG_HOME`),
created with commented defaults on first run. Edit it by hand; every key is
optional:

- `save_dir` — folder the Shift+drag Save-As dialog opens to (default
  `~/Pictures`, falling back to your home directory if it does not exist).
- `dim` — overlay dim opacity, 0–255 (default 140).
- `log_file` — path to append a debug log to (empty = none).

For a single run, `SKREENSHOT_DIM` and `SKREENSHOT_LOG` override `dim` and
`log_file`, and `--verbose` also prints the log lines to stderr.

## Testing

```
make test         # unit tests, no display needed
make e2e          # X11 end-to-end: real app on a private Xvfb, driven by xdotool
make e2e-wayland  # Wayland end-to-end: real app on a nested kwin_wayland
make lint         # ruff
```

`test`, the e2e targets and `lint` each depend on `make deps-dev`, so they
install pytest and ruff into `.venv` on first run if they are not there yet.

The X11 e2e suite verifies crop dimensions and pixel content against a known
screen pattern, clipboard survival after process exit with no clipboard
manager present, all cancel paths, `QT_SCALE_FACTOR=2` (HiDPI) crops, the
single-instance guard, focus-loss cancel, and overlay startup timing.

The Wayland e2e suite runs the app against a real nested `kwin_wayland`
compositor (hosted on a private Xvfb, driven by xdotool through the nested
window) with a mock `org.freedesktop.portal.Screenshot` service on a private
D-Bus serving a known pattern. It verifies session detection, the portal
request/response flow, pixel-exact crops, the clipboard serve-until-replaced
path, cancel paths, the instance lock, and — with two virtual outputs — that
a single drag can select across monitors. It needs `kwin_wayland`,
`dbus-daemon`, `xdotool` and `xwininfo`, and skips itself when they are
missing.

## How it works, briefly

The screen is grabbed first: on X11 per-screen `grabWindow(0)` composited
over the virtual-desktop union (flameshot's proven pattern), on Wayland one
portal screenshot that already covers that union. The frozen frame is then
shown in a frameless overlay with the dim painted on top and the selection
hole punched out — a single virtual-desktop-sized window on X11, one
fullscreen window per monitor on Wayland (the same shape KDE's Spectacle
uses, since Wayland clients cannot position windows). On release the
selection is mapped logical-to-device (correct at any devicePixelRatio) and
cropped from the frozen image. The PNG is offered as `image/png` plus
Klipper's `x-kde-force-image-copy` opt-out flag. On X11 a detached copy of
the process keeps serving the selection until something else takes the
clipboard over, exactly like `xclip` does; on Wayland persistence comes from
the desktop's clipboard manager or `wl-copy` (see Wayland notes).

## Known limitations

- KDE Plasma / KWin (X11): the multi-monitor overlay works (see below). The
  hotkey install (kglobalaccel) and the Klipper `x-kde-force-image-copy` mime
  flag are implemented from the primary sources (Spectacle, kglobalaccel,
  plasma-desktop) but are not yet end-to-end tested against a live
  kglobalacceld/Klipper.
- Multi-monitor: the overlay spans the whole virtual desktop as one window, so a
  single drag can cross every screen. It pins a fixed window size
  (WM_NORMAL_HINTS min == max == the desktop union), which stops window managers
  such as KWin from clamping the overlay to a single monitor. Monitors of
  different heights or offsets leave corners of the union bounding box that no
  monitor covers; a drag into those corners captures them as black.
- The launched-from-hotkey keyboard grab race (the DE daemon holds the keyboard
  until the shortcut keys are released) is handled by not grabbing the keyboard
  at all; the overlay is a normal focused window. This path has not been
  exercised across every desktop environment.
- On X11, if the overlay loses focus (another window activates), it cancels
  rather than fighting for the screen; this is deliberate, so the
  overlay can never soft-lock the session. On Wayland the overlay does not
  cancel on focus loss (with one window per monitor, focus legitimately
  moves between them); it cannot soft-lock either, because right-click and
  click-without-drag cancel regardless of keyboard focus.
- On Wayland, monitors of different heights or offsets leave corners of the
  union bounding box no monitor covers, same as X11; portal backends fill
  those with transparent pixels, which end up black in the crop.
- The Wayland portal writes its full-screen capture to a temporary file
  (KDE and GNOME put it in your Pictures folder); skreenshot deletes that
  file right after reading it, so it does not accumulate there.

## License

GPLv3 (PyQt6 is GPL-licensed). See LICENSE.
