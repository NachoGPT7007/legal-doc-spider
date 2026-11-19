# -*- coding: utf-8 -*-
"""
===============================================================================
 crawler.py —— 调度区（整个项目的「工头」）
===============================================================================
 【这个文件负责什么】
   前面三个文件各管一段（抓取 / 解析 / 清洗），这个文件负责把它们串起来，
   按正确顺序、按限速、按上限去干活。具体做四件事：

     第 1 件：抓入口页 → 解析出正文 → 记录成一条结果
     第 2 件：从入口页里找出「同一网站」的其它链接（这就是「钻一层」）
     第 3 件：给这些链接排个队（越像文章的越先抓），然后一个个抓
     第 4 件：全程控制「最多抓几页」「每页间隔几秒」「用户点了停止就停」

 【「钻一层」到底是什么意思（重要）】
   假设你填的入口页是某个法院网站的「司法解释列表页」：
       入口页（第 0 层）
         ├─ 链接 A → 某篇司法解释全文   ← 钻一层：抓
         ├─ 链接 B → 另一篇司法解释全文 ← 钻一层：抓
         └─ 链接 C → 某个栏目页
               └─ 链接 D → 更深处的文章  ← 第 2 层：不抓
   本程序默认只钻一层（只抓入口页上直接列出的那些链接），
   不会再从抓到的页面里继续往外爬 —— 这样数据不会失控，
   也不会因为无限递归把人家网站拖垮、把自己 IP 封掉。

 【本文件的分区】
   第 1 区  链接提取与规范化
   第 2 区  链接过滤（要不要抓这一条）
   第 3 区  抓取配置对象
   第 4 区  单页处理流程（抓 → 解析 → 清洗 → 记录）
   第 5 区  对外主函数 crawl
===============================================================================
"""

import re
import time
import urllib.parse

from . import cleaner       # 同包导入：清洗区
from . import exporter      # 同包导入：输出区
from . import fetcher       # 同包导入：抓取区
from . import parser        # 同包导入：解析区


# =============================================================================
#  第 1 区：链接提取与规范化
# =============================================================================

# 匹配 HTML 里的 <a href="..."> 标签。
# (?is)  = i 忽略大小写、s 让 . 也能匹配换行
# <a\s   = 标签名后跟一个空白（避免匹配到 <abbr> 这种同前缀标签）
# [^>]*? = 中间任意属性（非贪婪，尽量少匹配）
# href\s*=\s*["\']?([^"\'\s>]+) = 抓取网址部分
A_TAG_RE = re.compile(r"(?is)<a\s[^>]*?href\s*=\s*[\"']?([^\"'\s>]+)")

# 明显的非网页资源后缀：这些不是「页面」，抓回来也解析不了正文
NON_PAGE_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".ico",
    ".css", ".js", ".json", ".xml", ".zip", ".rar", ".7z", ".gz",
    ".mp3", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".exe", ".msi",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".pdf",
)

# 「看起来像文章」的网址特征词。命中越多，越优先抓。
ARTICLE_HINTS = (
    "detail", "content", "article", "show", "news", "zixun", "xiangqing",
    "fabu", "view", "info", "doc", "case", "judge", "shenpan", "gongbao",
)

# 肯定是「非文章」的链接文字，抓到也是浪费时间
BAD_LINK_TEXTS = (
    "更多", "首页", "登录", "注册", "返回", "上一个", "下一个", "下一页",
    "上一页", "末页", "english", "网站地图", "联系我们", "关于我们",
)


def normalize_url(href, base_url):
    """
    把网页里的「相对网址」变成「绝对网址」。

    背景知识：
      HTML 里的链接有三种写法：
        <a href="http://a.com/x.html">   完整地址（绝对地址）
        <a href="/law/123.html">         以斜杠开头（相对于网站根目录）
        <a href="../law/123.html">       相对路径（相对于当前页面）
      后两种必须结合「当前页面的地址」才能算出真实地址。
      urllib.parse.urljoin 就是干这件事的标准工具。

    额外清理：
      · 去掉 #锚点（#section2 指向同一页面的某一段，不是新页面）
      · 去掉 javascript: 和 mailto: 这类根本不是网页的链接
    """
    href = (href or "").strip()
    if not href:
        return ""
    lowered = href.lower()
    if lowered.startswith(("javascript:", "mailto:", "tel:", "file:", "#")):
        return ""
    absolute = urllib.parse.urljoin(base_url, href)
    absolute, _fragment = urllib.parse.urldefrag(absolute)      # 去掉 #后面的部分
    if not absolute.lower().startswith(("http://", "https://")):
        return ""
    return absolute


