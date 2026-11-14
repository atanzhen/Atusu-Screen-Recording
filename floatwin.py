# -*- coding: utf-8 -*-
"""
ATUSU 录屏 - 悬浮控件的原生窗口层
=========================================
录制中的圆形操作控件必须做到「一开始就出现、只有结束才消失」，
因此它绝不能依附于 Tk 窗口：主界面被最小化 / 隐藏 / 重绘时，
Tk 的 Toplevel 会被系统连带处理，这正是「闪一下就没了」的根源。

本模块用 ``CreateWindowExW`` 直接建一个与主界面毫无父子关系的弹窗：

* ``WS_EX_TOPMOST``     始终置顶（并周期性复位，防止被别的置顶窗口压住）
* ``WS_EX_TOOLWINDOW``  不出现在任务栏与 Alt+Tab 列表
* ``WS_EX_NOACTIVATE``  点击时不激活自身，不打断正在被录制的软件
* ``WS_EX_LAYERED`` + ``UpdateLayeredWindow`` 逐像素半透明

窗口使用的是系统内置的 ``STATIC`` 窗口类——**不注册任何 Python 窗口回调**。
ctypes 的 WNDPROC 回调会在录制线程（ctypes 释放 GIL）与主线程之间来回
切换 GIL 状态，实测会偶发 `PyEval_RestoreThread` 致命错误；改用轮询方式
读取光标位置与鼠标左键状态，既没有回调也没有 GIL 风险，逻辑还更简单。

Windows 7 / 8 / 10 / 11 全部支持这套接口。
"""

import ctypes
from ctypes import wintypes

from PIL import Image, ImageChops

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# ---------------------------------------------------------------- 常量

WS_POPUP = 0x80000000
SS_LEFT = 0x00000000
WS_EX_LAYERED = 0x00080000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000

SW_SHOWNOACTIVATE = 4
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
HWND_TOPMOST = -1

ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01

VK_LBUTTON = 0x01
CLASS_NAME = "STATIC"          # 系统内置类，无需自己注册窗口过程


# ---------------------------------------------------------------- 结构体

class _BmiHeader(ctypes.Structure):
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


class _BlendFunction(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte),
                ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte),
                ("AlphaFormat", ctypes.c_ubyte)]


# ---------------------------------------------------------------- API 原型

user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR,
                                   wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int,
                                   wintypes.HWND, wintypes.HMENU,
                                   wintypes.HINSTANCE, ctypes.c_void_p]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND,
                                ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.SetWindowPos.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = [wintypes.HWND,
                                 ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.UpdateLayeredWindow.argtypes = [
    wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
    ctypes.POINTER(wintypes.SIZE), wintypes.HDC,
    ctypes.POINTER(wintypes.POINT), wintypes.DWORD, ctypes.c_void_p,
    wintypes.DWORD]
user32.UpdateLayeredWindow.restype = wintypes.BOOL

kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE

gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p,
                                   wintypes.UINT,
                                   ctypes.POINTER(ctypes.c_void_p),
                                   wintypes.HANDLE, wintypes.DWORD]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL


def _premultiplied_bgra(im):
    """DWM 要求 AC_SRC_ALPHA 的位图为预乘格式，内存序 B,G,R,A。"""
    r, g, b, a = im.split()
    r = ImageChops.multiply(r, a)
    g = ImageChops.multiply(g, a)
    b = ImageChops.multiply(b, a)
    return Image.merge("RGBA", (r, g, b, a)).tobytes("raw", "BGRA")


