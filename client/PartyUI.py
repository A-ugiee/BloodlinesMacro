import asyncio
import ctypes
import hashlib
import io
import json
import os
import platform
import re
import ssl
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
from pathlib import Path

import mss
import numpy as np
from PIL import Image, ImageDraw, ImageSequence, ImageTk
from websockets.asyncio.client import connect

try:
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter
except Exception:
    OcrEngine = None

VERSION = "dev"
REPO = "A-ugiee/BloodlinesMacro"
ASSET = "BloodlinesParty.exe"
DEFAULT_SERVER = "wss://bloodlinesmacro-production.up.railway.app"


def ssl_context():
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        ctx = ssl.create_default_context()
        try:
            import certifi
            ctx.load_verify_locations(certifi.where())
        except Exception:
            pass
        return ctx


SSL = ssl_context()
CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / "BloodlinesParty"
CONFIG = CONFIG_DIR / "config.json"
MSS = getattr(mss, "MSS", None) or mss.mss
WIN = sys.platform == "win32"
TOL = 60
HOTBAR_TOL = 40
HOTBAR_MATCH = 0.6
MODE_TOL = 50
MISSING_READS = 10
TEXT_TOL = 60
TEXT_GRID = (60, 16)
TEXT_MATCH = 0.5
GRID = (24, 8)
MAX_AVATAR = 8 * 1024 * 1024
DIGITS = str.maketrans("OoDQlI|SsBZzg", "0000111558229")

FONT = "Segoe UI"
BG = "#111214"
PANEL = "#111214"
CARD = "#18191c"
FIELD = "#202226"
LINE = "#2a2c31"
TEXT = "#ececee"
MUTED = "#8b8d94"
ACCENT = "#d12f55"
ACCENT_HI = "#e64a6e"
OK = "#3ecf72"
WARN = "#f0b429"
BAD = "#ef4f5f"
TRACK = "#26282d"
AVATAR = "#2a2c31"
HP = ("#e5484d", "#6b3134")
CHAKRA = ("#3e8ef7", "#2c4466")
MODE = "#a855f7"
KEY = "#010203"

AREAS = (
    ("health", "Health bar", "Drag a box over your FULL health bar"),
    ("chakra", "Chakra bar", "Drag a box over your FULL chakra bar"),
    ("mode", "Mode icon", "Drag a box around your mode icon WHILE it is ready (purple glow)"),
    ("inventory", "Inventory", "Open your inventory, then drag a box over a part of it that never changes"),
    ("combat", "In combat", "While IN COMBAT is showing, drag a box tightly around those words"),
)
TIP = "Set bars at full health and chakra, and the mode icon while it glows purple. For inventory, pick a part that never changes (a border or header). When neither bar is on screen you show as Menu / Chakra point. Right-click a button to clear that area."


def load():
    try:
        return json.loads(CONFIG.read_text())
    except Exception:
        return {}


def save(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2))


def grab_rgb(sct, region):
    shot = sct.grab(region)
    return Image.frombytes("RGB", shot.size, shot.rgb)


def bar_config(img, mon, box):
    x0, y0, x1, y1 = box
    y = (y0 + y1) // 2
    row = np.asarray(img.crop((x0, y, x1, y + 1)))[0].astype(int)
    fill = np.median(row, axis=0)
    hits = np.nonzero(np.linalg.norm(row - fill, axis=1) < TOL)[0]
    if hits.size:
        x0, x1 = x0 + hits[0], x0 + hits[-1] + 1
    region = {"left": int(mon["left"] + x0), "top": int(mon["top"] + y), "width": int(x1 - x0), "height": 1}
    full = np.asarray(img.crop((x0, y, x1, y + 1)))[0].astype(int)
    frac = (np.linalg.norm(full - fill, axis=1) < TOL).mean()
    return {"region": region, "fill": fill.astype(int).tolist(), "full": max(float(frac), 0.5)}


def box_region(mon, box):
    x0, y0, x1, y1 = box
    return {"left": mon["left"] + x0, "top": mon["top"] + y0, "width": x1 - x0, "height": y1 - y0}


def snapshot_config(img, mon, box):
    ref = np.asarray(img.crop(box).resize(GRID, Image.Resampling.BOX)).astype(int)
    return {"region": box_region(mon, box), "ref": ref.tolist()}


def mode_config(img, mon, box):
    a = np.asarray(img.crop(box)).astype(int)
    vivid = a[(a.max(axis=2) - a.min(axis=2)) > 60]
    ref = np.median(vivid, axis=0) if vivid.size else np.array([120, 48, 129])
    frac = (np.linalg.norm(a - ref, axis=2) < MODE_TOL).mean()
    return {"region": box_region(mon, box), "ref": ref.astype(int).tolist(), "frac": max(float(frac), 0.02)}


def text_mask(a, color):
    hit = Image.fromarray((np.linalg.norm(a - np.array(color), axis=2) < TEXT_TOL).astype(np.uint8) * 255)
    return np.asarray(hit.resize(TEXT_GRID, Image.Resampling.BOX)) > 76


