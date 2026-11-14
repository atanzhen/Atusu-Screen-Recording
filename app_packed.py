# -*- coding: utf-8 -*-
"""ATUSU 录屏 - 整合版入口（单文件 + 内置 ffmpeg）。

把 ffmpeg.exe 一起打包进单文件，因此无需任何外部程序即可输出 MP4；
录屏文件默认保存到 D 盘（无 D 盘则 E 盘…，只有 C 盘则 C 盘）。
"""

import os
import sys

os.environ["ATUSU_SINGLEFILE"] = "1"
os.environ["ATUSU_PACKED_FFMPEG"] = "1"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app  # noqa: E402

if __name__ == "__main__":
    app.main()
