from __future__ import annotations

import io
import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence, Tuple

from endless_task.domain.models import ArtifactKind

MAX_TITLE_CHARS_IN_FILENAME = 80

_UNSAFE_FILENAME_CHARS = re.compile(r"[\\/:*?\"<>|\u0000-\u0008\u000e-\u001f\u007f]")
_WHITESPACE_RUN = re.compile(r"\s+")
_HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.*)$")
_HR_PATTERN = re.compile(r"^(-{3,}|\*{3,}|_{3,})$")
_UL_PATTERN = re.compile(r"^\s*[-*+]\s+(.*)$")
_OL_PATTERN = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_TABLE_SEPARATOR_PATTERN = re.compile(
    r"^\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)+\|?$"
)
_INLINE_CODE_PATTERN = re.compile(r"`([^`]+)`")
_BOLD_PATTERN = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_PATTERN = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")


class ExportError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 500) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class ParsedBlock:
    kind: str  # heading | paragraph | ul | ol | code | quote | hr | table
    level: int = 0
    text: str = ""
    items: Tuple[str, ...] = ()
    header: Tuple[str, ...] = ()
    rows: Tuple[Tuple[str, ...], ...] = ()


def parse_markdown_blocks(content: str) -> Tuple[ParsedBlock, ...]:
    lines = content.splitlines()
    blocks: list[ParsedBlock] = []
    index = 0
    total = len(lines)
    while index < total:
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            index += 1
            continue

        if stripped.startswith("```"):
            index += 1
            code_lines: list[str] = []
            while index < total and not lines[index].strip().startswith("```"):
                code_lines.append(lines[index])
                index += 1
            index += 1
            blocks.append(ParsedBlock(kind="code", text="\n".join(code_lines)))
            continue

        heading = _HEADING_PATTERN.match(stripped)
        if heading is not None:
            blocks.append(
                ParsedBlock(
                    kind="heading",
                    level=min(len(heading.group(1)), 3),
                    text=heading.group(2).strip(),
                )
            )
            index += 1
            continue

        if _HR_PATTERN.match(stripped):
            blocks.append(ParsedBlock(kind="hr"))
            index += 1
            continue

        if stripped.startswith(">"):
            quote_lines: list[str] = []
            while index < total and lines[index].strip().startswith(">"):
                quote_lines.append(re.sub(r"^\s*>\s?", "", lines[index]))
                index += 1
            blocks.append(
                ParsedBlock(kind="quote", text="\n".join(quote_lines).strip())
            )
            continue

        if (
            "|" in stripped
            and index + 1 < total
            and _TABLE_SEPARATOR_PATTERN.match(lines[index + 1].strip())
        ):
            header = _split_table_row(stripped)
            index += 2
            rows: list[Tuple[str, ...]] = []
            while index < total and lines[index].strip() and "|" in lines[index]:
                rows.append(tuple(_split_table_row(lines[index].strip())))
                index += 1
            blocks.append(
                ParsedBlock(kind="table", header=tuple(header), rows=tuple(rows))
            )
            continue

        if _UL_PATTERN.match(line):
            items: list[str] = []
            while index < total:
                item_match = _UL_PATTERN.match(lines[index])
                if item_match is not None:
                    items.append(item_match.group(1).strip())
                elif lines[index].strip() and lines[index][:1] in (" ", "\t") and items:
                    items[-1] = f"{items[-1]} {lines[index].strip()}"
                else:
                    break
                index += 1
            blocks.append(ParsedBlock(kind="ul", items=tuple(items)))
            continue

        if _OL_PATTERN.match(line):
            items = []
            while index < total:
                item_match = _OL_PATTERN.match(lines[index])
                if item_match is not None:
                    items.append(item_match.group(1).strip())
                elif lines[index].strip() and lines[index][:1] in (" ", "\t") and items:
                    items[-1] = f"{items[-1]} {lines[index].strip()}"
                else:
                    break
                index += 1
            blocks.append(ParsedBlock(kind="ol", items=tuple(items)))
            continue

        paragraph_lines = [stripped]
        index += 1
        while index < total and lines[index].strip() and not _starts_block(
            lines[index]
        ):
            paragraph_lines.append(lines[index].strip())
            index += 1
        blocks.append(ParsedBlock(kind="paragraph", text=" ".join(paragraph_lines)))
    return tuple(blocks)


def _starts_block(line: str) -> bool:
    stripped = line.strip()
    return (
        stripped.startswith("```")
        or _HEADING_PATTERN.match(stripped) is not None
        or _HR_PATTERN.match(stripped) is not None
        or stripped.startswith(">")
        or _UL_PATTERN.match(line) is not None
        or _OL_PATTERN.match(line) is not None
    )