def text_config(img, mon, box):
    a = np.asarray(img.crop(box)).astype(int)
    vivid = a[(a.max(axis=2) - np.median(a, axis=2)) > 60]
    color = np.median(vivid, axis=0) if vivid.size else np.array([254, 96, 91])
    return {"region": box_region(mon, box), "color": color.astype(int).tolist(), "mask": text_mask(a, color).tolist()}


def text_visible(sct, text):
    now = text_mask(np.asarray(grab_rgb(sct, text["region"])).astype(int), text["color"])
    ref = np.array(text["mask"])
    union = (now | ref).sum()
    return bool(union) and (now & ref).sum() / union >= TEXT_MATCH


CONFIGS = {
    "health": bar_config, "chakra": bar_config,
    "mode": mode_config, "inventory": snapshot_config, "combat": text_config,
}


def bar_pct(sct, bar):
    row = np.asarray(grab_rgb(sct, bar["region"]))[0].astype(int)
    frac = (np.linalg.norm(row - np.array(bar["fill"]), axis=1) < TOL).mean()
    return min(100, round(frac / bar.get("full", 1) * 100))


def matches(sct, snap):
    img = np.asarray(grab_rgb(sct, snap["region"]).resize(GRID, Image.Resampling.BOX)).astype(int)
    return (np.linalg.norm(img - np.array(snap["ref"]), axis=2) < HOTBAR_TOL).mean() >= HOTBAR_MATCH


def clean(mask):
    core = mask[:-1, :-1] & mask[1:, :-1] & mask[:-1, 1:] & mask[1:, 1:]
    out = np.zeros_like(mask)
    out[:-1, :-1] |= core
    out[1:, :-1] |= core
    out[:-1, 1:] |= core
    out[1:, 1:] |= core
    return out


def mode_state(sct, mode):
    a = np.asarray(grab_rgb(sct, mode["region"])).astype(int)
    purple = (np.linalg.norm(a - np.array(mode["ref"]), axis=2) < MODE_TOL).mean() >= 0.4 * mode["frac"]
    mask = clean((a[:, :, 1] > 140) & (a.min(axis=2) > 120))
    number = mask.sum() >= 20
    if purple:
        return ("active" if number else "ready"), mask
    return ("cooldown" if number else None), mask


def ocr_engine():
    if OcrEngine is None:
        return None
    try:
        engine = OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            from winrt.windows.globalization import Language
            engine = OcrEngine.try_create_from_language(Language("en-US"))
        return engine
    except Exception:
        return None


async def ocr_number(engine, mask):
    ys, xs = np.nonzero(mask)
    crop = Image.fromarray(np.where(mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1], 0, 255).astype(np.uint8))
    crop = crop.resize((crop.width * 4, crop.height * 4), Image.Resampling.BILINEAR)
    page = Image.new("L", (crop.width + 40, crop.height + 40), 255)
    page.paste(crop, (20, 20))
    writer = DataWriter()
    writer.write_bytes(page.convert("RGBA").tobytes())
    bitmap = SoftwareBitmap.create_copy_from_buffer(writer.detach_buffer(), BitmapPixelFormat.BGRA8, page.width, page.height)
    result = await engine.recognize_async(bitmap)
    digits = "".join(ch for ch in result.text.translate(DIGITS) if ch.isdigit())
    return int(digits) if 0 < len(digits) <= 3 else None


