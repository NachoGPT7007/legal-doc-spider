# -*- coding: utf-8 -*-
"""
===============================================================================
 exporter.py —— 输出区（整个项目的「笔」）
===============================================================================
 【这个文件负责什么】
   把抓取清洗好的结果写成文件，让你能直接打开阅读、筛选、分享。

 【为什么不做成「全部塞进一个 CSV」？】
   实测经验：一份判决书正文动辄几千到几万字，而 Excel 的单个单元格
   最多只能装 32767 个字符，超出的部分会被直接截断 —— 等于悄悄丢数据。
   所以本程序采用「索引表 + 单篇正文」的双轨方案：
     · 索引.csv   —— 一行一篇，装标题、网址、字数、时间等短字段，方便排序筛选
     · 正文/*.txt —— 每篇文书一个独立文本文件，全文完整保存，双击就能读
     · 结果.json  —— 结构化数据，给以后用程序再加工用
     · 汇编.md    —— 所有正文拼成一个大文件，方便一次性阅读和转发

 【本文件的分区】
   第 1 区  文件名安全化        —— 把标题变成一个合法的 Windows 文件名
   第 2 区  输出目录准备        —— 每次运行生成一个带时间的独立文件夹
   第 3 区  写 CSV 索引表
   第 4 区  写单篇正文 txt
   第 5 区  写 JSON
   第 6 区  写汇编 Markdown
   第 7 区  对外主函数 export_all
===============================================================================
"""

import csv          # 标准库：写 CSV 表格文件（Excel 能直接打开）
import json         # 标准库：写 JSON 结构化数据
import os           # 标准库：建文件夹、拼路径
import re           # 标准库：清洗文件名里的非法字符
import time         # 标准库：生成时间戳


# =============================================================================
#  第 1 区：文件名安全化
# =============================================================================

# Windows 文件名里绝对不能出现的字符：\ / : * ? " < > |
# 因为在 Windows 上这些符号有特殊含义（例如 \ 是路径分隔符）。
ILLEGAL_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]')

# Windows 保留设备名：这些名字不能用作文件名，否则系统会闹脾气。
# 例如你把文件命名成 "CON.txt"，Windows 会认为你在操作控制台设备。
RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}


def safe_filename(name, max_length=60, fallback="未命名"):
    """
    把任意一段文字（通常是文章标题）变成一个合法、安全、够短的文件名。

    处理步骤：
      1. 去掉换行、制表符，把连续空白压成一个空格
      2. 把非法字符全部替换成下划线
      3. 去掉首尾的空格和点（Windows 不允许文件名以点结尾）
      4. 如果太长就截断（防止整个路径超过 Windows 的 260 字符上限）
      5. 如果撞上保留名就加后缀
      6. 如果变成空字符串就用兜底名
    """
    text = re.sub(r"\s+", " ", (name or "")).strip()
    text = ILLEGAL_FILENAME_CHARS.sub("_", text)
    text = text.strip(" .")
    if len(text) > max_length:
        text = text[:max_length].rstrip(" .")
    if text.upper() in RESERVED_NAMES:
        text += "_文件"
    return text or fallback


# =============================================================================
#  第 2 区：输出目录准备
# =============================================================================

def make_output_dir(base_dir, source, label=""):
    """
    创建一个本次运行专属的输出文件夹，返回它的完整路径。

    命名规则：  年月日_时分秒_来源标记
    例子：      output/20260925_201530_最高人民法院官网/

    为什么要带时间戳？
      这样你每次运行的结果都各占一个文件夹，不会互相覆盖。
      想对比不同时间抓的结果（比如看某个网站有没有更新）就很方便。

    技术要点：os.makedirs(..., exist_ok=True) 会一路创建不存在的父目录；
    如果目标已存在也不报错，保证程序不会因为「文件夹已存在」而中断。
    """
    stamp = time.strftime("%Y%m%d_%H%M%S")
    # 从网址里取一个能看懂的名字：去掉协议头，取域名或文件名
    tag = label or source or "结果"
    tag = re.sub(r"^https?://", "", tag)
    tag = tag.split("/")[0]                       # 取域名部分
    tag = safe_filename(tag, max_length=30, fallback="结果")
    folder = os.path.join(base_dir, "%s_%s" % (stamp, tag))
    os.makedirs(folder, exist_ok=True)
    return folder


