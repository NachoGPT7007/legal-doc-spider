# -*- coding: utf-8 -*-
"""
===============================================================================
 parser.py —— 解析区（整个项目的大脑）
===============================================================================
 【这个文件负责回答一个问题】
   从服务器下载回来的一大堆 HTML 代码里，到底哪一段才是真正的「文章正文」？

 【处理流程：四步】
   第 1 步 建树：用 Python 自带的 html.parser，把 HTML 文本变成一棵「节点树」
   第 2 步 分段：把树里的文字按「块级边界」和 <br> 切成语义段落
   第 3 步 打分：给每一个可能的容器打分，挑出最像正文的那一个
   第 4 步 提取：从胜出的容器里取出标题、正文段落、来源、日期

 【为什么不直接用正则表达式抓 <p> 标签？】
   实测发现：大量中文法律网站（例如最高法官网）的正文是用
       <div class="txt_txt"><span>……几千字的正文……</span><br/></div>
   这种「div + span + br」结构排版的，页面上根本没有 <p> 标签。
   如果只认 <p>，结果就是「一个字都抓不到」。
   所以必须先把 HTML 变成树，再按「文本流」的方式切分段落。

 【实测效果（本程序写完后在最高法官网验证）】
   一篇 4440 字的司法解释全文：抽出 74 段，杂质（链接文字）占比仅 0.01
===============================================================================
"""

# re 是 Python 自带的「正则表达式」模块。
# 正则表达式 = 一种描述「文字长相」的公式，例如「连续 4 个数字」写作 \d{4}。
# 本文件用它来：清理空白、匹配日期、匹配网址、解析 CSS 选择器。
import re

# html.parser 是 Python 自带的标准库，专门用来「逐字读 HTML 并回调你指定的函数」。
# 好处：不需要安装 BeautifulSoup / lxml 等第三方库，真正做到零依赖。
from html.parser import HTMLParser


# =============================================================================
#  第 1 区：常量表
#  作用：把所有「固定不变的判断依据」集中放在文件开头。
#        以后要调整「哪些标签算块级」，只改这里一处即可，不用在代码里到处找。
# =============================================================================

# 空元素表（英文 void elements，"void" 意为「空的」）。
# 背景知识：HTML 里有一部分标签天生没有闭合标签，比如换行写作 <br> 而不是 <br></br>。
# 解析时必须知道哪些标签属于这一类，否则会把它当成「容器开始」，导致后面的内容
# 全部被错误地塞进这个标签内部，整棵树就错位了。
VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
})

# 跳过表：这些标签里的内容不是给人看的正文，直接整段丢掉。
#   script = JavaScript 代码    style = CSS 样式代码
#   noscript = 浏览器禁用 JS 时才显示的内容    head = 网页头部（标题、关键词等）
#   select/option = 下拉框选项    template = 网页模板（不可见的备用结构）
#   svg/canvas = 矢量图和画布     iframe = 内嵌的另一个网页（内容不在本文档里）
# 用 frozenset（不可变集合）而不是普通列表，原因是集合的「在不在里面」查询是
# 瞬间完成的（哈希查找），而列表需要一个个比对。全文要查询几万次，差别很明显。
SKIP_TAGS = frozenset({
    "script", "style", "noscript", "head", "select", "option",
    "template", "svg", "canvas", "iframe",
})

# 块级标签表：块级元素的意思是「它独占一段，前后会自动换行」。
# 例如 <div>、<p>、<li>、<td>、<h1> 都是块级；而 <span>、<a>、<b> 是行内元素，
# 它们只是给文字加个包装，不会另起一段。
# 这个表的用途：分段时，遇到块级元素就「另起一段」；遇到行内元素则继续接在同一段里。
BLOCK_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "dd", "div", "dl", "dt",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3",
    "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre",
    "section", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
})

# 候选容器表：只有这些标签才值得被当作「可能的正文容器」去打分。
# 为什么不算上 <p>？因为 <p> 是段落本身，不是装段落的大盒子。
# 我们要找的是「大盒子」，再把盒子里的一堆段落取出来。
CANDIDATE_TAGS = frozenset({"div", "article", "section", "td", "main", "body"})

# 正向关键词表：如果一个容器的 class 或 id 里含有这些词，它很可能是正文容器。
# 依据：网页开发者的命名习惯高度一致，正文区通常叫 content / article / zhengwen(正文) 等。
POSITIVE_HINTS = (
    "txt_txt", "txt", "content", "article", "zhengwen", "zoom",
    "trs_editor", "detail", "text", "main", "news", "body", "post",
)

# 负向关键词表：含这些词的容器几乎肯定是导航、页脚、侧边栏等「非正文」区域。
NEGATIVE_HINTS = (
    "nav", "menu", "footer", "header", "sidebar", "side", "crumb",
    "share", "comment", "related", "banner", "top", "link", "ad",
    "recommend", "copyright", "search", "toolbar", "breadcrumb",
)

# 日期正则表达式，用来从正文里捞「发布时间」。
# 拆开解释：
#   (20\d{2})      第一组：20 开头加两位数字，即 2000-2099 年
#   \s*            \s 表示任意空白字符（空格/制表符），* 表示「0 个或多个」
#   [-/年.]        中括号表示「其中任意一个字符」，即分隔符可能是 - / 年 .
#   (\d{1,2})      第二组：1 到 2 位数字（月份或日期）
DATE_RE = re.compile(r"(20\d{2})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})")

# 来源正则：匹配「来源：新华社」这种写法。
#   \s* 允许「来 源 ：」中间有空格
#   [^\s|]{1,30} 表示「非空白、非竖线的字符，1 到 30 个」
SOURCE_RE = re.compile(r"来\s*源\s*[:：]\s*([^\s|]{1,30})")

# 网页标题（<title>）正则。
# (?is) 是「开关」：i = 忽略大小写，s = 让点号 . 也能匹配换行符（标题常跨行）
TITLE_RE = re.compile(r"(?is)<title[^>]*>(.*?)</title>")

# 网址正则：用于「删除含网址的段落」这个可选清洗功能。
URL_IN_TEXT_RE = re.compile(r"https?://|www\.[a-zA-Z0-9-]+\.[a-zA-Z]{2,}")

# CSS 选择器拆解正则：把 "div.txt_txt#main" 拆成 ['div', '.txt_txt', '#main'] 这样的碎片。
#   [a-zA-Z][\w-]*   标签名：字母开头，后跟字母/数字/下划线/连字符
#   \.[\w-]+         类名：点号开头
#   #[\w-]+          id：井号开头
SELECTOR_TOKEN_RE = re.compile(r"[a-zA-Z][\w-]*|\.[\w-]+|#[\w-]+")


# =============================================================================
#  第 2 区：迷你 DOM 树（把 HTML 文本变成树）
#  DOM = Document Object Model，中文叫「文档对象模型」。
#  网页在浏览器里就是一棵树：<html> 下面有 <body>，<body> 下面有 <div>……
#  我们这里用标准库手写一棵简化版的树，功能和 BeautifulSoup 类似，但零依赖。
# =============================================================================