def smooth(hist, key, value):
    h = hist.setdefault(key, [])
    h.append(value)
    del h[:-3]
    return sorted(h)[len(h) // 2]


def roblox_focused():
    if not WIN:
        return None
    try:
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        hwnd = user32.GetForegroundWindow()
        buf = ctypes.create_unicode_buffer(260)
        user32.GetWindowTextW(hwnd, buf, 260)
        if buf.value == "Roblox":
            return True
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = kernel32.OpenProcess(0x1000, False, pid.value)
        if not handle:
            return False
        try:
            size = ctypes.c_ulong(260)
            kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
        finally:
            kernel32.CloseHandle(handle)
        return buf.value.lower().endswith("robloxplayerbeta.exe")
    except Exception:
        return None


def live_read(sct, cfg, hist, bars):
    out = {"mode": None, "mode_t": None}
    out["combat"] = bool(smooth(hist, "combat", text_visible(sct, cfg["combat"]))) if "combat" in cfg else False
    for key in ("hp", "chakra"):
        out[key] = smooth(hist, key, bars[key]) if key in bars else None
    if "mode" in cfg:
        raw, hist["mode_mask"] = mode_state(sct, cfg["mode"])
        mode = smooth(hist, "mode", raw or "")
        if mode != hist.get("mode_prev"):
            hist["mode_prev"] = mode
            hist.pop("mode_t", None)
            hist.pop("mode_raw", None)
        out["mode"] = mode or None
        if mode in ("active", "cooldown"):
            out["mode_t"] = hist.get("mode_t")
    return out


def frozen(last):
    if not last:
        return {"hp": None, "chakra": None, "mode": None, "mode_t": None, "combat": False}
    mode, t = last["mode"], last["mode_t"]
    if t is not None:
        t -= int(time.time() - last["at"])
        if t <= 0:
            mode, t = ("ready" if mode == "cooldown" else None), None
    return {"hp": last["hp"], "chakra": last["chakra"], "mode": mode, "mode_t": t, "combat": last.get("combat", False)}


def read(sct, cfg, hist):
    focus = roblox_focused() is not False
    bars = {}
    if focus:
        bars = {key: bar_pct(sct, cfg[area]) for key, area in (("hp", "health"), ("chakra", "chakra")) if area in cfg}
        hist["missing"] = hist.get("missing", 0) + 1 if bars and not any(bars.values()) else 0
        game = hist["missing"] < MISSING_READS
        inv = bool(game and "inventory" in cfg and smooth(hist, "inv", matches(sct, cfg["inventory"])))
        hist["was_game"] = game
    else:
        game, inv = hist.get("was_game", True), False
    live = game and focus and not inv
    if live and hist.get("missing"):
        out = frozen(hist.get("last"))
    elif live:
        out = live_read(sct, cfg, hist, bars)
    elif game:
        out = frozen(hist.get("last"))
    else:
        for key in ("hp", "chakra", "mode", "mode_prev", "mode_t", "mode_raw", "combat"):
            hist.pop(key, None)
        out = {"hp": None, "chakra": None, "mode": None, "mode_t": None, "combat": False}
    out.update(status="game" if game else "menu", inv=inv, focus=focus, frozen=game and not live)
    return out


async def read_timer(engine, hist):
    v = await ocr_number(engine, hist["mode_mask"])
    prev, hist["mode_raw"] = hist.get("mode_raw"), v
    last = hist.get("mode_t")
    if v is not None and (v == prev or (last is not None and 0 <= last - v <= 2)):
        hist["mode_t"] = v


async def reader(state):
    hist = {}
    engine = ocr_engine()
    state["ocr"] = engine is not None
    tick = 0
    with MSS() as sct:
        while True:
            try:
                me = read(sct, state["cfg"], hist)
                if engine and tick % 5 == 0 and not me["frozen"] and me["mode"] in ("active", "cooldown"):
                    try:
                        await read_timer(engine, hist)
                        me["mode_t"] = hist.get("mode_t")
                    except Exception:
                        engine = None
                        state["ocr"] = False
                if not me["frozen"] and not hist.get("missing") and me["status"] == "game":
                    hist["last"] = {k: me[k] for k in ("hp", "chakra", "mode", "mode_t", "combat")} | {"at": time.time()}
                state["me"] = me
            except Exception:
                state["me"] = None
            tick += 1
            await asyncio.sleep(0.1)


async def receive(ws, state):
    async for raw in ws:
        m = json.loads(raw)
        if m.get("t") == "state":
            state["members"] = m["members"]


def system_info(cfg, state):
    info = {"version": VERSION, "areas": [k for k, _, _ in AREAS if k in cfg], "ocr": bool(state.get("ocr"))}
    try:
        info["os"] = f"{platform.system()} {platform.release()} ({platform.version()})"
    except Exception:
        pass
    try:
        with MSS() as sct:
            m = sct.monitors[1]
            info["screen"] = f"{m['width']}x{m['height']}"
    except Exception:
        pass
    if WIN:
        try:
            info["scale"] = round(ctypes.windll.user32.GetDpiForSystem() * 100 / 96)
        except Exception:
            pass
    return info


async def sender(state):
    while True:
        cfg, gen = state["cfg"], state["gen"]
        if not cfg.get("name") or not cfg.get("party"):
            state["status"] = "Enter your name and party key, then Save & Connect"
            await asyncio.sleep(0.5)
            continue
        state["status"] = "Connecting..."
        try:
            url = cfg.get("server") or DEFAULT_SERVER
            async with connect(url, open_timeout=10, ssl=SSL if url.startswith("wss://") else None) as ws:
                await ws.send(json.dumps({
                    "t": "join", "party": cfg["party"], "name": cfg["name"], "avatar": cfg.get("avatar", ""),
                    "sys": system_info(cfg, state),
                }))
                state["status"] = f"Connected to party '{cfg['party']}'"
                recv = asyncio.create_task(receive(ws, state))
                last = None
                while not recv.done() and state["gen"] == gen:
                    me = state["me"]
                    if me and me != last:
                        await ws.send(json.dumps({"t": "update", **me}))
                        last = me
                    await asyncio.sleep(0.1)
                recv.cancel()
            state["members"] = {}
            if state["gen"] != gen:
                continue
            state["status"] = "Disconnected, retrying..."
        except Exception as e:
            state["status"] = f"Can't reach server ({type(e).__name__}), retrying..."
        state["members"] = {}
        await asyncio.sleep(2)


async def run(state):
    await asyncio.gather(reader(state), sender(state))


def version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def latest_release():
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "BloodlinesParty"},
    )
    with urllib.request.urlopen(req, timeout=8, context=SSL) as r:
        rel = json.load(r)
    asset = next((a for a in rel.get("assets", []) if a["name"] == ASSET), None)
    if asset and version_tuple(rel["tag_name"]) > version_tuple(VERSION):
        return rel["tag_name"], asset
    return None


