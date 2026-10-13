# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import asyncio
import argparse
from pathlib import Path
from typing import Iterable

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt
from markdown_it import MarkdownIt


import lark_oapi as lark
from lark_oapi.api.im.v1 import *


app_id="cli_aac6278dbd615bdb"
app_secret="DkMNYoAMW8Wsy99nYx5WRby1CjgINtpV"


feishu_client = lark.Client.builder() \
                .app_id(app_id) \
                .app_secret(app_secret) \
                .log_level(lark.LogLevel.DEBUG) \
                .build()

def set_cell_shading(cell, fill: str) -> None:
    """设置单元格背景色。"""
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = tc_pr.find(qn("w:shd"))

    if shd is None:
        shd = OxmlElement("w:shd")
        tc_pr.append(shd)

    shd.set(qn("w:fill"), fill)


def set_cell_border(cell, **kwargs) -> None:
    """设置单元格边框。"""
    tc = cell._tc
    tc_pr = tc.get_or_add_tcPr()
    tc_borders = tc_pr.first_child_found_in("w:tcBorders")

    if tc_borders is None:
        tc_borders = OxmlElement("w:tcBorders")
        tc_pr.append(tc_borders)

    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        if edge not in kwargs:
            continue

        edge_data = kwargs.get(edge)
        tag = "w:{}".format(edge)

        element = tc_borders.find(qn(tag))
        if element is None:
            element = OxmlElement(tag)
            tc_borders.append(element)

        for key in ["val", "sz", "space", "color"]:
            if key in edge_data:
                element.set(qn("w:{}".format(key)), str(edge_data[key]))


def set_run_font(run, font_name: str = "Microsoft YaHei", font_size: int = 10):
    """统一设置中英文字符字体。"""
    run.font.name = font_name
    run.font.size = Pt(font_size)

    run._element.rPr.rFonts.set(qn("w:eastAsia"), font_name)
    run._element.rPr.rFonts.set(qn("w:ascii"), font_name)
    run._element.rPr.rFonts.set(qn("w:hAnsi"), font_name)


def configure_document(document: Document) -> None:
    """设置文档页面、正文和标题样式。"""
    section = document.sections[0]
    section.top_margin = Cm(2.2)
    section.bottom_margin = Cm(2.2)
    section.left_margin = Cm(2.4)
    section.right_margin = Cm(2.4)

    styles = document.styles

    normal = styles["Normal"]
    normal.font.name = "Microsoft YaHei"
    normal.font.size = Pt(10.5)
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")

    for level in range(1, 7):
        style_name = f"Heading {level}"
        if style_name not in styles:
            continue

        style = styles[style_name]
        style.font.name = "Microsoft YaHei"
        style._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")

        if level == 1:
            style.font.size = Pt(18)
            style.font.bold = True
        elif level == 2:
            style.font.size = Pt(15)
            style.font.bold = True
        elif level == 3:
            style.font.size = Pt(13)
            style.font.bold = True
        else:
            style.font.size = Pt(11)
            style.font.bold = True


def render_inline_tokens(paragraph, tokens: Iterable, base_font_size: int = 10):
    """将 Markdown 行内 token 转换为 Word runs。"""
    bold = False
    italic = False
    strike = False
    code = False
    link_stack = []

    for token in tokens:
        token_type = token.type

        if token_type == "text":
            run = paragraph.add_run(token.content)
            run.bold = bold
            run.italic = italic
            run.font.strike = strike
            if code:
                run.font.name = "Consolas"
                run._element.rPr.rFonts.set(qn("w:eastAsia"), "Consolas")
            else:
                set_run_font(run, font_size=base_font_size)

        elif token_type == "code_inline":
            run = paragraph.add_run(token.content)
            run.font.name = "Consolas"
            run._element.rPr.rFonts.set(qn("w:eastAsia"), "Consolas")
            run.font.size = Pt(base_font_size)
            run.font.bold = bold
            run.font.italic = italic

        elif token_type == "strong_open":
            bold = True

        elif token_type == "strong_close":
            bold = False

        elif token_type == "em_open":
            italic = True

        elif token_type == "em_close":
            italic = False

        elif token_type in ("s_open", "del_open"):
            strike = True

        elif token_type in ("s_close", "del_close"):
            strike = False

        elif token_type == "code_open":
            code = True

        elif token_type == "code_close":
            code = False

        elif token_type == "softbreak":
            paragraph.add_run(" ")

        elif token_type == "hardbreak":
            paragraph.add_run().add_break()

        elif token_type == "link_open":
            link_stack.append(token.attrGet("href") or "")

        elif token_type == "link_close":
            if link_stack:
                link_stack.pop()

        elif token_type == "image":
            alt = token.attrGet("alt") or ""
            src = token.attrGet("src") or ""
            text = f"[{alt or src}]"
            run = paragraph.add_run(text)
            set_run_font(run, font_size=base_font_size)


