# -*- coding: utf-8 -*-
"""
===============================================================================
 gui.py —— 图形界面（你双击「启动.bat」后看到的那个窗口）
===============================================================================
 【这个文件负责什么】
   给你一个能点的窗口：填网址 → 点开始 → 看进度 → 点开结果文件夹。
   所有抓取逻辑都不在这里，这里只负责「显示」和「把你的操作转达给爬虫」。

 【本文件的分区】
   第 1 区  配置读取            —— 把 config.ini 里的参数读进来
   第 2 区  主窗口类 · 界面搭建 —— 画控件（输入框、按钮、进度条、日志区）
   第 3 区  事件处理            —— 点「开始」「停止」之后发生什么
   第 4 区  后台线程与消息队列  —— 让界面在抓取时保持不卡死的关键机制
   第 5 区  程序入口

 【一个必须理解的技术点：为什么抓取要放在「后台线程」】
   tkinter 界面只有一个线程（主线程）在跑。如果我们在主线程里执行抓取，
   那么「下载网页、等 20 秒超时、sleep 1.5 秒」这些耗时操作期间，
   主线程就没空去画界面了 —— 表现就是：窗口变成一片白，点什么都没反应，
   系统提示「程序未响应」。所以抓取必须交给另一个线程去做。

   但反过来：tkinter 也不是线程安全的，子线程里直接改界面控件（比如写日志）
   有可能让程序崩溃。所以标准做法是：
       子线程只往「消息队列」里丢消息
       主线程每隔 0.12 秒去队列里取消息，然后更新界面
   本文件第 4 区实现的就是这套机制。
===============================================================================
"""

import configparser     # 标准库：读 config.ini
import os               # 标准库：处理路径、打开文件夹
import queue            # 标准库：线程之间的安全队列
import sys              # 标准库：拿到解释器路径、处理启动参数
import threading        # 标准库：后台线程
import tkinter as tk    # 标准库自带的图形界面库（不需要额外安装）
from tkinter import filedialog, messagebox, ttk
from tkinter import scrolledtext

# 把项目根目录加进「模块搜索路径」。
# 目的：保证无论你从哪个目录启动，程序都能正确找到同目录下的 spider 包。
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# spider 是本项目的引擎包。这里用「延迟导入」的写法（放在函数里）会更好排查错误，
# 但为了让界面启动时就暴露问题，我们直接在顶部导入。
from spider import crawler      # noqa: E402  （E402 = 导入位置不在文件顶部，这里是刻意的）

CONFIG_PATH = os.path.join(BASE_DIR, "config.ini")

# 预置的检索模板（2026-09 新增）。
# 每项是 (按钮上显示的名字, 模板内容)。
# 双击程序里的按钮就会把模板填进输入框，你不用担心抄错。
#
# 这两个模板都是实测可用的：
#   ① 最高法官网：GET 检索，结果静态渲染，实测搜「建设工程施工合同」返回 13 条
#   ② 最高法公报：高级检索，标题/全文/文号三个字段可以分开限定
PRESET_TEMPLATES = (
    ("最高法官网", "http://www.court.gov.cn/search.html?content={关键词}"),
    ("最高法公报", "http://gongbao.court.gov.cn/QueryArticle.html"
                   "?title={标题}&content={全文}&document_number={文号}"),
)

# 界面字体：中文 Windows 上「微软雅黑」显示最舒服；万一没有就退回系统默认。
# 用 try/except 包住是因为极少数精简版系统缺少该字体，直接写死会报错。
try:
    UI_FONT = ("Microsoft YaHei UI", 10)
    UI_FONT_SMALL = ("Microsoft YaHei UI", 9)
    UI_FONT_TITLE = ("Microsoft YaHei UI", 14, "bold")
except Exception:
    UI_FONT = UI_FONT_SMALL = UI_FONT_TITLE = None


# =============================================================================
#  第 1 区：配置读取
# =============================================================================

def _as_bool(text):
    """
    把配置里写的中文「是/否」转成 Python 的 True/False。

    容错设计：同时接受 是/y/yes/true/1/on 等多种写法，
    这样即使你手抖写成 "Yes" 或 "1"，程序也能理解你的意思。
    """
    return str(text).strip().lower() in ("是", "y", "yes", "true", "1", "on", "开")


