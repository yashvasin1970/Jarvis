"""Jarvis "open things on this PC" tools, as a minimal MCP server over stdio.

Tools: open_app, list_apps, open_folder, open_file, open_website, open_settings,
and (added 2026-11-07) volume, media_control, brightness, take_screenshot,
lock_pc, battery_status, set_reminder, set_timer, list_reminders, cancel_reminder.

Reminders live in OpenJarvis\\data\\reminders.json and are checked every second
by a background thread here. When one is due: a Windows notification, plus the
"Jarvis" voice bar says it (via a file dropped in voice-popup\\inbox).

Safety by design: apps are opened only from the Windows Start menu list
(Get-StartApps), never by executable path; files are opened with their default
app but programs, scripts, installers and shortcuts are refused; websites must
be http(s). Nothing here runs arbitrary commands.

Standard library only, so Jarvis updates cannot break it. Launched by Jarvis
(see [tools.mcp] in ~/.openjarvis/config.toml); speaks JSON-RPC 2.0, one message
per line, on stdin/stdout.
"""

from __future__ import annotations

import base64
import ctypes
import difflib
import json
import os
import re
import struct
import subprocess
import sys
import threading
import time
import uuid
import zlib
from ctypes import wintypes
from datetime import datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

PROTOCOL = "2025-06-18"
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW
# The Jarvis folder (this file is in its pc-tools\ subfolder), so the same file works
# wherever Jarvis is installed (also from the setup kit for other PCs).
ROOT = Path(__file__).resolve().parent.parent
SETTINGS_FILE = ROOT / "JARVIS-SETTINGS.txt"


def _setting(name: str, default: str = "") -> str:
    """A value from JARVIS-SETTINGS.txt (lines like CITY=Chennai; # starts a comment)."""
    try:
        for line in SETTINGS_FILE.read_text(encoding="utf-8-sig").splitlines():
            key, sep, value = line.partition("=")
            if sep and not key.strip().startswith("#") and key.strip().upper() == name:
                return value.strip()
    except OSError:
        pass
    return default


REMINDERS_FILE = ROOT / "data" / "reminders.json"
VOICE_INBOX = ROOT / "voice-popup" / "inbox"
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)   # full-resolution screenshots
except Exception:  # noqa: BLE001
    pass

BLOCKED_EXT = {
    ".exe", ".com", ".bat", ".cmd", ".ps1", ".psm1", ".psd1", ".vbs", ".vbe", ".js",
    ".jse", ".wsf", ".wsh", ".ws", ".msi", ".msp", ".mst", ".scr", ".pif", ".cpl",
    ".hta", ".jar", ".lnk", ".url", ".reg", ".dll", ".sys", ".drv", ".ocx", ".appx",
    ".appxbundle", ".msix", ".msixbundle", ".appinstaller", ".application", ".gadget",
    ".inf", ".scf", ".msc", ".iso", ".img", ".vhd", ".vhdx", ".library-ms",
    ".settingcontent-ms", ".py", ".pyw", ".sh",
}

SETTINGS_PAGES = {
    "settings": "ms-settings:", "home": "ms-settings:", "wifi": "ms-settings:network-wifi",
    "wi-fi": "ms-settings:network-wifi", "network": "ms-settings:network-status",
    "internet": "ms-settings:network-status", "bluetooth": "ms-settings:bluetooth",
    "display": "ms-settings:display", "brightness": "ms-settings:display",
    "sound": "ms-settings:sound", "volume": "ms-settings:sound",
    "notifications": "ms-settings:notifications", "battery": "ms-settings:batterysaver",
    "power": "ms-settings:powersleep", "storage": "ms-settings:storagesense",
    "apps": "ms-settings:appsfeatures", "default apps": "ms-settings:defaultapps",
    "startup apps": "ms-settings:startupapps", "updates": "ms-settings:windowsupdate",
    "windows update": "ms-settings:windowsupdate", "privacy": "ms-settings:privacy",
    "microphone": "ms-settings:privacy-microphone", "camera": "ms-settings:privacy-webcam",
    "personalization": "ms-settings:personalization",
    "background": "ms-settings:personalization-background",
    "themes": "ms-settings:themes", "dark mode": "ms-settings:colors",
    "colors": "ms-settings:colors", "mouse": "ms-settings:mousetouchpad",
    "touchpad": "ms-settings:devices-touchpad", "printers": "ms-settings:printers",
    "keyboard": "ms-settings:keyboard", "language": "ms-settings:regionlanguage",
    "time": "ms-settings:dateandtime", "date": "ms-settings:dateandtime",
    "accounts": "ms-settings:yourinfo", "vpn": "ms-settings:network-vpn",
    "about": "ms-settings:about", "night light": "ms-settings:nightlight",
}

# Folder name -> Windows Known Folder ID (resolved with SHGetKnownFolderPath,
# so OneDrive-redirected folders resolve correctly).
KNOWN_FOLDERS = {
    "desktop": "B4BFCC3A-DB2C-424C-B029-7FE99A87C641",
    "documents": "FDD39AD0-238F-46AF-ADB4-6C85480369C7",
    "downloads": "374DE290-123F-4565-9164-39C4925E467B",
    "pictures": "33E28130-4E1E-4676-835A-98395C3BC3BB",
    "music": "4BD8D571-6D19-48D3-BE97-422220080E43",
    "videos": "18989B1D-99B5-455B-841C-AB7C74E4DDFC",
}
SHELL_FOLDERS = {
    "this pc": "shell:MyComputerFolder", "my computer": "shell:MyComputerFolder",
    "recycle bin": "shell:RecycleBinFolder", "recent": "shell:Recent",
    "screenshots": "shell:Screenshots", "onedrive": "shell:OneDrive",
}
ALIASES = {"docs": "documents", "download": "downloads", "photos folder": "pictures",
           "videos folder": "videos", "computer": "this pc", "trash": "recycle bin",
           "bin": "recycle bin"}


