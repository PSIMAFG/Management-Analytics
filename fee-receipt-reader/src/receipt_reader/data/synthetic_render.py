"""Escritura de las boletas sintéticas como PDF nativo, PDF escaneado o imagen.

El diseño sigue el orden de etiquetas de una boleta de honorarios
electrónica (encabezado del emisor, recuadro con el número, fecha, bloque
'Señor(es)', glosa y totales), sin logos ni timbres y con la marca visible
"EJEMPLO SINTÉTICO - SIN VALIDEZ TRIBUTARIA".

Los documentos "escaneados" se rasterizan con una leve rotación, ruido y
compresión JPEG, todo con semilla fija para que los archivos (y su hash)
sean idénticos en cada generación. Para cada página rasterizada se entregan
también las cajas de texto que devolvería un OCR, usadas para poblar la
base de ejemplo sin ejecutar el OCR real.
"""

from __future__ import annotations

import io
import random
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageChops, ImageFilter
from reportlab.lib.pagesizes import letter
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

from receipt_reader.data.synthetic import (
    ORGANIZATION_ADDRESS,
    DocFormat,
    PageSpec,
    SyntheticDocument,
    SyntheticReceipt,
    format_money,
    printed_rate,
    printed_rut,
)
from receipt_reader.domain.layout import TextBox
from receipt_reader.domain.text import MONTH_NAMES

PAGE_WIDTH, PAGE_HEIGHT = letter
WATERMARK = "EJEMPLO SINTÉTICO - SIN VALIDEZ TRIBUTARIA"
FOOTNOTE = "Documento generado con datos ficticios para pruebas del sistema."
# Etiqueta que antecede a la glosa, tal como la imprime una boleta electrónica.
SERVICE_LABEL = "Por atenci\u00f3n profesional:"
POINTS_PER_INCH = 72
FIXED_PDF_TIME = time.struct_time((2026, 1, 1, 0, 0, 0, 3, 1, 0))


@dataclass(frozen=True)
class LayoutItem:
    """Texto posicionado en la página (puntos, con y medida desde arriba hasta la línea base)."""

    key: str
    text: str
    x: float
    y: float
    size: float
    bold: bool = False
    right_aligned: bool = False
    gray: float = 0.0

    @property
    def font(self) -> str:
        return "Helvetica-Bold" if self.bold else "Helvetica"

    @property
    def width(self) -> float:
        return stringWidth(self.text, self.font, self.size)

    @property
    def left(self) -> float:
        return self.x - self.width if self.right_aligned else self.x

    def box(self, scale: float) -> tuple[float, float, float, float]:
        """Rectángulo (izquierda, arriba, derecha, abajo) en píxeles para una escala dada."""
        top = self.y - self.size * 0.78
        bottom = self.y + self.size * 0.22
        return self.left * scale, top * scale, (self.left + self.width) * scale, bottom * scale


