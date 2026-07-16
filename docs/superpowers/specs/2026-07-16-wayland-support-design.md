# Wayland support — design

Date: 2026-07-16. Status: approved for implementation (user delegated design
decisions; research verified against primary sources: xdg-desktop-portal,
qtbase/qtwayland, KWin, Mutter, Sway, Spectacle, flameshot, wl-clipboard).

## Goal

`skreenshot` runs natively on Wayland sessions (KDE Plasma 5.27+/6.x, GNOME,
wlroots compositors) with the same UX as on X11: hotkey → frozen dimmed
screen → drag → PNG on the clipboard (Shift adds Save-As). X11 behavior is
unchanged.

## Non-goals

- Layer-shell overlays (GNOME refuses the protocol for third parties;
  LayerShellQt has no PyQt bindings). Plain fullscreen toplevels are the only
  portable primitive — this is Spectacle's architecture.
- Compositor-specific capture APIs (KWin ScreenShot2, gnome-shell Screenshot):
  gated to allowlisted callers; the portal is the sanctioned path for apps.
- Global hotkey registration on Wayland beyond what `--install-hotkey`
  already does for KDE (kglobalaccel works on Wayland).

## Backend selection (`session.py`)

`detect_backend(environ) -> (kind, error)` with `kind in {"x11", "wayland"}`:

- `WAYLAND_DISPLAY` set → `wayland`. It is the one signal that predicts a
  reachable compositor (`XDG_SESSION_TYPE` is `tty` over ssh and absent in
  systemd user services; flameshot and Qt itself use the same rule).
- else `DISPLAY` set → `x11`. Covers ssh/X-forwarding and plain X sessions.
- else: error naming both variables.

On `wayland`, the CLI does `os.environ.setdefault("QT_QPA_PLATFORM",
"wayland")` before Qt loads: a user-set value always wins (flameshot's
unconditional override is a documented mistake), and running as an XWayland
client would make `grabWindow(0)` silently capture black. The compositor
socket is also pre-checked (`wayland_socket_error`): Qt aborts the whole
process with qFatal when the wayland platform plugin cannot connect, so a
dead `WAYLAND_DISPLAY` must fail with a normal error line before Qt loads.

## Capture (`portal.py`, `capture.py`)

`org.freedesktop.portal.Screenshot` over D-Bus via PyQt6's bundled QtDBus (no
new dependencies):

1. Compute the request path `/org/freedesktop/portal/desktop/request/
   <unique-name ':' stripped, '.'→'_'>/<handle_token>`; subscribe to
   `org.freedesktop.portal.Request.Response` on it BEFORE calling (the
   documented race-avoidance rule), call
   `Screenshot("", {handle_token, interactive: false})`, re-subscribe if the
   returned handle differs (pre-0.9 portals).
2. Wait for `Response(u code, a{sv} results)` in a local event loop with a
   long timeout (the first call may show a one-time permission dialog on
   GNOME and Plasma 6.4+; Plasma ≤ 6.3 captures silently).
   `code`: 0 → `results["uri"]`; 1 → user cancelled (exit 2); other → error.
3. Read the `file://` PNG, then **delete it**: KDE and GNOME write the portal
   screenshot into `~/Pictures` and nothing else ever cleans it up.
4. Geometry: every portal backend (KWin, Mutter, wlr/grim) returns ONE image
   covering the bounding box of all outputs in layout coordinates, rendered
   at `max(scale)` over the outputs. So `dpr = image_width / union_width`
   with `union` from `QScreen` geometries — the same composite-over-union
   model the X11 path already uses. Size mismatches are logged and tolerated
   (best effort, width-derived dpr).

## Overlay (`wayland_overlay.py`)