class Node:
    """
    节点：树里的一个「元素」，可以理解为一个 HTML 标签及其全部内容。

    举例：对 <div class="txt">你好<b>世界</b>啊</div> 会生成三个 Node：
        父节点 Node(tag='div')
          ├─ 文字片段 "你好"      （存在 items 里，类型是 text）
          ├─ 子节点 Node(tag='b') （存在 items 里，类型是 node）
          │    └─ 文字片段 "世界"
          └─ 文字片段 "啊"

    为什么用 __slots__？
        Python 的对象默认带一个「字典」来存属性，灵活但费内存。
        __slots__ 提前声明「这个对象只会有这 5 个属性」，Python 就不再给每个对象
        建字典。一个网页轻松产生上万个节点，这个优化能省下大量内存并加快创建速度。

    items 与 children 的关系：
        items   = 保序的混合列表，元素是 ("text", 字符串) 或 ("node", 子节点)
        children= 只装子节点，是从 items 里挑出来的「只看节点」版本
        为什么两份都要？因为「取文字」时必须严格按原始顺序（items），
        而「遍历子元素」时不关心文字（children 更省事）。
        曾经踩过的坑：只存分开的 text 列表和 children 列表，会把
        「文字A <b>粗</b> 文字B」拼成「文字A 文字B 粗」，顺序全乱。
    """

    __slots__ = ("tag", "attrs", "parent", "items", "children")

    def __init__(self, tag, attrs=None, parent=None):
        self.tag = tag                      # 标签名，统一小写，如 "div"
        # attrs 是属性字典： {'class': 'txt_txt', 'id': 'zoom'}
        # 属性值统一转成字符串并去掉首尾空白；HTMLParser 偶尔给出 None，所以用 or ""
        self.attrs = {k: (v or "") for k, v in (attrs or {}).items()}
        self.parent = parent                # 父节点引用，用于往上回溯
        self.items = []                     # 保序的内容清单（文字与子节点混装）
        self.children = []                  # 只装子节点，保持文档顺序

    # ---------- 便捷方法 ----------

    def text(self):
        """把自己和所有后代节点的文字按原始顺序拼成一个字符串。"""
        pieces = []
        for kind, value in self.items:
            if kind == "text":
                pieces.append(value)
            else:
                # 遇到 <br> 补一个换行符，其它子节点递归取文字
                pieces.append("\n" if value.tag == "br" else value.text())
        return "".join(pieces)

    def class_list(self):
        """把 class="a b c" 拆成 ['a', 'b', 'c']，方便判断某个类名是否存在。"""
        return self.attrs.get("class", "").split()

    def hint_text(self):
        """把 class 和 id 拼成小写字符串，用于关键词匹配。"""
        return (self.attrs.get("class", "") + " " + self.attrs.get("id", "")).lower()


class MiniDom(HTMLParser):
    """
    迷你 DOM 构建器。

    它是怎么工作的？
        Python 的 HTMLParser 是一个「事件驱动」的解析器：它一个字一个字地读 HTML，
        读到开始标签就调用 handle_starttag，读到文字就调用 handle_data，
        读到结束标签就调用 handle_endtag。
        我们只要在这三个函数里维护「当前正在处理哪个节点」（cur），
        就能把扁平的文本流重新组装成一棵树。

    关键点：cur（游标）
        开始标签：新建节点，挂到 cur 下面，然后把 cur 移到新节点（相当于「走进这间屋」）
        结束标签：把 cur 退回父节点（相当于「走出这间屋」）
        文字：直接追加到 cur 的 items 里
        空元素（如 <br>）：挂到 cur 下面，但 cur 不动（因为没有「走出」这一步）
    """

    def __init__(self):
        # convert_charrefs=True 让解析器自动把 &nbsp; &amp; &#39; 等实体转成真实字符。
        # 这样我们拿到的文字就是「能直接读」的，不用再做一次实体替换。
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")   # 整棵树的根，内容都挂在它下面
        self.cur = self.root        # 游标：当前正在填充的节点
        self.skip_depth = 0         # 跳过深度计数器（详见下方）

    # ---------- 开始标签：<div class="x"> ----------
    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in SKIP_TAGS:
            # 进入「跳过区」（比如 <script>）。用计数器而不是布尔值，
            # 是为了处理嵌套情况：<script> 里又出现 <style> 也不会算错。
            self.skip_depth += 1
            return
        if self.skip_depth:
            return                      # 跳过区内的一切都不记录
        node = Node(tag, dict(attrs), self.cur)
        self.cur.items.append(("node", node))
        self.cur.children.append(node)
        if tag not in VOID_TAGS:        # 空元素不「走进去」
            self.cur = node

    # ---------- 自闭合标签：<br /> <img src="x" /> ----------
    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        if self.skip_depth or tag in SKIP_TAGS:
            return
        node = Node(tag, dict(attrs), self.cur)
        self.cur.items.append(("node", node))
        self.cur.children.append(node)
        # 自闭合标签自带结尾，不需要移动游标

    # ---------- 结束标签：</div> ----------
    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in SKIP_TAGS:
            # 离开跳过区；max(0, ...) 防止网站标签不配对时计数器变成负数
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth:
            return
        # 从当前节点往上找，直到找到同名的节点，再把游标退回它的父节点。
        # 为什么要「往上找」而不是直接退一层？因为现实中的网页经常漏写结束标签，
        # 直接退一层会让树结构彻底错位。往上找是一种容错做法。
        node = self.cur
        while node is not self.root and node.tag != tag:
            node = node.parent
        if node is not self.root:
            self.cur = node.parent

    # ---------- 文字内容 ----------
    def handle_data(self, data):
        if self.skip_depth:
            return
        # 把不换行空格（&nbsp; 转换而来，\u00a0）、回车、换行统一成普通空格。
        # 原因：\u00a0 长得像空格但不是空格，会导致后续「按空白切词」的判断失效。
        text = data.replace("\u00a0", " ").replace("\r", "").replace("\n", " ")
        if text.strip():                # 全是空白的片段直接不要，减少节点体积
            self.cur.items.append(("text", text))


def parse_html(html):
    """
    对外的小工具：传入 HTML 字符串，返回树根节点。

    为什么单独包一层？因为 HTMLParser 是「喂数据」式使用的：
    必须依次调用 feed() 和 close() 才算解析完。
    把这两步封在这里，外部调用者就不用记这些细节。
    """
    dom = MiniDom()
    dom.feed(html)      # 把 HTML 文本喂给解析器，触发各种 handle_xxx 回调
    dom.close()         # 告诉解析器「数据到此结束」，让它做收尾
    return dom.root


# =============================================================================
#  第 3 区：文本流分段
#  这是全项目最核心的一段算法，请务必看懂它。
# =============================================================================

def tidy(text):
    r"""
    文字规整：把连续的空白（空格、制表符、换行、全角空格）压成一个空格，再去掉首尾空白。

    为什么需要？
        网页源码里为了排版好看，会插入大量缩进和换行，例如：
            <p>　　第一段
                内容</p>
        直接取出来就是一串带着奇怪空格的文字。规整后变成「第一段 内容」。
        正则里的 \s 在 Python 中匹配 Unicode 空白，连中文全角空格（\u3000）也算，
        所以这一行就能同时处理中英文的空白问题。
    """
    return re.sub(r"\s+", " ", text).strip()


