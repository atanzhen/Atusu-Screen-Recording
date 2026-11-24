# -*- coding: utf-8 -*-
"""
ATUSU 录屏 - 主界面
=========================================
支持 Windows 7 / 8 / 10 / 11
  * 全屏 / 自定义区域录制，区域外框以红色实线常显
  * 录制时主界面自动最小化到任务栏，右侧出现圆形录制控件（暂停 / 继续 / 结束）
  * 可勾选录制系统声音（WASAPI 环回，扬声器输出），暂停时同步暂停
  * 录制完成后自动还原主界面并生成录屏记录
  * F9 / F10 为系统级热键：程序在后台或最小化时依然可用
  * 单实例运行：重复启动只会把已打开的窗口还原到前台
"""

import os
import sys
import math
import time
import ctypes
import datetime
import webbrowser

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox

import engine as E
import overlayui as O
import floatwin as Fw
import hotkeys as H
import audiocap as AC


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

# ---------------------------------------------------------------- 单实例

MUTEX_NAME = "Local\\ATUSU_ScreenRecorder_SingleInstance_1"
_MUTEX = {"handle": None}


def already_running():
    """抢占单实例互斥体：首次运行返回 False，已有实例在跑返回 True。"""
    try:
        k = ctypes.windll.kernel32
        h = k.CreateMutexW(None, False, MUTEX_NAME)
        if not h:
            return False
        if k.GetLastError() == 183:          # ERROR_ALREADY_EXISTS
            k.CloseHandle(h)
            return True
        _MUTEX["handle"] = h                 # 进程存活期间一直持有
        return False
    except Exception:
        return False


def find_main_window():
    """找已运行实例的主窗口（跳过选区红框那几条 2px 细边窗口）。"""
    u = E.user32
    found = []

    def cb(h, _l):
        try:
            if not u.IsWindowVisible(h):
                return True
            n = u.GetWindowTextLengthW(h)
            if not n:
                return True
            b = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(h, b, n + 1)
            if not b.value.startswith("ATUSU 录屏"):
                return True
            # 录制中主窗口是最小化的，此时窗口矩形被挪到 (-32000,-32000)，
            # 不能再用尺寸过滤，否则找不到它。选区红框那几条细边不会是最小化状态。
            if u.IsIconic(h):
                found.append((1 << 30, h))
                return True
            r = _RECT()
            u.GetWindowRect(h, ctypes.byref(r))
            w, hh = r.right - r.left, r.bottom - r.top
            if w >= 500 and hh >= 400:
                found.append((w * hh, h))
        except Exception:
            pass
        return True

    try:
        u.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p,
                                        ctypes.c_void_p)(cb), 0)
    except Exception:
        return 0
    if not found:
        return 0
    found.sort(reverse=True)
    return found[0][1]


def activate_window(hwnd):
    """还原并前置已有窗口（含最小化状态）。"""
    u = E.user32
    k = ctypes.windll.kernel32
    try:
        if u.IsIconic(hwnd):
            u.ShowWindow(hwnd, 9)            # SW_RESTORE
        u.ShowWindow(hwnd, 5)                # SW_SHOW
    except Exception:
        pass
    # 绕过前台窗口限制：先附加到当前前台线程
    fg = 0
    tid_fg = 0
    attached = False
    try:
        fg = u.GetForegroundWindow()
        if fg and fg != hwnd:
            tid_fg = u.GetWindowThreadProcessId(fg, None)
            tid_me = k.GetCurrentThreadId()
            if tid_fg and tid_fg != tid_me:
                attached = bool(u.AttachThreadInput(tid_me, tid_fg, True))
        u.BringWindowToTop(hwnd)
        u.SetForegroundWindow(hwnd)
    except Exception:
        pass
    finally:
        if attached:
            try:
                u.AttachThreadInput(k.GetCurrentThreadId(), tid_fg, False)
            except Exception:
                pass
    return True


# ---------------------------------------------------------------- 外观常量

BG = "#F2F4F8"
CARD = "#FFFFFF"
BORDER = "#E4E8F0"
TEXT = "#1F2937"
MUTED = "#6B7280"
ACCENT = "#2563EB"
ACCENT_DARK = "#1D4ED8"
RED = "#E5484D"
RED_DARK = "#CB3B40"
FRAME_RED = "#FF3B30"
FRAME_W = 2              # 选区外框线宽（细小一级，不遮挡画面）
HINT_RED = "#E5484D"
AMBER = "#F59E0B"
GREEN = "#16A34A"
KEY = "#010203"          # 透明色键（仅在退化为 Canvas 绘制时使用）

FORMATS = ["MP4 (H.264)", "AVI (MJPEG)", "GIF 动图"]
FORMAT_EXT = {"MP4": ".mp4", "AVI": ".avi", "GIF": ".gif"}
RESOLUTIONS = ["原始分辨率", "2160P (4K)", "1440P (2K)", "1080P", "720P", "480P"]
RES_CAP = {"原始分辨率": 0, "2160P (4K)": 2160, "1440P (2K)": 1440,
           "1080P": 1080, "720P": 720, "480P": 480}
FPS_LIST = ["60", "30", "24", "15", "10"]
AREAS = ["全屏（全部显示器）", "自定义区域"]

HINT_IDLE = ("移动鼠标自动识别窗口　·　左键单击确认选择　　|　　"
             "按住左键拖动可自由框选　　|　　Esc 取消")
HINT_SNAP = ("已识别窗口　·　左键单击确认选择　　|　　"
             "按住左键拖动改为自定义框选　　|　　Esc 取消")
HINT_DRAG = "正在框选　·　松开左键确认　　|　　Esc 或右键取消"

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
LWA_ALPHA = 0x00000002
HWND_TOPMOST = -1
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020


def pick_font():
    import tkinter.font as tkfont
    fams = set(tkfont.families())
    for name in ("Microsoft YaHei UI", "Microsoft YaHei", "微软雅黑",
                 "Segoe UI", "Arial"):
        if name in fams:
            return name
    return "TkDefaultFont"


def hwnd_of(win):
    try:
        return E.user32.GetAncestor(win.winfo_id(), 2)
    except Exception:
        return 0


WDA_EXCLUDEFROMCAPTURE = 0x00000011

_EXCLUDE_OK = {"tested": False, "ok": False}


def set_exclude_capture(win):
    """让窗口不出现在截屏/录屏画面里（Win10 2004+ 支持）。"""
    try:
        h = hwnd_of(win)
        if not h:
            return False
        ok = bool(E.user32.SetWindowDisplayAffinity(h, WDA_EXCLUDEFROMCAPTURE))
        if not _EXCLUDE_OK["tested"]:
            _EXCLUDE_OK["tested"] = True
            _EXCLUDE_OK["ok"] = ok
        return ok
    except Exception:
        return False


def make_overlay(win, alpha=255, click_through=True, exclude=True):
    """把窗口设为置顶/不抢焦点/鼠标穿透的叠加层，并可排除出录制画面。"""
    excluded = False
    try:
        h = hwnd_of(win)
        if not h:
            return False
        style = E.user32.GetWindowLongW(h, GWL_EXSTYLE)
        style |= WS_EX_LAYERED | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
        if click_through:
            style |= WS_EX_TRANSPARENT
        E.user32.SetWindowLongW(h, GWL_EXSTYLE, style)
        E.user32.SetLayeredWindowAttributes(h, 0, int(alpha), LWA_ALPHA)
        E.user32.SetWindowPos(h, HWND_TOPMOST, 0, 0, 0, 0,
                              SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE |
                              SWP_FRAMECHANGED)
        if exclude:
            excluded = set_exclude_capture(win)
    except Exception:
        pass
    return excluded


def smooth_ellipse(cv, x0, y0, x1, y1, steps=44, **kw):
    """用高细分平滑多边形画椭圆，避免小尺寸下出现明显的多边形棱角。"""
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    rx, ry = (x1 - x0) / 2.0, (y1 - y0) / 2.0
    pts = []
    for i in range(16):
        a = math.pi * 2.0 * i / 16.0
        pts += [cx + rx * math.cos(a), cy + ry * math.sin(a)]
    return cv.create_polygon(pts, smooth=True, splinesteps=steps, **kw)


def _mode_text():
    if E.IS_PACKED_FFMPEG:
        return "单文件整合版（内置 ffmpeg，免安装）"
    if E.IS_SINGLE_FILE:
        return "单文件绿色版（免安装）"
    return "绿色文件夹版（免安装）"


def pill(cv, x0, y0, x1, y1, **kw):
    """画胶囊/圆形色块：两端为圆，中间为矩形，拼成顺滑的胶囊。"""
    r = (y1 - y0) / 2.0
    items = []
    if x1 - x0 > 2 * r + 1:
        items.append(cv.create_rectangle(x0 + r, y0, x1 - r, y1, **kw))
    items.append(smooth_ellipse(cv, x0, y0, x0 + 2 * r, y1, **kw))
    items.append(smooth_ellipse(cv, x1 - 2 * r, y0, x1, y1, **kw))
    return items


class FlatButton(tk.Button):
    """扁平按钮，带 hover / disabled 配色。"""

    def __init__(self, master, text, command=None, bg=ACCENT, fg="#FFFFFF",
                 hover=ACCENT_DARK, disabled_bg="#E5E7EB",
                 disabled_fg="#9AA3AF", padx=18, pady=8, font=None, **kw):
        tk.Button.__init__(self, master, text=text, command=command,
                           bg=bg, fg=fg, activebackground=hover,
                           activeforeground=fg, disabledforeground=disabled_fg,
                           relief="flat", bd=0, highlightthickness=0,
                           cursor="hand2", padx=padx, pady=pady, font=font, **kw)
        self._bg = bg
        self._hover = hover
        self._dbg = disabled_bg
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)

    def _on_enter(self, _e=None):
        if self["state"] != "disabled":
            self.configure(bg=self._hover)

    def _on_leave(self, _e=None):
        if self["state"] != "disabled":
            self.configure(bg=self._bg)

    def set_enabled(self, ok):
        if ok:
            self.configure(state="normal", bg=self._bg, cursor="hand2")
        else:
            self.configure(state="disabled", bg=self._dbg, cursor="arrow")


