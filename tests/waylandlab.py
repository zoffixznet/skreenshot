"""A disposable Wayland compositor lab for the e2e suite.

Stack: a private Xvfb hosts a nested kwin_wayland; a private dbus-daemon
carries a mock org.freedesktop.portal.Screenshot (tests/portal_mock.py) that
serves a known pattern PNG; xdotool drives real input into the nested
compositor window (KWin's X11 backend translates host XTEST events into
Wayland input for its clients).

KWin/libwayland mismatch workaround: some distributions ship a KWin built
against an older libwayland than the one installed. KWin then exports a
stale wl_shm_interface struct that shadows libwayland's own, wl_global_create
fails ("implemented version ... higher than interface version") and the
compositor never advertises wl_shm - which crashes every shm-rendering
client. When the probe below detects that, the lab restarts KWin with
freshly generated core interface structs preloaded. Two hurdles make that
preload non-trivial: kwin_wayland carries file capabilities, so the loader
runs in secure-exec mode and silently drops LD_PRELOAD; and both a copied
binary and an explicit ld.so invocation break KWin's static QPA plugin.
The one combination that works is a user namespace that bind-mounts a
capability-stripped copy over /usr/bin/kwin_wayland: original path, no
caps, LD_PRELOAD honored.
"""

import os
import shutil
import socket
import struct
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
XML = "/usr/share/wayland/wayland.xml"

CORE_TOOLS = ["kwin_wayland", "Xvfb", "xdotool", "dbus-daemon"]
SHIM_TOOLS = ["gcc", "wayland-scanner", "unshare"]


def missing_core_tools():
    return [t for t in CORE_TOOLS if not shutil.which(t)]


def can_build_shim():
    return not [t for t in SHIM_TOOLS if not shutil.which(t)] and os.path.exists(XML)


# -- wl_shm pre-flight (pure-stdlib wayland registry dump) -------------------