def download_update(asset, progress):
    exe = Path(sys.executable)
    new, old = exe.with_name(exe.stem + ".new.exe"), exe.with_name(exe.stem + ".old.exe")
    req = urllib.request.Request(asset["browser_download_url"], headers={"User-Agent": "BloodlinesParty"})
    digest, done, total = hashlib.sha256(), 0, asset.get("size") or 0
    with urllib.request.urlopen(req, timeout=30, context=SSL) as r, open(new, "wb") as f:
        while chunk := r.read(1 << 16):
            f.write(chunk)
            digest.update(chunk)
            done += len(chunk)
            progress(done, total)
    expected = asset.get("digest") or ""
    if (total and done != total) or (expected.startswith("sha256:") and expected[7:] != digest.hexdigest()):
        new.unlink(missing_ok=True)
        raise ValueError("download check failed")
    old.unlink(missing_ok=True)
    exe.rename(old)
    try:
        new.rename(exe)
    except OSError:
        old.rename(exe)
        raise
    subprocess.Popen(
        [str(exe)], close_fds=True, creationflags=0x00000008 | 0x00000200,
        env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"},
    )


def updater(state):
    if not getattr(sys, "frozen", False) or VERSION == "dev":
        return
    old = Path(sys.executable).with_name(Path(sys.executable).stem + ".old.exe")
    for _ in range(10):
        try:
            old.unlink(missing_ok=True)
            break
        except OSError:
            time.sleep(1)
    try:
        found = latest_release()
    except Exception:
        return
    if not found:
        return
    tag, asset = found
    try:
        state["update"] = f"Updating to {tag}..."
        download_update(asset, lambda d, t: state.update(update=f"Updating to {tag}  {d * 100 // t if t else 0}%"))
        state["update"] = f"Restarting into {tag}..."
        state["restart"] = True
    except Exception as e:
        state["update"] = f"Update to {tag} failed ({type(e).__name__})"


def hwnd_of(win):
    win.update_idletasks()
    return ctypes.windll.user32.GetParent(win.winfo_id()) or win.winfo_id()


def set_capturable(win, visible):
    if WIN:
        try:
            ctypes.windll.user32.SetWindowDisplayAffinity(hwnd_of(win), 0x00 if visible else 0x11)
        except Exception:
            pass


def dark_titlebar(win):
    if WIN:
        try:
            on = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd_of(win), 20, ctypes.byref(on), ctypes.sizeof(on))
        except Exception:
            pass


def fs(size, s):
    return max(6, round(size * s))


def rounded(c, x0, y0, x1, y1, r, **kw):
    r = max(0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    pts = (
        x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
        x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0,
    )
    return c.create_polygon(pts, smooth=True, **kw)


def circle_mask(size):
    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=255)
    return mask.resize((size, size), Image.Resampling.LANCZOS)


def square(img, size):
    w, h = img.size
    side = min(w, h)
    left, top = (w - side) // 2, (h - side) // 2
    return img.crop((left, top, left + side, top + side)).resize((size, size), Image.Resampling.LANCZOS)


def fetch_frames(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=8, context=SSL) as r:
        data = r.read(MAX_AVATAR + 1)
    if len(data) > MAX_AVATAR:
        raise ValueError("image too large")
    frames = []
    with Image.open(io.BytesIO(data)) as im:
        for i, frame in enumerate(ImageSequence.Iterator(im)):
            if i >= 120:
                break
            frames.append((square(frame.convert("RGBA"), 96), max(20, int(frame.info.get("duration") or 100))))
    if not frames:
        raise ValueError("no frames")
    return frames


class Avatars:
    def __init__(self):
        self.items = {}
        self.lock = threading.Lock()

    def load(self, url):
        try:
            frames = fetch_frames(url)
        except Exception:
            frames = None
        with self.lock:
            self.items[url] = {"frames": frames, "photos": {}, "retry": time.time() + 30}

    def get(self, url, size):
        with self.lock:
            item = self.items.get(url)
            if item is None or (item["frames"] is None and time.time() > item["retry"]):
                self.items[url] = {"frames": False, "photos": {}, "retry": 0}
                threading.Thread(target=self.load, args=(url,), daemon=True).start()
                return None
        if not item["frames"]:
            return None
        photos = item["photos"].get(size)
        if photos is None:
            mask = circle_mask(size)
            photos = []
            for frame, _ in item["frames"]:
                img = frame.resize((size, size), Image.Resampling.LANCZOS)
                img.putalpha(Image.fromarray(np.minimum(np.asarray(img.getchannel("A")), np.asarray(mask))))
                photos.append(ImageTk.PhotoImage(img))
            if len(item["photos"]) >= 4:
                item["photos"].pop(next(iter(item["photos"])))
            item["photos"][size] = photos
        durations = [d for _, d in item["frames"]]
        t = int(time.time() * 1000) % sum(durations)
        for photo, d in zip(photos, durations):
            if t < d:
                return photo
            t -= d
        return photos[-1]


def flat_bar(c, x0, y, x1, h, pct, color):
    rounded(c, x0, y, x1, y + h, h / 2, fill=TRACK, outline="")
    if pct:
        rounded(c, x0, y, x0 + max(h, (x1 - x0) * pct / 100), y + h, h / 2, fill=color, outline="")