def known_folder(fid: str) -> str | None:
    class GUID(ctypes.Structure):
        _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort), ("c", ctypes.c_ushort),
                    ("d", ctypes.c_ubyte * 8)]

    u = uuid.UUID(fid)
    g = GUID(u.fields[0], u.fields[1], u.fields[2],
             (ctypes.c_ubyte * 8)(*u.bytes[8:]))
    p = ctypes.c_wchar_p()
    if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(p)) != 0:
        return None
    try:
        return p.value
    finally:
        ctypes.windll.ole32.CoTaskMemFree(p)


_apps: list[tuple[str, str]] = []
_apps_at = 0.0


def start_apps() -> list[tuple[str, str]]:
    """[(name, AppID)] from Get-StartApps, cached for 10 minutes."""
    global _apps, _apps_at
    if _apps and time.time() - _apps_at < 600:
        return _apps
    out = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
         "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
         "Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress"],
        capture_output=True, text=True, encoding="utf-8", timeout=60, creationflags=NO_WINDOW,
    ).stdout
    data = json.loads(out or "[]")
    if isinstance(data, dict):
        data = [data]
    _apps = [(d["Name"], d["AppID"]) for d in data if d.get("Name") and d.get("AppID")]
    _apps_at = time.time()
    return _apps


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", s.lower()).strip()


def rank_apps(query: str) -> list[tuple[float, str, str]]:
    q = norm(query)
    q = re.sub(r"^(the|my)\s+", "", q)
    q = re.sub(r"\s+app$", "", q)
    scored = []
    for name, appid in start_apps():
        n = norm(name)
        if not n or "uninstall" in n:
            continue
        if n == q:
            s = 1.0
        elif n.startswith(q + " ") or n.startswith(q):
            s = 0.92 - min(len(n) - len(q), 30) / 300
        elif re.search(rf"\b{re.escape(q)}\b", n):
            s = 0.85 - min(len(n) - len(q), 30) / 300
        else:
            s = difflib.SequenceMatcher(None, q, n).ratio() * 0.8
        scored.append((s, name, appid))
    scored.sort(reverse=True)
    return scored


def launch(target: str) -> None:
    subprocess.Popen(["explorer.exe", target], creationflags=NO_WINDOW)


# ---- tools -------------------------------------------------------------------

def tool_open_app(name: str) -> str:
    ranked = rank_apps(name)
    if not ranked or ranked[0][0] < 0.6:
        tips = ", ".join(n for _, n, _ in ranked[:5])
        return f"No installed app matches '{name}'. Closest: {tips or 'none'}."
    _, app, appid = ranked[0]
    launch(f"shell:AppsFolder\\{appid}")
    return f"Opened {app}."


def tool_list_apps(query: str = "") -> str:
    if query:
        names = [n for s, n, _ in rank_apps(query)[:10] if s >= 0.4]
    else:
        names = sorted({n for n, _ in start_apps()}, key=str.lower)
    return ", ".join(names) if names else "No matching apps."


def tool_open_folder(folder: str) -> str:
    key = ALIASES.get(norm(folder), norm(folder))
    key = re.sub(r"\s+folder$", "", key)
    if key in KNOWN_FOLDERS:
        path = known_folder(KNOWN_FOLDERS[key])
        if path:
            launch(path)
            return f"Opened your {key.title()} folder ({path})."
    if key in SHELL_FOLDERS:
        launch(SHELL_FOLDERS[key])
        return f"Opened {key.title()}."
    p = Path(os.path.expandvars(os.path.expanduser(folder.strip().strip('"'))))
    if str(p).startswith("\\\\"):
        return "Network folders are not allowed."
    if p.is_dir():
        launch(str(p.resolve()))
        return f"Opened folder {p.resolve()}."
    return (f"Couldn't find the folder '{folder}'. Known names: "
            + ", ".join(list(KNOWN_FOLDERS) + list(SHELL_FOLDERS)) + ", or a full folder path.")


def tool_open_file(path: str) -> str:
    p = Path(os.path.expandvars(os.path.expanduser(path.strip().strip('"'))))
    if str(p).startswith("\\\\"):
        return "Files on network shares are not allowed."
    if not p.is_file():
        return f"File not found: {p}"
    if p.suffix.lower() in BLOCKED_EXT:
        return (f"For safety I don't open {p.suffix} files (programs, scripts, installers "
                "and shortcuts). Ask me to open the app by name instead.")
    os.startfile(str(p.resolve()))
    return f"Opened {p.name} with its default app."


def tool_open_website(url: str) -> str:
    u = url.strip()
    if not re.match(r"^https?://", u, re.I):
        if re.match(r"^[\w.-]+\.[a-z]{2,}(/.*)?$", u, re.I):
            u = "https://" + u
        else:
            return "Please give a website address like https://example.com."
    os.startfile(u)
    return f"Opened {u} in your browser."


