# -*- coding: utf-8 -*-
"""
===============================================================================
 fetcher.py —— 抓取区（整个项目的「手」）
===============================================================================
 【这个文件负责什么】
   给它一个网址（或一个你保存下来的网页文件），
   它负责把内容取回来，并正确地解码成能读的中文。

 【为什么「解码」是件大事】
   计算机存的其实是字节，不是文字。同一段中文，用不同编码存出来的字节完全不同：
        「法」字 用 UTF-8 存 = 3 个字节；用 GBK 存 = 2 个字节
   如果拿错编码去解，读出来就是「娉曘€」这样的乱码。
   中国老一批政府网站（含你给我的那份附件）用的是 GB2312/GBK，
   新网站基本都用 UTF-8，所以必须自动判断。

 【本文件的分区】
   第 1 区  常量与请求头        —— 定义「我们装成什么浏览器」
   第 2 区  编码识别            —— 判断这份字节该用哪种编码读
   第 3 区  网络下载            —— 真正去把网页抓回来（含重试、代理、超时）
   第 4 区  本地文件读取        —— 读你手动保存的网页（抓 JS 网站时的兜底办法）
   第 5 区  可插拔引擎注册表    —— 预留扩展位：以后加浏览器引擎只需注册一个新函数
   第 6 区  robots 协议检查     —— 可选的「礼貌开关」
   第 7 区  统一入口 load()     —— 不管网址还是文件，都从这一个口子进
===============================================================================
"""

import gzip            # 标准库：解压 gzip 压缩的网页（很多服务器会压缩传输以省流量）
import io              # 标准库：把字节流包装成「文件对象」，喂给 gzip 解压用
import os              # 标准库：判断路径是否存在、读本地文件
import ssl             # 标准库：控制 HTTPS 证书校验行为
import time            # 标准库：计时，用于统计每页耗时
import urllib.error    # 标准库：网络异常类型（URLError / HTTPError）
import urllib.parse    # 标准库：网址拼接、解析
import urllib.request  # 标准库：发起 HTTP 请求（本项目唯一的联网手段）
import urllib.robotparser  # 标准库：解析 robots.txt


# =============================================================================
#  第 1 区：常量与请求头
# =============================================================================

# 我们要伪装成的浏览器身份。
# 为什么必须伪装？很多网站会检查 User-Agent（用户代理，即「你是什么浏览器」），
# 如果发现是 Python 脚本，就直接返回 403 拒绝。实测中：
#   国务院政策文件库（gov.cn/zhengce/zhengceku）不带浏览器头就是 403 拒绝访问。
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 默认请求头。逐项说明它们的作用：
#   User-Agent      我是谁（伪装成 Chrome）
#   Accept          我能接收什么类型的内容（浏览器都这么写）
#   Accept-Language 我优先要中文内容（有些网站会据此返回不同语言版本）
#   Connection      用长连接，一次 TCP 连接上多传点数据，稍微快一点
#   注意：这里故意不声明 Accept-Encoding，避免服务器返回 Brotli(br) 压缩格式
#         （Python 标准库解不了 br，需要额外库）。gzip 我们下面自己处理。
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
}

# 单个网页最多读取多少字节（5 MB）。
# 作用：防止程序被一个巨大的文件（例如北大法宝首页有 1.5 MB，有些页面几十 MB）
#       拖死内存。超过这个量就只读前面一部分。
MAX_BYTES = 5 * 1024 * 1024

# 常见的中文网页编码候选，按「出现频率」排序。
# 说明：gb18030 是 gb2312 和 gbk 的超集 —— 用 gb18030 去解 gb2312 编码的页面
#       一定能正确解出，所以判断到 GB 系时统一用 gb18030 最保险。
GB_FAMILY = ("gb2312", "gbk", "gb18030", "gb_2312-80")


# =============================================================================
#  第 2 区：编码识别
# =============================================================================

# 从 HTML 源码里找编码声明，例如：
#   <meta http-equiv="Content-Type" content="text/html; charset=gb2312">
#   <meta charset="utf-8">
# 说明：b'...' 表示这是「字节串」而不是普通字符串，因为我们要在未解码的原始字节里找。
META_CHARSET_RE = None  # 具体的正则在函数里初始化（要用到 re 模块）


