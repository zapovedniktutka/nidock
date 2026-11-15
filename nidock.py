#!/usr/bin/env python3
"""NiDock - a simple zooming dock for Linux Mint (Cinnamon, X11).

Dependencies: python3-gi, python3-gi-cairo, gir1.2-gtk-3.0, gir1.2-wnck-3.0, x11-utils
Settings: ~/.config/nidock/config.json

Usage:
  python3 nidock.py --install     copies the dock to ~/.local/bin, enables start at
                                  login + an applications-menu entry, and starts it
  python3 nidock.py --uninstall   removes autostart, the menu entry and the copy
  python3 nidock.py --supervise   runs the dock and restarts it if it crashes
  python3 nidock.py --settings    opens settings (starts the dock if not running)
  python3 nidock.py               plain start, without supervision
"""
import faulthandler
import fcntl
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
try:
    gi.require_version("GdkX11", "3.0")
except ValueError:
    pass
try:
    gi.require_version("Wnck", "3.0")
except ValueError:
    pass

from gi.repository import Gtk, Gdk, GLib, Gio, GdkPixbuf  # noqa: E402

try:
    from gi.repository import GdkX11  # noqa: F401,E402
except ImportError:
    pass

try:
    from gi.repository import Wnck  # noqa: E402
except ImportError:
    Wnck = None
    print("WARNING: Wnck is not installed (gir1.2-wnck-3.0), "
          "running-app indicators will not work.", file=sys.stderr)

import cairo  # noqa: E402

# ---------------------------------------------------------------- settings

CONFIG_DIR = os.path.join(GLib.get_user_config_dir(), "nidock")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

DEFAULTS = {
    "icon_size": 48,          # icon size in pixels
    "zoom": 1.3,              # magnification on hover (1.0 = no zoom)
    "margin_bottom": 8,       # gap from the bottom edge of the screen
    "opacity": 0.55,          # dock background opacity (0 - 1)
    "bg_color": "#141721",        # dock color
    "border_color": "#ffffff",    # border and separator color
    "indicator_color": "#ffffff", # color of the running-app dots
    "spacing": 8,             # gap between icons
    "padding": 10,            # inner padding of the panel
    "radius": 18,             # corner radius
    "window_gap": 8,          # gap between windows and the dock
    "reserve_space": True,    # keep windows from going under the dock
    "autohide": False,        # hide the dock; it slides in when the mouse reaches the bottom edge
    "show_running": True,      # also show running apps that are not pinned
    "pinned": None,           # list of .desktop files (None = automatic)
}

CANDIDATES = [
    "firefox.desktop", "google-chrome.desktop", "chromium.desktop",
    "nemo.desktop", "org.gnome.Terminal.desktop", "mintinstall.desktop",
    "xed.desktop", "org.gnome.Calculator.desktop", "vlc.desktop",
    "code.desktop", "cinnamon-settings.desktop",
]

GAP = 8        # gap between icons
PAD = 10       # inner padding of the panel
SEP = 16       # separator width
SEP_KEY = "--separator--"   # marker for a user-placed separator in the pinned list
TIP_H = 36     # space reserved for the tooltip


SCRIPT = os.path.abspath(__file__)
AUTOSTART_FILE = os.path.join(GLib.get_user_config_dir(), "autostart", "nidock.desktop")
MENU_FILE = os.path.join(GLib.get_user_data_dir(), "applications", "nidock.desktop")

PRESETS = {
    "Night (default)":     {"bg_color": "#141721", "border_color": "#ffffff", "indicator_color": "#ffffff"},
    "Charcoal":            {"bg_color": "#1e1e1e", "border_color": "#ffffff", "indicator_color": "#ffffff"},
    "Midnight blue":       {"bg_color": "#0b1d3a", "border_color": "#6ea8ff", "indicator_color": "#9cc4ff"},
    "Mint green":          {"bg_color": "#0f2a1d", "border_color": "#87cf3e", "indicator_color": "#87cf3e"},
    "Purple":              {"bg_color": "#25133f", "border_color": "#b57bff", "indicator_color": "#d1a8ff"},
    "Rose":                {"bg_color": "#3a1226", "border_color": "#ff7eb6", "indicator_color": "#ffb3d6"},
    "Light":               {"bg_color": "#f2f2f2", "border_color": "#000000", "indicator_color": "#333333"},
}

_color_cache = {}


def hex_rgb(value, fallback):
    """'#rrggbb' -> (r, g, b) in range 0-1 (cached, called on every draw)."""
    key = (str(value), fallback)
    c = _color_cache.get(key)
    if c is None:
        rgba = Gdk.RGBA()
        if not rgba.parse(str(value)):
            rgba.parse(fallback)
        c = (rgba.red, rgba.green, rgba.blue)
        _color_cache[key] = c
    return c


