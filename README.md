# skreenshot

A Linux clone of the Windows Shift+Win+S snipping flow, reduced to its core:
press a hotkey, the screen freezes and dims, drag a rectangle, and on release
the region is on the clipboard as a PNG. Esc cancels. Nothing else: no
editor, no annotations, no save dialog, no tray icon.

X11 only (XFCE and KDE Plasma). On a Wayland session it prints one error
line and exits; it never produces a black capture.

## Requirements

- Python 3 with `venv`. `make deps` creates a local `.venv` and pip-installs
  PyQt6 into it; `make deps-dev` also installs the dev/test tools (pytest, ruff).
- X11 session.
- For the end-to-end tests only: the system tools `Xvfb`, `xdotool`, `xclip`
  and ImageMagick, from your OS package manager (pip can't provide these).

## Install

```
git clone <this repo>
cd skreenshot
make deps             # create .venv and pip-install PyQt6
make install          # symlinks ~/.local/bin/skreenshot, installs icons
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
immediately. Cancel with Esc, a right-click, or a click without a drag;
cancel never touches the clipboard.

Exit codes: 0 image copied, 2 cancelled, 1 error. A detached helper
process may briefly outlive the command; it serves the clipboard until
another application takes the selection over, then exits. That is what
makes the paste survive on sessions without a clipboard manager.

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

None, by design. Two environment variables exist for debugging and taste:

- `SKREENSHOT_LOG=/path/to/file` appends a debug log (session, capture
  size, selection, copy sizes, timing, cancel reason).
- `SKREENSHOT_DIM=0..255` sets the overlay dim opacity (default 140).
- `--verbose` prints the same log lines to stderr.

## Testing

```
make test    # unit tests, no display needed
make e2e     # end-to-end: real app on a private Xvfb, driven by xdotool
make lint    # ruff
```

`test`, `e2e` and `lint` each depend on `make deps-dev`, so they install pytest
and ruff into `.venv` on first run if they are not there yet.

The e2e suite verifies crop dimensions and pixel content against a known
screen pattern, clipboard survival after process exit with no clipboard
manager present, all cancel paths, `QT_SCALE_FACTOR=2` (HiDPI) crops, the
single-instance guard, focus-loss cancel, and overlay startup timing.

## How it works, briefly

The screen is grabbed first (per-screen `grabWindow(0)` composited over
the virtual-desktop union, flameshot's proven X11 pattern), then shown
frozen in a frameless fullscreen-sized window with the dim painted on top
and the selection hole punched out. On release the selection is mapped
logical-to-device (correct at any devicePixelRatio) and cropped from the
frozen image. The PNG is offered as `image/png` plus Klipper's
`x-kde-force-image-copy` opt-out flag, and a detached copy of the process
keeps serving the selection until something else takes the clipboard
over, exactly like `xclip` does.

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
- Wayland is not supported in v1. The tool detects a Wayland session and
  exits with a clear error instead of capturing a black screen.
- If the overlay loses focus (another window activates), it cancels
  rather than fighting for the screen; this is deliberate, so the
  overlay can never soft-lock the session.

## License

GPLv3 (PyQt6 is GPL-licensed). See LICENSE.