def receipt_items(receipt: SyntheticReceipt) -> list[LayoutItem]:
    """Elementos de una boleta, con las posiciones relativas de una boleta electrónica."""
    provider = receipt.provider
    month = MONTH_NAMES[receipt.issue_date.month - 1].capitalize()
    folio_text = f"N ° {receipt.folio}" if receipt.print_folio else "N °"
    items = [
        LayoutItem("header", "BOLETA DE HONORARIOS", 421, 86, 10, bold=True),
        LayoutItem("name", provider.name.upper(), 150, 100, 11, bold=True),
        LayoutItem("electronica", "ELECTRONICA", 452, 100, 10, bold=True),
        LayoutItem("folio", folio_text, 466, 118, 10, bold=True),
        LayoutItem("rut", f"RUT: {printed_rut(provider.rut)}", 216, 132, 9, bold=True),
        LayoutItem("activity", provider.activity, 115, 146, 8),
        LayoutItem("address", provider.address.upper(), 114, 160, 8),
        LayoutItem("phone", f"TELEFONO: {provider.phone}", 211, 174, 8),
        LayoutItem("date", f"Fecha: {receipt.issue_date.day:02d} de {month} de {receipt.issue_date.year}", 404, 206, 9),
        LayoutItem("receiver", f"Señor(es): {receipt.receiver_name.upper()}", 93, 232, 9),
        LayoutItem("receiver_rut", f"Rut: {printed_rut(receipt.receiver_rut)}", 419, 232, 9),
        LayoutItem("receiver_address", f"Domicilio: {ORGANIZATION_ADDRESS.upper()}", 93, 245, 9),
        LayoutItem("service", SERVICE_LABEL, 93, 270, 9),
    ]
    y = 284.0
    for index, line in enumerate(receipt.gloss_lines):
        items.append(LayoutItem(f"gloss_{index}", line, 94, y, 8.5))
        y += 11
    items.append(LayoutItem("gloss_amount", format_money(receipt.gross), 537, 284, 9, right_aligned=True))
    y += 12
    rate = printed_rate(receipt.rate_bp, receipt.rate_text_style)
    totals = (
        ("gross", "Total Honorarios: $:", format_money(receipt.gross)),
        ("retention", f"{rate} % Impto. Retenido:", format_money(receipt.retention)),
        ("net", "Total:", format_money(receipt.net)),
    )
    for key, label, amount in totals:
        items.append(LayoutItem(f"{key}_label", label, 417, y, 9, right_aligned=True))
        items.append(LayoutItem(f"{key}_amount", amount, 537, y, 9, right_aligned=True))
        y += 13
    stamp = f"{receipt.issue_date:%d/%m/%Y} {receipt.emission_time}"
    items.append(LayoutItem("emission", f"Fecha / Hora Emisión: {stamp}", 175, y + 20, 8))
    items.append(
        LayoutItem("watermark", WATERMARK, PAGE_WIDTH / 2 + 150, y + 70, 13, bold=True, right_aligned=True, gray=0.55)
    )
    items.append(LayoutItem("footnote", FOOTNOTE, 93, y + 88, 7.5, gray=0.45))
    return items


def annex_items(page: PageSpec) -> list[LayoutItem]:
    items = [LayoutItem("annex_title", page.annex_title, 93, 110, 13, bold=True)]
    y = 150.0
    for index, line in enumerate(page.annex_lines):
        items.append(LayoutItem(f"annex_{index}", line, 93, y, 10))
        y += 20
    items.append(
        LayoutItem("watermark", WATERMARK, PAGE_WIDTH / 2 + 150, y + 60, 13, bold=True, right_aligned=True, gray=0.55)
    )
    return items


def page_items(page: PageSpec) -> list[LayoutItem]:
    return receipt_items(page.receipt) if page.receipt is not None else annex_items(page)


def draw_pdf(pages: Sequence[Sequence[LayoutItem]]) -> bytes:
    """PDF con texto nativo. `invariant` fija metadatos para que los bytes sean reproducibles."""
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter, invariant=1, pageCompression=1)
    pdf.setTitle("Boleta de honorarios de ejemplo")
    pdf.setAuthor(WATERMARK)
    for items in pages:
        is_receipt = any(item.key == "header" for item in items)
        if is_receipt:
            pdf.setLineWidth(0.8)
            pdf.rect(410, PAGE_HEIGHT - 124, 136, 52)
        for item in items:
            pdf.setFillGray(item.gray)
            pdf.setFont(item.font, item.size)
            pdf.drawString(item.left, PAGE_HEIGHT - item.y, item.text)
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def _noise_layer(size: tuple[int, int], amount: float, rng: random.Random) -> Image.Image:
    raw = Image.frombytes("L", size, rng.randbytes(size[0] * size[1]))
    strength = max(amount, 0.0)
    # Ruido suave generalizado más algunos puntos oscuros aislados, como en un escaneo real.
    return raw.point(lambda v: int((v / 255) ** 6 * 90 * strength) + (140 if v > 253 and strength > 0.2 else 0))


def rasterize(pdf_bytes: bytes, dpi: int, noise: float, seed: int) -> list[Image.Image]:
    """Imágenes en escala de grises con rotación leve, ruido y desenfoque (deterministas)."""
    rng = random.Random(seed)
    document = pdfium.PdfDocument(pdf_bytes)
    images: list[Image.Image] = []
    try:
        for index in range(len(document)):
            page = document[index]
            try:
                image = page.render(scale=dpi / POINTS_PER_INCH, grayscale=True).to_pil().convert("L")
            finally:
                page.close()
            angle = rng.uniform(-0.8, 0.8) * max(noise, 0.2)
            image = image.rotate(angle, resample=Image.Resampling.BILINEAR, expand=False, fillcolor=255)
            image = ImageChops.subtract(image, _noise_layer(image.size, noise, rng))
            if noise > 0:
                image = image.filter(ImageFilter.GaussianBlur(radius=0.35 + 0.5 * noise))
            images.append(image)
    finally:
        document.close()
    return images