def _write_desktop(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _desktop_text(autostart):
    t = ("[Desktop Entry]\nType=Application\nName=NiDock\n"
         "Comment=Zooming dock\nExec=python3 \"%s\" --supervise\nIcon=user-desktop\n"
         "Terminal=false\n" % SCRIPT)
    if autostart:
        t += "X-GNOME-Autostart-enabled=true\nX-GNOME-Autostart-Delay=3\n"
    else:
        t += ("Categories=Settings;DesktopSettings;GTK;\nActions=Settings;\n\n"
              "[Desktop Action Settings]\nName=Settings\n"
              "Exec=python3 \"%s\" --settings\n" % SCRIPT)
    return t


def set_autostart(on):
    try:
        if on:
            _write_desktop(AUTOSTART_FILE, _desktop_text(True))
        elif os.path.exists(AUTOSTART_FILE):
            os.remove(AUTOSTART_FILE)
    except OSError as e:
        print("Autostart error:", e, file=sys.stderr)


def set_menu_entry(on):
    try:
        if on:
            _write_desktop(MENU_FILE, _desktop_text(False))
        elif os.path.exists(MENU_FILE):
            os.remove(MENU_FILE)
    except OSError as e:
        print("Error writing menu entry:", e, file=sys.stderr)


def install():
    """Copies the dock to a permanent location, enables autostart and the menu entry, then starts it."""
    global SCRIPT
    dest = os.path.join(os.path.expanduser("~"), ".local", "bin", "nidock.py")
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.abspath(__file__) != dest:
            shutil.copy2(os.path.abspath(__file__), dest)
        os.chmod(dest, 0o755)
    except OSError as e:
        print("Installation failed:", e, file=sys.stderr)
        sys.exit(1)
    SCRIPT = dest
    set_autostart(True)
    set_menu_entry(True)
    subprocess.Popen([sys.executable, dest, "--supervise"], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("NiDock installed to %s and started." % dest)
    print("It will start at every login and is also available in the applications menu.")


def uninstall():
    set_autostart(False)
    set_menu_entry(False)
    dest = os.path.join(os.path.expanduser("~"), ".local", "bin", "nidock.py")
    if os.path.abspath(__file__) != dest and os.path.exists(dest):
        try:
            os.remove(dest)
        except OSError:
            pass
    print("Autostart and menu entry removed. Close the running dock via "
          "right-click -> Quit dock.")


def supervise():
    """Runs the dock and restarts it if it crashes. A deliberate exit (code 0) or logout does not restart it."""
    me = os.path.abspath(__file__)
    stop = []
    child = [None]

    def on_term(signum, frame):
        stop.append(signum)
        c = child[0]
        if c is not None and c.poll() is None:
            c.terminate()

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, on_term)

    clean = (0, -signal.SIGTERM, -signal.SIGINT, -signal.SIGHUP)
    delay = 1
    while not stop:
        t0 = time.monotonic()
        child[0] = subprocess.Popen([sys.executable, me])
        rc = child[0].wait()
        if stop or rc in clean:
            break
        # if it keeps crashing right away, wait longer (up to 30 s) to avoid a tight loop
        delay = 2 if time.monotonic() - t0 > 30 else min(delay * 2, 30)
        print("NiDock stopped (code %s), restarting in %d s." % (rc, delay), file=sys.stderr)
        for _ in range(int(delay * 10)):
            if stop:
                break
            time.sleep(0.1)


def desktop_app(desktop_id):
    """Returns Gio.DesktopAppInfo or None (PyGObject raises TypeError for a missing file)."""
    try:
        return Gio.DesktopAppInfo.new(desktop_id)
    except (TypeError, GLib.Error):
        return None


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    if not cfg.get("pinned"):
        cfg["pinned"] = [c for c in CANDIDATES
                         if desktop_app(c) is not None][:8]
    return cfg


def save_config(cfg):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4, ensure_ascii=False)
    except OSError as e:
        print("Cannot save settings:", e, file=sys.stderr)


# ------------------------------------------------------------ helper functions

def strong_keys(app):
    keys = set()
    i = (app.get_id() or "").lower()
    if i.endswith(".desktop"):
        i = i[:-8]
    if i:
        keys.add(i)
        if "." in i:
            keys.add(i.split(".")[-1])
    wm = app.get_startup_wm_class()
    if wm:
        keys.add(wm.lower())
    ex = app.get_executable()
    if ex:
        keys.add(os.path.basename(ex).lower())
    return keys


def name_keys(app):
    n = app.get_name()
    return {n.lower()} if n else set()


def window_keys(w):
    keys = set()
    for getter in ("get_class_group_name", "get_class_instance_name"):
        try:
            s = getattr(w, getter)()
        except Exception:
            s = None
        if s:
            keys.add(s.lower())
    try:
        a = w.get_application()
        if a and a.get_name():
            keys.add(a.get_name().lower())
    except Exception:
        pass
    return keys


