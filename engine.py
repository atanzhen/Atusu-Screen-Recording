# -*- coding: utf-8 -*-
"""
atusu 录屏 - 采集与编码引擎
=========================================
纯标准库 + Pillow 实现，兼容 Windows 7 / 8 / 10 / 11 (Python 3.8+)。
  * GDI 高速截屏（ctypes），失败自动回退 PIL.ImageGrab
  * MP4(H.264) 通过 ffmpeg 管道实时编码
  * AVI(MJPG) 内置封装器，无需任何外部程序
  * GIF 动图收集器
"""

import os
import sys
import time
import json
import struct
import shutil
import threading
import subprocess
import datetime

from PIL import Image

APP_NAME = "ATUSU 录屏"
APP_VERSION = "1.3.3"
APP_PUBLISHER = "ATUSU"
APP_SITE = "atusu.cn/so"
APP_SITE_LABEL = "更多软件下载"
APP_SITE_URL = "https://atusu.cn/so"

# 单文件版标记：由启动脚本在启动前置 True
IS_SINGLE_FILE = bool(os.environ.get("ATUSU_SINGLEFILE"))
# 整合版标记：单文件且内置了 ffmpeg（可直接输出 MP4）
IS_PACKED_FFMPEG = bool(os.environ.get("ATUSU_PACKED_FFMPEG"))

# ---------------------------------------------------------------- win32 基础

import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
SM_CXSCREEN = 0
SM_CYSCREEN = 1

CREATE_NO_WINDOW = 0x08000000
SRCCOPY = 0x00CC0020