def mode_dot(c, cx, cy, r, m, s):
    mode, t = m.get("mode"), m.get("mode_t")
    if mode is None:
        return
    if mode == "ready":
        c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=MODE, outline="")
        return
    color = MODE if mode == "active" else MUTED
    c.create_oval(cx - r, cy - r, cx + r, cy + r, fill="", outline=color, width=max(1, 1.5 * s))
    if t is not None:
        c.create_text(cx, cy, text=str(t), fill=TEXT if mode == "active" else MUTED, font=(FONT, fs(7 if t < 100 else 6, s), "bold"))


def draw_frames(c, members, me, width, s, y, avatars, pics):
    pad, size, r = 10 * s, 28 * s, 10 * s
    h = 36 * s
    for name, m in members:
        ax, ay = pad, y + (h - size) / 2
        photo = avatars.get(m["avatar"], int(size)) if pics and m.get("avatar") else None
        if photo:
            c.create_image(ax, ay, image=photo, anchor="nw")
        else:
            c.create_oval(ax, ay, ax + size, ay + size, fill=AVATAR, outline="")
            c.create_text(ax + size / 2, ay + size / 2, text=(name[:1] or "?").upper(), fill=TEXT, font=(FONT, fs(11, s), "bold"))
        x0 = ax + size + 9 * s
        cx = width - pad - r
        x1 = cx - r - 8 * s
        hp, chakra, stale = m.get("hp"), m.get("chakra"), m.get("frozen")
        label = c.create_text(x0, y + 8 * s, text=name, anchor="w", fill=TEXT if name != me else ACCENT_HI, font=(FONT, fs(9, s), "bold"))
        menu = m.get("status") == "menu"
        tag = next((t for t, on in (
            ("Inventory", m.get("inv")),
            ("Tabbed out", m.get("focus") is False),
        ) if on), None)
        x = c.bbox(label)[2] + 6 * s
        if m.get("combat"):
            x = c.bbox(c.create_text(x, y + 8 * s, text="In combat", anchor="w", fill=BAD, font=(FONT, fs(8, s), "bold")))[2] + 6 * s
        if tag:
            c.create_text(x, y + 8 * s, text=tag, anchor="w", fill=MUTED, font=(FONT, fs(8, s)))
        if menu:
            c.create_text(x0, y + 24 * s, text="Menu / Chakra point", anchor="w", fill=MUTED, font=(FONT, fs(8, s), "italic"))
        else:
            if hp is not None:
                c.create_text(x1, y + 8 * s, text=f"{hp}%", anchor="e", fill=MUTED, font=(FONT, fs(8, s)))
            flat_bar(c, x0, y + 17 * s, x1, 6 * s, hp, HP[1] if stale else HP[0])
            flat_bar(c, x0, y + 26 * s, x1, 4 * s, chakra, CHAKRA[1] if stale else CHAKRA[0])
            mode_dot(c, cx, y + h / 2 + 3 * s, r, m, s)
        y += h + 6 * s
    return y


def draw_overlay(c, state, width, avatars, pics):
    s = width / 250
    me = state["cfg"].get("name")
    members = sorted(state["members"].items(), key=lambda kv: (kv[0] != me, kv[0].lower()))
    c.delete("all")
    if members:
        h = draw_frames(c, members, me, width, s, 8 * s, avatars, pics) + 2 * s
        rounded(c, 0, 0, width, h, 12 * s, fill=PANEL, outline="", tags="panel")
        c.tag_lower("panel")
    else:
        h = 32 * s
        rounded(c, 0, 0, width, h, 12 * s, fill=PANEL, outline="")
        c.create_text(width / 2, h / 2, text=state["status"], fill=MUTED, font=(FONT, fs(8, s)), width=width - 24 * s, justify="center")
    c.config(width=int(width), height=int(h) + 1)


def pick_region(root, prompt):
    with MSS() as sct:
        mon = sct.monitors[1]
        img = grab_rgb(sct, mon)
    win = tk.Toplevel(root)
    win.overrideredirect(True)
    win.geometry(f"{mon['width']}x{mon['height']}+{mon['left']}+{mon['top']}")
    win.attributes("-topmost", True)
    photo = ImageTk.PhotoImage(img)
    canvas = tk.Canvas(win, width=mon["width"], height=mon["height"], highlightthickness=0, cursor="crosshair")
    canvas.pack()
    canvas.create_image(0, 0, image=photo, anchor="nw")
    cx = mon["width"] // 2
    rounded(canvas, cx - 420, 24, cx + 420, 92, 14, fill=CARD, outline=ACCENT, width=2)
    canvas.create_text(cx, 48, text=prompt, fill=TEXT, font=(FONT, 16, "bold"))
    canvas.create_text(cx, 74, text="Click and drag  ·  right-click or Esc to cancel", fill=MUTED, font=(FONT, 10))
    box = {}

    def press(e):
        box.clear()
        box.update(x0=e.x, y0=e.y, rect=canvas.create_rectangle(e.x, e.y, e.x, e.y, outline=ACCENT_HI, width=2))

    def move(e):
        if "rect" in box:
            canvas.coords(box["rect"], box["x0"], box["y0"], e.x, e.y)

    def release(e):
        if "rect" in box:
            box.update(x1=e.x, y1=e.y)
            win.destroy()

    canvas.bind("<ButtonPress-1>", press)
    canvas.bind("<B1-Motion>", move)
    canvas.bind("<ButtonRelease-1>", release)
    canvas.bind("<Button-3>", lambda e: win.destroy())
    win.bind("<Escape>", lambda e: win.destroy())
    win.focus_force()
    root.wait_window(win)

    if "x1" not in box:
        return None
    x0, x1 = sorted((max(box["x0"], 0), min(box["x1"], mon["width"])))
    y0, y1 = sorted((max(box["y0"], 0), min(box["y1"], mon["height"])))
    if x1 - x0 < 6:
        return None
    return img, mon, (x0, y0, x1, max(y1, y0 + 1))