class LayeredFloat(object):
    """一块独立、置顶、逐像素透明的画布窗口。

    输入（点击 / 悬停 / 拖动）由调用方用 ``cursor()`` + ``left_down()``
    轮询判断，窗口本身不参与消息处理。
    """

    def __init__(self):
        self.hwnd = 0

    # ------------------------------------------------------------ 生命周期
    def create(self, w, h, x, y):
        if self.hwnd and user32.IsWindow(self.hwnd):
            return self.hwnd
        try:
            hwnd = user32.CreateWindowExW(
                WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW |
                WS_EX_NOACTIVATE,
                CLASS_NAME, "", WS_POPUP | SS_LEFT, int(x), int(y),
                max(1, int(w)), max(1, int(h)),
                None, None, kernel32.GetModuleHandleW(None), None)
        except Exception:
            return 0
        if not hwnd:
            return 0
        self.hwnd = int(hwnd)
        return self.hwnd

    def destroy(self):
        hwnd, self.hwnd = self.hwnd, 0
        if hwnd:
            try:
                user32.DestroyWindow(hwnd)
            except Exception:
                pass

    def alive(self):
        return bool(self.hwnd and user32.IsWindow(self.hwnd))

    def visible(self):
        return bool(self.hwnd and user32.IsWindowVisible(self.hwnd))

    def rect(self):
        """窗口矩形 (left, top, right, bottom)，失败返回 None。"""
        if not self.alive():
            return None
        try:
            r = wintypes.RECT()
            if user32.GetWindowRect(self.hwnd, ctypes.byref(r)):
                return (int(r.left), int(r.top), int(r.right), int(r.bottom))
        except Exception:
            pass
        return None

    def show(self):
        """显示且不抢焦点。"""
        if not self.alive():
            return False
        try:
            user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
            user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE |
                                SWP_SHOWWINDOW)
            return True
        except Exception:
            return False

    def keep_topmost(self):
        """周期性复位置顶，避免被后来出现的置顶窗口压住。"""
        if not self.alive():
            return False
        try:
            user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE |
                                SWP_SHOWWINDOW)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------ 绘制
    def paint(self, im, x, y):
        """把 RGBA 图推到屏幕 (x, y)（物理像素），同时设定窗口位置与大小。"""
        if not self.alive():
            return False
        try:
            w, h = im.size
            if w < 1 or h < 1:
                return False
            data = _premultiplied_bgra(im)
            hdc_screen = user32.GetDC(None)
            if not hdc_screen:
                return False
            hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
            if not hdc_mem:
                user32.ReleaseDC(None, hdc_screen)
                return False
            bmi = _BmiHeader(40, w, -h, 1, 32, 0, w * h * 4, 0, 0, 0, 0)
            bits = ctypes.c_void_p()
            hbmp = gdi32.CreateDIBSection(hdc_mem, ctypes.byref(bmi), 0,
                                          ctypes.byref(bits), None, 0)
            if not hbmp:
                gdi32.DeleteDC(hdc_mem)
                user32.ReleaseDC(None, hdc_screen)
                return False
            ctypes.memmove(bits, data, len(data))
            gdi32.SelectObject(hdc_mem, hbmp)
            size = wintypes.SIZE(int(w), int(h))
            src = wintypes.POINT(0, 0)
            dst = wintypes.POINT(int(x), int(y))
            blend = _BlendFunction(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
            ok = user32.UpdateLayeredWindow(
                self.hwnd, hdc_screen, ctypes.byref(dst), ctypes.byref(size),
                hdc_mem, ctypes.byref(src), 0, ctypes.byref(blend), ULW_ALPHA)
            gdi32.DeleteObject(hbmp)
            gdi32.DeleteDC(hdc_mem)
            user32.ReleaseDC(None, hdc_screen)
            return bool(ok)
        except Exception:
            return False

    # ------------------------------------------------------------ 输入轮询
    @staticmethod
    def cursor():
        """光标屏幕坐标（物理像素）。"""
        try:
            pt = wintypes.POINT()
            if user32.GetCursorPos(ctypes.byref(pt)):
                return (int(pt.x), int(pt.y))
        except Exception:
            pass
        return None

    @staticmethod
    def left_down():
        """鼠标左键当前是否按下。"""
        try:
            return bool(user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)
        except Exception:
            return False
