# -*- coding: utf-8 -*-
"""
===============================================================================
 cli.py —— 命令行入口（不开窗口的用法）
===============================================================================
 【这个文件有什么用】
   图形界面很好用，但有两种情况你会需要命令行版：
     1. 想一次抓很多个网址（写个循环就自动跑完）
     2. 想把抓取排进计划任务，每天自动执行
     3. 出问题时排查 —— 命令行能看到最完整的报错信息

 【怎么用】（在项目文件夹里打开命令行，然后执行）
   基础用法：
       python cli.py https://www.court.gov.cn/fabu.html

   常用参数：
       python cli.py 网址 --pages 10        最多抓 10 页
       python cli.py 网址 --interval 3      每页间隔 3 秒（更礼貌）
       python cli.py 网址 --only-entry      只抓这一页，不钻链接
       python cli.py 网址 --selector div.txt_txt    手动指定正文区域
       python cli.py 网址 --must 判决书,裁定书       只保留含这些词的页面
       python cli.py 网址 --any-site        允许跟到别的网站
       python cli.py 保存的网页.html         直接解析本地文件

   查看全部参数：
       python cli.py --help
===============================================================================
"""

import argparse       # 标准库：自动生成「命令行参数解析」，还能生成 --help 说明
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from spider import crawler      # noqa: E402


def build_parser():
    """
    定义命令行参数的说明书。

    技术说明：argparse 会自动根据这些定义做三件事：
      ① 解析你输入的命令行    ② 检查类型对不对    ③ 生成 --help 帮助文本
    """
    parser = argparse.ArgumentParser(
        description="法律文书抓取器（命令行版）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例子：\n"
            "  python cli.py https://www.court.gov.cn/fabu.html\n"
            "  python cli.py https://www.court.gov.cn/fabu.html --pages 10 --interval 2\n"
            "  python cli.py samples/某文书.html --only-entry\n"
        ),
    )
    # 位置参数：要抓的网址 / 文件路径。
    # nargs="?" 表示「可以不给」—— 因为检索模式下地址是由模板拼出来的，不需要你手动给。
    parser.add_argument("source", nargs="?", default="",
                        help="要抓取的网址，或本地保存的 .html 文件路径（检索模式下可不填）")

    # —— 检索模式（2026-09 新增）——
    parser.add_argument("--search-template", default="",
                        help="检索网址模板，含 {占位符}。例如：\n"
                             "  --search-template \"http://www.court.gov.cn/search.html?content={关键词}\"\n"
                             "  --search-template \"http://gongbao.court.gov.cn/QueryArticle.html"
                             "?title={标题}&content={全文}&document_number={文号}\"")
    parser.add_argument("--search-field", action="append", default=[], metavar="名字=值",
                        help="检索词，格式 名字=值，可重复。例如：\n"
                             "  --search-field 关键词=建设工程施工合同\n"
                             "  --search-field 标题=司法解释 --search-field 文号=法释")

    parser.add_argument("--pages", type=int, default=30,
                        help="最多抓几页（默认 30）")
    parser.add_argument("--interval", type=float, default=1.5,
                        help="每页之间的等待秒数（默认 1.5）")
    parser.add_argument("--only-entry", action="store_true",
                        help="只抓入口页这一页，不跟进链接")
    parser.add_argument("--any-site", action="store_true",
                        help="允许跟进其它网站的链接（默认只抓同一网站）")
    parser.add_argument("--selector", default="",
                        help="手动指定正文的 CSS 选择器，例如 div.txt_txt")
    parser.add_argument("--min-length", type=int, default=100,
                        help="正文最少字数，少于它就算抽取失败（默认 100）")
    parser.add_argument("--must", default="",
                        help="必须包含关键词（针对正文和标题），逗号分隔，例如：判决书,裁定书")
    parser.add_argument("--exclude", default="",
                        help="排除关键词（针对正文和标题），逗号分隔，例如：招聘,采购")
    parser.add_argument("--link-must", default="",
                        help="只跟进「链接文字含这些词」的链接，逗号分隔，例如：解释,规定,批复")
    parser.add_argument("--link-exclude", default="",
                        help="链接文字含这些词就跳过，逗号分隔，例如：招聘,公告,公示")
    parser.add_argument("--no-paging", action="store_true",
                        help="关闭自动翻页（默认开启：会跟随「更多/下一页」把整个栏目翻一遍）")
    parser.add_argument("--list-pages", type=int, default=20,
                        help="最多翻多少个列表页（默认 20；配合 --pages 一起决定最终能抓到多少篇）")
    parser.add_argument("--proxy", default="",
                        help="使用代理，例如 http://127.0.0.1:7890")
    parser.add_argument("--timeout", type=int, default=20,
                        help="单页超时秒数（默认 20）")
    parser.add_argument("--retries", type=int, default=3,
                        help="失败重试次数（默认 3）")
    parser.add_argument("--out", default="output",
                        help="结果输出目录（默认 output）")
    return parser


def parse_search_fields(pairs):
    """
    把命令行的 --search-field 参数解析成字典。

    输入形如：["关键词=建设工程", "文号=法释"]
    输出形如：{"关键词": "建设工程", "文号": "法释"}

    容错处理：如果某个参数忘了写「=」，就把它整个当成值、名字记为「关键词」，
    这样至少不会静默丢掉你填的内容。
    """
    result = {}
    for item in pairs:
        if "=" in item:
            name, _sep, value = item.partition("=")
            result[name.strip()] = value.strip()
        else:
            result["关键词"] = item.strip()
    return result


def main(argv=None):
    """命令行主流程：解析参数 → 组装配置 → 跑 → 报告结果。"""
    args = build_parser().parse_args(argv)

    search_values = parse_search_fields(args.search_field)
    search_mode = bool(args.search_template)

    if not search_mode and not args.source:
        print("错误：要么提供一个网址，要么用 --search-template 指定检索模板。")
        print("用 python cli.py --help 看完整说明。")
        return 2

    if search_mode and not search_values:
        print("错误：检索模式下必须至少给一个检索词，例如 --search-field 关键词=建设工程")
        return 2

    config = crawler.CrawlConfig(
        source=args.source,
        search_mode=search_mode,
        search_template=args.search_template,
        search_values=search_values,
        max_pages=1 if args.only_entry else max(1, args.pages),
        interval=max(0.0, args.interval),
        same_site_only=not args.any_site,
        timeout=args.timeout,
        retries=args.retries,
        use_proxy=bool(args.proxy),
        proxy_url=args.proxy,
        article_selector=args.selector,
        min_length=args.min_length,
        must_have=args.must,
        exclude=args.exclude,
        link_must=args.link_must,
        link_exclude=args.link_exclude,
        auto_paging=not args.no_paging,
        max_list_pages=max(1, args.list_pages),
        output_dir=os.path.join(BASE_DIR, args.out),
    )

    # 命令行模式直接把日志打在屏幕上（print 是「打印一行到控制台」）
    def log(text):
        print(text, flush=True)     # flush=True 表示「立刻输出，不要攒着」，这样你能实时看到进度

    records, summary, location = crawler.run_and_export(
        config, log=log, base_output_dir=config.output_dir)

    print("")
    print("=" * 56)
    print("完成。成功 %d 篇，失败/跳过 %d 篇。" % (summary["成功篇数"], summary["失败篇数"]))
    if location:
        print("结果文件夹：%s" % location["folder"])
        print("索引表：%s" % location["csv"])
        print("正文目录：%s" % location["articles"])
    else:
        print("本次没有产出结果。")
    print("=" * 56)
    return 0


if __name__ == "__main__":
    sys.exit(main())
