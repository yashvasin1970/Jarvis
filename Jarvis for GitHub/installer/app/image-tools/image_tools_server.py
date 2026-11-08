"""Jarvis "create image" tool, as a minimal MCP server over stdio.

Tool: create_image(prompt, shape, open). Makes a picture with NVIDIA's hosted
FLUX image models (free with the same NVIDIA key Jarvis already uses), saves it
as a JPG in Desktop\\Images and, by default, opens it on screen.

Models, tried in order (compared 2026-11-07 with this account's key on the same
prompt; FLUX.3 at 50 steps was clearly best: realistic, correct sign text):
  black-forest-labs/flux-3-image-klein  50 steps, ~9 s, best  <- main
      (4 steps gave a dark, grainy picture; NVIDIA's reference is 16+ steps;
       max ~1 megapixel, sides a multiple of 16)
  black-forest-labs/flux.2-klein-4b     4 steps (its max), ~3-13 s, good, misspells text
  black-forest-labs/flux.1-schnell      timed out that day     <- last resorts
  black-forest-labs/flux.1-dev          server error that day
Not available to this key (404): flux.2-klein-9b, flux.2-dev, stable-diffusion-3.5-large.

Also edit_photo (local Pillow/rembg edits) and restyle_photo (vision description +
FLUX.3), added 2026-11-07.

The key is read from NVIDIA-API-KEY.txt in the Jarvis folder (the file the user
pastes it into), so a new key works after a Jarvis restart.
Runs in its own environment (image-tools\\.venv: pillow, rembg, numpy), which
Jarvis updates don't touch. Speaks JSON-RPC 2.0, one message per line, on stdin/stdout.
"""

from __future__ import annotations

import base64
import ctypes  # noqa: F401  (also used by look_at_screen)
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

PROTOCOL = "2025-06-18"
# The Jarvis folder (this file is in its image-tools\ subfolder), so it works wherever
# Jarvis is installed (also from the setup kit for other PCs).
ROOT = Path(__file__).resolve().parent.parent
KEY_FILE = ROOT / "NVIDIA-API-KEY.txt"
API = "https://ai.api.nvidia.com/v1/genai/"
# ~1 megapixel each (the hosted models' limit); wide/tall are 16:9 / 9:16.
SHAPES = {"square": (1024, 1024), "wide": (1360, 768), "tall": (768, 1360)}
MODELS = [
    ("black-forest-labs/flux-3-image-klein", lambda p, w, h, s: {"prompt": p, "width": w, "height": h,
                                                               "seed": s, "steps": 50}, 90),
    ("black-forest-labs/flux.2-klein-4b", lambda p, w, h, s: {"prompt": p, "width": w, "height": h,
                                                            "seed": s, "steps": 4}, 90),
    ("black-forest-labs/flux.1-schnell", lambda p, w, h, s: {"prompt": p, "width": w, "height": h,
                                                           "seed": s, "steps": 4}, 120),
    ("black-forest-labs/flux.1-dev", lambda p, w, h, s: {"prompt": p, "mode": "base", "cfg_scale": 3.5,
                                                       "width": w, "height": h, "seed": s, "steps": 30}, 150),
]


def desktop() -> Path:
    class GUID(ctypes.Structure):
        _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort), ("c", ctypes.c_ushort),
                    ("d", ctypes.c_ubyte * 8)]

    u = uuid.UUID("B4BFCC3A-DB2C-424C-B029-7FE99A87C641")   # FOLDERID_Desktop (follows OneDrive)
    g = GUID(u.fields[0], u.fields[1], u.fields[2], (ctypes.c_ubyte * 8)(*u.bytes[8:]))
    p = ctypes.c_wchar_p()
    if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(p)) == 0:
        try:
            return Path(p.value)
        finally:
            ctypes.windll.ole32.CoTaskMemFree(p)
    return Path.home() / "Desktop"