def extract_links(html, base_url):
    """
    从 HTML 里取出全部链接，返回 [(链接文字, 绝对网址), ...]。

    实现方式：用正则找出所有 <a href>，再从「标签真正结束的 > 之后」
    截一小段，取 </a> 之前的文字当「链接文字」。
    用正则而不是完整建树，是因为这一步只需要「快」，
    链接文字质量差一点无所谓（它只用来排序和过滤）。

    踩过的坑（第一版就是错的）：
      最开始我从 href 属性值结束的位置往后取文字，结果取到的是
          " target="_blank">最高人民法院关于……
      也就是把标签里剩下的属性也当成了链接文字。
      正确做法是先往后找到标签的 >，从它后面才开始算文字。
    """
    results = []
    for match in A_TAG_RE.finditer(html):
        href = match.group(1)
        url = normalize_url(href, base_url)
        if not url:
            continue
        # 找到这个 <a 标签的结束位置：从匹配处往后找第一个 ">"
        tag_end = html.find(">", match.end())
        if tag_end < 0:
            continue
        tail = html[tag_end + 1: tag_end + 1 + 300]
        end = tail.lower().find("</a>")
        inner = tail[:end] if end >= 0 else ""
        # 去掉内部所有标签（例如 <span>、<img>、<font>），只留下文字
        text = re.sub(r"<[^>]+>", "", inner)
        text = re.sub(r"\s+", " ", text).strip()
        results.append((text, url))
    return results


def same_site(url_a, url_b):
    """
    判断两个网址是不是「同一个网站」。

    判据（依次放宽）：
      · 域名完全相同（www.a.com 与 www.a.com）        → 是
      · 去掉 www. 前缀后相同（www.a.com 与 a.com）    → 是
      · 端口号不参与比较（因为 :80 和 :443 是默认端口）
    注意：不比较协议（http 和 https 视为同一站），因为很多网站两种都能访问。
    """
    host_a = urllib.parse.urlsplit(url_a).netloc.lower().split(":")[0]
    host_b = urllib.parse.urlsplit(url_b).netloc.lower().split(":")[0]
    if not host_a or not host_b:
        return False
    strip = lambda host: host[4:] if host.startswith("www.") else host
    if host_a == host_b:
        return True
    return strip(host_a) == strip(host_b)


# =============================================================================
#  第 2 区：链接过滤与排序
# =============================================================================

def has_bad_extension(url):
    """判断网址是不是指向图片/文档/压缩包这类非网页资源。"""
    path = urllib.parse.urlsplit(url).path.lower()
    return path.endswith(NON_PAGE_EXTENSIONS)


def article_score(url, text, must_words=()):
    """
    给一个链接打「像不像我要的文章」的分。

    打分规则：
      链接文字含你在配置里指定的「链接必须包含关键词」   +3（最重要的一条）
      网址里含 detail/content/article 等词               +2
      网址以 .html / .htm / .shtml 结尾                  +2
      网址里含较长的数字（通常是文章 ID）                +1
      链接文字长度 >= 8（说明是有意义的标题）             +1
      链接文字命中「更多/首页/登录」这类词               -3
      链接文字太短（少于 2 个字）                        -1
    分数越高越先抓 —— 因为我们最大只抓 N 页，
    优先抓正经文章比抓一堆栏目页有价值得多。
    """
    score = 0
    lowered = url.lower()
    text_lower = text.lower()
    if must_words and any(word.lower() in text_lower for word in must_words):
        score += 3
    if any(word in lowered for word in ARTICLE_HINTS):
        score += 2
    if lowered.endswith((".html", ".htm", ".shtml", ".jsp", ".aspx")):
        score += 2
    if re.search(r"\d{4,}", lowered):
        score += 1
    if len(text) >= 8:
        score += 1
    if len(text) < 2:
        score -= 1
    if any(bad in text_lower for bad in BAD_LINK_TEXTS):
        score -= 3
    return score


def pick_links(html, base_url, same_site_only=True, max_links=200,
               link_must="", link_exclude=""):
    """
    从页面里挑出「值得抓」的链接，去重并按重要性排序。

    参数：
      link_must     链接文字里必须包含的关键词（逗号分隔），留空表示不筛
      link_exclude  链接文字里含这些词就跳过

    返回 [(链接文字, 网址, 分数), ...]

    处理步骤：
      1. 抽取全部链接
      2. 排除非网页资源（图片、PDF、Office 文档等）
      3. 如果开着「只抓同一网站」，排除跳往其它网站的链接
      4. ★ 按「链接必须包含 / 链接排除」关键词筛选（这是你最常用的筛子）
      5. 按网址去重（同一个地址在页面上出现多次只留一条）
      6. 按分数从高到低排序，最多保留 max_links 条
    """
    must_words = cleaner.split_keywords(link_must)
    exclude_words = cleaner.split_keywords(link_exclude)

    seen = set()
    picked = []
    for text, url in extract_links(html, base_url):
        if url in seen:
            continue
        if has_bad_extension(url):
            continue
        if same_site_only and not same_site(url, base_url):
            continue

        text_lower = text.lower()
        # 第 4 步：链接文字筛选
        if must_words and not any(word.lower() in text_lower for word in must_words):
            continue
        if exclude_words and any(word.lower() in text_lower for word in exclude_words):
            continue

        seen.add(url)
        picked.append((text, url, article_score(url, text, must_words)))
    picked.sort(key=lambda item: -item[2])
    return picked[:max_links]


