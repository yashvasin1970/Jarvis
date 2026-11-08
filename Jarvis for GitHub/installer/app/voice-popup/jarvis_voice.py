"""Jarvis voice pop-up: say "Hey Jarvis" (or "Jarvis") and talk.

A floating bar appears at the bottom of the screen, listens to the request,
sends it to the local Jarvis server (http://127.0.0.1:8000), shows and speaks
the reply, then listens a few seconds for a follow-up before hiding.

Wake word: openWakeWord's "hey_jarvis" model, run locally. Audio stays on
this PC; nothing is recorded or sent anywhere until the wake word is heard,
and then only to the local Jarvis server.

Started by start-jarvis.ps1 (pythonw, no console). Single instance.
Testing: --simulate <wav16k> feeds a recording instead of the microphone.
Log: logs\\voice-popup.log in the Jarvis folder
"""

from __future__ import annotations

import collections
import ctypes
import difflib
import io
import json
import logging
import math
import os
import queue
import random
import re
import sys
import threading
import time
from pathlib import Path

import httpx
import numpy as np
import sounddevice as sd
import soundfile as sf
import tkinter as tk
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageTk

# The Jarvis folder (this file is in its voice-popup\ subfolder), so it works wherever
# Jarvis is installed (also from the setup kit for other PCs).
ROOT = Path(__file__).resolve().parent.parent


def _setting(name: str, default: str = "") -> str:
    """A value from JARVIS-SETTINGS.txt in the Jarvis folder (NAME=value lines)."""
    try:
        for line in (ROOT / "JARVIS-SETTINGS.txt").read_text(encoding="utf-8-sig").splitlines():
            key, sep, value = line.partition("=")
            if sep and not key.strip().startswith("#") and key.strip().upper() == name:
                return value.strip()
    except OSError:
        pass
    return default


API = f"http://127.0.0.1:{_setting('PORT', '8000') or '8000'}"
# Reminders/timers (pc-tools) drop {"say": "..."} files here for the bar to speak.
INBOX = ROOT / "voice-popup" / "inbox"
# Where the user dragged the bar to (its bottom-centre point); no file = bottom-centre of the screen.
BAR_POS_FILE = ROOT / "data" / "voice-bar-position.json"
RATE, FRAME = 16000, 1280            # openWakeWord wants 80 ms frames at 16 kHz
# Wake word. The stock "hey_jarvis" model scores this user's real voice only
# 0.05-0.47 and plain "Jarvis" ~0 (measured 2026-11-05), so it is just the fast
# path; short phrases are also checked with Jarvis's speech recogniser and wake
# it when they start with "Jarvis" (see wake_command).
DIRECT_THRESHOLD = 0.5               # wake-word model score that wakes immediately
FOLLOW_UP_SECONDS = 6
# This room has a loud low rumble (measured 2026-11-07: ~2000 rms, 93% of it
# below 300 Hz, strongest at ~20 Hz), louder than speech on this laptop mic.
# So the wake-word model and the recogniser get audio high-passed above
# HIGHPASS_HZ (speech is mostly 300-3000 Hz), and "is someone speaking?" is
# decided by Silero's speech detector on the unfiltered audio (it scored the
# rumble 0.0-0.36 and a voice quieter than the rumble 0.5-0.9), not by loudness.
HIGHPASS_HZ = 200
SPEECH_PROB = 0.5           # starts speech
SPEECH_KEEP = 0.35          # keeps it going (brief dips between words)
# Hold-to-talk (added 2026-11-07): with the fan/AC on (room ~2500-2800 rms) the user's
# voice scored at most 0.38 on the speech detector, so neither "Jarvis" nor the end of
# a request could be detected. Holding Right Ctrl records exactly while it's held, so
# no detection is needed. (Wispr Flow uses Left Ctrl + `, so they don't clash.)
TALK_KEY = 0xA3             # VK_RCONTROL
TALK_HOLD = 0.3             # held this long (s) when idle = start talking
VOICE_HINT = (
    "The user is talking to you out loud through the Jarvis voice pop-up on their "
    "Windows PC. Answer in at most two or three short sentences that sound natural "
    "when spoken: no markdown, lists, tables, emojis or web addresses. When they ask "
    "to open an app, folder, file, website or Windows setting, use the open_app, "
    "open_folder, open_file, open_website or open_settings tool, then confirm briefly. "
    "When they ask for a picture, drawing, image, logo or wallpaper, use the create_image "
    "tool with a detailed description, then say it's on screen. Use the volume, "
    "media_control, brightness, take_screenshot, battery_status, set_reminder, set_timer, "
    "list_reminders and cancel_reminder tools for those requests. For 'what's on my screen' or "
    "'explain this', use look_at_screen; for 'what I copied', read_clipboard (and "
    "write_clipboard to copy a result back); for links and videos, read_webpage or "
    "youtube_transcript. For a 'morning briefing' "
    "or 'brief me', call morning_briefing and follow its instructions; keep the spoken "
    "briefing under about 120 words."
)

logging.basicConfig(filename=ROOT / "logs" / "voice-popup.log",
                    level=logging.DEBUG if "--debug" in sys.argv else logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("voice")
for _noisy in ("faster_whisper", "httpx", "httpcore", "httpcore2", "huggingface_hub", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
if "--debug" in sys.argv and sys.stderr is not None:
    import faulthandler

    faulthandler.dump_traceback_later(25, repeat=False, file=sys.stderr)

# ---------------------------------------------------------------------------
# Single instance + crisp rendering on scaled displays
# ---------------------------------------------------------------------------
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_mutex = _kernel32.CreateMutexW(None, False, "Local\\JarvisVoicePopup")
if ctypes.get_last_error() == 183 and "--simulate" not in sys.argv:  # ERROR_ALREADY_EXISTS
    sys.exit(0)
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:  # noqa: BLE001
    pass


# ---------------------------------------------------------------------------
# Graphics (drawn with Pillow at 3x and scaled down for smooth edges)
# ---------------------------------------------------------------------------
KEY = "#010203"           # window colour made transparent (outside the pill)
# "Graphite + violet" (chosen 2026-11-07; matches the chat window).
BG_TOP, BG_BOTTOM = (38, 36, 47), (21, 20, 26)
OUTLINE = (118, 98, 186)
FIELD_BG, FIELD_LINE = "#2A2735", (88, 76, 130)   # typing box
TEXT, TEXT_DIM, TEXT_STATUS = "#EEEBF7", "#9C98AE", "#A9A5BA"
ICON, ICON_ACTIVE, CARET = "#E4E0F2", "#C4B5FD", "#C4B5FD"


def pill_image(w: int, h: int, radius: int) -> Image.Image:
    s = 3
    W, H, R = w * s, h * s, radius * s
    grad = Image.new("RGB", (1, H))
    for y in range(H):
        t = y / max(H - 1, 1)
        grad.putpixel((0, y), tuple(int(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM)))
    grad = grad.resize((W, H))
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, W - 1, H - 1], radius=R, fill=255)
    img = Image.new("RGB", (W, H), (1, 2, 3))
    img.paste(grad, (0, 0), mask)
    ImageDraw.Draw(img).rounded_rectangle([1, 1, W - 2, H - 2], radius=R, outline=OUTLINE, width=s)
    img = img.resize((w, h), Image.LANCZOS)
    # Re-key the outside so anti-aliased edge pixels never turn into a halo.
    m = mask.resize((w, h), Image.LANCZOS)
    out = Image.new("RGB", (w, h), (1, 2, 3))
    out.paste(img, (0, 0), m.point(lambda v: 255 if v > 90 else 0))
    return out


def orb_frames(size: int, n: int = 36) -> list[list[Image.Image]]:
    """frames[level][i]: a glowing violet orb with slowly turning wisps; level = loudness."""
    s = 3
    S = size * s
    c = S / 2
    r = S * 0.40
    yy, xx = np.mgrid[0:S, 0:S].astype(np.float32)
    d = np.sqrt((xx - c) ** 2 + (yy - c) ** 2) / r
    base = np.zeros((S, S, 4), np.float32)
    core = np.clip(1 - d, 0, 1)
    base[..., 0] = 62 + 120 * core ** 1.5
    base[..., 1] = 42 + 110 * core ** 2
    base[..., 2] = 112 + 140 * core ** 1.5
    base[..., 3] = np.where(d <= 1, 255, 0)
    frames = []
    for level in range(4):
        row = []
        for i in range(n):
            img = Image.fromarray(base.astype(np.uint8), "RGBA")
            wisp = Image.new("RGBA", (S, S), (0, 0, 0, 0))
            dw = ImageDraw.Draw(wisp)
            for k in range(3):
                a = 2 * math.pi * (i / n + k / 3)
                ex, ey = c + math.cos(a) * r * 0.35, c + math.sin(a * 1.3) * r * 0.30
                ew, eh = r * (0.55 + 0.1 * k), r * 0.22
                dw.ellipse([ex - ew, ey - eh, ex + ew, ey + eh], fill=(232, 218, 255, 70 + 25 * level))
            wisp = wisp.filter(ImageFilter.GaussianBlur(S * 0.05))
            clip = Image.new("L", (S, S), 0)
            ImageDraw.Draw(clip).ellipse([c - r, c - r, c + r, c + r], fill=255)
            img.paste(wisp, (0, 0), Image.composite(wisp.getchannel("A"), Image.new("L", (S, S), 0), clip))
            glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
            gr = r * (1.04 + 0.05 * level)
            ImageDraw.Draw(glow).ellipse([c - gr, c - gr, c + gr, c + gr],
                                         outline=(192, 168, 255, 120 + 40 * level), width=int(S * 0.025))
            img = Image.alpha_composite(img, glow.filter(ImageFilter.GaussianBlur(S * 0.02)))
            # Transparent outside the orb: Tk blends it over the pill gradient.
            row.append(img.resize((size, size), Image.LANCZOS))
        frames.append(row)
    return frames


