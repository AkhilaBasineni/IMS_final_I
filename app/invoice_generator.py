"""
invoice_generator.py
--------------------
Generates professional PDF invoices for Sales Orders and Purchase Orders.

PDFs are generated on-demand and streamed directly to the client as a
BytesIO buffer — no files are written to disk.

    generate_sales_invoice(so)   → BytesIO
    generate_purchase_invoice(po) → BytesIO
"""

import os
from io import BytesIO
from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_RIGHT, TA_CENTER, TA_LEFT
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# DejaVu Sans fonts are bundled inside the project (app/fonts/).
# They support the ₹ rupee symbol and work on Windows, Linux, and Mac
# with no OS-level font dependencies.
_FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fonts')
pdfmetrics.registerFont(TTFont('AppFont',      os.path.join(_FONT_DIR, 'DejaVuSans-Regular.ttf')))
pdfmetrics.registerFont(TTFont('AppFont-Bold', os.path.join(_FONT_DIR, 'DejaVuSans-Bold.ttf')))



# ── Brand Colors ──────────────────────────────────────────────────────────────

BRAND_DARK   = colors.HexColor('#1e293b')   # slate-800
BRAND_ACCENT = colors.HexColor('#4f46e5')   # indigo-600
BRAND_LIGHT  = colors.HexColor('#f1f5f9')   # slate-100
BRAND_BORDER = colors.HexColor('#e2e8f0')   # slate-200
TEXT_MUTED   = colors.HexColor('#64748b')   # slate-500
SUCCESS      = colors.HexColor('#10b981')   # emerald-500
WARNING      = colors.HexColor('#f59e0b')   # amber-500
WHITE        = colors.white

PAGE_W, PAGE_H = A4
MARGIN = 18 * mm


# ── Shared Style Helpers ──────────────────────────────────────────────────────

def _styles():
    base = getSampleStyleSheet()
    return {
        'title': ParagraphStyle('title', fontName='AppFont-Bold',
                                fontSize=22, textColor=WHITE, leading=28),
        'subtitle': ParagraphStyle('subtitle', fontName='AppFont',
                                   fontSize=10, textColor=colors.HexColor('#a5b4fc'), leading=14),
        'section': ParagraphStyle('section', fontName='AppFont-Bold',
                                  fontSize=9, textColor=BRAND_ACCENT,
                                  spaceAfter=4, leading=12),
        'body': ParagraphStyle('body', fontName='AppFont',
                               fontSize=9, textColor=BRAND_DARK, leading=13),
        'body_bold': ParagraphStyle('body_bold', fontName='AppFont-Bold',
                                    fontSize=9, textColor=BRAND_DARK, leading=13),
        'small': ParagraphStyle('small', fontName='AppFont',
                                fontSize=8, textColor=TEXT_MUTED, leading=11),
        'right': ParagraphStyle('right', fontName='AppFont',
                                fontSize=9, textColor=BRAND_DARK,
                                leading=13, alignment=TA_RIGHT),
        'right_bold': ParagraphStyle('right_bold', fontName='AppFont-Bold',
                                     fontSize=9, textColor=BRAND_DARK,
                                     leading=13, alignment=TA_RIGHT),
        'total': ParagraphStyle('total', fontName='AppFont-Bold',
                                fontSize=11, textColor=WHITE, leading=16,
                                alignment=TA_RIGHT),
        'footer': ParagraphStyle('footer', fontName='AppFont',
                                 fontSize=8, textColor=TEXT_MUTED,
                                 alignment=TA_CENTER, leading=11),
    }


def _header_table(order_number, order_type, status, date_str, s):
    """Dark branded header band."""
    left = [
        Paragraph(f"{'SALES' if order_type == 'SO' else 'PURCHASE'} INVOICE", s['title']),
        Spacer(1, 2),
        Paragraph(order_number, s['subtitle']),
    ]
    right_lines = [
        Paragraph(f"<b>Date:</b>  {date_str}", ParagraphStyle(
            'hdr_r', fontName='AppFont', fontSize=9,
            textColor=WHITE, alignment=TA_RIGHT, leading=13)),
        Paragraph(f"<b>Status:</b>  {status}", ParagraphStyle(
            'hdr_r2', fontName='AppFont', fontSize=9,
            textColor=colors.HexColor('#a5b4fc'), alignment=TA_RIGHT, leading=13)),
    ]
    tbl = Table([[left, right_lines]], colWidths=['60%', '40%'])
    tbl.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), BRAND_DARK),
        ('TOPPADDING',    (0, 0), (-1, -1), 14),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 14),
        ('LEFTPADDING',   (0, 0), (0, 0),   16),
        ('RIGHTPADDING',  (-1, 0), (-1, 0), 16),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    return tbl