# =============================================================================
#  第 3 区：抓取配置对象
# =============================================================================

class CrawlConfig:
    """
    把 config.ini 里读出来的参数打包成一个对象，方便在函数之间传递。

    为什么不用一堆参数一个个传？
      参数超过 8 个时，调用处会变成「一长串逗号」，极容易传错位置。
      打包成对象后，函数签名只要一个 config 就够了，而且每个字段都有名字。
    """

    __slots__ = (
        "source", "max_pages", "interval", "same_site_only", "timeout",
        "retries", "use_proxy", "proxy_url", "ignore_cert", "respect_robots",
        "link_must", "link_exclude",
        "auto_paging", "max_list_pages",
        "search_mode", "search_template", "search_values",
        "article_selector", "title_selector", "min_length",
        "must_have", "exclude", "drop_duplicates", "remove_url_lines",
        "extra_noise", "output_dir", "generate_json", "generate_compilation",
        "engine", "extract_fields",
    )

    def __init__(self, **kwargs):
        # 默认值集中在这里 —— 就算配置文件丢了，程序也能用这套默认值跑起来
        defaults = dict(
            source="",
            max_pages=30,
            interval=1.5,
            same_site_only=True,
            timeout=20,
            retries=3,
            use_proxy=False,
            proxy_url="http://127.0.0.1:7890",
            ignore_cert=False,
            respect_robots=False,
            link_must="",
            link_exclude="",
            # —— 自动翻页（2026-09 新增，解决「抓到的数据太少」）——
            # auto_paging   ：True 表示自动跟随「更多 / 下一页」链接，把整个栏目翻一遍
            # max_list_pages：最多翻多少个列表页（防止无限翻）
            auto_paging=True,
            max_list_pages=20,
            # —— 检索模式（2026-09 新增）——
            # search_mode    ：True 表示「先拿关键词去网站检索，再抓检索到的每一条」
            # search_template：检索网址模板，形如
            #                 http://www.court.gov.cn/search.html?content={关键词}
            # search_values  ：一个字典，把模板里每个占位符映射到具体要搜的值
            #                 例如 {"关键词": "建设工程施工合同"}
            search_mode=False,
            search_template="",
            search_values=None,
            article_selector="",
            title_selector="",
            min_length=100,
            must_have="",
            exclude="",
            drop_duplicates=True,
            remove_url_lines=False,
            extra_noise="",
            output_dir="output",
            generate_json=True,
            generate_compilation=True,
            engine="urllib",
            # 是否从每篇正文里抽案号/文书类型/裁判日期/法院/案由这几列
            extract_fields=True,
        )
        defaults.update(kwargs)
        for key, value in defaults.items():
            setattr(self, key, value)
        if self.search_values is None:
            self.search_values = {}

    def snapshot(self):
        """把参数导出成字典，用于写进「抓取说明.txt」，方便以后回溯。"""
        info = {
            "起始来源": self.source,
            "抓取模式": "关键词检索模式" if self.search_mode else "直接抓取模式",
            "最大页数": self.max_pages,
            "自动翻页": "开启（最多翻 %d 个列表页）" % self.max_list_pages if self.auto_paging else "关闭",
            "每页间隔秒": self.interval,
            "只抓同一网站": self.same_site_only,
            "超时秒数": self.timeout,
            "正文选择器": self.article_selector or "（自动识别）",
            "钻取链接必须包含": self.link_must or "（不筛选）",
            "钻取链接排除": self.link_exclude or "（不筛选）",
            "必须包含关键词": self.must_have or "（不筛选）",
            "排除关键词": self.exclude or "（不筛选）",
        }
        if self.search_mode:
            info["检索网址模板"] = self.search_template
            for name, value in self.search_values.items():
                info["检索词·%s" % name] = value or "（空）"
        return info


# =============================================================================
#  第 3.5 区：检索模式的支持函数（2026-09 新增）
# -----------------------------------------------------------------------------
#  【检索模式解决什么问题】
#    你不想只抓「列表页上排在前面的那些」，而是想「按关键词去搜，把搜到的都抓回来」。
#
#  【做法】
#    利用网站自己的检索功能：把关键词填进它的检索网址里。
#    例如最高人民法院官网的检索网址长这样：
#        http://www.court.gov.cn/search.html?content=建设工程施工合同
#    我们把「建设工程施工合同」这个位置做成占位符 {关键词}，
#    你换词，程序就换网址，检索由网站自己做 —— 比你猜它的筛选规则可靠得多。
#
#  【为什么这是最靠谱的路子】
#    筛选逻辑是网站自己实现的，我们只是把它的结果接过来。
#    就算它的筛选规则再复杂（多字段、模糊匹配、同义词），我们也不用管。
# =============================================================================

