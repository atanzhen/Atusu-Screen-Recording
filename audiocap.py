# -*- coding: utf-8 -*-
"""系统声音录制（WASAPI Loopback）。

录屏时把「扬声器正在播放的声音」录下来，不需要虚拟声卡、不需要安装驱动。
实现要点：

* 通过 COM 调用 WASAPI：``IMMDeviceEnumerator`` → 默认播放设备 →
  ``IAudioClient`` → ``IAudioCaptureClient``，并打开
  ``AUDCLNT_STREAMFLAGS_LOOPBACK`` 环回流。
* 使用设备自己的混音格式（``GetMixFormat``）打开环回流，WAV 头也写入设备
  实际的采样率。不要再强行改成 48 kHz：某些设备实际以 44.1 kHz 输出，
  若仍把 WAV 标成 48 kHz，声音就会快约 8.8%，越录越不同步。
* 采集线程只做「取数据 → 写 WAV」，暂停时继续排空设备缓冲但不写入，
  因此 WAV 的时长与视频的有效录制时长一致。
* 录制结束后由 ``engine`` 用 ffmpeg 把 WAV 与视频合并。

Windows 7+ 均支持 WASAPI 环回；需要系统存在可用的播放设备。
"""

import array
import ctypes
import os
import tempfile
import threading
import time
import wave
from ctypes import wintypes

TARGET_RATE = 48000
TARGET_CHANNELS = 2
TARGET_BLOCK = 4                      # 2 声道 × 16 bit

# ---------------------------------------------------------------- Win32 常量

_CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
_IID_IMMDeviceEnumerator = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
_IID_IAudioClient = "{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}"
_IID_IAudioCaptureClient = "{C8ADBD64-E71E-48a0-A4DE-185C395CD317}"

_CLSCTX_ALL = 23
_E_RENDER = 0
_E_CONSOLE = 0
_E_MULTIMEDIA = 1
_E_COMMUNICATIONS = 2

_SHARED = 0
_LOOPBACK = 0x00020000
_BUFFER_DURATION = 2_000_000         # 200 ms（100ns 单位）

_WAVE_FORMAT_PCM = 1
_WAVE_FORMAT_IEEE_FLOAT = 3
_WAVE_FORMAT_EXTENSIBLE = 0xFFFE

_AUDCLNT_BUFFERFLAGS_SILENT = 0x2

# 常见 HRESULT，用来给出能看懂的错误提示
_ERROR_TEXT = {
    0x80070005: "没有权限访问音频设备",
    0x80070490: "未找到默认播放设备，请确认扬声器 / 耳机已连接并启用",
    0x88890004: "音频设备已失效（可能被拔出或切换）",
    0x88890008: "音频设备不支持该采样格式",
    0x8889000A: "音频设备正被其他程序独占",
    0x88890017: "音频格式不受支持",
}

# ---------------------------------------------------------------- ctypes 结构

ole32 = ctypes.WinDLL("ole32", use_last_error=True)

ole32.CLSIDFromString.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p]
ole32.CLSIDFromString.restype = ctypes.HRESULT
ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
ole32.CoInitializeEx.restype = ctypes.HRESULT
ole32.CoUninitialize.argtypes = []
ole32.CoCreateInstance.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                   wintypes.DWORD, ctypes.c_void_p,
                                   ctypes.POINTER(ctypes.c_void_p)]
ole32.CoCreateInstance.restype = ctypes.HRESULT
ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
ole32.CoTaskMemFree.restype = None

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.QueryPerformanceCounter.argtypes = [ctypes.POINTER(ctypes.c_longlong)]
kernel32.QueryPerformanceCounter.restype = wintypes.BOOL
kernel32.QueryPerformanceFrequency.argtypes = [ctypes.POINTER(ctypes.c_longlong)]
kernel32.QueryPerformanceFrequency.restype = wintypes.BOOL


def _qpc_now():
    """当前 QPC 时间（秒）。包时间戳也是 QPC，但固定按 100ns 换算。"""
    counter = ctypes.c_longlong()
    freq = ctypes.c_longlong()
    try:
        if (kernel32.QueryPerformanceCounter(ctypes.byref(counter))
                and kernel32.QueryPerformanceFrequency(ctypes.byref(freq))
                and freq.value):
            return float(counter.value) / float(freq.value)
    except Exception:
        pass
    return 0.0


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]