class LinkLabel(tk.Label):
    """无下划线可点击文字（只显示文字，不显示网址）。"""

    def __init__(self, master, text, url=None, font=None, bg=CARD,
                 command=None, fg=ACCENT, hover=ACCENT_DARK, **kw):
        tk.Label.__init__(self, master, text=text, fg=fg, bg=bg,
                          cursor="hand2", font=font, **kw)
        self._url = url or ""
        self._cmd = command
        self._fg = fg
        self._hover = hover
        self.bind("<Button-1>", self._open)
        self.bind("<Enter>", lambda e: self.configure(fg=self._hover))
        self.bind("<Leave>", lambda e: self.configure(fg=self._fg))

    def _open(self, _e=None):
        if self._cmd:
            self._cmd()
            return
        try:
            webbrowser.open(self._url if "://" in self._url
                            else "https://" + self._url)
        except Exception:
            pass


class MiniScrollbar(tk.Canvas):
    """细圆角滚动条：Canvas 自绘胶囊滑块，比系统滚动条更贴合浅色界面。

    只负责绘制与拖拽；「内容是否超出」由外部判断后决定 pack / pack_forget。
    """

    W = 10                  # 轨道宽度
    THUMB_MIN = 28          # 滑块最小高度（内容很长时也能抓住）
    TRACK = "#F7F9FD"
    TRACK_HOVER = "#EFF3F9"
    THUMB = "#CBD5E2"
    THUMB_HOVER = "#AAB7C9"

    def __init__(self, master, command=None, **kw):
        kw.setdefault("width", self.W)
        tk.Canvas.__init__(self, master, bg=self.TRACK, highlightthickness=0,
                           bd=0, takefocus=0, **kw)
        self._cmd = command
        self._first, self._last = 0.0, 1.0
        self._hover = False
        self._grab_y = None
        self._grab_first = 0.0
        self._thumb = (0.0, 0.0)
        self._off_top = 0.0      # 顶部留白（让开表头）
        self._off_bottom = 0.0
        self.bind("<Configure>", lambda e: self._redraw())
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<MouseWheel>", self._wheel)

    # ---------------------------------------------------------- 与列表同步
    def raise_widget(self):
        """把滚动条抬到表格之上（Canvas.lift 需要 tag，这里抬窗口本身）。"""
        try:
            self.tk.call("raise", str(self))
        except Exception:
            pass

    def set_view(self, first, last):
        try:
            self._first, self._last = float(first), float(last)
        except (TypeError, ValueError):
            self._first, self._last = 0.0, 1.0
        self._redraw()

    def set_offset(self, top, bottom=0):
        """顶部/底部留白，使滑块只落在表格行区域（不含表头）。"""
        self._off_top, self._off_bottom = float(top), float(bottom)
        self._redraw()

    def _scroll_to(self, frac):
        if self._cmd:
            self._cmd("moveto", max(0.0, min(1.0, frac)))

    # ---------------------------------------------------------------- 绘制
    def _redraw(self):
        self.delete("all")
        h = float(self.winfo_height())
        span = h - self._off_top - self._off_bottom
        if span <= 12 or self._last - self._first >= 0.999:
            self._thumb = (0.0, 0.0)
            return
        top = self._off_top + self._first * span
        bot = self._off_top + self._last * span
        if bot - top < self.THUMB_MIN:
            mid = (top + bot) / 2.0
            top, bot = mid - self.THUMB_MIN / 2.0, mid + self.THUMB_MIN / 2.0
        lo = self._off_top + 2.0
        hi = h - self._off_bottom - 2.0
        top = max(lo, min(top, hi - 8.0))
        bot = min(hi, max(bot, top + 8.0))
        self._thumb = (top, bot)
        x = self.W / 2.0
        # 圆头直线 = 胶囊滑块，端点天然是半圆，不用多边形近似
        self.create_line(x, top + 4, x, bot - 4, width=self.W - 2,
                         capstyle="round",
                         fill=self.THUMB_HOVER if self._hover else self.THUMB)

    # ---------------------------------------------------------------- 交互
    def _enter(self, _e=None):
        self._hover = True
        self.configure(bg=self.TRACK_HOVER)
        self._redraw()

    def _leave(self, _e=None):
        self._hover = False
        self.configure(bg=self.TRACK)
        self._redraw()

    def _press(self, e):
        top, bot = self._thumb
        if bot - top <= 0:
            return
        if top <= e.y <= bot:
            self._grab_y = e.y
            self._grab_first = self._first
            return
        page = (self._last - self._first) * 0.9
        self._scroll_to(self._first - page if e.y < top else self._first + page)

    def _drag(self, e):
        if self._grab_y is None:
            return
        h = max(1.0, self.winfo_height() - self._off_top - self._off_bottom)
        self._scroll_to(self._grab_first + (e.y - self._grab_y) / h)

    def _release(self, _e=None):
        self._grab_y = None

    def _wheel(self, e):
        step = -1 if getattr(e, "delta", 0) > 0 else 1
        self._scroll_to(self._first + step * 0.08)


class RegionFrame(object):
    """常显的红色实线选区外框（4 条无边框窗口拼成，鼠标可穿透且不进入录制画面）。"""

    def __init__(self, app, thickness=FRAME_W, color=FRAME_RED, alpha=236):
        self.app = app
        self.t = int(thickness)
        self.color = color
        self.alpha = alpha
        self.wins = []
        self.region = None
        self.inside = False
        self.excluded = False

    def _strip(self, idx):
        while len(self.wins) <= idx:
            w = tk.Toplevel(self.app.root)
            w.overrideredirect(True)
            w.configure(bg=self.color)
            try:
                w.attributes("-topmost", True)
            except Exception:
                pass
            w.withdraw()
            self.wins.append(w)
        return self.wins[idx]

    def set_region(self, rect, bounds=None, force_outside=False):
        """把外框摆到 rect 四周：外侧有空间就画在外侧，贴屏幕边则画在内侧。"""
        if not rect:
            self.hide()
            return
        x, y, w, h = [int(v) for v in rect]
        self.region = (x, y, w, h)
        t = self.t
        if bounds:
            bx, by, bw, bh = [int(v) for v in bounds]
        else:
            bx, by, bw, bh = E.virtual_screen_rect()

        def out_x(ok):
            return ok and not force_outside

        top_out = out_x(y - t >= by)
        bot_out = out_x(y + h + t <= by + bh)
        left_out = out_x(x - t >= bx)
        right_out = out_x(x + w + t <= bx + bw)
        self.inside = not (top_out and bot_out and left_out and right_out)

        geo = []
        # 上
        geo.append((x - t, y - t, w + 2 * t, t) if top_out
                   else (x, y, w, t))
        # 下
        geo.append((x - t, y + h, w + 2 * t, t) if bot_out
                   else (x, y + h - t, w, t))
        # 左
        geo.append((x - t, y, t, h) if left_out
                   else (x, y, t, h))
        # 右
        geo.append((x + w, y, t, h) if right_out
                   else (x + w - t, y, t, h))

        for i, (gx, gy, gw, gh) in enumerate(geo):
            win = self._strip(i)
            try:
                win.geometry("%dx%d+%d+%d" % (max(1, gw), max(1, gh), gx, gy))
                win.deiconify()
                win.lift()
                win.update_idletasks()
                self.excluded = make_overlay(win, self.alpha, True)
            except Exception:
                pass

    def hide(self):
        for w in self.wins:
            try:
                w.withdraw()
            except Exception:
                pass

    def destroy(self):
        for w in self.wins:
            try:
                w.destroy()
            except Exception:
                pass
        self.wins = []