def tool_open_settings(page: str = "settings") -> str:
    key = norm(page)
    uri = SETTINGS_PAGES.get(key)
    if not uri:
        best = difflib.get_close_matches(key, SETTINGS_PAGES, n=1, cutoff=0.6)
        uri = SETTINGS_PAGES[best[0]] if best else None
    if not uri and re.fullmatch(r"ms-settings:[a-z0-9-]*", page.strip().lower()):
        uri = page.strip().lower()
    if not uri:
        return "Unknown settings page. Try: " + ", ".join(sorted(SETTINGS_PAGES))
    os.startfile(uri)
    return f"Opened Windows Settings ({page})."


# ---- sound, media, screen, power (added 2026-11-07) ------------------------------

class GUID(ctypes.Structure):
    _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort), ("c", ctypes.c_ushort),
                ("d", ctypes.c_ubyte * 8)]

    @classmethod
    def of(cls, s: str) -> "GUID":
        u = uuid.UUID(s)
        return cls(u.fields[0], u.fields[1], u.fields[2], (ctypes.c_ubyte * 8)(*u.bytes[8:]))


def _vcall(ptr, index, argtypes, *args):
    """Call method `index` of a COM interface pointer (raw vtable call)."""
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    fn = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)(vtbl[index])
    return fn(ptr, *args)


def _endpoint_volume():
    """IAudioEndpointVolume of the default speakers (Windows Core Audio)."""
    ctypes.windll.ole32.CoInitialize(None)
    enum = ctypes.c_void_p()
    hr = ctypes.windll.ole32.CoCreateInstance(
        ctypes.byref(GUID.of("BCDE0395-E52F-467C-8E3D-C4579291692E")), None, 23,
        ctypes.byref(GUID.of("A95664D2-9614-4F35-A746-DE8DB63617E6")), ctypes.byref(enum))
    if hr != 0:
        raise OSError(f"audio unavailable ({hr & 0xFFFFFFFF:#x})")
    dev, vol = ctypes.c_void_p(), ctypes.c_void_p()
    try:
        _vcall(enum, 4, (ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)),
               0, 1, ctypes.byref(dev))                                   # GetDefaultAudioEndpoint
        _vcall(dev, 3, (ctypes.POINTER(GUID), ctypes.c_uint, ctypes.c_void_p,
                        ctypes.POINTER(ctypes.c_void_p)),
               ctypes.byref(GUID.of("5CDF2C82-841E-4546-9722-0CF74078229A")), 23, None,
               ctypes.byref(vol))                                         # Activate
    finally:
        for p in (dev, enum):
            if p:
                _vcall(p, 2, ())                                          # Release
    return vol


def _get_volume(vol) -> tuple[int, bool]:
    level, muted = ctypes.c_float(), wintypes.BOOL()
    _vcall(vol, 9, (ctypes.POINTER(ctypes.c_float),), ctypes.byref(level))   # GetMasterVolumeLevelScalar
    _vcall(vol, 15, (ctypes.POINTER(wintypes.BOOL),), ctypes.byref(muted))   # GetMute
    return round(level.value * 100), bool(muted.value)


def _press(vk: int, times: int = 1) -> None:
    for _ in range(times):
        ctypes.windll.user32.keybd_event(vk, 0, 1, 0)        # KEYEVENTF_EXTENDEDKEY
        ctypes.windll.user32.keybd_event(vk, 0, 1 | 2, 0)    # ... | KEYEVENTF_KEYUP