# 占位符正则：匹配 {关键词}、{标题}、{文号} 这样的花括号内容。
#   [^{}]+  表示「不含花括号的任意字符」，这样不会跨越两个占位符匹配
PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")


def placeholders_of(template):
    """
    从模板里找出所有占位符名字，保持出现顺序并去重。

    例子：placeholders_of("...title={标题}&content={全文}&document_number={文号}")
          → ["标题", "全文", "文号"]

    为什么去重？因为你可能在模板里同一个占位符写两次，
    但界面上只需要一个输入框。
    dict.fromkeys 是 Python 里「保序去重」的常用写法。
    """
    return list(dict.fromkeys(PLACEHOLDER_RE.findall(template or "")))


def build_search_url(template, values):
    """
    把模板里的占位符替换成实际检索词，返回可访问的完整网址。

    关键技术点 1：必须做网址编码（urllib.parse.quote）
      中文不能直接出现在网址里。例如「建设工程」必须变成
      %E5%BB%BA%E8%AE%BE%E5%B7%A5%E7%A8%8B，否则有的服务器会理解不了。
      quote 就是干这个转换的标准工具。

    关键技术点 2：缺失的占位符替换成空字符串
      如果模板里有 {文号} 但你没填，就替换成空 —— 相当于「这个字段不筛」。
      这正是公报那种三字段检索要的行为（title=&content=xxx&document_number=）。
    """
    def replace(match):
        name = match.group(1)
        value = values.get(name, "") or ""
        return urllib.parse.quote(str(value).strip())

    return PLACEHOLDER_RE.sub(replace, template or "")


# =============================================================================
#  第 4 区：单页处理流程
# =============================================================================

def process_one_page(url, config, index, log=None):
    """
    处理一个页面：抓回来 → 解析正文 → 清洗 → 打包成一条结果记录。

    返回一个字典（记录）。字典里的 ok 字段表示这一页是否成功产出正文。

    这个函数是整个程序最核心的「流水线」，
    每抓到一篇文书，它就会被调用一次。
    """
    def say(message):
        """内部小工具：有日志回调就输出一条日志，没有就什么都不做。"""
        if log:
            log(message)

    stamp = time.strftime("%Y-%m-%d %H:%M:%S")

    # ---------- 第 1 步：抓 ----------
    result = fetcher.load(
        url,
        timeout=config.timeout,
        retries=config.retries,
        use_proxy=config.use_proxy,
        proxy_url=config.proxy_url,
        ignore_cert=config.ignore_cert,
        engine=config.engine,
    )

    if not result.ok:
        say("   ✗ 抓取失败：%s" % result.error)
        return {
            "ok": False, "index": index, "url": url, "title": "",
            "source": "", "date": "", "body": "", "paragraphs": [],
            "char_count": 0, "fetch_time": stamp, "status": "抓取失败：%s" % result.error,
            "engine": result.engine,
        }

    # ---------- 第 2 步：解析 ----------
    article = parser.extract_article(
        result.html,
        url=url,
        article_selector=config.article_selector,
        title_selector=config.title_selector,
        min_length=config.min_length,
    )

    # ---------- 第 3 步：清洗 ----------
    article = cleaner.clean_article(
        article,
        extra_noise=config.extra_noise,
        drop_duplicates=config.drop_duplicates,
        remove_url_lines=config.remove_url_lines,
    )

    # ---------- 第 4 步：打包记录 ----------
    record = {
        # 下划线开头的键是「程序内部临时用」的字段，导出成 JSON 时会被自动剔除。
        # 这里保留原始 HTML，是为了让 crawl() 不必为了找链接而把入口页重复下载一遍。
        "_html": result.html,
        "ok": bool(article.get("ok")) and article.get("char_count", 0) > 0,
        "index": index,
        "url": result.final_url or url,
        "title": article.get("title", ""),
        "source": article.get("meta", {}).get("source", ""),
        "date": article.get("meta", {}).get("date", ""),
        "body": article.get("body", ""),
        "paragraphs": article.get("paragraphs", []),
        "char_count": article.get("char_count", 0),
        "fetch_time": stamp,
        "engine": article.get("engine", ""),
        "status": "成功",
        # 链接占比：这个页面里有多大比例的文字其实是「超链接文字」。
        # 数值高（超过 0.5）说明这个页面更像「列表页/导航页」而不是一篇文章。
        "link_ratio": article.get("debug", {}).get("link_ratio", 0.0),
        "debug": article.get("debug", {}),
        "clean_report": article.get("clean_report", {}),
        "encoding": result.encoding,
    }

    # ---------- 第 4.5 步：抽法律文书字段（2026-09 新增） ----------
    # 把「案号 / 文书类型 / 裁判日期 / 法院名称 / 案由」抽出来，平铺进记录里。
    # 平铺（而不是嵌套一个字典）是为了让 exporter 写 CSV 时直接用 item.get("案号")。
    if config.extract_fields:
        fields = parser.extract_legal_fields(
            record["title"], record["body"], {"date": record["date"]},
        )
        record.update(fields)
        # 顺手把抽到的日期回填到「发布时间」列 —— 如果页面上没写发布时间，
        # 但文书落款里有裁判日期，这也是有用的信息，不要浪费
        if not record["date"] and fields.get("裁判日期"):
            record["date"] = fields["裁判日期"]

    # ---------- 第 5 步：页面级筛选 ----------
    if record["ok"]:
        passed, reason = cleaner.page_passes_filter(
            record["title"], record["body"],
            must_have=config.must_have, exclude=config.exclude,
        )
        if not passed:
            record["ok"] = False
            record["status"] = "被筛选规则排除：%s" % reason
            say("   ⊘ 已跳过（%s）" % reason)
            return record

        # 抽失败时说明原因，方便你决定要不要去配置里指定选择器
        say("   ✓ 成功：%s 字 / %d 段" % (record["char_count"], len(record["paragraphs"])))
        # 提醒一句：这一类页面其实是「目录/列表」，不是单篇文章
        if record.get("link_ratio", 0) > 0.5:
            record["status"] = "成功（疑似列表页，链接占比 %d%%）" % round(record["link_ratio"] * 100)
            say("   ⓘ 提示：这一页链接占比 %d%%，看起来是列表页/导航页而不是单篇文章"
                % round(record["link_ratio"] * 100))
        return record

    # 走到这里说明解析没成功，把原因写清楚
    reason = article.get("debug", {}).get("reason", "")
    if not reason:
        if article.get("engine", "").startswith("selector"):
            reason = "指定的选择器没匹配到内容，请检查 config.ini 里的「正文选择器」"
        else:
            reason = "没能识别出正文（可能这个页面本身不是文章，或它是 JS 动态加载的）"
    record["status"] = "抽取失败：%s" % reason
    say("   ✗ %s" % reason)
    return record


