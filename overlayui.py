# -*- coding: utf-8 -*-
"""
ATUSU 录屏 - 悬浮控件渲染层
=========================================
Tk 自带的 Canvas 不支持抗锯齿，64px 的圆形在屏幕上会看到明显的阶梯边。
本模块改用 Pillow 以 3 倍超采样绘制，返回一张带 alpha 通道的 RGBA 图，
由 floatwin 模块通过 ``UpdateLayeredWindow`` 逐像素推到屏幕，
圆形、胶囊与文字都保持平滑。

本模块只负责「画」，不碰窗口；窗口与消息在 ``floatwin`` 里。
"""

import os

from PIL import Image, ImageDraw, ImageFont

# 超采样倍数：3 倍足以让 1px 的圆弧过渡自然
SS = 3

# ---------------------------------------------------------------- 字体

_FONT_CACHE = {}


def cn_font(px, bold=True):
    """按像素字号取中文字体（雅黑优先），找不到时退回默认位图字体。"""
    px = max(6, int(px))
    key = (px, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    win = os.environ.get("WINDIR") or r"C:\Windows"
    names = ("msyhbd.ttc", "msyh.ttc") if bold else ("msyh.ttc", "msyhbd.ttc")
    font = None
    for name in names:
        path = os.path.join(win, "Fonts", name)
        if os.path.isfile(path):
            try:
                font = ImageFont.truetype(path, px)
                break
            except Exception:
                font = None
    if font is None:
        for name in ("simhei.ttf", "segoeuib.ttf", "arialbd.ttf"):
            try:
                font = ImageFont.truetype(name, px)
                break
            except Exception:
                font = None
    if font is None:
        font = ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font


# ---------------------------------------------------------------- 配色

COLORS = {
    "rec": ("#F0575C", "#D6383E"),
    "paused": ("#F5A93B", "#D8890F"),
}
TEXT_PAUSE = "#A8560A"
TEXT_RESUME = "#12783A"
TEXT_STOP = "#B4231F"


# ---------------------------------------------------------------- 几何

def control_metrics(width, height):
    """按钮与圆形的横向区间（与命中测试共用同一套几何）。"""
    left = 10.0
    gap = 8.0
    area_right = width - height - 8.0
    avail = area_right - left - gap
    if avail < 44:                 # 展开动画初期空间不足，不排按钮
        return (0.0, 0.0), (0.0, 0.0), (width - height, width)
    bw = avail / 2.0
    btn0 = (left, left + bw)
    btn1 = (left + bw + gap, area_right)
    circle = (width - height, width)
    return btn0, btn1, circle


def _lerp(c0, c1, t):
    return (int(round(c0[0] + (c1[0] - c0[0]) * t)),
            int(round(c0[1] + (c1[1] - c0[1]) * t)),
            int(round(c0[2] + (c1[2] - c0[2]) * t)))


def _hex2rgb(s):
    s = s.lstrip("#")
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


def _draw_icon(d, kind, cx, cy, size, color, ss):
    """在 (cx, cy) 处画一个 size 大小的操作图标（ss 为超采样倍数）。"""
    if kind == "stop":
        s = size * 0.52
        d.rounded_rectangle([cx - s, cy - s, cx + s, cy + s],
                            radius=s * 0.42, fill=color)
    elif kind == "play":
        s = size * 0.52
        d.polygon([(cx - s * 0.72, cy - s), (cx - s * 0.72, cy + s),
                   (cx + s * 0.95, cy)], fill=color)
    else:
        w = size * 0.26
        h = size * 0.92
        d.rounded_rectangle([cx - w * 1.45, cy - h / 2, cx - w * 0.45,
                             cy + h / 2], radius=w * 0.42, fill=color)
        d.rounded_rectangle([cx + w * 0.45, cy - h / 2, cx + w * 1.45,
                             cy + h / 2], radius=w * 0.42, fill=color)


# ---------------------------------------------------------------- 渲染

def render_control(width, height, kind="rec", text="00:00", hover=-1,
                   ss=SS):
    """渲染悬浮控件：右侧圆形计时 + 左侧（展开时）两个操作按钮。

    kind  : 'rec' 录制中（红）/ 'paused' 已暂停（琥珀）
    hover : -1 无 / 0 第一个按钮 / 1 第二个按钮
    返回：RGBA 图像，尺寸 (width, height)
    """
    W, H = int(width), int(height)
    w, h = W * ss, H * ss
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    top_c, bot_c = [_hex2rgb(c) for c in COLORS.get(kind, COLORS["rec"])]

    # 渐变底色 + 胶囊形遮罩（左端为圆弧，右端与圆形重合）
    grad = Image.new("RGBA", (1, h))
    gd = ImageDraw.Draw(grad)
    for yy in range(h):
        gd.point((0, yy), fill=_lerp(top_c, bot_c, yy / float(max(1, h - 1)))
                 + (255,))
    grad = grad.resize((w, h))
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1],
                                           radius=h // 2, fill=255)
    img.paste(grad, (0, 0), mask)

    # 高光描边（1px，让控件在深色画面上也有轮廓）
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([ss, ss, w - 1 - ss, h - 1 - ss],
                        radius=(h - 2 * ss) // 2,
                        outline=(255, 255, 255, 64), width=ss)

    # ---- 右侧圆形：白色细环 + 计时
    r = H / 2.0
    cx, cy = w - r * ss, h / 2.0
    ring_r = (r - 5.5) * ss
    d.ellipse([cx - ring_r, cy - ring_r, cx + ring_r, cy + ring_r],
              outline=(255, 255, 255, 238), width=max(2, int(1.9 * ss)))
    tf = cn_font(int(H * 0.215) * ss, bold=True)
    d.text((cx, cy - ss), text, font=tf, fill=(255, 255, 255, 255),
           anchor="mm")

    if W <= H + 60:
        return img.resize((W, H), Image.LANCZOS)

    # ---- 左侧两个操作按钮
    if kind == "paused":
        items = [("继续", "play", TEXT_RESUME), ("结束", "stop", TEXT_STOP)]
    else:
        items = [("暂停", "pause", TEXT_PAUSE), ("结束", "stop", TEXT_STOP)]
    btn0, btn1, _circle = control_metrics(W, H)
    boxes = (btn0, btn1)
    bh = H - 18
    by0 = (H - bh) / 2.0

    bf = cn_font(int(H * 0.22) * ss, bold=True)
    for i, (label, icon, color) in enumerate(items):
        x0, x1 = boxes[i]
        box = [x0 * ss, by0 * ss, x1 * ss, (by0 + bh) * ss]
        radius = (bh / 2.0) * ss
        if hover == i:
            # 悬停：纯白 + 底色描边，反馈更明确
            edge = _lerp(bot_c, (0, 0, 0), 0.22)
            d.rounded_rectangle(box, radius=radius, fill=(255, 255, 255, 255),
                                outline=edge + (255,),
                                width=max(2, int(1.5 * ss)))
        else:
            d.rounded_rectangle(box, radius=radius, fill=(255, 255, 255, 232))
        col = _hex2rgb(color)
        tw = d.textlength(label, font=bf)
        icon_size = 10 * ss
        gap = 5 * ss
        total = icon_size + gap + tw
        gx = (x0 + x1) / 2.0 * ss - total / 2.0
        _draw_icon(d, icon, gx + icon_size / 2.0, (H / 2.0) * ss,
                   icon_size, col + (255,), ss)
        d.text((gx + icon_size + gap, (H / 2.0) * ss - ss), label, font=bf,
               fill=col + (255,), anchor="lm")
    return img.resize((W, H), Image.LANCZOS)