def tool_volume(action: str = "get", level: int | None = None, step: int = 10) -> str:
    action = (action or "get").lower().strip()
    try:
        vol = _endpoint_volume()
    except Exception:  # noqa: BLE001 - fall back to the keyboard's volume keys
        vol = None
    if vol is None:
        keys = {"up": (0xAF, max(1, step // 2)), "down": (0xAE, max(1, step // 2)),
                "mute": (0xAD, 1), "unmute": (0xAD, 1)}
        if action == "set" and level is not None:
            _press(0xAE, 50)
            _press(0xAF, max(0, min(100, int(level))) // 2)
            return f"Volume set to about {level}%."
        if action in keys:
            _press(*keys[action])
            return f"Volume {action}."
        return "Couldn't read the volume on this PC."
    try:
        now, muted = _get_volume(vol)
        if action in ("set", "up", "down"):
            new = int(level) if action == "set" and level is not None else now + (
                step if action == "up" else -step)
            new = max(0, min(100, new))
            _vcall(vol, 7, (ctypes.c_float, ctypes.c_void_p), ctypes.c_float(new / 100), None)
            if muted and new > 0:
                _vcall(vol, 14, (wintypes.BOOL, ctypes.c_void_p), False, None)
            return f"Volume is now {new}% (was {now}%{', muted' if muted else ''})."
        if action in ("mute", "unmute"):
            _vcall(vol, 14, (wintypes.BOOL, ctypes.c_void_p), action == "mute", None)
            return "Sound muted." if action == "mute" else f"Sound unmuted (volume {now}%)."
        return f"Volume is {now}%{' and muted' if muted else ''}."
    finally:
        _vcall(vol, 2, ())


def tool_media_control(action: str) -> str:
    keys = {"play": 0xB3, "pause": 0xB3, "play_pause": 0xB3, "next": 0xB0, "previous": 0xB1,
            "stop": 0xB2}
    a = (action or "").lower().strip().replace(" ", "_").replace("-", "_")
    a = {"resume": "play", "skip": "next", "prev": "previous", "back": "previous"}.get(a, a)
    if a not in keys:
        return "Unknown action. Use play, pause, next, previous or stop."
    _press(keys[a])
    return {"next": "Skipped to the next track.", "previous": "Went back to the previous track.",
            "stop": "Stopped playback."}.get(a, "Pressed play/pause.")


def _powershell(script: str, timeout: int = 30) -> str:
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
                       capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW)
    return (r.stdout or "").strip()


def tool_brightness(action: str = "get", level: int | None = None, step: int = 10) -> str:
    now = _powershell("(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness "
                      "-ErrorAction Stop | Select-Object -First 1).CurrentBrightness")
    if not now.isdigit():
        return "This screen's brightness can't be changed from Windows (e.g. an external monitor)."
    cur = int(now)
    action = (action or "get").lower().strip()
    if action == "get":
        return f"Screen brightness is {cur}%."
    new = int(level) if action == "set" and level is not None else cur + (step if action == "up" else -step)
    new = max(5, min(100, new))
    _powershell("$m = Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods | "
                "Select-Object -First 1; Invoke-CimMethod -InputObject $m -MethodName WmiSetBrightness "
                f"-Arguments @{{Timeout=1; Brightness=[byte]{new}}} | Out-Null")
    return f"Screen brightness is now {new}% (was {cur}%)."


def _desktop() -> Path:
    return Path(known_folder(KNOWN_FOLDERS["desktop"]) or (Path.home() / "Desktop"))


def tool_take_screenshot() -> str:
    u32, gdi = ctypes.windll.user32, ctypes.windll.gdi32
    x, y = u32.GetSystemMetrics(76), u32.GetSystemMetrics(77)      # whole virtual screen
    w, h = u32.GetSystemMetrics(78), u32.GetSystemMetrics(79)
    gdi.CreateCompatibleBitmap.restype = gdi.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi.CreateCompatibleDC.argtypes = (ctypes.c_void_p,)
    gdi.CreateCompatibleBitmap.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_int)
    gdi.SelectObject.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    gdi.BitBlt.argtypes = (ctypes.c_void_p,) + (ctypes.c_int,) * 4 + (ctypes.c_void_p,) + (ctypes.c_int,) * 2 + (ctypes.c_uint,)
    gdi.GetDIBits.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
                              ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint)
    gdi.DeleteObject.argtypes = gdi.DeleteDC.argtypes = (ctypes.c_void_p,)
    u32.GetDC.restype = ctypes.c_void_p
    u32.ReleaseDC.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    screen = u32.GetDC(None)
    mem = gdi.CreateCompatibleDC(screen)
    bmp = gdi.CreateCompatibleBitmap(screen, w, h)
    try:
        gdi.SelectObject(mem, bmp)
        gdi.BitBlt(mem, 0, 0, w, h, screen, x, y, 0x00CC0020 | 0x40000000)   # SRCCOPY | CAPTUREBLT
        header = struct.pack("<IiiHHIIiiII", 40, w, -h, 1, 32, 0, 0, 0, 0, 0, 0)  # top-down BGRA
        info = ctypes.create_string_buffer(header + b"\0" * 16)
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi.GetDIBits(mem, bmp, 0, h, buf, info, 0)
    finally:
        gdi.DeleteObject(bmp)
        gdi.DeleteDC(mem)
        u32.ReleaseDC(None, screen)
    data, raw, stride = buf.raw, bytearray(), w * 4
    for r in range(h):
        row = data[r * stride:(r + 1) * stride]
        rgb = bytearray(w * 3)
        rgb[0::3], rgb[1::3], rgb[2::3] = row[2::4], row[1::4], row[0::4]
        raw += b"\0" + rgb

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))
    folder = _desktop() / "Screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"Screenshot {datetime.now():%Y-%m-%d %H.%M.%S}.png"
    path.write_bytes(png)
    return (f"Screenshot taken ({w}x{h}). [Internal file path, for your use only: {path}] "
            "Don't mention where it is saved unless the user asks.")


def tool_lock_pc() -> str:
    ctypes.windll.user32.LockWorkStation()
    return "Locked the PC."


def tool_battery_status() -> str:
    class SPS(ctypes.Structure):
        _fields_ = [("ac", ctypes.c_ubyte), ("flag", ctypes.c_ubyte), ("pct", ctypes.c_ubyte),
                    ("saver", ctypes.c_ubyte), ("left", ctypes.c_ulong), ("full", ctypes.c_ulong)]

    s = SPS()
    ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s))
    if s.flag == 128 or s.pct == 255:
        return "This PC has no battery (or it can't be read)."
    plug = "plugged in" if s.ac == 1 else "on battery"
    charging = ", charging" if s.flag & 8 else ""
    left = ""
    if s.ac != 1 and s.left not in (0xFFFFFFFF, 0):
        left = f", about {s.left // 3600} h {s.left % 3600 // 60} min left"
    return f"Battery is at {s.pct}%, {plug}{charging}{left}." + (" Battery saver is on." if s.saver else "")


