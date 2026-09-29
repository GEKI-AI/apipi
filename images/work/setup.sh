#!/bin/sh
set -eu
PIP_CONFIG_FILE=/dev/null python3 -m pip install --no-cache-dir --break-system-packages \
  'python-docx==1.2.0' \
  'python-pptx==1.0.2' \
  'fpdf2==2.8.9'
python3 - <<'PY'
from pathlib import Path

import fpdf
import matplotlib
import openpyxl
import pandas
import pptx
import pypdf
import reportlab.pdfgen.canvas
import xlsxwriter
from docx import Document

out = Path("/tmp/apipi-work-check")
out.mkdir(exist_ok=True)
book = openpyxl.Workbook()
book.active["A1"] = "ok"
book.save(out / "t.xlsx")
sheet = xlsxwriter.Workbook(out / "t2.xlsx")
sheet.add_worksheet().write(0, 0, "ok")
sheet.close()
document = Document()
document.add_paragraph("ok")
document.save(out / "t.docx")
pres = pptx.Presentation()
slide = pres.slides.add_slide(pres.slide_layouts[0])
slide.shapes.title.text = "ok"
pres.save(out / "t.pptx")
pdf = fpdf.FPDF()
pdf.add_page()
pdf.set_font("Helvetica", size=12)
pdf.cell(40, 10, "ok")
pdf.output(out / "t.pdf")
reader = pypdf.PdfReader(out / "t.pdf")
assert reader.pages
canvas = reportlab.pdfgen.canvas.Canvas(str(out / "t2.pdf"))
canvas.drawString(72, 72, "ok")
canvas.save()
frame = pandas.DataFrame({"n": [1]})
assert int(frame["n"].iloc[0]) == 1
assert matplotlib.__version__
PY
pdftotext /tmp/apipi-work-check/t.pdf - | grep -q ok
rm -rf /tmp/apipi-work-check