def sniff_charset(raw, http_charset="", html_head="", default="utf-8"):
    """
    判断这份字节应该用哪种编码来读。返回编码名（如 'utf-8' 或 'gb18030'）。

    判断顺序（这是「从最可靠到最不可靠」排列的）：
      1. HTML 源码里声明的 charset（最可靠，因为页面自己说了它是什么编码）
      2. HTTP 响应头里的 charset（服务器说的，偶尔会说错或干脆不说）
      3. 猜：先试严格 UTF-8，能解开就用 UTF-8
      4. 猜：再用 GB18030 试，能解开就用 GB18030
      5. 都不行，退回默认编码并用「忽略错误」的方式硬解（保证程序不崩）

    为什么把「HTML 源码声明」放在 HTTP 头之前？
      实战经验：很多国内网站的 HTTP 头写的是 utf-8，但页面实际是 gb2312。
      页面源码里的 meta 声明反而更准。
    """
    # 1) 页面源码里的声明
    if html_head:
        found = _META_CHARSET_RE.search(html_head)
        if found:
            return _normalize_charset(found.group(1))
    # 2) HTTP 响应头
    if http_charset:
        return _normalize_charset(http_charset)
    # 3) 严格试 UTF-8
    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    # 4) 试 GB18030
    try:
        raw.decode("gb18030")
        return "gb18030"
    except UnicodeDecodeError:
        pass
    # 5) 兜底
    return default


def _normalize_charset(name):
    """把五花八门的编码写法统一成 Python 认识的规范名字。"""
    name = (name or "").strip().strip('"\'').lower()
    if not name:
        return "utf-8"
    if name in ("utf8",):
        return "utf-8"
    if name in GB_FAMILY:
        # 统一升格为 gb18030（超集，兼容性最好）
        return "gb18030"
    if name in ("big5", "big5-hkscs"):
        return "big5"          # 繁体中文站（港澳台）常用
    return name


def decode_bytes(raw, http_charset="", default="utf-8"):
    """
    把字节解码成字符串，并同时返回「用了哪种编码」和「页面开头的文字」。

    技术要点：为什么先只看开头 4000 字节找 meta？
      HTML 的编码声明必须出现在前 1024 字节内（规范要求），
      但现实中有网站写得很靠后，所以放宽到 4000 字节。
      而且这段字节此刻还没解码，只能用「字节正则」去找。
    """
    head = raw[:4000]
    try:
        head_text = head.decode("ascii", "ignore")
    except Exception:
        head_text = ""
    charset = sniff_charset(raw, http_charset, head_text, default)
    try:
        text = raw.decode(charset, "replace")
    except LookupError:
        # 网站声明了一个 Python 不认识的编码名（例如 "unknown"），退回 UTF-8
        charset = "utf-8"
        text = raw.decode(charset, "replace")
    return text, charset


def _init_regex():
    """
    延迟初始化正则表达式。

    为什么不直接写在文件顶部？
      为了让「第 1 区」只放常量、不依赖其它模块的初始化顺序，
      也避免在 import 阶段做多余的编译工作。这是个工程习惯问题，功能上等价。
    """
    global _META_CHARSET_RE
    import re
    # 匹配规则说明：
    #   <meta            字面量
    #   [^>]*            「非 > 的任意字符」出现任意次（跳过中间其它属性）
    #   charset\s*=\s*   charset = （允许等号周围有空格）
    #   ["\']?           可选的引号
    #   ([\w-]+)         捕获组：编码名（字母数字和连字符）
    _META_CHARSET_RE = re.compile(r"<meta[^>]*charset\s*=\s*[\"']?([\w-]+)", re.I)


_init_regex()


# =============================================================================
#  第 3 区：网络下载
# =============================================================================