# ---- clipboard (added 2026-11-07) -----------------------------------------------------------
# Through PowerShell's clipboard cmdlets (STA, UTF-8 both ways, text passed on stdin so
# any language and any length up to the cap works).

def _ps_stdin(script: str, data: str = "", timeout: int = 20) -> str:
    r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-STA", "-Command",
                        "[Console]::InputEncoding=[Text.Encoding]::UTF8;"
                        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;" + script],
                       input=data, capture_output=True, text=True, encoding="utf-8", timeout=timeout,
                       creationflags=NO_WINDOW)
    return r.stdout or ""


def tool_read_clipboard() -> str:
    text = _ps_stdin("$t = Get-Clipboard -Raw; if ($t) { [Console]::Out.Write($t) }")
    if not text.strip():
        return "The clipboard is empty (or holds a picture/file, not text)."
    cut = len(text) > 20000
    return ("Text the user copied" + (" (first part only)" if cut else "") + ":\n" + text[:20000])


def tool_write_clipboard(text: str) -> str:
    if not text:
        return "Nothing to copy."
    _ps_stdin("$t = [Console]::In.ReadToEnd(); Set-Clipboard -Value $t", text[:100000])
    return f"Copied to the clipboard ({len(text)} characters) - the user can paste it with Ctrl+V."


# ---- weather and briefing (added 2026-11-07) ------------------------------------------------
# Open-Meteo: free, no key or sign-up. Home city: CITY= in JARVIS-SETTINGS.txt (read at start).
HOME_CITY = _setting("CITY")
WMO = {0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
       51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
       61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
       71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers",
       81: "showers", 82: "violent showers", 85: "snow showers", 86: "heavy snow showers",
       95: "thunderstorm", 96: "thunderstorm with hail", 99: "severe thunderstorm with hail"}


def _get_json(url: str, timeout: int = 20) -> dict:
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "Jarvis-home-assistant"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def tool_get_weather(city: str = "", days: int = 1) -> str:
    import urllib.parse

    city = (city or HOME_CITY).strip()
    if not city:
        return ("No home city is set. Ask the user which city they mean (they can also save it as "
                "CITY=... in JARVIS-SETTINGS.txt in the Jarvis folder, then restart Jarvis).")
    geo = _get_json("https://geocoding-api.open-meteo.com/v1/search?count=1&language=en&name="
                    + urllib.parse.quote(city))
    if not geo.get("results"):
        return f"Couldn't find a place called '{city}'."
    g = geo["results"][0]
    days = max(1, min(7, int(days or 1)))
    w = _get_json(
        "https://api.open-meteo.com/v1/forecast?timezone=auto"
        f"&latitude={g['latitude']}&longitude={g['longitude']}&forecast_days={days}"
        "&current=temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m"
        "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
        "uv_index_max,sunrise,sunset")
    c, d = w["current"], w["daily"]
    place = ", ".join(x for x in (g.get("name"), g.get("admin1"), g.get("country")) if x)
    lines = [f"Weather for {place} now: {WMO.get(c['weather_code'], 'unknown')}, {c['temperature_2m']:.0f}°C "
             f"(feels like {c['apparent_temperature']:.0f}°C), humidity {c['relative_humidity_2m']}%, "
             f"wind {c['wind_speed_10m']:.0f} km/h."]
    for i in range(len(d["time"])):
        day = "Today" if i == 0 else datetime.strptime(d["time"][i], "%Y-%m-%d").strftime("%A")
        lines.append(f"{day}: {WMO.get(d['weather_code'][i], 'unknown')}, {d['temperature_2m_min'][i]:.0f}-"
                     f"{d['temperature_2m_max'][i]:.0f}°C, chance of rain {d['precipitation_probability_max'][i]}%, "
                     f"UV {d['uv_index_max'][i]:.0f}, sunrise {d['sunrise'][i][-5:]}, sunset {d['sunset'][i][-5:]}.")
    return "\n".join(lines)


def tool_morning_briefing() -> str:
    now = datetime.now()
    parts = [f"Date: {now:%A %d %B %Y}, time now {now:%I:%M %p}."]
    try:
        if HOME_CITY:
            parts.append(tool_get_weather(HOME_CITY, 1))
        else:
            parts.append("(No home city is set, so no weather. Ask the user for their city if they want it.)")
    except Exception as exc:  # noqa: BLE001
        parts.append(f"(Weather unavailable: {exc.__class__.__name__})")
    end = datetime(now.year, now.month, now.day) + timedelta(days=1)
    with _rem_lock:
        today = [r for r in _load_reminders() if r["due"] < end.timestamp()]
    parts.append("Reminders and timers still due today: " + (
        "; ".join(f"{_when(r['due'])}: {r['text']}" for r in sorted(today, key=lambda r: r["due"]))
        if today else "none."))
    parts.append(
        "TO FINISH THE BRIEFING: if a calendar_events tool is available, call it for today; if a "
        "check_email tool is available, check unread email from the last day and mention only "
        "important-looking ones (sender + subject). Then web_search for today's top news (India and "
        "world) and pick 3 headlines. Present it all as one short, friendly spoken-style briefing: "
        "greeting, weather (with an umbrella/sunscreen tip if useful), calendar, reminders, email, "
        "news. No lists of links.")
    return "\n".join(parts)


# ---- reminders and timers --------------------------------------------------------------

_rem_lock = threading.Lock()