class FloatingControl(object):
    """录制中的圆形操作控件（原生分层窗口，与主界面完全解耦）。

    为什么不用 Tk 的 Toplevel：Tk 窗口属于 Tk 的窗口树，主界面被最小化 /
    隐藏 / 重建时，系统与 Tk 都可能把它一起处理掉，于是「闪一下就没了」。
    这里改用 CreateWindowExW 直接建一个父窗口为空的原生弹窗：

      * 与主界面没有任何父子 / owner 关系，主界面怎么变都影响不到它；
      * 只在「开始录制」时创建，只在「结束录制 / 退出程序」时销毁，
        中间任何代码路径都不会隐藏它；
      * 700ms 一轮看守：被意外销毁就重建，被压到下层就复位置顶，
        保证「只要在录制，就一定看得见」。
    """

    H = 64
    W_MIN = 64
    W_MAX = 268
    EDGE_R = 20          # 收起时距屏幕右边缘 20px
    EDGE_B = 20          # 距任务栏上沿 20px
    MARGIN = 12          # 夹取位置时保留的最小边距

    def __init__(self, app):
        self.app = app
        self.ft = Fw.LayeredFloat()
        self.hwnd = 0
        self.expanded = False
        self.w = self.W_MIN
        self.right = 0
        self.top = 0
        self.state = E.STATE_IDLE
        self.text = "00:00"
        self.hover = -1
        self._drag = None
        self._pressed = False
        self._prev_down = False
        self._anim_job = None
        self._watch_job = None
        self._poll_job = None
        self._active = False

    # -- 兼容旧调用 --------------------------------------------------
    @property
    def win(self):
        """窗口句柄（未显示时为 None）。"""
        return self.hwnd or None

    # -- 显示 / 关闭 -------------------------------------------------
    def show(self):
        """开始录制时调用：创建并显示控件，此后一直可见到结束。"""
        self._active = True
        self.expanded = False
        self.w = self.W_MIN
        self.hover = -1
        self._drag = None
        self._pressed = False
        self._resolve_pos()
        if not self._create():
            self._active = False
            return False
        self._draw(force=True)
        self._start_loops()
        return True

    def hide(self):
        """只有「结束录制 / 退出程序」才走这里。"""
        self._active = False
        self._stop_loops()
        self._cancel_anim()
        self._drag = None
        self._pressed = False
        self._prev_down = False
        try:
            self.ft.destroy()
        except Exception:
            pass
        self.hwnd = 0
        self.hover = -1

    def _screen(self):
        try:
            return (int(self.app.root.winfo_screenwidth()),
                    int(self.app.root.winfo_screenheight()))
        except Exception:
            return (int(E.user32.GetSystemMetrics(0)) or 1920,
                    int(E.user32.GetSystemMetrics(1)) or 1080)

    def _work_area(self):
        """主屏工作区 (left, top, right, bottom)，已扣掉任务栏。"""
        try:
            return E.work_area()
        except Exception:
            sw, sh = self._screen()
            return (0, 0, sw, sh)

    def _resolve_pos(self):
        """默认位置：屏幕右下角——距右边 20px、距任务栏上沿 20px。

        每次「开始录制」都回到这个位置；拖动只在本次录制内生效。
        """
        _l, _t, right, bottom = self._work_area()
        self.right = right - self.EDGE_R
        self.top = bottom - self.EDGE_B - self.H
        self._clamp()

    def _clamp(self):
        """限制在工作区内，避免控件跑到屏幕外或被任务栏盖住。"""
        wl, wt, wr, wb = self._work_area()
        self.right = max(wl + int(self.w) + self.MARGIN,
                         min(int(self.right), wr - 2))
        self.top = max(wt, min(int(self.top), max(wt, wb - self.H)))

    def _create(self):
        if self.hwnd and self.ft.alive():
            self.ft.show()
            self.ft.keep_topmost()
            return True
        self.hwnd = self.ft.create(int(self.w), self.H,
                                   self.right - int(self.w), self.top)
        if self.hwnd:
            self.ft.show()
        return bool(self.hwnd)

    # -- 周期任务 ----------------------------------------------------
    def _start_loops(self):
        if self._watch_job is None:
            self._watch()
        if self._poll_job is None:
            self._prev_down = self.ft.left_down()
            self._poll()

    def _stop_loops(self):
        for name in ("_watch_job", "_poll_job"):
            job = getattr(self, name)
            if job is not None:
                try:
                    self.app.root.after_cancel(job)
                except Exception:
                    pass
                setattr(self, name, None)

    def _watch(self):
        """看守定时器：控件绝不允许自己消失。"""
        self._watch_job = None
        if not self._active:
            return
        try:
            if not self.ft.alive():
                # 被系统或别的程序弄没了：原地重建（宽度/位置都保持）
                self.hwnd = 0
                if self._create():
                    self._draw(force=True)
            else:
                self.hwnd = self.ft.hwnd
                if not self.ft.visible():
                    self.ft.show()
                self.ft.keep_topmost()
        except Exception:
            pass
        self._watch_job = self.app.root.after(700, self._watch)

    def _poll(self):
        """轮询光标与鼠标左键：不依赖窗口回调，避免线程 / GIL 问题。"""
        self._poll_job = None
        if not self._active:
            return
        try:
            self._poll_input()
        except Exception:
            pass
        self._poll_job = self.app.root.after(16, self._poll)

    def _poll_input(self):
        pos = self.ft.cursor()
        rect = self.ft.rect()
        if pos is None or rect is None:
            return
        left, top, right, bottom = rect
        cx, cy = pos[0] - left, pos[1] - top
        inside = 0 <= cx < (right - left) and 0 <= cy < (bottom - top)
        down = self.ft.left_down()

        if self._pressed:
            if down:
                self._drag_to(pos[0], pos[1])
            else:
                self._release_press(cx, inside)
            self._prev_down = down
            return

        if down and not self._prev_down and inside:
            # 按下沿：记下起点，抬起时再决定是「点击」还是「拖动」
            self._pressed = True
            self._drag = [pos[0], pos[1], self.right, self.top, False,
                          self._target_at(cx)]
        elif inside:
            self._set_hover(self._target_at(cx) if self.expanded else -1)
        else:
            self._set_hover(-1)
        self._prev_down = down

    # -- 状态 --------------------------------------------------------
    def set_state(self, state, text=None):
        changed = (state != self.state) or (text is not None and
                                            text != self.text)
        self.state = state
        if text is not None:
            self.text = text
        if changed and self.hwnd:
            self._draw()

    def _kind(self):
        return "paused" if self.state == E.STATE_PAUSED else "rec"

    # -- 绘制 --------------------------------------------------------
    def _draw(self, force=False):
        if not self.hwnd:
            return
        ss = O.SS if (force or self._anim_job is None) else 2
        try:
            im = O.render_control(int(self.w), self.H, self._kind(),
                                  self.text, self.hover, ss=ss)
        except Exception:
            return
        self.ft.paint(im, self.right - int(self.w), self.top)

    # -- 向左展开 / 收起的动画 ---------------------------------------
    def toggle(self):
        if not self.hwnd:
            return
        self._cancel_anim()
        self.hover = -1
        target = self.W_MIN if self.expanded else self.W_MAX
        self.expanded = not self.expanded
        self._animate(target)

    def _cancel_anim(self):
        if self._anim_job is not None:
            try:
                self.app.root.after_cancel(self._anim_job)
            except Exception:
                pass
            self._anim_job = None

    def _animate(self, target):
        self._anim_job = None
        if not self.hwnd:
            return
        step = 26 if target > self.w else -26
        nxt = self.w + step
        if (step > 0 and nxt >= target) or (step < 0 and nxt <= target):
            nxt = target
        self.w = nxt
        self._draw()
        if self.w != target:
            self._anim_job = self.app.root.after(
                14, lambda: self._animate(target))
        else:
            self._draw(force=True)      # 收尾用最高精度重绘一次

    # -- 命中与交互 --------------------------------------------------
    def _target_at(self, x):
        b0, b1, circ = O.control_metrics(int(self.w), self.H)
        if x >= circ[0]:
            return "circle"
        if not self.expanded:
            return "circle"
        if b0[0] - 2 <= x <= b0[1] + 2:
            return 0
        if b1[0] - 2 <= x <= b1[1] + 2:
            return 1
        return -1

    def _set_hover(self, idx):
        if idx != self.hover:
            self.hover = idx
            if self.hwnd:
                self._draw()

    def _drag_to(self, sx, sy):
        d = self._drag
        if not d:
            return
        dx, dy = sx - d[0], sy - d[1]
        if not d[4] and abs(dx) + abs(dy) < 5:
            return
        d[4] = True
        self.right = d[2] + dx
        self.top = d[3] + dy
        self.hover = -1
        self._clamp()
        self._draw()

    def _release_press(self, cx, inside):
        d = self._drag
        self._pressed = False
        self._drag = None
        if not d:
            return
        if d[4]:                     # 拖动了：只改位置，不触发按钮
            self._clamp()
            self._draw(force=True)
            return
        if inside:
            self._act(self._target_at(cx))

    def _act(self, target):
        """按钮动作交回 Tk 主循环执行，避免在轮询里嵌套业务逻辑。"""
        if target == "circle":
            self.toggle()
        elif target == 0:
            fn = (self.app._resume if self.state == E.STATE_PAUSED
                  else self.app._pause)
            self.app.root.after(0, fn)
        elif target == 1:
            self.app.root.after(0, self.app._stop)




def make_click_through(win):
    """只设置鼠标穿透/不抢焦点，不改动透明属性（保留 -transparentcolor 色键）。"""
    try:
        h = hwnd_of(win)
        if not h:
            return
        style = E.user32.GetWindowLongW(h, GWL_EXSTYLE)
        style |= WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
        E.user32.SetWindowLongW(h, GWL_EXSTYLE, style)
        E.user32.SetWindowPos(h, HWND_TOPMOST, 0, 0, 0, 0,
                              SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE |
                              SWP_FRAMECHANGED)
    except Exception:
        pass