def _as_int(text, default):
    """把配置里的数字文本转成整数；转不了就用默认值（防止你把数字打错字导致程序崩溃）。"""
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return default


def _as_float(text, default):
    """把配置里的数字文本转成小数；转不了就用默认值。"""
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return default


def load_ini(path=CONFIG_PATH):
    """
    读取 config.ini，返回一个「按区分的字典」。

    技术要点：interpolation=None
      configparser 默认会把值里的 % 当成特殊语法去解析（叫插值）。
      我们的配置里虽然没有 %，但关掉它更安全 —— 以后你在关键词里写了 %，
      程序也不会莫名其妙报错。
    """
    defaults = {
        "抓取": {
            "起始网址": "", "最大页数": 60, "每页间隔秒": 1.5, "只抓同一网站": True,
            "超时秒数": 20, "重试次数": 3, "使用代理": False,
            "代理地址": "http://127.0.0.1:7890", "遵守robots协议": False,
            "钻取链接必须包含": "", "钻取链接排除": "",
            "自动翻页": True, "列表页上限": 20,
        },
        # —— 关键词检索（2026-09 新增）——
        "关键词检索": {
            "启用检索模式": False,
            "检索网址模板": "",
        },
        "正文抽取": {
            "正文选择器": "", "标题选择器": "", "正文最短字数": 100,
        },
        "清洗筛选": {
            "必须包含关键词": "", "排除关键词": "", "段落去重": True,
            "删除含网址的段落": False, "额外杂质词": "",
        },
        "输出": {
            "输出目录": "output", "生成JSON": True, "生成汇编": True,
        },
    }
    if not os.path.isfile(path):
        return defaults        # 配置文件丢了也不影响使用，直接用内置默认值

    parser = configparser.ConfigParser(interpolation=None)
    try:
        # 用 utf-8-sig 读：它能自动跳过文件开头的 BOM 标记。
        # 为什么重要：config.ini 存的是「带 BOM 的 UTF-8」——
        #   带 BOM 的好处是 Windows 记事本一定能认出中文（不会显示乱码）；
        #   但如果用普通 utf-8 去读，BOM 会被当成第一个节名的一部分，
        #   导致「[抓取]」这个节名变成「\ufeff[抓取]」，所有参数都读不到、静默退回默认值。
        # utf-8-sig 对「有 BOM」和「没 BOM」的文件都能正确处理，所以统一用它。
        parser.read(path, encoding="utf-8-sig")
    except Exception:
        return defaults

    def get(section, key, fallback=""):
        """安全取值：区或键不存在时返回兜底值，绝不抛异常。"""
        try:
            return parser.get(section, key).strip()
        except Exception:
            return fallback

    return {
        "抓取": {
            "起始网址": get("抓取", "起始网址"),
            "最大页数": _as_int(get("抓取", "最大页数", "30"), 30),
            "每页间隔秒": _as_float(get("抓取", "每页间隔秒", "1.5"), 1.5),
            "只抓同一网站": _as_bool(get("抓取", "只抓同一网站", "是")),
            "超时秒数": _as_int(get("抓取", "超时秒数", "20"), 20),
            "重试次数": _as_int(get("抓取", "重试次数", "3"), 3),
            "使用代理": _as_bool(get("抓取", "使用代理", "否")),
            "代理地址": get("抓取", "代理地址", "http://127.0.0.1:7890"),
            "遵守robots协议": _as_bool(get("抓取", "遵守robots协议", "否")),
            "钻取链接必须包含": get("抓取", "钻取链接必须包含"),
            "钻取链接排除": get("抓取", "钻取链接排除"),
            "自动翻页": _as_bool(get("抓取", "自动翻页", "是")),
            "列表页上限": _as_int(get("抓取", "列表页上限", "20"), 20),
        },
        "关键词检索": {
            "启用检索模式": _as_bool(get("关键词检索", "启用检索模式", "否")),
            "检索网址模板": get("关键词检索", "检索网址模板"),
        },
        "正文抽取": {
            "正文选择器": get("正文抽取", "正文选择器"),
            "标题选择器": get("正文抽取", "标题选择器"),
            "正文最短字数": _as_int(get("正文抽取", "正文最短字数", "100"), 100),
        },
        "清洗筛选": {
            "必须包含关键词": get("清洗筛选", "必须包含关键词"),
            "排除关键词": get("清洗筛选", "排除关键词"),
            "段落去重": _as_bool(get("清洗筛选", "段落去重", "是")),
            "删除含网址的段落": _as_bool(get("清洗筛选", "删除含网址的段落", "否")),
            "额外杂质词": get("清洗筛选", "额外杂质词"),
        },
        "输出": {
            "输出目录": get("输出", "输出目录", "output") or "output",
            "生成JSON": _as_bool(get("输出", "生成JSON", "是")),
            "生成汇编": _as_bool(get("输出", "生成汇编", "是")),
        },
    }