def _load_reminders() -> list[dict]:
    try:
        return json.loads(REMINDERS_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - missing or damaged: start empty
        return []


def _save_reminders(items: list[dict]) -> None:
    REMINDERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = REMINDERS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(REMINDERS_FILE)


def _when(due: float) -> str:
    d = datetime.fromtimestamp(due)
    now = datetime.now()
    clock = d.strftime("%I:%M %p").lstrip("0")
    if d.date() == now.date():
        return f"today at {clock}"
    if d.date() == (now + timedelta(days=1)).date():
        return f"tomorrow at {clock}"
    return d.strftime("%a %d %b at ") + clock


def _parse_at(at: str) -> datetime:
    """'17:30', '5:30 pm', '5 pm', '2026-11-08 09:00', 'tomorrow 9am' -> next such local time."""
    s = at.strip().lower().replace(".", ":")
    day = None
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        day = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        s = s[:m.start()] + s[m.end():]
    tomorrow = "tomorrow" in s
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", s)
    if not m:
        raise ValueError(f"Couldn't understand the time '{at}'. Use e.g. '17:30' or '2026-11-08 09:00'.")
    hh, mm, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap == "pm" and hh < 12:
        hh += 12
    if ap == "am" and hh == 12:
        hh = 0
    now = datetime.now()
    base = day or now
    t = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if tomorrow:
        t += timedelta(days=1)
    elif day is None and t <= now:
        t += timedelta(days=1)                   # "at 9" after 9 today means tomorrow
    return t


def tool_set_reminder(text: str, in_minutes: float | None = None, at: str | None = None,
                      repeat: str = "none") -> str:
    text = (text or "").strip() or "Reminder"
    if in_minutes is not None:
        due = time.time() + float(in_minutes) * 60
    elif at:
        due = _parse_at(at).timestamp()
    else:
        return "Say when: give in_minutes or at (a time like '17:30')."
    if due <= time.time():
        return "That time has already passed."
    repeat = (repeat or "none").lower()
    if repeat not in ("none", "daily", "weekdays", "weekly"):
        repeat = "none"
    with _rem_lock:
        items = _load_reminders()
        rid = max([r["id"] for r in items] + [0]) + 1
        items.append({"id": rid, "text": text, "due": due, "repeat": repeat, "created": time.time()})
        _save_reminders(items)
    again = "" if repeat == "none" else f", repeating {repeat}"
    return f"Reminder #{rid} set for {_when(due)}{again}: {text}"


def tool_set_timer(minutes: float = 0, seconds: float = 0, label: str = "") -> str:
    total = float(minutes or 0) * 60 + float(seconds or 0)
    if total <= 0:
        return "How long should the timer be?"
    text = f"Timer done{': ' + label if label else ''} ({_duration(total)})"
    with _rem_lock:
        items = _load_reminders()
        rid = max([r["id"] for r in items] + [0]) + 1
        items.append({"id": rid, "text": text, "due": time.time() + total, "repeat": "none",
                      "created": time.time(), "timer": True})
        _save_reminders(items)
    return f"Timer #{rid} set for {_duration(total)}{' (' + label + ')' if label else ''}."


def _duration(sec: float) -> str:
    sec = int(round(sec))
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    parts = [f"{h} hour{'s' * (h != 1)}" if h else "", f"{m} minute{'s' * (m != 1)}" if m else "",
             f"{s} second{'s' * (s != 1)}" if s else ""]
    return " ".join(p for p in parts if p) or "0 seconds"


def tool_list_reminders() -> str:
    with _rem_lock:
        items = sorted(_load_reminders(), key=lambda r: r["due"])
    if not items:
        return "No reminders or timers are set."
    lines = []
    for r in items:
        left = r["due"] - time.time()
        extra = f" (in {_duration(left)})" if left < 6 * 3600 else ""
        rep = "" if r.get("repeat", "none") == "none" else f", repeats {r['repeat']}"
        lines.append(f"#{r['id']} {_when(r['due'])}{extra}{rep}: {r['text']}")
    return "\n".join(lines)


def tool_cancel_reminder(which: str) -> str:
    w = str(which or "").strip().lower().lstrip("#")
    with _rem_lock:
        items = _load_reminders()
        if w in ("all", "everything"):
            keep, gone = [], items
        elif w.isdigit():
            keep = [r for r in items if r["id"] != int(w)]
            gone = [r for r in items if r["id"] == int(w)]
        else:
            gone = [r for r in items if w and w in r["text"].lower()]
            keep = [r for r in items if r not in gone]
        if not gone:
            return "No matching reminder. " + tool_list_reminders()
        _save_reminders(keep)
    return "Cancelled: " + "; ".join(f"#{r['id']} {r['text']}" for r in gone)


AUMID = "OpenJarvis.Jarvis"
PS_AUMID = "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe"
LOG = ROOT / "logs" / "pc-tools.log"


def _log(msg: str) -> None:
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except OSError:
        pass


def _register_notifications() -> None:
    """Let notifications show as "Jarvis" with its icon (per-user registration
    for an unpackaged app; without it they'd say "Windows PowerShell")."""
    import winreg

    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\AppUserModelId\{AUMID}") as k:
            winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, "Jarvis")
            icon = ROOT / "jarvis-notify.png"
            if icon.exists():
                winreg.SetValueEx(k, "IconUri", 0, winreg.REG_SZ, str(icon))
    except OSError as exc:
        _log(f"notification registration failed: {exc}")