def registry_interfaces(socket_path, timeout=5.0):
    """Connect to a wayland socket and return the advertised global names.

    Speaks just enough of the wire protocol: get_registry + sync, then
    collects wl_registry.global events until the sync callback fires. This
    is the pre-flight that catches a compositor whose renderer or libwayland
    is broken enough to not offer wl_shm (Qt clients segfault on those).
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(socket_path)
    try:
        # wl_display(1).get_registry(new id 2); wl_display(1).sync(new id 3)
        sock.sendall(struct.pack("<III", 1, (12 << 16) | 1, 2))
        sock.sendall(struct.pack("<III", 1, (12 << 16) | 0, 3))
        interfaces = []
        buf = b""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                chunk = sock.recv(65536)
            except TimeoutError:
                break
            if not chunk:
                break
            buf += chunk
            while len(buf) >= 8:
                obj, sizeop = struct.unpack_from("<II", buf)
                size, opcode = sizeop >> 16, sizeop & 0xFFFF
                if len(buf) < size:
                    break
                body = buf[8:size]
                buf = buf[size:]
                if obj == 2 and opcode == 0:  # wl_registry.global
                    # uint name, string interface (len incl NUL, pad 4), uint version
                    (strlen,) = struct.unpack_from("<I", body, 4)
                    name = body[8 : 8 + strlen - 1].decode()
                    interfaces.append(name)
                elif obj == 3 and opcode == 0:  # wl_callback.done: sync complete
                    return interfaces
        return interfaces
    finally:
        sock.close()


# -- lab pieces ---------------------------------------------------------------


def free_x_display(start=89, stop=79):
    for n in range(start, stop, -1):
        if not os.path.exists(f"/tmp/.X{n}-lock"):
            return f":{n}"
    raise RuntimeError("no free X display number")


def make_runtime_dir():
    """XDG_RUNTIME_DIR for the lab. Must be SHORT: the wayland socket path
    has the 108-byte unix-socket limit, and pytest tmp paths overflow it."""
    import tempfile

    path = tempfile.mkdtemp(prefix="skreew-")
    os.chmod(path, 0o700)
    return path


def build_shim(workdir):
    """Compile current core interface structs + a capability-less kwin copy."""
    gen_c = os.path.join(workdir, "wl_core_ifaces.c")
    shim_so = os.path.join(workdir, "wl_core_ifaces.so")
    kwin_copy = os.path.join(workdir, "kwin_nocap")
    subprocess.run(
        ["wayland-scanner", "public-code", XML, gen_c], check=True, timeout=30
    )
    subprocess.run(
        ["gcc", "-shared", "-fPIC", "-O2", "-o", shim_so, gen_c],
        check=True,
        timeout=60,
    )
    shutil.copy2(shutil.which("kwin_wayland"), kwin_copy)
    return shim_so, kwin_copy


class WaylandLab:
    """One nested compositor + bus + mock portal, torn down as a unit."""

    def __init__(self, outputs=1, width=1280, height=800):
        self.outputs = outputs
        self.width = width
        self.height = height
        self.procs = []
        self.rtd = make_runtime_dir()
        self.display = None
        self.socket_name = "wayland-lab"
        self.bus_address = f"unix:path={self.rtd}/bus"
        self.pattern_png = os.path.join(self.rtd, "pattern.png")
        self.skip_reason = None

    # -- setup steps, each raising RuntimeError with a skip-worthy message --

    def start_xvfb(self):
        self.display = free_x_display()
        host_w = self.width * self.outputs + 40
        proc = subprocess.Popen(
            ["Xvfb", self.display, "-screen", "0", f"{host_w}x{self.height + 40}x24"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.procs.append(proc)
        sock = f"/tmp/.X11-unix/X{self.display[1:]}"
        self._wait_for(lambda: os.path.exists(sock), 10, "Xvfb did not come up")

    def _kwin_cmd(self, shim=None):
        args = [
            "--socket", self.socket_name,
            "--width", str(self.width),
            "--height", str(self.height),
            "--no-lockscreen", "--no-global-shortcuts",
        ]
        if self.outputs > 1:
            args += ["--output-count", str(self.outputs)]
        if shim is None:
            return ["kwin_wayland", *args]
        shim_so, kwin_copy = shim
        kwin_path = shutil.which("kwin_wayland")
        inner = (
            f"mount --bind {kwin_copy} {kwin_path} && "
            f"exec env LD_PRELOAD={shim_so} {kwin_path} " + " ".join(args)
        )
        return ["unshare", "-r", "-m", "sh", "-c", inner]

    def _kwin_env(self):
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),
            "DISPLAY": self.display,
            "XDG_RUNTIME_DIR": self.rtd,
            # Isolated config/data: a stale kscreen profile in the real HOME
            # would silently override the default left-to-right output layout.
            "XDG_CONFIG_HOME": os.path.join(self.rtd, "kwin-config"),
            "XDG_DATA_HOME": os.path.join(self.rtd, "kwin-data"),
            "KWIN_COMPOSE": "Q",  # QPainter: no GL needed on a bare Xvfb
        }
        return env

    def start_kwin(self):
        sock_path = os.path.join(self.rtd, self.socket_name)
        log_path = os.path.join(self.rtd, "kwin.log")

        def launch(shim):
            log_fh = open(log_path, "ab")
            proc = subprocess.Popen(
                self._kwin_cmd(shim),
                env=self._kwin_env(),
                stdout=log_fh,
                stderr=log_fh,
            )
            self.procs.append(proc)
            self._wait_for(
                lambda: os.path.exists(sock_path),
                15,
                "nested kwin_wayland never created its socket",
            )
            # Give the compositor a beat to finish wiring globals.
            time.sleep(1.0)
            return proc

        proc = launch(None)
        if "wl_shm" not in registry_interfaces(sock_path):
            # Broken kwin/libwayland combination (see module docstring).
            proc.terminate()
            proc.wait(timeout=10)
            self.procs.remove(proc)
            # kwin removes its socket on exit; make sure it is gone either
            # way so the relaunch wait below sees the NEW socket appear.
            try:
                os.unlink(sock_path)
            except FileNotFoundError:
                pass
            if not can_build_shim():
                raise RuntimeError(
                    "nested kwin does not advertise wl_shm and the interface "
                    f"shim cannot be built (need {SHIM_TOOLS} and {XML})"
                )
            shim = build_shim(self.rtd)
            launch(shim)
            if "wl_shm" not in registry_interfaces(sock_path):
                raise RuntimeError(
                    "nested kwin does not advertise wl_shm even with the "
                    "interface shim; cannot host Qt clients"
                )

    def start_bus_and_mock(self):
        proc = subprocess.Popen(
            [
                "dbus-daemon", "--session", "--nofork",
                f"--address={self.bus_address}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.procs.append(proc)
        self._wait_for(
            lambda: os.path.exists(f"{self.rtd}/bus"), 10, "dbus-daemon did not start"
        )

        mock_log = open(os.path.join(self.rtd, "mock.log"), "wb")
        mock = subprocess.Popen(
            [sys.executable, os.path.join(REPO, "tests", "portal_mock.py"),
             self.pattern_png],
            env={
                **self._kwin_env(),
                "DBUS_SESSION_BUS_ADDRESS": self.bus_address,
                "QT_QPA_PLATFORM": "offscreen",
            },
            stdout=mock_log,
            stderr=mock_log,
        )
        self.procs.append(mock)
        self._wait_for(
            lambda: b"MOCK: ready"
            in open(os.path.join(self.rtd, "mock.log"), "rb").read(),
            15,
            "mock portal never became ready",
        )

    def write_pattern(self, painter_fn):
        """Render the pattern PNG the mock portal serves; painter_fn(image)."""
        from PyQt6.QtGui import QImage

        img = QImage(
            self.width * self.outputs, self.height, QImage.Format.Format_RGB32
        )
        painter_fn(img)
        if not img.save(self.pattern_png):
            raise RuntimeError("could not write the pattern PNG")

    def setup(self, painter_fn):
        try:
            self.start_xvfb()
            self.start_kwin()
            self.start_bus_and_mock()
            self.write_pattern(painter_fn)
        except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
            self.skip_reason = str(exc)
            self.teardown()

    # -- running clients in the lab -----------------------------------------

    def env(self, **extra):
        """Environment for a client of the nested compositor + private bus.

        DISPLAY stays set (pointing at the lab Xvfb) exactly like a real
        Wayland session offering XWayland; backend detection must still pick
        wayland.
        """
        env = dict(os.environ)
        for var in ("QT_QPA_PLATFORM", "QT_SCALE_FACTOR", "XDG_CURRENT_DESKTOP"):
            env.pop(var, None)
        env.update(
            DISPLAY=self.display,
            WAYLAND_DISPLAY=self.socket_name,
            XDG_RUNTIME_DIR=self.rtd,
            DBUS_SESSION_BUS_ADDRESS=self.bus_address,
            XDG_CONFIG_HOME=os.path.join(self.rtd, "app-config"),
            XDG_CURRENT_DESKTOP="",
            SKREENSHOT_PORTAL_TIMEOUT_MS="15000",
            # Pin the clipboard persistence path: there is no Klipper or
            # GNOME manager in the nested session, and whether the HOST has
            # wl-copy installed must not change what these tests exercise.
            SKREENSHOT_WL_PERSIST="serve",
        )
        env.update(extra)
        return env

    def xdo(self, *args):
        subprocess.run(
            ["xdotool", *args],
            env={"DISPLAY": self.display, "PATH": os.environ.get("PATH", "")},
            check=True,
            timeout=20,
        )

    def drag(self, x1, y1, x2, y2):
        mx, my = (x1 + x2) // 2, (y1 + y2) // 2
        self.xdo(
            "mousemove", str(x1), str(y1),
            "mousedown", "1",
            "mousemove", str(mx), str(my),
            "sleep", "0.2",
            "mousemove", str(x2), str(y2),
            "sleep", "0.2",
            "mouseup", "1",
        )

    def compositor_windows(self):
        """Host X window ids of the nested compositor's output windows.

        xdotool's name search does not match these windows (they carry only a
        legacy WM_NAME), so enumerate the root's children with xwininfo and
        pick the ones with exactly one output's geometry. Sorted by id, which
        is creation order: KWin creates the window for output 0 first.
        """
        import re as _re

        if not shutil.which("xwininfo"):
            return []
        result = subprocess.run(
            ["xwininfo", "-root", "-children"],
            env={"DISPLAY": self.display, "PATH": os.environ.get("PATH", "")},
            capture_output=True,
            text=True,
            timeout=20,
        )
        ids = []
        for line in result.stdout.splitlines():
            m = _re.match(
                rf"\s+(0x[0-9a-f]+) .*:.*\s{self.width}x{self.height}\+", line
            )
            if m:
                ids.append(int(m.group(1), 16))
        return sorted(ids)

    # -- plumbing -------------------------------------------------------------

    def _wait_for(self, predicate, timeout, message):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return
            except OSError:
                pass
            time.sleep(0.1)
        raise RuntimeError(message)

    def teardown(self):
        for proc in reversed(self.procs):
            if proc.poll() is None:
                proc.terminate()
        for proc in reversed(self.procs):
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        self.procs.clear()
        shutil.rmtree(self.rtd, ignore_errors=True)