# =============================================================================
#  第 3 区：写 CSV 索引表
# =============================================================================

# CSV 的列标题。顺序就是你在 Excel 里看到的从左到右的顺序。
#
# 【列顺序的设计依据】
#   前 12 列是「案件属性」，顺序对齐主流法律数据库（裁判文书网 / 北大法宝）的字段习惯：
#       先编号（案号、文书类型），再分类（案件分类、案由、诉讼分类），
#       最后机构与时间（法院名称、法院级别、地区、审判程序、裁判日期）
#   中间是「体量指标」（字数、段落数、链接占比）—— 用来判断抓得对不对
#   最后是「溯源信息」（抓取时间、正文文件、网址、正文开头、抽取方式、状态）
#
# 【哪些列是程序自动抽的，抽不到就留空】
#   案号 / 文书类型 / 案件分类 / 案由 / 诉讼分类 / 法院名称 / 法院级别 / 地区 /
#   审判程序 / 裁判日期
CSV_COLUMNS = [
    "序号", "标题", "来源", "发布时间",
    "案号", "文书类型", "案件分类", "案由", "诉讼分类",
    "法院名称", "法院级别", "地区", "审判程序", "裁判日期",
    "字数", "段落数", "链接占比",
    "抓取时间", "正文文件", "网址", "正文开头", "抽取方式", "状态",
]


def write_index_csv(records, path):
    """
    把结果写成索引 CSV。

    关键技术点：encoding="utf-8-sig"
      普通 UTF-8 文件在 Excel 里打开会变成乱码（Excel 默认按 GBK 解码）。
      而 utf-8-sig 会在文件开头多写 3 个特殊字节（叫 BOM），
      Excel 一看到这个标记就知道「这是 UTF-8」，中文就正常显示了。

    关键技术点：newline=""
     不加这个参数，Windows 上的 csv 模块会在每行后面多写一个空行。
    """
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        for item in records:
            preview = re.sub(r"\s+", " ", item.get("body", ""))[:100]
            writer.writerow([
                item.get("index", ""),
                item.get("title", ""),
                item.get("source", ""),
                item.get("date", ""),
                item.get("案号", ""),
                item.get("文书类型", ""),
                item.get("案件分类", ""),
                item.get("案由", ""),
                item.get("诉讼分类", ""),
                item.get("法院名称", ""),
                item.get("法院级别", ""),
                item.get("地区", ""),
                item.get("审判程序", ""),
                item.get("裁判日期", ""),
                item.get("char_count", 0),
                len(item.get("paragraphs", [])),
                item.get("link_ratio", 0),
                item.get("fetch_time", ""),
                item.get("txt_file", ""),
                item.get("url", ""),
                preview,
                item.get("engine", ""),
                item.get("status", ""),
            ])
    return path


# =============================================================================
#  第 4 区：写单篇正文 txt
# =============================================================================