class RegionSelector(object):
    """框选录制区域。

    底座把屏幕压暗，上层用不透明红色实线画选框：
      * 鼠标移动到某个窗口上时，自动吸附成该窗口的可见边界，提示「左键确认」；
      * 左键单击即选中该窗口；若不单击、直接按住左键拖动，则退回自由框选，
        可在任意位置拉出任意大小的矩形。
    """

    def __init__(self, app):
        self.app = app
        self.root = app.root
        self.result = None

    def select(self):
        vx, vy, vw, vh = E.virtual_screen_rect()
        f = self.app.app_font

        dim = tk.Toplevel(self.root)
        dim.overrideredirect(True)
        dim.attributes("-topmost", True)
        try:
            dim.attributes("-alpha", 0.30)
        except Exception:
            pass
        dim.configure(bg="#000000")
        dim.geometry("%dx%d+%d+%d" % (vw, vh, 0, 0))

        over = tk.Toplevel(self.root)
        over.overrideredirect(True)
        over.attributes("-topmost", True)
        try:
            over.attributes("-transparentcolor", KEY)
        except Exception:
            pass
        over.geometry("%dx%d+%d+%d" % (vw, vh, 0, 0))

        canvas = tk.Canvas(dim, bg="#000000", highlightthickness=0,
                           cursor="crosshair")
        canvas.pack(fill="both", expand=True)
        ocv = tk.Canvas(over, bg=KEY, highlightthickness=0)
        ocv.pack(fill="both", expand=True)

        for w in (dim, over):
            w.update_idletasks()
            try:
                E.user32.MoveWindow(hwnd_of(w), vx, vy, vw, vh, True)
            except Exception:
                pass
        over.lift()
        make_click_through(over)

        hint_text = [""]

        def set_hint(text):
            if hint_text[0] == text:
                return
            hint_text[0] = text
            ocv.delete("hint")
            # 顶部只显示红色提示文字，不加黑色背景块
            ocv.create_text(vp["cw"] // 2, 46, text=text, fill=HINT_RED,
                            font=(f, 14, "bold"), tags="hint", width=vp["cw"] - 80)
            ocv.tag_raise("hint")

        vp = {"cw": vw, "ch": vh}

        def on_configure(e):
            vp["cw"], vp["ch"] = max(1, e.width), max(1, e.height)
            cur = hint_text[0]
            hint_text[0] = ""
            if cur:
                set_hint(cur)

        ocv.bind("<Configure>", on_configure)
        set_hint(HINT_IDLE)

        state = {"x": 0, "y": 0, "pressed": False, "moved": False,
                 "cand": None, "t": 0.0}

        def to_local(sx, sy):
            cw, ch = vp["cw"], vp["ch"]
            return (int(round((sx - vx) * float(cw) / vw)),
                    int(round((sy - vy) * float(ch) / vh)))

        def to_screen(ex, ey):
            cw, ch = vp["cw"], vp["ch"]
            return (vx + int(round(ex * float(vw) / cw)),
                    vy + int(round(ey * float(vh) / ch)))

        def draw_frame(x0, y0, x1, y1, tag="box"):
            ocv.create_rectangle(x0, y0, x1, y1, outline=FRAME_RED,
                                 width=2, tags=tag)
            # 四角加长一小段，方便看清边界
            for cx, cy, dx, dy in ((x0, y0, 1, 1), (x1, y0, -1, 1),
                                   (x0, y1, 1, -1), (x1, y1, -1, -1)):
                ocv.create_line(cx, cy, cx + 14 * dx, cy, fill=FRAME_RED,
                                width=3, tags=tag)
                ocv.create_line(cx, cy, cx, cy + 14 * dy, fill=FRAME_RED,
                                width=3, tags=tag)

        def draw_label(cx, cy, text, tag="label", anchor="center"):
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                ocv.create_text(cx + dx, cy + dy, text=text, fill="#FFFFFF",
                                font=(f, 11, "bold"), tags=tag, anchor=anchor)
            ocv.create_text(cx, cy, text=text, fill=HINT_RED,
                            font=(f, 11, "bold"), tags=tag, anchor=anchor)

        def clear(*tags):
            for t in tags:
                ocv.delete(t)

        def show_snap(cand):
            clear("box", "label")
            if not cand:
                canvas.configure(cursor="crosshair")
                set_hint(HINT_IDLE)
                return
            canvas.configure(cursor="hand2")
            set_hint(HINT_SNAP)
            x, y, w, h = cand["rect"]
            x0, y0 = to_local(x, y)
            x1, y1 = to_local(x + w, y + h)
            draw_frame(x0, y0, x1, y1)
            title = cand.get("title") or "窗口"
            if len(title) > 22:
                title = title[:21] + "…"
            txt = "%s   %d × %d   ·   左键确认" % (title, w, h)
            # 上方放不下就放到框内顶部，再不行放框下方，保证不出屏
            if y0 >= 100:
                draw_label((x0 + x1) // 2, y0 - 16, txt)
            elif y1 + 26 < vp["ch"]:
                draw_label((x0 + x1) // 2, y1 + 16, txt)
            else:
                draw_label(x0 + 14, y0 + 20, txt, anchor="w")
            ocv.tag_raise("hint")

        def redraw_custom(ex, ey):
            x0, y0 = state["x"], state["y"]
            x1, y1 = ex, ey
            if x1 < x0:
                x0, x1 = x1, x0
            if y1 < y0:
                y0, y1 = y1, y0
            clear("box", "label", "snap")
            draw_frame(x0, y0, x1, y1)
            w, h = x1 - x0, y1 - y0
            if w >= 24 and h >= 24:
                txt = "%d × %d" % (w, h)
                if y0 >= 100:
                    draw_label((x0 + x1) // 2, y0 - 16, txt)
                elif y1 + 26 < vp["ch"]:
                    draw_label((x0 + x1) // 2, y1 + 16, txt)
                else:
                    draw_label(x0 + 14, y0 + 20, txt, anchor="w")
            ocv.tag_raise("hint")

        def on_move(e):
            """未按下鼠标时：自动识别光标下的窗口并吸附显示。"""
            if state["pressed"]:
                return
            now = time.time()
            if now - state["t"] < 0.05:
                return
            state["t"] = now
            sx, sy = to_screen(e.x, e.y)
            cand = E.pick_window(sx, sy, exclude_pid=os.getpid())
            state["cand"] = cand
            show_snap(cand)

        def on_press(e):
            state["pressed"] = True
            state["moved"] = False
            state["x"], state["y"] = e.x, e.y
            if state["cand"] is None:
                sx, sy = to_screen(e.x, e.y)
                state["cand"] = E.pick_window(sx, sy,
                                              exclude_pid=os.getpid())

        def on_drag(e):
            if not state["pressed"]:
                return
            if (not state["moved"] and
                    abs(e.x - state["x"]) < 5 and abs(e.y - state["y"]) < 5):
                return
            state["moved"] = True
            state["cand"] = None
            set_hint(HINT_DRAG)
            redraw_custom(e.x, e.y)

        def on_release(e):
            # 没有按下就收到的松开事件（刚打开选择器时的残留事件）直接忽略
            if not state["pressed"]:
                return
            state["pressed"] = False
            rect = None
            if state["moved"]:
                x0, y0 = min(state["x"], e.x), min(state["y"], e.y)
                x1, y1 = max(state["x"], e.x), max(state["y"], e.y)
                if x1 - x0 >= 24 and y1 - y0 >= 24:
                    sx0, sy0 = to_screen(x0, y0)
                    sx1, sy1 = to_screen(x1, y1)
                    rect = (sx0, sy0, sx1 - sx0, sy1 - sy0)
            else:
                cand = state["cand"]
                if not cand:
                    sx, sy = to_screen(e.x, e.y)
                    cand = E.pick_window(sx, sy, exclude_pid=os.getpid())
                if cand:
                    rect = tuple(cand["rect"])
            if rect:
                self.result = rect
            close()

        def close(_e=None):
            for w in (dim, over):
                try:
                    w.grab_release()
                except Exception:
                    pass
                try:
                    w.destroy()
                except Exception:
                    pass

        canvas.bind("<Motion>", on_move)
        canvas.bind("<ButtonPress-1>", on_press)
        canvas.bind("<B1-Motion>", on_drag)
        canvas.bind("<ButtonRelease-1>", on_release)
        canvas.bind("<Button-3>", close)
        dim.bind("<Escape>", close)
        try:
            dim.grab_set()
        except Exception:
            pass
        dim.focus_force()
        over.lift()
        self.root.wait_window(dim)
        return self.result


class AboutDialog(object):
    """关于软件：软件信息、功能说明与版权。"""

    def __init__(self, app):
        self.app = app
        win = tk.Toplevel(app.root)
        self.win = win
        win.title("关于 %s" % E.APP_NAME)
        win.configure(bg=CARD)
        win.resizable(False, False)
        win.transient(app.root)
        f = app.font

        pad = tk.Frame(win, bg=CARD)
        pad.pack(fill="both", expand=True, padx=28, pady=15)

        top = tk.Frame(pad, bg=CARD)
        top.pack(fill="x")
        tk.Label(top, text=E.APP_NAME, bg=CARD, fg=TEXT,
                 font=(f, 16, "bold")).pack(side="left")
        tk.Label(top, text="  v" + E.APP_VERSION, bg=CARD, fg=MUTED,
                 font=(f, 9)).pack(side="left", pady=(7, 0))
        if E.IS_PACKED_FFMPEG:
            tag = "整合版（内置 ffmpeg）"
        elif E.IS_SINGLE_FILE:
            tag = "单文件版"
        else:
            tag = "文件夹版"
        tk.Label(top, text="  " + tag, bg="#EEF2F7", fg=MUTED,
                 font=(f, 8)).pack(side="left", pady=(7, 0), padx=(6, 0))

        tk.Label(pad, text="轻量桌面录屏工具 · 全屏 / 区域录制 · 支持 Windows 7 / 8 / 10 / 11",
                 bg=CARD, fg=MUTED, font=(f, 9)).pack(anchor="w", pady=(4, 9))

        info = tk.Frame(pad, bg="#F8FAFD", highlightthickness=1,
                        highlightbackground=BORDER, highlightcolor=BORDER)
        info.pack(fill="x")
        rows = [("软件名称", E.APP_NAME),
                ("软件版本", "v" + E.APP_VERSION),
                ("开发者", E.APP_PUBLISHER),
                ("运行模式", _mode_text()),
                ("默认保存", app.dir_var.get() or E.default_save_dir())]
        for k, v in rows:
            r = tk.Frame(info, bg="#F8FAFD")
            r.pack(fill="x", padx=12, pady=1)
            tk.Label(r, text=k, bg="#F8FAFD", fg=MUTED, width=8,
                     anchor="w", font=(f, 9)).pack(side="left")
            tk.Label(r, text=v, bg="#F8FAFD", fg=TEXT, anchor="w",
                     font=(f, 9)).pack(side="left")

        self._section(pad, "软件功能")
        feats = [
            "全屏（全部显示器）录制，或自定义矩形区域录制",
            "智能选区：鼠标移到窗口上自动吸附窗口边界，左键单击即可选中",
            "也可按住左键拖动，在任意位置框选任意大小的矩形",
            "选区以红色细实线外框常显，录制过程中同样显示",
            "录制画面包含鼠标指针与全部窗口（含系统弹窗）",
            "支持原始 / 4K / 2K / 1080P / 720P / 480P 分辨率输出",
            "支持 MP4（H.264）、AVI（MJPEG）、GIF 动图三种格式",
            "可勾选录制系统声音（扬声器输出，不含麦克风），暂停时同步暂停",
            "开始 / 暂停 / 继续 / 结束，录制中主界面最小化到任务栏、右下角悬浮圆形控件操作",
            "结束即自动保存，并回到主界面生成录屏记录",
            "录屏记录支持右键「打开文件」「打开文件位置」",
            "单实例运行：重复双击只会把已打开的界面还原到前台",
        ]
        for t in feats:
            self._bullet(pad, t)

        self._section(pad, "使用方法")
        steps = [
            "1. 点红色的「选择区域」，鼠标移到要录的窗口上单击左键即可（拖动可自由框选）",
            "2. 选择输出分辨率、帧率与文件格式（默认 30 帧）",
            "3. 点击「开始录屏」，主界面最小化到任务栏，右下角出现圆形控件",
            "4. 点圆形控件向左展开，可「暂停 / 继续 / 结束」",
            "5. 结束后主界面自动还原，文件信息显示在「录屏记录」中",
        ]
        for t in steps:
            tk.Label(pad, text=t, bg=CARD, fg=TEXT, font=(f, 9),
                     anchor="w", justify="left").pack(fill="x", pady=1)

        tip_txt = ("提示：勾选「录制系统声音」可录下扬声器播放的声音（GIF 不支持，AVI 需要 ffmpeg）；"
                   "程序在后台或最小化时，F9 开始 / 结束、F10 暂停 / 继续同样有效；"
                   "圆形控件可拖动位置；重复双击程序不会开出第二个窗口，只会把已打开的界面还原到前台。")
        if E.IS_PACKED_FFMPEG:
            tip_txt += "\n本版已内置 ffmpeg，可直接输出 MP4，无需安装任何外部程序。"
        elif E.IS_SINGLE_FILE:
            tip_txt += ("\n本版内置 AVI / GIF 编码；把 ffmpeg.exe 放到本程序同一目录"
                        "即可输出 MP4，或直接使用整合版。")
        else:
            tip_txt += "\nMP4 格式需要 ffmpeg，未检测到时自动改用 AVI 格式保存。"
        tip = tk.Frame(pad, bg="#F6F8FC", highlightthickness=1,
                       highlightbackground=BORDER, highlightcolor=BORDER)
        tip.pack(fill="x", pady=(9, 0))
        tk.Label(tip, text=tip_txt, bg="#F6F8FC", fg=MUTED, font=(f, 9),
                 justify="left", anchor="w", wraplength=720).pack(
                     fill="x", padx=12, pady=9)

        tk.Frame(pad, bg=BORDER, height=1).pack(fill="x", pady=(10, 8))

        bottom = tk.Frame(pad, bg=CARD)
        bottom.pack(fill="x")
        FlatButton(bottom, "确定", command=win.destroy, bg="#EEF2F7",
                   fg=TEXT, hover="#E2E8F0", padx=22, pady=6,
                   font=(f, 9)).pack(side="right")
        # 版权、下载入口、开发者放在同一行
        line = tk.Frame(bottom, bg=CARD)
        line.pack(side="left")
        tk.Label(line, text="© 2026 %s　保留所有权利" % E.APP_PUBLISHER,
                 bg=CARD, fg=MUTED, font=(f, 9)).pack(side="left")
        tk.Label(line, text="　·　", bg=CARD, fg="#D5DBE5",
                 font=(f, 9)).pack(side="left")
        LinkLabel(line, E.APP_SITE_LABEL, E.APP_SITE_URL, font=(f, 9),
                  bg=CARD).pack(side="left")
        tk.Label(line, text="　·　开发者 %s" % E.APP_PUBLISHER, bg=CARD,
                 fg=MUTED, font=(f, 9)).pack(side="left")

        win.update_idletasks()
        w, h = win.winfo_width(), win.winfo_height()
        px, py = app.root.winfo_rootx(), app.root.winfo_rooty()
        pw, ph = app.root.winfo_width(), app.root.winfo_height()
        win.geometry("+%d+%d" % (max(0, px + (pw - w) // 2),
                                 max(0, py + (ph - h) // 3)))
        try:
            win.grab_set()
        except Exception:
            pass

    def _section(self, parent, text):
        tk.Label(parent, text=text, bg=CARD, fg=TEXT,
                 font=(self.app.font, 10, "bold")).pack(anchor="w", pady=(7, 3))

    def _bullet(self, parent, text):
        row = tk.Frame(parent, bg=CARD)
        row.pack(fill="x", pady=1)
        tk.Label(row, text="•", bg=CARD, fg=RED,
                 font=(self.app.font, 9)).pack(side="left", anchor="n")
        tk.Label(row, text=text, bg=CARD, fg=TEXT, font=(self.app.font, 9),
                 anchor="w", justify="left").pack(side="left", padx=(6, 0))


# ---------------------------------------------------------------- 主程序

class App(object):

    def __init__(self, root):
        self.root = root
        self.store = E.Store()
        self.settings = self.store.load_settings()
        self.records = self.store.load_records()
        self.recorder = E.Recorder()
        self.font = pick_font()
        self._families = set(tkfont.families())
        self.region = tuple(self.settings.get("region") or ())
        self._stopping = False
        self._hotkey_busy = False
        self._blink = False
        self._status_job = None
        self._status_raw = ""

        root.title("%s  v%s" % (E.APP_NAME, E.APP_VERSION))
        root.configure(bg=BG)
        root.geometry("1000x660")
        root.minsize(940, 600)
        self._center(1000, 660)
        self._set_icon()

        self.app_font = self.font
        self._init_style()
        self._build()
        self._refresh_records_tree()
        self._sync_state()

        if not getattr(self, "_ffmpeg_ready", True):
            self._set_status("未检测到 ffmpeg，已改用 AVI 格式")
        else:
            self._set_status("就绪：点「选择区域」确定录制范围（自动识别窗口），或直接开始录屏")

        self.floating = FloatingControl(self)
        self.frame = RegionFrame(self)
        self._refresh_frame()

        root.protocol("WM_DELETE_WINDOW", self._on_close)
        # F9 / F10 注册为系统级热键：主界面最小化或切到后台后依旧有效。
        # 事件由 hotkeys 模块转回 Tk 主线程，这里收到的回调可以安全操作界面。
        self.hotkeys = H.GlobalHotkeys(root, self._on_hotkey).start()
        self.root.after(150, self._tick)

    # ------------------------------------------------------------ 基础
    def _center(self, w, h):
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 2 - 20)
        self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))

    def _set_icon(self):
        dirs = [os.path.dirname(os.path.abspath(__file__))]
        if getattr(sys, "_MEIPASS", None):
            dirs.insert(0, sys._MEIPASS)
        if getattr(sys, "frozen", False):
            dirs.insert(0, os.path.dirname(sys.executable))
        for d in dirs:
            for name in ("atusu.ico", "recorder.ico"):
                p = os.path.join(d, name)
                if os.path.isfile(p):
                    try:
                        self.root.iconbitmap(default=p)
                        return
                    except Exception:
                        pass

    def _init_style(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except Exception:
            pass
        f = self.font
        st.configure(".", font=(f, 9), background=BG, foreground=TEXT)
        st.configure("TCombobox", fieldbackground=CARD, background=CARD,
                     foreground=TEXT, arrowcolor=MUTED, bordercolor=BORDER,
                     lightcolor=BORDER, darkcolor=BORDER, padding=4)
        st.map("TCombobox",
               fieldbackground=[("readonly", CARD)],
               foreground=[("readonly", TEXT)],
               selectbackground=[("readonly", CARD)],
               selectforeground=[("readonly", TEXT)])
        st.configure("Treeview", background=CARD, fieldbackground=CARD,
                     foreground=TEXT, rowheight=30, borderwidth=0,
                     bordercolor=CARD, lightcolor=CARD, darkcolor=CARD,
                     focuscolor=CARD, focuswidth=0,
                     font=(f, 9))
        st.configure("Treeview.Heading", background="#F7F9FC",
                     foreground=MUTED, relief="flat", font=(f, 9),
                     borderwidth=0, padding=(8, 7))
        st.map("Treeview.Heading", background=[("active", "#EFF3F9")])
        # 点击后不要变黑：焦点/选中的边框色一律跟随卡片底色
        st.map("Treeview", background=[("selected", "#DCE9FF")],
               foreground=[("selected", TEXT)],
               bordercolor=[("focus", CARD), ("!focus", CARD)],
               lightcolor=[("focus", CARD), ("!focus", CARD)],
               darkcolor=[("focus", CARD), ("!focus", CARD)])

    def _card(self, parent):
        # highlightcolor 与 highlightbackground 同色：聚焦时也不会变成黑框
        return tk.Frame(parent, bg=CARD, highlightthickness=1,
                        highlightbackground=BORDER, highlightcolor=BORDER,
                        takefocus=0)

    def _label(self, parent, text, size=9, color=TEXT, bold=False, bg=CARD):
        fnt = (self.font, size, "bold") if bold else (self.font, size)
        return tk.Label(parent, text=text, bg=bg, fg=color, font=fnt)

    # ------------------------------------------------------------ 界面
    def _build(self):
        self.root.grid_rowconfigure(0, weight=1)
        self.root.grid_columnconfigure(0, weight=1)

        body = tk.Frame(self.root, bg=BG)
        body.grid(row=0, column=0, sticky="nsew", padx=14, pady=(12, 8))
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(2, weight=1)

        self._build_settings(body).grid(row=0, column=0, sticky="ew")
        self._build_controls(body).grid(row=1, column=0, sticky="ew",
                                        pady=(12, 0))
        self._build_records(body).grid(row=2, column=0, sticky="nsew",
                                       pady=(12, 0))
        self._build_statusbar().grid(row=1, column=0, sticky="ew")

    def _build_settings(self, parent):
        card = self._card(parent)
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill="x", padx=16, pady=12)

        r1 = tk.Frame(inner, bg=CARD)
        r1.pack(fill="x")

        self._label(r1, "录制区域").pack(side="left")
        self.area_var = tk.StringVar(value=self.settings.get("area", AREAS[0]))
        if self.area_var.get() not in AREAS:
            self.area_var.set(AREAS[0])
        self.cb_area = ttk.Combobox(r1, textvariable=self.area_var,
                                    values=AREAS, state="readonly", width=17)
        self.cb_area.pack(side="left", padx=(8, 8))
        self.cb_area.bind("<<ComboboxSelected>>", self._on_area_change)
        # 「选择区域」是最关键的一步，用红色突出显示；
        # pady 取 3 让其高度与左侧「录制区域」下拉框一致（32px vs 31px）
        self.btn_region = FlatButton(r1, "选择区域", command=self._choose_region,
                                     bg=RED, fg="#FFFFFF", hover=RED_DARK,
                                     disabled_bg="#F4D2D3",
                                     disabled_fg="#FFFFFF",
                                     padx=15, pady=3,
                                     font=(self.font, 9, "bold"))
        self.btn_region.pack(side="left")

        self.lbl_region = self._label(r1, "", 8, MUTED)
        self.lbl_region.pack(side="left", padx=(10, 0))

        right1 = tk.Frame(r1, bg=CARD)
        right1.pack(side="right")
        self._label(right1, "分辨率").pack(side="left", padx=(0, 6))
        self.res_var = tk.StringVar(value=self.settings.get("resolution",
                                                            RESOLUTIONS[0]))
        if self.res_var.get() not in RESOLUTIONS:
            self.res_var.set(RESOLUTIONS[0])
        self.cb_res = ttk.Combobox(right1, textvariable=self.res_var,
                                   values=RESOLUTIONS, state="readonly", width=13)
        self.cb_res.pack(side="left")
        self.cb_res.bind("<<ComboboxSelected>>", lambda e: self._save_settings())

        r2 = tk.Frame(inner, bg=CARD)
        r2.pack(fill="x", pady=(10, 0))
        self._label(r2, "保存位置").pack(side="left")
        self.dir_var = tk.StringVar(value=self.settings.get("save_dir") or
                                    E.default_save_dir())
        self.ent_dir = tk.Entry(r2, textvariable=self.dir_var,
                                bg="#F8FAFD", fg=TEXT, relief="flat",
                                highlightthickness=1,
                                highlightbackground=BORDER,
                                highlightcolor=ACCENT, font=(self.font, 9))
        self.ent_dir.pack(side="left", fill="x", expand=True, padx=(8, 8), ipady=5)
        FlatButton(r2, "浏览…", command=self._browse_dir, bg="#EEF2F7",
                   fg=TEXT, hover="#E2E8F0", padx=12, pady=4,
                   font=(self.font, 9)).pack(side="left")
        FlatButton(r2, "打开目录", command=self._open_save_dir, bg="#EEF2F7",
                   fg=TEXT, hover="#E2E8F0", padx=12, pady=4,
                   font=(self.font, 9)).pack(side="left", padx=(6, 0))

        right2 = tk.Frame(r2, bg=CARD)
        right2.pack(side="right", padx=(14, 0))
        self._label(right2, "格式").pack(side="left", padx=(0, 6))
        self.fmt_var = tk.StringVar(value=self.settings.get("format", FORMATS[0]))
        if self.fmt_var.get() not in FORMATS:
            self.fmt_var.set(FORMATS[0])
        self.cb_fmt = ttk.Combobox(right2, textvariable=self.fmt_var,
                                   values=FORMATS, state="readonly", width=13)
        self.cb_fmt.pack(side="left")
        self.cb_fmt.bind("<<ComboboxSelected>>", self._on_fmt_change)
        # 没有 ffmpeg 时 MP4 不可用，直接切到 AVI，避免每次录制都降级
        self._ffmpeg_ready = bool(E.find_ffmpeg())
        if not self._ffmpeg_ready and self.fmt_var.get().startswith("MP4"):
            self.fmt_var.set("AVI (MJPEG)")

        self._label(right2, "帧率").pack(side="left", padx=(12, 6))
        self.fps_var = tk.StringVar(value=str(self.settings.get("fps", "30")))
        if self.fps_var.get() not in FPS_LIST:
            self.fps_var.set("30")
        self.cb_fps = ttk.Combobox(right2, textvariable=self.fps_var,
                                   values=FPS_LIST, state="readonly", width=5)
        self.cb_fps.pack(side="left")
        self.cb_fps.bind("<<ComboboxSelected>>", lambda e: self._save_settings())

        r3 = tk.Frame(inner, bg=CARD)
        r3.pack(fill="x", pady=(8, 0))
        self.audio_var = tk.BooleanVar(
            value=bool(self.settings.get("audio", False)))
        self.chk_audio = tk.Checkbutton(
            r3, text="录制系统声音", variable=self.audio_var,
            command=self._on_audio_toggle, bg=CARD, fg=TEXT,
            activebackground=CARD, activeforeground=TEXT,
            selectcolor="#FFFFFF", disabledforeground="#A6AEBB",
            font=(self.font, 9), bd=0, highlightthickness=0,
            anchor="w", cursor="hand2")
        self.chk_audio.pack(side="left")
        self.lbl_audio = self._label(r3, "", 8, MUTED)
        self.lbl_audio.pack(side="left", padx=(10, 0))
        return card

    def _audio_supported(self):
        """当前格式 + 编码器能否容纳系统声音。"""
        return (self._fmt_code() in ("MP4", "AVI")
                and bool(getattr(self, "_ffmpeg_ready", False)))

    def _on_audio_toggle(self):
        self._save_settings()
        self._update_audio_state()

    def _on_fmt_change(self, _e=None):
        self._save_settings()
        self._update_audio_state()

    def _update_audio_state(self):
        """声音复选框的可用状态与说明文字。"""
        if not hasattr(self, "chk_audio"):
            return
        idle = not self.recorder.is_active
        supported = self._audio_supported()
        try:
            self.chk_audio.configure(
                state="normal" if (idle and supported) else "disabled")
        except Exception:
            pass
        if not idle:
            hint = "录制中不可更改"
        elif not getattr(self, "_ffmpeg_ready", False):
            hint = "录制声音需要 ffmpeg"
        elif self._fmt_code() == "GIF":
            hint = "GIF 不支持声音"
        elif not supported:
            hint = "当前格式不支持声音"
        elif self.audio_var.get():
            hint = "将录制扬声器输出（不含麦克风）"
        else:
            hint = "勾选后录制扬声器输出（不含麦克风）"
        try:
            self.lbl_audio.configure(text=hint)
        except Exception:
            pass

    def _build_controls(self, parent):
        bar = tk.Frame(parent, bg=BG)
        self.btn_start = FlatButton(bar, "●  开始录屏", command=self._start,
                                    bg=RED, fg="#FFFFFF", hover=RED_DARK,
                                    padx=24, pady=10, font=(self.font, 10, "bold"))
        self.btn_start.pack(side="left")
        self.btn_pause = FlatButton(bar, "❚❚  暂停录屏", command=self._pause,
                                    bg="#FFFFFF", fg="#B45309",
                                    hover="#FFF7E6", padx=20, pady=10,
                                    font=(self.font, 10, "bold"))
        self.btn_pause.pack(side="left", padx=(10, 0))
        self.btn_resume = FlatButton(bar, "▶  继续录屏", command=self._resume,
                                     bg="#FFFFFF", fg="#15803D",
                                     hover="#EFFDF3", padx=20, pady=10,
                                     font=(self.font, 10, "bold"))
        self.btn_resume.pack(side="left", padx=(10, 0))
        self.btn_stop = FlatButton(bar, "■  结束录屏", command=self._stop,
                                   bg="#FFFFFF", fg="#B91C1C",
                                   hover="#FEF2F2", padx=20, pady=10,
                                   font=(self.font, 10, "bold"))
        self.btn_stop.pack(side="left", padx=(10, 0))
        for b in (self.btn_pause, self.btn_resume, self.btn_stop):
            b.configure(highlightthickness=1, highlightbackground=BORDER)

        # 右侧状态区：占满按钮右边余下的空间，让「状态 + 计时」在其内部居中
        right = tk.Frame(bar, bg=BG)
        right.pack(side="left", fill="both", expand=True)
        tfont = ("Consolas", 19, "bold")
        if "Consolas" not in self._families:
            tfont = (self.font, 20, "bold")
        # 状态与计时放大显示，一眼能看清是否在录制、录了多久
        box = tk.Frame(right, bg=BG)
        box.pack(expand=True)
        self.lbl_state = tk.Label(box, text="●  准备就绪", bg=BG, fg=MUTED,
                                  font=(self.font, 11, "bold"))
        self.lbl_state.pack(side="left")
        self.lbl_timer = tk.Label(box, text="00:00", bg=BG, fg=TEXT,
                                  font=tfont)
        self.lbl_timer.pack(side="left", padx=(12, 0))
        return bar

    def _build_records(self, parent):
        card = self._card(parent)
        head = tk.Frame(card, bg=CARD)
        head.pack(fill="x", padx=16, pady=(10, 6))
        self._label(head, "录屏记录", 10, TEXT, bold=True).pack(side="left")
        self.lbl_count = self._label(head, "共 0 条", 8, MUTED)
        self.lbl_count.pack(side="left", padx=(10, 0))

        right = tk.Frame(head, bg=CARD)
        right.pack(side="right")
        FlatButton(right, "刷新", command=self._refresh_records_tree,
                   bg="#EEF2F7", fg=TEXT, hover="#E2E8F0", padx=12, pady=4,
                   font=(self.font, 9)).pack(side="right")
        self._label(right, "右键记录可打开文件 / 打开文件位置 　", 8,
                    MUTED).pack(side="right")

        body = tk.Frame(card, bg=CARD)
        body.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        cols = ("name", "duration", "size", "res", "fmt", "audio", "time")
        self.tree = ttk.Treeview(body, columns=cols, show="headings",
                                 selectmode="browse", height=6)
        heads = {"name": ("文件名", 300), "duration": ("时长", 78),
                 "size": ("大小", 88), "res": ("分辨率", 110),
                 "fmt": ("格式", 64), "audio": ("声音", 52),
                 "time": ("录制时间", 176)}
        for c in cols:
            text, width = heads[c]
            self.tree.heading(c, text=text, anchor="w")
            self.tree.column(c, width=width, anchor="w",
                             stretch=(c == "name"))
        # 自绘细圆角滚动条：浮在表格右缘（不占布局，显隐时列宽不跳动），
        # 内容不超出显示区域时整体隐藏（见 _sync_scrollbar）
        self.sb = MiniScrollbar(body, command=self.tree.yview)
        self._sb_shown = True
        self.tree.configure(yscrollcommand=self._on_tree_scroll)
        self.tree.tag_configure("missing", foreground="#9AA3AF")
        self.tree.pack(side="left", fill="both", expand=True)
        self.sb.place(relx=1.0, y=0, x=-3, anchor="ne", relheight=1.0)

        self.lbl_empty = tk.Label(body, text="暂无录屏记录，完成一次录制后文件信息会显示在这里",
                                  bg=CARD, fg="#A6AEBB", font=(self.font, 9))

        self.tree.bind("<Double-1>", lambda e: self._open_selected_file())
        self.tree.bind("<Button-3>", self._popup_menu)
        # 窗口缩放后可视高度变化，滚动条显隐与滑块长度都要重算
        self.tree.bind("<Configure>",
                       lambda e: self.root.after_idle(self._after_records_layout))

        self.menu = tk.Menu(self.root, tearoff=0, bg=CARD, fg=TEXT,
                            activebackground="#EEF2F7", activeforeground=TEXT,
                            bd=0, relief="flat", font=(self.font, 9))
        self.menu.add_command(label="打开文件", command=self._open_selected_file)
        self.menu.add_command(label="打开文件位置",
                              command=self._reveal_selected_file)
        self.menu.add_separator()
        self.menu.add_command(label="复制文件路径", command=self._copy_path)
        self.menu.add_separator()
        self.menu.add_command(label="从列表移除", command=self._remove_selected)
        self.menu.add_command(label="删除文件", command=self._delete_selected_file)
        return card

    def _dot(self, parent):
        return tk.Label(parent, text="·", bg=CARD, fg="#D5DBE5",
                        font=(self.font, 9))

    def _build_statusbar(self):
        wrap = tk.Frame(self.root, bg=CARD)
        tk.Frame(wrap, bg=BORDER, height=1).pack(fill="x")
        bar = tk.Frame(wrap, bg=CARD, height=36)
        bar.pack(fill="x")
        bar.pack_propagate(False)

        # 右侧信息统一成一行（从右往左 pack）：
        # 更多软件下载 · 关于软件 · 版权 · 快捷键
        # 最后一项离窗口右边缘 18px，不再贴着边框
        LinkLabel(bar, E.APP_SITE_LABEL, E.APP_SITE_URL,
                  font=(self.font, 9), bg=CARD).pack(side="right", padx=(0, 18))
        self._dot(bar).pack(side="right", padx=9)
        LinkLabel(bar, "关于软件", command=lambda: AboutDialog(self),
                  font=(self.font, 9), bg=CARD, fg=MUTED,
                  hover=ACCENT).pack(side="right")
        self._dot(bar).pack(side="right", padx=9)
        tk.Label(bar, text="© 2026 %s　保留所有权利" % E.APP_PUBLISHER,
                 bg=CARD, fg=MUTED, font=(self.font, 9)).pack(side="right")
        self._dot(bar).pack(side="right", padx=9)
        tk.Label(bar, text="F9 开始/结束　F10 暂停/继续（全局）", bg=CARD,
                 fg="#9AA3AF", font=(self.font, 9)).pack(side="right",
                                                         padx=(0, 16))

        # 左侧：操作提示，2 秒后自动隐藏
        self.lbl_status = tk.Label(bar, text="", bg=CARD, fg=MUTED,
                                   font=(self.font, 9), anchor="w")
        self.lbl_status.pack(side="left", fill="x", expand=True,
                             padx=(16, 12))
        self.lbl_status.bind("<Configure>",
                             lambda e: self._render_status())
        return wrap

    # ------------------------------------------------------------ 设置与状态
    def _save_settings(self):
        self.settings.update({
            "save_dir": self.dir_var.get().strip(),
            "area": self.area_var.get(),
            "resolution": self.res_var.get(),
            "fps": self.fps_var.get(),
            "format": self.fmt_var.get(),
            "audio": (bool(self.audio_var.get())
                      if hasattr(self, "audio_var") else False),
            "region": list(self.region) if self.region else None,
        })
        self.store.save_settings(self.settings)
        if not self.recorder.is_active:
            self._update_region_label()

    def _is_fullscreen(self):
        return self.area_var.get() == AREAS[0] or not self.region

    def _current_region(self):
        if self._is_fullscreen():
            return E.virtual_screen_rect()
        return tuple(self.region)

    def _fmt_code(self):
        return self.fmt_var.get().split(" ")[0]

    def _update_region_label(self):
        r = self._current_region()
        if self._is_fullscreen():
            txt = "全屏 %d × %d（红框在屏幕边缘外侧）" % (r[2], r[3])
        else:
            txt = "当前区域 %d × %d @ (%d, %d)" % (r[2], r[3], r[0], r[1])
        self.lbl_region.configure(text=txt)

    def _refresh_frame(self):
        """按当前状态摆放红色选区外框（始终显示，且不进入录制画面）。"""
        if not hasattr(self, "frame"):
            return
        region = self._current_region()
        bounds = E.virtual_screen_rect()
        # 若系统不支持把窗口排除出录制（Win10 2004 之前），
        # 录制中一律画到选区外侧，保证录像里没有红边
        force_out = self.recorder.is_active and not _EXCLUDE_OK["ok"]
        self.frame.set_region(region, bounds, force_outside=force_out)

    def _on_area_change(self, _e=None):
        custom = self.area_var.get() == AREAS[1]
        self.btn_region.set_enabled(not self.recorder.is_active)
        if custom and not self.region:
            self._choose_region()
        else:
            self._save_settings()
            self._refresh_frame()

    def _choose_region(self):
        if self.recorder.is_active:
            return
        self.frame.hide()
        self.root.withdraw()
        try:
            self.root.update()
            time.sleep(0.2)
            sel = RegionSelector(self).select()
        finally:
            self.root.deiconify()
            self.root.lift()
        if sel:
            self.region = sel
            self.area_var.set(AREAS[1])
            self.btn_region.set_enabled(True)
            self._save_settings()
            self._set_status("已选择录制区域：%d × %d" % (sel[2], sel[3]))
        self._refresh_frame()

    def _browse_dir(self):
        d = filedialog.askdirectory(title="选择录制文件保存目录",
                                    initialdir=self.dir_var.get() or os.getcwd())
        if d:
            self.dir_var.set(os.path.normpath(d))
            self._save_settings()

    def _open_save_dir(self):
        d = self.dir_var.get().strip()
        try:
            os.makedirs(d)
        except Exception:
            pass
        ok, err = E.open_folder(d)
        if not ok:
            messagebox.showwarning("打开目录失败", err)

    def _set_status(self, text, hold=False):
        """底部操作提示：默认 2 秒后自动隐藏，避免长期占用底栏。"""
        self._status_raw = text or ""
        if self._status_job:
            try:
                self.root.after_cancel(self._status_job)
            except Exception:
                pass
            self._status_job = None
        self._render_status()
        if text and not hold:
            self._status_job = self.root.after(2000, self._clear_status)

    def _clear_status(self):
        self._status_job = None
        self._status_raw = ""
        try:
            self.lbl_status.configure(text="")
        except Exception:
            pass

    def _render_status(self):
        """按可用宽度截断提示文字，避免和右侧版权信息挤在一起。"""
        raw = getattr(self, "_status_raw", "")
        if not raw:
            try:
                self.lbl_status.configure(text="")
            except Exception:
                pass
            return
        avail = self.lbl_status.winfo_width()
        fnt = tkfont.Font(font=self.lbl_status.cget("font"))
        txt = raw
        if avail > 40 and fnt.measure(txt) > avail - 6:
            while txt and fnt.measure(txt + "…") > avail - 6:
                txt = txt[:-1]
            txt += "…"
        try:
            self.lbl_status.configure(text=txt)
        except Exception:
            pass

    # ------------------------------------------------------------ 录制控制
    def _unique_path(self, ext):
        d = self.dir_var.get().strip() or E.default_save_dir()
        try:
            os.makedirs(d)
        except Exception:
            pass
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        name = "录屏_%s%s" % (stamp, ext)
        path = os.path.join(d, name)
        i = 1
        while os.path.exists(path):
            path = os.path.join(d, "录屏_%s_%d%s" % (stamp, i, ext))
            i += 1
        return path

    def _minimize_for_record(self):
        """录制开始：把主窗口最小化到任务栏（不是隐藏），任务栏图标保持可见。"""
        try:
            self.root.iconify()
        except Exception:
            pass
        try:
            self.root.update_idletasks()
        except Exception:
            pass
        try:
            hwnd = hwnd_of(self.root)
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 6)   # SW_MINIMIZE
        except Exception:
            pass
        self.root.update()

    def _restore_after_record(self):
        """录制结束：把主窗口从最小化还原并置前。"""
        try:
            hwnd = hwnd_of(self.root)
            if hwnd and ctypes.windll.user32.IsIconic(hwnd):
                ctypes.windll.user32.ShowWindow(hwnd, 9)   # SW_RESTORE
        except Exception:
            pass
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except Exception:
            pass

    def _start(self):
        if self.recorder.is_active or self._stopping:
            return
        region = self._current_region()
        if not region or region[2] < 8 or region[3] < 8:
            messagebox.showwarning("无法开始录制", "录制区域无效，请重新选择区域。")
            return
        cap = RES_CAP.get(self.res_var.get(), 0)
        out_w, out_h = E.calc_output_size(region[2], region[3], cap)
        fmt = self._fmt_code()
        use_fmt = E.resolve_format(fmt)
        ext = FORMAT_EXT.get(use_fmt, ".mp4")
        path = self._unique_path(ext)

        # 系统声音：先做一次快速预检，避免开始录制后才发现没有播放设备，
        # 那时主界面已经最小化，提示反而不容易被看到。
        audio = bool(self.audio_var.get()) and self._audio_supported()
        if audio:
            self._set_status("正在检查系统声音…", hold=True)
            try:
                self.root.update_idletasks()
            except Exception:
                pass
            ok, reason = AC.probe(timeout=3.0)
            if not ok:
                audio = False
                messagebox.showwarning(
                    "系统声音不可用",
                    "无法录制系统声音：\n%s\n\n本次将仅录制视频。" % reason)

        # 先最小化主界面（任务栏图标保留，随时能点回来），避免它出现在录制画面里。
        # 不用 withdraw：主窗口隐藏会让任务栏图标一起消失。
        self._minimize_for_record()

        # 圆形操作控件是独立的原生窗口，与主界面没有任何父子关系：
        # 从这里开始它就一直显示在屏幕右侧，只有「结束录制」才会消失。
        self.floating.set_state(E.STATE_RECORDING, "00:00")
        self.floating.show()
        self.root.update_idletasks()
        time.sleep(0.12)

        try:
            self.recorder.start(region, (out_w, out_h), int(self.fps_var.get()),
                                fmt, path, audio=audio)
        except Exception as e:
            self.floating.hide()
            self._restore_after_record()
            messagebox.showerror("启动录制失败", str(e))
            return

        self._refresh_frame()
        self.floating.set_state(self.recorder.state, "00:00")
        if fmt == "MP4" and self.recorder.effective_fmt != "MP4":
            self._set_status("未检测到 ffmpeg，本次改存为 AVI")
        else:
            base = "正在录制：%s" % os.path.basename(path)
            if self.recorder.audio_error:
                self._set_status(base + "（系统声音未启用）")
            elif self.recorder.audio_active:
                self._set_status(base + "（含系统声音）")
            else:
                self._set_status(base)
        self._sync_state()

    def _pause(self):
        if self.recorder.state != E.STATE_RECORDING:
            return
        self.recorder.pause()
        self._set_status("录制已暂停")
        self._sync_state()

    def _resume(self):
        if self.recorder.state != E.STATE_PAUSED:
            return
        self.recorder.resume()
        self._set_status("正在录制：%s" % os.path.basename(self.recorder.path))
        self._sync_state()

    def _stop(self):
        if not self.recorder.is_active or self._stopping:
            return
        self._stopping = True
        self._set_status("正在保存文件…")
        try:
            self.btn_stop.set_enabled(False)
        except Exception:
            pass
        if self.floating:
            self.floating.hide()
        fmt = self.recorder.fmt
        info = self.recorder.stop()
        err = self.recorder.error
        self._stopping = False

        # 结束录制 -> 自动回到主界面展示录屏记录
        self._restore_after_record()

        if info and info.get("size"):
            if self.recorder.effective_fmt != fmt:
                info["note"] = "已自动改用 %s 格式" % self.recorder.effective_fmt
            self.records.insert(0, info)
            self.store.save_records(self.records)
            self._refresh_records_tree(select_path=info["path"])
            msg = "已保存：%s（%s）" % (info["name"],
                                       E.human_size(info["size"]))
            if info.get("audio"):
                msg += (" · 含系统声音" if not self.recorder.audio_error
                        else " · 含部分系统声音")
            elif self.recorder.audio_requested:
                msg += " · 未含系统声音"
            self._set_status(msg)
        else:
            self._set_status("录制未生成文件")
            if err:
                messagebox.showerror("保存失败", err)

        self._sync_state()
        self._refresh_frame()

    def _on_hotkey(self, action):
        """全局热键回调（Tk 主线程）：录制控制不允许重入。"""
        if self._hotkey_busy:
            return
        # 正在选择录制区域时主窗口是 withdraw 状态，此时按热键会打乱选区流程
        try:
            if self.root.state() == "withdrawn":
                return
        except Exception:
            pass
        self._hotkey_busy = True
        try:
            if action == H.ACTION_START_STOP:
                self._hotkey_start_stop()
            elif action == H.ACTION_PAUSE_RESUME:
                self._hotkey_pause_resume()
        finally:
            self._hotkey_busy = False

    def _hotkey_start_stop(self):
        if self.recorder.is_active:
            self._stop()
        else:
            self._start()

    def _hotkey_pause_resume(self):
        if self.recorder.state == E.STATE_RECORDING:
            self._pause()
        elif self.recorder.state == E.STATE_PAUSED:
            self._resume()

    def _sync_state(self):
        st = self.recorder.state
        idle = st == E.STATE_IDLE
        rec = st == E.STATE_RECORDING
        paused = st == E.STATE_PAUSED

        self.btn_start.set_enabled(idle)
        self.btn_pause.set_enabled(rec)
        self.btn_resume.set_enabled(paused)
        self.btn_stop.set_enabled(rec or paused)
        self.cb_area.configure(state="readonly" if idle else "disabled")
        self.cb_res.configure(state="readonly" if idle else "disabled")
        self.cb_fps.configure(state="readonly" if idle else "disabled")
        self.cb_fmt.configure(state="readonly" if idle else "disabled")
        self.ent_dir.configure(state="normal" if idle else "disabled")
        self.btn_region.set_enabled(idle)

        if idle:
            self.lbl_state.configure(text="●  准备就绪", fg=MUTED)
            self.lbl_timer.configure(text="00:00")
        elif rec:
            self.lbl_state.configure(text="●  录制中", fg=RED)
        else:
            self.lbl_state.configure(text="●  已暂停", fg="#B45309")
        self._update_region_label()
        self._update_audio_state()

    # ------------------------------------------------------------ 定时刷新
    def _tick(self):
        try:
            self._tick_once()
        except Exception:
            pass
        self.root.after(150, self._tick)

    def _tick_once(self):
        rec = self.recorder
        if rec.is_active:
            txt = E.human_duration(rec.elapsed())
            self.lbl_timer.configure(text=txt)
            if rec.state == E.STATE_RECORDING:
                self._blink = not self._blink
                self.lbl_state.configure(
                    text="●  录制中", fg=RED if self._blink else "#F3A9AC")
            if self.floating:
                self.floating.set_state(rec.state, txt)
                if not self.floating.win:
                    # 兜底：录制期间控件绝不允许消失
                    self.floating.show()
            if rec.error:
                msg = rec.error
                self._stop()
                messagebox.showerror("录制中断", msg)
                return
        # 注意：这里故意没有「录制结束就隐藏控件」的分支。
        # 控件只由 _stop()（用户点结束 / 出错收尾）销毁，不会自己消失。

    # ------------------------------------------------------------ 记录列表
    def _on_tree_scroll(self, first, last):
        """列表滚动时同步自绘滚动条，并按需显示 / 隐藏。"""
        self.sb.set_view(first, last)
        self._sync_scrollbar(first, last)

    def _sync_scrollbar(self, first=None, last=None):
        """内容没超出显示区域（或没有记录）时，右侧不出现滚动条。"""
        if first is None or last is None:
            try:
                first, last = self.tree.yview()
            except Exception:
                first, last = 0.0, 1.0
        try:
            ratio = float(last) - float(first)
        except (TypeError, ValueError):
            ratio = 1.0
        need = len(self.tree.get_children()) > 0 and ratio < 0.999
        if need == self._sb_shown:
            return
        self._sb_shown = need
        if need:
            self.sb.place(relx=1.0, y=0, x=-3, anchor="ne", relheight=1.0)
            # Canvas 的 lift() 是 tag_raise，需要 tag 参数；这里直接调窗口的 raise
            self.sb.raise_widget()
        else:
            self.sb.place_forget()

    def _align_scrollbar(self):
        """让滑块从表头下方开始，不与表头挤在一起。"""
        kids = self.tree.get_children()
        if not kids:
            return
        try:
            box = self.tree.bbox(kids[0])
        except Exception:
            box = ""
        if box:
            self.sb.set_offset(box[1], 2)

    def _refresh_records_tree(self, select_path=None):
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        valid = []
        for r in self.records:
            path = r.get("path", "")
            exists = os.path.isfile(path)
            if exists:
                try:
                    r["size"] = os.path.getsize(path)
                except Exception:
                    pass
            valid.append(r)
            self.tree.insert(
                "", "end", iid=path or r.get("name", ""),
                values=(r.get("name", "-"),
                        E.human_duration(r.get("duration", 0)),
                        E.human_size(r.get("size", 0)) if exists else "已丢失",
                        "%d×%d" % (r.get("width", 0), r.get("height", 0)),
                        r.get("format", "-"),
                        "有声" if r.get("audio") else "—",
                        r.get("created", "-")),
                tags=() if exists else ("missing",))
        self.records = valid
        self.store.save_records(self.records)
        n = len(valid)
        self.lbl_count.configure(text="共 %d 条" % n)
        if n == 0:
            self.lbl_empty.place(relx=0.5, rely=0.55, anchor="center")
        else:
            self.lbl_empty.place_forget()
        if select_path:
            if self.tree.exists(select_path):
                self.tree.selection_set(select_path)
                self.tree.focus(select_path)
                self.tree.see(select_path)
        # 布局完成后再判断是否需要滚动条（bbox 与 yview 此时才准确）
        self.root.after_idle(self._after_records_layout)

    def _after_records_layout(self):
        self._align_scrollbar()
        self._sync_scrollbar()

    def _selected_record(self):
        sel = self.tree.selection()
        if not sel:
            return None
        iid = sel[0]
        for r in self.records:
            if (r.get("path") or r.get("name")) == iid:
                return r
        return None

    def _popup_menu(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def _open_selected_file(self):
        r = self._selected_record()
        if not r:
            return
        p = r.get("path", "")
        if not os.path.isfile(p):
            messagebox.showwarning("文件不存在", "文件已不存在：\n%s" % p)
            return
        ok, err = E.open_file(p)
        if not ok:
            messagebox.showwarning("无法打开文件", err)

    def _reveal_selected_file(self):
        r = self._selected_record()
        if not r:
            return
        p = r.get("path", "")
        ok, err = E.reveal_file(p)
        if not ok:
            messagebox.showwarning("无法打开位置", err)

    def _copy_path(self):
        r = self._selected_record()
        if not r:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(r.get("path", ""))
        self._set_status("已复制文件路径")

    def _remove_selected(self):
        r = self._selected_record()
        if not r:
            return
        self.records = [x for x in self.records if x is not r]
        self.store.save_records(self.records)
        self._refresh_records_tree()
        self._set_status("已从列表移除（文件仍保留在磁盘）")

    def _delete_selected_file(self):
        r = self._selected_record()
        if not r:
            return
        p = r.get("path", "")
        if not messagebox.askyesno(
                "删除文件",
                "确定要删除该录制文件吗？此操作不可恢复。\n\n%s" % p):
            return
        try:
            if os.path.isfile(p):
                os.remove(p)
        except Exception as e:
            messagebox.showerror("删除失败", str(e))
            return
        self.records = [x for x in self.records if x is not r]
        self.store.save_records(self.records)
        self._refresh_records_tree()
        self._set_status("文件已删除")

    # ------------------------------------------------------------ 关闭
    def _on_close(self):
        if self.recorder.is_active:
            if not messagebox.askyesno("正在录制",
                                       "当前正在录制，退出将停止并保存文件。是否继续退出？"):
                return
            self._stopping = True
            self.recorder.stop()
        try:
            self.floating.hide()
        except Exception:
            pass
        try:
            self.hotkeys.close()
        except Exception:
            pass
        try:
            self.frame.destroy()
        except Exception:
            pass
        self._save_settings()
        self.root.destroy()


def main():
    E.enable_dpi_awareness()
    if already_running():
        # 已经开着一个：把它还原到前台，自己直接退出（不弹第二个窗口）
        hwnd = find_main_window()
        for _ in range(25):                  # 对方可能还在初始化，稍等再找
            if hwnd:
                break
            time.sleep(0.12)
            hwnd = find_main_window()
        if hwnd:
            activate_window(hwnd)
        return
    root = tk.Tk()
    try:
        hdc = E.user32.GetDC(0)
        dpi = E.gdi32.GetDeviceCaps(hdc, 88) or 96
        E.user32.ReleaseDC(0, hdc)
        root.tk.call("tk", "scaling", max(1.0, dpi / 72.0))
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