class FetchResult:
    """
    抓取结果：一个小盒子，装着「这次抓取的完整情况」。

    为什么用类而不是直接返回字符串？
      因为一次抓取除了「内容」以外，还有很多信息需要一起带回来：
      成功了没有、最终地址是什么（可能被重定向）、耗时多少、失败原因是什么。
      把它们打包成一个对象，调用方就不用记「第几个返回值是什么」。
    """

    __slots__ = ("ok", "url", "final_url", "status", "html", "encoding",
                 "error", "elapsed", "engine", "from_file")

    def __init__(self, ok, url, final_url="", status=0, html="", encoding="",
                 error="", elapsed=0.0, engine="urllib", from_file=False):
        self.ok = ok                  # True/False：这次成功了吗
        self.url = url                # 你给的那个地址/文件路径
        self.final_url = final_url    # 最终地址（跟随跳转之后的真实地址）
        self.status = status          # HTTP 状态码（200 = 正常，403 = 被拒绝，404 = 不存在）
        self.html = html              # 网页源码（已经解码成中文）
        self.encoding = encoding      # 实际使用的编码
        self.error = error            # 失败时的原因说明（给人看的白话）
        self.elapsed = elapsed        # 耗时（秒）
        self.engine = engine          # 用了哪个抓取引擎
        self.from_file = from_file    # 是本地文件还是网址


def build_opener(use_proxy=False, proxy_url="", ignore_cert=False, follow_redirect=True):
    """
    组装一个「下载器」（urllib 把它叫做 opener，可以理解为配置好的浏览器实例）。

    为什么要组装而不是直接 urllib.request.urlopen？
      因为我们要往里面装「处理器」：
        · 代理处理器   —— 让请求经过你本机的 Clash 代理（127.0.0.1:7890）
        · HTTPS 处理器 —— 控制证书校验（遇到证书有问题的老政府站时可选择跳过）
        · 重定向处理器 —— 控制最多跟随多少次跳转，防止陷入死循环
    """
    handlers = []

    # 代理：只有开启时才装。格式必须是 http://主机:端口
    if use_proxy and proxy_url:
        handlers.append(urllib.request.ProxyHandler({
            "http": proxy_url,
            "https": proxy_url,
        }))

    # HTTPS 处理器
    if ignore_cert:
        # 跳过证书校验（安全等级降低，但能救回一部分证书配置有问题的站点）
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=context))

    if not follow_redirect:
        # 不跟随跳转的写法：自定义一个「什么都不做」的重定向处理器
        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        handlers.append(_NoRedirect())

    return urllib.request.build_opener(*handlers)


def _decompress(raw, content_encoding):
    """
    解压服务器压缩过的内容。

    原理：为了省流量，服务器常用 gzip 把网页压小再发（能压到 1/4）。
    浏览器收到后自动解压，我们也要自己解开，否则拿到的是乱码二进制。
    """
    encoding = (content_encoding or "").lower()
    try:
        if "gzip" in encoding:
            return gzip.decompress(raw)
        if "deflate" in encoding:
            # deflate 有两种包装格式（zlib 带头 和 裸 deflate），先试前者再试后者
            try:
                return gzip.decompress(raw)
            except Exception:
                import zlib
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception:
        # 解压失败就把原始数据返回，让后面的解码环节去处理，避免整个程序崩掉
        return raw
    return raw