# =============================================================================
#  第 2 区：主窗口类 · 界面搭建
# =============================================================================

class SpiderWindow:
    """主窗口。一个类 = 一个窗口的全部状态和行为。"""

    def __init__(self, root):
        self.root = root                       # tkinter 的根窗口对象
        self.ini = load_ini()                  # 启动时读一次配置

        # 线程间通信用的消息队列（详见文件顶部说明）
        self.queue = queue.Queue()
        # 「停止」信号：threading.Event 是一个线程安全的开关，
        # 界面线程 set() 它，抓取线程用 is_set() 检查
        self.stop_event = threading.Event()
        self.worker = None                     # 保存后台线程对象
        self.last_output_dir = None            # 上一次结果的文件夹，用于「打开结果文件夹」

        self._build_window()                   # 画窗口
        self._build_widgets()                  # 画控件
        self._apply_ini_defaults()             # 把配置里的默认值填进输入框
        self._poll_queue()                     # 启动「消息队列轮询」（它会自我循环）

    # ---------------- 窗口本身 ----------------

    def _build_window(self):
        """设置窗口标题、大小、位置。"""
        self.root.title("法律文书抓取器  v1.1.1")
        width, height = 1020, 880
        # 让窗口在屏幕中央出现（而不是缩在左上角）
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 2)
        self.root.geometry("%dx%d+%d+%d" % (width, height, x, y))
        self.root.minsize(820, 600)            # 再小就会挤，限制最小尺寸

        # 高 DPI 屏幕（笔记本常见的 150% 缩放）上让文字不模糊。
        # 这是 Windows 特有的 API，其它系统会抛异常，所以包一层 try。
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    # ---------------- 控件布局 ----------------

    def _build_widgets(self):
        """
        画出全部控件。

        布局思路（从上到下五个区块）：
          ① 标题行
          ② 地址输入区（网址 / 本地文件）
          ③ 参数区（最大页数、间隔、开关）
          ④ 按钮区（开始 / 停止 / 打开结果 / 打开配置）
          ⑤ 进度条 + 日志区（占满剩余空间）
        """
        # 顶层容器：padx/pady 是四周留白，让界面不至于贴边
        container = ttk.Frame(self.root, padding=12)
        container.pack(fill="both", expand=True)

        # ---------- ① 标题 ----------
        ttk.Label(container, text="法律文书抓取器", font=UI_FONT_TITLE).pack(anchor="w")
        ttk.Label(
            container,
            text="填一个网页地址 → 点开始 → 自动把页面上的正文抓下来存成文件",
            font=UI_FONT_SMALL, foreground="#666666",
        ).pack(anchor="w", pady=(2, 10))

        # ---------- ② 地址输入区 ----------
        addr_frame = ttk.LabelFrame(container, text="要抓取的地址", padding=10)
        addr_frame.pack(fill="x")

        self.url_var = tk.StringVar()          # StringVar 是「和输入框绑定的变量」，读写它即可读写输入框
        entry = ttk.Entry(addr_frame, textvariable=self.url_var, font=UI_FONT)
        entry.pack(side="left", fill="x", expand=True)
        # 回车键 = 点开始，符合直觉
        entry.bind("<Return>", lambda _event: self.on_start())

        ttk.Button(addr_frame, text="选择本地网页…",
                   command=self.on_pick_file).pack(side="left", padx=(8, 0))

        ttk.Label(
            container,
            text="提示：也可以点右边按钮，选一个你用浏览器 Ctrl+S 保存下来的网页文件（对付不让抓的网站）",
            font=UI_FONT_SMALL, foreground="#888888",
        ).pack(anchor="w", pady=(3, 10))

        # ---------- ②.5 关键词检索区（2026-09 新增） ----------
        # 这个区的思路：让用户选好一个网站的检索接口，然后只填检索词，
        # 程序自动把词拼进网址去检索，再抓检索到的每一条。
        search_frame = ttk.LabelFrame(
            container, text="关键词检索（可选：让程序先去网站搜，再抓搜到的每一条）", padding=10)
        search_frame.pack(fill="x", pady=(0, 10))

        # 第一行：开关 + 预置模板按钮
        search_top = ttk.Frame(search_frame)
        search_top.pack(fill="x")
        self.search_mode_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(search_top, text="启用检索模式", variable=self.search_mode_var,
                        command=self._on_search_toggle).pack(side="left")
        ttk.Label(search_top, text="　一键套用预置模板：", font=UI_FONT).pack(side="left")
        for label, template in PRESET_TEMPLATES:
            ttk.Button(search_top, text=label, width=11,
                       command=lambda t=template: self._set_template(t)).pack(side="left", padx=2)

        # 第二行：模板输入框
        search_row = ttk.Frame(search_frame)
        search_row.pack(fill="x", pady=(6, 0))
        ttk.Label(search_row, text="检索模板：", font=UI_FONT).pack(side="left")
        self.search_template_var = tk.StringVar()
        template_entry = ttk.Entry(search_row, textvariable=self.search_template_var, font=UI_FONT_SMALL)
        template_entry.pack(side="left", fill="x", expand=True)
        # 光标离开输入框（或按回车）时才去解析占位符 ——
        # 如果每次按键都解析，输入框会被不停重建，根本没法打字
        template_entry.bind("<FocusOut>", lambda _e: self._refresh_search_fields())
        template_entry.bind("<Return>", lambda _e: self._refresh_search_fields())

        # 第三行：占位符输入框（由 _refresh_search_fields 动态生成）
        self.search_fields_frame = ttk.Frame(search_frame)
        self.search_fields_frame.pack(fill="x", pady=(6, 0))
        self.search_field_vars = {}          # 名字 → 输入框变量
        self._refresh_search_fields()

        # ---------- ③ 参数区 ----------
        param_frame = ttk.LabelFrame(container, text="本次抓取设置（想永久修改请编辑 config.ini）", padding=10)
        param_frame.pack(fill="x")

        ttk.Label(param_frame, text="最多抓几页：", font=UI_FONT).grid(row=0, column=0, sticky="w")
        self.pages_var = tk.StringVar()
        ttk.Entry(param_frame, textvariable=self.pages_var, width=6, font=UI_FONT).grid(row=0, column=1, sticky="w")

        ttk.Label(param_frame, text="  每页间隔(秒)：", font=UI_FONT).grid(row=0, column=2, sticky="w")
        self.interval_var = tk.StringVar()
        ttk.Entry(param_frame, textvariable=self.interval_var, width=6, font=UI_FONT).grid(row=0, column=3, sticky="w")

        self.same_site_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(param_frame, text="只抓同一个网站", variable=self.same_site_var).grid(
            row=0, column=4, sticky="w", padx=(16, 0))

        self.only_entry_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(param_frame, text="只抓这一页（不钻链接）", variable=self.only_entry_var).grid(
            row=0, column=5, sticky="w", padx=(16, 0))

        ttk.Label(param_frame, text="正文选择器：", font=UI_FONT).grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.selector_var = tk.StringVar()
        ttk.Entry(param_frame, textvariable=self.selector_var, width=34, font=UI_FONT).grid(
            row=1, column=1, columnspan=3, sticky="we", pady=(8, 0))
        ttk.Label(param_frame, text="（留空＝自动识别；填了就以它为准，例如 div.txt_txt）",
                  font=UI_FONT_SMALL, foreground="#888888").grid(
            row=1, column=4, columnspan=2, sticky="w", pady=(8, 0))

        # 第 3 行：链接关键词筛选（最实用的一个筛子）
        ttk.Label(param_frame, text="只抓链接含：", font=UI_FONT).grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.link_must_var = tk.StringVar()
        ttk.Entry(param_frame, textvariable=self.link_must_var, width=24, font=UI_FONT).grid(
            row=2, column=1, columnspan=2, sticky="we", pady=(8, 0))

        ttk.Label(param_frame, text="链接排除：", font=UI_FONT).grid(row=2, column=3, sticky="w", pady=(8, 0))
        self.link_exclude_var = tk.StringVar()
        ttk.Entry(param_frame, textvariable=self.link_exclude_var, width=18, font=UI_FONT).grid(
            row=2, column=4, columnspan=2, sticky="we", pady=(8, 0))

        ttk.Label(param_frame,
                  text="（例：填「解释,规定,批复」→ 只跟进标题含这些词的链接；列表页上混着的开庭公告、招聘就不会被抓）",
                  font=UI_FONT_SMALL, foreground="#888888").grid(
            row=3, column=0, columnspan=6, sticky="w", pady=(3, 0))

        # 第 4 行：自动翻页（2026-09 新增，解决「抓到的数据太少」）
        self.auto_paging_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(param_frame, text="自动翻页（跟随「更多 / 下一页」，把整个栏目翻一遍，抓得更多）",
                        variable=self.auto_paging_var).grid(
            row=4, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Label(param_frame, text="最多翻列表页：", font=UI_FONT).grid(
            row=4, column=4, sticky="w", pady=(8, 0))
        self.max_list_var = tk.StringVar()
        ttk.Entry(param_frame, textvariable=self.max_list_var, width=6, font=UI_FONT).grid(
            row=4, column=5, sticky="w", pady=(8, 0))

        # ---------- ④ 按钮区 ----------
        button_frame = ttk.Frame(container)
        button_frame.pack(fill="x", pady=10)

        self.start_button = ttk.Button(button_frame, text="开始抓取", command=self.on_start)
        self.start_button.pack(side="left")

        self.stop_button = ttk.Button(button_frame, text="停止", command=self.on_stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)

        self.open_button = ttk.Button(button_frame, text="打开结果文件夹",
                                      command=self.on_open_output, state="disabled")
        self.open_button.pack(side="left", padx=8)

        ttk.Button(button_frame, text="打开 config.ini",
                   command=self.on_open_config).pack(side="left", padx=8)

        ttk.Button(button_frame, text="重读配置",
                   command=self.on_reload_config).pack(side="left", padx=8)

        # ---------- ⑤ 进度条 ----------
        progress_frame = ttk.Frame(container)
        progress_frame.pack(fill="x")
        self.progress = ttk.Progressbar(progress_frame, mode="determinate", maximum=100)
        self.progress.pack(side="left", fill="x", expand=True)
        self.progress_label = ttk.Label(progress_frame, text="等待开始", font=UI_FONT_SMALL, width=24)
        self.progress_label.pack(side="left", padx=(10, 0))

        # ---------- ⑥ 日志区 ----------
        log_frame = ttk.LabelFrame(container, text="运行日志", padding=6)
        log_frame.pack(fill="both", expand=True, pady=(10, 0))

        self.log_box = scrolledtext.ScrolledText(
            log_frame, wrap="word", font=UI_FONT_SMALL, height=14,
            background="#1E1E1E", foreground="#D4D4D4", insertbackground="#D4D4D4",
        )
        self.log_box.pack(fill="both", expand=True)
        self.log_box.configure(state="disabled")   # 只读：用户不能手打，但程序可以写

        self._log("准备就绪。")
        self._log("用法：把网页地址粘到最上面的输入框，点「开始抓取」。")
        self._log("")

    # ---------------- 检索模板相关（2026-09 新增） ----------------

    def _refresh_search_fields(self):
        """
        根据检索模板里的 {占位符}，自动生成对应的输入框。

        这是本界面最方便的一个设计：你套用「最高法公报」模板后，
        程序发现模板里有 {标题}{全文}{文号} 三个占位符，
        就自动给你摆出三个输入框 —— 你想按哪个字段搜就填哪个，不填的留空。
        """
        # 先把已经填过的值记下来，避免刷新时把用户输入清空
        old_values = {name: var.get() for name, var in self.search_field_vars.items()}

        # 清掉旧的输入框
        for child in self.search_fields_frame.winfo_children():
            child.destroy()
        self.search_field_vars = {}

        names = crawler.placeholders_of(self.search_template_var.get())
        if not names:
            ttk.Label(
                self.search_fields_frame,
                text="（模板里还没有 {占位符}。花括号里的名字就是你要填的东西，"
                     "例如 {关键词}；点上面的预置模板按钮可以一键填好）",
                font=UI_FONT_SMALL, foreground="#888888",
            ).pack(side="left")
            return

        ttk.Label(self.search_fields_frame, text="填写检索词：", font=UI_FONT).pack(side="left")
        for name in names:
            ttk.Label(self.search_fields_frame, text="%s＝" % name, font=UI_FONT).pack(side="left", padx=(8, 0))
            var = tk.StringVar(value=old_values.get(name, ""))
            ttk.Entry(self.search_fields_frame, textvariable=var, width=16, font=UI_FONT).pack(side="left")
            self.search_field_vars[name] = var

    def _set_template(self, template):
        """套用一个预置检索模板，并自动勾上「启用检索模式」。"""
        self.search_template_var.set(template)
        self.search_mode_var.set(True)
        self._refresh_search_fields()
        self._log("已套用检索模板：%s" % template)

    def _on_search_toggle(self):
        """勾选/取消「启用检索模式」时的提示。"""
        if self.search_mode_var.get():
            self._log("已启用检索模式：程序会先拿检索词去网站搜，再抓搜到的每一条。")
        else:
            self._log("已关闭检索模式：回到「直接抓取你填的那个网址」的用法。")

    def _apply_ini_defaults(self):
        """把 config.ini 里的参数值填进界面控件。"""
        grab = self.ini["抓取"]
        extract = self.ini["正文抽取"]
        self.url_var.set(grab.get("起始网址", ""))
        self.pages_var.set(str(grab.get("最大页数", 30)))
        self.interval_var.set(str(grab.get("每页间隔秒", 1.5)))
        self.same_site_var.set(bool(grab.get("只抓同一网站", True)))
        self.selector_var.set(extract.get("正文选择器", ""))
        self.link_must_var.set(grab.get("钻取链接必须包含", ""))
        self.link_exclude_var.set(grab.get("钻取链接排除", ""))
        self.auto_paging_var.set(bool(grab.get("自动翻页", True)))
        self.max_list_var.set(str(grab.get("列表页上限", 20)))
        # 检索区（2026-09 新增）
        search = self.ini["关键词检索"]
        self.search_mode_var.set(bool(search.get("启用检索模式", False)))
        self.search_template_var.set(search.get("检索网址模板", ""))
        self._refresh_search_fields()

    # ---------------- 日志写入 ----------------

    def _log(self, text):
        """
        往日志框追加一行。

        技术细节：日志框平时是「只读」状态（state="disabled"），
        要写内容必须先临时改成 normal，写完再改回 disabled。
        这是 tkinter 的常规套路。
        """
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")        # 自动滚动到最底部
        self.log_box.configure(state="disabled")

    # =========================================================================
    #  第 3 区：事件处理（按钮点下去之后做什么）
    # =========================================================================

    def on_pick_file(self):
        """选择本地网页文件，把路径填进地址框。"""
        path = filedialog.askopenfilename(
            title="选择你保存下来的网页文件",
            filetypes=[("网页文件", "*.html *.htm *.xhtml"), ("全部文件", "*.*")],
            initialdir=os.path.join(BASE_DIR, "samples"),
        )
        if path:
            self.url_var.set(path)
            self._log("已选择本地文件：%s" % path)

    def on_reload_config(self):
        """重新读取 config.ini（你在记事本里改完不用重启程序）。"""
        self.ini = load_ini()
        self._apply_ini_defaults()
        self._log("已重新读取 config.ini 的默认设置。")

    def on_open_config(self):
        """用系统默认程序（通常是记事本）打开 config.ini。"""
        if not os.path.isfile(CONFIG_PATH):
            messagebox.showwarning("找不到配置文件", "没有找到 config.ini：\n%s" % CONFIG_PATH)
            return
        try:
            os.startfile(CONFIG_PATH)      # Windows 专用：用默认程序打开文件
        except Exception as exc:
            messagebox.showerror("打开失败", "无法打开配置文件：\n%s" % exc)

    def on_open_output(self):
        """打开上一次结果的文件夹。"""
        target = self.last_output_dir
        if not target or not os.path.isdir(target):
            messagebox.showinfo("还没有结果", "还没有生成结果文件夹，先抓一次试试。")
            return
        try:
            os.startfile(target)
        except Exception as exc:
            messagebox.showerror("打开失败", "无法打开文件夹：\n%s" % exc)

    def _collect_config(self):
        """
        把界面上的设置收集成一个 CrawlConfig 对象。

        这里体现一个设计原则：界面上的改动只影响「本次运行」，
        不会偷偷改你的 config.ini。想永久修改，就去编辑配置文件。
        """
        grab = self.ini["抓取"]
        extract = self.ini["正文抽取"]
        cleaning = self.ini["清洗筛选"]
        output = self.ini["输出"]

        source = self.url_var.get().strip()
        max_pages = _as_int(self.pages_var.get(), grab.get("最大页数", 30))
        if self.only_entry_var.get():
            max_pages = 1              # 勾了「只抓这一页」，就强制只抓一页

        # 检索相关的三个值（2026-09 新增）
        search_template = self.search_template_var.get().strip()
        search_mode = bool(self.search_mode_var.get()) and bool(search_template)
        search_values = {name: var.get().strip() for name, var in self.search_field_vars.items()}

        return crawler.CrawlConfig(
            source=source,
            search_mode=search_mode,
            search_template=search_template,
            search_values=search_values,
            max_pages=max(1, max_pages),
            interval=max(0.0, _as_float(self.interval_var.get(), grab.get("每页间隔秒", 1.5))),
            same_site_only=bool(self.same_site_var.get()),
            timeout=grab.get("超时秒数", 20),
            retries=grab.get("重试次数", 3),
            use_proxy=grab.get("使用代理", False),
            proxy_url=grab.get("代理地址", ""),
            respect_robots=grab.get("遵守robots协议", False),
            link_must=self.link_must_var.get().strip() or grab.get("钻取链接必须包含", ""),
            link_exclude=self.link_exclude_var.get().strip() or grab.get("钻取链接排除", ""),
            auto_paging=bool(self.auto_paging_var.get()),
            max_list_pages=max(1, _as_int(self.max_list_var.get(), grab.get("列表页上限", 20))),
            article_selector=self.selector_var.get().strip() or extract.get("正文选择器", ""),
            title_selector=extract.get("标题选择器", ""),
            min_length=extract.get("正文最短字数", 100),
            must_have=cleaning.get("必须包含关键词", ""),
            exclude=cleaning.get("排除关键词", ""),
            drop_duplicates=cleaning.get("段落去重", True),
            remove_url_lines=cleaning.get("删除含网址的段落", False),
            extra_noise=cleaning.get("额外杂质词", ""),
            output_dir=os.path.join(BASE_DIR, output.get("输出目录", "output")),
            generate_json=output.get("生成JSON", True),
            generate_compilation=output.get("生成汇编", True),
        )

    def on_start(self):
        """点「开始抓取」：先检查输入，再启动后台线程。"""
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("正在运行", "上一次抓取还没结束，请先点「停止」或等它跑完。")
            return

        source = self.url_var.get().strip()
        search_mode = bool(self.search_mode_var.get()) and bool(self.search_template_var.get().strip())

        if search_mode:
            # 检索模式：地址可以不填，但至少要有一个检索词
            values = {name: var.get().strip() for name, var in self.search_field_vars.items()}
            if not any(values.values()):
                messagebox.showwarning(
                    "还没填检索词",
                    "检索模式下至少要填一个检索词。\n"
                    "例如在「关键词＝」后面的框里填「建设工程施工合同」。",
                )
                return
        else:
            # 直接抓取模式：必须有地址
            if not source:
                messagebox.showwarning(
                    "还没填地址",
                    "请先在输入框里粘贴一个网页地址。\n\n"
                    "或者：勾选上面的「启用检索模式」，改用关键词检索。",
                )
                return
            # 输入检查：要么是 http(s) 开头的网址，要么是一个存在的本地文件
            if not (source.lower().startswith(("http://", "https://")) or os.path.isfile(source)):
                messagebox.showwarning(
                    "地址看起来不对",
                    "请填写完整的网页地址（要以 http:// 或 https:// 开头），\n"
                    "或者点「选择本地网页…」挑一个已保存的网页文件。",
                )
                return

        config = self._collect_config()

        # 清空日志、重置状态
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")
        self.stop_event.clear()               # 清掉上一次的停止信号
        self.progress.configure(value=0)
        self.progress_label.configure(text="正在启动…")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.open_button.configure(state="disabled")

        # 启动后台线程。daemon=True 表示「主程序关闭时这个线程跟着一起死」，
        # 否则程序会因为你关了窗口但线程还在跑而无法退出。
        self.worker = threading.Thread(target=self._worker, args=(config,), daemon=True)
        self.worker.start()

    def on_stop(self):
        """点「停止」：设置停止信号，抓取线程会在处理完当前这一页后停下来。"""
        if self.worker and self.worker.is_alive():
            self.stop_event.set()
            self._log("")
            self._log("⚠ 收到停止指令，将在当前这一页处理完后停下…")
            self.progress_label.configure(text="正在停止…")

    # =========================================================================
    #  第 4 区：后台线程与消息队列
    # =========================================================================

    def _worker(self, config):
        """
        后台线程的本体：调用爬虫一条龙，把结果丢进队列。

        注意：这个函数运行在「子线程」里，所以它绝对不碰任何界面控件，
        只往 self.queue 里放消息。
        用 try/except 包住，是为了把意外报错也变成一条日志显示给你看，
        而不是让窗口莫名其妙地卡在「正在抓取」状态。
        """
        try:
            records, summary, location = crawler.run_and_export(
                config,
                log=lambda text: self.queue.put(("log", text)),
                progress=lambda cur, total, url, status: self.queue.put(
                    ("progress", (cur, total, status))),
                should_stop=lambda: self.stop_event.is_set(),
                base_output_dir=config.output_dir,
            )
            self.queue.put(("done", (records, summary, location)))
        except Exception as exc:
            import traceback
            self.queue.put(("log", "程序内部错误：%s" % exc))
            self.queue.put(("log", traceback.format_exc()))
            self.queue.put(("done", ([], {}, None)))

    def _poll_queue(self):
        """
        主线程的「收件员」：每 0.12 秒检查一次队列，把子线程发来的消息变成界面变化。

        为什么用 after(120, ...) 而不是 while 循环？
          after() 是 tkinter 的定时器：它把「下一次检查」登记到界面事件循环里，
          这样在两次检查之间界面依然能响应用户的点击。
          如果写成 while True 循环，界面就彻底卡死了。
        """
        while True:
            try:
                kind, payload = self.queue.get_nowait()   # 非阻塞取消息：没有就抛 Empty
            except queue.Empty:
                break

            if kind == "log":
                self._log(payload)
            elif kind == "progress":
                current, total, status = payload
                percent = int(current / total * 100) if total else 0
                self.progress.configure(value=min(percent, 100))
                self.progress_label.configure(text="第 %d/%d 页 · %s" % (current, total, status[:12]))
            elif kind == "done":
                self._finish(*payload)

        # 登记下一次检查（120 毫秒后）
        self.root.after(120, self._poll_queue)

    def _finish(self, records, summary, location):
        """抓取结束后的收尾工作：恢复按钮、显示统计。"""
        if location:
            self.last_output_dir = location.get("folder")
            self.open_button.configure(state="normal")
            self.progress.configure(value=100)
            self.progress_label.configure(text="完成")
            self._log("")
            self._log("=" * 56)
            self._log("抓取完成！")
            self._log("  成功篇数：%s" % summary.get("成功篇数", 0))
            self._log("  失败/跳过：%s" % summary.get("失败篇数", 0))
            self._log("  合计字数：%s" % summary.get("合计字数", 0))
            self._log("  结果位置：%s" % location.get("folder"))
            self._log("=" * 56)
        else:
            self.progress_label.configure(text="没有结果")
            self._log("")
            self._log("本次没有产生结果（可能是地址打不开，或页面里没识别出正文）。")

        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.stop_event.clear()


# =============================================================================
#  第 5 区：程序入口
# =============================================================================

def main():
    """创建窗口并进入 tkinter 的事件循环（程序会一直停在这里等你操作）。"""
    root = tk.Tk()
    # ttk 主题：vista 在 Windows 上观感最好；没有就退回默认
    try:
        ttk.Style().theme_use("vista")
    except Exception:
        pass
    SpiderWindow(root)
    root.mainloop()


if __name__ == "__main__":
    main()
