"""从 source_spec 生成课程 PDF、确定性对象金标和展平文本基线。"""
from __future__ import annotations

import argparse
import io
import json
import math
from pathlib import Path
from xml.sax.saxutils import escape

import pymupdf as fitz
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, Table, TableStyle

if __package__:
    from .export_rag_for_dify import export
else:
    from export_rag_for_dify import export

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "datasets/rag/document-ai/v1"
PDF_NAME = "xiamen_travel_service_training_v1.pdf"
WIDTH, HEIGHT = 595, 842
FONT = "STSong-Light"


def paragraph(c, text, x, top, width, size=11):
    style = ParagraphStyle("body", fontName=FONT, fontSize=size, leading=size * 1.5)
    p = Paragraph(escape(text), style)
    _, height = p.wrap(width, HEIGHT)
    p.drawOn(c, x, HEIGHT - top - height)
    return height


def arrow(c, start, end):
    x1, y1 = start
    x2, y2 = end
    c.line(x1, HEIGHT - y1, x2, HEIGHT - y2)
    angle = math.atan2(y2 - y1, x2 - x1)
    for offset in (-0.5, 0.5):
        x = x2 - 7 * math.cos(angle + offset)
        y = y2 - 7 * math.sin(angle + offset)
        c.line(x2, HEIGHT - y2, x, HEIGHT - y)


def diagram(page):
    """布局是代码；所有节点、条件、消息文字均来自源规格。"""
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=(WIDTH, HEIGHT), invariant=1)
    c.setStrokeColor(colors.HexColor("#44546a"))
    c.setLineWidth(1)
    if "flowchart" in page:
        nodes = page["flowchart"]["nodes"]
        positions = [(298, 170), (298, 250), (110, 345), (298, 345),
                     (485, 345), (485, 450), (298, 560), (485, 680)]
        if len(nodes) != len(positions):
            raise ValueError("流程节点数量改变，需要更新布局")
        pos = {node["id"]: xy for node, xy in zip(nodes, positions, strict=True)}
        for edge in page["flowchart"]["edges"]:
            x1, y1 = pos[edge["from"]]
            x2, y2 = pos[edge["to"]]
            arrow(c, (x1, y1 + 27), (x2, y2 - 27))
            if edge.get("condition"):
                paragraph(c, edge["condition"], (x1+x2)/2 + 4,
                          (y1+y2)/2 - 10, 105, 10)
        for node in nodes:
            x, y = pos[node["id"]]
            c.setFillColor(colors.HexColor("#edf3f8"))
            c.roundRect(x-75, HEIGHT-y-27, 150, 54, 7, fill=1)
            c.setFillColor(colors.black)
            paragraph(c, node["label"], x-66, y-17, 132, 11)
    else:
        seq = page["sequence"]
        positions = {name: 55 + i*121 for i, name in enumerate(seq["actors"])}
        for name, x in positions.items():
            paragraph(c, name, max(25, x-30), 140, 85, 10)
            c.setDash(3, 3)
            c.line(x, HEIGHT-172, x, HEIGHT-710)
            c.setDash()
        for index, event in enumerate(seq["events"]):
            y = 210 + index*70
            x1, x2 = positions[event["sender"]], positions[event["receiver"]]
            arrow(c, (x1, y), (x2, y))
            text = f'{event["sequence"]}. {event["message"]}'
            if event.get("condition"):
                text += f'（{event["condition"]}）'
            paragraph(c, text, max(30, min(x1, x2)), y-40,
                      min(530, max(220, abs(x2-x1))), 10)
    c.save()
    with fitz.open(stream=buffer.getvalue(), filetype="pdf") as doc:
        return doc[0].get_pixmap(matrix=fitz.Matrix(2, 2),
                                  clip=fitz.Rect(20, 120, 575, 735)).tobytes("png")