def _lut(stops: list[tuple[int, tuple[int, int, int]]]) -> list[int]:
    """Colour ramp (brightness -> colour) for Image.point on an RGB image of grey values."""
    out: list[list[int]] = [[], [], []]
    for v in range(256):
        for (a, ca), (b, cb) in zip(stops, stops[1:]):
            if a <= v <= b:
                t = (v - a) / max(1, b - a)
                for ch in range(3):
                    out[ch].append(int(ca[ch] + (cb[ch] - ca[ch]) * t))
                break
    return out[0] + out[1] + out[2]


class GoldOrb:
    """Golden "hologram" orb shown while Jarvis speaks (added 2026-11-08 at the user's request,
    made richer the same day; the chat window has the same design in SpeakingOrb.tsx).

    Drawn with Pillow each frame (about 7 ms): dotted globe lines, amber light fragments on
    tilted rings turning in 3D, flickering "data blocks" that light up with the voice, a bright
    sweeping swirl, a lens ring and turning arcs round the core, embers orbiting outside,
    a ripple on each louder word, and sparks; it grows in when it appears (start()).
    Drawn as brightness at 1.5x, blurred for glow, coloured gold through a lookup table, and
    put on a round dark panel (outside the panel = KEY, i.e. transparent).
    frame(level, speaking) with level 0..1 = loudness of the voice.
    """
    def __init__(self, size: int, panel: bool = True) -> None:
        self.size, self.s = size, 1.5
        S = self.S = int(size * self.s)
        rnd = random.Random()
        self.rings = [dict(r=0.28 + 0.72 * math.sqrt(i / 15) * (0.93 + 0.07 * rnd.random()),
                           tx=(rnd.random() - 0.5) * math.pi, ty=(rnd.random() - 0.5) * math.pi,
                           sp=(0.1 + rnd.random() * 0.45) * rnd.choice((-1, 1))) for i in range(16)]
        self.dashes = [dict(ring=i, a0=rnd.random() * math.tau, ln=0.04 + rnd.random() * 0.42,
                            w=0.8 + rnd.random() * 2.2, fl=rnd.random() * 10)
                       for i in range(16) for _ in range(14 + rnd.randrange(14))]
        self.blocks = [dict(th=rnd.random() * math.tau, ph=math.acos(2 * rnd.random() - 1),
                            r=0.55 + rnd.random() * 0.5, ln=0.025 + rnd.random() * 0.05,
                            w=2 + rnd.random() * 2.5, fl=rnd.random() * 10, fq=1.5 + rnd.random() * 4)
                       for _ in range(150)]
        self.embers = [dict(a=rnd.random() * math.tau, r=1.08 + rnd.random() * 0.22,
                            sp=(0.05 + rnd.random() * 0.25) * rnd.choice((-1, 1)), fl=rnd.random() * 10,
                            z=rnd.random()) for _ in range(46)]
        self.sparks: list[list[float]] = []
        self.pulses: list[float] = []
        self.level = self.slow = self.spin = self.swirl = 0.0
        self.last_pulse = 0.0
        self.start()
        yy, xx = np.mgrid[0:S, 0:S].astype(np.float32)
        d = np.sqrt((xx - S / 2) ** 2 + (yy - S / 2) ** 2) / (S / 2)
        # Pre-made core glows for 12 loudness steps (saves per-frame maths).
        self.cores = []
        for k in range(12):
            lv = k / 11
            rc = 0.06 + lv * 0.06
            # (A softer wide halo without the panel: on light screens it read as a tint.)
            core = np.exp(-(d / rc) ** 2) * 255 + np.exp(-(d / 0.7) ** 2) * (14 + lv * 26) * (1 if panel else 0.6)
            self.cores.append(Image.fromarray(np.clip(core, 0, 255).astype(np.uint8), "L"))
        self.lut = _lut([(0, (0, 0, 0)), (45, (66, 30, 4)), (115, (200, 112, 22)),
                         (180, (255, 190, 78)), (255, (255, 250, 230))])
        P, k3 = size, 3
        big = Image.new("RGB", (P * k3, P * k3), (1, 2, 3))
        mask = Image.new("L", (P * k3, P * k3), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, P * k3 - 1, P * k3 - 1], fill=255)
        if panel:
            grad = Image.new("RGB", (1, P * k3))
            for y in range(P * k3):
                t = y / (P * k3 - 1)
                grad.putpixel((0, y), tuple(int(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM)))
            big.paste(grad.resize((P * k3, P * k3)), (0, 0), mask)
            ImageDraw.Draw(big).ellipse([2, 2, P * k3 - 3, P * k3 - 3], outline=OUTLINE, width=k3)
        self.panel = big.resize((P, P), Image.LANCZOS)
        self.mask = mask.resize((P, P), Image.LANCZOS).point(lambda v: 255 if v > 90 else 0)
        self.key = Image.new("RGB", (P, P), (1, 2, 3))
        # Without the panel the glow must fade to nothing well inside the window's square,
        # otherwise its faint edge shows as a box on light screens (the user saw one): a round
        # fade from 78% to 96% of the radius.
        yy2, xx2 = np.mgrid[0:P, 0:P].astype(np.float32)
        rr = np.sqrt((xx2 - P / 2 + 0.5) ** 2 + (yy2 - P / 2 + 0.5) ** 2) / (P / 2)
        f = np.clip((0.96 - rr) / (0.96 - 0.78), 0, 1)
        self.fade = f * f * (3 - 2 * f)                     # smoothstep

    def start(self) -> None:
        """Call when the orb appears: it grows in."""
        self.t0 = self.last = self.born = time.time()
        self.sparks, self.pulses = [], []

    @staticmethod
    def _rot(x, y, z, ax, ay):
        cx, sx, cy, sy = math.cos(ax), math.sin(ax), math.cos(ay), math.sin(ay)
        y1, z1 = y * cx - z * sx, y * sx + z * cx
        return x * cy + z1 * sy, y1, -x * sy + z1 * cy

    def frame(self, target: float, speaking: bool) -> Image.Image:
        """The orb on its round dark panel (outside = KEY, for a colour-keyed window)."""
        return Image.composite(ImageChops.add(self.panel, self._gold(target, speaking)), self.key, self.mask)

    def frame_bgra(self, target: float, speaking: bool) -> bytes:
        """The orb with NO background (the user's request, 2026-11-08): premultiplied BGRA for
        a per-pixel-alpha (layered) window. Alpha = the brightest channel (red, on this ramp),
        so dark parts are see-through and the glow lies softly over whatever is on screen."""
        rgb = np.asarray(self._gold(target, speaking), dtype=np.float32) * self.fade[..., None]
        a = rgb[..., 0]
        keep = a >= 4                       # drop the near-invisible tint entirely (no faint box)
        out = np.zeros((self.size, self.size, 4), np.uint8)
        out[keep, 0] = rgb[keep, 2]
        out[keep, 1] = rgb[keep, 1]
        out[keep, 2] = rgb[keep, 0]
        out[keep, 3] = a[keep]
        return out.tobytes()

    def _gold(self, target: float, speaking: bool) -> Image.Image:
        now = time.time()
        dt, self.last = min(0.08, now - self.last), now
        t = now - self.t0
        grow = min(1.0, (now - self.born) / 0.5)
        grow = 1 - (1 - grow) ** 3                                  # ease-out
        if not speaking:
            target = 0.06
        self.level += (target - self.level) * (0.45 if target > self.level else 0.12)
        self.slow += (self.level - self.slow) * 0.05
        lv = self.level
        # A ripple on each louder word.
        if speaking and lv - self.slow > 0.12 and now - self.last_pulse > 0.28:
            self.pulses.append(now)
            self.last_pulse = now
        self.spin += dt * (0.22 + lv * 1.0)
        self.swirl += dt * (0.9 + lv * 3.0)
        S, s = self.S, self.s
        c = S / 2
        R = S * 0.33 * (0.78 + 0.22 * grow) * (1 + lv * 0.07)
        img = Image.new("L", (S, S), 0)
        dr = ImageDraw.Draw(img)
        gain = (0.5 + lv * 0.8) * (0.3 + 0.7 * grow)
        W = lambda w: max(1, int(round(w * s)))  # noqa: E731

        def P3(x, y, z, ax, ay):
            x, y, z = self._rot(x, y, z, ax, ay)
            p = 1 / (1.6 - z * 0.35)
            return c + x * R * p * 1.6, c + y * R * p * 1.6, z

        # Globe lines: latitude bands, dotted.
        for lat in (-1.0, -0.5, 0.0, 0.5, 1.0):
            cy_, cr = math.sin(lat), math.cos(lat)
            pts = [P3(math.cos(a / 40 * math.tau) * cr, cy_, math.sin(a / 40 * math.tau) * cr, 0.38, self.spin * 0.7)
                   for a in range(41)]
            for k in range(0, 40, 2):
                depth = (pts[k][2] + 1) / 2
                dr.line([pts[k][:2], pts[k + 1][:2]], fill=int((18 + depth * 45) * gain), width=W(1))
        # Light fragments on tilted rings (front = brighter).
        for dsh in self.dashes:
            ring = self.rings[dsh["ring"]]
            a = dsh["a0"] + self.spin * ring["sp"] * 3
            ax, ay = ring["tx"] + self.spin * 0.3, ring["ty"] + self.spin
            pts = [P3(math.cos(a + dsh["ln"] * k / 4) * ring["r"], math.sin(a + dsh["ln"] * k / 4) * ring["r"], 0, ax, ay)
                   for k in range(5)]
            depth = (sum(p[2] for p in pts) / 5 + 1) / 2
            fl = 0.75 + 0.25 * math.sin(t * 6 + dsh["fl"] * 7)
            dr.line([p[:2] for p in pts], fill=int(min(255, (50 + depth * 175) * gain * fl)),
                    width=W(dsh["w"] * (0.55 + depth * 0.75)))
        # Data blocks: short thick tangential bars that flicker on with the voice.
        for b in self.blocks:
            if math.sin(t * b["fq"] + b["fl"] * 9) < 0.35 - lv * 1.2:
                continue
            sp, cp = math.sin(b["ph"]), math.cos(b["ph"])
            r = b["r"]
            x0, y0, z0 = sp * math.cos(b["th"]) * r, cp * r, sp * math.sin(b["th"]) * r
            tx, tz = -math.sin(b["th"]) * b["ln"], math.cos(b["th"]) * b["ln"]
            p0 = P3(x0 - tx, y0, z0 - tz, 0.38, self.spin * 0.7)
            p1 = P3(x0 + tx, y0, z0 + tz, 0.38, self.spin * 0.7)
            depth = (p0[2] + 1) / 2
            dr.line([p0[:2], p1[:2]], fill=int(min(255, (40 + depth * 190) * gain)), width=W(b["w"] * (0.5 + depth * 0.6)))
        # The swirl: a bright, tapering arc sweeping round on a tilted ring.
        for k, (tilt, dirn, ln) in enumerate(((0.95, 1, 1.9), (-0.6, -1, 1.2))):
            a0 = self.swirl * dirn * (1 + k * 0.35)
            prev = None
            for j in range(17):
                a = a0 - ln * j / 16
                p = P3(math.cos(a) * 0.62, math.sin(a) * 0.62 * 0.92, 0, tilt, 0.3 + k)
                if prev:
                    f = 1 - j / 17
                    dr.line([prev[:2], p[:2]], fill=int(min(255, (120 + 135 * f) * gain * (0.9 - k * 0.3))),
                            width=W((1 + 5.5 * f) * (1 + lv * 0.6) * (1 - k * 0.35)))
                prev = p
        # Lens ring and inner arcs around the core.
        lr = R * 0.17
        dr.ellipse([c - lr, c - lr, c + lr, c + lr], outline=int(150 * gain + 40), width=W(1.4))
        for k, rr in enumerate((0.25, 0.31)):
            rr *= R
            st = math.degrees(self.swirl * (1.6 if k else -1.2))
            for g in range(3):
                dr.arc([c - rr, c - rr, c + rr, c + rr], st + g * 120, st + g * 120 + 70,
                       fill=int((90 + lv * 110) * (0.4 + 0.6 * grow)), width=W(1.8 - k * 0.6))
        # Ripples.
        alive = []
        for born in self.pulses:
            age = (now - born) / 0.9
            if age < 1:
                alive.append(born)
                rr = R * (0.35 + age * 1.0)
                dr.ellipse([c - rr, c - rr, c + rr, c + rr], outline=int(170 * (1 - age) ** 1.5), width=W(2.2 * (1 - age) + 0.6))
        self.pulses = alive
        # Embers orbiting the outside.
        for e in self.embers:
            a = e["a"] + t * e["sp"]
            rr = R * e["r"] * (1 + 0.04 * math.sin(t * 2 + e["fl"]))
            x, y = c + math.cos(a) * rr, c + math.sin(a) * rr * (0.55 + 0.45 * e["z"])
            on = 0.5 + 0.5 * math.sin(t * 4 + e["fl"] * 5)
            er = s * (0.8 + 1.4 * on * e["z"])
            dr.ellipse([x - er, y - er, x + er, y + er], fill=int((60 + 160 * on) * gain))
        # Sparks thrown off when the voice is loud.
        if speaking:
            for _ in range(int(lv * 5 + random.random())):
                a, rr = random.random() * math.tau, R * (0.85 + random.random() * 0.2)
                v = (0.6 + random.random() * 2.2) * s * (0.6 + lv)
                self.sparks.append([c + math.cos(a) * rr, c + math.sin(a) * rr, math.cos(a) * v, math.sin(a) * v,
                                    0, 18 + random.random() * 30])
        live = []
        for sp_ in self.sparks:
            sp_[4] += 1
            sp_[0] += sp_[2]
            sp_[1] += sp_[3]
            if sp_[4] < sp_[5]:
                live.append(sp_)
                a = 1 - sp_[4] / sp_[5]
                dr.line([(sp_[0], sp_[1]), (sp_[0] - sp_[2] * 3, sp_[1] - sp_[3] * 3)], fill=int(230 * a), width=W(1))
        self.sparks = live[-160:]
        # Glow, core, colour.
        glow = img.filter(ImageFilter.GaussianBlur(4.5 * s))
        img = ImageChops.add(ImageChops.add(img, glow), glow.point(lambda v: v // 2))
        core = self.cores[min(11, int(lv * 11.99))]
        if grow < 1:
            core = core.point(lambda v, g=grow: int(v * g))
        img = ImageChops.add(img, core)
        small = img.resize((self.size, self.size), Image.BILINEAR)
        return Image.merge("RGB", (small, small, small)).point(self.lut)


class AlphaWindow:
    """Shows a premultiplied BGRA image in a window with per-pixel transparency
    (UpdateLayeredWindow), so a soft glow blends with whatever is behind it. Tk's own
    -transparentcolor can only make one exact colour see-through, which would leave a dark
    fringe round the glow; that's why the background-free orb (2026-11-08) uses this."""

    def __init__(self, hwnd: int, w: int, h: int) -> None:
        from ctypes import wintypes as wt

        self.wt = wt
        # Own copies of the DLLs, so these function signatures don't affect other code.
        u32, gdi = self.u32, self.gdi = ctypes.WinDLL("user32"), ctypes.WinDLL("gdi32")
        Hn = ctypes.c_void_p
        u32.GetDC.argtypes, u32.GetDC.restype = [Hn], Hn
        u32.GetWindowLongW.argtypes, u32.GetWindowLongW.restype = [Hn, ctypes.c_int], ctypes.c_long
        u32.SetWindowLongW.argtypes, u32.SetWindowLongW.restype = [Hn, ctypes.c_int, ctypes.c_long], ctypes.c_long
        u32.UpdateLayeredWindow.argtypes = [Hn, Hn, ctypes.c_void_p, ctypes.c_void_p, Hn, ctypes.c_void_p,
                                            wt.DWORD, ctypes.c_void_p, wt.DWORD]
        u32.UpdateLayeredWindow.restype = wt.BOOL
        gdi.CreateCompatibleDC.argtypes, gdi.CreateCompatibleDC.restype = [Hn], Hn
        gdi.CreateDIBSection.argtypes = [Hn, ctypes.c_void_p, wt.UINT, ctypes.POINTER(ctypes.c_void_p), Hn, wt.DWORD]
        gdi.CreateDIBSection.restype = Hn
        gdi.SelectObject.argtypes, gdi.SelectObject.restype = [Hn, Hn], Hn

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                        ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]

        # WS_EX_LAYERED | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW: see-through, never takes the
        # keyboard focus, no taskbar button. Clicks land where the orb is drawn (alpha > 0).
        u32.SetWindowLongW(hwnd, -20, u32.GetWindowLongW(hwnd, -20) | 0x80000 | 0x08000000 | 0x80)
        bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        self.screen = u32.GetDC(None)
        self.mem = gdi.CreateCompatibleDC(self.screen)
        self.bits = ctypes.c_void_p()
        self.bmp = gdi.CreateDIBSection(self.mem, ctypes.byref(bmi), 0, ctypes.byref(self.bits), None, 0)
        gdi.SelectObject(self.mem, self.bmp)
        self.hwnd, self.w, self.h = hwnd, w, h
        self.blend = (ctypes.c_ubyte * 4)(0, 0, 255, 1)        # AC_SRC_OVER, 0, 255, AC_SRC_ALPHA
        self.size, self.src = wt.SIZE(w, h), wt.POINT(0, 0)

    def show(self, bgra: bytes, x: int, y: int) -> bool:
        ctypes.memmove(self.bits, bgra, self.w * self.h * 4)
        pt = self.wt.POINT(x, y)
        return bool(self.u32.UpdateLayeredWindow(self.hwnd, self.screen, ctypes.byref(pt), ctypes.byref(self.size),
                                                 self.mem, ctypes.byref(self.src), 0, ctypes.byref(self.blend),
                                                 2))   # ULW_ALPHA

    def clear(self, x: int, y: int) -> None:
        self.show(bytes(self.w * self.h * 4), x, y)


def chime() -> np.ndarray:
    t = np.linspace(0, 0.16, int(24000 * 0.16), endpoint=False)
    tone = 0.18 * (np.sin(2 * np.pi * 880 * t) + 0.5 * np.sin(2 * np.pi * 1320 * t))
    return (tone * np.exp(-t * 18)).astype(np.float32)


def sounds_like_jarvis(text: str) -> bool:
    """True for 'Jarvis' and close mishearings (Jervis, Zervis, Javis, ...)."""
    for word in re.findall(r"[a-z]+", text.lower()):
        if len(word) >= 4 and difflib.SequenceMatcher(None, word, "jarvis").ratio() >= 0.66:
            return True
    return False


FILLER = {"please", "hi", "hello", "hey", "there", "ok", "okay", "um", "uh", "so", "yeah", "thank", "thanks", "you"}


# Words that may come before "Jarvis" (greetings, and how the recogniser
# sometimes hears "Hey": "I'm Jarvis", "A Jarvis").
GREETINGS = {"hey", "hi", "hello", "ok", "okay", "yo", "oh", "hay", "a", "ay", "eh", "i'm", "im"}


def wake_command(text: str) -> str | None:
    """None unless the phrase starts with "Jarvis" (or "Hey/Hi/Okay Jarvis").

    Otherwise the request said after it ("" if there is none), e.g.
    "Jarvis, what's the time?" -> "what's the time". Merely mentioning
    Jarvis mid-sentence ("I told Jarvis...") does not count.
    """
    words = list(re.finditer(r"[A-Za-z']+", text))
    for i, m in enumerate(words[:2]):
        if i == 1 and words[0].group().lower() not in GREETINGS:
            break
        if sounds_like_jarvis(m.group()):
            rest = text[m.end():].strip(" ,.!?-")
            meaningful = [w for w in re.findall(r"[A-Za-z']+", rest.lower()) if w not in FILLER]
            return rest if meaningful else ""
    return None


# Phrases speech recognisers invent from silence or noise.
HALLUCINATION = re.compile(
    r"(thanks? (you )?(so much )?for watching|see you (in|on) the next|subscribe|like and share"
    r"|^\W*(thank you|thanks|you|bye|bye[- ]bye|okay|uh|um)\W*$)", re.I)


def made_up(text: str) -> bool:
    """Recogniser output that is really noise: stock phrases, one token repeated
    ("ॐ ॐ ॐ ॐ"), or mostly non-English characters."""
    if HALLUCINATION.search(text):
        return True
    tokens = text.split()
    if len(tokens) >= 4 and len(set(tokens)) <= 2:
        return True
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum(c.isascii() for c in letters) < len(letters) * 0.5


def wispr_ptt_keys() -> list[int]:
    """Wispr Flow's push-to-talk keys (Windows key codes) from its settings.

    On this PC: "162+192" = Left Ctrl + ` (backtick). Read on every start so a
    changed shortcut is followed.
    """
    try:
        cfg = json.loads((Path(os.environ["APPDATA"]) / "Wispr Flow" / "config.json").read_text(encoding="utf-8"))
        for combo, action in cfg["prefs"]["user"]["shortcuts"].items():
            if action == "ptt":
                return [int(k) for k in combo.split("+")]
    except Exception:  # noqa: BLE001 - not installed / format changed
        pass
    return [162, 192]


def keys_down(keys: list[int]) -> bool:
    return bool(keys) and all(ctypes.windll.user32.GetAsyncKeyState(k) & 0x8000 for k in keys)


# The ` key is named in words: a lone ` is too small to see, and "Ctrl + `"
# read as "Ctrl +" (2026-11-07).
KEY_NAMES = {16: "Shift", 17: "Ctrl", 18: "Alt", 160: "Shift", 161: "Right Shift", 162: "Ctrl",
             163: "Right Ctrl", 164: "Alt", 165: "Right Alt", 91: "Win", 92: "Win", 32: "Space",
             192: "the key left of 1"}


def keys_label(keys: list[int]) -> str:
    return " + ".join(KEY_NAMES.get(k, chr(k) if 48 <= k <= 90 else f"key {k}") for k in keys)


def speakable(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"https?://\S+", "a link", text)
    text = re.sub(r"[*_`#>|]+", "", text)
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.M)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:1200]