Wayland clients cannot position windows, so the X11 single-window-spanning
trick is impossible. Instead (Spectacle's proven shape):

- One frameless `QWidget` per `QScreen`; `setScreen()` + geometry BEFORE
  `show`, then `showFullScreen()` — Qt 6 sends
  `xdg_toplevel.set_fullscreen(output)`, honored by KWin/Mutter/wlroots.
  Re-apply screen+geometry on `QWindow::screenChanged` (fractional-scale
  mis-assignment, KDE bug 502047).
- A shared `SelectionController` holds the drag state in union-local logical
  coordinates. Each window maps local→virtual by adding its screen offset and
  paints the shared frozen pixmap at `-offset`, dim + selection hole exactly
  like the X11 overlay.
- Cross-screen drags work through the implicit grab: while a button is held,
  KWin/Mutter/Sway all keep pointer focus on the pressed surface and deliver
  surface-local motion beyond its bounds (verified in each compositor's
  source); Qt forwards the coordinates unclamped. The controller clamps to
  the union, so one gesture can span monitors.
- Esc handling on every window (press+release pairing as on X11);
  `requestActivate()` on mouse press so keyboard focus follows clicks between
  outputs (xdg-activation, Qt 6.3+).
- **No focus-loss cancel on Wayland.** With N overlay windows, activation
  legitimately bounces between them, and on GNOME the overlay may not get
  initial keyboard focus at all (focus-stealing prevention) — auto-cancel
  would self-destruct. Soft-lock is still impossible: right-click and
  click-without-drag cancel via pointer events, which need no keyboard focus.
  (Spectacle likewise never cancels on deactivation.)

## Clipboard (`clip.py`)

Setting the clipboard works from the release handler on all compositors (the
release event's serial is the freshest; KWin doesn't validate, Mutter needs
the caller focused — we are). The hard part is surviving exit:

- The X11 detached-holder cannot work on GNOME (no data-control protocol, and
  core `set_selection` requires focus), so persistence is decided per
  environment, in `run_capture` after the copy:
  1. Klipper on the bus (`org.kde.klipper` service) → exit; Klipper stores
     the image (we already offer `x-kde-force-image-copy`, which its default
     `IgnoreImages=true` requires) and its `PreventEmptyClipboard` restores
     it when our source dies.
  2. GNOME (`XDG_CURRENT_DESKTOP`) → exit; Mutter's built-in clipboard
     manager (3.34+) re-owns the "best" mimetype after the owner exits — it
     prefers text over images, so the offer must stay image-only (it already
     is).
  3. `wl-copy` available → pipe the PNG to it and exit; it uses data-control
     (zwlr on Plasma 5.20–6.3/wlroots, ext on wl-clipboard ≥ 2.3 for
     Plasma 6.4+) and serves in the background until replaced.
  4. Otherwise → the process itself keeps serving until another client takes
     the selection (the X11 holder semantics, minus the detach), with a
     stderr note recommending wl-clipboard.
- In all cases the event loop stays alive for a short grace period after the
  copy: Klipper and Mutter read the pixels asynchronously through a pipe, and
  Qt only writes on the data source's `send` — exiting immediately races
  them. The mime data is subclassed to observe reads; the grace ends early
  once a consumer finished reading (plus idle margin) or at a fixed cap.
- Robustness rules on top of the plan (added during review): right after the
  copy a ~150 ms settle check (`selection_accepted`) distinguishes a
  compositor *rejection* — which arrives as a cancel within one round trip —
  from a later legitimate replacement. A rejected copy is recovered through
  wl-copy or reported as an error (exit 1), never silently dropped. A
  clipboard the user already replaced (e.g. while the save dialog was open)
  is left alone. A wl-copy hand-off failure falls back to in-process serving
  rather than aborting. And before any open-ended serving, the process
  releases the single-instance lock: no overlay exists at that point, so a
  new invocation must be allowed to start one (its copy then also releases
  the old server).

## CLI (`cli.py`)

- `check_session()` → `detect_backend()`; wayland no longer refuses.
- Instance lock keyed on both `DISPLAY` and `WAYLAND_DISPLAY` (default
  `wayland-0`), same flock scheme.
- `run_capture(backend, ...)` picks grab + overlay per backend; crop, PNG
  encode, Save-As dialog are shared. Portal "cancelled" maps to exit 2.

## Testing

- Unit (offscreen platform): portal helpers (token/path/uri, response
  handling against a mock bus service), controller state machine incl.
  cross-screen mapping and multi-window focus rules, backend detection, lock
  keying, persistence decision table, portal-image dpr derivation.
- End-to-end on a real Wayland compositor, headless: private Xvfb hosting a
  nested `kwin_wayland`, private `dbus-run-session` bus running a mock
  `org.freedesktop.portal.Screenshot` that serves a quadrant pattern PNG;
  real input via xdotool into the nested compositor window (KWin's X11
  backend translates host XTEST into Wayland input); clipboard read back by
  a focused Qt client in the nested session. Auto-skips when tools are
  missing. Multi-output via `--output-count 2` (nested output windows are
  moved apart with xdotool; KWin lays outputs left-to-right).
- Some distributions pair a KWin 5.27 built against an older libwayland with
  a newer libwayland 1.23 at runtime (KWin's stale `wl_shm_interface` v1
  symbol shadows libwayland's v2 → `wl_global_create` fails → no `wl_shm`
  global → every shm client crashes). The harness detects that and works
  around it by bind-mounting a capability-stripped copy of the binary in a
  user namespace and preloading current core interface structs. This is a
  harness-only concern; a running Wayland desktop session implies a
  consistent stack.

## Known risks (unresolvable without a live desktop Wayland session)

- Real portal backends (permission dialogs, GNOME's capture flash/shutter
  sound) are mocked in tests; behavior is implemented from portal/back-end
  sources and may differ in minor ways.
- GNOME may deny initial keyboard focus to hotkey-launched overlays (Esc dead
  until first click; pointer cancel paths unaffected).
- Mixed per-monitor fractional scales: dpr derivation and per-window painting
  follow the documented max-scale rule but have only been exercised at
  uniform scale in the harness.