def build_txt_content(item):
    """
    组装一个单篇 txt 文件的内容。

    为什么要在正文前面加一段「信息头」？
      因为 txt 文件被单独转发出去以后，就脱离了原始网页。
      头部的标题、网址、来源、抓取时间就是它的「身份证」，
      以后你回头查证某句话的出处时，靠的就是这几行。
    """
    lines = []
    lines.append("=" * 60)
    lines.append("标题：%s" % item.get("title", "（无标题）"))
    lines.append("网址：%s" % item.get("url", ""))
    if item.get("source"):
        lines.append("来源：%s" % item["source"])
    if item.get("date"):
        lines.append("发布/文书日期：%s" % item["date"])
    # 下面这几行是 2026-09 新增的「法律文书字段」，字段顺序对齐裁判文书网/北大法宝，
    # 抽不到就整行省略，不占地方
    if item.get("案号"):
        lines.append("案号：%s" % item["案号"])
    if item.get("文书类型"):
        lines.append("文书类型：%s" % item["文书类型"])
    if item.get("案件分类"):
        lines.append("案件分类：%s" % item["案件分类"])
    if item.get("案由"):
        lines.append("案由：%s" % item["案由"])
    if item.get("诉讼分类"):
        lines.append("诉讼分类：%s" % item["诉讼分类"])
    if item.get("法院名称"):
        lines.append("法院名称：%s" % item["法院名称"])
    if item.get("法院级别"):
        lines.append("法院级别：%s" % item["法院级别"])
    if item.get("地区"):
        lines.append("地区：%s" % item["地区"])
    if item.get("审判程序"):
        lines.append("审判程序：%s" % item["审判程序"])
    if item.get("裁判日期"):
        lines.append("裁判日期：%s" % item["裁判日期"])
    lines.append("字数：%s" % item.get("char_count", 0))
    lines.append("抓取时间：%s" % item.get("fetch_time", ""))
    lines.append("抽取方式：%s" % item.get("engine", ""))
    lines.append("=" * 60)
    lines.append("")
    lines.append(item.get("body", ""))
    lines.append("")
    return "\n".join(lines)


def write_article_files(records, folder):
    """
    给每一篇正文写一个独立的 txt 文件。

    文件命名：序号_标题.txt
      加序号是为了让文件在文件夹里按抓取顺序排列，
      不加序号的话，Windows 会按标题拼音排序，顺序就乱了。
    """
    os.makedirs(folder, exist_ok=True)
    written = 0
    for item in records:
        if not item.get("ok"):
            continue                       # 抓失败的页面不生成正文文件
        index = item.get("index", 0)
        title = safe_filename(item.get("title", ""), max_length=60, fallback="未命名")
        filename = "%03d_%s.txt" % (index, title)
        full_path = os.path.join(folder, filename)
        try:
            with open(full_path, "w", encoding="utf-8") as handle:
                handle.write(build_txt_content(item))
        except OSError:
            # 万一文件名还是有问题（例如路径太长），退化成只用序号命名，
            # 保证「不丢数据」优先于「文件名好看」
            filename = "%03d.txt" % index
            full_path = os.path.join(folder, filename)
            with open(full_path, "w", encoding="utf-8") as handle:
                handle.write(build_txt_content(item))
        item["txt_file"] = filename
        written += 1
    return written


# =============================================================================
#  第 5 区：写 JSON
# =============================================================================

def write_json(records, path):
    r"""
    写结构化数据文件。

    技术要点：ensure_ascii=False
      json.dump 默认会把所有中文转成 \uXXXX 这种转义形式（例如「法」写成 \u6cd5），
      文件能读但没法用眼睛看。设成 False 就会原样写中文。
    技术要点：indent=2
      让文件有缩进换行，人类可读（缺点是文件大一些，但我们不差这点空间）。
    """
    # 过滤掉内部字段（以 _ 开头的键，例如 _html 保存的原始网页源码）。
    # 不滤掉的话，一份 JSON 里会混进几十页的网页源码，文件变得又大又没法看。
    public_records = [
        {key: value for key, value in item.items() if not key.startswith("_")}
        for item in records
    ]
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(public_records, handle, ensure_ascii=False, indent=2)
    return path


# =============================================================================
#  第 6 区：写汇编 Markdown
# =============================================================================