class _WaveFormatEx(ctypes.Structure):
    # WAVEFORMATEX 在 Windows 头文件里是 1 字节对齐（18 字节），
    # 不能按 ctypes 默认对齐，否则 WAVEFORMATEXTENSIBLE 的 SubFormat 会错位。
    _pack_ = 1
    _fields_ = [("wFormatTag", wintypes.WORD),
                ("nChannels", wintypes.WORD),
                ("nSamplesPerSec", wintypes.DWORD),
                ("nAvgBytesPerSec", wintypes.DWORD),
                ("nBlockAlign", wintypes.WORD),
                ("wBitsPerSample", wintypes.WORD),
                ("cbSize", wintypes.WORD)]


class _WaveFormatExtensible(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("Format", _WaveFormatEx),
                ("wValidBitsPerSample", wintypes.WORD),
                ("dwChannelMask", wintypes.DWORD),
                ("SubFormat", _GUID)]


_VOIDPP = ctypes.POINTER(ctypes.c_void_p)


class AudioUnavailable(RuntimeError):
    """系统声音不可用（没有播放设备、格式不支持、启动失败等）。"""


def _guid(text):
    value = _GUID()
    if ole32.CLSIDFromString(ctypes.c_wchar_p(text),
                             ctypes.byref(value)) < 0:
        raise AudioUnavailable("无效的 COM GUID：%s" % text)
    return value


def _method(pointer, index, *argtypes):
    """取 COM vtable 里的一个方法（第 0~2 项是 IUnknown）。"""
    vtable = ctypes.cast(pointer, ctypes.POINTER(_VOIDPP))
    prototype = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)
    return prototype(vtable[0][index])


def _release(pointer):
    if pointer:
        try:
            _method(pointer, 2)(pointer)
        except Exception:
            pass


def _hr_text(hr):
    code = int(hr) & 0xFFFFFFFF
    return _ERROR_TEXT.get(code, "0x%08X" % code)


def _check(hr, what):
    if hr < 0:
        raise AudioUnavailable("%s（%s）" % (what, _hr_text(hr)))


def _activate_client(device):
    client = ctypes.c_void_p()
    hr = _method(device, 3, ctypes.POINTER(_GUID), wintypes.DWORD,
                 ctypes.c_void_p, _VOIDPP)(
        device, ctypes.byref(_guid(_IID_IAudioClient)), _CLSCTX_ALL,
        None, ctypes.byref(client))
    _check(hr, "无法打开系统声音客户端")
    return client


def _default_render_device(enumerator):
    device = ctypes.c_void_p()
    last = 0
    for role in (_E_CONSOLE, _E_MULTIMEDIA, _E_COMMUNICATIONS):
        hr = _method(enumerator, 4, ctypes.c_int, ctypes.c_int, _VOIDPP)(
            enumerator, _E_RENDER, role, ctypes.byref(device))
        if hr >= 0 and device:
            return device
        last = hr
    raise AudioUnavailable("未找到系统播放设备（%s）" % _hr_text(last))


def _parse_mix_format(pointer):
    """读取设备混音格式，返回 (kind, channels, rate, block, mask)。"""
    fmt = pointer.contents
    tag = int(fmt.wFormatTag)
    channels = int(fmt.nChannels)
    rate = int(fmt.nSamplesPerSec)
    block = int(fmt.nBlockAlign)
    bits = int(fmt.wBitsPerSample)
    mask = 0
    if tag == _WAVE_FORMAT_EXTENSIBLE and int(fmt.cbSize) >= 22:
        ext = ctypes.cast(pointer, ctypes.POINTER(_WaveFormatExtensible)).contents
        tag = int(ext.SubFormat.Data1)
        mask = int(ext.dwChannelMask)
    if channels <= 0 or rate <= 0 or block <= 0:
        raise AudioUnavailable("音频设备返回了无效的格式")
    if tag == _WAVE_FORMAT_IEEE_FLOAT and bits == 32:
        kind = "f32"
    elif tag == _WAVE_FORMAT_IEEE_FLOAT and bits == 64:
        kind = "f64"
    elif tag == _WAVE_FORMAT_PCM and bits == 16:
        kind = "s16"
    elif tag == _WAVE_FORMAT_PCM and bits == 24:
        kind = "s24"
    elif tag == _WAVE_FORMAT_PCM and bits == 32:
        kind = "s32"
    elif tag == _WAVE_FORMAT_PCM and bits == 8:
        kind = "u8"
    else:
        raise AudioUnavailable("不支持的音频格式（tag=%s, %d bit）" % (tag, bits))
    return kind, channels, rate, block, mask


