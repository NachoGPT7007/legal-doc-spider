# -*- coding: utf-8 -*-
"""
===============================================================================
 cleaner.py —— 清洗区（整个项目的「筛子」）
===============================================================================
 【这个文件负责什么】
   解析器（parser.py）把正文「抽」出来了，但它抽出来的东西往往还混着杂质：
     · 「责任编辑：张三」「来源：新华网」「打印本页」「字号：大 中 小」
     · 「上一篇：……」「下一篇：……」这类翻页导航
     · 「京ICP备05023036号」「版权所有」这类页脚信息
     · 重复出现的同一句话（导航条被抽进来时经常这样）
     · 夹在正文里的原文链接

   这个文件负责把这些杂质去掉，并且按你配置的规则做筛选。

 【清洗的底线原则：宁可留着，也不误删】
   法律文书是严肃文本，多留一行废话只是难看，删错一行可能就是事故。
   所以本文件的清洗规则都很保守：
     · 只删「整行」并且「很短」且「明确命中杂质词」的行
     · 绝不改写句子内部的内容
     · 绝不删除正文里的数字、法条编号、判决主文

 【本文件的分区】
   第 1 区  内置杂质词表        —— 程序自带的「常见垃圾行」清单
   第 2 区  单行判断与规整      —— 一行文字要不要删 / 怎么规整
   第 3 区  整篇段落清洗        —— 对一整篇正文逐行过筛
   第 4 区  页面级筛选          —— 按关键词决定「这一页要不要」
   第 5 区  对外主函数          —— 给 crawler.py 调用的总装线
===============================================================================
"""

import re       # 正则：匹配网址、规整空白


# =============================================================================
#  第 1 区：内置杂质词表
# =============================================================================

# 这些词一旦出现在「短行」里，基本可以断定这行不是正文，而是网页的功能性文字。
# 每一条都是实测中真实出现过的（多个法院、政府网站）。
NOISE_KEYWORDS = (
    # —— 编辑与来源信息（这些信息我们已经单独存进「来源/日期」字段了，正文里不必重复）
    "责任编辑", "编辑：", "作者：", "供稿", "通讯员",
    "来源：", "来源:", "文章来源", "发布时间", "发布日期", "更新时间",
    # —— 阅读辅助工具条
    "打印本页", "打印本文", "字号：", "字号:", "分享到", "分享：",
    "关闭窗口", "返回顶部", "返回首页", "大 中 小", "【字体：",
    # —— 翻页导航
    "上一篇", "下一篇", "上一条", "下一条", "上一篇：", "下一篇：",
    # —— 相关推荐（正文之外的列表）
    "相关阅读", "相关文章", "相关推荐", "推荐阅读", "延伸阅读", "猜你喜欢",
    # —— 免责与版权（法律网站底部必备）
    "版权所有", "免责声明", "copyright", "京icp备", "京公网安备",
    "沪icp备", "粤icp备", "浙icp备", "网站地图", "联系我们", "关于我们",
    "主办单位", "承办单位", "技术支持", "政府网站标识码",
    # —— 社交引导
    "扫描二维码", "微信扫一扫", "关注微信", "关注微博", "点击查看全文",
    "阅读全文", "查看更多", "更多内容",
    # —— 页面框架
    "所在位置：", "当前位置：", "您现在的位置", "首页 >", "首页>",
)

# 这些是「行首特征」：一行以这些词开头，且很短，就判定为杂质。
# 用行首判断是因为「来源」这类词也可能出现在正文里（例如「事故来源不明」），
# 但只要它出现在行首、且整行很短，就基本可以确定是元信息行。
NOISE_LINE_PREFIXES = (
    "来源", "编辑", "责任编辑", "作者", "发布时间", "发布日期", "更新时间",
    "字号", "打印", "分享", "上一篇", "下一篇", "相关阅读", "相关文章",
    "免责声明", "版权所有", "主办", "承办", "技术支持",
)

# 「长行豁免阈值」：
# 一行如果超过这个长度，就算命中杂质词也不删。
# 原因：正文里完全可能出现「本院认为，来源不明的证据不能采信……」这种长句子，
#       它含有「来源」二字，但它是货真价实的正文。短行才是杂质。
LONG_LINE_EXEMPT = 50


# =============================================================================
#  第 2 区：单行判断与规整
# =============================================================================

def normalize_line(line):
    """
    把一行文字规整一下。做三件事：
      1. 把连续空白（含全角空格 \u3000）压成一个普通空格
      2. 去掉首尾空白
      3. 去掉行首的装饰符号（·、•、>、-、| 等网页残留的项目符号）

    注意：这里只动「空白和首尾装饰」，绝对不动文字本身的内容。
    """
    line = re.sub(r"\s+", " ", line).strip()
    # 行首装饰符号清理：^ 表示行首，[...] 里是要清理的符号集合，+ 表示一个或多个
    line = re.sub(r"^[·•●◆■□▶►>\-|、~\s]+", "", line)
    return line.strip()


def is_noise_line(line, extra_noise=()):
    """
    判断这一行是不是「杂质行」，返回 True（是杂质，该删）或 False（是正文，保留）。

    判断顺序（依次尝试，命中一条即可判定）：
      1. 空行 → 杂质
      2. 太长的行 → 一律不是杂质（见上面的「长行豁免」说明）
      3. 命中内置杂质词表 → 杂质
      4. 命中用户额外补充的杂质词 → 杂质
      5. 以元信息词开头且很短（不超过 30 字）→ 杂质
      6. 其它 → 正文，保留
    """
    if not line:
        return True

    # 第 2 步：长行豁免。法律文书的正文段落动辄上百字，不会被误伤。
    if len(line) > LONG_LINE_EXEMPT:
        return False

    lowered = line.lower()          # 转小写再比，这样 "Copyright" 和 "copyright" 都能匹配
    for word in NOISE_KEYWORDS:
        if word in lowered:
            return True
    for word in extra_noise:
        word = (word or "").strip()
        if word and word.lower() in lowered:
            return True

    # 第 5 步：行首特征 + 必须很短
    if len(line) <= 30:
        for prefix in NOISE_LINE_PREFIXES:
            if line.startswith(prefix):
                return True
    return False