def _jpeg_bytes(image: Image.Image, quality: int = 72) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality, optimize=False)
    return buffer.getvalue()


def images_to_pdf(images: Sequence[Image.Image], dpi: int) -> bytes:
    """PDF "escaneado": cada página es una imagen JPEG sin capa de texto.

    Las fechas del documento se fijan para que el archivo sea idéntico en cada generación.
    """
    buffer = io.BytesIO()
    first, *rest = images
    first.save(
        buffer,
        format="PDF",
        save_all=True,
        append_images=rest,
        resolution=float(dpi),
        quality=72,
        title="Boleta de honorarios de ejemplo",
        author=WATERMARK,
        creationDate=FIXED_PDF_TIME,
        modDate=FIXED_PDF_TIME,
    )
    return buffer.getvalue()


def ocr_boxes(
    items: Sequence[LayoutItem],
    *,
    dpi: int,
    confidence: tuple[float, float],
    overrides: dict[str, str],
    rng: random.Random,
) -> list[TextBox]:
    """Cajas que devolvería el OCR para una página: texto por línea impresa, con su confianza."""
    scale = dpi / POINTS_PER_INCH
    boxes: list[TextBox] = []
    low, high = confidence
    for item in items:
        text = overrides.get(item.key, item.text).replace("\u2212", "-")
        left, top, right, bottom = item.box(scale)
        boxes.append(TextBox(text, left, top, right, bottom, round(rng.uniform(low, high), 4)))
    return boxes


def _corrupt_bytes(rng: random.Random) -> bytes:
    return b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n" + rng.randbytes(2048)


def render_document(document: SyntheticDocument, *, dpi: int, seed: int) -> tuple[bytes, list[list[TextBox]]]:
    """Bytes del archivo y, para las páginas rasterizadas, las cajas del OCR simulado."""
    rng = random.Random(seed)
    pages = [page_items(page) for page in document.pages]
    pdf = draw_pdf(pages)
    if document.doc_format is DocFormat.NATIVE_PDF:
        return pdf, []
    if document.doc_format is DocFormat.CORRUPT_PDF:
        return _corrupt_bytes(rng), []
    images = rasterize(pdf, dpi, document.noise, seed)
    boxes = []
    for page, items in zip(document.pages, pages, strict=True):
        overrides = page.receipt.ocr_overrides if page.receipt is not None else {}
        boxes.append(ocr_boxes(items, dpi=dpi, confidence=document.ocr_confidence, overrides=overrides, rng=rng))
    if document.doc_format is DocFormat.PNG:
        buffer = io.BytesIO()
        images[0].save(buffer, format="PNG", optimize=False)
        return buffer.getvalue(), boxes[:1]
    if document.doc_format is DocFormat.JPG:
        return _jpeg_bytes(images[0], quality=80), boxes[:1]
    return images_to_pdf(images, dpi), boxes


def write_documents(
    documents: Sequence[SyntheticDocument], root: Path, *, dpi: int, seed: int
) -> dict[tuple[str, int], list[TextBox]]:
    """Escribe los archivos bajo `root` y devuelve las cajas del OCR simulado por (ruta, página)."""
    written: dict[str, bytes] = {}
    boxes_by_document: dict[str, list[list[TextBox]]] = {}
    ocr_pages: dict[tuple[str, int], list[TextBox]] = {}
    for index, document in enumerate(documents):
        target = root / document.relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if document.copy_of is not None:
            data = written[document.copy_of]
            page_boxes = boxes_by_document.get(document.copy_of, [])
        else:
            data, page_boxes = render_document(document, dpi=dpi, seed=seed * 1000 + index)
        target.write_bytes(data)
        written[document.relative_path] = data
        boxes_by_document[document.relative_path] = page_boxes
        for page_index, boxes in enumerate(page_boxes):
            ocr_pages[(str(target), page_index)] = boxes
    return ocr_pages