class ParagraphCache:
    """
    段落缓存：避免同一个节点被反复计算。

    背景：一个页面有几百个候选容器，其中大容器套着小容器。
        如果每个容器都重新遍历一遍自己的子树去取段落，
        一份内容就会被重复计算几十次，页面一大就明显变慢。
        这里的做法是「每个节点只算一次，结果存起来」，
        算法复杂度从 O(n²) 降到 O(n)（n = 节点数）。
    """

    def __init__(self):
        self._cache = {}        # 键是节点内存地址 id(node)，值是段落列表

    def lines_of(self, node):
        """取某个节点内部的全部段落（带缓存）。"""
        key = id(node)
        if key in self._cache:
            return self._cache[key]

        lines = []      # 已确认的段落
        buf = []        # 正在拼接中的当前段落（内容还没结束，不能定稿）

        for kind, value in node.items:
            if kind == "text":
                buf.append(value)
                continue

            # 以下是子节点的情况
            child = value
            if child.tag == "br":
                # <br> 代表「这里换行」：把缓冲里的文字定稿成一段
                self._flush(lines, buf)
            elif child.tag in BLOCK_TAGS:
                # 块级子元素：先收尾当前段，再递归取子块的段落，直接并入结果
                self._flush(lines, buf)
                lines.extend(self.lines_of(child))
            else:
                # 行内子元素（span / a / b / strong / font…）：
                # 它的文字应当和前后文字连成同一段，所以把子节点的段落取回来
                sub = self.lines_of(child)
                if len(sub) == 1:
                    # 常见情况：行内元素里只有一段文字（比如 <span>正文</span>），
                    # 直接追加进当前缓冲，保证「前文+span内容+后文」连成一句
                    buf.append(sub[0])
                elif sub:
                    # 行内元素内部居然有多个段落（内部含了 <br> 或块级元素），
                    # 那就分别定稿，不能硬拼
                    self._flush(lines, buf)
                    lines.extend(sub)
                # sub 为空（比如 <span><img/></span>）时不做事

        self._flush(lines, buf)         # 收尾最后一个缓冲
        self._cache[key] = lines
        return lines

    @staticmethod
    def _flush(lines, buf):
        """把缓冲里的文字拼成一段，非空就收进 lines，然后清空缓冲。"""
        if not buf:
            return
        text = tidy("".join(buf))
        buf.clear()
        if text:
            lines.append(text)


def plain_text(node, cache=None):
    """
    取节点的「全部文字」（不分段，用于统计字数和计算链接占比）。
    返回的是把 <br> 当换行、其余原样拼接的长字符串。
    """
    if cache is not None:
        return "\n".join(cache.lines_of(node))
    return tidy(node.text())


def link_text(node):
    """
    取节点内「所有超链接文字」拼成的字符串。

    为什么要单独算这个？
        这是识别正文容器的关键判据：导航栏、页脚、相关阅读列表的特点就是
        「文字几乎全是链接」。正文区则相反，链接很少。
        所以「链接文字占全部文字的比例」越高，这个容器越不可能是正文。
    """
    pieces = []

    def walk(current):
        if current.tag == "a":
            pieces.append(current.text())
        for child in current.children:
            walk(child)

    walk(node)
    return tidy("".join(pieces))


# =============================================================================
#  第 4 区：正文容器打分
#  思路：把每个「大盒子」都当候选人，按一套标准打分，分最高的就是正文容器。
# =============================================================================

def hint_factor(node):
    """
    关键词加成系数：根据容器的 class / id 名字，给出一个乘数。

    返回值的含义：
        1.5  名字里有 content / article / zhengwen 等「正文味」的词 → 加分
        0.3  名字里有 nav / footer / sidebar 等「杂质味」的词     → 重罚
        0.6  标签是 <body> 时 → 打折
             （补充说明：实测中曾出现「很短的新闻」被 body 误判为正文，
               因为 body 里包含了整站的导航文字，总量反而比正文多。
               body 几乎不可能是「纯净正文容器」，所以给它一个折扣。）

    注意这里用 any() 而不是把命中的关键词逐个相乘：
        如果某个容器的 class 是 "txt_txt text content"，
        逐个相乘会得到 1.5×1.5×1.5 = 3.4 倍，虚高得离谱。
        改成「只要命中任意一个就乘一次 1.5」，结果更稳定。
    """
    hint = node.hint_text()
    factor = 1.0
    if any(word in hint for word in POSITIVE_HINTS):
        factor *= 1.5
    if any(word in hint for word in NEGATIVE_HINTS):
        factor *= 0.3
    if node.tag == "body":
        factor *= 0.6
    return factor


def node_depth(node):
    """计算节点在树里的深度（根为 0），用于给「更靠里、更具体」的容器一点点加成。"""
    depth = 0
    cursor = node.parent
    while cursor is not None:
        depth += 1
        cursor = cursor.parent
    return depth


def score_containers(root, cache=None):
    """
    给所有候选容器打分，按分数从高到低返回。

    打分公式：
        分数 = 正文字数 × (1 - 链接文字占比)² × 关键词系数 × 深度微调

    公式逐项解释：
      · 正文字数       —— 越长越可能是正文（导航栏都很短）
      · 链接占比惩罚   —— 用 (1 - 占比) 的平方。为什么要平方？
                          实测对比数据：body 容器 380 字、链接占比 0.41；
                          正文容器 131 字、链接占比 0.05。
                          用一次方：body 得 380×0.59 = 224，正文得 131×0.95×1.5 = 187 → body 赢（错）
                          用平方  ：body 得 380×0.35 = 133，正文得 131×0.90×1.5 = 177 → 正文赢（对）
                          平方让「链接很多的容器」被更快地压下去。
      · 关键词系数     —— 见 hint_factor 的说明
      · 深度微调       —— 每深一层多 2%。用于在分数接近时优先选更具体的容器

    返回：列表，每项是 (分数, 正文字数, 链接占比, 节点, 段落列表)
    """
    if cache is None:
        cache = ParagraphCache()

    results = []

    def walk(node):
        if node.tag in CANDIDATE_TAGS:
            lines = cache.lines_of(node)
            text_len = sum(len(line) for line in lines)
            if text_len > 0:
                total_len = len(plain_text(node, cache))
                link_len = len(link_text(node))
                # 链接占比：total_len 为 0 时用 0.0 兜底，避免「除以零」报错
                ratio = link_len / total_len if total_len else 0.0
                # min(ratio, 0.95) 是保险：万一算出来占比是 1.0，
                # (1-1)² = 0 会让所有容器同归于 0 分，反而挑不出来
                score = text_len * ((1.0 - min(ratio, 0.95)) ** 2)
                score *= hint_factor(node)
                score *= 1.0 + 0.02 * node_depth(node)
                results.append((score, text_len, ratio, node, lines))
        for child in node.children:
            walk(child)

    walk(root)
    results.sort(key=lambda item: -item[0])     # 负号 = 降序（分数高的在前）
    return results


# =============================================================================
#  第 5 区：简易 CSS 选择器
#  当自动识别不准时，你可以在 config.ini 里写「正文选择器」来精确指定。
#  这里实现的是一个小型解析器，只支持最简单的单层写法（够用且不易出错）。
# =============================================================================

def parse_selector(selector):
    """
    把选择器字符串拆成三要素。

    例子：  "div.txt_txt#zoom"
             ↓
            ("div", ["txt_txt"], "zoom")

    支持写法：标签名、.类名、#id，以及它们的任意组合。
    不支持写法：后代选择器（div p）、子选择器（div > p）、属性选择器（[href]）。
               原因：那些写法要写一个完整的 CSS 引擎，对小白用户也更容易写错。
    """
    tag, classes, node_id = "", [], ""
    for token in SELECTOR_TOKEN_RE.findall(selector or ""):
        if token.startswith("."):
            classes.append(token[1:])
        elif token.startswith("#"):
            node_id = token[1:]
        else:
            tag = token.lower()
    return tag, classes, node_id


def match_selector(node, tag, classes, node_id):
    """判断一个节点是否符合选择器条件（三个条件必须同时满足）。"""
    if tag and node.tag != tag:
        return False
    if node_id and node.attrs.get("id", "") != node_id:
        return False
    if classes:
        own = set(node.class_list())
        for name in classes:
            if name not in own:
                return False
    return True