def has_url(line):
    """判断这一行里是否含网址（用于「删除含网址的段落」这个可选功能）。"""
    return bool(re.search(r"https?://|www\.[a-zA-Z0-9-]+\.[a-zA-Z]{2,}", line))


# =============================================================================
#  第 3 区：整篇段落清洗
# =============================================================================

def clean_paragraphs(paragraphs, extra_noise=(), drop_duplicates=True,
                     remove_url_lines=False):
    """
    对一整篇正文的所有段落做清洗，返回 (清洗后的段落列表, 清洗报告)。

    处理流程（顺序很重要）：
      第 1 步 规整每一行（空白、装饰符号）
      第 2 步 判杂质，删掉杂质行
      第 3 步 可选：删掉含网址的行
      第 4 步 可选：去重
      第 5 步 收尾：去掉首尾多余的空白段

    参数说明：
      paragraphs       paragraph 列表（parser 抽出来的原始段落）
      extra_noise      你在 config.ini 里补充的杂质词
      drop_duplicates  是否丢掉完全重复的段落
      remove_url_lines 是否删掉含网址的段落
    """
    report = {"原始段落数": len(paragraphs), "删除杂质行": 0, "删除含网址行": 0,
              "删除重复行": 0, "保留段落数": 0}

    kept = []
    seen = set()            # 集合：记录已经出现过的段落，用于去重（集合查询是瞬间完成的）

    for raw_line in paragraphs:
        line = normalize_line(raw_line)
        if not line:
            continue

        # 第 2 步：杂质判断
        if is_noise_line(line, extra_noise):
            report["删除杂质行"] += 1
            continue

        # 第 3 步：可选删网址行
        if remove_url_lines and has_url(line):
            report["删除含网址行"] += 1
            continue

        # 第 4 步：可选去重
        if drop_duplicates:
            # 用「去掉了所有空格」的版本做指纹，这样排版差异不会导致漏判重复
            fingerprint = re.sub(r"\s+", "", line)
            if fingerprint in seen:
                report["删除重复行"] += 1
                continue
            seen.add(fingerprint)

        kept.append(line)

    # 第 5 步：去掉开头的空段（有些网站正文前面会有一堆空行残留）
    while kept and not kept[0].strip():
        kept.pop(0)

    report["保留段落数"] = len(kept)
    return kept, report


def join_paragraphs(paragraphs):
    """
    把段落列表拼成一段可读的完整正文。
    每段之间空一行（这是中文长文最舒服的排版方式）。
    """
    return "\n\n".join(paragraphs)


# =============================================================================
#  第 4 区：页面级筛选
# =============================================================================

def split_keywords(text):
    """
    把配置里的关键词字符串拆成列表。

    支持多种分隔方式，因为小白用户可能用中文逗号、英文逗号、空格或换行：
        "判决书,裁定书"   → ['判决书', '裁定书']
        "判决书，裁定书"  → ['判决书', '裁定书']
        "判决书 裁定书"   → ['判决书', '裁定书']
    """
    if not text:
        return []
    parts = re.split(r"[,，;；\s]+", text.strip())
    return [p for p in (item.strip() for item in parts) if p]


def page_passes_filter(title, body_text, must_have="", exclude=""):
    """
    判断这一页要不要保留。返回 (是否保留, 原因说明)。

    规则（两条独立的关卡）：
      关卡一「必须包含」：如果配置了必须包含关键词，那么标题或正文里
                        必须至少出现其中一个词，否则丢弃。
      关卡二「排除」    ：如果标题或正文里出现任何一个排除词，就丢弃。

    为什么用「或」的关系而不是「与」？
      因为你的目的是「把想要的那几类文书捞出来」，
      要求同时含两个不同关键词会几乎筛不到东西。
    """
    haystack = (title + "\n" + body_text).lower()

    must_list = split_keywords(must_have)
    if must_list:
        if not any(word.lower() in haystack for word in must_list):
            return False, "不含必须包含的关键词（%s）" % "、".join(must_list)

    exclude_list = split_keywords(exclude)
    for word in exclude_list:
        if word.lower() in haystack:
            return False, "命中排除关键词（%s）" % word

    return True, ""


# =============================================================================
#  第 5 区：对外主函数
# =============================================================================

def clean_article(article, extra_noise="", drop_duplicates=True, remove_url_lines=False):
    """
    给 crawler.py 调用的总装线：把 parser 的结果清洗一遍。

    参数 article 是 parser.extract_article 返回的字典。
    返回一个新的字典（不修改原字典），多出这几个字段：
        paragraphs  清洗后的段落
        body        清洗后拼好的完整正文
        clean_report 清洗报告（删了多少行、为什么）
    """
    noise_words = split_keywords(extra_noise) if extra_noise else []
    cleaned, report = clean_paragraphs(
        article.get("paragraphs", []),
        extra_noise=noise_words,
        drop_duplicates=drop_duplicates,
        remove_url_lines=remove_url_lines,
    )
    result = dict(article)          # 复制一份，避免影响调用方手里的原对象
    result["paragraphs"] = cleaned
    result["body"] = join_paragraphs(cleaned)
    result["char_count"] = len(re.sub(r"\s+", "", result["body"]))
    result["clean_report"] = report
    return result