def _info_block(label, lines, s):
    """A labelled address/info block."""
    parts = [Paragraph(label.upper(), s['section'])]
    for line in lines:
        parts.append(Paragraph(line, s['body']))
    return parts


def _items_table(rows, col_headers, col_widths, s):
    """Styled line-items table."""
    header_row = [Paragraph(h, ParagraphStyle(
        'th', fontName='AppFont-Bold', fontSize=8.5,
        textColor=WHITE, alignment=(TA_RIGHT if i > 0 else TA_LEFT), leading=12))
        for i, h in enumerate(col_headers)]

    data = [header_row]
    for i, row in enumerate(rows):
        styled = []
        for j, cell in enumerate(row):
            align = TA_RIGHT if j > 0 else TA_LEFT
            bg = BRAND_LIGHT if i % 2 == 0 else WHITE
            styled.append(Paragraph(str(cell), ParagraphStyle(
                f'td_{i}_{j}', fontName='AppFont', fontSize=8.5,
                textColor=BRAND_DARK, alignment=align, leading=12)))
        data.append(styled)

    tbl = Table(data, colWidths=col_widths)
    style_cmds = [
        ('BACKGROUND',    (0, 0), (-1, 0),  BRAND_ACCENT),
        ('ROWBACKGROUNDS',(0, 1), (-1, -1), [BRAND_LIGHT, WHITE]),
        ('GRID',          (0, 0), (-1, -1), 0.4, BRAND_BORDER),
        ('TOPPADDING',    (0, 0), (-1, -1), 7),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 7),
        ('LEFTPADDING',   (0, 0), (-1, -1), 8),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 8),
        ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
    ]
    tbl.setStyle(TableStyle(style_cmds))
    return tbl