def render_table(document: Document, tokens: list, start_index: int) -> int:
    """
    解析并生成 Markdown 表格。
    返回 table_close 之后的 token 下标。
    """
    rows = []
    current_row = None
    current_cell = None
    current_cell_is_header = False

    i = start_index + 1

    while i < len(tokens):
        token = tokens[i]

        if token.type == "tr_open":
            current_row = []

        elif token.type == "tr_close":
            if current_row is not None:
                rows.append(current_row)
            current_row = None

        elif token.type in ("th_open", "td_open"):
            current_cell = []
            current_cell_is_header = token.type == "th_open"

        elif token.type == "inline" and current_cell is not None:
            if token.children:
                current_cell.extend(token.children)

        elif token.type in ("th_close", "td_close"):
            if current_row is not None and current_cell is not None:
                current_row.append(
                    {
                        "tokens": current_cell,
                        "header": current_cell_is_header,
                    }
                )

            current_cell = None
            current_cell_is_header = False

        elif token.type == "table_close":
            break

        i += 1

    if not rows:
        return i + 1

    column_count = max(len(row) for row in rows)
    table = document.add_table(rows=len(rows), cols=column_count)
    table.style = "Table Grid"
    table.autofit = True

    for row_index, row_data in enumerate(rows):
        for col_index in range(column_count):
            cell = table.cell(row_index, col_index)
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER

            set_cell_border(
                cell,
                top={"val": "single", "sz": "4", "color": "B7B7B7"},
                bottom={"val": "single", "sz": "4", "color": "B7B7B7"},
                left={"val": "single", "sz": "4", "color": "B7B7B7"},
                right={"val": "single", "sz": "4", "color": "B7B7B7"},
            )

            if col_index >= len(row_data):
                continue

            cell_data = row_data[col_index]
            paragraph = cell.paragraphs[0]
            paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT

            render_inline_tokens(
                paragraph,
                cell_data["tokens"],
                base_font_size=9,
            )

            if cell_data["header"]:
                set_cell_shading(cell, "D9EAF7")
                for run in paragraph.runs:
                    run.bold = True

    document.add_paragraph()
    return i + 1


