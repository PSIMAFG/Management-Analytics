"""Lectura de archivos de boletas: PDF (nativo o escaneado) e imágenes.

Por cada página se intenta primero el texto nativo con pypdfium2, tomando
cada segmento con su rectángulo para reconstruir las líneas por posición.
Si la página no tiene texto útil, se rasteriza y se pasa al OCR. Los
archivos se leen como bytes (sirve para el hash y evita problemas con rutas
que tienen tildes o ñ en Windows).
"""

from __future__ import annotations

import hashlib
import io
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageOps, UnidentifiedImageError

from receipt_reader.data.ocr import OcrEngine, PageImageSource
from receipt_reader.domain.layout import TextBox, TextLine, build_lines, mean_confidence
from receipt_reader.domain.models import TextKind
from receipt_reader.errors import DataError

log = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = frozenset({".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"})
IMAGE_EXTENSIONS = SUPPORTED_EXTENSIONS - {".pdf"}
# Caracteres alfanuméricos mínimos para considerar que una página PDF tiene texto nativo útil.
MIN_NATIVE_CHARS = 40
POINTS_PER_INCH = 72


class ReadCancelledError(Exception):
    """El usuario pidió cancelar mientras se leía un archivo."""


@dataclass(frozen=True)
class PageReading:
    index: int
    text_kind: TextKind
    lines: tuple[TextLine, ...]
    ocr_confidence: float | None


@dataclass(frozen=True)
class DocumentReading:
    path: Path
    sha256: str
    page_count: int
    pages: tuple[PageReading, ...] = ()
    error: str | None = None


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def list_input_files(root: Path) -> list[Path]:
    """Archivos admitidos bajo la carpeta, recorrida en forma recursiva y en orden estable."""
    if not root.is_dir():
        raise DataError(f"La carpeta de entrada no existe: {root}")
    found = [
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix.lower() in SUPPORTED_EXTENSIONS
        and not any(part.startswith(".") for part in path.relative_to(root).parts)
    ]
    return sorted(found, key=lambda p: str(p.relative_to(root)).casefold())


def _native_boxes(page: pdfium.PdfPage) -> list[TextBox]:
    textpage = page.get_textpage()
    try:
        height = page.get_height()
        boxes: list[TextBox] = []
        for index in range(textpage.count_rects()):
            left, bottom, right, top = textpage.get_rect(index)
            text = textpage.get_text_bounded(left, bottom, right, top)
            if text and text.strip():
                boxes.append(TextBox(text, left, height - top, right, height - bottom, 1.0))
        return boxes
    finally:
        textpage.close()


def _alnum_count(boxes: list[TextBox]) -> int:
    return sum(char.isalnum() for box in boxes for char in box.text)


def _check_cancel(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise ReadCancelledError


def _ocr_page(ocr: OcrEngine, source: PageImageSource) -> tuple[TextKind, tuple[TextLine, ...], float | None]:
    lines = tuple(build_lines(ocr.recognize(source)))
    if not lines:
        return TextKind.NONE, (), None
    return TextKind.OCR, lines, mean_confidence(lines)


def _read_pdf(
    path: Path,
    data: bytes,
    ocr: OcrEngine,
    dpi: int,
    cancel: threading.Event | None,
    on_page: Callable[[int, int], None] | None,
) -> tuple[int, tuple[PageReading, ...]]:
    pdf = pdfium.PdfDocument(data)
    try:
        total = len(pdf)
        pages: list[PageReading] = []
        for index in range(total):
            _check_cancel(cancel)
            page = pdf[index]
            try:
                boxes = _native_boxes(page)
                if _alnum_count(boxes) >= MIN_NATIVE_CHARS:
                    lines = tuple(build_lines(boxes))
                    pages.append(PageReading(index, TextKind.NATIVE, lines, None))
                else:

                    def render(page: pdfium.PdfPage = page) -> Image.Image:
                        return page.render(scale=dpi / POINTS_PER_INCH).to_pil()

                    kind, lines, confidence = _ocr_page(ocr, PageImageSource(path, index, render))
                    pages.append(PageReading(index, kind, lines, confidence))
            finally:
                page.close()
            if on_page is not None:
                on_page(index + 1, total)
        return total, tuple(pages)
    finally:
        pdf.close()


def _load_frame(data: bytes, index: int) -> Image.Image:
    with Image.open(io.BytesIO(data)) as image:
        image.seek(index)
        frame = image.convert("RGB")
        return ImageOps.exif_transpose(frame) if index == 0 else frame


def _read_image(
    path: Path,
    data: bytes,
    ocr: OcrEngine,
    cancel: threading.Event | None,
    on_page: Callable[[int, int], None] | None,
) -> tuple[int, tuple[PageReading, ...]]:
    with Image.open(io.BytesIO(data)) as image:
        total = int(getattr(image, "n_frames", 1))
    pages: list[PageReading] = []
    for index in range(total):
        _check_cancel(cancel)

        def render(index: int = index) -> Image.Image:
            return _load_frame(data, index)

        kind, lines, confidence = _ocr_page(ocr, PageImageSource(path, index, render))
        pages.append(PageReading(index, kind, lines, confidence))
        if on_page is not None:
            on_page(index + 1, total)
    return total, tuple(pages)


def read_document(
    path: Path,
    *,
    ocr: OcrEngine,
    dpi: int = 200,
    cancel: threading.Event | None = None,
    on_page: Callable[[int, int], None] | None = None,
    data: bytes | None = None,
) -> DocumentReading:
    """Lee todas las páginas de un archivo. Un archivo dañado no lanza error: se informa en `error`.

    `data` permite pasar el contenido ya leído (por ejemplo, para calcular antes el hash).
    """
    if data is None:
        try:
            data = path.read_bytes()
        except OSError as error:
            log.warning("No se pudo abrir %s: %s", path, error)
            return DocumentReading(path, "", 0, error=f"No se pudo abrir el archivo: {error.strerror or error}")
    digest = sha256_of(data)
    try:
        if path.suffix.lower() == ".pdf":
            total, pages = _read_pdf(path, data, ocr, dpi, cancel, on_page)
        else:
            total, pages = _read_image(path, data, ocr, cancel, on_page)
    except ReadCancelledError:
        raise
    except (pdfium.PdfiumError, UnidentifiedImageError, OSError, ValueError) as error:
        log.warning("Archivo ilegible %s: %s", path, error)
        return DocumentReading(
            path, digest, 0, error=f"El archivo está dañado o no es un PDF o imagen válido ({error})."
        )
    return DocumentReading(path, digest, total, pages)


def render_page(path: Path, page_index: int, dpi: int = 110) -> Image.Image:
    """Imagen de una página para la vista previa."""
    try:
        data = path.read_bytes()
    except OSError as error:
        raise DataError(f"El archivo original ya no está disponible: {path}") from error
    try:
        if path.suffix.lower() == ".pdf":
            pdf = pdfium.PdfDocument(data)
            try:
                page = pdf[page_index]
                try:
                    return page.render(scale=dpi / POINTS_PER_INCH).to_pil().convert("RGB")
                finally:
                    page.close()
            finally:
                pdf.close()
        return _load_frame(data, page_index)
    except (pdfium.PdfiumError, UnidentifiedImageError, OSError, ValueError, IndexError, EOFError) as error:
        raise DataError(f"No se pudo mostrar la página {page_index + 1} de {path.name}.") from error


def render_page_png(path: Path, page_index: int, dpi: int = 110) -> bytes:
    buffer = io.BytesIO()
    render_page(path, page_index, dpi).save(buffer, format="PNG")
    return buffer.getvalue()
