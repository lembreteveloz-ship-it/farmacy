"""Server-side PDF rendering. All data is selected by the authenticated handler."""
from io import BytesIO
from math import ceil, sqrt
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, LongTable, TableStyle
from reportlab.graphics.barcode import createBarcodeDrawing
from reportlab.pdfbase.pdfmetrics import stringWidth


LABEL_CAPACITIES = (30, 35, 40)
MAX_LABEL_COPIES = 1000


def label_layout(capacity=30):
    if capacity not in LABEL_CAPACITIES:
        raise ValueError("Escolha 30, 35 ou 40 etiquetas por folha.")
    width, height, margin, gap = 297 * mm, 210 * mm, 5 * mm, 1 * mm
    candidates = []
    for cols in range(2, capacity + 1):
        if capacity % cols:
            continue
        rows = capacity // cols
        cell_w = (width - 2 * margin - (cols - 1) * gap) / cols
        cell_h = (height - 2 * margin - (rows - 1) * gap) / rows
        qr = min(cell_w - 2 * mm, cell_h - 10 * mm)
        candidates.append((qr, cols, rows, cell_w, cell_h))
    qr, cols, rows, cell_w, cell_h = max(candidates)
    if qr < 25 * mm:
        raise ValueError("A grade deixa o QR Code pequeno demais.")
    return dict(width=width, height=height, margin=margin, gap=gap, columns=cols,
                rows=rows, cell_width=cell_w, cell_height=cell_h, qr_size=qr, capacity=capacity)


def label_grid_dimensions(label_count=30):
    layout = label_layout(label_count)
    return layout['columns'], layout['rows']


def _name_lines(text, width):
    # Wrap without dropping medication or concentration, including long words.
    for size in (6.5, 6, 5.5, 5, 4.5):
        lines, line = [], ''
        for character in text:
            if stringWidth(line + character, 'Helvetica', size) > width:
                lines.append(line); line = ''
            line += character
        lines.append(line)
        if len(lines) * (size + .5) <= 15:
            return size, lines
    raise ValueError("Nome e concentração extensos demais para a etiqueta. Revise o cadastro antes de imprimir.")


def label_sheet_drawing(labels, capacity=30):
    from reportlab.graphics.shapes import Drawing, Group, String, Rect
    layout = label_layout(capacity)
    drawing = Drawing(layout['width'], layout['height'])
    drawing.add(Rect(0, 0, layout['width'], layout['height'], fillColor=colors.white, strokeColor=None))
    for index, label in enumerate(labels):
        col, row = index % layout['columns'], index // layout['columns']
        x = layout['margin'] + col * (layout['cell_width'] + layout['gap'])
        top = layout['height'] - layout['margin'] - row * (layout['cell_height'] + layout['gap'])
        size = layout['qr_size']
        qr = createBarcodeDrawing('QR', value=label['code'], width=size, height=size, barLevel='M', barBorder=4)
        group = Group(qr)
        group.translate(x + (layout['cell_width'] - size) / 2, top - size)
        drawing.add(group)
        center = x + layout['cell_width'] / 2
        drawing.add(String(center, top - size - 6, label['code'], fontName='Helvetica', fontSize=5, textAnchor='middle', fillColor=colors.black))
        name = ' '.join(str(v).strip() for v in (label.get('medicine_name',''), label.get('concentration','')) if v)
        font, lines = _name_lines(name, layout['cell_width'] - 2 * mm)
        for line_index, text in enumerate(lines):
            drawing.add(String(center, top - size - 13 - line_index * (font + .5), text,
                               fontName='Helvetica', fontSize=font, textAnchor='middle', fillColor=colors.black))
    return drawing


def render_label_sheet_pdf(labels, codes_per_sheet=30):
    from reportlab.graphics import renderPDF
    if not labels or len(labels) > MAX_LABEL_COPIES:
        raise ValueError("Informe entre 1 e 1000 etiquetas.")
    layout = label_layout(codes_per_sheet)
    output = BytesIO()
    pdf = canvas.Canvas(output, pagesize=(layout['width'], layout['height']))
    pdf.setTitle('Etiquetas QR por lote — A4 paisagem')
    pdf.setViewerPreference('PrintScaling', 'None')
    for start in range(0, len(labels), codes_per_sheet):
        renderPDF.draw(label_sheet_drawing(labels[start:start+codes_per_sheet], codes_per_sheet), pdf, 0, 0)
        pdf.showPage()
    pdf.save()
    return output.getvalue()


def render_label_preview(labels, codes_per_sheet=30):
    from reportlab.graphics import renderSVG
    if not labels or len(labels) > MAX_LABEL_COPIES:
        raise ValueError("Informe entre 1 e 1000 etiquetas.")
    layout = label_layout(codes_per_sheet)
    sheets = [renderSVG.drawToString(label_sheet_drawing(labels[start:start+codes_per_sheet], codes_per_sheet))
              for start in range(0, len(labels), codes_per_sheet)]
    return {'sheets': sheets, 'layout': {**layout, 'qr_mm': layout['qr_size']/mm,
                                        'margin_mm': layout['margin']/mm}, 'total': len(labels)}


def render_pdf(title, unit, sections, barcode=None, minimal=False):
    output = BytesIO()
    styles = getSampleStyleSheet()
    styles['BodyText'].fontSize = 8
    styles['BodyText'].leading = 11
    paragraph = lambda value: Paragraph(escape(str(value if value is not None else '—')).replace('\n', '<br/>'), styles['BodyText'])
    story = []
    if minimal:
        styles['BodyText'].fontSize = 10
        styles['BodyText'].alignment = 1
        story.extend([
            Spacer(1, 8),
            createBarcodeDrawing('QR', value=barcode, width=35*mm, height=35*mm),
            Spacer(1, 10),
            Paragraph(escape(str(barcode)), styles['BodyText']),
            Spacer(1, 10),
            createBarcodeDrawing('QR', value=barcode, width=26*mm, height=26*mm),
        ])
    else:
        story = [Paragraph(escape(title), styles['Title']), paragraph(unit), Spacer(1, 12)]
        for subtitle, headers, rows in sections:
            story.extend([Paragraph(escape(subtitle), styles['Heading2']), Spacer(1, 5)])
            if not rows:
                story.append(paragraph('Nenhum registro encontrado para os filtros selecionados.'))
                continue
            table = LongTable([[paragraph(x) for x in headers]] + [[paragraph(x) for x in row] for row in rows],
                              colWidths=[180 * mm / len(headers)] * len(headers), repeatRows=1, splitInRow=1)
            table.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#e9f2eb')),
                ('GRID', (0, 0), (-1, -1), .3, colors.lightgrey), ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('TOPPADDING', (0, 0), (-1, -1), 6), ('BOTTOMPADDING', (0, 0), (-1, -1), 6)]))
            story.extend([table, Spacer(1, 12)])
        if barcode:
            story.extend([paragraph(barcode), createBarcodeDrawing('QR', value=barcode, width=40*mm, height=40*mm)])
    SimpleDocTemplate(output, pagesize=(210*mm, 297*mm), leftMargin=15*mm, rightMargin=15*mm,
                      topMargin=15*mm, bottomMargin=15*mm, title=title).build(story)
    return output.getvalue()
