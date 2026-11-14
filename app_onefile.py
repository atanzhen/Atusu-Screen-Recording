# -*- coding: utf-8 -*-
"""ATUSU 录屏 - 单文件版入口。

单文件（onefile）打包时以本文件为入口：先置单文件标记，
录屏文件默认保存到 D 盘（无 D 盘则 E 盘…，只有 C 盘则 C 盘）。
"""

import os
import sys

os.environ["ATUSU_SINGLEFILE"] = "1"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app  # noqa: E402

if __name__ == "__main__":
    app.main()