class _StreamInfo(object):
    def __init__(self, kind, channels, rate, block, mask, direct):
        self.kind = kind
        self.channels = int(channels)
        self.rate = int(rate)
        self.block = int(block)
        self.mask = int(mask)
        self.direct = bool(direct)     # True：已经是双声道 16-bit PCM


def _open_session():
    """在当前线程打开默认播放设备的环回流。

    返回 (enumerator, device, client, capture, info)。调用方负责 Release。
    """
    enumerator = ctypes.c_void_p()
    hr = ole32.CoCreateInstance(
        ctypes.byref(_guid(_CLSID_MMDeviceEnumerator)), None, _CLSCTX_ALL,
        ctypes.byref(_guid(_IID_IMMDeviceEnumerator)),
        ctypes.byref(enumerator))
    _check(hr, "无法打开音频设备列表")

    device = client = capture = None
    try:
        device = _default_render_device(enumerator)
        client = _activate_client(device)

        # 一定要用设备自己的混音格式：共享模式环回流本来就会按这个格式
        # 交付数据。若强行请求 48 kHz 而设备实际是 44.1 kHz，某些声卡 /
        # 虚拟声卡会照旧交付 44.1 kHz 数据，但 WAV 头写成 48 kHz，声音
        # 就会快 8.8%，录制时间越长音画偏差越大。
        mix = ctypes.POINTER(_WaveFormatEx)()
        hr = _method(client, 8,
                     ctypes.POINTER(ctypes.POINTER(_WaveFormatEx)))(
            client, ctypes.byref(mix))
        _check(hr, "无法读取音频设备格式")
        try:
            kind, channels, rate, block, mask = _parse_mix_format(mix)
            hr = _method(client, 3, ctypes.c_int, wintypes.DWORD,
                         ctypes.c_longlong, ctypes.c_longlong,
                         ctypes.POINTER(_WaveFormatEx), ctypes.c_void_p)(
                client, _SHARED, _LOOPBACK, _BUFFER_DURATION, 0,
                mix, None)
            _check(hr, "无法打开系统声音环回流")
        finally:
            ole32.CoTaskMemFree(mix)
        info = _StreamInfo(kind, channels, rate, block, mask, False)

        capture = ctypes.c_void_p()
        hr = _method(client, 14, ctypes.POINTER(_GUID), _VOIDPP)(
            client, ctypes.byref(_guid(_IID_IAudioCaptureClient)),
            ctypes.byref(capture))
        _check(hr, "无法读取系统声音数据")
        return enumerator, device, client, capture, info
    except Exception:
        _release(capture)
        _release(client)
        _release(device)
        _release(enumerator)
        raise


# ---------------------------------------------------------------- 采样转换

def _clamp16(value):
    if value > 32767:
        return 32767
    if value < -32768:
        return -32768
    return int(value)