def render_markdown_to_docx(markdown_text: str, output_path: Path) -> None:
    """将 Markdown 文本转换为 DOCX 文件。"""
    md = MarkdownIt("gfm-like")
    tokens = md.parse(markdown_text)

    document = Document()
    configure_document(document)

    current_paragraph = None
    list_stack = []

    i = 0

    while i < len(tokens):
        token = tokens[i]
        token_type = token.type

        if token_type == "table_open":
            current_paragraph = None
            i = render_table(document, tokens, i)
            continue

        if token_type == "heading_open":
            level = int(token.tag[1:])
            style_name = f"Heading {level}"
            current_paragraph = document.add_paragraph(style=style_name)
            i += 1
            continue

        if token_type == "heading_close":
            current_paragraph = None
            i += 1
            continue

        if token_type == "paragraph_open":
            if list_stack:
                current_paragraph = document.add_paragraph(
                    style=list_stack[-1]
                )
            else:
                current_paragraph = document.add_paragraph()
            i += 1
            continue

        if token_type == "paragraph_close":
            current_paragraph = None
            i += 1
            continue

        if token_type == "inline":
            if current_paragraph is None:
                current_paragraph = document.add_paragraph()

            render_inline_tokens(
                current_paragraph,
                token.children or [],
                base_font_size=10,
            )
            i += 1
            continue

        if token_type == "bullet_list_open":
            list_stack.append("List Bullet")
            i += 1
            continue

        if token_type == "bullet_list_close":
            if list_stack:
                list_stack.pop()
            i += 1
            continue

        if token_type == "ordered_list_open":
            list_stack.append("List Number")
            i += 1
            continue

        if token_type == "ordered_list_close":
            if list_stack:
                list_stack.pop()
            i += 1
            continue

        if token_type == "blockquote_open":
            current_paragraph = document.add_paragraph()
            current_paragraph.paragraph_format.left_indent = Cm(0.8)
            i += 1
            continue

        if token_type == "blockquote_close":
            current_paragraph = None
            i += 1
            continue

        if token_type in ("fence", "code_block"):
            paragraph = document.add_paragraph()
            paragraph.paragraph_format.left_indent = Cm(0.5)
            paragraph.paragraph_format.right_indent = Cm(0.5)

            run = paragraph.add_run(token.content.rstrip("\n"))
            run.font.name = "Consolas"
            run._element.rPr.rFonts.set(qn("w:eastAsia"), "Consolas")
            run.font.size = Pt(9)

            current_paragraph = None
            i += 1
            continue

        if token_type == "hr":
            paragraph = document.add_paragraph()
            paragraph.paragraph_format.space_after = Pt(4)
            run = paragraph.add_run("_" * 70)
            set_run_font(run, font_size=8)
            current_paragraph = None
            i += 1
            continue

        i += 1

    document.save(output_path)


async def send_to_feishu(local_file: str, open_id: str):

        with open(local_file, "rb") as file:

            request: CreateFileRequest = CreateFileRequest.builder() \
                .request_body(CreateFileRequestBody.builder()
                    .file_type("doc")
                    .file_name(Path(local_file).name)
                    .file(file)
                    .build()) \
                .build()

            # 发起请求
            response: CreateFileResponse = await asyncio.to_thread(feishu_client.im.v1.file.create, request)

            # 处理失败返回
            if not response.success():
                lark.logger.error(
                    f"client.im.v1.image.create failed, code: {response.code}, msg: {response.msg}, log_id: {response.get_log_id()}, resp: \n{json.dumps(json.loads(response.raw.content), indent=4, ensure_ascii=False)}")
                return

            # 处理业务结果
            lark.logger.info(lark.JSON.marshal(response.data, indent=4))

            request: CreateMessageRequest = CreateMessageRequest.builder() \
                .receive_id_type("open_id") \
                .request_body(CreateMessageRequestBody.builder()
                    .receive_id(open_id)
                    .msg_type("file")
                    .content(f"{{\"file_key\":\"{response.data.file_key}\"}}")
                    .build()) \
                .build()

            # 发起请求
            response: CreateMessageResponse = await asyncio.to_thread(feishu_client.im.v1.message.create, request)

            # 处理失败返回
            if not response.success():
                lark.logger.error(
                    f"client.im.v1.message.create failed, code: {response.code}, msg: {response.msg}, log_id: {response.get_log_id()}, resp: \n{json.dumps(json.loads(response.raw.content), indent=4, ensure_ascii=False)}")
                return

            # 处理业务结果
            lark.logger.info(lark.JSON.marshal(response.data, indent=4))


async def main():
    parser = argparse.ArgumentParser(
        description="将 Markdown 文件转换为 DOCX 文件"
    )
    parser.add_argument(
        "--input",
        nargs="?",
        required=True,
        default=None,
        help="输入 Markdown 文件路径",
    )
    parser.add_argument(
        "--open_id",
        default=None,
        required=True,
        help="user's id in current conversation",
    )

    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = input_path.with_suffix(".docx")

    if not input_path.exists():
        raise FileNotFoundError(f"找不到输入文件：{input_path}")

    markdown_text = input_path.read_text(encoding="utf-8")

    render_markdown_to_docx(markdown_text, output_path)
    print(f"转换完成：{output_path.resolve()}")

    await send_to_feishu(local_file=output_path, open_id=args.open_id)


if __name__ == "__main__":
    asyncio.run(main())