def _split_table_row(row: str) -> list[str]:
    trimmed = row.strip()
    if trimmed.startswith("|"):
        trimmed = trimmed[1:]
    if trimmed.endswith("|"):
        trimmed = trimmed[:-1]
    return [cell.strip() for cell in trimmed.split("|")]


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _escape_pdf(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _inline_html(text: str) -> str:
    rendered = _escape_html(text)
    rendered = _INLINE_CODE_PATTERN.sub(r"<code>\1</code>", rendered)
    rendered = _BOLD_PATTERN.sub(r"<b>\1</b>", rendered)
    rendered = _ITALIC_PATTERN.sub(r"<i>\1</i>", rendered)
    return rendered


def _inline_pdf(text: str) -> str:
    rendered = _escape_pdf(text)
    rendered = _INLINE_CODE_PATTERN.sub(r'<font name="Courier">\1</font>', rendered)
    rendered = _BOLD_PATTERN.sub(r"<b>\1</b>", rendered)
    rendered = _ITALIC_PATTERN.sub(r"<i>\1</i>", rendered)
    return rendered


def export_filename(artifact, *, fmt: str) -> str:
    if fmt == "markdown":
        extension = "md" if artifact.kind is ArtifactKind.MARKDOWN else "txt"
    elif fmt == "html":
        extension = "html"
    elif fmt == "pdf":
        extension = "pdf"
    else:
        raise ExportError(
            "invalid_request",
            "format 必须是 markdown / html / pdf。",
            status_code=400,
        )
    title = _UNSAFE_FILENAME_CHARS.sub("_", artifact.title)
    title = _WHITESPACE_RUN.sub(" ", title).strip()
    title = title.strip("_").strip()
    if not title:
        title = f"artifact-{artifact.id}"
    if len(title) > MAX_TITLE_CHARS_IN_FILENAME:
        title = title[:MAX_TITLE_CHARS_IN_FILENAME].rstrip()
    return f"{title}-v{artifact.current_version_ordinal}.{extension}"


def content_disposition_header(filename: str) -> str:
    ascii_name = re.sub(r"[^\x20-\x7e]", "", filename).replace('"', "").strip()
    if not ascii_name or ascii_name.startswith("."):
        suffix = filename[filename.rfind("."):] if "." in filename else ""
        ascii_name = f"endless-task-export{suffix}"
    encoded = urllib.parse.quote(filename)
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded}"


def _export_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def build_export(artifact, version, *, fmt: str) -> Tuple[bytes, str]:
    if fmt == "markdown":
        media_type = (
            "text/markdown; charset=utf-8"
            if artifact.kind is ArtifactKind.MARKDOWN
            else "text/plain; charset=utf-8"
        )
        return version.content.encode("utf-8"), media_type
    if fmt == "html":
        return render_html(artifact, version), "text/html; charset=utf-8"
    if fmt == "pdf":
        return render_pdf(artifact, version), "application/pdf"
    raise ExportError(
        "invalid_request", "format 必须是 markdown / html / pdf。", status_code=400
    )


_HTML_STYLE = """
body { margin: 0; background: #ffffff; color: #1f2328; }
main { max-width: 46em; margin: 0 auto; padding: 2.5em 1.5em 4em;
  font-family: -apple-system, "PingFang SC", "Hiragino Sans GB",
  "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
  font-size: 16px; line-height: 1.75; }
h1 { font-size: 1.8em; margin: 0 0 0.2em; }
h2 { font-size: 1.4em; margin: 1.4em 0 0.5em; }
h3 { font-size: 1.15em; margin: 1.2em 0 0.4em; }
p.meta { color: #6b7280; font-size: 0.85em; margin: 0 0 2em; }
p { margin: 0.6em 0; }
ul, ol { margin: 0.6em 0; padding-left: 1.6em; }
li { margin: 0.25em 0; }
pre { background: #f6f8fa; border-radius: 6px; padding: 0.9em 1em;
  overflow-x: auto; font-size: 0.9em; line-height: 1.55; }
code { font-family: "SF Mono", Menlo, Consolas, "Courier New", monospace;
  font-size: 0.9em; }
p code, li code, td code { background: #f6f8fa; border-radius: 4px;
  padding: 0.1em 0.35em; }
blockquote { margin: 0.8em 0; padding: 0.2em 1em; border-left: 4px solid #d0d7de;
  color: #57606a; }
hr { border: none; border-top: 1px solid #d0d7de; margin: 1.6em 0; }
table { border-collapse: collapse; margin: 0.9em 0; width: 100%; }
th, td { border: 1px solid #d0d7de; padding: 0.45em 0.7em; text-align: left; }
th { background: #f6f8fa; }
@media print {
  main { max-width: none; padding: 0; font-size: 12pt; }
  pre { white-space: pre-wrap; }
}
"""