def _coefficients(channels, mask):
    """把设备的每个声道映射到左右声道；返回 [(left, right), ...]。"""
    if mask:
        bits = [b for b in range(32) if (mask & (1 << b))]
        if len(bits) == channels:
            table = {
                0: (1.0, 0.0),        # FL
                1: (0.0, 1.0),        # FR
                2: (0.7071, 0.7071),  # FC
                3: (0.5, 0.5),        # LFE
                4: (0.7071, 0.0),     # BL
                5: (0.0, 0.7071),     # BR
                6: (0.5, 0.0),        # FLC
                7: (0.0, 0.5),        # FRC
                8: (0.5, 0.5),        # BC
                9: (0.7071, 0.0),     # SL
                10: (0.0, 0.7071),    # SR
            }
            return [table.get(b, (0.0, 0.0)) for b in bits]
    if channels == 1:
        return [(1.0, 1.0)]
    if channels == 2:
        return [(1.0, 0.0), (0.0, 1.0)]
    if channels == 3:
        return [(1.0, 0.0), (0.0, 1.0), (0.7071, 0.7071)]
    if channels == 4:
        return [(1.0, 0.0), (0.0, 1.0), (0.7071, 0.0), (0.0, 0.7071)]
    if channels == 5:
        return [(1.0, 0.0), (0.0, 1.0), (0.7071, 0.7071),
                (0.7071, 0.0), (0.0, 0.7071)]
    if channels == 6:
        return [(1.0, 0.0), (0.0, 1.0), (0.7071, 0.7071), (0.5, 0.5),
                (0.7071, 0.0), (0.0, 0.7071)]
    # 7.1 / 更多：按 FL FR FC LFE BL BR SL SR 处理，多余声道忽略
    base = [(1.0, 0.0), (0.0, 1.0), (0.7071, 0.7071), (0.5, 0.5),
            (0.7071, 0.0), (0.0, 0.7071), (0.7071, 0.0), (0.0, 0.7071)]
    return (base + [(0.0, 0.0)] * channels)[:channels]


def _downmix(src, channels, mask, scale):
    """把 array('h'/'i'/'f') 的帧下混成双声道 16-bit PCM bytes。"""
    coeffs = _coefficients(channels, mask)
    out = array.array("h")
    append = out.append
    total = len(src)
    for base in range(0, total - channels + 1, channels):
        left = right = 0.0
        for c, (cl, cr) in enumerate(coeffs):
            value = src[base + c] * scale
            left += value * cl
            right += value * cr
        append(_clamp16(left))
        append(_clamp16(right))
    return out.tobytes()


def _stereo_float32(raw):
    src = array.array("f")
    src.frombytes(raw)
    out = array.array("h")
    append = out.append
    for i in range(0, len(src) - 1, 2):
        append(_clamp16(src[i] * 32767.0))
        append(_clamp16(src[i + 1] * 32767.0))
    return out.tobytes()


def _pcm24_to_s16(raw):
    src = array.array("h")
    append = src.append
    for i in range(0, len(raw) - 2, 3):
        value = raw[i] | (raw[i + 1] << 8) | (raw[i + 2] << 16)
        if value & 0x800000:
            value -= 0x1000000
        append(value >> 8)
    return src


def _to_stereo_pcm16(raw, info):
    """把设备格式的原始字节转换成双声道 16-bit PCM。"""
    if info.direct:
        return raw
    if info.kind == "f32":
        if info.channels == 2:
            return _stereo_float32(raw)
        src = array.array("f")
        src.frombytes(raw)
        return _downmix(src, info.channels, info.mask, 32767.0)
    if info.kind == "f64":
        src = array.array("d")
        src.frombytes(raw)
        return _downmix(src, info.channels, info.mask, 32767.0)
    if info.kind == "s16":
        if info.channels == 2:
            return raw
        src = array.array("h")
        src.frombytes(raw)
        return _downmix(src, info.channels, info.mask, 1.0)
    if info.kind == "s32":
        src = array.array("i")
        src.frombytes(raw)
        return _downmix(src, info.channels, info.mask, 1.0 / 65536.0)
    if info.kind == "s24":
        src = _pcm24_to_s16(raw)
        if info.channels == 2:
            return src.tobytes()
        return _downmix(src, info.channels, info.mask, 1.0)
    if info.kind == "u8":
        src = array.array("h")
        append = src.append
        for value in raw:
            append((value - 128) << 8)
        return _downmix(src, info.channels, info.mask, 1.0)
    raise AudioUnavailable("不支持的音频格式：%s" % info.kind)


# ---------------------------------------------------------------- 采集器