def api_key() -> str:
    raw = KEY_FILE.read_text(encoding="utf-8-sig") if KEY_FILE.exists() else os.environ.get("NIM_API_KEY", "")
    key = raw.strip().strip("\"'")
    key = re.sub(r"^(NIM_API_KEY|NVIDIA_API_KEY)\s*=\s*", "", key).strip().strip("\"'")
    if not key.startswith("nvapi-"):
        raise RuntimeError("No NVIDIA key found in NVIDIA-API-KEY.txt")
    return key


def generate(model: str, body: dict, timeout: float) -> bytes:
    req = urllib.request.Request(API + model, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Authorization": f"Bearer {api_key()}",
                                          "Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    art = (data.get("artifacts") or [{}])[0]
    reason = art.get("finishReason", "")
    if reason and reason != "SUCCESS":
        if "FILTER" in reason.upper():
            raise PermissionError("NVIDIA's safety filter blocked this picture")
        raise RuntimeError(f"finished with {reason}")
    if not art.get("base64"):
        raise RuntimeError("no image in the reply")
    return base64.b64decode(art["base64"])


def flux_size(w: int, h: int) -> tuple[int, int]:
    """FLUX.3 size with the photo's shape: <= ~1 megapixel, sides multiples of 16."""
    r = max(0.5, min(2.0, w / max(h, 1)))
    hh = int((1_040_000 / r) ** 0.5) // 16 * 16
    ww = int(r * hh) // 16 * 16
    return max(512, ww), max(512, hh)


# ---- photo editing (added 2026-11-07) --------------------------------------------------
# NVIDIA's free tier only edits its own sample images (every model answered "Expected:
# example_id"), so edits run here on the PC (Pillow; background removal with rembg's
# isnet model in OpenJarvis\models\rembg), and "restyle" = the NVIDIA vision model
# describes the photo + FLUX.3 paints a new picture from that description.
EDITS = ["remove_background", "black_and_white", "sepia", "pencil_sketch", "cartoon", "blur",
         "sharpen", "brighter", "darker", "more_contrast", "less_contrast", "more_colour",
         "less_colour", "rotate_left", "rotate_right", "flip", "crop_square", "resize"]
VISION_MODEL = "nvidia/nemotron-3.5-super-vl-120b-a12b"
_rembg = None


def _allowed_photo(path_str: str) -> Path:
    p = Path(os.path.expandvars(os.path.expanduser((path_str or "").strip().strip('"'))))
    if not p.is_absolute():
        p = desktop() / p
    p = p.resolve()
    d = desktop().resolve()
    if not (p == d or p.is_relative_to(d)):
        raise PermissionError("For privacy I can only edit pictures inside the Desktop folder.")
    if not p.is_file():
        raise FileNotFoundError(f"Picture not found: {p}")
    if p.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".heic"):
        raise ValueError(f"{p.suffix} isn't a picture format I can edit.")
    return p


def _open_photo(p: Path):
    from PIL import Image, ImageOps

    im = Image.open(p)
    im = ImageOps.exif_transpose(im)          # phone photos: respect their rotation
    im.thumbnail((4096, 4096))
    return im