def render_html(artifact, version) -> bytes:
    parts: list[str] = []
    parts.append("<!DOCTYPE html>")
    parts.append('<html lang="zh-CN">')
    parts.append("<head>")
    parts.append('<meta charset="utf-8">')
    parts.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    parts.append(f"<title>{_escape_html(artifact.title)}</title>")
    parts.append(f"<style>{_HTML_STYLE}</style>")
    parts.append("</head>")
    parts.append("<body>")
    parts.append("<main>")
    parts.append(f"<h1>{_escape_html(artifact.title)}</h1>")
    parts.append(
        f'<p class="meta">Endless Task 导出 · v{version.ordinal} · {_export_date()}</p>'
    )
    for block in parse_markdown_blocks(version.content):
        parts.extend(_html_block(block))
    parts.append("</main>")
    parts.append("</body>")
    parts.append("</html>")
    return "\n".join(parts).encode("utf-8")


def _html_block(block: ParsedBlock) -> list[str]:
    if block.kind == "heading":
        tag = f"h{block.level + 1 if block.level >= 2 else 2}"
        return [f"<{tag}>{_inline_html(block.text)}</{tag}>"]
    if block.kind == "paragraph":
        return [f"<p>{_inline_html(block.text)}</p>"]
    if block.kind == "ul":
        items = "".join(f"<li>{_inline_html(item)}</li>" for item in block.items)
        return [f"<ul>{items}</ul>"]
    if block.kind == "ol":
        items = "".join(f"<li>{_inline_html(item)}</li>" for item in block.items)
        return [f"<ol>{items}</ol>"]
    if block.kind == "code":
        return [f"<pre><code>{_escape_html(block.text)}</code></pre>"]
    if block.kind == "quote":
        return [f"<blockquote>{_inline_html(block.text)}</blockquote>"]
    if block.kind == "hr":
        return ["<hr>"]
    if block.kind == "table":
        header_cells = "".join(
            f"<th>{_inline_html(cell)}</th>" for cell in block.header
        )
        body_rows = "".join(
            "<tr>"
            + "".join(f"<td>{_inline_html(cell)}</td>" for cell in row)
            + "</tr>"
            for row in block.rows
        )
        return [f"<table><thead><tr>{header_cells}</tr></thead><tbody>{body_rows}</tbody></table>"]
    return []


_PDF_FONT_REGISTERED = False


def load_pdf_dependencies() -> dict:
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.platypus import (
            HRFlowable,
            Paragraph,
            Preformatted,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )
    except ImportError as error:
        raise ExportError(
            "export_unavailable",
            "PDF 导出依赖缺失，请先安装 reportlab（uv sync）。",
            status_code=503,
        ) from error
    return {
        "colors": colors,
        "A4": A4,
        "ParagraphStyle": ParagraphStyle,
        "cm": cm,
        "pdfmetrics": pdfmetrics,
        "UnicodeCIDFont": UnicodeCIDFont,
        "HRFlowable": HRFlowable,
        "Paragraph": Paragraph,
        "Preformatted": Preformatted,
        "SimpleDocTemplate": SimpleDocTemplate,
        "Spacer": Spacer,
        "Table": Table,
        "TableStyle": TableStyle,
    }


def render_pdf(artifact, version) -> bytes:
    deps = load_pdf_dependencies()
    global _PDF_FONT_REGISTERED
    try:
        if not _PDF_FONT_REGISTERED:
            deps["pdfmetrics"].registerFont(deps["UnicodeCIDFont"]("STSong-Light"))
            deps["pdfmetrics"].registerFontFamily(
                "STSong-Light",
                normal="STSong-Light",
                bold="STSong-Light",
                italic="STSong-Light",
                boldItalic="STSong-Light",
            )
            _PDF_FONT_REGISTERED = True
        return _render_pdf_with(deps, artifact, version)
    except ExportError:
        raise
    except Exception as error:  # noqa: BLE001 - rendering must degrade gracefully
        raise ExportError(
            "export_unavailable",
            "PDF 渲染失败，请重试或改用 HTML 导出。",
            status_code=503,
        ) from error