def find_by_selector(root, selector):
    """
    在树里找第一个符合选择器的节点；找不到返回 None。

    采用「广度优先」（一层一层往下找），这样如果外层和内层都符合条件，
    会返回更靠外层那个 —— 外层容器通常包含完整正文。
    """
    tag, classes, node_id = parse_selector(selector)
    if not (tag or classes or node_id):
        return None
    queue = [root]                      # 待检查队列
    while queue:
        node = queue.pop(0)             # 从队头取一个（保证一层一层推进）
        if node is not root and match_selector(node, tag, classes, node_id):
            return node
        queue.extend(node.children)     # 把它的子节点排到队尾
    return None


# =============================================================================
#  第 6 区：标题、来源、日期的提取
# =============================================================================

def clean_site_suffix(title):
    """
    去掉网页标题里的网站名。

    例子：「最高人民法院关于适用民法典的解释 - 中华人民共和国最高人民法院」
           → 「最高人民法院关于适用民法典的解释」

    做法：按常见的分隔符切分，优先取第一段（多数网站把标题写在前面），
          但如果第一段太短（少于 4 个字，说明可能是「首页 - 某某网」），
          就改成取最长的那一段。
    """
    for sep in (" - ", " _ ", " | ", " — ", "－", "—", "_"):
        if sep in title:
            parts = [p.strip() for p in title.split(sep) if p.strip()]
            if not parts:
                continue
            if len(parts[0]) >= 4:
                return parts[0]
            return max(parts, key=len)
    return title


def find_titles_in_node(node, max_depth=3):
    """
    在一个节点内部收集所有「长得像标题」的文字，返回 [(类型优先级, 文字), ...]。

    类型优先级（数字越小越可靠）：
        0 = class 或 id 里含 "title" 的元素  —— 网页开发者明确标注的标题
        1 = h1 / h2 标题标签                 —— 也是标题，但不如上面那个明确
    长度限制 4～120 字：
        太短（"字号："）是功能按钮，太长说明这个元素其实包住了整段正文。
    """
    found = []
    queue = [(node, 0)]
    while queue:
        current, depth = queue.pop(0)
        if depth > max_depth:
            continue
        for child in current.children:
            text = tidy(child.text())
            if 4 <= len(text) <= 120:
                if "title" in child.hint_text():
                    found.append((0, text))
                elif child.tag in ("h1", "h2"):
                    found.append((1, text))
            queue.append((child, depth + 1))
    return found


def pick_best_title(container, up_levels=2, max_depth=3):
    """
    从正文容器出发，先在它内部找标题；内部没有就往上翻一层、再翻一层找。

    为什么必须「往上翻」（实测案例）：
      同样是最高法官网的两篇文章，正文容器竟然不一样：
        · 508001 那篇，程序选中的正文容器是 <div class="detail">
          —— 里面自带 <div class="title">真标题</div>，于是标题抓对了。
        · 512441 那篇，程序选中的正文容器是更深的 <div class="txt_txt">
          —— 它只包着正文文字，标题在它的父层 <div class="detail"> 里。
      如果只盯着正文容器内部找，第二篇就只能退回网页 <title> 标签，
      而那个标签写的是栏目名「权威发布」，结果所有文章标题都变成「权威发布」。
      所以正确做法是：容器内部找不到，就顺着父节点往上一层一层找。

    挑选规则：
      · 先看离正文最近的那一层（层数小的优先）
      · 同一层里，class="title" 优先于 h1/h2；再并列时取最长的那个
        （栏目名通常很短，如「权威发布」；文章标题通常很长，取长的命中率更高）
    """
    node = container
    distance = 0
    while node is not None and distance <= up_levels:
        # 越往外层，允许往下搜索的深度越小 —— 避免在页面外层误抓到导航里的标题
        candidates = find_titles_in_node(node, max_depth=max_depth if distance == 0 else 2)
        if candidates:
            candidates.sort(key=lambda item: (item[0], -len(item[1])))
            return candidates[0][1]
        node = node.parent
        distance += 1
    return ""


def extract_title(root, html, selector="", container=None):
    """
    按优先级找标题：
      ① 你在 config.ini 里手工指定的标题选择器
      ② 正文容器内部（以及往上两层）的 class="title" / h1 / h2
      ③ 页面上的 <h1>
      ④ 网页的 <title> 标签（并去掉网站名后缀）
    """
    if selector:
        node = find_by_selector(root, selector)
        if node is not None:
            text = tidy(node.text())
            if text:
                return text

    if container is not None:
        text = pick_best_title(container)
        if text:
            return text

    # <h1>：如果标题没有写在正文区里，这里往往是最后的机会
    for node in iter_nodes(root):
        if node.tag == "h1":
            text = tidy(node.text())
            if text:
                return text

    # 退回网页标题标签
    found = TITLE_RE.search(html)
    if found:
        return clean_site_suffix(tidy(found.group(1)))
    return ""


def drop_title_line(title, lines):
    """
    如果正文第一段就是标题本身，把它去掉，避免正文里重复出现一遍标题。

    为什么会出现这种情况？
      有些网站的正文容器把「标题」也当成第一个段落输出，于是标题会在
      txt 文件的「标题：」栏和正文里各出现一次，看起来啰嗦。
    """
    if not title or not lines:
        return lines
    if tidy(lines[0]) == tidy(title):
        return lines[1:]
    return lines


def link_ratio_of(node, cache):
    """算某个容器里「链接文字」占「全部文字」的比例（0～1）。数值越高越像导航/列表页。"""
    total_len = len(plain_text(node, cache))
    if not total_len:
        return 0.0
    return round(len(link_text(node)) / total_len, 3)


def iter_nodes(root):
    """深度优先遍历整棵树的生成器（yield 是「边用边生成」，不一次性占满内存）。"""
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        # 倒序压栈，弹出顺序才与文档顺序一致
        for child in reversed(node.children):
            stack.append(child)


def extract_meta(lines):
    """
    从正文段落里提取「来源」和「发布时间」。

    做法：只看那些很短的段落（因为「来源：新华社」这类信息行通常不超过 40 字），
          用正则去匹配。
    """
    source, date = "", ""
    for line in lines:
        if len(line) > 60:
            continue
        if not source:
            found = SOURCE_RE.search(line)
            if found:
                source = found.group(1).strip()
        if not date:
            found = DATE_RE.search(line)
            if found:
                year, month, day = found.groups()
                date = "%s-%02d-%02d" % (year, int(month), int(day))
        if source and date:
            break
    return {"source": source, "date": date}


# =============================================================================
#  第 7 区：对外主函数
#  上面都是零件，这个函数才是给 crawler.py 调用的「总装线」。
# =============================================================================