# =============================================================================
#  第 5 区：对外主函数
# =============================================================================

def crawl(config, log=None, progress=None, should_stop=None):
    """
    执行一次完整抓取。

    参数：
      config       CrawlConfig 对象（所有参数）
      log          日志回调函数 log(text)，用来往界面或控制台输出一行字
      progress     进度回调函数 progress(current, total, url, status)
      should_stop  停止检查函数 should_stop()，返回 True 时立即停止
                   （图形界面的「停止」按钮就是靠它实现的）

    返回：
      (records, summary) —— 结果列表 和 一份统计摘要

    执行流程：
      第 1 步 处理入口页
      第 2 步 从入口页挑出同站链接（钻一层）
      第 3 步 依次处理这些链接，每页之间 sleep 等待（礼貌抓取）
      第 4 步 统计并返回
    """
    records = []
    started = time.time()

    def say(message):
        if log:
            log(message)

    source = config.source

    # ---------- 开场的参数播报 ----------
    if config.search_mode:
        say("模式：关键词检索（先用关键词去网站检索，再抓检索到的每一条）")
        say("检索模板：%s" % config.search_template)
        for name, value in config.search_values.items():
            say("   %s = %s" % (name, value if value else "（空）"))
    else:
        say("模式：直接抓取（从你给的页面出发，跟进它的链接）")
        say("开始抓取：%s" % source)
    say("参数：最多 %d 页，每页间隔 %.1f 秒，%s" % (
        config.max_pages, config.interval,
        "只抓同一网站" if config.same_site_only else "允许跨站"))

    # ---------- 第 1 步：拿到「入口 HTML」 ----------
    # 两种模式的区别就在这一步：
    #   直接抓取模式 → 入口 HTML 来自「你填的那个网址」，而且它本身算一篇文章
    #   关键词检索模式 → 入口 HTML 来自「检索结果页」，它本身不算文章（它只是个目录）
    if config.search_mode:
        search_url = build_search_url(config.search_template, config.search_values)
        if not search_url.lower().startswith(("http://", "https://")):
            say("")
            say("✗ 检索网址模板不对：必须以 http:// 或 https:// 开头。")
            say("  当前模板是：%s" % (config.search_template or "（空）"))
            say("  提示：模板里要用 {关键词} 这样的花括号表示「这里填检索词」。")
            return records, _summary(records, started)

        if config.respect_robots:
            allowed = fetcher.robots_allows(search_url)
            if allowed is False:
                say("⚠ 该网站的 robots.txt 明确禁止抓取，程序已停止。")
                return records, _summary(records, started)

        say("")
        say("正在检索，请稍等……")
        if progress:
            progress(1, max(1, config.max_pages), search_url, "正在检索")
        search_result = fetcher.load(
            search_url, timeout=config.timeout, retries=config.retries,
            use_proxy=config.use_proxy, proxy_url=config.proxy_url,
            ignore_cert=config.ignore_cert, engine=config.engine,
        )
        if not search_result.ok:
            say("✗ 检索页抓取失败：%s" % search_result.error)
            return records, _summary(records, started)

        html = search_result.html
        base_url = search_url
        say("检索页拿到 %d 个字符" % len(html))

        if should_stop and should_stop():
            say("已被用户停止。")
            return records, _summary(records, started)

    else:
        if config.respect_robots and fetcher.looks_like_url(source):
            allowed = fetcher.robots_allows(source)
            if allowed is False:
                say("⚠ 该网站的 robots.txt 明确禁止抓取这个地址，程序已停止。")
                say("  如果你确认自己有权限，可以到 config.ini 把「遵守robots协议」改成「否」。")
                return records, _summary(records, started)
            if allowed is None:
                say("   （没有查到有效的 robots.txt，继续抓取）")

        say("")
        say("[1/…] 抓取入口页")
        if progress:
            progress(1, max(1, config.max_pages), source, "抓取入口页")
        first = process_one_page(source, config, index=1, log=log)
        records.append(first)

        if should_stop and should_stop():
            say("已被用户停止。")
            return records, _summary(records, started)

        # 最多抓 1 页时到此为止（对应「只抓入口页」模式）
        if config.max_pages <= 1:
            return records, _summary(records, started)

        # 直接用刚才入口页抓回来的原始 HTML 去找链接，不重复下载（省一次请求，也更快）。
        # 关于字段名前的下划线：那是标记「内部临时数据」，导出文件时会自动被过滤掉，
        # 所以你不用担心结果文件里塞满了网页源码。
        html = first.get("_html", "")
        if not html:
            # 兜底：万一记录里没有 HTML（理论上不会发生），就补抓一次
            again = fetcher.load(source, timeout=config.timeout, retries=config.retries,
                                 use_proxy=config.use_proxy, proxy_url=config.proxy_url,
                                 ignore_cert=config.ignore_cert, engine=config.engine)
            html = again.html if again.ok else ""
        base_url = source

    # ---------- 第 2 步：发现要抓的链接（支持自动翻页，2026-09 增强） ----------
    links = collect_detail_links(html, base_url, config, log=log,
                                 progress=progress, should_stop=should_stop)
    if config.search_mode:
        links = filter_search_links(links)
    say("")
    say("共发现 %d 个要抓的详情页链接" % len(links))
    if config.link_must:
        say("   （详情页已按「链接必须包含：%s」筛选）" % config.link_must)
    if config.link_exclude:
        say("   （详情页已按「链接排除：%s」筛选）" % config.link_exclude)
    if not links:
        say("   ⚠ 没找到可抓的链接。可能原因：")
        say("      · 检索词太生僻，网站没返回结果")
        say("      · 这个页面的链接是 JS 动态生成的（程序读不到）")
        say("      · 「链接必须包含」筛得太严（试试清空它）")

    # ---------- 第 3 步：逐页抓取 ----------
    remaining = config.max_pages - len(records)      # 检索模式下 records 还是空的，所以不扣 1
    start_index = len(records) + 1
    total_planned = min(len(links), remaining)
    if len(links) > remaining:
        say("   （本次最多抓 %d 篇，只取排在前面的这些；想抓更多就调大「最大页数」）" % remaining)

    for offset, (text, url, score) in enumerate(links[:remaining]):
        if should_stop and should_stop():
            say("已被用户停止，已完成 %d 篇。" % len(records))
            break

        index = start_index + offset
        show_text = (text or url)[:40]
        say("")
        say("[%d/%d] %s" % (offset + 1, total_planned, show_text))
        if progress:
            progress(offset + 1, max(1, total_planned), url, show_text)

        record = process_one_page(url, config, index=index, log=log)
        records.append(record)

        # 礼貌间隔：最后一页之后就不必等了
        if offset + 1 < total_planned and config.interval > 0:
            time.sleep(config.interval)

    summary = _summary(records, started)
    say("")
    say("抓取结束：成功 %d 篇，失败/跳过 %d 篇，用时 %.1f 秒" % (
        summary["成功篇数"], summary["失败篇数"], summary["总耗时秒"]))
    return records, summary