def _render_pdf_with(deps: dict, artifact, version) -> bytes:
    from reportlab.lib.colors import HexColor

    ParagraphStyle = deps["ParagraphStyle"]
    body_font = "STSong-Light"
    styles = {
        "title": ParagraphStyle(
            "title", fontName=body_font, fontSize=18, leading=24, spaceAfter=4,
            wordWrap="CJK",
        ),
        "meta": ParagraphStyle(
            "meta", fontName=body_font, fontSize=9, leading=12,
            textColor=HexColor("#666666"), spaceAfter=14,
        ),
        "h2": ParagraphStyle(
            "h2", fontName=body_font, fontSize=14, leading=19,
            spaceBefore=12, spaceAfter=6, wordWrap="CJK",
        ),
        "h3": ParagraphStyle(
            "h3", fontName=body_font, fontSize=12, leading=16,
            spaceBefore=10, spaceAfter=4, wordWrap="CJK",
        ),
        "body": ParagraphStyle(
            "body", fontName=body_font, fontSize=10.5, leading=17, spaceAfter=6,
            wordWrap="CJK",
        ),
        "item": ParagraphStyle(
            "item", fontName=body_font, fontSize=10.5, leading=17,
            leftIndent=16, spaceAfter=2, wordWrap="CJK",
        ),
        "quote": ParagraphStyle(
            "quote", fontName=body_font, fontSize=10.5, leading=17,
            leftIndent=18, textColor="#555555", spaceAfter=6, wordWrap="CJK",
        ),
        "cell": ParagraphStyle(
            "cell", fontName=body_font, fontSize=9.5, leading=13, wordWrap="CJK"
        ),
        "code": ParagraphStyle(
            "code", fontName="Courier", fontSize=9, leading=13,
            leftIndent=6, spaceAfter=6, textColor=HexColor("#2d2d2d"),
        ),
    }

    flowables = [
        deps["Paragraph"](_escape_pdf(artifact.title), styles["title"]),
        deps["Paragraph"](
            f"Endless Task 导出 · v{version.ordinal} · {_export_date()}",
            styles["meta"],
        ),
    ]
    for block in parse_markdown_blocks(version.content):
        flowables.extend(_pdf_block(deps, styles, block))

    buffer = io.BytesIO()
    cm = deps["cm"]
    document = deps["SimpleDocTemplate"](
        buffer,
        pagesize=deps["A4"],
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
        title=artifact.title,
    )
    document.build(
        flowables,
        onFirstPage=_draw_page_number,
        onLaterPages=_draw_page_number,
    )
    return buffer.getvalue()


def _draw_page_number(canvas, doc) -> None:
    canvas.saveState()
    canvas.setFont("STSong-Light", 9)
    page_width = doc.pagesize[0]
    canvas.drawCentredString(page_width / 2, 30, str(canvas.getPageNumber()))
    canvas.restoreState()


def _pdf_block(deps: dict, styles: dict, block: ParsedBlock) -> list:
    Paragraph = deps["Paragraph"]
    if block.kind == "heading":
        style = styles["h2"] if block.level <= 2 else styles["h3"]
        if block.level == 1:
            style = styles["h2"]
        return [Paragraph(_inline_pdf(block.text), style)]
    if block.kind == "paragraph":
        return [Paragraph(_inline_pdf(block.text), styles["body"])]
    if block.kind == "ul":
        return [
            Paragraph(f"• {_inline_pdf(item)}", styles["item"])
            for item in block.items
        ]
    if block.kind == "ol":
        return [
            Paragraph(f"{index}. {_inline_pdf(item)}", styles["item"])
            for index, item in enumerate(block.items, start=1)
        ]
    if block.kind == "code":
        return [deps["Preformatted"](block.text, styles["code"]), deps["Spacer"](1, 6)]
    if block.kind == "quote":
        return [
            Paragraph(_inline_pdf(line), styles["quote"])
            for line in block.text.splitlines()
            if line.strip()
        ]
    if block.kind == "hr":
        return [deps["Spacer"](1, 4), deps["HRFlowable"](width="100%"), deps["Spacer"](1, 4)]
    if block.kind == "table":
        table_data = [
            [Paragraph(_inline_pdf(cell), styles["cell"]) for cell in block.header]
        ]
        for row in block.rows:
            table_data.append(
                [Paragraph(_inline_pdf(cell), styles["cell"]) for cell in row]
            )
        table = deps["Table"](table_data)
        table.setStyle(
            deps["TableStyle"](
                [
                    ("GRID", (0, 0), (-1, -1), 0.5, deps["colors"].grey),
                    ("BACKGROUND", (0, 0), (-1, 0), deps["colors"].Color(0.96, 0.97, 0.98)),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        return [table, deps["Spacer"](1, 6)]
    return []