def readable(text: str) -> str:
    """The reply as shown in the bar: whole (the bar scrolls), markdown symbols
    removed, list items kept on their own lines."""
    text = re.sub(r"```.*?```", "[code]", text, flags=re.S)
    text = re.sub(r"[*_`#>|]+", "", text)
    text = re.sub(r"^\s*[-•]\s+", "• ", text, flags=re.M)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


# ---------------------------------------------------------------------------
# The pop-up window (Tk main thread)
# ---------------------------------------------------------------------------
class Popup:
    def __init__(self, ui_queue: queue.Queue, actions: queue.Queue) -> None:
        self.q, self.actions = ui_queue, actions
        self.root = tk.Tk()
        self.root.withdraw()
        self.k = self.root.winfo_fpixels("1i") / 96.0
        px = lambda v: int(round(v * self.k))  # noqa: E731
        self.px = px
        self.W, self.H1 = px(470), px(64)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=KEY)
        self.root.attributes("-transparentcolor", KEY)
        self.canvas = tk.Canvas(self.root, width=self.W, height=self.H1, bg=KEY, highlightthickness=0, bd=0)
        self.canvas.pack()
        self.orb_size = px(44)
        self.frames = [[ImageTk.PhotoImage(f) for f in row] for row in orb_frames(self.orb_size)]
        # Golden orb shown while Jarvis speaks (2026-11-08). It takes the bar's place: the
        # text bar is hidden while he speaks (the user's request) and comes back after.
        # Its own small window; it never takes the keyboard focus; clicking it stops speech.
        # No background (the user's request): the glow is drawn straight over the screen through
        # a per-pixel-alpha window (AlphaWindow); Tk only provides the window and the clicks.
        self.orb_px = px(250)
        self.gold = GoldOrb(self.orb_px, panel=False)
        self.voice_level, self.orb_on, self._orb_after = 0.0, False, None
        self.orb_win = tk.Toplevel(self.root)
        self.orb_win.withdraw()
        self.orb_win.overrideredirect(True)
        self.orb_win.attributes("-topmost", True)
        self.orb_win.geometry(f"{self.orb_px}x{self.orb_px}+0+0")
        self.orb_win.bind("<Button-1>", lambda e: self.actions.put("stop"))
        self.orb_win.update_idletasks()
        self.orb_xy = (0, 0)
        self.alpha_win = AlphaWindow(ctypes.windll.user32.GetParent(self.orb_win.winfo_id()) or self.orb_win.winfo_id(),
                                     self.orb_px, self.orb_px)
        self.alpha_win.clear(0, 0)                          # starts empty, so it never flashes a box
        self.font = ("Segoe UI Variable Text", 12)
        self.icon_font = ("Segoe Fluent Icons", 13)
        self.text = "Say something..."
        self.dim = True
        self.level = 0
        self.mode = "idle"
        self.frame_i = 0
        self.visible = False
        self.root.bind("<Escape>", lambda e: self.actions.put("stop"))
        # Typing box: Wispr Flow (or the keyboard) types the request here.
        self.input_on, self.prev_fg, self._settle, self._muted = False, 0, None, False
        self.entry_var = tk.StringVar()
        self.entry = tk.Entry(self.root, textvariable=self.entry_var, bg=FIELD_BG, fg=TEXT,
                              insertbackground=CARET, relief="flat", bd=0, highlightthickness=0,
                              font=("Segoe UI Variable Text", 11))
        self.entry.bind("<KeyPress>", self._key_filter)
        self.entry.bind("<Return>", lambda e: self._submit_typed())
        self.entry.bind("<Escape>", lambda e: self.actions.put("stop"))
        self.entry_var.trace_add("write", lambda *a: self._on_typed())
        self.test_typing: tuple[float, str] | None = None     # --type-after (tests only)
        import tkinter.font as tkfont

        self.font_obj = tkfont.Font(root=self.root, family="Segoe UI Variable Text", size=12)
        self.line_h = self.font_obj.metrics("linespace")
        self.text_w = self.W - px(176)
        self.lines, self.n_vis, self.hint = [self.text], 1, ""
        self.scroll_px = self.scroll_target = 0.0             # smooth scrolling, in pixels
        self._anim = None
        self.view = None
        self.auto: tuple[float, float] | None = None          # (start, seconds) while speaking
        self.thumb_item = self.track_item = self.hint_item = None
        for w in (self.root, self.canvas, self.entry):
            w.bind("<MouseWheel>", self._on_wheel)
        # The bar can be dragged anywhere (2026-11-08); double-click puts it back.
        self.anchor, self._drag = self._load_anchor(), None
        self._drag_bindings(self.canvas)
        self._layout()
        self.root.after(40, self._tick)

    # geometry ------------------------------------------------------------------
    # Text is wrapped here (not by Tk) so long replies can scroll: up to MAX_LINES
    # show at once; the mouse wheel scrolls the rest, a thin bar shows where you
    # are, and while Jarvis speaks the text follows the voice (2026-11-07).
    MAX_LINES = 8

    def _wrap(self, text: str) -> list[str]:
        out = []
        for para in (text or "").split("\n"):
            line = ""
            for word in para.split(" "):
                while self.font_obj.measure(word) > self.text_w:      # very long word/link
                    cut = len(word)
                    while cut > 1 and self.font_obj.measure(word[:cut]) > self.text_w:
                        cut -= 1
                    if line:
                        out.append(line)
                        line = ""
                    out.append(word[:cut])
                    word = word[cut:]
                trial = f"{line} {word}" if line else word
                if self.font_obj.measure(trial) <= self.text_w:
                    line = trial
                else:
                    out.append(line)
                    line = word
            out.append(line)
        return out or [""]

    def _layout(self) -> None:
        px, lh, n = self.px, self.line_h, self.n_vis
        top = (self.H1 - lh) // 2                     # a single line sits centred, as before
        self.text_top = top
        if self.input_on:     # reply/prompt lines, a small hint line, then the typing box
            self.H = top + n * lh + px(16) + px(34) + px(12)
        else:
            self.H = max(self.H1, top * 2 + n * lh)
        cx, bottom = self._anchor()                   # grows upwards from its bottom-centre point
        x, y = self._clamp(cx - self.W // 2, bottom - self.H, self.W, self.H)
        self.root.geometry(f"{self.W}x{self.H}+{x}+{y}")
        self.canvas.config(width=self.W, height=self.H)
        self.bg_pil = pill_image(self.W, self.H, min(self.H // 2, px(32)))
        self.bg = ImageTk.PhotoImage(self.bg_pil)
        c = self.canvas
        c.delete("all")
        c.create_image(0, 0, image=self.bg, anchor="nw")
        oy = self.H // 2
        self.orb_item = c.create_image(px(10), oy, image=self.frames[0][0], anchor="w")
        # The text lives in its own small canvas (a "window" onto it) so it can glide
        # pixel by pixel and be clipped at the edges; its background is the matching
        # slice of the pill, so it looks seamless.
        if getattr(self, "view", None) is not None:
            self.view.destroy()
        vw, vh = self.text_w + px(6), n * lh
        self.view_h = vh
        self.view_bg = ImageTk.PhotoImage(self.bg_pil.crop((px(68), top, px(68) + vw, top + vh)))
        self.view = tk.Canvas(self.root, width=vw, height=vh, highlightthickness=0, bd=0,
                              bg="#%02x%02x%02x" % BG_BOTTOM)
        self.view.create_image(0, 0, image=self.view_bg, anchor="nw")
        self.text_item = self.view.create_text(0, 0, text="", anchor="nw", font=self.font, fill=TEXT_STATUS)
        self.view.bind("<MouseWheel>", self._on_wheel)
        self._drag_bindings(self.view)                # the text area moves the bar too
        c.create_window(px(68), top, window=self.view, anchor="nw")
        self.track_item = self.thumb_item = self.hint_item = None
        if len(self.lines) > n:                       # scroll bar
            x = self.W - px(102)
            self.track_item = c.create_rectangle(x, top + px(2), x + px(3), top + n * lh - px(2),
                                                 fill="#3A3646", outline="")
            self.thumb_item = c.create_rectangle(x, top, x + px(3), top, fill=ICON_ACTIVE, outline="")
        if self.input_on:
            y = top + n * lh
            self.hint_item = c.create_text(px(68), y + px(1), text="", anchor="nw",
                                           font=("Segoe UI Variable Text", 9), fill=TEXT_DIM)
            fw, fh = self.W - px(150), px(30)
            field = Image.new("RGBA", (fw * 3, fh * 3), (0, 0, 0, 0))
            ImageDraw.Draw(field).rounded_rectangle([0, 0, fw * 3 - 1, fh * 3 - 1], radius=fh * 3 // 2,
                                                    fill=FIELD_BG, outline=FIELD_LINE, width=3)
            self.field_img = ImageTk.PhotoImage(field.resize((fw, fh), Image.LANCZOS))
            ey = y + px(16) + px(17)
            c.create_image(px(60), ey, image=self.field_img, anchor="w")
            self.entry_item = c.create_window(px(72), ey, window=self.entry, anchor="w", width=fw - px(24))
        self.mic_item = c.create_text(self.W - px(76), oy, text="\uE720", font=self.icon_font, fill=ICON)
        r = px(9)
        stop = Image.new("RGBA", (r * 6, r * 6), (0, 0, 0, 0))
        ImageDraw.Draw(stop).rounded_rectangle([0, 0, r * 6 - 1, r * 6 - 1], radius=r * 2, fill=(238, 234, 248, 255))
        self.stop_img = ImageTk.PhotoImage(stop.resize((r * 2, r * 2), Image.LANCZOS))
        self.stop_item = c.create_image(self.W - px(36), oy, image=self.stop_img)
        for item in (self.mic_item,):
            c.tag_bind(item, "<Button-1>", lambda e: self.actions.put("mic"))
        c.tag_bind(self.stop_item, "<Button-1>", lambda e: self.actions.put("stop"))
        for item in (self.mic_item, self.stop_item):  # buttons get a hand, the rest a move cursor
            c.tag_bind(item, "<Enter>", lambda e: c.config(cursor="hand2"))
            c.tag_bind(item, "<Leave>", lambda e: c.config(cursor="fleur"))
        self._render()

    # moving the bar (2026-11-08) -------------------------------------------------------
    # Press anywhere on the bar except its buttons and the typing box, and drag. The spot is
    # remembered as the bar's bottom-centre point (it still grows upwards from there) in
    # data\voice-bar-position.json; the speaking orb appears there too. Double-click = back
    # to the bottom centre of the screen.
    def _drag_bindings(self, w) -> None:  # noqa: ANN001
        w.bind("<ButtonPress-1>", self._drag_start)
        w.bind("<B1-Motion>", self._drag_move)
        w.bind("<ButtonRelease-1>", self._drag_end)
        w.bind("<Double-Button-1>", self._reset_position)
        w.config(cursor="fleur")

    @staticmethod
    def _load_anchor() -> tuple[int, int] | None:
        try:
            d = json.loads(BAR_POS_FILE.read_text(encoding="utf-8"))
            return int(d["cx"]), int(d["bottom"])
        except Exception:  # noqa: BLE001  (no file yet, or unreadable: use the default)
            return None

    def _anchor(self) -> tuple[int, int]:
        if self.anchor:
            return self.anchor
        return self.root.winfo_screenwidth() // 2, self.root.winfo_screenheight() - self.px(84)

    @staticmethod
    def _clamp(x: int, y: int, w: int, h: int) -> tuple[int, int]:
        """Keep a w x h window fully on the screens (all monitors together)."""
        u32 = ctypes.windll.user32
        vx, vy, vw, vh = (u32.GetSystemMetrics(i) for i in (76, 77, 78, 79))   # virtual screen
        return max(vx, min(x, vx + vw - w)), max(vy, min(y, vy + vh - h))

    def _on_button(self, e) -> bool:  # noqa: ANN001
        if e.widget is not self.canvas:
            return False
        cur = self.canvas.find_withtag("current")
        return bool(cur) and cur[0] in (self.mic_item, self.stop_item)

    def _drag_start(self, e) -> None:  # noqa: ANN001
        self._drag = None if self._on_button(e) else [e.x_root, e.y_root, self.root.winfo_x(), self.root.winfo_y(), False]

    def _drag_move(self, e) -> None:  # noqa: ANN001
        d = self._drag
        if not d:
            return
        dx, dy = e.x_root - d[0], e.y_root - d[1]
        if not d[4] and abs(dx) + abs(dy) < 4:       # still just a click
            return
        d[4] = True
        x, y = self._clamp(d[2] + dx, d[3] + dy, self.W, self.H)
        self.root.geometry(f"+{x}+{y}")

    def _drag_end(self, e) -> None:  # noqa: ANN001
        d, self._drag = self._drag, None
        if not d or not d[4]:
            return
        self.anchor = (self.root.winfo_x() + self.W // 2, self.root.winfo_y() + self.H)
        try:
            BAR_POS_FILE.parent.mkdir(parents=True, exist_ok=True)
            BAR_POS_FILE.write_text(json.dumps({"cx": self.anchor[0], "bottom": self.anchor[1]}), encoding="utf-8")
        except OSError as exc:
            log.warning("Couldn't save the bar's position: %s", exc)
        log.info("Bar moved")

    def _reset_position(self, e=None) -> None:  # noqa: ANN001
        if e is not None and self._on_button(e):
            return
        self.anchor = None
        try:
            BAR_POS_FILE.unlink()
        except OSError:
            pass
        log.info("Bar moved back to the bottom centre")
        self._layout()

    def _max_scroll(self) -> float:
        return max(0.0, len(self.lines) * self.line_h - self.view_h)

    def _render(self) -> None:
        self.view.itemconfig(self.text_item, text="\n".join(self.lines), fill=TEXT_DIM if self.dim else TEXT)
        self._place()
        if self.hint_item:
            self.canvas.itemconfig(self.hint_item, text=self.hint)

    def _place(self) -> None:
        """Move the text to the current scroll position and update the scroll bar."""
        self.scroll_px = max(0.0, min(self.scroll_px, self._max_scroll()))
        self.view.coords(self.text_item, 0, -round(self.scroll_px))
        if self.thumb_item:
            total = len(self.lines) * self.line_h
            span = self.view_h - self.px(4)
            y0 = self.text_top + self.px(2) + span * self.scroll_px / total
            x0, _, x1, _ = self.canvas.coords(self.track_item)
            self.canvas.coords(self.thumb_item, x0, y0, x1, y0 + max(self.px(8), span * self.view_h / total))

    def _scroll_to(self, target: float) -> None:
        """Glide (ease out) towards target pixels, ~60 frames a second."""
        self.scroll_target = max(0.0, min(target, self._max_scroll()))
        if self._anim is None:
            self._animate()

    def _animate(self) -> None:
        gap = self.scroll_target - self.scroll_px
        if abs(gap) < 0.5:
            self.scroll_px, self._anim = self.scroll_target, None
            self._place()
            return
        self.scroll_px += gap * 0.2
        self._place()
        self._anim = self.root.after(16, self._animate)

    def set_text(self, text: str, dim: bool = False) -> None:
        self.text, self.dim = text, dim
        self.lines, self.auto = self._wrap(text), None
        self.scroll_px = self.scroll_target = 0.0
        n = min(len(self.lines), self.MAX_LINES)
        had_bar = self.thumb_item is not None
        if n != self.n_vis or had_bar != (len(self.lines) > n):
            self.n_vis = n
            self._layout()
        else:
            self._render()

    def set_hint(self, text: str) -> None:
        self.hint = text
        if self.hint_item:
            self._render()

    def _on_wheel(self, e) -> None:  # noqa: ANN001
        if len(self.lines) <= self.n_vis:
            return
        self.auto = None                              # you're reading: stop following the voice
        self._scroll_to(self.scroll_target - e.delta / 120 * self.line_h * 1.5)

    def show(self) -> None:
        if not self.visible:
            self.visible = True
            self._layout()
            self.root.deiconify()
            self.root.attributes("-topmost", True)
            self.root.lift()

    # golden speaking orb ----------------------------------------------------------
    def _orb_place(self) -> None:
        cx, bottom = self._anchor()                         # where the bar is (or was dragged to)
        self.orb_xy = self._clamp(cx - self.orb_px // 2, bottom - self.orb_px + self.px(16), self.orb_px, self.orb_px)
        self.orb_win.geometry(f"{self.orb_px}x{self.orb_px}+{self.orb_xy[0]}+{self.orb_xy[1]}")

    def _orb_show(self) -> None:
        self.orb_on = True
        self.gold.start()                                   # grows in
        self._orb_place()
        self.root.withdraw()                                # no text bar while he speaks
        self.alpha_win.clear(*self.orb_xy)
        self.orb_win.deiconify()
        self.orb_win.attributes("-topmost", True)
        if self._orb_after is None:
            self._orb_step()

    def _orb_hide(self, restore_bar: bool = True) -> None:
        self.orb_on = False
        if self._orb_after is not None:
            self.root.after_cancel(self._orb_after)
            self._orb_after = None
        self.alpha_win.clear(*self.orb_xy)                  # no stale frame next time
        self.orb_win.withdraw()
        self.voice_level = 0.0
        if restore_bar and self.visible:                    # the bar comes back for a follow-up
            self.root.deiconify()
            self.root.attributes("-topmost", True)
            self.root.lift()
            if self.input_on:                               # the typing box needs the focus again
                self.root.after(150, self._take_focus)

    def _orb_step(self) -> None:
        """About 30 frames a second while the orb is on screen."""
        self._orb_after = None
        if not self.orb_on:
            return
        try:
            if not self.alpha_win.show(self.gold.frame_bgra(self.voice_level, self.voice_level > 0), *self.orb_xy):
                log.warning("Orb: UpdateLayeredWindow failed (error %s)", ctypes.get_last_error())
        except Exception:  # noqa: BLE001  (a drawing glitch must never break the bar)
            log.exception("Orb frame failed")
        self._orb_after = self.root.after(33, self._orb_step)

    def hide(self) -> None:
        if self.orb_on:
            self._orb_hide(restore_bar=False)
        if self.visible:
            self.visible = False
            self.set_input(False)
            self.root.withdraw()
            if self.prev_fg:                   # give focus back to what you were using
                ctypes.windll.user32.SetForegroundWindow(self.prev_fg)
                self.prev_fg = 0

    # typing box (Wispr Flow / keyboard) ----------------------------------------
    def set_input(self, on: bool) -> None:
        if on == self.input_on:
            return
        self.input_on = on
        self._muted = True                     # clearing the box is not "typing"
        self.entry_var.set("")
        self._muted = False
        self._layout()
        if on:
            # Give the bar a moment to actually appear: Windows won't focus a
            # window that isn't on screen yet.
            self.root.after(120, self._take_focus)
            if self.test_typing:               # --type-after: pretend Wispr typed this
                delay, text = self.test_typing
                self.test_typing = None
                for i, ch in enumerate(text):
                    self.root.after(int(delay * 1000) + i * 40, lambda c=ch: self.entry.insert("end", c))

    def _take_focus(self, tries_left: int = 5) -> None:
        """Wispr types into whichever box has keyboard focus, so take it.

        Windows only lets the foreground app hand out focus; attaching to the
        foreground window's input queue for a moment is the standard way.
        """
        if not self.input_on or not self.visible or self.orb_on:    # (bar hidden behind the orb)
            return
        u32 = ctypes.windll.user32
        self.root.update_idletasks()
        hwnd = u32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
        fg = u32.GetForegroundWindow()
        if fg and fg != hwnd:
            self.prev_fg = self.prev_fg or fg
        for attempt in range(3):
            fg = u32.GetForegroundWindow()
            if fg == hwnd:
                break
            fg_thread = u32.GetWindowThreadProcessId(fg, None)
            me = ctypes.windll.kernel32.GetCurrentThreadId()
            attached = bool(fg_thread) and fg_thread != me and u32.AttachThreadInput(me, fg_thread, True)
            if attempt:
                # Windows refuses focus changes unless the Alt key is down (as
                # in Alt+Tab). Alt is released only after our window has the
                # focus, so the other app never sees an Alt tap.
                u32.keybd_event(0x12, 0, 0, 0)
            u32.SetForegroundWindow(hwnd)
            u32.BringWindowToTop(hwnd)
            if attempt:
                u32.keybd_event(0x12, 0, 2, 0)
            if attached:
                u32.AttachThreadInput(me, fg_thread, False)
            self.root.update()
        self.root.focus_force()
        self.entry.focus_set()
        ok = u32.GetForegroundWindow() == hwnd
        if not ok and tries_left > 1:
            self.root.after(200, lambda: self._take_focus(tries_left - 1))
            return
        log.info("Typing box ready (has focus: %s)", ok)

    def _key_filter(self, e):  # noqa: ANN001
        """Symbols pressed into an empty box (a "+" or "`" while reaching for
        Wispr's shortcut) are not typed; a reminder of the shortcut shows instead."""
        if (e.char and e.char.isprintable() and not e.char.isalnum()
                and not re.search(r"\w", self.entry_var.get())):
            self.set_text(f"For Wispr, hold {keys_label(wispr_ptt_keys())}", True)
            return "break"
        return None

    def _on_typed(self) -> None:
        # Ignore stray symbols (e.g. a ` from the Wispr shortcut leaking through).
        if self._muted or not self.input_on or not re.search(r"\w", self.entry_var.get()):
            return
        if self._settle is None:
            self.actions.put(("typing", None))  # stop Jarvis's own listening at once
        else:
            self.root.after_cancel(self._settle)
        # Wispr types the whole phrase quickly; send once it has stopped changing.
        self._settle = self.root.after(900, self._submit_typed)

    def _submit_typed(self) -> None:
        if self._settle is not None:
            self.root.after_cancel(self._settle)
            self._settle = None
        text = self.entry_var.get().replace("`", "").strip()
        if re.search(r"\w", text):
            self.actions.put(("typed", text))
            self._muted = True
            self.entry_var.set("")
            self._muted = False

    # animation + messages from the worker ---------------------------------------
    def _tick(self) -> None:
        try:
            while True:
                kind, value = self.q.get_nowait()
                if kind == "show":
                    self.show()
                elif kind == "hide":
                    self.hide()
                elif kind == "text":
                    self.set_text(*value)
                elif kind == "input":
                    self.set_input(value)
                elif kind == "hint":
                    self.set_hint(value)
                elif kind == "speaking_for":          # seconds of speech: follow it
                    if len(self.lines) > self.n_vis:
                        self.auto = (time.time(), max(1.0, float(value)))
                elif kind == "mode":
                    self.mode = value
                elif kind == "level":
                    self.level = value
                elif kind == "voice":                 # loudness 0..1 of the reply being spoken
                    self.voice_level = float(value)
                elif kind == "quit":
                    self.root.destroy()
                    return
        except queue.Empty:
            pass
        want_orb = self.visible and self.mode == "speaking"
        if want_orb and not self.orb_on:
            self._orb_show()
        elif not want_orb and self.orb_on:
            self._orb_hide()
        if self.visible:
            speed = {"listening": 1, "thinking": 3, "speaking": 2}.get(self.mode, 1)
            self.frame_i = (self.frame_i + speed) % len(self.frames[0])
            lvl = self.level if self.mode in ("listening", "speaking") else (2 if self.mode == "thinking" else 0)
            self.canvas.itemconfig(self.orb_item, image=self.frames[min(lvl, 3)][self.frame_i])
            self.canvas.itemconfig(self.mic_item, fill=ICON_ACTIVE if self.mode == "listening" else ICON)
            if self.auto:                     # glide along with the spoken reply
                t0, dur = self.auto
                progress = min(1.0, (time.time() - t0) / dur)
                # keep the part being spoken a little above the middle of the view
                target = progress * len(self.lines) * self.line_h - self.view_h * 0.4
                if target > self.scroll_target:
                    self._scroll_to(target)
                if progress >= 1:
                    self.auto = None
        self.root.after(50, self._tick)


# ---------------------------------------------------------------------------
# Voice worker (wake word -> listen -> Jarvis -> speak)
# ---------------------------------------------------------------------------
class Voice(threading.Thread):
    def __init__(self, ui: queue.Queue, actions: queue.Queue, simulate: str | None) -> None:
        super().__init__(daemon=True)
        self.ui, self.actions, self.simulate = ui, actions, simulate
        self.frames: queue.Queue = queue.Queue(maxsize=200)
        self.history: list[dict] = []
        self.last_active = 0.0
        self.noise = 300.0
        self.http = httpx.Client(base_url=API, timeout=httpx.Timeout(240, connect=5))
        self.stop_flag = threading.Event()
        self.ptt = wispr_ptt_keys()               # Wispr Flow's push-to-talk shortcut
        self.ptt_label = keys_label(self.ptt)
        from scipy.signal import butter, sosfilt_zi

        self.sos = butter(4, HIGHPASS_HZ, btype="highpass", fs=RATE, output="sos")
        self.zi = sosfilt_zi(self.sos) * 0.0
        from openwakeword.vad import VAD

        self.vad = VAD()
        self.prob = 0.0

    def say(self, kind, value=None):
        self.ui.put((kind, value))

    # audio input ---------------------------------------------------------------
    def _mic(self) -> None:
        def cb(indata, n, t, status):  # noqa: ANN001
            try:
                self.frames.put_nowait(indata[:, 0].copy())
            except queue.Full:
                pass
        while True:
            try:
                with sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=FRAME, callback=cb):
                    log.info("Microphone open: %s", sd.query_devices(kind="input")["name"])
                    while True:
                        time.sleep(1)
            except Exception as exc:  # noqa: BLE001 - mic unplugged, privacy off, ...
                log.warning("Microphone unavailable (%s); retrying in 10 s", exc)
                time.sleep(10)

    def _feed_file(self) -> None:
        audio, sr = sf.read(self.simulate, dtype="int16")
        if audio.ndim > 1:
            audio = audio[:, 0]
        audio = np.concatenate([np.zeros(RATE, np.int16), audio, np.zeros(RATE * 20, np.int16)])
        for i in range(0, len(audio) - FRAME, FRAME):
            self.frames.put(audio[i:i + FRAME])
            time.sleep(FRAME / RATE)

    def next_frame(self, timeout: float = 1.0):
        """Next 80 ms of audio with the room's low rumble filtered out."""
        from scipy.signal import sosfilt

        try:
            f = self.frames.get(timeout=timeout)
        except queue.Empty:
            return None
        # self.prob: 0-1, how much this frame sounds like someone talking.
        try:
            self.prob = float(self.vad.predict(f, frame_size=640))
        except Exception:  # noqa: BLE001
            self.prob = 0.0
        y, self.zi = sosfilt(self.sos, f.astype(np.float32), zi=self.zi)
        return np.clip(y, -32768, 32767).astype(np.int16)

    def drain(self) -> None:
        while not self.frames.empty():
            self.frames.get_nowait()

    # main loop -------------------------------------------------------------------
    def run(self) -> None:
        from openwakeword.model import Model

        self.wake = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
        threading.Thread(target=self._feed_file if self.simulate else self._mic, daemon=True).start()
        # Small, fast recogniser for the "did they say Jarvis?" check, on the
        # CPU, so Jarvis's own (bigger) recogniser stays free for requests.
        from faster_whisper import WhisperModel

        try:   # use the downloaded copy; only fetch it if it's missing
            self.quick = WhisperModel("tiny.en", device="cpu", compute_type="int8", cpu_threads=4,
                                      local_files_only=True)
        except Exception:  # noqa: BLE001
            self.quick = WhisperModel("tiny.en", device="cpu", compute_type="int8", cpu_threads=4)
        log.info("Listening for 'Jarvis'")
        cooldown = 0.0
        pre: collections.deque = collections.deque(maxlen=4)          # 0.32 s before speech
        recent: collections.deque = collections.deque(maxlen=12)      # last ~1 s, for late wakes
        quiet: collections.deque = collections.deque(maxlen=150)      # ~12 s of levels
        checks: collections.deque = collections.deque(maxlen=12)      # times of recent checks
        seg, seg_silence, quiet_run = None, 0.0, 0.0
        last_inbox = 0.0
        talk_since = None
        while True:
            act = self._poll_action()
            if act == "mic":
                self.converse()
                seg = None
                continue
            if keys_down([TALK_KEY]):            # hold-to-talk
                talk_since = talk_since or time.time()
                if time.time() - talk_since >= TALK_HOLD:
                    talk_since = None
                    self.converse(lead=list(recent)[-6:], talk=True)
                    self.wake.reset()
                    self.drain()
                    seg, cooldown = None, time.time() + 1.0
                    if self.simulate:
                        break
                    continue
            else:
                talk_since = None
            if time.time() - last_inbox > 1.0 and seg is None:
                last_inbox = time.time()
                note = self._inbox()
                if note:
                    self.announce(note)
                    self.wake.reset()
                    self.drain()
                    seg, cooldown = None, time.time() + 1.5
                    continue
            f = self.next_frame()
            if f is None:
                continue
            rms = float(np.sqrt(np.mean(f.astype(np.float32) ** 2)))
            if seg is None:
                quiet.append(rms)
            # The room's quiet level, from the quietest moments recently heard.
            self.noise = float(np.percentile(quiet, 15)) if len(quiet) > 20 else 100.0
            speaking = self.prob >= (SPEECH_KEEP if seg is not None else SPEECH_PROB)
            score = max(self.wake.predict(f).values())
            recent.append(f)
            woke, command, lead = False, None, None

            # 1) Fast path: a clear "Hey Jarvis" from the wake-word model.
            if score >= DIRECT_THRESHOLD and time.time() > cooldown:
                log.info("Wake word (clear, score %.2f)", score)
                # This model often fires a little late, after the first words
                # of the request: keep the last second so they aren't lost.
                woke, lead = True, list(recent)
            # 2) Short phrases said after a pause: does it start with "Jarvis"?
            #    Handles plain "Jarvis", accents and "Jarvis, <request>".
            elif seg is None:
                if speaking:
                    if quiet_run >= 0.5:                           # people pause before addressing Jarvis
                        seg, seg_silence = list(pre) + [f], 0.0
                    quiet_run = 0.0
                else:
                    quiet_run += FRAME / RATE
            else:
                seg.append(f)
                seg_silence = seg_silence + FRAME / RATE if not speaking else 0.0
                if len(seg) * FRAME / RATE > 4.0:                 # too long for a wake phrase
                    seg, quiet_run = None, 0.0
                elif seg_silence >= 0.4:
                    spoken = len(seg) * FRAME / RATE - seg_silence
                    busy = len(checks) == checks.maxlen and time.time() - checks[0] < 60
                    if spoken >= 0.3 and time.time() > cooldown and not busy:
                        checks.append(time.time())
                        command = wake_command(self.quick_text(seg))
                        # Numbers only (no words), to see that phrases are being checked.
                        log.info("Checked a %.1f s phrase: %s", spoken,
                                 "starts with Jarvis" if command is not None else "not for Jarvis")
                        if command:
                            # The request was in the same phrase: show the bar now,
                            # then get the request's exact words from Jarvis.
                            self.say("show")
                            self.say("mode", "thinking")
                            self.say("text", ("...", True))
                            exact = self.transcribe(seg, timeout=30) or ""
                            command = wake_command(exact) or command
                        if command is not None:
                            # Only phrases that start with "Jarvis" are ever logged.
                            log.info("Wake word (heard 'Jarvis'%s)", f", request: {command!r}" if command else "")
                            woke = True
                    seg, quiet_run = None, seg_silence
            pre.append(f)
            if woke:
                self.wake.reset()
                self.converse(first_text=command or None, lead=lead)
                self.wake.reset()
                self.drain()
                seg, cooldown = None, time.time() + 1.5
                if self.simulate:
                    break
        self.say("quit")

    def _inbox(self) -> str | None:
        """Next message a reminder or timer left for the bar to say, oldest first
        (files older than 10 minutes are dropped: they're stale)."""
        try:
            files = sorted(INBOX.glob("*.json"))
        except OSError:
            return None
        for f in files:
            try:
                fresh = time.time() - f.stat().st_mtime < 600
                text = json.loads(f.read_text(encoding="utf-8")).get("say", "") if fresh else ""
            except Exception:  # noqa: BLE001
                text = ""
            try:
                f.unlink()
            except OSError:
                pass
            if text:
                return text
        return None

    def announce(self, text: str) -> None:
        """Show and say a reminder/timer, then hide. Esc or the stop button silences it."""
        log.info("Announcing a reminder or timer")
        try:
            self.say("show")
            self.say("mode", "speaking")
            self.say("text", (text, False))
            sd.play(chime(), 24000)
            time.sleep(0.5)
            self.speak(text)
            time.sleep(1.5)
        finally:
            while not self.actions.empty():      # a "stop" here shouldn't end the next chat
                self.actions.get_nowait()
            self.say("mode", "idle")
            self.say("text", ("Say something...", True))
            self.say("hide")

    def quick_text(self, frames: list) -> str:
        """Local tiny.en transcript of a short phrase (about 1 s on this CPU)."""
        audio = np.concatenate(frames).astype(np.float32) / 32768.0
        try:
            segs, _ = self.quick.transcribe(audio, beam_size=1, language="en", vad_filter=True,
                                            condition_on_previous_text=False)
            text = " ".join(s.text.strip() for s in segs)
        except Exception as exc:  # noqa: BLE001
            log.warning("Quick check failed: %s", exc.__class__.__name__)
            return ""
        return "" if made_up(text) else text

    def speech_threshold(self) -> float:
        """Loudness that counts as speech: about twice the room's steady
        background level (min 150). Measured 2026-11-07: a quiet room sits near
        60, but with a fan or similar running the background is 1300-2000, and
        a fixed level made the whole room look like speech."""
        return max(self.noise * 2.0, 150.0)

    def transcribe(self, frames: list, timeout: float = 60) -> str | None:
        buf = io.BytesIO()
        sf.write(buf, np.concatenate(frames), RATE, format="WAV", subtype="PCM_16")
        try:
            # English only: auto-detection turned noise into Hindi/Sanskrit text.
            r = self.http.post("/v1/speech/transcribe", timeout=timeout, data={"language": "en"},
                               files={"file": ("speech.wav", buf.getvalue(), "audio/wav")})
            return (r.json().get("text") or "").strip()
        except Exception as exc:  # noqa: BLE001
            log.warning("Transcription failed: %s", exc.__class__.__name__)
            return None

    def _poll_action(self):
        try:
            return self.actions.get_nowait()
        except queue.Empty:
            return None

    def converse(self, first_text: str | None = None, lead: list | None = None, talk: bool = False) -> None:
        """Show the bar and talk until silence. first_text = request already heard;
        lead = audio from just before the wake (may hold the request's start);
        talk = started by holding the talk key (record while it's held)."""
        try:
            self._converse(first_text, lead, talk)
        except Exception:  # noqa: BLE001 - never let one bad turn stop the wake word
            log.exception("Conversation failed; back to listening for 'Jarvis'")
            self.say("input", False)
            self.say("mode", "idle")
            self.say("hide")

    def _converse(self, first_text: str | None, lead: list | None, talk: bool = False) -> None:
        if time.time() - self.last_active > 180:
            self.history = []
        self.say("show")
        if not talk:                              # (a chime would be recorded while talking)
            sd.play(chime(), 24000)
        first = True
        while True:
            if first and talk:
                text = self.hold_to_talk(lead)
            elif first and first_text:
                text = first_text
                self.say("text", (text, False))
            else:
                text = self.listen(wait=8 if first else FOLLOW_UP_SECONDS, lead=lead if first else None)
            if text is None:                     # stopped, or nothing said
                break
            if re.fullmatch(r"\W*(stop|cancel|never ?mind|nothing|that'?s all|thanks?,? that'?s all)\W*",
                            text, re.I):
                self.say("text", ("Okay.", False))
                time.sleep(0.8)
                break
            if not self.reply(text):
                break
            first = False
        self.say("mode", "idle")
        self.say("text", ("Say something...", True))
        self.say("hide")
        self.last_active = time.time()

    def listen(self, wait: float, lead: list | None = None):
        """Record one utterance; return its text, or None if silent/stopped.

        Audio queued right after the wake word is kept (not drained), so the
        start of a quickly spoken request isn't lost.

        Wispr Flow can take the request instead: while listening, the bar has
        a typing box with the keyboard focus. Holding Wispr's shortcut, or text
        appearing in the box, hands over to Wispr and its typed text is used.
        """
        self.say("mode", "listening")
        self.say("input", True)
        if wait > FOLLOW_UP_SECONDS:
            self.say("text", ("Listening...", True))
            self.say("hint", "Speak now, or hold Right Ctrl and talk")
        else:            # follow-up: keep the last reply on screen so it can still be read
            self.say("hint", "Anything else? Speak, or hold Right Ctrl and talk")
        start = time.time()
        threshold = self.speech_threshold()
        voiced, silent, got_speech, chunks = 0, 0.0, False, list(lead or [])
        loudest, speech_peak, speech_time, best_prob = 0.0, 0.0, 0.0, 0.0
        while True:
            act = self._poll_action()
            if act == "stop":
                log.info("Stopped (stop button or Esc)")
                self.say("input", False)
                sd.stop()
                return None
            if act == "mic" and got_speech:
                break
            if isinstance(act, tuple) and act[0] == "typed":
                return self._use_typed(act[1])
            if (isinstance(act, tuple) and act[0] == "typing") or keys_down(self.ptt):
                return self._wait_for_wispr(typing=isinstance(act, tuple))
            if keys_down([TALK_KEY]):
                self.say("input", False)
                return self.hold_to_talk(chunks[-6:])
            f = self.next_frame(timeout=0.5)
            if f is None:
                if time.time() - start > wait and not got_speech:
                    break
                continue
            rms = float(np.sqrt(np.mean(f.astype(np.float32) ** 2)))
            loudest = max(loudest, rms)
            prob = self.prob
            best_prob = max(best_prob, prob)
            self.say("level", 0 if prob < SPEECH_KEEP else (1 if rms < threshold * 2 else (2 if rms < threshold * 4 else 3)))
            # Speech vs. not-speech comes from the speech detector, so the
            # room's steady rumble can neither start nor prolong a recording.
            if prob >= (SPEECH_KEEP if got_speech or voiced else SPEECH_PROB):
                voiced += 1                      # 3 loud frames in a row (240 ms) = speech;
                silent = 0.0                     # the short start-up chime is only ~2
                if got_speech:
                    speech_time += FRAME / RATE
                    speech_peak = max(speech_peak, rms)
                elif voiced >= 3:
                    got_speech, speech_time = True, 3 * FRAME / RATE
                    speech_peak = rms
            else:
                voiced = 0
                if got_speech:
                    silent += FRAME / RATE
            chunks.append(f)
            if got_speech and silent > 0.9:
                if speech_time >= 0.3:
                    break
                # Only a blip (e.g. the end of "Jarvis" itself): keep waiting.
                got_speech, voiced, silent, speech_time, speech_peak = False, 0, 0.0, 0.0, 0.0
            if not got_speech and time.time() - start > wait:
                break
            if time.time() - start > 12:
                break
        self.say("input", False)
        # Numbers only (no audio), to tune this to the room and microphone.
        log.info("Request: %s; most speech-like %.2f (needs %.2f), room level %.0f, loudest %.0f, "
                 "speech %.1f s of %.1f s", "heard speech" if got_speech else "nothing heard", best_prob,
                 SPEECH_PROB, self.noise, loudest, speech_time, len(chunks) * FRAME / RATE)
        if not got_speech:
            return None
        return self._understand(chunks, again=lambda: self.listen(wait))

    def _understand(self, chunks: list, again=None):
        """Send recorded audio to Jarvis's recogniser; return the request text or None.
        again(): what to do if it was only the wake word (keep listening)."""
        self.say("level", 0)
        self.say("mode", "thinking")
        self.say("text", ("...", True))
        text = self.transcribe(chunks)
        if text is None:
            self.say("text", ("I can't reach Jarvis right now. Is it still starting?", False))
            time.sleep(3)
            return None
        if text and made_up(text):
            log.info("Ignored a phrase the recogniser made up from noise")
            text = ""
        else:
            log.info("Heard: %s", text)
            rest = wake_command(text)            # "Jarvis, open X" -> "open X"
            if rest == "" and again is not None:
                log.info("That was just the wake word; still listening")
                return again()
            if rest:
                text = rest
        if not text:
            self.say("text", ("Sorry, I didn't catch that.", False))
            time.sleep(1.5)
            return None
        self.say("text", (text, False))
        return text

    def hold_to_talk(self, lead: list | None = None):
        """Record exactly while the talk key (Right Ctrl) is held, then understand it.
        Needs no speech detection, so it works next to a loud fan or AC."""
        log.info("Hold-to-talk: recording")
        self.say("show")
        self.say("input", False)
        self.say("mode", "listening")
        self.say("text", ("Listening... let go of Right Ctrl when you're done", True))
        chunks, start = list(lead or []), time.time()
        threshold = self.speech_threshold()
        while keys_down([TALK_KEY]) and time.time() - start < 60:
            f = self.next_frame(timeout=0.5)
            if f is None:
                continue
            chunks.append(f)
            rms = float(np.sqrt(np.mean(f.astype(np.float32) ** 2)))
            self.say("level", 1 if rms < threshold * 2 else (2 if rms < threshold * 4 else 3))
        end = time.time() + 0.3                  # people let go on their last syllable
        while time.time() < end:
            f = self.next_frame(timeout=0.4)
            if f is not None:
                chunks.append(f)
        held = time.time() - start
        log.info("Hold-to-talk: %.1f s recorded", held)
        if held < 0.6:
            self.say("text", ("Hold Right Ctrl while you speak, then let go.", False))
            time.sleep(1.8)
            return None
        return self._understand(chunks)

    def _use_typed(self, text: str) -> str:
        self.say("input", False)
        self.say("level", 0)
        log.info("Typed (Wispr Flow or keyboard): %s", text)
        self.say("text", (text, False))
        return text

    def _wait_for_wispr(self, typing: bool) -> str | None:
        """Wispr Flow is taking this request: stop listening ourselves and wait
        for the text it types into the box (it types once you let go)."""
        log.info("Handing the request to Wispr Flow (%s)", "text appeared" if typing else "shortcut held")
        self.say("level", 0)
        self.say("mode", "thinking" if typing else "listening")
        self.say("text", ("Wispr is typing..." if typing else "Wispr is listening...", True))
        last, act = time.time(), None
        while time.time() - last < (20 if typing else 10):
            act = self._poll_action()
            if act == "stop":
                break
            if isinstance(act, tuple) and act[0] == "typed":
                return self._use_typed(act[1])
            if isinstance(act, tuple) and act[0] == "typing":
                typing, last = True, time.time()
                self.say("mode", "thinking")
                self.say("text", ("Wispr is typing...", True))
            elif not typing and keys_down(self.ptt):
                last = time.time()                 # still holding the shortcut
            self.drain()                           # not listening ourselves meanwhile
            time.sleep(0.05)
        self.say("input", False)
        log.info("Wispr Flow typed nothing into the box")
        if act != "stop":
            self.say("text", ("Nothing came through from Wispr.", False))
            time.sleep(1.5)
        return None

    def reply(self, text: str) -> bool:
        self.say("mode", "thinking")
        self.history.append({"role": "user", "content": text})
        try:
            model = self.http.get("/v1/info").json().get("model", "")
            r = self.http.post("/v1/chat/completions", json={
                "model": model, "stream": False, "temperature": 0.5, "max_tokens": 600,
                "messages": [{"role": "system", "content": VOICE_HINT}] + self.history[-10:],
            })
            answer = r.json()["choices"][0]["message"]["content"].strip()
        except Exception as exc:  # noqa: BLE001
            log.warning("Chat failed: %s", exc)
            self.say("text", ("Sorry, Jarvis didn't answer. Is it running?", False))
            time.sleep(3)
            return False
        log.info("Reply: %s", answer)
        self.history.append({"role": "assistant", "content": answer})
        self.say("text", (readable(answer), False))
        self.speak(speakable(answer))
        return True

    def speak(self, text: str) -> None:
        if not text:
            return
        self.say("mode", "speaking")
        try:
            wav = self.http.post("/v1/speech/synthesize", json={"text": text}).content
            data, sr = sf.read(io.BytesIO(wav), dtype="float32")
        except Exception as exc:  # noqa: BLE001
            log.warning("Speech failed: %s", exc)
            return
        sd.play(data, sr)
        start = time.time()
        end = start + len(data) / sr
        self.say("speaking_for", len(data) / sr)     # the bar scrolls along with the voice
        mono = data if data.ndim == 1 else data.mean(axis=1)
        while time.time() < end:
            if self._poll_action() == "stop":
                sd.stop()
                self.actions.put("stop")   # also end the conversation
                break
            if keys_down([TALK_KEY]):      # holding the talk key interrupts: you're replying
                sd.stop()
                break
            # How loud the voice is right now drives the orbs (2026-11-08: real loudness).
            i = int((time.time() - start) * sr)
            seg = mono[i:i + sr // 20]
            amp = float(np.sqrt(np.mean(seg * seg))) if len(seg) else 0.0
            self.say("voice", min(1.0, amp * 6))
            self.say("level", 2 if amp > 0.04 else 1)
            time.sleep(0.05)
        self.say("voice", 0.0)
        self.say("level", 0)
        self.drain()                         # don't treat our own voice as the next request


def main() -> None:
    simulate = sys.argv[sys.argv.index("--simulate") + 1] if "--simulate" in sys.argv else None
    # pythonw has no console: make sure a crash in the worker reaches the log.
    threading.excepthook = lambda a: log.error("Voice worker stopped", exc_info=(a.exc_type, a.exc_value,
                                                                                 a.exc_traceback))
    ui, actions = queue.Queue(), queue.Queue()
    popup = Popup(ui, actions)
    if "--type-after" in sys.argv:   # test: pretend Wispr types TEXT after SECONDS
        i = sys.argv.index("--type-after")
        popup.test_typing = (float(sys.argv[i + 1]), sys.argv[i + 2])
    Voice(ui, actions, simulate).start()
    log.info("Voice pop-up started%s", " (simulation)" if simulate else "")
    popup.root.mainloop()


if __name__ == "__main__":
    main()