def enable_dpi_awareness():
    """让程序按物理像素工作，保证区域选择坐标与截屏坐标一致。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # Win8.1+
        return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()                      # Win7 / Win8
    except Exception:
        pass


def primary_screen_rect():
    return (0, 0, int(user32.GetSystemMetrics(SM_CXSCREEN)),
            int(user32.GetSystemMetrics(SM_CYSCREEN)))


def virtual_screen_rect():
    x = int(user32.GetSystemMetrics(SM_XVIRTUALSCREEN))
    y = int(user32.GetSystemMetrics(SM_YVIRTUALSCREEN))
    w = int(user32.GetSystemMetrics(SM_CXVIRTUALSCREEN))
    h = int(user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
    if w <= 0 or h <= 0:
        return primary_screen_rect()
    return (x, y, w, h)


SPI_GETWORKAREA = 0x0030


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def work_area():
    """主显示器的工作区 (left, top, right, bottom)，已排除任务栏。

    悬浮控件的默认位置要贴在任务栏上方，直接取屏幕高度会把控件压到任务栏
    底下，所以这里读 SPI_GETWORKAREA；失败时退回整块主屏。
    """
    try:
        r = _RECT()
        if user32.SystemParametersInfoW(SPI_GETWORKAREA, 0,
                                        ctypes.byref(r), 0):
            if r.right > r.left and r.bottom > r.top:
                return (int(r.left), int(r.top),
                        int(r.right), int(r.bottom))
    except Exception:
        pass
    return primary_screen_rect()


# ---------------------------------------------------------------- 智能选区

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
GA_ROOT = 2

# 桌面外壳类窗口不作为候选（避免框到任务栏/桌面/输入法候选框）
_SKIP_CLASSES = {"progman", "workerw", "shell_traywnd", "shell_secondarytraywnd",
                 "windows.ui.core.corewindow", "applicationframewindow",
                 "ime", "msctfimeui", "default ime"}

try:
    _dwmapi = ctypes.windll.dwmapi
except Exception:
    _dwmapi = None


def window_visual_rect(hwnd):
    """取窗口的「看起来的」边界：Win8+ 用 DWM 扩展边框，Win7 退回 GetWindowRect。

    DWM 扩展边框会去掉 Win10/11 隐藏在边框里的阴影留白，框选出来更贴合视觉。
    """
    r = wintypes.RECT()
    if _dwmapi is not None:
        try:
            if _dwmapi.DwmGetWindowAttribute(
                    wintypes.HWND(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS,
                    ctypes.byref(r), ctypes.sizeof(r)) == 0:
                if r.right > r.left and r.bottom > r.top:
                    return (int(r.left), int(r.top),
                            int(r.right - r.left), int(r.bottom - r.top))
        except Exception:
            pass
    try:
        if user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r)):
            return (int(r.left), int(r.top),
                    int(r.right - r.left), int(r.bottom - r.top))
    except Exception:
        pass
    return None


def _window_cloaked(hwnd):
    """UWP 应用会留下「隐形」的幽灵窗口，需要跳过。"""
    if _dwmapi is None:
        return False
    try:
        v = wintypes.DWORD(0)
        if _dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), DWMWA_CLOAKED,
                                        ctypes.byref(v),
                                        ctypes.sizeof(v)) == 0:
            return bool(v.value)
    except Exception:
        pass
    return False


def window_title(hwnd):
    try:
        n = user32.GetWindowTextLengthW(wintypes.HWND(hwnd))
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(wintypes.HWND(hwnd), buf, n + 1)
        return buf.value.strip()
    except Exception:
        return ""


def window_class(hwnd):
    try:
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(wintypes.HWND(hwnd), buf, 256)
        return buf.value.strip()
    except Exception:
        return ""


def pick_window(px, py, exclude_pid=None, min_size=64):
    """拾取屏幕坐标 (px, py) 处最上层的可见窗口。

    返回 dict(rect=(x, y, w, h), title=str, cls=str, hwnd=int)，没命中返回 None。
    EnumWindows 按 z 序从上到下遍历，命中第一个即停止。
    """
    px, py = int(px), int(py)
    vx, vy, vw, vh = virtual_screen_rect()
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):
        try:
            h = int(hwnd)
            if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                return True
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if exclude_pid and pid.value == int(exclude_pid):
                return True
            if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
                return True
            title = window_title(h)
            if not title:
                return True
            if window_class(h).lower() in _SKIP_CLASSES:
                return True
            if _window_cloaked(h):
                return True
            rect = window_visual_rect(h)
            if not rect:
                return True
            x, y, w, hh = rect
            if w < min_size or hh < min_size:
                return True
            if not (x <= px < x + w and y <= py < y + hh):
                return True
            # 与虚拟屏幕求交（窗口可能部分在屏幕外）
            nx, ny = max(x, vx), max(y, vy)
            nr = min(x + w, vx + vw)
            nb = min(y + hh, vy + vh)
            if nr - nx < min_size or nb - ny < min_size:
                return True
            found.append({"rect": (nx, ny, nr - nx, nb - ny),
                          "title": title, "cls": window_class(h),
                          "hwnd": h})
            return False
        except Exception:
            return True

    try:
        user32.EnumWindows(_cb, 0)
    except Exception:
        return None
    return found[0] if found else None


# ---------------------------------------------------------------- 截屏实现

class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD),
                ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class _CursorInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("hCursor", ctypes.c_void_p),
                ("ptScreenPos", wintypes.POINT)]


CURSOR_SHOWING = 0x00000001
DI_NORMAL = 0x0003

user32.GetCursorInfo.argtypes = [ctypes.POINTER(_CursorInfo)]
user32.GetCursorInfo.restype = wintypes.BOOL
try:
    user32.DrawIconEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                  wintypes.UINT, wintypes.HBRUSH, wintypes.UINT]
    user32.DrawIconEx.restype = wintypes.BOOL
except Exception:
    pass


class _IconInfo(ctypes.Structure):
    _fields_ = [("fIcon", wintypes.BOOL),
                ("xHotspot", wintypes.DWORD),
                ("yHotspot", wintypes.DWORD),
                ("hbmMask", wintypes.HBITMAP),
                ("hbmColor", wintypes.HBITMAP)]


user32.GetIconInfo.argtypes = [wintypes.HICON, ctypes.POINTER(_IconInfo)]
user32.GetIconInfo.restype = wintypes.BOOL

_HOTSPOT_CACHE = {}


def _cursor_hotspot(hcursor):
    """读取光标热点（同一句柄只查询一次，避免频繁创建位图）。"""
    key = int(hcursor or 0)
    if key in _HOTSPOT_CACHE:
        return _HOTSPOT_CACHE[key]
    hx = hy = 0
    try:
        ii = _IconInfo()
        if user32.GetIconInfo(wintypes.HICON(hcursor), ctypes.byref(ii)):
            hx, hy = int(ii.xHotspot), int(ii.yHotspot)
            for hb in (ii.hbmMask, ii.hbmColor):
                if hb:
                    gdi32.DeleteObject(hb)
    except Exception:
        pass
    _HOTSPOT_CACHE[key] = (hx, hy)
    return hx, hy


def _draw_cursor_on_dc(hdc, ox, oy, clip_w=None, clip_h=None):
    """把系统鼠标指针画到 hdc 上（ox/oy 为 hdc 对应的屏幕原点）。

    DrawIconEx 以左上角定位，所以要用「光标坐标 - 热点」作为绘制点。
    """
    ci = _CursorInfo()
    ci.cbSize = ctypes.sizeof(_CursorInfo)
    try:
        if not user32.GetCursorInfo(ctypes.byref(ci)):
            return False
        if not (ci.flags & CURSOR_SHOWING) or not ci.hCursor:
            return False
        hx, hy = _cursor_hotspot(ci.hCursor)
        cx = int(ci.ptScreenPos.x) - hx - int(ox)
        cy = int(ci.ptScreenPos.y) - hy - int(oy)
        if clip_w is not None and (cx < -32 or cy < -32 or
                                   cx > clip_w + 32 or cy > clip_h + 32):
            return False
        user32.DrawIconEx(hdc, cx, cy, ci.hCursor, 0, 0, 0, None, DI_NORMAL)
        return True
    except Exception:
        return False



class GdiGrabber(object):
    """基于 BitBlt + GetDIBits 的高速截屏器（每次 grab 复用 DC/位图缓存）。"""

    def __init__(self):
        self._mem_dc = None
        self._bmp = None
        self._bmp_size = (0, 0)
        self._screen_dc = None

    def _ensure(self, w, h):
        if self._screen_dc is None:
            self._screen_dc = user32.GetDC(0)
            if not self._screen_dc:
                raise OSError("GetDC 失败")
        if self._mem_dc is None:
            self._mem_dc = gdi32.CreateCompatibleDC(self._screen_dc)
            if not self._mem_dc:
                raise OSError("CreateCompatibleDC 失败")
        if self._bmp is None or self._bmp_size != (w, h):
            if self._bmp:
                gdi32.DeleteObject(self._bmp)
                self._bmp = None
            self._bmp = gdi32.CreateCompatibleBitmap(self._screen_dc, w, h)
            if not self._bmp:
                raise OSError("CreateCompatibleBitmap 失败")
            self._bmp_size = (w, h)
            gdi32.SelectObject(self._mem_dc, self._bmp)

    def grab(self, x, y, w, h, cursor=False):
        w = max(1, int(w))
        h = max(1, int(h))
        self._ensure(w, h)
        if not gdi32.BitBlt(self._mem_dc, 0, 0, w, h, self._screen_dc,
                            int(x), int(y), SRCCOPY):
            raise OSError("BitBlt 失败")
        if cursor:
            _draw_cursor_on_dc(self._mem_dc, x, y, w, h)
        bi = _BitmapInfoHeader()
        bi.biSize = 40
        bi.biWidth = w
        bi.biHeight = -h          # 负数 = 自上而下
        bi.biPlanes = 1
        bi.biBitCount = 32
        bi.biCompression = 0      # BI_RGB
        bi.biSizeImage = w * h * 4
        buf = ctypes.create_string_buffer(w * h * 4)
        got = gdi32.GetDIBits(self._mem_dc, self._bmp, 0, h, buf,
                              ctypes.byref(bi), 0)
        if not got:
            raise OSError("GetDIBits 失败")
        return Image.frombytes("RGB", (w, h), buf.raw, "raw", "BGRX", 0, 1)

    def close(self):
        try:
            if self._bmp:
                gdi32.DeleteObject(self._bmp)
            if self._mem_dc:
                gdi32.DeleteDC(self._mem_dc)
            if self._screen_dc:
                user32.ReleaseDC(0, self._screen_dc)
        except Exception:
            pass
        self._bmp = self._mem_dc = self._screen_dc = None


class PillowGrabber(object):
    def grab(self, x, y, w, h, cursor=False):
        from PIL import ImageGrab
        try:
            img = ImageGrab.grab(bbox=(x, y, x + int(w), y + int(h)),
                                 all_screens=True).convert("RGB")
        except TypeError:
            img = ImageGrab.grab(bbox=(x, y, x + int(w), y + int(h))).convert("RGB")
        return img

    def close(self):
        pass


def make_grabber():
    try:
        g = GdiGrabber()
        g.grab(0, 0, 8, 8)     # 试抓一帧验证可用
        return g
    except Exception:
        return PillowGrabber()


# ---------------------------------------------------------------- 编码器

class AviWriter(object):
    """流式写入 MJPG-AVI，无需 ffmpeg，Windows 播放器均可播放。"""

    def __init__(self, path, width, height, fps=30, quality=75):
        self.path = path
        self.width = int(width)
        self.height = int(height)
        self.fps = max(1, int(fps))
        self.quality = int(quality)
        self.frames = 0
        self.bytes_written = 0
        self._index = []
        self._f = open(path, "wb")
        self._write_header()

    # -- header ------------------------------------------------------
    def _write_header(self):
        f = self._f
        w, h, fps = self.width, self.height, self.fps
        f.write(b"RIFF")
        self._riff_size_pos = f.tell()
        f.write(struct.pack("<I", 0))
        f.write(b"AVI ")

        f.write(b"LIST")
        hdrl_size_pos = f.tell()
        f.write(struct.pack("<I", 0))
        hdrl_start = f.tell()
        f.write(b"hdrl")

        f.write(b"avih")
        f.write(struct.pack("<I", 56))
        self._avih_pos = f.tell()
        f.write(struct.pack("<14I",
                            int(1000000 / fps),          # dwMicroSecPerFrame
                            w * h * 3 * fps // 2,        # dwMaxBytesPerSec
                            0,                           # dwPaddingGranularity
                            0x10,                        # dwFlags: HASINDEX
                            0,                           # dwTotalFrames
                            0,                           # dwInitialFrames
                            1,                           # dwStreams
                            w * h * 3,                   # dwSuggestedBufferSize
                            w, h, 0, 0, 0, 0))

        f.write(b"LIST")
        strl_size_pos = f.tell()
        f.write(struct.pack("<I", 0))
        strl_start = f.tell()
        f.write(b"strl")

        f.write(b"strh")
        f.write(struct.pack("<I", 56))
        self._strh_pos = f.tell()
        f.write(struct.pack("<4s4sIHHIIIIIIIIhhhh",
                            b"vids", b"MJPG",
                            0, 0, 0, 0,
                            1, fps,                  # dwScale / dwRate
                            0,                       # dwStart
                            0,                       # dwLength
                            w * h * 3,
                            0xFFFFFFFF,              # dwQuality (-1)
                            0,
                            0, 0, w, h))

        f.write(b"strf")
        f.write(struct.pack("<I", 40))
        f.write(struct.pack("<IiiHH4sIiiII",
                            40, w, h, 1, 24, b"MJPG",
                            w * h * 3, 0, 0, 0, 0))

        f.write(b"JUNK")
        f.write(struct.pack("<I", 4))
        f.write(b"\x00\x00\x00\x00")

        end = f.tell()
        f.seek(strl_size_pos)
        f.write(struct.pack("<I", end - strl_start))
        f.seek(hdrl_size_pos)
        f.write(struct.pack("<I", end - hdrl_start))

        f.seek(end)
        f.write(b"LIST")
        self._movi_size_pos = f.tell()
        f.write(struct.pack("<I", 0))
        self._movi_fourcc_pos = f.tell()
        f.write(b"movi")

    # -- frames ------------------------------------------------------
    def add_image(self, img):
        import io
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=self.quality, subsampling=2,
                 optimize=False)
        data = buf.getvalue()
        f = self._f
        pos = f.tell()
        f.write(b"00dc")
        f.write(struct.pack("<I", len(data)))
        f.write(data)
        if len(data) % 2:
            f.write(b"\x00")
        self._index.append((pos - self._movi_fourcc_pos, len(data)))
        self.frames += 1
        self.bytes_written += len(data)

    def close(self):
        f = self._f
        end = f.tell()
        movi_size = end - self._movi_fourcc_pos
        f.seek(self._movi_size_pos)
        f.write(struct.pack("<I", movi_size))
        f.seek(end)

        f.write(b"idx1")
        f.write(struct.pack("<I", 16 * len(self._index)))
        for off, size in self._index:
            f.write(struct.pack("<4sIII", b"00dc", 0x10, off, size))

        total = f.tell()
        f.seek(self._riff_size_pos)
        f.write(struct.pack("<I", total - 8))
        f.seek(self._avih_pos + 16)
        f.write(struct.pack("<I", self.frames))
        f.seek(self._strh_pos + 32)
        f.write(struct.pack("<I", self.frames))
        f.seek(0, os.SEEK_END)
        f.close()
        return self.path


class FfmpegWriter(object):
    """把原始 RGB 帧通过管道交给 ffmpeg 实时编码为 H.264 MP4。"""

    def __init__(self, path, width, height, fps=30, ffmpeg=None, crf=23):
        self.path = path
        self.width = int(width)
        self.height = int(height)
        self.fps = max(1, int(fps))
        self.frames = 0
        self.ffmpeg = ffmpeg or find_ffmpeg()
        if not self.ffmpeg:
            raise RuntimeError("未找到 ffmpeg")
        vf = "scale=trunc(iw/2)*2:trunc(ih/2)*2"
        cmd = [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
               "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-s", "%dx%d" % (self.width, self.height),
               "-r", str(self.fps), "-i", "pipe:0",
               "-an", "-vf", vf,
               "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
               "-pix_fmt", "yuv420p", "-movflags", "+faststart",
               self.path]
        self._err = None
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE,
                                     creationflags=CREATE_NO_WINDOW)

    def add_image(self, img):
        if img.mode != "RGB":
            img = img.convert("RGB")
        self.proc.stdin.write(img.tobytes())
        self.frames += 1

    def close(self):
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            _, err = self.proc.communicate(timeout=120)
            self._err = err
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        if not os.path.exists(self.path) or os.path.getsize(self.path) < 1024:
            msg = (self._err or b"").decode("utf-8", "ignore")[-400:]
            raise RuntimeError("视频编码失败：%s" % (msg or "ffmpeg 未输出文件"))
        return self.path


class GifWriter(object):
    """把帧降采样成 JPEG 暂存，停止时合成动图，控制内存占用。"""

    MAX_WIDTH = 720
    MAX_FPS = 12
    MAX_FRAMES = 4000

    def __init__(self, path, width, height, fps=30):
        self.path = path
        self.fps = min(self.MAX_FPS, max(1, int(fps)))
        scale = 1.0
        if width > self.MAX_WIDTH:
            scale = float(self.MAX_WIDTH) / float(width)
        self.size = (max(2, int(round(width * scale))),
                     max(2, int(round(height * scale))))
        self.frames = 0
        self._data = []
        self._last_t = 0.0
        self._dropped = False

    def add_image(self, img):
        now = time.time()
        if self.frames and (now - self._last_t) < (1.0 / self.fps) - 0.008:
            self._dropped = True
            return
        if self.frames >= self.MAX_FRAMES:
            return
        self._last_t = now
        small = img if img.size == self.size else img.resize(self.size,
                                                             Image.BILINEAR)
        import io
        buf = io.BytesIO()
        small.convert("RGB").save(buf, "JPEG", quality=88)
        self._data.append(buf.getvalue())
        self.frames += 1

    def close(self):
        import io
        delay = int(round(1000.0 / self.fps))
        images = []
        for raw in self._data:
            images.append(Image.open(io.BytesIO(raw)).convert("RGB"))
        if not images:
            images = [Image.new("RGB", self.size, (255, 255, 255))]
        first = images[0].convert("P", palette=Image.ADAPTIVE, colors=256)
        rest = [im.convert("P", palette=Image.ADAPTIVE, colors=256)
                for im in images[1:]]
        first.save(self.path, save_all=True, append_images=rest,
                   duration=delay, loop=0, optimize=False, disposal=1)
        self._data = []
        return self.path


# ---------------------------------------------------------------- ffmpeg 查找

_FFMPEG_CACHE = {"path": ""}


def _app_dirs():
    dirs = []
    try:
        if getattr(sys, "frozen", False):
            dirs.append(os.path.dirname(sys.executable))
            if hasattr(sys, "_MEIPASS"):
                dirs.append(sys._MEIPASS)
        else:
            dirs.append(os.path.dirname(os.path.abspath(__file__)))
    except Exception:
        pass
    return dirs


def _ffmpeg_works(path):
    try:
        p = subprocess.Popen([path, "-hide_banner", "-version"],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             creationflags=CREATE_NO_WINDOW)
        try:
            out, _ = p.communicate(timeout=25)
        except subprocess.TimeoutExpired:
            # 首次运行可能被杀毒软件扫描拖慢，视为可用
            try:
                p.kill()
            except Exception:
                pass
            return True
        return p.returncode == 0 and b"ffmpeg version" in (out or b"")
    except Exception:
        return False


def find_ffmpeg():
    if _FFMPEG_CACHE["path"]:
        p = _FFMPEG_CACHE["path"]
        return p if os.path.isfile(p) else ""
    for c in _ffmpeg_candidates():
        if os.path.isfile(c) and _ffmpeg_works(c):
            _FFMPEG_CACHE["path"] = c
            return c
    return ""


def _ffmpeg_candidates():
    cands = []
    for d in _app_dirs():
        cands += [os.path.join(d, "ffmpeg.exe"),
                  os.path.join(d, "ffmpeg", "ffmpeg.exe"),
                  os.path.join(d, "ffmpeg", "bin", "ffmpeg.exe")]
    w = shutil.which("ffmpeg")
    if w:
        cands.append(w)
    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p:
            cands.append(p)
    except Exception:
        pass
    seen = []
    for c in cands:
        if c and c not in seen:
            seen.append(c)
    return seen


# ---------------------------------------------------------------- 录制核心

STATE_IDLE = "idle"
STATE_RECORDING = "recording"
STATE_PAUSED = "paused"


def even(v):
    v = int(v)
    return v if v % 2 == 0 else v - 1


def calc_output_size(src_w, src_h, cap_height):
    """按“限高”计算输出尺寸，不放大原画面，宽高取偶数。"""
    src_w, src_h = max(2, int(src_w)), max(2, int(src_h))
    if not cap_height or cap_height >= src_h:
        return even(src_w), even(src_h)
    ratio = float(cap_height) / float(src_h)
    return even(src_w * ratio), even(src_h * ratio)


def resolve_format(fmt):
    """返回实际可用的输出格式（缺少 ffmpeg 时 MP4 自动降级为 AVI）。"""
    if fmt == "MP4" and not find_ffmpeg():
        return "AVI"
    return fmt


class Recorder(object):
    """录制控制器：开始 / 暂停 / 继续 / 停止。"""

    def __init__(self):
        self.state = STATE_IDLE
        self.region = (0, 0, 800, 600)
        self.out_size = (800, 600)
        self.fps = 30
        self.fmt = "MP4"
        self.path = ""
        self.frame_count = 0
        self.preview = None
        self.error = ""
        self.capture_cursor = True
        self.writer = None
        self._thread = None
        self._running = False
        self._grabber = None
        self._lock = threading.Lock()
        self._start_ts = 0.0
        self._paused_at = 0.0
        self._paused_total = 0.0
        self.effective_fmt = "MP4"

    # -- 状态查询 ----------------------------------------------------
    @property
    def is_active(self):
        return self.state in (STATE_RECORDING, STATE_PAUSED)

    def elapsed(self):
        if not self.is_active or not self._start_ts:
            return 0.0
        if self.state == STATE_PAUSED:
            return max(0.0, self._paused_at - self._start_ts - self._paused_total)
        return max(0.0, time.time() - self._start_ts - self._paused_total)

    def video_duration(self):
        return self.frame_count / float(max(1, self.fps))

    # -- 生命周期 ----------------------------------------------------
    def start(self, region, out_size, fps, fmt, path):
        if self.is_active:
            raise RuntimeError("正在录制中")
        self.region = tuple(int(v) for v in region)
        self.out_size = (even(out_size[0]), even(out_size[1]))
        self.fps = int(fps)
        self.fmt = fmt
        self.path = path
        self.frame_count = 0
        self.preview = None
        self.error = ""
        self._paused_total = 0.0
        self._paused_at = 0.0
        self._start_ts = time.time()

        use_fmt = resolve_format(fmt)
        self.effective_fmt = use_fmt

        w, h = self.out_size
        if use_fmt == "MP4":
            self.writer = FfmpegWriter(path, w, h, self.fps)
        elif use_fmt == "AVI":
            self.writer = AviWriter(path, w, h, self.fps)
        else:
            self.writer = GifWriter(path, w, h, self.fps)

        self._grabber = None
        self._running = True
        self.state = STATE_RECORDING
        self._thread = threading.Thread(target=self._loop, name="recorder")
        self._thread.daemon = True
        self._thread.start()

    def pause(self):
        if self.state == STATE_RECORDING:
            self._paused_at = time.time()
            self.state = STATE_PAUSED

    def resume(self):
        if self.state == STATE_PAUSED:
            self._paused_total += time.time() - self._paused_at
            self._paused_at = 0.0
            self.state = STATE_RECORDING

    def stop(self):
        if not self.is_active:
            return None
        self._running = False
        self.state = STATE_IDLE
        if self._thread:
            self._thread.join(timeout=8.0)
        self._thread = None
        info = None
        err = ""
        duration = self.video_duration()
        try:
            if self.writer:
                if self.effective_fmt == "GIF":
                    duration = float(self.writer.frames) / float(
                        max(1, self.writer.fps))
                final_path = self.writer.close()
                info = self._build_info(final_path, duration)
        except Exception as e:
            err = str(e)
        finally:
            self.writer = None
            if self._grabber:
                self._grabber.close()
                self._grabber = None
        self.error = err
        return info

    # -- 采集线程 ----------------------------------------------------
    def _loop(self):
        interval = 1.0 / float(max(1, self.fps))
        next_t = time.perf_counter()
        x, y, w, h = self.region
        out_w, out_h = self.out_size
        try:
            self._grabber = make_grabber()
            while self._running:
                if self.state == STATE_PAUSED:
                    time.sleep(0.03)
                    next_t = time.perf_counter()
                    continue
                img = self._grabber.grab(x, y, w, h, cursor=self.capture_cursor)
                if img.size != (out_w, out_h):
                    img = img.resize((out_w, out_h), Image.BILINEAR)
                self.writer.add_image(img)
                self.frame_count += 1
                next_t += interval
                delay = next_t - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
                else:
                    next_t = time.perf_counter()
        except Exception as e:
            self.error = "录制中断：%s" % e
            self._running = False

    def _build_info(self, final_path, duration=None):
        try:
            size = os.path.getsize(final_path)
        except Exception:
            size = 0
        if duration is None:
            duration = self.video_duration()
        return {
            "path": final_path,
            "name": os.path.basename(final_path),
            "size": size,
            "duration": round(duration, 2),
            "width": self.out_size[0],
            "height": self.out_size[1],
            "fps": self.fps,
            "format": self.effective_fmt,
            "created": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }


# ---------------------------------------------------------------- 配置 / 记录

def config_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "atusuRecorder")
    try:
        os.makedirs(d)
    except Exception:
        pass
    return d


def default_save_dir():
    home = os.path.expanduser("~")
    for name in ("Videos", "视频"):
        p = os.path.join(home, name)
        if os.path.isdir(p):
            return os.path.join(p, "ATUSU录屏")
    return os.path.join(home, "ATUSU录屏")


DRIVE_FIXED = 3


def available_drives():
    """返回可用的固定磁盘根目录列表（如 ['C:\\\\', 'D:\\\\']）。"""
    out = []
    try:
        k32 = ctypes.windll.kernel32
        for ch in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            root = ch + ":\\"
            try:
                t = k32.GetDriveTypeW(root)
            except Exception:
                t = 0
            if t == DRIVE_FIXED:
                out.append(root)
    except Exception:
        pass
    if not out:
        for ch in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            root = ch + ":\\"
            if os.path.isdir(root):
                out.append(root)
    return out


def drive_save_dir():
    """单文件版默认保存目录：优先 D 盘，其次 E 盘…，只剩 C 盘则用 C 盘。"""
    drives = available_drives()
    pick = ""
    for root in drives:
        if root[0].upper() != "C":
            pick = root
            break
    if not pick:
        pick = drives[0] if drives else (os.environ.get("SystemDrive", "C:") + "\\")
    return os.path.join(pick, "ATUSU录屏")


class Store(object):
    """settings.json / records.json 读写。"""

    def __init__(self):
        self.dir = config_dir()
        self.settings_path = os.path.join(self.dir, "settings.json")
        self.records_path = os.path.join(self.dir, "records.json")

    def load_settings(self):
        base_dir = drive_save_dir() if IS_SINGLE_FILE else default_save_dir()
        default = {"save_dir": base_dir,
                   "area": "全屏（全部显示器）",
                   "resolution": "原始分辨率", "fps": "30",
                   "format": ("AVI (MJPEG)"
                              if (IS_SINGLE_FILE and not IS_PACKED_FFMPEG)
                              else "MP4 (H.264)"),
                   "region": None}
        try:
            with open(self.settings_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                default.update(data)
        except Exception:
            pass
        if IS_SINGLE_FILE:
            cur = (default.get("save_dir") or "").strip()
            # 单文件版：沿用旧版目录时，自动改为磁盘优先目录
            if (not cur) or cur == default_save_dir() or cur.endswith("atusu录屏"):
                default["save_dir"] = drive_save_dir()
        return default

    def save_settings(self, data):
        try:
            with open(self.settings_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def load_records(self):
        try:
            with open(self.records_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return [r for r in data if isinstance(r, dict)]
        except Exception:
            pass
        return []

    def save_records(self, records):
        try:
            with open(self.records_path, "w", encoding="utf-8") as f:
                json.dump(records, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


# ---------------------------------------------------------------- 杂项工具

def human_size(num):
    try:
        num = float(num)
    except Exception:
        return "-"
    if num < 1024:
        return "%d B" % num
    for unit in ("KB", "MB", "GB", "TB"):
        num /= 1024.0
        if num < 1024 or unit == "TB":
            return ("%.1f %s" % (num, unit)) if num < 100 else ("%.0f %s" % (num, unit))
    return "-"


def human_duration(sec):
    try:
        sec = int(round(float(sec)))
    except Exception:
        return "00:00"
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%d:%02d:%02d" % (h, m, s)
    return "%02d:%02d" % (m, s)


def open_file(path):
    try:
        os.startfile(path)
        return True, ""
    except Exception as e:
        return False, str(e)


def reveal_file(path):
    try:
        if os.path.exists(path):
            subprocess.Popen('explorer /select,"%s"' % os.path.normpath(path))
        else:
            subprocess.Popen('explorer "%s"' % os.path.dirname(os.path.normpath(path)))
        return True, ""
    except Exception as e:
        return False, str(e)


def open_folder(path):
    try:
        os.startfile(path)
        return True, ""
    except Exception as e:
        return False, str(e)
