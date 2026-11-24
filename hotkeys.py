# -*- coding: utf-8 -*-
"""全局热键：程序切到后台 / 最小化后仍然监听 F9（开始 / 结束）与 F10（暂停 / 继续）。

实现方式
--------
1. 主方案 ``RegisterHotKey``：把 F9 / F10 注册成**系统级**热键，然后在一个独立
   线程里用 ``GetMessageW`` 收 ``WM_HOTKEY``。WM_HOTKEY 只会投递给「注册它的
   线程」，所以注册与取消息必须放在同一个线程；主线程（Tk）只从队列里取结果，
   不参与任何 Win32 消息处理，也就不会出现 WNDPROC 回调、GIL 来回切换的问题。

2. 兜底方案 ``GetAsyncKeyState``：如果热键已被别的程序占用（或老系统不支持
   ``MOD_NOREPEAT``），注册会失败，此时改由 Tk 主线程定时轮询按键状态并做
   按键沿检测。轮询同样不受窗口焦点影响，仍然满足「后台可用」。

3. ``MOD_NOREPEAT``：按住 F9 / F10 不放时系统只投递一次，避免连续开始 / 结束。

回调 ``callback(action)`` 始终在 Tk 主线程里执行，调用方可以直接操作界面与
录制器，不需要再加锁。
"""

import ctypes
import queue
import threading
from ctypes import wintypes

# ---------------------------------------------------------------- Win32 常量

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_NOREPEAT = 0x4000
VK_F9 = 0x78
VK_F10 = 0x79

ACTION_START_STOP = "start_stop"
ACTION_PAUSE_RESUME = "pause_resume"

# (热键 id, 虚拟键码, 名字, 动作)
DEFAULT_BINDINGS = (
    (1, VK_F9, "F9", ACTION_START_STOP),
    (2, VK_F10, "F10", ACTION_PAUSE_RESUME),
)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int,
                                  wintypes.UINT, wintypes.UINT]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.UnregisterHotKey.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                               wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = ctypes.c_int
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM]
user32.PostThreadMessageW.restype = wintypes.BOOL
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
kernel32.GetCurrentThreadId.argtypes = []
kernel32.GetCurrentThreadId.restype = wintypes.DWORD


def _vk_down(vk):
    """按键当前是否处于按下状态（含本进程无焦点时）。"""
    try:
        return bool(user32.GetAsyncKeyState(int(vk)) & 0x8000)
    except Exception:
        return False


class GlobalHotkeys(object):
    """把 F9 / F10 变成全局热键，并异步把动作送回 Tk 主线程。

    用法::

        hot = GlobalHotkeys(root, on_action).start()
        ...
        hot.close()
    """

    POLL_MS = 30          # 兜底轮询间隔：足够跟手，又几乎不占 CPU

    def __init__(self, root, callback, bindings=None):
        self.root = root
        self.callback = callback
        self.bindings = tuple(bindings or DEFAULT_BINDINGS)

        self.registered = []      # 注册成功的 [(名字, 动作)]
        self.fallback = []        # 注册失败、改用轮询的 [(vk, 名字, 动作)]

        self._queue = queue.Queue()
        self._thread = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._closed = False
        self._poll_job = None
        self._previous = {}       # 轮询用的上一次按键状态

    # ------------------------------------------------------------ 生命周期
    def start(self):
        """启动监听。返回 self，方便链式调用。"""
        if self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._run, name="atusu-hotkeys",
                                        daemon=True)
        self._thread.start()
        # 等线程把注册结果写出来，避免主线程过早认为已生效
        self._ready.wait(2.0)
        if not self._ready.is_set():
            # 极端情况（线程没能启动）：全部按键交给轮询兜底
            self.fallback = [(vk, name, action)
                             for _hid, vk, name, action in self.bindings]
        for vk, _name, _action in self.fallback:
            self._previous.setdefault(vk, False)
        self._schedule_poll()
        return self

    def close(self):
        """注销热键并停掉线程（退出程序前调用）。"""
        if self._closed:
            return
        self._closed = True
        if self._poll_job is not None:
            try:
                self.root.after_cancel(self._poll_job)
            except Exception:
                pass
            self._poll_job = None
        if self._thread_id:
            try:
                user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            except Exception:
                pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(0.5)

    # ------------------------------------------------------------ 注册线程
    def _run(self):
        registered = []
        try:
            self._thread_id = int(kernel32.GetCurrentThreadId())
            for hid, vk, name, action in self.bindings:
                # 注册到本线程的队列（hWnd=None），这样 WM_HOTKEY 一定由下面
                # 的 GetMessageW 收到，不会被 Tk 的消息循环截走。
                if user32.RegisterHotKey(None, hid, MOD_NOREPEAT, vk):
                    registered.append((hid, vk, name, action))
                else:
                    self.fallback.append((vk, name, action))
                    self._previous[vk] = False
            self.registered = [(name, action)
                               for _hid, _vk, name, action in registered]
        finally:
            self._ready.set()

        if not registered or self._closed:
            self._unregister(registered)
            return

        msg = wintypes.MSG()
        failed = False
        while not self._closed:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret <= 0:                 # 0 = WM_QUIT，-1 = 出错
                failed = ret < 0
                break
            if msg.message == WM_HOTKEY:
                hid = int(msg.wParam)
                for rhid, _vk, _name, action in registered:
                    if rhid == hid:
                        self._queue.put(action)
                        break
        self._unregister(registered)
        # 消息循环意外退出：转成轮询，热键不会就此失效
        if failed and not self._closed:
            for _hid, vk, name, action in registered:
                self.fallback.append((vk, name, action))
                self._previous[vk] = False

    @staticmethod
    def _unregister(registered):
        for hid, _vk, _name, _action in registered:
            try:
                user32.UnregisterHotKey(None, hid)
            except Exception:
                pass

    # ------------------------------------------------------------ 主线程轮询
    def _schedule_poll(self):
        if self._closed:
            return
        try:
            self._poll_job = self.root.after(self.POLL_MS, self._poll)
        except Exception:
            self._poll_job = None

    def _poll(self):
        self._poll_job = None
        if self._closed:
            return
        try:
            self._poll_fallback()
            while True:
                try:
                    action = self._queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    self.callback(action)
                except Exception:
                    pass
        finally:
            self._schedule_poll()

    def _poll_fallback(self):
        """只轮询注册失败的那些键，避免和 WM_HOTKEY 重复触发。"""
        for vk, _name, action in list(self.fallback):
            down = _vk_down(vk)
            if down and not self._previous.get(vk, False):
                self._queue.put(action)
            self._previous[vk] = down