def _notify(title: str, text: str) -> None:
    """A Windows notification that stays until dismissed, with the reminder sound."""
    xml = ('<toast scenario="reminder"><visual><binding template="ToastGeneric">'
           f"<text>{xml_escape(title)}</text><text>{xml_escape(text)}</text></binding></visual>"
           '<audio src="ms-winsoundevent:Notification.Reminder"/><actions>'
           '<action content="Dismiss" arguments="dismiss" activationType="system"/></actions></toast>')
    for app in (AUMID, PS_AUMID):        # Jarvis's own name first; PowerShell's as a fallback
        script = (
            "$ProgressPreference = 'SilentlyContinue'; $ErrorActionPreference = 'Stop'\n"
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
            "ContentType = WindowsRuntime] | Out-Null\n"
            "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, "
            "ContentType = WindowsRuntime] | Out-Null\n"
            "$x = New-Object Windows.Data.Xml.Dom.XmlDocument\n"
            f"$x.LoadXml('{xml.replace(chr(39), chr(39) * 2)}')\n"
            "$t = New-Object Windows.UI.Notifications.ToastNotification $x\n"
            f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{app}').Show($t)\n"
            "'shown'")
        try:
            out = _powershell(script)
        except Exception as exc:  # noqa: BLE001
            out = f"error {exc.__class__.__name__}"
        if out.endswith("shown"):
            _log(f"notification shown ({'Jarvis' if app == AUMID else 'PowerShell'} name)")
            return
        _log(f"notification failed with app id {app}: {out[:200]}")