# =============================================================================
#  第 2.5 区：自动翻页 / 列表页递归发现（2026-09 增强）
# -----------------------------------------------------------------------------
#  【为什么需要这一区 —— 这是「抓到的数据太少」的根因】
#    实测发现两件事：
#      ① 最高法官网的 /fabu.html 只是「首屏」，完整列表挂在
#         /fabu/gengduo/14.html、/fabu/gengduo/79.html 这些「更多」页里。
#         只抓首屏，最多拿到十几条。
#      ② 公报这类检索结果页，结果超过一页时会出现「下一页」链接。
#         只读第一页，结果就被截断了。
#
#  【解决办法】
#    把「列表页」也当成要抓的对象：先翻遍所有列表页、把详情页链接收集齐，
#    最后再逐篇抓正文。这就是「先逛遍目录，再逐篇取书」。
#
#  【关键设计：列表页链接不受关键词筛选约束】
#    「更多」「下一页」这些文字的链接文字跟你的关键词（比如「解释,规定」）
#    毫无关系。如果把列表页链接也拿去跑关键词筛选，翻页就永远翻不动。
#    所以两类链接要分开处理。
# =============================================================================

# 判断「这是列表页/翻页链接」的线索（看链接文字）
LIST_TEXT_WORDS = (
    "更多", "查看更多", "下一页", "下页", "后一页", "下一屏", "more", "next", "list",
)