def button(parent, text, command, primary=False, small=False):
    bg, hi = (ACCENT, ACCENT_HI) if primary else (FIELD, LINE)
    b = tk.Label(
        parent, text=text, bg=bg, fg=TEXT, cursor="hand2",
        font=(FONT, 9 if small else 10, "bold"), padx=14, pady=4 if small else 8,
    )
    b.bind("<Enter>", lambda e: b.config(bg=hi))
    b.bind("<Leave>", lambda e: b.config(bg=bg))
    b.bind("<ButtonRelease-1>", lambda e: 0 <= e.x < b.winfo_width() and 0 <= e.y < b.winfo_height() and command())
    return b


def entry(parent, var):
    return tk.Entry(
        parent, textvariable=var, bg=FIELD, fg=TEXT, insertbackground=TEXT, relief="flat",
        font=(FONT, 10), highlightthickness=1, highlightbackground=LINE, highlightcolor=ACCENT,
    )


def card(parent, title):
    frame = tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=LINE)
    tk.Label(frame, text=title.upper(), bg=CARD, fg=MUTED, font=(FONT, 8, "bold")).grid(
        row=0, column=0, columnspan=3, sticky="w", padx=14, pady=(12, 6)
    )
    frame.columnconfigure(1, weight=1)
    return frame


def switch(parent, var, command):
    c = tk.Canvas(parent, width=36, height=20, bg=BG, highlightthickness=0, cursor="hand2")

    def paint():
        c.delete("all")
        on = var.get()
        rounded(c, 1, 1, 35, 19, 9, fill=ACCENT if on else LINE, outline="")
        x = 26 if on else 10
        c.create_oval(x - 7, 3, x + 7, 17, fill=TEXT, outline="")

    def flip(e):
        var.set(not var.get())
        paint()
        command()

    c.bind("<Button-1>", flip)
    c.paint = paint
    paint()
    return c