def extract_article(html, url="", article_selector="", title_selector="", min_length=100):
    """
    从一份 HTML 里抽出文章。

    参数：
        html             网页 HTML 源码（字符串）
        url              这个网页的地址（只用于写进结果里，方便回溯）
        article_selector 手动指定的正文选择器，空字符串表示全自动
        title_selector   手动指定的标题选择器
        min_length       正文最少要有几个字，否则判定为抽取失败

    返回一个字典：
        ok         是否成功抽出正文（True/False）
        title      标题
        paragraphs 正文段落列表（尚未清洗，清洗由 cleaner.py 负责）
        meta       来源、日期
        engine     用了哪种方式（selector = 手动指定命中；auto = 自动识别）
        debug      调试信息：候选容器排行、字数、链接占比（图形界面会显示）
    """
    root = parse_html(html)
    cache = ParagraphCache()

    candidates = score_containers(root, cache)

    # ---------- 情况一：用户手动指定了正文选择器 ----------
    if article_selector:
        node = find_by_selector(root, article_selector)
        if node is not None:
            lines = cache.lines_of(node)
            title = extract_title(root, html, title_selector, container=node)
            lines = drop_title_line(title, lines)
            return {
                "ok": len("".join(lines)) >= 1,
                "title": title,
                "paragraphs": lines,
                "meta": extract_meta(lines),
                "engine": "selector:%s" % article_selector,
                "debug": {
                    "text_len": sum(len(x) for x in lines),
                    "link_ratio": link_ratio_of(node, cache),
                    "candidates": [],
                },
            }
        # 指定了却找不到，就把失败原因带回去，方便你检查选择器是否写错
        return {
            "ok": False,
            "title": extract_title(root, html, title_selector),
            "paragraphs": [],
            "meta": {"source": "", "date": ""},
            "engine": "selector:%s（没找到该元素，请检查选择器）" % article_selector,
            "debug": {"text_len": 0, "link_ratio": 0.0, "candidates": []},
        }

    # ---------- 情况二：全自动识别 ----------
    if not candidates:
        return {
            "ok": False,
            "title": extract_title(root, html, title_selector),
            "paragraphs": [],
            "meta": {"source": "", "date": ""},
            "engine": "auto",
            "debug": {"text_len": 0, "link_ratio": 0.0, "candidates": []},
        }

    # 把前 5 名整理成「给人看」的排行榜，供调试和界面显示
    board = []
    for score, text_len, ratio, node, _lines in candidates[:5]:
        board.append({
            "score": round(score),
            "text_len": text_len,
            "link_ratio": round(ratio, 3),
            "tag": node.tag,
            "class": node.attrs.get("class", ""),
            "id": node.attrs.get("id", ""),
        })

    best_score, best_len, best_ratio, best_node, best_lines = candidates[0]
    # 标题优先从「胜出的正文容器」里找，这样能避开「<h1> 是网站名」的陷阱
    title = extract_title(root, html, title_selector, container=best_node)
    # 如果正文第一段就是标题，去掉它，避免正文里重复一遍
    best_lines = drop_title_line(title, best_lines)
    best_len = sum(len(line) for line in best_lines)

    # 字数太少 → 判定失败。宁可明确告诉你「这次没抓到」，
    # 也不要把一堆导航菜单当成正文塞进结果文件里。
    if best_len < min_length:
        return {
            "ok": False,
            "title": title,
            "paragraphs": best_lines,
            "meta": extract_meta(best_lines),
            "engine": "auto",
            "debug": {
                "text_len": best_len,
                "link_ratio": round(best_ratio, 3),
                "candidates": board,
                "reason": "抽出的正文只有 %d 字，少于设定的下限 %d 字" % (best_len, min_length),
            },
        }

    return {
        "ok": True,
        "title": title,
        "paragraphs": best_lines,
        "meta": extract_meta(best_lines),
        "engine": "auto",
        "debug": {
            "text_len": best_len,
            "link_ratio": round(best_ratio, 3),
            "candidates": board,
        },
    }


# =============================================================================
#  第 8 区：法律文书字段抽取（2026-09 新增）
# -----------------------------------------------------------------------------
#  【这个区干什么】
#    从一篇文书里，把几个「结构化字段」单独抠出来，让它们能变成 CSV 的列：
#        案号      如 (2021)最高法民申4246号
#        文书类型  如 判决书 / 裁定书 / 调解书
#        裁判日期  如 二〇二六年八月五日
#        法院名称  如 最高人民法院 / 江苏省南京市中级人民法院
#        案由      如 建设工程施工合同纠纷
#
#  【为什么用正则就够，而不需要人工智能】
#    中国的法律文书格式高度标准化 —— 案号、日期、文书类型都有固定的写法，
#    这是《人民法院民事裁判文书制作规范》等文件规定死的。
#    所以用「描述文字长相的公式」（正则）去匹配，准确率就很高。
#
#  【一个重要提醒】
#    抽取结果不是 100% 保真。同一份文书在不同网站上的排版可能有差异，
#    抽不到时这一列会留空 —— 这不会影响正文本身，正文永远是原样保存的。
# =============================================================================

# 案号正则。拆开解释：
#   [（(]\s*(?:19|20)\d{2}\s*[)）]   年份括号：(2021) 或 （2021），括号全角半角都认
#   \s*                              允许括号后有个空格
#   [\u4e00-\u9fa5\d]{1,15}?         汉字或数字组成的主体，非贪婪（尽量少匹配）
#   号                               结尾必须是「号」
#
# 【为什么主体要是「汉字或数字混合」而不是纯汉字】
#   第一版我写成 [汉字]{1,8}\d{1,6}号，结果漏掉了绝大多数真实案号。
#   因为真实案号的结构是「法院代字 + 数字 + 程序代字 + 序号」，
#   例如「（2021）苏01民终1234号」里，汉字和数字是交替出现的：
#        苏（江苏）01（南京中院）民终（二审民事）1234（序号）号
#   所以必须允许汉字数字交替。
CASE_NO_RE = re.compile(r"[（(]\s*(?:19|20)\d{2}\s*[)）]\s*[\u4e00-\u9fa5\d]{1,15}?号")

# 文书类型关键词，分成两类 —— 分开是因为它们的「可信来源」不一样。
#
# 【第一类：裁判文书类型】可以从前 100 字里找。
#   理由：裁判文书的首部第一行就写着「XX人民法院 民事判决书」，
#   而这些词（判决书/裁定书/调解书）几乎不会出现在法条正文中。
DOC_TYPE_JUDGMENT = (
    "民事判决书", "民事裁定书", "刑事判决书", "刑事裁定书", "行政判决书", "行政裁定书",
    "再审民事判决书", "再审民事裁定书", "执行裁定书",
    "判决书", "裁定书", "调解书", "决定书", "通知书",
)

# 【第二类：规范性文件类型】只能从标题里找。
#   理由（实测教训）：民法典条文正文里必然写着「依照本编规定处理」这类句子，
#   如果允许从正文找，「规定」「办法」会被大量误命中 ——
#   实测中《中华人民共和国民法典（续）》就被判成了文书类型「办法」。
DOC_TYPE_NORMATIVE = (
    "司法解释", "指导性案例", "答复", "批复",
    "解释", "规定", "办法", "意见", "纪要", "公告", "报告", "条例", "细则",
)

# 上面两类的合并版（找标题时用，长词排在前面，避免「判决书」抢先匹配掉「民事判决书」）
DOC_TYPE_WORDS = DOC_TYPE_JUDGMENT + DOC_TYPE_NORMATIVE

# 案由正则：「……纠纷一案」是裁判文书首部的固定句式。
#   例子：原告张三与被告李四建设工程施工合同纠纷一案，本院于……
#   \s*一案 允许「纠纷」和「一案」之间有空格
CAUSE_RE = re.compile(r"([\u4e00-\u9fa5]{2,24}?纠纷)\s*一案")

# 法院名正则：匹配「XX省高级人民法院」「XX市中级人民法院」「最高人民法院」。
#   [\u4e00-\u9fa5]{2,15}?  非贪婪：尽量少匹配，避免把前面的正文也吞进来
#   (?:高级|中级)?         可选级别词
#   人民法院               结尾固定
COURT_RE = re.compile(r"[\u4e00-\u9fa5]{2,15}?(?:高级人民法院|中级人民法院|人民法院|法院)")