# 判断「这是列表页/翻页链接」的线索（看网址）
# 说明：这些都是实测中见过的真实写法
LIST_URL_WORDS = (
    "gengduo",          # 最高法官网的「更多」列表页：/fabu/gengduo/14.html
    "/more", "more.",   # 常见的更多页
    "list.", "/list",   # 常见的列表页
    "page=", "pageindex=", "pageno=", "pageid=", "p=",   # 常见的翻页参数
    "/index_", "_2.html",
)


def is_list_page_link(text, url):
    """
    判断一个链接是不是「列表页 / 翻页」，而不是「具体的一篇文章」。

    判断依据（任一成立即可）：
      · 链接文字里出现「更多 / 下一页 / more / next」这类词
      · 网址里出现 gengduo / /list / page= / pageIndex= 这类特征

    为什么要小心：误判会把真正的文章当成列表页去「翻」，
    结果那篇文章的正文反而不抓了。所以下面的特征词都选得比较具体。
    """
    lowered_text = (text or "").strip().lower()
    lowered_url = (url or "").lower()
    if any(word in lowered_text for word in LIST_TEXT_WORDS):
        return True
    if any(word in lowered_url for word in LIST_URL_WORDS):
        return True
    return False


def collect_detail_links(first_html, first_url, config, log=None,
                         progress=None, should_stop=None):
    """
    收集所有要抓的详情页链接（必要时自动翻页 / 跟随「更多」链接）。

    算法（广度优先，像水波一样一层层扩）：
      第 1 步：把入口页当作第一个「待处理的列表页」
      第 2 步：从列表页里把链接分成两类
                · 列表页链接 → 放进「待翻页队列」
                · 详情页链接 → 放进「详情页字典」（用网址去重）
      第 3 步：从待翻页队列里取一个，下载它，回到第 2 步
      第 4 步：直到队列空了、或者翻页数量达到上限

    参数：
      first_html  入口页的 HTML（已经抓好了，不用重复下载）
      first_url   入口页网址
      config      抓取配置（用到 auto_paging / max_list_pages / same_site_only /
                  link_must / link_exclude）

    返回：
      详情页链接列表，按「像不像文章」的分数从高到低排好序。
      每项是 (链接文字, 网址, 分数)。
    """
    def say(message):
        if log:
            log(message)

    detail = {}                    # 网址 → (文字, 网址, 分数)，字典天然去重
    visited_list = {first_url}     # 已经处理过或已排队的列表页
    pending = [(first_html, first_url)]     # 待处理的列表页： (HTML, 网址)
    to_fetch = []                  # 待下载的列表页网址
    list_fetched = 0

    max_list = max(1, int(config.max_list_pages))
    must_words = cleaner.split_keywords(config.link_must)
    exclude_words = cleaner.split_keywords(config.link_exclude)

    if config.auto_paging:
        say("")
        say("开始收集链接（自动翻页已开启，最多翻 %d 个列表页）……" % max_list)
    else:
        say("")
        say("开始收集链接（自动翻页已关闭，只读入口页这一页）……")

    while pending or to_fetch:
        if list_fetched >= max_list:
            say("   ⓘ 已达到「列表页上限」%d 个，停止翻页（想翻更多就调大这个值）" % max_list)
            break
        if should_stop and should_stop():
            break

        # 如果当前没有已下载的列表页可用，就从待下载队列里取一个去下载
        if not pending:
            next_url = to_fetch.pop(0)
            # 【注意：这里不要再检查 visited_list】
            #   第一版代码在这里写了一句 `if next_url in visited_list: continue`，
            #   结果所有翻页链接刚入队就被跳过 —— 因为它们是「入队时」被加进
            #   visited_list 的（那个集合的作用是防止同一个网址重复入队，
            #   不是记录「已处理」）。这个 bug 导致自动翻页完全失效，
            #   日志上看起来就是「处理了 1 个列表页」。
            say("   [翻列表页 %d] %s" % (list_fetched + 1, next_url[:78]))
            if progress:
                progress(list_fetched + 1, max_list, next_url, "翻列表页")
            result = fetcher.load(
                next_url, timeout=config.timeout, retries=config.retries,
                use_proxy=config.use_proxy, proxy_url=config.proxy_url,
                ignore_cert=config.ignore_cert, engine=config.engine,
            )
            if not result.ok:
                say("      × 打不开：%s" % result.error[:70])
                continue
            pending.append((result.html, next_url))
            # 翻页也要讲礼貌，别把人家网站当压测工具
            if config.interval > 0:
                time.sleep(min(config.interval, 2.0))

        page_html, page_url = pending.pop(0)
        list_fetched += 1

        # 从这个列表页里提取两类链接
        for text, url in extract_links(page_html, page_url):
            if url in visited_list:
                continue
            if has_bad_extension(url):
                continue
            if config.same_site_only and not same_site(url, page_url):
                continue

            if config.auto_paging and is_list_page_link(text, url):
                # 列表页链接：排队继续翻，**不受关键词筛选约束**
                visited_list.add(url)
                to_fetch.append(url)
                continue

            if url in detail:
                continue

            # 详情页链接：才应用「链接必须包含 / 排除」关键词筛选
            lowered = (text or "").lower()
            if must_words and not any(word.lower() in lowered for word in must_words):
                continue
            if exclude_words and any(word.lower() in lowered for word in exclude_words):
                continue

            detail[url] = (text, url, article_score(url, text, must_words))

    # 排好序返回（越像文章的越靠前）
    ordered = sorted(detail.values(), key=lambda item: -item[2])
    if config.auto_paging:
        say("   翻页完成：处理了 %d 个列表页，收集到 %d 个详情页链接"
            % (list_fetched, len(ordered)))
    else:
        say("   收集完成：%d 个详情页链接" % len(ordered))
    return ordered