def _totals_block(subtotal, discount, grand_total, currency, s):
    """Right-aligned totals section."""
    rows = []
    if discount and float(discount) > 0:
        rows += [
            [Paragraph('Subtotal', s['right']),
             Paragraph(f"{currency}{subtotal:,.2f}", s['right'])],
            [Paragraph('Discount', ParagraphStyle('disc', fontName='AppFont',
                       fontSize=9, textColor=SUCCESS, alignment=TA_RIGHT, leading=13)),
             Paragraph(f"- {currency}{float(discount):,.2f}",
                       ParagraphStyle('discv', fontName='AppFont', fontSize=9,
                                      textColor=SUCCESS, alignment=TA_RIGHT, leading=13))],
        ]
    # Grand total row
    grand_row = [
        Table([[Paragraph('GRAND TOTAL', s['total'])]], colWidths=['*'],
              style=[('BACKGROUND', (0,0),(-1,-1), BRAND_ACCENT),
                     ('TOPPADDING',(0,0),(-1,-1),8),
                     ('BOTTOMPADDING',(0,0),(-1,-1),8),
                     ('LEFTPADDING',(0,0),(-1,-1),10),
                     ('RIGHTPADDING',(0,0),(-1,-1),10)]),
        Table([[Paragraph(f"{currency}{float(grand_total):,.2f}", s['total'])]], colWidths=['*'],
              style=[('BACKGROUND', (0,0),(-1,-1), BRAND_ACCENT),
                     ('TOPPADDING',(0,0),(-1,-1),8),
                     ('BOTTOMPADDING',(0,0),(-1,-1),8),
                     ('LEFTPADDING',(0,0),(-1,-1),10),
                     ('RIGHTPADDING',(0,0),(-1,-1),10)]),
    ]
    rows.append(grand_row)

    tbl = Table(rows, colWidths=['55%', '45%'])
    tbl.setStyle(TableStyle([
        ('LINEABOVE', (0, 0), (-1, 0), 0.5, BRAND_BORDER),
        ('TOPPADDING',    (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING',   (0, 0), (-1, -1), 6),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 6),
    ]))
    return tbl


# ── Sales Order Invoice ───────────────────────────────────────────────────────

def generate_sales_invoice(so) -> BytesIO:
    """
    Generate a PDF invoice for a SalesOrder ORM object.
    Returns a BytesIO buffer — no files written to disk.
    """
    s = _styles()
    buffer = BytesIO()

    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=MARGIN,
        title=f"Sales Invoice {so.so_number}",
    )

    story = []

    # ── Header ────────────────────────────────────────────────────────────────
    date_str = so.order_date.strftime('%d %b %Y') if so.order_date else '—'
    story.append(_header_table(so.so_number, 'SO', so.status, date_str, s))
    story.append(Spacer(1, 8 * mm))

    # ── Bill To / Ship From ───────────────────────────────────────────────────
    cust = so.customer
    site = so.warehouse

    bill_to = _info_block('Bill To', [
        str(cust.name)  if cust and cust.name  else 'Walk-in Customer',
        str(cust.email) if cust and cust.email else '',
        str(cust.phone) if cust and getattr(cust, 'phone', None) else '',
        str(so.shipping_address) if so.shipping_address else '',
    ], s)

    ship_from = _info_block('Fulfilled By', [
        str(site.site_name) if site else str(so.warehouse_id or '—'),
        str(site.state_id)  if site and getattr(site, 'state_id', None) else '',
        f"Transport: {so.transport}" if so.transport else '',
    ], s)

    info_tbl = Table([[bill_to, ship_from]], colWidths=['50%', '50%'])
    info_tbl.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING',  (0, 0), (0, 0), 0),
        ('RIGHTPADDING', (-1, 0), (-1, 0), 0),
    ]))
    story.append(info_tbl)
    story.append(Spacer(1, 6 * mm))
    story.append(HRFlowable(width='100%', thickness=0.5, color=BRAND_BORDER))
    story.append(Spacer(1, 4 * mm))

    # ── Line Items ────────────────────────────────────────────────────────────
    story.append(Paragraph('ORDER ITEMS', s['section']))
    story.append(Spacer(1, 2 * mm))

    col_headers = ['Product', 'Qty', 'Unit Price', 'Line Total']
    usable_w = PAGE_W - 2 * MARGIN
    col_widths = [usable_w * 0.45, usable_w * 0.12, usable_w * 0.22, usable_w * 0.21]

    subtotal = 0.0
    rows = []
    for item in so.items:
        prod_name = item.product.product_name if item.product else str(item.product_id or '—')
        qty       = item.quantity
        price     = float(item.unit_price or 0)
        line      = float(item.line_total or qty * price)
        subtotal += line
        rows.append([str(prod_name), str(qty), f"₹{price:,.2f}", f"₹{line:,.2f}"])

    story.append(_items_table(rows, col_headers, col_widths, s))
    story.append(Spacer(1, 4 * mm))

    # ── Totals ────────────────────────────────────────────────────────────────
    discount    = float(so.discount or 0)
    grand_total = float(so.total_amount or subtotal - discount)

    totals_wrap = Table(
        [[Spacer(1, 1), _totals_block(subtotal, discount, grand_total, '₹', s)]],
        colWidths=['40%', '60%']
    )
    totals_wrap.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING',  (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (-1, 0), (-1, -1), 0),
    ]))
    story.append(totals_wrap)

    # ── Notes ─────────────────────────────────────────────────────────────────
    if so.notes:
        story.append(Spacer(1, 5 * mm))
        story.append(HRFlowable(width='100%', thickness=0.5, color=BRAND_BORDER))
        story.append(Spacer(1, 3 * mm))
        story.append(Paragraph('NOTES', s['section']))
        story.append(Paragraph(str(so.notes), s['body']))

    # ── Footer ────────────────────────────────────────────────────────────────
    story.append(Spacer(1, 8 * mm))
    story.append(HRFlowable(width='100%', thickness=0.5, color=BRAND_BORDER))
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph(
        f"Generated by IMS • {datetime.now().strftime('%d %b %Y %H:%M')} • "
        f"This is a computer-generated document and requires no signature.",
        s['footer']
    ))

    doc.build(story)
    buffer.seek(0)
    return buffer


# ── Purchase Order Invoice ────────────────────────────────────────────────────