def generate(spec, target):
    pdfmetrics.registerFont(UnicodeCIDFont(FONT))
    (target / "source").mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(target / "source" / PDF_NAME),
                      pagesize=(WIDTH, HEIGHT), invariant=1)
    c.setTitle(spec["title"])
    c.setAuthor("Wang Hao")
    objects, chunks = [], []
    for page in spec["pages"]:
        number = page["page"]
        prefix = f'{page["document_id"]}-P{number}'
        meta = {key: page.get(key, spec[key]) for key in
                ("observed_at", "valid_until", "revision", "is_simulated")}
        page_objects = []

        def record(suffix, kind, title, content, bbox, **extra):
            item = dict(object_id=f"{prefix}-{suffix}", document_id=page["document_id"],
                        page=number, title=title, object_type=kind, bbox=bbox,
                        section_path=[spec["title"], page["heading"]], content=content,
                        retrieval_text=f'{title} {content}', entities=[], fine_grained_ids=[],
                        source=f"课程合成 PDF：{PDF_NAME}", **meta)
            item.update(extra)
            item.update(page.get("object_annotations", {}).get(suffix, {}))
            page_objects.append(item)

        paragraph(c, spec["title"], 44, 24, 507, 18)
        paragraph(c, f'第 {number} 页 | {page["heading"]}', 44, 58, 507, 14)
        top = 96
        if len(page["paragraphs"]) != len(page["paragraph_ids"]):
            raise ValueError("每段正文必须有稳定的 paragraph_id")
        for suffix, text in zip(page["paragraph_ids"], page["paragraphs"], strict=True):
            height = paragraph(c, text, 44, top, 507)
            record(suffix, "paragraph", page["heading"], text, [44, top, 551, top+height])
            top += height + 16
        flat = list(page["paragraphs"])
        if "table" in page:
            table = page["table"]
            top += 12
            style = ParagraphStyle("cell", fontName=FONT, fontSize=9, leading=14)
            raw = [table["headers"], *table["rows"]]
            widths = [507/len(table["headers"])]*len(table["headers"])
            rows = [[Paragraph(escape(value), style) for value in row] for row in raw]
            t = Table(rows, colWidths=widths)
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e5edf5")),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#adbaca")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ]))
            _, height = t.wrap(507, HEIGHT)
            t.drawOn(c, 44, HEIGHT-top-height)
            cells = []
            for r, row in enumerate(table["rows"], 1):
                for col, value in enumerate(row, 1):
                    y = top + sum(t._rowHeights[:r])
                    x = 44 + sum(widths[:col-1])
                    cells.append(dict(cell_id=f"P{number}-T1-R{r}-C{col}", row=r,
                                      column=col, value=value,
                                      bbox=[x, y, x+widths[col-1], y+t._rowHeights[r]]))
            content = "\n".join("；".join(f"{h}：{v}" for h, v in
                                  zip(table["headers"], row, strict=True))
                                for row in table["rows"]) + "\n" + table["footnote"]
            caption_height = paragraph(c, table["caption"] + "。" + table["footnote"],
                                       44, top+height+14, 507, 10)
            record(page["table_id"], "table", table["caption"], content,
                   [44, top, 551, top+height+14+caption_height], cells=cells,
                   fine_grained_ids=[x["cell_id"] for x in cells],
                   entities=[row[0] for row in table["rows"]])
            flat += [" ".join(row) for row in raw] + [table["footnote"]]
        if "flowchart" in page or "sequence" in page:
            c.drawImage(ImageReader(io.BytesIO(diagram(page))), 30, HEIGHT-720,
                        width=535, height=593)
            paragraph(c, page["diagram_caption"], 44, 740, 507, 10)
            if "flowchart" in page:
                flow = page["flowchart"]
                labels = {n["id"]: n["label"] for n in flow["nodes"]}
                edges = [dict(edge_id=f"P{number}-F1-E{i}", **e)
                         for i, e in enumerate(flow["edges"], 1)]
                content = "\n".join(f'{labels[e["from"]]} → {labels[e["to"]]}；'
                                    f'条件：{e.get("condition") or "无"}' for e in edges)
                record(page["diagram_id"], "flowchart", page["diagram_caption"], content,
                       [30, 127, 565, 720], nodes=flow["nodes"], edges=edges,
                       fine_grained_ids=[e["edge_id"] for e in edges])
                flat += list(labels.values()) + [e["condition"] for e in edges if e["condition"]]
            else:
                seq = page["sequence"]
                events = [dict(event_id=f'P{number}-S1-E{e["sequence"]}', **e)
                          for e in seq["events"]]
                content = "\n".join(f'{e["sequence"]}. {e["sender"]} → {e["receiver"]}：'
                                    f'{e["message"]}；条件：{e.get("condition") or "无"}'
                                    for e in events)
                record(page["diagram_id"], "sequence_diagram", page["diagram_caption"],
                       content, [30, 127, 565, 720], events=events,
                       fine_grained_ids=[e["event_id"] for e in events])
                flat += seq["actors"] + [e["message"] for e in events]
                flat += [e["condition"] for e in events if e["condition"]]
        paragraph(c, "教学模拟资料" if number == 1 else spec["title"], 44, 804, 450, 9)
        paragraph(c, str(number), 540, 804, 25, 9)
        c.showPage()
        objects += page_objects
        chunks.append(dict(chunk_id=f"{prefix}-TEXT", document_id=page["document_id"],
                           title=page["heading"], page=number, content=" ".join(flat),
                           supports_object_ids=[o["object_id"] for o in page_objects],
                           source="源规格生成的展平文本基线（非现场 PDF/OCR 解析）", **meta))
    c.save()
    for name, items in [("object-aware", objects), ("text-only", chunks)]:
        directory = target / "parsed"
        directory.mkdir(exist_ok=True)
        (directory / f"{name}.jsonl").write_text(
            "".join(json.dumps(x, ensure_ascii=False, separators=(",", ":"))+"\n" for x in items),
            encoding="utf-8")
    export(target)
    manifest = {key: spec[key] for key in
                ("revision", "source", "observed_at", "valid_until", "is_simulated")}
    manifest.update(dataset_id="xiamen-document-ai-v1", page_count=len(chunks),
                    object_count=len(objects), document_count=len({x["document_id"] for x in chunks}),
                    embedded_raster_diagram_pages=sum("diagram_id" in p for p in spec["pages"]),
                    notice="教学模拟资料，不用于真实出行或交易决策。")
    (target / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+"\n")
    return objects, chunks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DATASET / "source/source_spec.json")
    parser.add_argument("--output-dir", type=Path, default=DATASET)
    args = parser.parse_args()
    objects, chunks = generate(json.loads(args.source.read_text()), args.output_dir)
    print(f"Generated PDF, {len(objects)} objects, {len(chunks)} pages and Dify Markdown")


if __name__ == "__main__":
    main()