def main():
    if WIN:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass

    cfg = load()
    cfg["server"] = cfg.get("server") or DEFAULT_SERVER
    state = {"cfg": dict(cfg), "gen": 0, "members": {}, "status": "Starting...", "me": False}
    threading.Thread(target=lambda: asyncio.run(run(state)), daemon=True).start()
    threading.Thread(target=updater, args=(state,), daemon=True).start()

    root = tk.Tk()
    root.withdraw()
    root.title("Bloodlines Party")
    root.configure(bg=BG)
    root.minsize(380, 640)
    root.geometry(cfg.get("window", "440x780"))

    frm = tk.Frame(root, bg=BG)
    frm.pack(fill="both", expand=True, padx=16, pady=14)
    frm.columnconfigure(0, weight=1)

    tk.Label(frm, text="Bloodlines Party", bg=BG, fg=TEXT, font=(FONT, 16, "bold")).grid(row=0, column=0, sticky="w")
    update_var = tk.StringVar()
    update_label = tk.Label(frm, textvariable=update_var, bg=BG, fg=MUTED, font=(FONT, 9))
    update_label.grid(row=0, column=0, sticky="e")
    status_row = tk.Frame(frm, bg=BG)
    status_row.grid(row=1, column=0, sticky="we", pady=(2, 12))
    dot = tk.Canvas(status_row, width=10, height=10, bg=BG, highlightthickness=0)
    dot.pack(side="left", padx=(1, 7))
    dot_id = dot.create_oval(1, 1, 9, 9, fill=MUTED, outline="")
    status_var = tk.StringVar()
    tk.Label(status_row, textvariable=status_var, bg=BG, fg=MUTED, font=(FONT, 9), anchor="w").pack(side="left", fill="x", expand=True)

    conn = card(frm, "Connection")
    conn.grid(row=2, column=0, sticky="we")
    fields = {}
    for i, (key, label) in enumerate((("name", "Name"), ("party", "Party key"), ("avatar", "Picture link"), ("server", "Server")), start=1):
        tk.Label(conn, text=label, bg=CARD, fg=MUTED, font=(FONT, 9)).grid(row=i, column=0, sticky="w", padx=(14, 10), pady=4)
        var = tk.StringVar(value=cfg.get(key, ""))
        entry(conn, var).grid(row=i, column=1, columnspan=2, sticky="we", padx=(0, 14), pady=4, ipady=5)
        fields[key] = var

    def apply():
        for key, var in fields.items():
            cfg[key] = var.get().strip()
        cfg["server"] = cfg["server"] or DEFAULT_SERVER
        fields["server"].set(cfg["server"])
        save(cfg)
        state["cfg"] = dict(cfg)
        state["gen"] += 1

    tk.Label(
        conn, text="Picture link: a direct .png / .jpg / .gif link (GIFs animate). Leave empty to show your initial.",
        bg=CARD, fg=MUTED, font=(FONT, 8), justify="left", anchor="w", wraplength=340,
    ).grid(row=5, column=0, columnspan=3, sticky="we", padx=14, pady=(2, 0))
    button(conn, "Save & Connect", apply, primary=True).grid(row=6, column=0, columnspan=3, sticky="we", padx=14, pady=(10, 14))
    root.bind("<Return>", lambda e: apply())

    areas = card(frm, "Screen areas")
    areas.grid(row=3, column=0, sticky="we", pady=(12, 0))
    chips, buttons = {}, {}
    for i, (key, label, prompt) in enumerate(AREAS, start=1):
        tk.Label(areas, text=label, bg=CARD, fg=TEXT, font=(FONT, 10)).grid(row=i, column=0, sticky="w", padx=(14, 10), pady=5)
        chips[key] = tk.Label(areas, bg=CARD, font=(FONT, 9))
        chips[key].grid(row=i, column=1, sticky="w")
        buttons[key] = button(areas, "Set", lambda k=key, p=prompt: calibrate(k, p), small=True)
        buttons[key].grid(row=i, column=2, sticky="e", padx=14, pady=5)
        buttons[key].bind("<Button-3>", lambda e, k=key: clear_area(k))
    hint = tk.Label(areas, text=TIP, bg=CARD, fg=MUTED, font=(FONT, 8), justify="left", anchor="w")
    hint.grid(row=len(AREAS) + 1, column=0, columnspan=3, sticky="we", padx=14, pady=(4, 14))
    hint.bind("<Configure>", lambda e: hint.config(wraplength=max(e.width - 4, 100)))

    def clear_area(key):
        if cfg.pop(key, None) is not None:
            save(cfg)
            state["cfg"] = dict(cfg)
            refresh_areas()

    def refresh_areas():
        for key in chips:
            done = key in cfg
            chips[key].config(text="●  Set" if done else "●  Not set", fg=OK if done else MUTED)
            buttons[key].config(text="Redo" if done else "Set")

    you = card(frm, "You")
    you.grid(row=4, column=0, sticky="we", pady=(12, 0))
    pc = tk.Canvas(you, bg=CARD, height=40, highlightthickness=0)
    pc.grid(row=1, column=0, columnspan=3, sticky="we", padx=4, pady=(0, 10))

    def draw_preview():
        pc.delete("all")
        w = max(pc.winfo_width(), 200)
        me = state["me"]
        if me is False:
            pc.create_text(12, 14, text="Reading your screen...", anchor="w", fill=MUTED, font=(FONT, 9))
            h = 28
        elif me is None:
            pc.create_text(12, 14, text="Can't read your screen areas. Try setting them again.", anchor="w", fill=BAD, font=(FONT, 9))
            h = 28
        else:
            h = draw_frames(pc, [(cfg.get("name") or "You", {**me, "avatar": cfg.get("avatar")})], None, w - 8, 1, 2, avatars, pics.get())
            if me["status"] == "game" and me["hp"] is None and me["chakra"] is None:
                pc.create_text(12, h, text="Set your health and chakra bars to see them here.", anchor="w", fill=MUTED, font=(FONT, 8))
                h += 14
            if "mode" in cfg and not state.get("ocr", True):
                pc.create_text(12, h, text="Mode timer needs Windows OCR, which isn't available on this PC.", anchor="w", fill=MUTED, font=(FONT, 8))
                h += 14
        if int(pc["height"]) != int(h):
            pc.config(height=int(h))

    show = tk.BooleanVar(value=cfg.get("overlay", True))
    ov = tk.Toplevel(root)

    def toggle_overlay():
        cfg["overlay"] = show.get()
        save(cfg)
        ov.deiconify() if show.get() else ov.withdraw()

    foot = tk.Frame(frm, bg=BG)
    foot.grid(row=5, column=0, sticky="we", pady=(14, 0))
    sw = switch(foot, show, toggle_overlay)
    sw.pack(side="left")
    tk.Label(foot, text="Party overlay", bg=BG, fg=TEXT, font=(FONT, 10)).pack(side="left", padx=(8, 22))

    def toggle_pics():
        cfg["avatars"] = pics.get()
        save(cfg)

    pics = tk.BooleanVar(value=cfg.get("avatars", True))
    switch(foot, pics, toggle_pics).pack(side="left")
    tk.Label(foot, text="Profile pictures", bg=BG, fg=TEXT, font=(FONT, 10)).pack(side="left", padx=8)
    avatars = Avatars()

    def toggle_share():
        cfg["overlay_share"] = share.get()
        save(cfg)
        set_capturable(ov, share.get())

    share = tk.BooleanVar(value=cfg.get("overlay_share", False))
    foot2 = tk.Frame(frm, bg=BG)
    foot2.grid(row=6, column=0, sticky="we", pady=(10, 0))
    switch(foot2, share, toggle_share).pack(side="left")
    tk.Label(foot2, text="Show overlay on screen share", bg=BG, fg=TEXT, font=(FONT, 10)).pack(side="left", padx=8)
    tk.Label(
        frm, text="When on, keep the overlay off your bars, mode icon and In Combat text, or it will block the readings.",
        bg=BG, fg=MUTED, font=(FONT, 8), anchor="w", justify="left", wraplength=360,
    ).grid(row=7, column=0, sticky="we", pady=(2, 0))
    tk.Label(
        frm, text="Drag to move  ·  drag the corner or scroll to resize  ·  right-click to hide",
        bg=BG, fg=MUTED, font=(FONT, 8), anchor="w",
    ).grid(row=8, column=0, sticky="we", pady=(10, 0))

    view = {"w": cfg.get("overlay_width", 260)}
    ov.overrideredirect(True)
    ov.attributes("-topmost", True)
    ov.attributes("-alpha", 0.93)
    ov.configure(bg=KEY)
    if WIN:
        ov.attributes("-transparentcolor", KEY)
    ov.geometry(cfg.get("overlay_pos", "+20+20"))
    oc = tk.Canvas(ov, bg=KEY, width=view["w"], height=60, highlightthickness=0)
    oc.pack()
    drag = {}

    def near_grip(e):
        return e.x >= oc.winfo_width() - 18 and e.y >= oc.winfo_height() - 18

    def resize(width):
        view["w"] = max(190, min(700, width))
        draw_overlay(oc, state, view["w"], avatars, pics.get())

    def remember(e=None):
        cfg["overlay_pos"] = f"+{ov.winfo_x()}+{ov.winfo_y()}"
        cfg["overlay_width"] = view["w"]
        save(cfg)

    def press(e):
        drag.update(mode="resize" if near_grip(e) else "move", x=e.x, y=e.y, rx=e.x_root, w=view["w"])

    def motion(e):
        if drag.get("mode") == "resize":
            resize(drag["w"] + e.x_root - drag["rx"])
        else:
            ov.geometry(f"+{ov.winfo_x() + e.x - drag['x']}+{ov.winfo_y() + e.y - drag['y']}")

    def wheel(e):
        resize(view["w"] + (20 if e.delta > 0 else -20))
        remember()

    def hide(e):
        show.set(False)
        sw.paint()
        toggle_overlay()

    oc.bind("<Motion>", lambda e: oc.config(cursor="size_nw_se" if near_grip(e) else "fleur"))
    oc.bind("<ButtonPress-1>", press)
    oc.bind("<B1-Motion>", motion)
    oc.bind("<ButtonRelease-1>", remember)
    oc.bind("<MouseWheel>", wheel)
    oc.bind("<Button-3>", hide)
    if not show.get():
        ov.withdraw()

    busy = {"on": False}

    def calibrate(key, prompt):
        if busy["on"]:
            return
        busy["on"] = True
        root.withdraw()
        ov.withdraw()
        toast = tk.Toplevel(root)
        toast.overrideredirect(True)
        toast.attributes("-topmost", True)
        toast.configure(bg=ACCENT)
        lbl = tk.Label(toast, bg=CARD, fg=TEXT, font=(FONT, 12, "bold"), padx=22, pady=12)
        lbl.pack(padx=1, pady=1)

        def step(n):
            if n:
                lbl.config(text=f"Switch to Roblox  ·  capturing in {n}")
                toast.update_idletasks()
                toast.geometry(f"+{(toast.winfo_screenwidth() - toast.winfo_width()) // 2}+60")
                root.after(1000, step, n - 1)
                return
            toast.destroy()
            root.update()
            root.after(200, finish)

        def finish():
            try:
                picked = pick_region(root, prompt)
                if picked:
                    cfg[key] = CONFIGS[key](*picked)
                    save(cfg)
                    state["cfg"] = dict(cfg)
            finally:
                root.deiconify()
                if show.get():
                    ov.deiconify()
                busy["on"] = False
                refresh_areas()

        step(3)

    def close():
        cfg["window"] = root.geometry().split("+")[0]
        save(cfg)
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)

    def tick():
        note = state.get("update")
        update_var.set(note or ("dev build" if VERSION == "dev" else f"v{VERSION}"))
        update_label.config(fg=ACCENT_HI if note else MUTED)
        if state.get("restart"):
            close()
            os._exit(0)
        st = state["status"]
        status_var.set(st)
        color = OK if st.startswith("Connected") else WARN if st.startswith(("Connecting", "Starting")) else MUTED if st.startswith("Enter") else BAD
        dot.itemconfig(dot_id, fill=color)
        draw_preview()
        draw_overlay(oc, state, view["w"], avatars, pics.get())
        root.after(60, tick)

    refresh_areas()
    root.update_idletasks()
    need_w, need_h = frm.winfo_reqwidth() + 32, frm.winfo_reqheight() + 28
    root.minsize(max(380, need_w), need_h)
    w, _, h = cfg.get("window", "440x780").partition("x")
    root.geometry(f"{max(int(w), need_w)}x{max(int(h), need_h)}")
    dark_titlebar(root)
    root.deiconify()
    root.after(300, lambda: set_capturable(ov, share.get()))
    tick()
    root.mainloop()


if __name__ == "__main__":
    main()