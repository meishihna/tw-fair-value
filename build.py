#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
打包成「免裝 Python」的單一執行檔。

用法：
    pip install pyinstaller
    python build.py

完成後執行檔在 dist/ 底下（Windows: dist/tw-fair-value.exe）。
雙擊即會啟動本機伺服器並自動開啟瀏覽器，與直接跑 .py 相同。
"""
import PyInstaller.__main__

PyInstaller.__main__.run([
    "台股合理價估算器.py",
    "--onefile",              # 打成單一檔
    "--name", "tw-fair-value",
    "--clean",
    "--noconfirm",
    # 保留主控台視窗：使用者需看到網址、並用 Ctrl+C 結束（不要用 --noconsole）
])