def fetch_url(url, timeout=20, retries=3, use_proxy=False, proxy_url="",
              ignore_cert=False, extra_headers=None):
    """
    下载一个网址，返回 FetchResult。

    重试逻辑（这是稳定性的关键）：
      · 网络抖动、连接被重置、超时 —— 这些是「临时故障」，重试往往就成功 → 重试
      · 404 网页不存在、403 被拒绝  —— 这些是「确定性故障」，重试也没用     → 不重试
      · 429 请求太频繁、5xx 服务器错误 —— 属于「可能过一会就好」          → 重试
    每次重试前等待的时间依次为 1 秒、2 秒、3 秒（逐渐拉长，给服务器喘气的时间）。
    """
    headers = dict(DEFAULT_HEADERS)
    if extra_headers:
        headers.update(extra_headers)

    opener = build_opener(use_proxy, proxy_url, ignore_cert)
    last_error = ""
    started = time.time()

    for attempt in range(1, max(1, retries) + 1):
        try:
            request = urllib.request.Request(url, headers=headers)
            with opener.open(request, timeout=timeout) as response:
                raw = response.read(MAX_BYTES)
                raw = _decompress(raw, response.headers.get("Content-Encoding"))
                # 从响应头里找 charset（例如 text/html; charset=gb2312）
                content_type = response.headers.get("Content-Type", "")
                http_charset = ""
                if "charset=" in content_type.lower():
                    http_charset = content_type.lower().split("charset=", 1)[1].split(";")[0].strip()
                html, encoding = decode_bytes(raw, http_charset)
                return FetchResult(
                    ok=True, url=url, final_url=response.geturl(),
                    status=getattr(response, "status", 200), html=html,
                    encoding=encoding, elapsed=time.time() - started,
                )
        except urllib.error.HTTPError as exc:
            # HTTPError 是「服务器答复了，但答复的是一个错误状态码」
            last_error = "HTTP %s %s" % (exc.code, exc.reason)
            if exc.code in (400, 401, 403, 404, 405, 410, 451):
                break           # 确定性错误，不必重试，直接跳出
        except urllib.error.URLError as exc:
            # URLError 是「压根没连上」：DNS 解析失败、连接超时、SSL 握手失败等
            last_error = _explain_network_error(exc)
        except Exception as exc:                      # 其它意外情况也不让程序崩
            last_error = "%s: %s" % (type(exc).__name__, exc)

        if attempt < retries:
            time.sleep(attempt)                        # 退避等待：1 秒、2 秒、3 秒

    return FetchResult(
        ok=False, url=url, error=last_error, elapsed=time.time() - started,
    )


def _explain_network_error(exc):
    """
    把 Python 的英文报错翻译成小白能看懂的中文，并给出可行的建议。

    这一步很重要：直接丢给用户「SSL: UNEXPECTED_EOF_WHILE_READING」等于没说，
    用户需要知道「该怎么办」。
    """
    text = str(getattr(exc, "reason", exc))
    lower = text.lower()
    if "ssl" in lower or "certificate" in lower:
        return ("SSL/证书握手失败（网站拒绝了我们的连接，或被网络设备干扰）。"
                " 建议：① 在 config.ini 里把「使用代理」改成「是」再试；"
                " ② 打开浏览器确认这个网址能正常打开；③ 换成「保存网页→离线解析」的方式。")
    if "timed out" in lower or "timeout" in lower:
        return "连接超时（网站响应太慢或网络不通）。建议：调大 config.ini 里的「超时秒数」。"
    if "name or service not known" in lower or "getaddrinfo" in lower:
        return "域名解析失败（网址可能拼错了，或本站的域名已失效）。"
    if "connection refused" in lower or "reset" in lower:
        return "连接被拒绝或重置（对方服务器主动断开，通常是反爬机制）。建议：换用「保存网页→离线解析」方式。"
    return text


# =============================================================================
#  第 4 区：本地文件读取（抓 JS 动态网站时的兜底办法）
# =============================================================================

def read_local_file(path):
    """
    读取你手动保存下来的网页文件，返回 FetchResult。

    使用场景（很重要，请记住这条路）：
      遇到裁判文书网、威科先行、北大法宝这类「JS 动态渲染」或「必须登录」的网站时，
      自动抓取是拿不到内容的。这时可以手动操作：
        1. 用你自己的浏览器打开那篇文书（你有权限看的页面，浏览器能正常显示）
        2. 按 Ctrl+S → 保存类型选「网页，仅 HTML」
        3. 把保存下来的 .html 文件丢进本项目的 samples 文件夹
        4. 在图形界面里选择这个文件，程序照样能解析出正文
      原理：浏览器保存下来的 HTML 已经是「渲染后」的完整内容，
           里面就包含了我们要的正文文字。

    编码处理：本地文件同样会遇到 GB2312 老网页，所以走同一套识别逻辑。
    """
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        return FetchResult(ok=False, url=path, error="文件不存在：%s" % path, from_file=True)

    started = time.time()
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_BYTES)
    except OSError as exc:
        return FetchResult(ok=False, url=path, error="读取文件失败：%s" % exc, from_file=True)

    html, encoding = decode_bytes(raw)
    return FetchResult(
        ok=True, url=path, final_url=path, status=0, html=html,
        encoding=encoding, elapsed=time.time() - started, from_file=True,
    )