# 检索结果页上，这些文字对应的链接是「网站功能链接」，不是文章结果，直接剔掉。
#
# 【为什么用「完全等于」而不是「包含」】
#   如果判定改成「文字里包含这些词」，那么标题含「检索」二字的正经文章
#   （例如《关于人民法院检索工作的规定》）就会被误杀。用完全相等最安全。
#
# 【这份词表是怎么来的】
#   实测中在最高人民法院公报的检索结果页上，混进了「公报过刊」「读者留言」「订购」
#   这几个导航链接（正文只有 39 字和 357 字，明显不是文章）。所以补进词表。
NOISE_LINK_TEXTS = (
    # 检索表单自己的按钮
    "检索", "查询", "搜索", "高级检索", "结果中检索", "全库检索",
    # 分页
    "下一页", "上一页", "末页", "尾页", "首页", "返回", "更多",
    # 站点导航（实测出现的）
    "公报过刊", "读者留言", "订购", "联系我们", "关于我们", "网站地图",
    "使用帮助", "意见建议", "返回主站", "登录", "注册", "English",
    "下载", "打印", "分享", "收藏", "订阅",
)


def filter_search_links(links):
    """
    在检索模式下，把「检索按钮、分页导航、站点导航」这类链接剔掉。

    为什么要剔：
      检索结果页上除了文章，还有「检 索」按钮、「下一页」「末页」、
      「公报过刊」「读者留言」这些功能链接。
      不剔的话，程序会把它们当成文章去抓，浪费次数还抓回一堆垃圾。
      （实测就踩过：公报检索页上抓到了「公报过刊」和「读者留言」。）

    判定方式：
      · 链接文字**完全等于**某个功能词 → 剔除
      · 链接文字短于 4 个字 → 剔除（正经标题不会这么短）
    """
    result = []
    for text, url, score in links:
        clean_text = (text or "").strip()
        if clean_text in NOISE_LINK_TEXTS:
            continue
        if len(clean_text) < 4:
            continue
        result.append((text, url, score))
    return result


def _summary(records, started):
    """统计本次抓取的结果，返回一个摘要字典。"""
    ok_items = [item for item in records if item.get("ok")]
    return {
        "尝试页数": len(records),
        "成功篇数": len(ok_items),
        "失败篇数": len(records) - len(ok_items),
        "合计字数": sum(item.get("char_count", 0) for item in ok_items),
        "总耗时秒": round(time.time() - started, 1),
    }


def run_and_export(config, log=None, progress=None, should_stop=None,
                   base_output_dir=None):
    """
    「一条龙」函数：抓取 → 导出 → 返回结果位置。

    图形界面点「开始」按钮时调用的就是它。
    把它单独抽出来，是为了让界面代码保持干净（界面只管显示，不掺业务逻辑）。
    """
    records, summary = crawl(config, log=log, progress=progress, should_stop=should_stop)

    if not records:
        return records, summary, None

    base_dir = base_output_dir or config.output_dir
    location = exporter.export_all(
        records,
        base_dir,
        source=config.source,
        generate_json=config.generate_json,
        generate_compilation=config.generate_compilation,
        settings=config.snapshot(),
    )
    if log:
        log("")
        log("结果已保存到：%s" % location["folder"])
    return records, summary, location