def write_compilation(records, path, source=""):
    """
    把所有正文拼成一个大 Markdown 文件，方便一次性阅读、搜索和转发。

    Markdown 是一种极简的排版语法：用 # 表示标题、空行表示分段。
    它在记事本里是普通文字，在支持 Markdown 的软件里（如 Typora、微信读书、
    GitHub、各种笔记软件）就会渲染成带排版的漂亮文档。
    """
    lines = []
    ok_items = [item for item in records if item.get("ok")]
    lines.append("# 法律文书抓取汇编")
    lines.append("")
    lines.append("- 抓取来源：%s" % (source or "（未记录）"))
    lines.append("- 生成时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    lines.append("- 成功篇数：%d" % len(ok_items))
    lines.append("")
    lines.append("---")
    lines.append("")

    for item in ok_items:
        lines.append("## %d. %s" % (item.get("index", 0), item.get("title", "（无标题）")))
        lines.append("")
        meta_bits = []
        if item.get("source"):
            meta_bits.append("来源：%s" % item["source"])
        if item.get("date"):
            meta_bits.append("日期：%s" % item["date"])
        meta_bits.append("字数：%s" % item.get("char_count", 0))
        lines.append("> " + "　|　".join(meta_bits))
        lines.append("")
        lines.append("原文链接：%s" % item.get("url", ""))
        lines.append("")
        lines.append(item.get("body", ""))
        lines.append("")
        lines.append("---")
        lines.append("")

    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    return path


def write_report(records, path, source="", settings=None):
    """
    写一份「抓取说明.txt」，记录本次运行的参数和结果概览。

    为什么需要它？
      过一个月你再打开结果文件夹，很可能已经忘了当时是怎么抓的。
      这份小文件就是留给未来的你的备忘录。
    """
    ok_items = [item for item in records if item.get("ok")]
    failed = [item for item in records if not item.get("ok")]
    lines = []
    lines.append("本次抓取说明")
    lines.append("=" * 60)
    lines.append("抓取来源：%s" % (source or "（未记录）"))
    lines.append("完成时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    lines.append("尝试页面：%d 个" % len(records))
    lines.append("成功页面：%d 个" % len(ok_items))
    lines.append("失败/跳过：%d 个" % len(failed))
    lines.append("成功正文合计字数：%d" % sum(item.get("char_count", 0) for item in ok_items))
    lines.append("")
    if settings:
        lines.append("本次使用的参数：")
        for key, value in settings.items():
            lines.append("  %s = %s" % (key, value))
        lines.append("")
    if failed:
        lines.append("失败/跳过明细（前 30 条）：")
        for item in failed[:30]:
            lines.append("  · [%s] %s" % (item.get("status", "未知原因"), item.get("url", "")))
        lines.append("")
    lines.append("温馨提示：")
    lines.append("  · 索引.csv 可以用 Excel 打开，按「字数」或「发布时间」排序看最方便")
    lines.append("  · 正文 文件夹里是每一篇的完整全文")
    lines.append("  · 汇编.md 是所有正文拼成的大文件，适合整份阅读或转发")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    return path


# =============================================================================
#  第 7 区：对外主函数
# =============================================================================

def export_all(records, base_dir, source="", generate_json=True,
               generate_compilation=True, settings=None, label=""):
    """
    一次性导出全部结果。返回一个字典，说明各个文件的位置。

    执行顺序：
      1. 创建本次专属的输出文件夹
      2. 先写每篇正文 txt（因为写完才知道文件名，索引表里要记录这个文件名）
      3. 再写索引 CSV（此时 txt 文件名已经填进每条记录了）
      4. 可选写 JSON 和汇编
      5. 最后写说明文件
    """
    folder = make_output_dir(base_dir, source, label=label)

    # 第 2 步：正文文件（必须最先做，索引表要引用它的文件名）
    article_dir = os.path.join(folder, "正文")
    count = write_article_files(records, article_dir)

    # 第 3 步：索引表
    csv_path = write_index_csv(records, os.path.join(folder, "索引.csv"))

    result = {
        "folder": folder,
        "csv": csv_path,
        "articles": article_dir,
        "article_count": count,
    }

    if generate_json:
        result["json"] = write_json(records, os.path.join(folder, "结果.json"))
    if generate_compilation:
        result["compilation"] = write_compilation(
            records, os.path.join(folder, "汇编.md"), source=source)

    write_report(records, os.path.join(folder, "抓取说明.txt"),
                 source=source, settings=settings)
    return result
