#!/usr/bin/env python3
"""Build docs/diseño_caja_negra.pdf from docs/design.md.

The layout constants below are the ones measured in
`diseno_caja_negra_mejorado.pdf`, the render the client asked us to match, by
inflating that file's own content streams: Letter pages, a navy header band and
footer band, a centered title block, blue info boxes, amber warning boxes, and
striped tables with a navy header row.

`docs/design.md` stays the single source of truth for the content. The build
supports the Markdown subset that document uses: headings, paragraphs, bullet
and numbered lists, pipe tables, fenced code blocks, inline bold and inline
code, plus three HTML-comment directives that Markdown hides on GitHub:

    <!-- callout info: Título -->   ... <!-- /callout -->
    <!-- callout warn: Título -->   ... <!-- /callout -->
    <!-- figure: architecture.png | Figura 1 — Caption -->

reportlab is a tool-only dependency, never a service one:

    python3 -m venv .venv-pdf && .venv-pdf/bin/pip install reportlab
    .venv-pdf/bin/python tools/build_pdf.py [source.md] [output.pdf]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    Image,
    KeepTogether,
    PageTemplate,
    Paragraph,
    Preformatted,
    Spacer,
    Table,
    TableStyle,
)

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
SOURCE = DOCS / "design.md"
DEFAULT_OUT = DOCS / "diseño_caja_negra.pdf"

# Page geometry, from the reference PDF's own operators.
PAGE_W, PAGE_H = letter  # 612 x 792
CONTENT_LEFT = 49.2
CONTENT_W = 489.6
FRAME_TOP = 741.6
FRAME_BOTTOM = 40.8
HEADER_BAND_H = 32.4
FOOTER_BAND_H = 28.8
BAND_LEFT = 43.2  # band text sits closer to the edge than the frame does
BAND_BASELINE_TOP = 771.84
BAND_BASELINE_BOTTOM = 12.96

NAVY = colors.Color(0.121569, 0.305882, 0.47451)
BLUE = colors.Color(0.180392, 0.458824, 0.713725)
INK = colors.Color(0.109804, 0.156863, 0.2)
GRID = colors.Color(0.741176, 0.764706, 0.780392)
ZEBRA = colors.Color(0.972549, 0.976471, 0.976471)
INFO_BG = colors.Color(0.839216, 0.917647, 0.972549)
WARN_BG = colors.Color(0.992157, 0.921569, 0.815686)
WARN_EDGE = colors.Color(0.901961, 0.494118, 0.133333)
CODE_BG = colors.Color(0.960784, 0.960784, 0.960784)
CAPTION_GRAY = colors.Color(0.501961, 0.501961, 0.501961)

HEADER_RIGHT = "Diseño técnico"
FOOTER_LEFT = "Santiago Duque"

DIRECTIVE = re.compile(r"^<!--\s*([a-z]+)(?:\s+([a-z]+))?\s*:(.*?)-->\s*$")
END_DIRECTIVE = re.compile(r"^<!--\s*/([a-z]+)\s*-->\s*$")


def build_styles() -> dict[str, ParagraphStyle]:
    return {
        "title": ParagraphStyle(
            "title", fontName="Helvetica-Bold", fontSize=22, leading=26,
            textColor=NAVY, alignment=TA_CENTER, spaceAfter=2,
        ),
        "subtitle": ParagraphStyle(
            "subtitle", fontName="Helvetica", fontSize=12, leading=15,
            textColor=BLUE, alignment=TA_CENTER, spaceAfter=18,
        ),
        "lede": ParagraphStyle(
            "lede", fontName="Helvetica", fontSize=9.5, leading=13,
            alignment=TA_JUSTIFY, spaceAfter=6,
        ),
        "h2": ParagraphStyle(
            "h2", fontName="Helvetica-Bold", fontSize=14, leading=18,
            textColor=NAVY, spaceBefore=14, spaceAfter=6, keepWithNext=1,
        ),
        "h3": ParagraphStyle(
            "h3", fontName="Helvetica-Bold", fontSize=12, leading=15,
            textColor=BLUE, spaceBefore=10, spaceAfter=4, keepWithNext=1,
        ),
        "body": ParagraphStyle(
            "body", fontName="Helvetica", fontSize=9.5, leading=13,
            alignment=TA_JUSTIFY, spaceAfter=6,
        ),
        "bullet": ParagraphStyle(
            "bullet", fontName="Helvetica", fontSize=9.5, leading=13,
            alignment=TA_JUSTIFY, leftIndent=15, bulletIndent=0,
            bulletFontName="Helvetica", bulletFontSize=9.5, spaceAfter=3,
        ),
        "th": ParagraphStyle(
            "th", fontName="Helvetica-Bold", fontSize=8, leading=10,
            textColor=colors.white,
        ),
        "td": ParagraphStyle(
            "td", fontName="Helvetica", fontSize=7.5, leading=10,
            textColor=INK,
        ),
        "callout_title": ParagraphStyle(
            "callout_title", fontName="Helvetica-Bold", fontSize=10, leading=12,
            textColor=NAVY,
        ),
        "callout_body": ParagraphStyle(
            "callout_body", fontName="Helvetica", fontSize=9, leading=12,
            textColor=INK, alignment=TA_JUSTIFY, spaceAfter=4,
        ),
        "callout_item": ParagraphStyle(
            "callout_item", fontName="Helvetica", fontSize=9, leading=12,
            textColor=INK, alignment=TA_JUSTIFY, leftIndent=12, bulletIndent=0,
            bulletFontName="Helvetica", bulletFontSize=9, spaceAfter=2,
        ),
        "code": ParagraphStyle(
            "code", fontName="Courier", fontSize=8, leading=10.5,
        ),
        "caption": ParagraphStyle(
            "caption", fontName="Helvetica-Oblique", fontSize=8.5, leading=11,
            textColor=CAPTION_GRAY, alignment=TA_CENTER, spaceBefore=6,
        ),
    }


def render_inline(text: str) -> str:
    """Markdown inline markup the document uses -> ReportLab paragraph markup."""
    parts = []
    for chunk in re.split(r"(`[^`]+`)", text.strip()):
        if len(chunk) > 1 and chunk.startswith("`") and chunk.endswith("`"):
            parts.append('<font name="Courier" size="8">%s</font>' % escape(chunk[1:-1]))
        else:
            parts.append(re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escape(chunk)))
    return "".join(parts)


def column_widths(rows: list[list[str]]) -> list[float]:
    """Width per column: never below the longest word, then by longest cell.

    A cell is measured in the font it is actually rendered in (bold 8 for the
    header, 7.5 for the body), so a column stops breaking words mid-word and
    still stretches to the widest content it actually holds.
    """

    def measure(text: str, bold: bool) -> tuple[float, float]:
        """(widest word, whole cell) in the fonts the cell is rendered in.

        Inline code renders in Courier 8, which is wider than the 7.5pt body
        font, so it has to be measured on its own or a cell that looks narrow
        on paper breaks mid-word on the page.
        """
        body_font, body_size = ("Helvetica-Bold", 8.0) if bold else ("Helvetica", 7.5)
        code_font, code_size = ("Courier-Bold", 8.0) if bold else ("Courier", 8.0)
        total, widest = 0.0, 0.0
        for chunk in re.split(r"(`[^`]+`)", text.strip()):
            if not chunk:
                continue
            if len(chunk) > 1 and chunk.startswith("`") and chunk.endswith("`"):
                piece, font, size = chunk[1:-1], code_font, code_size
            else:
                piece, font, size = chunk.replace("**", ""), body_font, body_size
            total += stringWidth(piece, font, size)
            for word in re.split(r"\s+", piece):
                if word:
                    widest = max(widest, stringWidth(word, font, size))
        return widest, total

    padding = 12.0  # 4 left, 4 right, and slack for the paragraph splitter
    limit = 0.55 * CONTENT_W
    minimums, preferred = [], []
    for index in range(len(rows[0])):
        cells = [(rows[0][index], True)] + [(row[index], False) for row in rows[1:]]
        word = min(limit, max(measure(cell, bold)[0] for cell, bold in cells) + padding)
        minimums.append(word)
        preferred.append(min(limit, max(word, max(measure(cell, bold)[1] for cell, bold in cells) + padding)))

    floor = sum(minimums)
    if floor >= CONTENT_W:
        return [width * CONTENT_W / floor for width in minimums]
    slack = CONTENT_W - floor
    wants = [preferred[i] - minimums[i] for i in range(len(minimums))]
    appetite = sum(wants) or 1.0
    return [minimums[i] + slack * wants[i] / appetite for i in range(len(minimums))]


def make_table(rows: list[list[str]], styles: dict[str, ParagraphStyle]) -> Table:
    data = [[Paragraph(render_inline(cell), styles["th"]) for cell in rows[0]]]
    for row in rows[1:]:
        data.append([Paragraph(render_inline(cell), styles["td"]) for cell in row])
    table = Table(data, colWidths=column_widths(rows), repeatRows=1)
    commands = [
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("GRID", (0, 0), (-1, -1), 0.4, GRID),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    for index in range(1, len(data)):
        if index % 2 == 1:
            commands.append(("BACKGROUND", (0, index), (-1, index), ZEBRA))
    table.setStyle(TableStyle(commands))
    return table


def make_callout(kind: str, title: str, items: list, styles: dict) -> Table:
    background, edge = (WARN_BG, WARN_EDGE) if kind == "warn" else (INFO_BG, BLUE)
    if kind == "warn" and not title.startswith("■"):
        title = "■ " + title
    cell = [Paragraph(render_inline(title), styles["callout_title"]), Spacer(1, 5)]
    cell.extend(items)
    table = Table([[cell]], colWidths=[CONTENT_W])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), background),
                ("BOX", (0, 0), (-1, -1), 1.5, edge),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    return table


def make_code_block(code: str, styles: dict) -> Table:
    table = Table([[Preformatted(code, styles["code"])]], colWidths=[CONTENT_W])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), CODE_BG),
                ("BOX", (0, 0), (-1, -1), 0.4, GRID),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return table


def make_figure(target: str, caption: str, styles: dict) -> KeepTogether:
    path = (DOCS / target).resolve()
    width, height = ImageReader(str(path)).getSize()
    image = Image(str(path))
    image.drawWidth = CONTENT_W
    image.drawHeight = CONTENT_W * height / width
    return KeepTogether([image, Paragraph(render_inline(caption), styles["caption"])])


def parse_lists(lines: list[str]) -> list[list[str]]:
    """Split a run of list lines into items, dropping the marker text."""
    items = []
    for line in lines:
        item = re.sub(r"^\s*(?:[-*]|\d+\.)\s+", "", line.rstrip())
        marker = re.match(r"^\s*(\d+)\.\s+", line)
        items.append((marker.group(1) if marker else None, item))
    return items


def render_items(raw: list[str], styles: dict, in_callout: bool) -> list:
    """List lines -> flowables; inside a callout the markers stay inline."""
    items = parse_lists(raw)
    paragraphs = []
    for number, text in items:
        if in_callout:
            marker = "%s." % number if number else "•"
            paragraphs.append(
                Paragraph(
                    '<font color="#1F4E79"><b>%s</b></font> %s' % (marker, render_inline(text)),
                    styles["callout_item"],
                )
            )
        else:
            marker = "%s." % number if number else "•"
            paragraphs.append(
                Paragraph(render_inline(text), styles["bullet"], bulletText=marker)
            )
    return paragraphs


def parse_markdown(text: str, styles: dict) -> tuple[str, str, list]:
    """Return (title, subtitle, flowables)."""
    lines = text.splitlines()
    title, subtitle = "La caja negra", ""
    story: list = []
    lede: list = []
    paragraph: list[str] = []
    list_buffer: list[str] = []
    table_buffer: list[str] = []
    code_buffer: list[str] = []
    in_code = False
    callout: tuple[str, str] | None = None
    callout_body: list = []

    def flush_paragraph() -> None:
        if not paragraph:
            return
        flow = Paragraph(render_inline(" ".join(paragraph)), styles["body"])
        (callout_body if callout else (lede if not story else story)).append(flow)
        paragraph.clear()

    def flush_list() -> None:
        if not list_buffer:
            return
        flows = render_items(list_buffer, styles, in_callout=bool(callout))
        (callout_body if callout else story).extend(flows)
        list_buffer.clear()

    def flush_table() -> None:
        if not table_buffer:
            return
        rows = []
        for line in table_buffer:
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", cell) for cell in cells):
                continue
            rows.append(cells)
        flow = make_table(rows, styles)
        (callout_body if callout else story).append(flow)
        table_buffer.clear()

    def flush_callout() -> None:
        nonlocal callout
        story.append(make_callout(callout[0], callout[1], callout_body, styles))
        callout_body.clear()
        callout = None

    for line in lines:
        stripped = line.strip()

        if in_code:
            if stripped.startswith("```"):
                in_code = False
                (callout_body if callout else story).append(
                    make_code_block("\n".join(code_buffer), styles)
                )
                code_buffer.clear()
            else:
                code_buffer.append(line.rstrip())
            continue

        if stripped.startswith("```"):
            flush_paragraph()
            flush_list()
            flush_table()
            in_code = True
            continue

        end = END_DIRECTIVE.match(stripped)
        if end:
            flush_paragraph()
            flush_list()
            if end.group(1) == "callout" and callout:
                flush_callout()
            continue

        directive = DIRECTIVE.match(stripped)
        if directive:
            flush_paragraph()
            flush_list()
            flush_table()
            kind, mode, argument = directive.group(1), directive.group(2), directive.group(3).strip()
            if kind == "callout":
                callout = ((mode or "info").strip(), argument)
            elif kind == "figure":
                target, _, caption = argument.partition("|")
                (callout_body if callout else story).append(
                    make_figure(target.strip(), caption.strip(), styles)
                )
            continue

        if stripped.startswith("#"):
            flush_paragraph()
            flush_list()
            flush_table()
            level = len(stripped) - len(stripped.lstrip("#"))
            heading = stripped[level:].strip()
            if level == 1:
                head, sep, tail = heading.partition(" — ")
                title = head.strip()
                subtitle = tail.strip() if sep else ""
            elif level == 2:
                (callout_body if callout else story).append(Paragraph(render_inline(heading), styles["h2"]))
            else:
                (callout_body if callout else story).append(Paragraph(render_inline(heading), styles["h3"]))
            continue

        if stripped.startswith("|"):
            flush_paragraph()
            flush_list()
            table_buffer.append(stripped)
            continue

        if re.match(r"^\s*(?:[-*]|\d+\.)\s+", line) and not table_buffer:
            flush_paragraph()
            list_buffer.append(line)
            continue

        if not stripped:
            flush_paragraph()
            flush_list()
            flush_table()
            continue

        if list_buffer or table_buffer:
            flush_list()
            flush_table()
        paragraph.append(stripped)

    flush_paragraph()
    flush_list()
    flush_table()
    if callout:
        flush_callout()

    story[0:0] = lede
    return title, subtitle, story


def build(source: Path, out: Path) -> int:
    styles = build_styles()
    title, subtitle, story = parse_markdown(source.read_text(encoding="utf-8"), styles)
    heading = "%s — %s" % (title, subtitle) if subtitle else title

    def draw_bands(canvas, doc) -> None:
        canvas.saveState()
        canvas.setFillColor(NAVY)
        canvas.rect(0, PAGE_H - HEADER_BAND_H, PAGE_W, HEADER_BAND_H, stroke=0, fill=1)
        canvas.rect(0, 0, PAGE_W, FOOTER_BAND_H, stroke=0, fill=1)
        canvas.setFillColor(colors.white)
        canvas.setFont("Helvetica-Bold", 9)
        canvas.drawString(BAND_LEFT, BAND_BASELINE_TOP, heading)
        canvas.setFont("Helvetica", 8)
        canvas.drawRightString(PAGE_W - BAND_LEFT, BAND_BASELINE_TOP, HEADER_RIGHT)
        canvas.drawString(BAND_LEFT, BAND_BASELINE_BOTTOM, FOOTER_LEFT)
        canvas.drawRightString(
            PAGE_W - BAND_LEFT, BAND_BASELINE_BOTTOM, "Página %d" % canvas.getPageNumber()
        )
        canvas.restoreState()

    doc = BaseDocTemplate(
        str(out),
        pagesize=letter,
        leftMargin=CONTENT_LEFT,
        rightMargin=PAGE_W - CONTENT_LEFT - CONTENT_W,
        topMargin=PAGE_H - FRAME_TOP,
        bottomMargin=FRAME_BOTTOM,
        title=heading,
        author=FOOTER_LEFT,
        subject="Diseño y arquitectura del reemplazo",
    )
    frame = Frame(
        CONTENT_LEFT, FRAME_BOTTOM, CONTENT_W, FRAME_TOP - FRAME_BOTTOM,
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0, id="body",
    )
    doc.addPageTemplates([PageTemplate(id="page", frames=[frame], onPage=draw_bands)])

    front = [
        Paragraph(escape(title), styles["title"]),
        Paragraph(escape(subtitle), styles["subtitle"]),
    ]
    doc.build(front + story)
    return doc.page


def main() -> int:
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else SOURCE
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else (
        DEFAULT_OUT if source == SOURCE else source.with_suffix(".pdf")
    )
    pages = build(source, out)
    shown = out.relative_to(ROOT) if out.is_absolute() and out.is_relative_to(ROOT) else out
    print("%s: %d páginas" % (shown, pages))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