# =============================================================================
#  第 5 区：可插拔引擎注册表（为未来的升级预留的接口）
# =============================================================================

# 这是一个「引擎名 → 抓取函数」的对照表。

# 为什么这样设计？
#   你以后说「我要能直接抓 JS 网站」，我只需要：
#     ① 新写一个 browser_engine.py
#     ② 在这里调用 register_engine("browser", 抓取函数)
#   其它所有代码（解析、清洗、输出、界面）一行都不用改，它们只认 FetchResult 这个格式。
#   这就是「可插拔」的含义：零件可以换，但接口不变。
ENGINE_REGISTRY = {
    "urllib": fetch_url,     # 当前唯一启用的引擎，纯标准库
}


def register_engine(name, func):
    """
    注册一个新引擎。

    参数 func 必须是一个函数，签名要满足：
        func(url, timeout=..., retries=..., use_proxy=..., proxy_url=...) -> FetchResult
    也就是「进去一个网址，出来一个 FetchResult」。
    """
    ENGINE_REGISTRY[name] = func


def get_engine(name):
    """按名字取引擎；找不到就退回 urllib（保证程序永远有得用，不会因为配置写错而崩溃）。"""
    return ENGINE_REGISTRY.get(name, ENGINE_REGISTRY["urllib"])


# =============================================================================
#  第 6 区：robots 协议检查（可选开关）
# =============================================================================

def robots_allows(url, user_agent=USER_AGENT, timeout=10):
    """
    问一句：网站的 robots.txt 允许我们抓这个地址吗？

    背景知识：robots.txt 是网站放在根目录的一个纯文本文件，
    告诉爬虫「哪些目录可以抓、哪些不要抓」。它是行业惯例，不是法律。

    返回三态：
        True  = 允许
        False = 明确禁止
        None  = 查不到（没有 robots.txt 或读取失败）→ 交给调用方决定怎么办

    注意坑点：有些网站的 robots.txt 故意返回一个网页（而不是纯文本），
    甚至返回 404 错误页。这种情况我们当作「查不到」处理，不阻塞抓取。
    """
    try:
        parsed = urllib.parse.urlsplit(url)
        robots_url = "%s://%s/robots.txt" % (parsed.scheme, parsed.netloc)
        request = urllib.request.Request(robots_url, headers={"User-Agent": user_agent})
        opener = build_opener()
        with opener.open(request, timeout=timeout) as response:
            data = response.read(256 * 1024)
        text = data.decode("utf-8", "ignore")
        # 明显不是一个 robots 文件（比如是 HTML 页面）→ 视为查不到
        if "<html" in text[:2000].lower():
            return None
        parser = urllib.robotparser.RobotFileParser()
        parser.parse(text.splitlines())
        return parser.can_fetch(user_agent, url)
    except Exception:
        return None


# =============================================================================
#  第 7 区：统一入口
# =============================================================================

def looks_like_url(text):
    """
    判断用户给的是「网址」还是「本地文件路径」。
    规则很简单：以 http:// 或 https:// 开头的就是网址。
    """
    return text.strip().lower().startswith(("http://", "https://"))


def load(source, timeout=20, retries=3, use_proxy=False, proxy_url="",
         ignore_cert=False, engine="urllib"):
    """
    统一入口：不管给的是网址还是本地文件路径，都从这里进，都返回 FetchResult。

    这样做的好处：crawler.py 不用写两套逻辑（一套处理网址、一套处理文件），
    只需要写一句 result = fetcher.load(用户输入) 就够了。
    """
    source = (source or "").strip().strip('"').strip("'")
    if not source:
        return FetchResult(ok=False, url=source, error="没有填写网址或文件路径。")

    if looks_like_url(source):
        return get_engine(engine)(
            source, timeout=timeout, retries=retries,
            use_proxy=use_proxy, proxy_url=proxy_url, ignore_cert=ignore_cert,
        )
    return read_local_file(source)