def generate_purchase_invoice(po) -> BytesIO:
    """
    Generate a PDF invoice for a PurchaseOrder ORM object.
    Returns a BytesIO buffer — no files written to disk.
    """
    from app.models import Supplier, Site
    s = _styles()
    buffer = BytesIO()

    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=MARGIN,
        title=f"Purchase Invoice {po.po_number}",
    )

    story = []

    # ── Header ────────────────────────────────────────────────────────────────
    date_str = po.order_date.strftime('%d %b %Y') if po.order_date else '—'
    story.append(_header_table(po.po_number, 'PO', po.status, date_str, s))
    story.append(Spacer(1, 8 * mm))

    # ── Supplier / Warehouse ──────────────────────────────────────────────────
    supplier = Supplier.query.get(po.supplier_id) if po.supplier_id else None
    site     = Site.query.get(po.warehouse_id)   if po.warehouse_id else None

    supplier_info = _info_block('Supplier', [
        str(supplier.supplier_name) if supplier else '—',
        str(supplier.contact_email) if supplier and getattr(supplier, 'contact_email', None) else '',
        str(supplier.contact_phone) if supplier and getattr(supplier, 'contact_phone', None) else '',
        str(supplier.address)       if supplier and getattr(supplier, 'address',       None) else '',
    ], s)

    delivery_info = _info_block('Deliver To', [
        str(site.site_name) if site else str(po.warehouse_id or '—'),
        str(site.state_id)  if site and getattr(site, 'state_id', None) else '',
        f"Expected: {po.expected_delivery.strftime('%d %b %Y')}" if po.expected_delivery else '',
    ], s)

    info_tbl = Table([[supplier_info, delivery_info]], colWidths=['50%', '50%'])
    info_tbl.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING',  (0, 0), (0, 0), 0),
        ('RIGHTPADDING', (-1, 0), (-1, 0), 0),
    ]))
    story.append(info_tbl)
    story.append(Spacer(1, 6 * mm))
    story.append(HRFlowable(width='100%', thickness=0.5, color=BRAND_BORDER))
    story.append(Spacer(1, 4 * mm))

    # ── Line Items ────────────────────────────────────────────────────────────
    story.append(Paragraph('ORDERED ITEMS', s['section']))
    story.append(Spacer(1, 2 * mm))

    col_headers = ['Product', 'Qty Ordered', 'Unit Price', 'Line Total']
    usable_w = PAGE_W - 2 * MARGIN
    col_widths = [usable_w * 0.42, usable_w * 0.15, usable_w * 0.22, usable_w * 0.21]

    subtotal = 0.0
    rows = []
    for item in po.items:
        prod_name = item.product.product_name if item.product else str(item.product_id or '—')
        qty       = item.quantity
        price     = float(item.unit_price or 0)
        line      = float(item.line_total or qty * price)
        subtotal += line
        rows.append([str(prod_name), str(qty), f"₹{price:,.2f}", f"₹{line:,.2f}"])

    story.append(_items_table(rows, col_headers, col_widths, s))
    story.append(Spacer(1, 4 * mm))

    # ── Totals ────────────────────────────────────────────────────────────────
    grand_total = float(po.total_amount or subtotal)

    totals_wrap = Table(
        [[Spacer(1, 1), _totals_block(subtotal, 0, grand_total, '₹', s)]],
        colWidths=['40%', '60%']
    )
    totals_wrap.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING',  (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (-1, 0), (-1, -1), 0),
    ]))
    story.append(totals_wrap)

    # ── Notes ─────────────────────────────────────────────────────────────────
    if po.notes:
        story.append(Spacer(1, 5 * mm))
        story.append(HRFlowable(width='100%', thickness=0.5, color=BRAND_BORDER))
        story.append(Spacer(1, 3 * mm))
        story.append(Paragraph('NOTES', s['section']))
        story.append(Paragraph(str(po.notes), s['body']))

    # ── Footer ────────────────────────────────────────────────────────────────
    story.append(Spacer(1, 8 * mm))
    story.append(HRFlowable(width='100%', thickness=0.5, color=BRAND_BORDER))
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph(
        f"Generated by IMS • {datetime.now().strftime('%d %b %Y %H:%M')} • "
        f"This is a computer-generated document and requires no signature.",
        s['footer']
    ))

    doc.build(story)
    buffer.seek(0)
    return buffer
