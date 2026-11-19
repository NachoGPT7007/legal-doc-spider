# -*- coding: utf-8 -*-
"""
===============================================================================
 启动.pyw —— 备用启动方式（双击即可，不会弹出黑色命令行窗口）
===============================================================================
 【什么时候用它】
   正常情况下请双击「启动.bat」。这个 .pyw 是备用方案：
   .pyw 文件由 pythonw.exe 执行，它**根本不会创建命令行窗口**，
   所以也就彻底不会出现「黑窗口里中文乱码」这类问题。

 【.pyw 和 .bat 的区别】
   启动.bat   → 会开一个黑色命令行窗口，好处是程序出错时你能看到报错信息
   启动.pyw   → 不弹黑窗口，界面直接出来；如果出错会用弹窗告诉你

 【这个文件做了什么】
   1. 把「本文件所在的文件夹」加进模块搜索路径 —— 保证能找到 spider 包
   2. 把工作目录也切过去 —— 双击 .pyw 时 Windows 给的工作目录可能是别的地方
   3. 启动图形界面
   4. 万一启动失败，用弹窗把错误显示出来（因为 .pyw 没有控制台，
      如果不弹窗，出错时你会以为「双击了没反应」）
===============================================================================
"""

import os
import sys

# __file__ 是「本文件自己的路径」。取它的目录，就是项目根目录。
# 无论你把这个文件夹复制到哪里、从哪个盘双击，这两行都能定位正确的位置。
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
os.chdir(BASE_DIR)


def main():
    """
    启动界面。用 try/except 包住全过程 ——
    因为 .pyw 没有控制台窗口，报错信息会无处显示，所以必须弹窗告诉用户。
    """
    try:
        import gui
        gui.main()
    except Exception:
        # traceback 能把「哪一行、什么错误」完整记录下来
        import traceback
        detail = traceback.format_exc()
        try:
            # 用 tkinter 的弹窗显示错误（tkinter 是 Python 自带的，一定有）
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()          # 把空的主窗口藏起来，只留弹窗
            messagebox.showerror(
                "启动失败",
                "程序启动时出错了。请把下面的内容截图发给我：\n\n" + detail[-1500:],
            )
            root.destroy()
        except Exception:
            # 连弹窗都失败（极端情况），就写一个错误日志文件，让用户能找到
            try:
                with open(os.path.join(BASE_DIR, "启动错误日志.txt"), "w",
                          encoding="utf-8") as handle:
                    handle.write(detail)
            except Exception:
                pass


if __name__ == "__main__":
    main()