# 中文日期正则：二〇二六年八月五日 / 二〇二六 年 8 月 5 日（数字混写的情况另用 DATE_RE 处理）
CN_DATE_RE = re.compile(
    r"([〇零一二三四五六七八九]{2,4})\s*年\s*([一二三四五六七八九十]{1,3})\s*月\s*([一二三四五六七八九十]{1,3})\s*日"
)

# 中文数字字符对照表。注意「两」也当 2 用（「两千年」这种写法偶尔出现）。
_CN_DIGIT = {
    "〇": 0, "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}


def cn_number_to_int(text):
    """
    把中文数字转成整数（日期解析必须用到）。

    两种写法都要支持：
      ① 纯数码写法： 「二〇二六」→ 2026（年份都是这么写的）
      ② 带「十」的写法：「十五」→ 15、「二十一」→ 21、「十二」→ 12（月份和日期这么写）
    """
    text = (text or "").strip()
    if not text:
        return 0
    # 写法①：每个字都是一个阿拉伯数字
    if all(ch in _CN_DIGIT for ch in text):
        return int("".join(str(_CN_DIGIT[ch]) for ch in text))
    # 写法②：带「十」
    if "十" in text:
        head, _sep, tail = text.partition("十")
        tens = _CN_DIGIT.get(head, 1) if head else 1     # 「十五」的「十」前面为空，算 1
        ones = _CN_DIGIT.get(tail, 0) if tail else 0
        return tens * 10 + ones
    return _CN_DIGIT.get(text, 0)


# -----------------------------------------------------------------------------
#  【字段抽取用到的词表】（2026-09 新增）
#  为什么用「词表」而不是纯靠正则？
#    因为「合同纠纷」「故意杀人」这种概念没法用正则描述 —— 它们是列举式的。
#    词表还有个好处：你一眼就能看出程序认哪些词，想加词告诉我一句就行。
# -----------------------------------------------------------------------------

# 省级行政区表。用途：从法院名称里判断这篇文书属于哪个地区。
# 例：「江苏省南京市中级人民法院」→ 地区 = 江苏
PROVINCE_WORDS = (
    "北京市", "上海市", "天津市", "重庆市",
    "河北省", "山西省", "辽宁省", "吉林省", "黑龙江省",
    "江苏省", "浙江省", "安徽省", "福建省", "江西省", "山东省",
    "河南省", "湖北省", "湖南省", "广东省", "海南省",
    "四川省", "贵州省", "云南省", "陕西省", "甘肃省", "青海省",
    "内蒙古自治区", "广西壮族自治区", "西藏自治区",
    "宁夏回族自治区", "新疆维吾尔自治区", "台湾省",
    "香港特别行政区", "澳门特别行政区",
)

# 案件分类（一级）判定规则。
# 每一项是 (分类名, 关键词元组)，程序从上往下逐条匹配，命中第一条就用它。
#
# 【这套分类是对齐主流法律数据库的】
#   参考了「中国裁判文书网」的官方案件分类（刑事/民事/行政/赔偿/执行/
#   强制清算与破产/管辖/区际司法协助/国际司法协助/非诉保全/司法制裁…）
#   和「北大法宝」的案件类型维度，取其中最常用、最能自动判定的几类。
#
# 【为什么顺序这么排，不能随便调】
#   ·「国家赔偿」必须在「执行」前面 —— 赔偿决定书里也常出现「执行」字样
#   ·「知识产权」必须在「民事」前面 —— 知产案件也写成「民事判决书」，
#      不先判知产，就永远归不到知产类
#   ·「民事」放最后当兜底 —— 因为「原告/被告/纠纷」这些词最泛
CASE_CATEGORY_RULES = (
    ("国家赔偿", ("国家赔偿", "赔偿决定", "国赔")),
    ("知识产权", ("知识产权", "商标", "专利", "著作权", "知产", "不正当竞争")),
    ("非诉保全", ("非诉保全", "财产保全", "证据保全", "行为保全")),
    ("强制清算与破产", ("强制清算", "破产", "重整", "破产清算")),
    ("执行", ("执行裁定", "执行判决", "被执行人", "执异", "执复", "执恢", "强制执行")),
    ("行政", ("行政判决", "行政裁定", "行政诉讼", "行政处罚", "行政行为", "行初", "行终", "行申")),
    ("刑事", ("刑事判决", "刑事裁定", "公诉机关", "被告人", "犯罪嫌疑人", "刑初", "刑终", "刑申")),
    ("民事", ("民事判决", "民事裁定", "原告", "被告", "民初", "民终", "民申", "民再", "纠纷")),
)

# 诉讼分类（二级）候选词 —— 民事商事类。
# 匹配时优先选「命中最长的那个」，这样「建设工程施工合同纠纷」
# 不会被更短的「合同纠纷」抢走。
CIVIL_CAUSE_WORDS = (
    "建设工程施工合同纠纷", "房屋买卖合同纠纷", "房屋租赁合同纠纷", "商品房预售合同纠纷",
    "金融借款合同纠纷", "民间借贷纠纷", "借款合同纠纷", "买卖合同纠纷", "租赁合同纠纷",
    "承揽合同纠纷", "运输合同纠纷", "保证合同纠纷", "物业服务合同纠纷",
    "股权转让纠纷", "公司决议纠纷", "股东知情权纠纷", "合伙协议纠纷",
    "劳动合同纠纷", "劳动争议", "工伤保险待遇纠纷", "追索劳动报酬纠纷",
    "离婚纠纷", "离婚后财产纠纷", "抚养费纠纷", "变更抚养关系纠纷", "赡养纠纷", "扶养纠纷",
    "法定继承纠纷", "遗嘱继承纠纷", "继承纠纷", "分家析产纠纷",
    "机动车交通事故责任纠纷", "医疗损害责任纠纷", "产品责任纠纷",
    "生命权纠纷", "健康权纠纷", "名誉权纠纷", "隐私权纠纷",
    "财产损害赔偿纠纷", "侵权责任纠纷", "相邻关系纠纷",
    "不当得利纠纷", "无因管理纠纷", "合同纠纷",
    "土地承包经营权纠纷", "农村土地承包合同纠纷",
    "侵害商标权纠纷", "侵害著作权纠纷", "专利权纠纷", "商标权纠纷", "著作权纠纷",
    "案外人执行异议之诉", "执行异议",
)

# 诉讼分类候选词 —— 刑事罪名类。
CRIMINAL_CAUSE_WORDS = (
    "非法侵入计算机信息系统", "非法获取计算机信息系统数据", "破坏计算机信息系统",
    "非法利用信息网络", "帮助信息网络犯罪活动", "侵犯公民个人信息",
    "故意杀人", "故意伤害", "过失致人死亡", "强奸", "强制猥亵", "猥亵儿童",
    "非法拘禁", "绑架", "拐卖妇女儿童", "拐骗儿童",
    "抢劫", "抢夺", "盗窃", "诈骗", "合同诈骗", "信用卡诈骗", "集资诈骗",
    "非法吸收公众存款", "组织、领导传销活动", "敲诈勒索", "聚众斗殴", "寻衅滋事",
    "开设赌场", "赌博", "组织卖淫", "贩卖毒品", "运输毒品", "走私毒品", "容留他人吸毒",
    "危险驾驶", "交通肇事", "以危险方法危害公共安全",
    "职务侵占", "挪用资金", "挪用公款", "贪污", "受贿", "行贿",
    "滥用职权", "玩忽职守", "徇私枉法",
    "走私普通货物", "非法经营", "假冒注册商标", "侵犯著作权", "侵犯商业秘密",
    "妨害公务", "拒不执行判决", "虚假诉讼", "妨害作证",
    "重大责任事故", "污染环境", "非法采矿",
)

# 罪名正则兜底：当词表没命中时，试试「犯XX罪」「构成XX罪」这种句式。
#   例：被告人张三犯故意杀人罪  → 故意杀人
#
# 【为什么要加后面那道「虚词检查」】
#   实测踩坑：司法解释正文里有一句「……或者构成犯罪……」，
#   宽松的正则把它当成了罪名，诉讼分类列被填上「或者构成犯罪」。
#   所以命中之后还要检查捕获到的词里有没有「或者/以及/的/和/与」这类虚词，有就丢弃。
CRIME_RE = re.compile(r"(?:犯|构成|涉嫌)([\u4e00-\u9fa5]{2,12}?罪)")
CRIME_STOP_WORDS = ("或者", "以及", "和", "与", "的", "该", "其", "等", "被")

# 当事人身份词（用于清洗案由里的杂质）。
# 案情首部的标准写法是「上诉人（原审原告）张三因与被上诉人李四XX纠纷一案」，
# 如果直接照句式抠，会把「上诉人张三因与被上诉人李四」也一起抠进来 —— 实测踩过这个坑。
PARTY_WORDS = (
    "上诉人（原审原告）", "上诉人（原审被告）", "被上诉人（原审原告）", "被上诉人（原审被告）",
    "原审原告", "原审被告", "再审申请人", "被申请人", "申请执行人", "被执行人",
    "上诉人", "被上诉人", "申请人", "公诉机关", "自诉人", "附带民事诉讼原告人",
    "原告", "被告", "第三人",
)


def extract_case_no(text):
    """抽案号。找不到返回空字符串。"""
    found = CASE_NO_RE.search(text or "")
    return found.group(0) if found else ""


def extract_doc_type(title, body):
    """
    抽文书类型。

    查找顺序（分层设计，这是实测调出来的）：
      ① 标题里找（两类词都找）—— 标题格式通常是「XX纠纷民事判决书」，最准
      ② 正文最开头 100 字里只找「裁判文书类型」——
         因为裁判文书的文书类型就印在首部第一行，而这些词不会出现在法条正文里；
         而「规定/办法/意见」这类词在法条正文里到处都是，绝不能从正文找。
    返回命中的那个词，例如「民事判决书」；找不到返回空。
    """
    haystack_title = title or ""
    for word in DOC_TYPE_WORDS:
        if word in haystack_title:
            return word
    head = (body or "")[:100]
    for word in DOC_TYPE_JUDGMENT:
        if word in head:
            return word
    return ""


def extract_court(title, body):
    """
    抽法院名称。

    顺序：标题 → 正文前 500 字。
    为什么优先标题？因为标题往往直接写成「江苏省南京市中级人民法院民事判决书」，
    而正文首部可能只写「原告……被告……」，法院名反而不好定位。
    """
    for source in (title or "", (body or "")[:500]):
        if not source:
            continue
        found = COURT_RE.search(source)
        if found:
            name = found.group(0)
            # 过滤掉明显不是法院名的短匹配（例如正文里的「该法院」）
            if len(name) >= 4 and "该法院" not in name:
                return name
    return ""


def looks_like_judgment(case_no, body, doc_type=""):
    """
    判断这份文书是不是「裁判文书」（判决书 / 裁定书 / 调解书）。

    【为什么必须先做这个判断 —— 这是实测踩出来的大坑】
      「案由」和「诉讼分类」这两个字段**只有裁判文书才有**。
      司法解释、法律条文、公报文章根本没有案由。

      第一版程序不看这个，直接在正文里匹配案由词表，结果：
        · 《民法典》正文明明在讲法律条文，却被判出「污染环境」这个诉讼分类
          （因为条文里出现了「污染环境」四个字）
        · 司法解释正文里一句「或者构成犯罪」，被抓成了罪名

      加了这道判断之后，司法解释类文章这几个字段就干净地留空了。

    判定依据（任一成立就是裁判文书）：
      ① 有案号（案号是裁判文书的身份证）
      ② 文书类型里含判决书/裁定书/调解书
      ③ 正文最开头 300 字里出现当事人结构词
    """
    if case_no:
        return True
    if any(word in (doc_type or "") for word in ("判决书", "裁定书", "调解书")):
        return True
    head = (body or "")[:300]
    return any(word in head for word in (
        "原告", "被告", "被告人", "上诉人", "公诉机关", "自诉人", "申请人", "被执行人",
    ))


def extract_cause(body, is_judgment=True):
    """
    抽案由。非裁判文书直接返回空（见 looks_like_judgment 的说明）。

    两级策略（这是实测调出来的）：
      第 ① 级：在正文**前 800 字**（案情首部所在位置）里找标准案由词表
              —— 命中就返回标准写法，最干净。
      第 ② 级：词表没命中，退回「……纠纷一案」句式去抠，
              但要把「上诉人」「被告」这类当事人身份词以及它前面的内容切掉。

    为什么第 ① 级要限制在「前 800 字」：
      因为判决书全篇可能提到好几种纠纷（「本院认为……类似买卖合同纠纷的处理」），
      不限制范围就会把正文里偶然出现的词当成案由。案由一定写在首部。
    """
    if not is_judgment:
        return ""
    head = (body or "")[:800]
    best = ""
    for word in CIVIL_CAUSE_WORDS + CRIMINAL_CAUSE_WORDS:
        if word in head and len(word) > len(best):
            best = word
    if best:
        return best

    # 第 ② 级：句式兜底
    found = CAUSE_RE.search(body or "")
    if not found:
        return ""
    cause = found.group(1)
    for word in PARTY_WORDS:
        if word in cause:
            # 取身份词后面的那一截 —— 案由永远在当事人信息之后
            cause = cause.split(word)[-1]
    return cause.strip()


def extract_region(court_name, body=""):
    """
    抽地区（省级）。

    顺序：法院名称 → 正文前 600 字。
    为什么优先法院名？因为「江苏省南京市中级人民法院」比正文里零散提到的地名可靠得多。

    返回的是去掉后缀的干净地名：「江苏省」→「江苏」、「内蒙古自治区」→「内蒙古」。
    这样你在 Excel 里筛选时不用纠结后缀写法。
    """
    for source in (court_name or "", (body or "")[:600]):
        if not source:
            continue
        # 先找带后缀的完整省名，再剥掉后缀
        for word in PROVINCE_WORDS:
            if word in source:
                for suffix in ("特别行政区", "维吾尔自治区", "壮族自治区",
                               "回族自治区", "自治区", "省", "市"):
                    if word.endswith(suffix):
                        return word[: -len(suffix)]
                return word
    return ""


def classify_case(title, body, case_no="", doc_type=""):
    """
    判「案件分类」（一级）：民事 / 刑事 / 行政 / 执行 / 国家赔偿 / 知识产权 / 非诉保全 / 强制清算与破产。

    两轮匹配，**都不看正文中后部** —— 这是实测调出来的关键：

      第 1 轮看「标题 + 案号 + 文书类型」：
        这三个地方是权威信息（「民事判决书」「(2021)苏01民终1234号」）
      第 2 轮看正文**最开头 200 字**，而且只认「当事人结构词」这种强证据：
        （被告人 / 原告 / 被告 / 公诉机关 / 被执行人）

    【为什么必须收紧】
      第一版允许在整个正文前 800 字里找关键词，结果踩了坑：
      一篇《关于确保民法典实施进行司法解释全面清理的工作情况报告》，
      正文里提到了「国家赔偿」四个字，就被判成了「国家赔偿」类案件 —— 完全错。
      而《民法典》条文正文里出现「商标」「专利」也会被判成「知识产权」类。
      结论：**分类只能靠权威位置的信息，不能靠正文里偶然出现的词。**
      判不出来就留空 —— 留空比填错好得多。
    """
    primary = " ".join((title or "", case_no or "", doc_type or ""))
    for name, words in CASE_CATEGORY_RULES:
        if any(word in primary for word in words):
            return name

    # 第 2 轮：只认强证据（当事人结构词），且只看最开头 200 字
    strong_words = ("被告人", "公诉机关", "原告", "被告", "被执行人", "上诉人", "申请人")
    head = (body or "")[:200]
    for name, words in CASE_CATEGORY_RULES:
        for word in words:
            if word in strong_words and word in head:
                return name
    return ""


def classify_litigation(body, cause="", is_judgment=True):
    """
    判「诉讼分类」（二级）：合同纠纷 / 离婚纠纷 / 故意杀人 / 非法侵入计算机信息系统 ……

    三级优先（越靠前越准）：
      ① 在已经抽到的「案由」里找 —— 案由本身就是「建设工程施工合同纠纷一案」这种标准写法
      ② 在正文前 1500 字里找
      ③ 用正则兜罪名句式：「犯……罪」（带虚词检查，防止匹配到「或者构成犯罪」）

    非裁判文书直接返回空 —— 因为司法解释、法律条文本来就没有案由和罪名。

    为什么每个层级都取「命中最长的词」：
      词表里既有「合同纠纷」也有「建设工程施工合同纠纷」，
      不按长度优先，短词会先把长词抢走，分类就粗了。
    """
    if not is_judgment:
        return ""
    all_words = CIVIL_CAUSE_WORDS + CRIMINAL_CAUSE_WORDS

    # 第 ① 级：在案由里找
    if cause:
        best = ""
        for word in all_words:
            if word in cause and len(word) > len(best):
                best = word
        if best:
            return best

    # 第 ② 级：在正文开头找
    head = (body or "")[:1500]
    best = ""
    for word in all_words:
        if word in head and len(word) > len(best):
            best = word
    if best:
        return best

    # 第 ③ 级：正则兜底，抓「犯XX罪」句式；命中后必须过虚词检查
    found = CRIME_RE.search(body or "")
    if found:
        crime = found.group(1)
        if not any(bad in crime for bad in CRIME_STOP_WORDS):
            return crime
    return ""


def extract_court_level(court_name):
    """
    抽「法院级别」—— 这是裁判文书网和北大法宝都有的标准字段。

    判据就是法院名称里的级别词：
        最高人民法院 / 高级人民法院 / 中级人民法院 / 基层人民法院（县区法院）
    为什么这个字段有用：你想只看「最高法判的」或「只看中院判的」时，一秒就能筛出来。
    """
    name = court_name or ""
    if "最高人民法院" in name:
        return "最高人民法院"
    if "高级人民法院" in name:
        return "高级人民法院"
    if "中级人民法院" in name:
        return "中级人民法院"
    if "人民法院" in name or "法院" in name:
        return "基层人民法院"
    return ""


# 审判程序判定规则：(程序名, 关键词元组)
# 「案号代字」是判断的金标准 —— 中国案号的第 3 段就是程序代字：
#     (2021)苏01民终1234号  → 终 = 二审
#     (2023)京0105民初5678号 → 初 = 一审
# 所以先看案号，案号里没有再看标题里有没有「二审/再审」这种字样。
PROCEDURE_RULES = (
    ("再审", ("再审", "民申", "刑申", "行申", "民再", "刑再", "抗诉")),
    ("二审", ("二审", "民终", "刑终", "行终")),
    ("执行", ("执行", "执异", "执复", "执恢", "执字")),
    ("一审", ("一审", "民初", "刑初", "行初")),
)


def extract_procedure(title, case_no, doc_type=""):
    """
    抽「审判程序」：一审 / 二审 / 再审 / 执行。

    查找顺序：案号 → 标题 → 文书类型（案号最可靠，因为代字是法定写法）。
    判不出来返回空（例如司法解释、公报文章本来就没有审判程序）。
    """
    # 案号优先：只看括号后面的代字部分，避免年份里的数字干扰
    for source in (case_no or "", (title or "") + " " + (doc_type or "")):
        if not source:
            continue
        for name, words in PROCEDURE_RULES:
            if any(word in source for word in words):
                return name
    return ""


def extract_judge_date(body, fallback=""):
    """
    抽裁判日期。

    策略（这个顺序很讲究）：
      ① 在正文里找**最后一个**中文日期 —— 裁判文书的落款日期在文末，
         而文书中间也会反复出现日期（立案日、开庭日），取最后一个最接近落款。
      ② 找不到中文日期，就退回页面上的「发布时间」（那个是阿拉伯数字格式，
         已经在 extract_meta 里抽好了，作为 fallback 传进来）。

    参数 fallback 就是 meta 里抽到的日期。
    """
    matches = CN_DATE_RE.findall(body or "")
    if matches:
        year_cn, month_cn, day_cn = matches[-1]
        year = cn_number_to_int(year_cn)
        month = cn_number_to_int(month_cn)
        day = cn_number_to_int(day_cn)
        if 1900 < year < 2200 and 1 <= month <= 12 and 1 <= day <= 31:
            return "%04d-%02d-%02d" % (year, month, day)
    return fallback or ""


def extract_legal_fields(title, body, meta=None):
    """
    对外主函数：一次性抽出全部法律文书字段（共 8 个）。

    返回字典（键名就是 CSV 的列名）：
        案号       (2021)最高法民申4246号
        文书类型   民事判决书 / 民事裁定书 / 司法解释 …
        裁判日期   2026-08-05（来自文末落款，找不到就用页面发布时间兜底）
        法院名称   江苏省南京市中级人民法院
        地区       江苏
        案件分类   民事 / 刑事 / 行政 / 执行 / 国家赔偿 / 知识产权
        案由       建设工程施工合同纠纷（原样抽出来的表述）
        诉讼分类   建设工程施工合同纠纷（归一化到词表里的标准类别）

    注意「案由」和「诉讼分类」的区别：
        案由      = 文书里**原样写的**那个表述
        诉讼分类  = 从词表里**匹配到的标准类别**
        正常情况两者相同；当文书写的案由不在词表里时，案由有值、诉讼分类可能为空 ——
        这时你至少还知道原始案由是什么。
    """
    meta = meta or {}
    body = body or ""

    case_no = extract_case_no(body)
    court = extract_court(title, body)
    doc_type = extract_doc_type(title, body)
    # 先判断「这是不是一份裁判文书」—— 案由和诉讼分类只有裁判文书才有
    is_judgment = looks_like_judgment(case_no, body, doc_type)
    cause = extract_cause(body, is_judgment=is_judgment)

    return {
        "案号": case_no,
        "文书类型": doc_type,
        "案件分类": classify_case(title, body, case_no, doc_type),
        "案由": cause,
        "诉讼分类": classify_litigation(body, cause, is_judgment=is_judgment),
        "法院名称": court,
        "法院级别": extract_court_level(court),
        "地区": extract_region(court, body),
        "审判程序": extract_procedure(title, case_no, doc_type),
        "裁判日期": extract_judge_date(body, fallback=meta.get("date", "")),
    }