def _fire(r: dict, missed: bool = False) -> None:
    _log(f"{'timer' if r.get('timer') else 'reminder'} #{r['id']} is due{' (missed)' if missed else ''}")
    text = r["text"]
    if missed:
        text += f" (this was due at {datetime.fromtimestamp(r['due']):%I:%M %p}, while Jarvis was off)"
    try:   # the "Jarvis" voice bar picks this up and says it out loud
        VOICE_INBOX.mkdir(parents=True, exist_ok=True)
        say = text if r.get("timer") else f"Reminder: {text}"
        (VOICE_INBOX / f"{time.time():.3f}.json").write_text(json.dumps({"say": say}), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    _notify("Jarvis timer" if r.get("timer") else "Jarvis reminder", text)


def _next_due(r: dict) -> float | None:
    rep = r.get("repeat", "none")
    if rep == "none":
        return None
    d = datetime.fromtimestamp(r["due"])
    while d.timestamp() <= time.time():
        d += timedelta(days=7 if rep == "weekly" else 1)
        while rep == "weekdays" and d.weekday() >= 5:
            d += timedelta(days=1)
    return d.timestamp()


def _reminder_loop() -> None:
    first = True
    while True:
        try:
            now, due = time.time(), []
            with _rem_lock:
                items = _load_reminders()
                keep = []
                for r in items:
                    if r["due"] <= now:
                        late = now - r["due"]
                        if late < 6 * 3600:      # older than 6 h (PC was off): skip it
                            due.append((r, first and late > 60))
                        nxt = _next_due(r)
                        if nxt:
                            keep.append({**r, "due": nxt})
                    else:
                        keep.append(r)
                if due or len(keep) != len(items) or any(a is not b for a, b in zip(keep, items)):
                    _save_reminders(keep)
            for r, missed in due:
                threading.Thread(target=_fire, args=(r, missed), daemon=True).start()
            first = False
        except Exception:  # noqa: BLE001 - never let the loop die
            pass
        time.sleep(1)


TOOLS = {
    "open_app": (tool_open_app, "Open an installed app on the user's Windows PC by its name "
                 "(e.g. 'Notepad', 'Spotify', 'Calculator', 'Chrome', 'WhatsApp', 'Word').",
                 {"name": {"type": "string", "description": "App name as the user said it."}}, ["name"]),
    "list_apps": (tool_list_apps, "List installed apps on the PC, optionally filtered by a "
                  "search word. Use when unsure of an app's exact name.",
                  {"query": {"type": "string", "description": "Optional search word."}}, []),
    "open_folder": (tool_open_folder, "Open a folder in File Explorer on the PC: 'desktop', "
                    "'documents', 'downloads', 'pictures', 'music', 'videos', 'onedrive', "
                    "'this pc', 'recycle bin', 'screenshots', or a full folder path.",
                    {"folder": {"type": "string"}}, ["folder"]),
    "open_file": (tool_open_file, "Open a document, photo, video, PDF or similar file with its "
                  "default app, given its full path. Programs and scripts are refused.",
                  {"path": {"type": "string", "description": "Full path to the file."}}, ["path"]),
    "open_website": (tool_open_website, "Open a website in the user's default browser.",
                     {"url": {"type": "string", "description": "e.g. https://youtube.com"}}, ["url"]),
    "open_settings": (tool_open_settings, "Open a Windows Settings page, e.g. 'wifi', "
                      "'bluetooth', 'display', 'sound', 'battery', 'updates', 'dark mode'.",
                      {"page": {"type": "string"}}, []),
    "volume": (tool_volume, "Read or change the PC's sound volume: get, set (to a level 0-100), "
               "up, down (by step, default 10), mute, unmute.",
               {"action": {"type": "string", "enum": ["get", "set", "up", "down", "mute", "unmute"]},
                "level": {"type": "integer", "description": "0-100, for set."},
                "step": {"type": "integer", "description": "For up/down; default 10."}}, ["action"]),
    "media_control": (tool_media_control, "Control whatever music/video is playing (Spotify, "
                      "YouTube, etc.) like the keyboard's media keys: play, pause, next, previous, stop.",
                      {"action": {"type": "string", "enum": ["play", "pause", "next", "previous", "stop"]}},
                      ["action"]),
    "brightness": (tool_brightness, "Read or change the laptop screen brightness: get, set "
                   "(to 0-100), up, down (by step, default 10).",
                   {"action": {"type": "string", "enum": ["get", "set", "up", "down"]},
                    "level": {"type": "integer"}, "step": {"type": "integer"}}, ["action"]),
    "take_screenshot": (tool_take_screenshot, "Take a screenshot of the whole screen and save it "
                        "as a PNG (in Desktop\\Screenshots).", {}, []),
    "lock_pc": (tool_lock_pc, "Lock the PC (Windows sign-in screen). Only when the user clearly "
                "asks to lock it.", {}, []),
    "battery_status": (tool_battery_status, "Battery percentage, whether it's charging, and "
                       "time left.", {}, []),
    "set_reminder": (tool_set_reminder, "Remind the user later: at that time a Windows "
                     "notification pops up and Jarvis says it out loud. Use for 'remind me...', "
                     "alarms and wake-ups, including repeating ones. Give EITHER in_minutes OR at "
                     "(local time 'HH:MM' 24-hour, or 'YYYY-MM-DD HH:MM'). Use this, not "
                     "schedule_task, for reminders (schedule_task is for jobs Jarvis performs).",
                     {"text": {"type": "string", "description": "What to remind them about, short."},
                      "in_minutes": {"type": "number", "description": "Minutes from now (may be fractional)."},
                      "at": {"type": "string", "description": "e.g. '17:30' or '2026-11-08 09:00'."},
                      "repeat": {"type": "string", "enum": ["none", "daily", "weekdays", "weekly"]}},
                     ["text"]),
    "set_timer": (tool_set_timer, "Start a countdown timer (e.g. 'set a timer for 10 minutes'); "
                  "when it ends a notification pops up and Jarvis says it.",
                  {"minutes": {"type": "number"}, "seconds": {"type": "number"},
                   "label": {"type": "string", "description": "Optional, e.g. 'pasta'."}}, []),
    "list_reminders": (tool_list_reminders, "List the reminders and timers that are set.", {}, []),
    "read_clipboard": (tool_read_clipboard, "Read the text the user has copied (clipboard). Use only "
                       "when they refer to what they copied ('summarise what I copied', 'translate "
                       "this', 'fix the grammar of what I copied'). Never repeat passwords or codes "
                       "that turn up in it.", {}, []),
    "write_clipboard": (tool_write_clipboard, "Put text on the user's clipboard so they can paste it "
                        "(e.g. the translated or corrected version). Use when they say 'copy it' / "
                        "'copy it back', or after transforming text they copied.",
                        {"text": {"type": "string"}}, ["text"]),
    "weather_forecast": (tool_get_weather, "Current weather and forecast for a city (default: the "
                    f"user's home city, {HOME_CITY or 'not set yet - ask which city'}). Free, no key needed.",
                    {"city": {"type": "string",
                              "description": f"City name; omit for {HOME_CITY}." if HOME_CITY else "City name."},
                     "days": {"type": "integer", "description": "Forecast days 1-7 (default 1)."}}, []),
    "morning_briefing": (tool_morning_briefing, "Start the user's briefing when they ask for a "
                         "'morning briefing', 'daily briefing', 'brief me' or 'what's my day like': "
                         f"returns date, {HOME_CITY + ' ' if HOME_CITY else ''}weather and today's "
                         "reminders, plus instructions to "
                         "add calendar, email and top news.", {}, []),
    "cancel_reminder": (tool_cancel_reminder, "Cancel a reminder or timer by its number, by a "
                        "word from its text, or 'all'.",
                        {"which": {"type": "string"}}, ["which"]),
}


def handle(msg: dict) -> dict | None:
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:  # notification
        return None
    if method == "initialize":
        result = {"protocolVersion": msg.get("params", {}).get("protocolVersion", PROTOCOL),
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "jarvis-pc-tools", "version": "1.0"}}
    elif method == "tools/list":
        result = {"tools": [{"name": n, "description": d,
                             "inputSchema": {"type": "object", "properties": props, "required": req}}
                            for n, (_, d, props, req) in TOOLS.items()]}
    elif method == "tools/call":
        p = msg.get("params", {})
        name, args = p.get("name"), p.get("arguments") or {}
        if name not in TOOLS:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"Unknown tool {name}"}}
        try:
            text, err = TOOLS[name][0](**args), False
        except Exception as exc:  # noqa: BLE001 - report to the model instead of crashing
            text, err = f"{name} failed: {exc}", True
        result = {"content": [{"type": "text", "text": text}], "isError": err}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Unknown method {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main() -> None:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    if "--no-reminders" not in sys.argv:     # tests run a second copy without the alarm loop
        _register_notifications()
        threading.Thread(target=_reminder_loop, daemon=True).start()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            reply = handle(json.loads(line))
        except json.JSONDecodeError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