def rrect(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    cr.close_path()


def now_ts():
    t = Gtk.get_current_event_time()
    return t if t else int(time.time())


# ----------------------------------------------------------------------- Item

class Item:
    def __init__(self, key, app=None, name="", fallback=None, pinned=False):
        self.key = key
        self.app = app
        self.name = name or (app.get_name() if app else key)
        self.pinned = pinned
        self.fallback = fallback
        self.windows = []
        self.keys = set()
        if app:
            self.keys = strong_keys(app) | name_keys(app)
        self.is_sep = False
        self.scale = 1.0
        self.target = 1.0
        self.bounce_t0 = None
        self.x = 0.0
        self.w = 0.0
        self.size = 0.0
        self.icons = {}

    def icon(self, size):
        size = int(size)
        if size in self.icons:
            return self.icons[size]
        pb = None
        theme = Gtk.IconTheme.get_default()
        if self.app is not None:
            gicon = self.app.get_icon()
            if gicon is not None:
                info = theme.lookup_by_gicon(gicon, size, Gtk.IconLookupFlags.FORCE_SIZE)
                if info is not None:
                    try:
                        pb = info.load_icon()
                    except GLib.Error:
                        pb = None
        if pb is None and self.fallback is not None:
            pb = self.fallback.scale_simple(size, size, GdkPixbuf.InterpType.BILINEAR)
        if pb is None:
            try:
                pb = theme.load_icon("application-x-executable", size,
                                     Gtk.IconLookupFlags.FORCE_SIZE)
            except GLib.Error:
                pb = None
        self.icons[size] = pb
        return pb


# ----------------------------------------------------------------------- Dock

class Dock(Gtk.Window):
    def __init__(self, cfg):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.cfg = cfg
        self.S = int(cfg["icon_size"])
        self.Z = float(cfg["zoom"])
        self.gap = int(cfg["spacing"])
        self.pad = int(cfg["padding"])
        self.items = []
        self.cache = {}
        self.has_sep = False
        self.sep_index = 0
        self.hover = False
        self.mx = None
        self.my = None
        self.press_item = None
        self.press_x = 0
        self.press_btn = 0
        self.dragging = False
        self.menu = None
        self.menu_open = False
        self._tick_id = 0
        self._refresh_id = 0
        self._save_id = 0
        self._strut_id = 0
        self.settings_win = None
        self._wa = None
        self.win_w = 400
        self.win_h = 100
        self.win_x = 0
        self.win_y = 0
        self.base_w = 0
        self.panel_h = self.S + 2 * self.pad
        self.headroom = 0
        self.real_h = 100
        self.reveal = 0.0 if cfg.get("autohide") else 1.0   # 1 = visible, 0 = hidden
        self.reveal_target = self.reveal
        self._hide_id = 0

        self.set_title("NiDock")
        self.set_decorated(False)
        self.set_app_paintable(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_keep_above(True)
        self.set_type_hint(Gdk.WindowTypeHint.DOCK)
        self.stick()

        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None:
            self.set_visual(visual)
        css = Gtk.CssProvider()
        css.load_from_data(b"window, .background { background: transparent; }")
        Gtk.StyleContext.add_provider_for_screen(
            screen, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.area = Gtk.DrawingArea()
        self.area.add_events(Gdk.EventMask.POINTER_MOTION_MASK |
                             Gdk.EventMask.ENTER_NOTIFY_MASK |
                             Gdk.EventMask.LEAVE_NOTIFY_MASK |
                             Gdk.EventMask.BUTTON_PRESS_MASK |
                             Gdk.EventMask.BUTTON_RELEASE_MASK)
        self.area.connect("draw", self.on_draw)
        self.area.connect("motion-notify-event", self.on_motion)
        self.area.connect("enter-notify-event", self.on_enter)
        self.area.connect("leave-notify-event", self.on_leave)
        self.area.connect("button-press-event", self.on_press)
        self.area.connect("button-release-event", self.on_release)
        self.add(self.area)

        # index of installed apps (used to match windows)
        self.app_index = {}
        self._build_app_index()

        self.wnck = None
        if Wnck is not None:
            try:
                Wnck.set_client_type(Wnck.ClientType.PAGER)
                self.wnck = Wnck.Screen.get_default()
                self.wnck.force_update()
                self.wnck.connect("window-opened", self.on_windows_changed)
                self.wnck.connect("window-closed", self.on_windows_changed)
                self.wnck.connect("active-window-changed", self.on_active_changed)
            except Exception as e:
                print("Wnck error:", e, file=sys.stderr)
                self.wnck = None

        self.refresh_items()
        self.show_all()
        GLib.idle_add(self.post_show)

    # ------------------------------------------------------------- preparation

    def _build_app_index(self):
        apps = [a for a in Gio.AppInfo.get_all()
                if isinstance(a, Gio.DesktopAppInfo) and a.should_show()]
        for a in apps:
            for k in strong_keys(a):
                self.app_index.setdefault(k, a)
        for a in apps:
            for k in name_keys(a):
                self.app_index.setdefault(k, a)

    def post_show(self):
        self.apply_geometry()
        return False

    def monitor_area(self):
        if self._wa is None:
            display = Gdk.Display.get_default()
            mon = display.get_primary_monitor() or display.get_monitor(0)
            self._wa = mon.get_workarea()
        return self._wa

    # ------------------------------------------------------- items and windows

    def get_windows(self):
        if not self.wnck:
            return []
        res = []
        for w in self.wnck.get_windows():
            try:
                if w.get_window_type() != Wnck.WindowType.NORMAL:
                    continue
                if w.is_skip_tasklist():
                    continue
            except Exception:
                continue
            res.append(w)
        return res

    def on_windows_changed(self, *args):
        if not self._refresh_id:
            self._refresh_id = GLib.timeout_add(80, self._do_refresh)

    def _do_refresh(self):
        self._refresh_id = 0
        self.refresh_items()
        return False

    def on_active_changed(self, *args):
        self.area.queue_draw()

    def refresh_items(self):
        for it in self.cache.values():
            it.windows = []

        pinned_items = []
        pinned_by_id = {}
        for pid in self.cfg["pinned"]:
            if pid == SEP_KEY:
                sep = Item(SEP_KEY, pinned=True)
                sep.is_sep = True
                pinned_items.append(sep)
                continue
            it = self.cache.get(pid)
            if it is None:
                app = desktop_app(pid)
                if app is None:
                    continue
                it = Item(pid, app=app, pinned=True)
                self.cache[pid] = it
            it.pinned = True
            pinned_items.append(it)
            pinned_by_id[pid] = it

        extras = {}
        order = []
        if self.wnck:
            for w in self.get_windows():
                wk = window_keys(w)
                target = None
                for it in pinned_items:
                    if it.keys & wk:
                        target = it
                        break
                if target is None:
                    app = None
                    for k in wk:
                        app = self.app_index.get(k)
                        if app:
                            break
                    if app is not None and app.get_id() in pinned_by_id:
                        target = pinned_by_id[app.get_id()]
                    elif self.cfg["show_running"]:
                        if app is not None:
                            key = app.get_id()
                        else:
                            key = "wm:" + (sorted(wk)[0] if wk else str(w.get_xid()))
                        target = extras.get(key)
                        if target is None:
                            target = self.cache.get(key)
                            if target is None or target.pinned:
                                fb = None
                                try:
                                    fb = w.get_icon()
                                except Exception:
                                    pass
                                name = w.get_application().get_name() if w.get_application() else key
                                target = Item(key, app=app, name=name, fallback=fb, pinned=False)
                                self.cache[key] = target
                            target.pinned = False
                            extras[key] = target
                            order.append(target)
                if target is not None:
                    target.windows.append(w.get_xid())

        # drop unpinned items that no longer exist from the cache
        for k in [k for k, v in self.cache.items() if not v.pinned and k not in extras]:
            del self.cache[k]

        self.items = pinned_items + order
        self.has_sep = any(not p.is_sep for p in pinned_items) and bool(order)
        self.sep_index = len(pinned_items)
        self.apply_geometry()
        self.update_targets()
        self.layout()
        self.area.queue_draw()

    # ------------------------------------------------------------- geometry

    def apply_geometry(self, struts=True):
        S, Z = self.S, self.Z
        self.base_w = (sum(SEP if it.is_sep else S + self.gap for it in self.items)
                       or (S + self.gap)) + (SEP if self.has_sep else 0)
        self.panel_h = S + 2 * self.pad
        self.headroom = int(S * (Z - 1)) + TIP_H + int(S * 0.5)
        self.win_h = self.headroom + self.panel_h
        wa = self.monitor_area()
        extra = int((Z - 1) * S * 2.8) + 60
        self.win_w = min(wa.width, int(self.base_w + 2 * self.pad + extra))
        self.win_x = wa.x + (wa.width - self.win_w) // 2
        mb = int(self.cfg["margin_bottom"])
        if self.autohide_on():
            # the window sits exactly on the bottom screen edge (so the mouse can hit it easily),
            # and the gap from the edge becomes extra transparent height
            self.real_h = self.win_h + mb
            self.win_y = wa.y + wa.height - self.real_h
        else:
            self.real_h = self.win_h
            self.win_y = wa.y + wa.height - self.win_h - mb

        self.area.set_size_request(self.win_w, self.real_h)
        self.resize(self.win_w, self.real_h)
        self.move(self.win_x, self.win_y)
        if self.get_window() is not None:
            self.set_input_region(self.hover)
            if struts:
                self.set_struts()

    def autohide_on(self):
        return bool(self.cfg.get("autohide"))

    def _reveal_now(self):
        """Show the dock (mouse is on it or at the screen edge)."""
        if self._hide_id:
            GLib.source_remove(self._hide_id)
            self._hide_id = 0
        if self.autohide_on() and self.reveal_target != 1.0:
            self.reveal_target = 1.0
            self.set_input_region(True)
            self.kick()

    def schedule_hide(self):
        if not self.autohide_on() or self._hide_id:
            return
        self._hide_id = GLib.timeout_add(450, self._do_hide)

    def _do_hide(self):
        self._hide_id = 0
        if (not self.autohide_on() or self.hover or self.menu_open or
                self.dragging or self.settings_win is not None):
            return False
        self.reveal_target = 0.0
        self.kick()
        return False

    def set_input_region(self, big):
        gw = self.get_window()
        if gw is None:
            return
        if big:
            top = self.win_h - self.panel_h - int(self.S * (self.Z - 1)) - 8
            x, w = 0, self.win_w
        else:
            top = self.win_h - self.panel_h
            x = max(0, int((self.win_w - self.base_w) / 2 - self.pad - 4))
            w = min(self.win_w, int(self.base_w + 2 * self.pad + 8))
        top = max(0, top)
        if self.autohide_on() and self.reveal_target == 0.0 and self.reveal < 0.02:
            top = max(0, self.real_h - 3)   # hidden: only a thin strip at the edge
        rect = cairo.RectangleInt(x, top, w, self.real_h - top)
        gw.input_shape_combine_region(cairo.Region(rect), 0, 0)

    def set_struts(self):
        gw = self.get_window()
        try:
            xid = gw.get_xid()
        except Exception:
            return
        if not self.cfg["reserve_space"] or self.autohide_on():
            for prop in ("_NET_WM_STRUT_PARTIAL", "_NET_WM_STRUT"):
                try:
                    subprocess.Popen(["xprop", "-id", str(xid), "-remove", prop])
                except OSError:
                    pass
            return
        display = Gdk.Display.get_default()
        root_h = 0
        for i in range(display.get_n_monitors()):
            g = display.get_monitor(i).get_geometry()
            root_h = max(root_h, g.y + g.height)
        panel_top = self.win_y + self.win_h - self.panel_h
        strut = max(0, root_h - panel_top) + int(self.cfg["window_gap"])
        x0 = self.win_x
        x1 = self.win_x + self.win_w - 1
        try:
            subprocess.Popen(["xprop", "-id", str(xid), "-f", "_NET_WM_STRUT_PARTIAL", "32c",
                              "-set", "_NET_WM_STRUT_PARTIAL",
                              "0,0,0,%d,0,0,0,0,0,0,%d,%d" % (strut, x0, x1)])
            subprocess.Popen(["xprop", "-id", str(xid), "-f", "_NET_WM_STRUT", "32c",
                              "-set", "_NET_WM_STRUT", "0,0,0,%d" % strut])
        except OSError:
            pass  # xprop is not installed (package x11-utils)

    def base_centers(self):
        slot = self.S + self.gap
        x = (self.win_w - self.base_w) / 2.0
        res = []
        for i, it in enumerate(self.items):
            if self.has_sep and i == self.sep_index:
                x += SEP
            w = SEP if it.is_sep else slot
            res.append(x + w / 2.0)
            x += w
        return res

    def layout(self):
        S = self.S
        total = (sum(SEP if it.is_sep else S * it.scale + self.gap for it in self.items)
                 + (SEP if self.has_sep else 0))
        x = (self.win_w - total) / 2.0
        for i, it in enumerate(self.items):
            if self.has_sep and i == self.sep_index:
                x += SEP
            if it.is_sep:
                it.x, it.size, it.w = x, 0.0, float(SEP)
                x += SEP
                continue
            s = S * it.scale
            it.x = x
            it.size = s
            it.w = s + self.gap
            x += it.w
        self.total = total

    def item_at(self, x):
        if x is None:
            return None
        for it in self.items:
            if it.x <= x <= it.x + it.w:
                return it
        return None

    # ---------------------------------------------------------------- animation

    def update_targets(self):
        for it in self.items:
            it.target = 1.0
        if self.hover and self.mx is not None and not self.dragging and not self.menu_open:
            R = 2.2 * (self.S + self.gap)
            for it, cx in zip(self.items, self.base_centers()):
                if it.is_sep:
                    continue
                d = abs(self.mx - cx)
                if d < R:
                    it.target = 1.0 + (self.Z - 1.0) * 0.5 * (1.0 + math.cos(math.pi * d / R))

    def kick(self):
        if not self._tick_id:
            self._tick_id = GLib.timeout_add(16, self.tick)

    def tick(self):
        busy = False
        now = time.monotonic()
        for it in self.items:
            if abs(it.scale - it.target) > 0.003:
                it.scale += (it.target - it.scale) * 0.3
                busy = True
            else:
                it.scale = it.target
            if it.bounce_t0 is not None:
                if now - it.bounce_t0 > 2.0 or it.windows:
                    it.bounce_t0 = None
                else:
                    busy = True
        if abs(self.reveal - self.reveal_target) > 0.003:
            self.reveal += (self.reveal_target - self.reveal) * 0.25
            busy = True
        elif self.reveal != self.reveal_target:
            self.reveal = self.reveal_target
            self.set_input_region(self.hover)
        self.layout()
        self.area.queue_draw()
        if not busy:
            self._tick_id = 0
            return False
        return True

    # ------------------------------------------------------------------ drawing

    def on_draw(self, widget, cr):
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        if self.reveal < 0.999:
            # hiding: the whole dock slides below the bottom edge of the window
            cr.translate(0, (1.0 - self.reveal) *
                         (self.panel_h + int(self.cfg["margin_bottom"]) + 8))

        self.layout()
        S = self.S
        total = getattr(self, "total", self.base_w)
        px = (self.win_w - total) / 2.0 - self.pad
        pw = total + 2 * self.pad
        py = self.win_h - self.panel_h

        # panel
        rrect(cr, px, py, pw, self.panel_h, min(float(self.cfg["radius"]), self.panel_h / 2.0))
        bg = hex_rgb(self.cfg["bg_color"], DEFAULTS["bg_color"])
        bd = hex_rgb(self.cfg["border_color"], DEFAULTS["border_color"])
        ind = hex_rgb(self.cfg["indicator_color"], DEFAULTS["indicator_color"])
        cr.set_source_rgba(bg[0], bg[1], bg[2], float(self.cfg["opacity"]))
        cr.fill_preserve()
        cr.set_source_rgba(bd[0], bd[1], bd[2], 0.35)
        cr.set_line_width(1)
        cr.stroke()

        # separator
        if self.has_sep and self.sep_index < len(self.items):
            sx = self.items[self.sep_index].x - SEP / 2.0
            cr.set_source_rgba(bd[0], bd[1], bd[2], 0.3)
            cr.set_line_width(1)
            cr.move_to(sx, py + 12)
            cr.line_to(sx, py + self.panel_h - 12)
            cr.stroke()

        baseline = self.win_h - self.pad
        active_ids = set()
        if self.wnck:
            try:
                aw = self.wnck.get_active_window()
                if aw is not None:
                    active_ids.add(aw.get_xid())
            except Exception:
                pass
        now = time.monotonic()

        for it in self.items:
            if it.is_sep:
                sx = it.x + SEP / 2.0
                if self.dragging and it is self.press_item and self.mx is not None:
                    sx = self.mx
                cr.set_source_rgba(bd[0], bd[1], bd[2], 0.4)
                cr.set_line_width(1)
                cr.move_to(sx, py + 12)
                cr.line_to(sx, py + self.panel_h - 12)
                cr.stroke()
                continue
            s = it.size
            x = it.x + self.gap / 2.0
            y = baseline - s
            if it.bounce_t0 is not None:
                t = now - it.bounce_t0
                y -= S * 0.4 * abs(math.sin(t * math.pi * 2.2)) * max(0.0, 1 - t / 2.0)
            if self.dragging and it is self.press_item and self.mx is not None:
                x = self.mx - s / 2.0
                y -= 6
            pb = it.icon(math.ceil(S * self.Z) if it.scale > 1.02 else S)
            if pb is not None:
                k = s / float(pb.get_width())
                cr.save()
                cr.translate(x, y)
                cr.scale(k, k)
                Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
                cr.paint()
                cr.restore()

            # running-window indicator
            if it.windows:
                n = min(len(it.windows), 3)
                is_active = any(x in active_ids for x in it.windows)
                cx = it.x + it.w / 2.0
                cy = self.win_h - 5
                r = 2.8 if is_active else 2.2
                cr.set_source_rgba(ind[0], ind[1], ind[2], 0.95 if is_active else 0.55)
                for i in range(n):
                    dx = (i - (n - 1) / 2.0) * 8
                    cr.new_sub_path()
                    cr.arc(cx + dx, cy, r, 0, 2 * math.pi)
                    cr.fill()

        # tooltip
        if self.hover and not self.dragging and not self.menu_open:
            it = self.item_at(self.mx)
            if it is not None and not it.is_sep:
                label = it.name
                if len(it.windows) > 1:
                    label += " (%d)" % len(it.windows)
                cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
                cr.set_font_size(13)
                te = cr.text_extents(label)
                bw, bh = te.width + 22, 26
                cx = it.x + it.w / 2.0
                bx = min(max(4, cx - bw / 2.0), self.win_w - bw - 4)
                top = baseline - it.size - 10 - bh
                by = max(2, top)
                rrect(cr, bx, by, bw, bh, 9)
                cr.set_source_rgba(0.07, 0.08, 0.12, 0.9)
                cr.fill_preserve()
                cr.set_source_rgba(1, 1, 1, 0.15)
                cr.set_line_width(1)
                cr.stroke()
                cr.set_source_rgba(1, 1, 1, 0.95)
                cr.move_to(bx + 11 - te.x_bearing, by + bh / 2.0 + te.height / 2.0 - 1)
                cr.show_text(label)
                cr.new_path()
        return False

    # ----------------------------------------------------------------- events

    def on_enter(self, w, ev):
        self.hover = True
        self.mx, self.my = ev.x, ev.y
        self._reveal_now()
        self.set_input_region(True)
        self.update_targets()
        self.kick()
        return False

    def on_leave(self, w, ev):
        if self.press_item is not None:
            return False
        self.hover = False
        self.mx = None
        self.set_input_region(False)
        self.update_targets()
        self.kick()
        self.area.queue_draw()
        self.schedule_hide()
        return False

    def on_motion(self, w, ev):
        self.mx, self.my = ev.x, ev.y
        self.hover = True
        self._reveal_now()
        if (self.press_item is not None and self.press_item.pinned and
                self.press_btn == 1 and not self.dragging and abs(ev.x - self.press_x) > 6):
            self.dragging = True
        if self.dragging:
            self.reorder(ev.x)
        self.update_targets()
        self.kick()
        self.area.queue_draw()
        return False

    def reorder(self, mx):
        pinned = [it for it in self.items if it.pinned]
        if len(pinned) < 2 or self.press_item not in pinned:
            return
        centers = self.base_centers()[:len(pinned)]
        idx = min(range(len(pinned)), key=lambda i: abs(centers[i] - mx))
        cur = pinned.index(self.press_item)
        if idx != cur:
            pinned.insert(idx, pinned.pop(cur))
            extras = [it for it in self.items if not it.pinned]
            self.items = pinned + extras
            self.cfg["pinned"] = [it.key for it in pinned]

    def on_press(self, w, ev):
        if ev.button == 3:
            self.show_menu(self.item_at(ev.x), ev)
            return True
        if ev.button in (1, 2):
            self.press_item = self.item_at(ev.x)
            self.press_x = ev.x
            self.press_btn = ev.button
            self.dragging = False
        return True

    def on_release(self, w, ev):
        it = self.press_item
        was_drag = self.dragging
        self.press_item = None
        self.dragging = False
        if was_drag:
            save_config(self.cfg)
            self.update_targets()
            self.kick()
            return True
        if it is not None and not it.is_sep and it is self.item_at(ev.x):
            if ev.button == 1:
                self.activate_item(it)
            elif ev.button == 2:
                self.launch(it)
        return True

    # ------------------------------------------------------------------- actions

    def launch(self, it, action=None):
        if it.app is None:
            return
        try:
            ctx = Gdk.Display.get_default().get_app_launch_context()
            ctx.set_timestamp(now_ts())
            if action:
                it.app.launch_action(action, ctx)
            else:
                it.app.launch([], ctx)
        except Exception as e:
            print("Cannot launch %s: %s" % (it.name, e), file=sys.stderr)
            return
        if not it.windows:
            it.bounce_t0 = time.monotonic()
            self.kick()

    def live_windows(self, it):
        """Wnck windows for an item, looked up by ID (no references kept)."""
        res = []
        if not self.wnck:
            return res
        for xid in it.windows:
            try:
                w = Wnck.Window.get(xid)
            except Exception:
                w = None
            if w is not None:
                res.append(w)
        return res

    def activate_item(self, it):
        wins = self.live_windows(it)
        if not wins:
            self.launch(it)
            return
        ts = now_ts()
        active = [w for w in wins if w.is_active()]
        if len(wins) == 1:
            w = wins[0]
            if w.is_active() and not w.is_minimized():
                w.minimize()
            else:
                if w.is_minimized():
                    w.unminimize(ts)
                w.activate(ts)
            return
        if active:
            i = wins.index(active[0])
            w = wins[(i + 1) % len(wins)]
        else:
            w = wins[0]
        if w.is_minimized():
            w.unminimize(ts)
        w.activate(ts)

    def apply_settings(self):
        cfg = self.cfg
        self.S = int(cfg["icon_size"])
        self.Z = float(cfg["zoom"])
        self.gap = int(cfg["spacing"])
        self.pad = int(cfg["padding"])
        for it in list(self.cache.values()) + list(self.items):
            it.icons.clear()
        self.apply_geometry(struts=False)
        if self.autohide_on():
            if not self.hover:
                self.schedule_hide()
        else:
            if self._hide_id:
                GLib.source_remove(self._hide_id)
                self._hide_id = 0
            self.reveal = self.reveal_target = 1.0
            self.set_input_region(self.hover)
        self.update_targets()
        self.layout()
        self.area.queue_draw()
        if self._save_id:
            GLib.source_remove(self._save_id)
        self._save_id = GLib.timeout_add(500, self._save_now)
        if self._strut_id:
            GLib.source_remove(self._strut_id)
        self._strut_id = GLib.timeout_add(300, self._strut_now)

    def _save_now(self):
        self._save_id = 0
        save_config(self.cfg)
        return False

    def colors_changed(self):
        self.area.queue_draw()
        if self._save_id:
            GLib.source_remove(self._save_id)
        self._save_id = GLib.timeout_add(500, self._save_now)

    def _strut_now(self):
        self._strut_id = 0
        self.set_struts()
        return False

    def show_settings(self):
        if self.settings_win is not None:
            self.settings_win.present()
            return
        self.settings_win = SettingsWindow(self)
        self.settings_win.connect("destroy", self._settings_closed)

    def _settings_closed(self, *_):
        self.settings_win = None
        self.schedule_hide()

    def pin(self, it):
        if it.app is not None and it.key not in self.cfg["pinned"]:
            self.cfg["pinned"].append(it.app.get_id())
            save_config(self.cfg)
            self.cache.pop(it.key, None)
            self.refresh_items()

    def unpin(self, it):
        if it.key in self.cfg["pinned"]:
            self.cfg["pinned"].remove(it.key)
            save_config(self.cfg)
            self.cache.pop(it.key, None)
            self.refresh_items()

    def add_separator_after(self, key):
        keys = [x.key for x in self.items if x.pinned]
        if key not in keys:
            return
        keys.insert(keys.index(key) + 1, SEP_KEY)
        self.cfg["pinned"] = keys
        save_config(self.cfg)
        self.refresh_items()

    def remove_separator(self, ordinal):
        seps = [x for x in self.items if x.is_sep]
        if ordinal >= len(seps):
            return
        target = seps[ordinal]
        self.cfg["pinned"] = [x.key for x in self.items if x.pinned and x is not target]
        save_config(self.cfg)
        self.refresh_items()

    def close_windows(self, it):
        ts = now_ts()
        for w in self.live_windows(it):
            try:
                w.close(ts)
            except Exception:
                pass

    def show_menu(self, it, ev):
        menu = Gtk.Menu()

        def add(label, cb):
            mi = Gtk.MenuItem(label=label)
            mi.connect("activate", lambda *_: cb())
            menu.append(mi)

        if it is not None and it.is_sep:
            n = [x for x in self.items if x.is_sep].index(it)
            add("Remove separator", lambda: self.remove_separator(n))
            menu.append(Gtk.SeparatorMenuItem())
        elif it is not None:
            add("Open" if not it.windows else "New window", lambda: self.launch(it))
            if it.app is not None:
                try:
                    for a in it.app.list_actions():
                        add(it.app.get_action_name(a), lambda a=a: self.launch(it, a))
                except Exception:
                    pass
            if it.windows:
                add("Close all windows", lambda: self.close_windows(it))
            menu.append(Gtk.SeparatorMenuItem())
            if it.pinned:
                add("Remove from dock", lambda: self.unpin(it))
                add("Add separator after", lambda: self.add_separator_after(it.key))
            elif it.app is not None:
                add("Pin to dock", lambda: self.pin(it))
            menu.append(Gtk.SeparatorMenuItem())
        add("Settings…", self.show_settings)
        add("Quit dock", Gtk.main_quit)

        self.menu = menu
        self.menu_open = True
        menu.connect("deactivate", self._menu_closed)
        menu.show_all()
        self.update_targets()
        self.kick()
        menu.popup_at_pointer(ev)

    def _menu_closed(self, *args):
        self.menu_open = False
        self.update_targets()
        self.kick()
        self.area.queue_draw()
        self.schedule_hide()



# ------------------------------------------------------------ settings window

SETTINGS_ROWS = [
    ("icon_size", "Icon size", 24, 96, 2, 0),
    ("zoom", "Hover zoom (1.0 = no zoom)", 1.0, 2.0, 0.05, 2),
    ("spacing", "Spacing between icons", 0, 24, 1, 0),
    ("padding", "Panel inner padding", 2, 24, 1, 0),
    ("radius", "Corner radius", 0, 32, 1, 0),
    ("opacity", "Background opacity", 0.0, 1.0, 0.05, 2),
    ("margin_bottom", "Dock distance from bottom edge", 0, 80, 1, 0),
    ("window_gap", "Gap between windows and dock", 0, 80, 1, 0),
]

SETTINGS_COLORS = [
    ("bg_color", "Dock color"),
    ("border_color", "Border and separator color"),
    ("indicator_color", "App indicator color"),
]

SETTINGS_SWITCHES = [
    ("autohide", "Auto-hide (mouse at the bottom edge reveals it)"),
    ("reserve_space", "Windows do not go under the dock"),
    ("show_running", "Also show running apps that are not pinned"),
]


class SettingsWindow(Gtk.Window):
    def __init__(self, dock):
        super().__init__(title="NiDock - settings")
        self.dock = dock
        self.widgets = {}
        self._loading = False
        self.set_border_width(18)
        self.set_resizable(False)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)

        grid = Gtk.Grid(column_spacing=16, row_spacing=10)
        row = 0
        for key, label, lo, hi, step, digits in SETTINGS_ROWS:
            lab = Gtk.Label(label=label)
            lab.set_halign(Gtk.Align.START)
            scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, step)
            scale.set_digits(digits)
            scale.set_value_pos(Gtk.PositionType.RIGHT)
            scale.set_size_request(240, -1)
            scale.set_hexpand(True)
            scale.set_value(float(dock.cfg[key]))
            scale.connect("value-changed", self.on_scale, key, digits)
            grid.attach(lab, 0, row, 1, 1)
            grid.attach(scale, 1, row, 1, 1)
            self.widgets[key] = scale
            row += 1

        for key, label in SETTINGS_SWITCHES:
            lab = Gtk.Label(label=label)
            lab.set_halign(Gtk.Align.START)
            sw = Gtk.Switch()
            sw.set_halign(Gtk.Align.START)
            sw.set_active(bool(dock.cfg[key]))
            sw.connect("notify::active", self.on_switch, key)
            grid.attach(lab, 0, row, 1, 1)
            grid.attach(sw, 1, row, 1, 1)
            self.widgets[key] = sw
            row += 1

        lab = Gtk.Label(label="Color theme")
        lab.set_halign(Gtk.Align.START)
        combo = Gtk.ComboBoxText()
        combo.append_text("— choose —")
        for name in PRESETS:
            combo.append_text(name)
        combo.set_active(0)
        combo.connect("changed", self.on_preset)
        grid.attach(lab, 0, row, 1, 1)
        grid.attach(combo, 1, row, 1, 1)
        row += 1

        for key, label in SETTINGS_COLORS:
            lab = Gtk.Label(label=label)
            lab.set_halign(Gtk.Align.START)
            btn = Gtk.ColorButton()
            btn.set_use_alpha(False)
            btn.set_rgba(self._rgba(dock.cfg[key]))
            btn.set_halign(Gtk.Align.START)
            btn.connect("color-set", self.on_color, key)
            grid.attach(lab, 0, row, 1, 1)
            grid.attach(btn, 1, row, 1, 1)
            self.widgets[key] = btn
            row += 1

        for label, state, setter in (
                ("Start dock at login", os.path.exists(AUTOSTART_FILE), set_autostart),
                ("Show NiDock in the applications menu", os.path.exists(MENU_FILE), set_menu_entry)):
            lab = Gtk.Label(label=label)
            lab.set_halign(Gtk.Align.START)
            sw = Gtk.Switch()
            sw.set_halign(Gtk.Align.START)
            sw.set_active(state)
            sw.connect("notify::active", lambda s, _p, fn=setter: fn(s.get_active()))
            grid.attach(lab, 0, row, 1, 1)
            grid.attach(sw, 1, row, 1, 1)
            row += 1

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        buttons.set_halign(Gtk.Align.END)
        b_reset = Gtk.Button(label="Reset to defaults")
        b_reset.connect("clicked", self.on_reset)
        b_close = Gtk.Button(label="Close")
        b_close.connect("clicked", lambda *_: self.destroy())
        buttons.pack_start(b_reset, False, False, 0)
        buttons.pack_start(b_close, False, False, 0)
        grid.attach(buttons, 0, row, 2, 1)

        self.add(grid)
        self.connect("destroy", self.on_destroy)
        self.show_all()

    @staticmethod
    def _rgba(value):
        c = Gdk.RGBA()
        if not c.parse(str(value)):
            c.parse("#000000")
        return c

    def on_color(self, btn, key):
        c = btn.get_rgba()
        self.dock.cfg[key] = "#%02x%02x%02x" % (
            round(c.red * 255), round(c.green * 255), round(c.blue * 255))
        self.dock.colors_changed()

    def on_preset(self, combo):
        preset = PRESETS.get(combo.get_active_text())
        if not preset:
            return
        for key, val in preset.items():
            self.dock.cfg[key] = val
            self.widgets[key].set_rgba(self._rgba(val))
        self.dock.colors_changed()

    def on_scale(self, scale, key, digits):
        if self._loading:
            return
        v = scale.get_value()
        self.dock.cfg[key] = int(round(v)) if digits == 0 else round(v, 2)
        self.dock.apply_settings()

    def on_switch(self, sw, _param, key):
        if self._loading:
            return
        self.dock.cfg[key] = bool(sw.get_active())
        self.dock.apply_settings()
        if key == "show_running":
            self.dock.refresh_items()

    def on_reset(self, *_):
        self._loading = True
        for key, _l, *_rest in SETTINGS_ROWS:
            self.dock.cfg[key] = DEFAULTS[key]
            self.widgets[key].set_value(float(DEFAULTS[key]))
        for key, _l in SETTINGS_SWITCHES:
            self.dock.cfg[key] = DEFAULTS[key]
            self.widgets[key].set_active(bool(DEFAULTS[key]))
        for key, _l in SETTINGS_COLORS:
            self.dock.cfg[key] = DEFAULTS[key]
            self.widgets[key].set_rgba(self._rgba(DEFAULTS[key]))
        self._loading = False
        self.dock.apply_settings()
        self.dock.refresh_items()

    def on_destroy(self, *_):
        if self.dock._save_id:
            GLib.source_remove(self.dock._save_id)
            self.dock._save_id = 0
        save_config(self.dock.cfg)


# ----------------------------------------------------------------------- main

def single_instance(open_settings=False):
    runtime = GLib.get_user_runtime_dir() or "/tmp"
    fd = os.open(os.path.join(runtime, "nidock.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    lock = os.fdopen(fd, "r+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        if open_settings:  # the dock is already running: just tell it to open settings
            try:
                os.kill(int(lock.read().strip()), signal.SIGUSR1)
            except (ValueError, OSError):
                pass
        else:
            print("NiDock is already running.")
        sys.exit(0)
    lock.truncate(0)
    lock.write(str(os.getpid()))
    lock.flush()
    return lock


def main():
    args = sys.argv[1:]
    if "--install" in args:
        install()
        return
    if "--uninstall" in args:
        uninstall()
        return
    if "--supervise" in args:
        supervise()
        return
    faulthandler.enable()
    want_settings = "--settings" in sys.argv[1:]
    lock = single_instance(want_settings)  # noqa: F841 (reference must be kept)
    cfg = load_config()
    if not os.path.exists(CONFIG_FILE):
        save_config(cfg)
    dock = Dock(cfg)

    def on_usr1():
        dock.show_settings()
        return True  # stay registered

    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1, on_usr1)
    if want_settings:
        GLib.idle_add(lambda: (dock.show_settings(), False)[1])
    Gtk.main()


if __name__ == "__main__":
    main()