def _apply_edit(im, action: str, amount: float | None, width: int | None):
    import numpy as np
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps

    a = action
    k = (amount if amount is not None else 30) / 100.0          # strength for adjustments
    rgb = im.convert("RGB")
    if a == "remove_background":
        global _rembg
        os.environ.setdefault("U2NET_HOME", str(ROOT / "models" / "rembg"))
        from rembg import new_session, remove

        _rembg = _rembg or new_session("isnet-general-use")
        return remove(rgb, session=_rembg)                       # RGBA, transparent background
    if a == "black_and_white":
        return ImageOps.grayscale(rgb)
    if a == "sepia":
        g = np.asarray(ImageOps.grayscale(rgb), dtype=np.float32)
        out = np.stack([g * 1.07, g * 0.74, g * 0.43], axis=-1) + np.array([30, 18, 8])
        return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))
    if a == "pencil_sketch":
        g = ImageOps.grayscale(rgb)
        inv_blur = ImageOps.invert(g).filter(ImageFilter.GaussianBlur(max(2, min(g.size) // 120)))
        ga, ba = np.asarray(g, dtype=np.float32), np.asarray(inv_blur, dtype=np.float32)
        sketch = np.clip(ga * 255.0 / np.maximum(255.0 - ba, 1.0), 0, 255).astype(np.uint8)
        return ImageEnhance.Contrast(Image.fromarray(sketch)).enhance(1.4)
    if a == "cartoon":
        # Flatten colours on a smoothed copy; draw only the strongest outlines
        # (textured pictures otherwise turn into black speckle).
        work = rgb.copy()
        work.thumbnail((1000, 1000))
        smooth = work
        for _ in range(3):
            smooth = smooth.filter(ImageFilter.MedianFilter(7))
        flat = smooth.quantize(colors=10, method=Image.Quantize.MEDIANCUT).convert("RGB")
        flat = ImageEnhance.Color(flat).enhance(1.3)
        edge = np.asarray(ImageOps.grayscale(smooth).filter(ImageFilter.FIND_EDGES), dtype=np.float32)
        thr = max(float(np.percentile(edge, 93)), 25.0)
        mask = Image.fromarray(((edge > thr) * 255).astype(np.uint8)).filter(ImageFilter.MedianFilter(3))
        out = Image.composite(Image.new("RGB", work.size, (30, 30, 30)), flat, mask)
        return out.resize(rgb.size, Image.LANCZOS)
    if a == "blur":
        return rgb.filter(ImageFilter.GaussianBlur(max(1, (amount or 40) / 10)))
    if a == "sharpen":
        return rgb.filter(ImageFilter.UnsharpMask(radius=2, percent=int(100 + (amount or 50) * 2), threshold=3))
    adjust = {"brighter": (ImageEnhance.Brightness, 1 + k), "darker": (ImageEnhance.Brightness, 1 - k * 0.8),
              "more_contrast": (ImageEnhance.Contrast, 1 + k), "less_contrast": (ImageEnhance.Contrast, 1 - k * 0.8),
              "more_colour": (ImageEnhance.Color, 1 + k * 1.5), "less_colour": (ImageEnhance.Color, 1 - k)}
    if a in adjust:
        cls, factor = adjust[a]
        return cls(rgb if im.mode != "RGBA" else im).enhance(max(0.05, factor))
    if a == "rotate_left":
        return im.rotate(90, expand=True)
    if a == "rotate_right":
        return im.rotate(-90, expand=True)
    if a == "flip":
        return ImageOps.mirror(im)
    if a == "crop_square":
        s = min(im.size)
        x, y = (im.width - s) // 2, (im.height - s) // 2
        return im.crop((x, y, x + s, y + s))
    if a == "resize":
        if not width:
            raise ValueError("Say the new width in pixels for resize.")
        w = max(16, min(8000, int(width)))
        return im.resize((w, max(1, round(im.height * w / im.width))), Image.LANCZOS)
    raise ValueError(f"Unknown edit '{action}'. Options: {', '.join(EDITS)}")


def _save_result(im, src: Path, label: str, open_it: bool) -> Path:
    folder = desktop() / "Images"
    folder.mkdir(parents=True, exist_ok=True)
    keep_png = im.mode in ("RGBA", "LA", "P") or src.suffix.lower() == ".png"
    path = folder / f"{src.stem} ({label}) {time.strftime('%H%M%S')}{'.png' if keep_png else '.jpg'}"
    if keep_png:
        im.save(path)
    else:
        im.convert("RGB").save(path, quality=92)
    if open_it:
        os.startfile(str(path))
    return path


def tool_edit_photo(path: str, action: str, amount: float | None = None, width: int | None = None,
                    open: bool = True) -> str:  # noqa: A002
    action = (action or "").strip().lower().replace(" ", "_").replace("-", "_")
    action = {"remove_bg": "remove_background", "grayscale": "black_and_white", "greyscale": "black_and_white",
              "sketch": "pencil_sketch", "bw": "black_and_white", "more_color": "more_colour",
              "less_color": "less_colour", "mirror": "flip", "square": "crop_square"}.get(action, action)
    try:
        src = _allowed_photo(path)
        t = time.time()
        out = _apply_edit(_open_photo(src), action, amount, width)
        dest = _save_result(out, src, action.replace("_", " "), open)
    except (PermissionError, FileNotFoundError, ValueError) as exc:
        return str(exc)
    return (f"Edited ({action.replace('_', ' ')}) in {time.time() - t:.1f} s"
            + (" and opened it." if open else ".")
            + f" The original is unchanged. [Internal file path, for your use only: {dest}] Don't mention "
            "where it is saved unless the user asks.")


def describe_photo(p: Path) -> str:
    import io

    from PIL import Image

    im = _open_photo(p).convert("RGB")
    im.thumbnail((640, 640))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=82)
    b64 = base64.b64encode(buf.getvalue()).decode()
    # It "thinks" before answering by default and ran out of tokens doing so (content
    # came back None), so thinking is switched off and the budget is generous.
    body = {"model": VISION_MODEL, "max_tokens": 1500, "temperature": 0.2,
            "chat_template_kwargs": {"enable_thinking": False}, "messages": [{"role": "user", "content": [
        {"type": "text", "text": "Describe this image precisely so an artist could repaint it: every subject "
                                 "(appearance, age, hair, clothing, expression, pose), setting, background, "
                                 "composition and camera angle, lighting, colours. One paragraph, about 100 "
                                 "words, no preamble."},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}]}
    req = urllib.request.Request("https://integrate.api.nvidia.com/v1/chat/completions", method="POST",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Authorization": f"Bearer {api_key()}", "Accept": "application/json",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read().decode("utf-8"))
    text = data["choices"][0]["message"].get("content") or ""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    if not text:
        raise RuntimeError("the vision model gave no description")
    return text


def tool_restyle_photo(path: str, style: str, open: bool = True) -> str:  # noqa: A002
    try:
        src = _allowed_photo(path)
        size = _open_photo(src).size
    except (PermissionError, FileNotFoundError, ValueError) as exc:
        return str(exc)
    style = (style or "").strip() or "oil painting"
    t = time.time()
    try:
        desc = describe_photo(src)
    except Exception as exc:  # noqa: BLE001
        return f"Couldn't look at the photo right now ({exc.__class__.__name__}). Try again in a minute."
    w, h = flux_size(*size)
    prompt = f"{desc}\n\nRender this exact scene as: {style}. Keep the same subjects, poses, composition and colours."
    seed = random.randint(1, 2_000_000_000)
    problems = []
    for model, make_body, timeout in MODELS:
        try:
            img = generate(model, make_body(prompt[:2000], w, h, seed), timeout)
            break
        except PermissionError as exc:
            return f"{exc}. Try a different style."
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{model.split('/')[1]}: {exc.__class__.__name__}")
    else:
        return "Couldn't create the restyled picture right now (" + "; ".join(problems) + ")."
    from PIL import Image
    import io

    dest = _save_result(Image.open(io.BytesIO(img)), src, f"restyled {slug(style)}", open)
    return (f"Restyled as '{style}' in {time.time() - t:.0f} s" + (" and opened it." if open else ".")
            + " It's a new picture painted from a description of the photo, so faces and small "
            "details won't match exactly. The original is unchanged. "
            f"[Internal file path, for your use only: {dest}] Don't mention where it is saved unless asked.")


# ---- screen, web pages, YouTube (added 2026-11-07) ---------------------------------------

def _ask_vision(jpeg: bytes, question: str, max_tokens: int = 900) -> str:
    b64 = base64.b64encode(jpeg).decode()
    body = {"model": VISION_MODEL, "max_tokens": max_tokens, "temperature": 0.2,
            "chat_template_kwargs": {"enable_thinking": False}, "messages": [{"role": "user", "content": [
                {"type": "text", "text": question},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}]}
    req = urllib.request.Request("https://integrate.api.nvidia.com/v1/chat/completions", method="POST",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Authorization": f"Bearer {api_key()}", "Accept": "application/json",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read().decode("utf-8"))
    text = re.sub(r"<think>.*?</think>", "", data["choices"][0]["message"].get("content") or "", flags=re.S)
    return text.strip()


def _is_jarvis_title(title: str) -> bool:
    # Edge app window ("OpenJarvis") or a normal Edge tab ("OpenJarvis - Personal - Microsoft Edge")
    return title == "OpenJarvis" or title.startswith(("OpenJarvis - ", "OpenJarvis and "))


def _user32():
    from ctypes import wintypes

    u = ctypes.windll.user32
    H = wintypes.HWND
    for name, args, res in (("GetTopWindow", [H], H), ("GetWindow", [H, wintypes.UINT], H),
                            ("GetForegroundWindow", [], H), ("GetWindowLongW", [H, ctypes.c_int], ctypes.c_long),
                            ("IsWindowVisible", [H], wintypes.BOOL), ("IsIconic", [H], wintypes.BOOL),
                            ("GetWindowTextLengthW", [H], ctypes.c_int),
                            ("GetWindowTextW", [H, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
                            ("GetWindowRect", [H, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
                            ("ShowWindow", [H, ctypes.c_int], wintypes.BOOL),
                            ("SetWindowPos", [H, H, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                              wintypes.UINT], wintypes.BOOL),
                            ("BringWindowToTop", [H], wintypes.BOOL), ("SetForegroundWindow", [H], wintypes.BOOL),
                            ("GetWindowThreadProcessId", [H, ctypes.c_void_p], wintypes.DWORD),
                            ("AttachThreadInput", [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL], wintypes.BOOL)):
        f = getattr(u, name)
        f.argtypes, f.restype = args, res
    return u


def _front_jarvis_window() -> int | None:
    """Handle of the Jarvis chat window if it is the front app window (the user is looking at Jarvis,
    e.g. they clicked its screen button), else None. Topmost bars (taskbar, voice bar) are skipped."""
    from ctypes import wintypes

    user32, dwm = _user32(), ctypes.windll.dwmapi
    h = user32.GetTopWindow(None)
    for _ in range(3000):
        if not h:
            return None
        if user32.IsWindowVisible(h) and not user32.IsIconic(h):
            ex = user32.GetWindowLongW(h, -20)                   # GWL_EXSTYLE
            cloaked = ctypes.c_int(0)
            dwm.DwmGetWindowAttribute(wintypes.HWND(h), 14, ctypes.byref(cloaked), 4)   # DWMWA_CLOAKED
            r = wintypes.RECT()
            user32.GetWindowRect(h, ctypes.byref(r))
            n = user32.GetWindowTextLengthW(h)
            if (not cloaked.value and not ex & 0x88 and n        # 0x88 = topmost | tool window
                    and (r.right - r.left) > 200 and (r.bottom - r.top) > 150):
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(h, buf, n + 1)
                return h if _is_jarvis_title(buf.value) else None
        h = user32.GetWindow(h, 2)                               # GW_HWNDNEXT (next lower in z-order)
    return None


def _bring_back(h: int) -> None:
    """Restore a window minimized by look_at_screen and give it the keyboard focus again."""
    user32, kernel32 = _user32(), ctypes.windll.kernel32
    user32.ShowWindow(h, 9)                                      # SW_RESTORE (back to maximized if it was)
    for _ in range(4):
        time.sleep(0.1)
        fg = user32.GetForegroundWindow()
        if fg == h:
            return
        mine = kernel32.GetCurrentThreadId()
        theirs = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        if theirs and theirs != mine:
            user32.AttachThreadInput(mine, theirs, True)
        user32.BringWindowToTop(h)
        user32.SetForegroundWindow(h)
        if theirs and theirs != mine:
            user32.AttachThreadInput(mine, theirs, False)


_MOVE_ONLY = 0x1 | 0x4 | 0x10 | 0x200   # SWP_NOSIZE | NOZORDER | NOACTIVATE | NOOWNERZORDER


def _window_pos(h: int) -> tuple[int, int]:
    from ctypes import wintypes

    r = wintypes.RECT()
    _user32().GetWindowRect(h, ctypes.byref(r))
    return r.left, r.top


def tool_look_at_screen(question: str = "") -> str:
    """Screenshot of the main screen -> NVIDIA vision model -> its answer.

    If the Jarvis chat window is in front (the user asked from it, e.g. with its screen button), it
    steps aside for a moment so the screenshot shows what is behind it: it is moved off-screen and
    back, which keeps it active with the cursor still in its text box (Windows won't let a background
    program re-activate a window, so minimizing is only the fallback)."""
    import io

    from PIL import ImageGrab

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)      # full-resolution capture, real pixel positions
    except Exception:  # noqa: BLE001
        pass
    jarvis, home, minimized = None, None, False
    try:
        try:
            jarvis = _front_jarvis_window()
        except Exception:  # noqa: BLE001
            jarvis = None
        if jarvis:
            u = _user32()
            home = _window_pos(jarvis)
            u.SetWindowPos(jarvis, None, -32000, -32000, 0, 0, _MOVE_ONLY)
            time.sleep(0.25)
            if _window_pos(jarvis)[0] > -30000:                  # didn't move: minimize instead
                u.ShowWindow(jarvis, 6)                           # SW_MINIMIZE
                minimized = True
                time.sleep(0.6)                                  # let the animation finish
        shot = ImageGrab.grab().convert("RGB")
    finally:
        if jarvis:
            try:
                if minimized:
                    _bring_back(jarvis)
                else:
                    _user32().SetWindowPos(jarvis, None, home[0], home[1], 0, 0, _MOVE_ONLY)
            except Exception:  # noqa: BLE001
                pass
    jpeg = b""
    for width, quality in ((1600, 70), (1400, 62), (1280, 55), (1100, 50)):   # inline limit ~180 KB
        im = shot.copy()
        im.thumbnail((width, width))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality)
        jpeg = buf.getvalue()
        if len(jpeg) < 175_000:
            break
    q = (question or "").strip() or "Describe what is on this screen."
    prompt = ("This is a screenshot of the user's Windows PC screen (the small dark bar at the bottom "
              "centre may be the Jarvis voice bar - ignore it). Answer the user's question about it "
              "precisely; read any text, errors, numbers or code exactly as shown. Question: " + q)
    try:
        answer = _ask_vision(jpeg, prompt)
    except Exception as exc:  # noqa: BLE001
        return f"Couldn't look at the screen right now ({exc.__class__.__name__})."
    return answer or "The vision model gave no answer; try asking again."


def _youtube_id(url: str) -> str | None:
    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([A-Za-z0-9_-]{11})", url or "")
    if m:
        return m.group(1)
    return url.strip() if re.fullmatch(r"[A-Za-z0-9_-]{11}", (url or "").strip()) else None


def tool_youtube_transcript(url: str) -> str:
    vid = _youtube_id(url)
    if not vid:
        return "That doesn't look like a YouTube link."
    from youtube_transcript_api import YouTubeTranscriptApi

    title = ""
    try:
        req = urllib.request.Request("https://www.youtube.com/oembed?format=json&url=https://www.youtube.com/watch?v="
                                     + vid, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            meta = json.loads(r.read().decode("utf-8"))
        title = f"{meta.get('title', '')} (by {meta.get('author_name', 'unknown')})"
    except Exception:  # noqa: BLE001
        pass
    api = YouTubeTranscriptApi()
    try:
        tl = api.list(vid)
        langs = ["en", "en-IN", "en-US", "en-GB"]
        try:
            tr = tl.find_transcript(langs)
        except Exception:  # noqa: BLE001 - no English: take any, translated to English if possible
            tr = next(iter(tl))
            if tr.is_translatable and not tr.language_code.startswith("en"):
                try:
                    tr = tr.translate("en")
                except Exception:  # noqa: BLE001
                    pass
        snippets = tr.fetch()
        text = " ".join(s.text.replace("\n", " ") for s in snippets)
        lang = getattr(tr, "language", "")
    except Exception as exc:  # noqa: BLE001
        return (f"{title or 'This video'}: no transcript available ({exc.__class__.__name__}). "
                "It may have no captions, or YouTube blocked the request.")
    text = re.sub(r"\s+", " ", text).strip()
    cut = len(text) > 24000
    return (f"YouTube video: {title or vid}\nTranscript ({lang}{', first part only' if cut else ''}):\n"
            + text[:24000])


def tool_read_webpage(url: str) -> str:
    url = (url or "").strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    if _youtube_id(url) and "youtu" in url:
        return tool_youtube_transcript(url)
    import trafilatura

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                                                 "AppleWebKit/537.36 Chrome/130 Safari/537.36"})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read(6_000_000)
            charset = r.headers.get_content_charset() or "utf-8"
        html_text = raw.decode(charset, "replace")
    except Exception as exc:  # noqa: BLE001
        return f"Couldn't open that page ({exc.__class__.__name__}: {exc})."
    text = trafilatura.extract(html_text, url=url, include_comments=False, include_tables=True,
                               favor_recall=True) or ""
    meta = trafilatura.extract_metadata(html_text, default_url=url)
    title = (meta.title if meta and meta.title else "") or url
    if not text.strip():
        return f"{title}: the page has no readable text (it may need a login or be mostly pictures/video)."
    cut = len(text) > 24000
    return f"Web page: {title}\nURL: {url}\n{'(first part only)' if cut else ''}\n{text[:24000]}"


def slug(text: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", text.lower())[:6]
    return "-".join(words) or "image"


# ---- tool ------------------------------------------------------------------------

def tool_create_image(prompt: str, shape: str = "square", open: bool = True) -> str:  # noqa: A002
    prompt = (prompt or "").strip()
    if not prompt:
        return "Please describe the picture to make."
    w, h = SHAPES.get((shape or "square").lower().strip(), SHAPES["square"])
    seed = random.randint(1, 2_000_000_000)    # a new variation every time ("make another one")
    problems = []
    for model, make_body, timeout in MODELS:
        try:
            t = time.time()
            img = generate(model, make_body(prompt[:2000], w, h, seed), timeout)
        except PermissionError as exc:
            return f"{exc}. Try describing it differently."
        except urllib.error.HTTPError as exc:
            problems.append(f"{model.split('/')[1]}: HTTP {exc.code}")
            continue
        except Exception as exc:  # noqa: BLE001 - timeouts, network, odd replies: try the next model
            problems.append(f"{model.split('/')[1]}: {exc.__class__.__name__} {exc}".strip())
            continue
        folder = desktop() / "Images"            # Desktop\Images (user's choice, 2026-11-07)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{slug(prompt)}-{time.strftime('%Y%m%d-%H%M%S')}.jpg"
        path.write_bytes(img)
        if open:
            os.startfile(str(path))
        # The user asked (2026-11-07) that replies not mention where pictures are
        # stored. The path stays in the result so Jarvis can still use the file
        # (e.g. in a website) or answer "where is it?".
        return ("Picture created" + (" and now open on screen." if open else " and saved.")
                + f" [Internal file path, for your use only: {path}] Do NOT mention the file "
                "name, folder, path or where it is saved in your reply, unless the user asks "
                "where it is or the path is needed for something they asked for.")
    return "Couldn't create the picture right now (" + "; ".join(problems) + "). Try again in a minute."


TOOLS = {
    "create_image": (
        tool_create_image,
        "Create (draw/generate) a new picture from a text description, e.g. a photo, painting, "
        "logo, poster or illustration. Saves it as a JPG and opens it on screen (don't tell "
        "the user where it is saved unless they ask). Each call makes a fresh variation. It cannot edit existing pictures. Takes "
        "about 10 seconds. QUALITY: never pass the user's short request as-is; expand it into one "
        "rich English scene description (40-120 words) covering: main subject and what it is "
        "doing; setting and background; composition/framing (close-up, wide shot, centred, "
        "eye level...); lighting and time of day; medium and style (photo with camera/lens, "
        "watercolour, 3D render, flat vector logo...); colour palette; mood; fine details. "
        "Any words that must appear in the picture go in double quotes and should be short. "
        "Keep everything the user asked for; don't add text or people they didn't ask for.",
        {"prompt": {"type": "string", "description": "The expanded, detailed scene description."},
         "shape": {"type": "string", "enum": ["square", "wide", "tall"],
                   "description": "square (default), wide (landscape/banner/wallpaper) or tall (phone/poster)."},
         "open": {"type": "boolean", "description": "Open it on screen afterwards (default true)."}},
        ["prompt"],
    ),
    "edit_photo": (
        tool_edit_photo,
        "Edit a picture that is on the user's Desktop (find its full path first, e.g. with "
        "search_files): remove_background (cut out the subject, transparent PNG), "
        "black_and_white, sepia, pencil_sketch, cartoon, blur, sharpen, brighter, darker, "
        "more_contrast, less_contrast, more_colour, less_colour (amount 0-100, default 30), "
        "rotate_left, rotate_right, flip, crop_square, resize (width in pixels). Runs on this PC "
        "(private). Saves a new copy and opens it; the original is never changed.",
        {"path": {"type": "string", "description": "Full path of the picture (Desktop only)."},
         "action": {"type": "string", "enum": EDITS},
         "amount": {"type": "number", "description": "Strength 0-100 for adjustments/blur/sharpen."},
         "width": {"type": "integer", "description": "New width in pixels, for resize."},
         "open": {"type": "boolean", "description": "Open the result on screen (default true)."}},
        ["path", "action"],
    ),
    "restyle_photo": (
        tool_restyle_photo,
        "Re-create a picture from the user's Desktop in a new art style (e.g. 'oil painting', "
        "'anime', 'Pixar-style 3D', 'watercolour', 'comic book', 'Van Gogh'). Jarvis looks at "
        "the photo and paints a NEW picture of the same scene, so people's faces and small "
        "details won't match exactly (tell the user this). Takes ~15 s. Saves a new copy and "
        "opens it; the original is never changed. For exact edits use edit_photo instead.",
        {"path": {"type": "string", "description": "Full path of the picture (Desktop only)."},
         "style": {"type": "string", "description": "The art style, in a few words."},
         "open": {"type": "boolean", "description": "Open the result on screen (default true)."}},
        ["path", "style"],
    ),
    "look_at_screen": (
        tool_look_at_screen,
        "Look at the user's screen right now (takes a screenshot and asks a vision AI about it). "
        "Use for 'what's on my screen', 'explain this error', 'summarise what I'm reading', "
        "'what does this say', and any message starting 'Look at my screen' (sent by the chat's screen "
        "button). You cannot see the screen any other way, so ALWAYS call this tool for such requests and "
        "never guess what is on the screen. If the Jarvis chat window is in front, it hides for a moment "
        "so the screenshot shows what is behind it. Pass the user's question. Only when the user asks "
        "about their screen.",
        {"question": {"type": "string", "description": "What the user wants to know about the screen."}},
        [],
    ),
    "read_webpage": (
        tool_read_webpage,
        "Read the main text of a web page (article, blog, docs, news) from its link, so you can "
        "summarise it or answer questions about it. YouTube links return the video's transcript.",
        {"url": {"type": "string"}},
        ["url"],
    ),
    "youtube_transcript": (
        tool_youtube_transcript,
        "Get the title and spoken transcript of a YouTube video from its link, to summarise it or "
        "answer questions about it. Works when the video has captions.",
        {"url": {"type": "string"}},
        ["url"],
    ),
}


def handle(msg: dict) -> dict | None:
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:  # notification
        return None
    if method == "initialize":
        result = {"protocolVersion": msg.get("params", {}).get("protocolVersion", PROTOCOL),
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "jarvis-image-tools", "version": "1.0"}}
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