class SystemAudioCapture(object):
    """在后台线程里录制扬声器输出，写入一个双声道 16-bit WAV。"""

    IDLE_MARGIN = 0.5
    IDLE_STEP = 0.02

    def __init__(self, path):
        self.path = path
        self.rate = TARGET_RATE
        self.frames = 0
        self.error = ""
        self.started_at = 0.0
        self._thread = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._paused = False
        self._wav = None
        self._start_qpc = 0.0
        self._last_qpc_end = 0.0
        self._last_pos_end = -1

    # -- 控制 --------------------------------------------------------
    def start(self, timeout=5.0):
        if self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._run, name="atusu-audio",
                                        daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            self._stop.set()
            self._thread.join(1.0)
            raise AudioUnavailable("系统声音启动超时")
        if self.error:
            self._stop.set()
            self._thread.join(1.0)
            raise AudioUnavailable(self.error)
        return self

    def pause(self):
        self._paused = True
        # 暂停期间的时间不能补静音，继续时重新对齐时间轴。
        self._start_qpc = 0.0
        self._last_qpc_end = 0.0
        self._last_pos_end = -1

    def resume(self):
        self._paused = False

    def stop(self):
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(5.0)
        if thread is not None and thread.is_alive():
            self.error = self.error or "系统声音采集线程未能及时停止"

    @property
    def thread_alive(self):
        return bool(self._thread is not None and self._thread.is_alive())

    @property
    def duration(self):
        return float(self.frames) / float(max(1, self.rate))

    # -- 线程 --------------------------------------------------------
    def _run(self):
        enumerator = device = client = capture = None
        started = False
        com_ready = False
        info = None
        try:
            hr = ole32.CoInitializeEx(None, 0)
            if hr < 0:
                raise AudioUnavailable("无法初始化系统声音（%s）" % _hr_text(hr))
            com_ready = True

            enumerator, device, client, capture, info = _open_session()
            self.rate = info.rate
            self._wav = wave.open(self.path, "wb")
            self._wav.setnchannels(TARGET_CHANNELS)
            self._wav.setsampwidth(2)
            self._wav.setframerate(info.rate)

            _check(_method(client, 10)(client), "无法启动系统声音采集")
            started = True
            self.started_at = time.perf_counter()
            self._start_qpc = _qpc_now()
            self._ready.set()
            self._pump(capture, info)
            # 停止时把最后一段尚未补上的静音补齐（暂停时不能补）。
            if not self._paused:
                end = _qpc_now()
                start = self._last_qpc_end or self._start_qpc
                if end and start and end > start:
                    self._write_silence(round((end - start) * self.rate))
        except Exception as exc:
            self.error = str(exc) or exc.__class__.__name__
        finally:
            self._ready.set()
            if started and client:
                try:
                    _method(client, 11)(client)
                except Exception:
                    pass
            if self._wav is not None:
                try:
                    self._wav.close()
                except Exception:
                    pass
                self._wav = None
            _release(capture)
            _release(client)
            _release(device)
            _release(enumerator)
            if com_ready:
                try:
                    ole32.CoUninitialize()
                except Exception:
                    pass

    def _write_silence(self, frames):
        """向 WAV 写入指定帧数的双侧静音。"""
        frames = int(frames)
        if frames <= 0 or self._wav is None:
            return
        frames = min(frames, int(self.rate * 600))   # 单段最多补 10 分钟
        chunk = max(1, min(int(self.rate), frames))
        zeros = b"\x00" * (chunk * TARGET_BLOCK)
        while frames > 0:
            n = min(frames, chunk)
            self._wav.writeframesraw(zeros[:n * TARGET_BLOCK])
            self.frames += n
            frames -= n

    def _fill_timeline(self, qpc, pos, count):
        """按设备时间戳补上缺失的静音。

        某些声卡在系统没有播放声音时不会投递环回包；如果直接拼接，
        WAV 会少掉这些静音段，表现为声音越来越快、音画不同步。这里用
        ``GetBuffer`` 返回的 QPC 时间 / 设备位置把空档补成静音。
        """
        qpc_sec = qpc / 10000000.0 if qpc else 0.0
        if qpc_sec:
            if self._last_qpc_end:
                gap = qpc_sec - self._last_qpc_end
                if gap > 0:
                    self._write_silence(round(gap * self.rate))
            elif self._start_qpc and qpc_sec > self._start_qpc:
                self._write_silence(
                    round((qpc_sec - self._start_qpc) * self.rate))
            self._last_qpc_end = qpc_sec + count / float(self.rate)
            self._last_pos_end = pos + count if pos else -1
            return
        if pos:
            if self._last_pos_end >= 0 and pos > self._last_pos_end:
                self._write_silence(pos - self._last_pos_end)
            self._last_pos_end = pos + count

    def _fill_idle_silence(self):
        """设备长时间没有投递数据时，主动按时间补静音。

        WASAPI 环回在系统完全静音时可能完全不产生 packet；只在收到下一个
        packet 时补空档还不够，因为最后一段静音可能永远不会补上。这里在
        轮询空闲时按 QPC 时间把已经过去的空档写成静音，并留出 500ms 余量
        防止和正常延迟到达的 packet 重叠。
        """
        if self._paused:
            return
        now = _qpc_now()
        if not now:
            return
        end = self._last_qpc_end or self._start_qpc
        if not end:
            return
        gap = now - end - self.IDLE_MARGIN
        if gap >= self.IDLE_STEP:
            frames = int(round(gap * self.rate))
            if frames > 0:
                self._write_silence(frames)
                self._last_qpc_end = end + frames / float(self.rate)

    def _pump(self, capture, info):
        get_next = _method(capture, 5, ctypes.POINTER(wintypes.UINT))
        get_buffer = _method(capture, 3,
                             ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
                             ctypes.POINTER(wintypes.UINT),
                             ctypes.POINTER(wintypes.DWORD),
                             ctypes.POINTER(ctypes.c_ulonglong),
                             ctypes.POINTER(ctypes.c_ulonglong))
        release = _method(capture, 4, wintypes.UINT)

        while not self._stop.is_set():
            waiting = wintypes.UINT(0)
            hr = get_next(capture, ctypes.byref(waiting))
            if hr < 0:
                raise AudioUnavailable("读取系统声音失败（%s）" % _hr_text(hr))
            if waiting.value == 0:
                self._fill_idle_silence()
                self._stop.wait(0.01)
                continue
            while waiting.value > 0 and not self._stop.is_set():
                data = ctypes.POINTER(ctypes.c_ubyte)()
                frames = wintypes.UINT(0)
                flags = wintypes.DWORD(0)
                pos = ctypes.c_ulonglong(0)
                qpc = ctypes.c_ulonglong(0)
                hr = get_buffer(capture, ctypes.byref(data),
                                ctypes.byref(frames), ctypes.byref(flags),
                                ctypes.byref(pos), ctypes.byref(qpc))
                if hr < 0:
                    raise AudioUnavailable("读取系统声音失败（%s）" % _hr_text(hr))
                try:
                    count = int(frames.value)
                    if count and not self._paused:
                        self._fill_timeline(int(qpc.value), int(pos.value),
                                            count)
                        if int(flags.value) & _AUDCLNT_BUFFERFLAGS_SILENT:
                            pcm = b"\x00" * (count * TARGET_BLOCK)
                        else:
                            raw = ctypes.string_at(data, count * info.block)
                            pcm = _to_stereo_pcm16(raw, info)
                        if pcm:
                            self._wav.writeframesraw(pcm)
                            self.frames += len(pcm) // TARGET_BLOCK
                finally:
                    release(capture, frames.value)
                hr = get_next(capture, ctypes.byref(waiting))
                if hr < 0:
                    raise AudioUnavailable("读取系统声音失败（%s）" % _hr_text(hr))


# ---------------------------------------------------------------- 预检

def probe(timeout=5.0):
    """快速试开一次环回流，确认系统声音当前是否可用。

    返回 ``(ok, message)``；不抛异常，方便界面在开始录制前提示。
    """
    fd, path = tempfile.mkstemp(prefix="atusu-audio-", suffix=".wav")
    os.close(fd)
    capture = None
    try:
        capture = SystemAudioCapture(path)
        capture.start(timeout=timeout)
        capture.stop()
        if capture.thread_alive:
            return False, "系统声音采集线程未能及时停止"
        if capture.error:
            return False, capture.error
        return True, ""
    except Exception as exc:
        return False, str(exc) or exc.__class__.__name__
    finally:
        if capture is not None:
            try:
                capture.stop()
            except Exception:
                pass
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass
