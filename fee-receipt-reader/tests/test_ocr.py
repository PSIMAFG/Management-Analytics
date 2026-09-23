"""OCR real sobre una imagen sintética (un solo caso) y autoprueba del OCR."""

from __future__ import annotations

from pathlib import Path

from receipt_reader.data.ocr import SyntheticOcrEngine, boxes_from_polygons
from receipt_reader.data.synthetic import DocFormat, build_sample_set
from receipt_reader.data.synthetic_render import write_documents
from receipt_reader.services.processing import ProcessingService
from support import DemoEnvironment, fixed_clock


def test_polygons_become_axis_aligned_boxes() -> None:
    boxes = boxes_from_polygons([[[10, 5], [50, 7], [50, 20], [10, 18]]], ["Total:"], [0.97])
    box = boxes[0]
    assert (box.text, box.left, box.top, box.right, box.bottom, box.confidence) == ("Total:", 10, 5, 50, 20, 0.97)


def test_real_ocr_reads_a_synthetic_image(demo_env: DemoEnvironment) -> None:
    # Un solo caso con el motor real (RapidOCR): una imagen PNG de las muestras.
    documents = build_sample_set()
    sample = next(document for document in documents if document.doc_format is DocFormat.PNG)
    truth = sample.receipt
    assert truth is not None
    service = ProcessingService(demo_env.db_path, clock=fixed_clock)
    analysis = service.analyze_file(demo_env.samples_dir / sample.relative_path, demo_env.samples_dir)
    assert analysis.error is None
    page = analysis.receipt_pages[0]
    assert page.draft is not None
    data = page.draft.data
    assert data.issuer_rut == truth.provider.rut
    assert data.folio == truth.folio
    assert data.issue_date == truth.issue_date
    assert (data.gross, data.retention, data.net) == (truth.gross, truth.retention, truth.net)
    assert data.printed_rate_bp == truth.rate_bp
    assert data.service_period == truth.service_period
    assert page.draft.ocr_confidence is not None
    assert page.draft.ocr_confidence > 0.8
    # Las tildes y la eñe del nombre se reconocen (o, a lo más, se pierden sin afectar la extracción).
    assert data.issuer_name is not None


def test_ocr_self_check_with_a_known_engine(catalog_db: Path, tmp_path: Path) -> None:
    sample = next(document for document in build_sample_set() if document.doc_format is DocFormat.PNG)
    pages = write_documents([sample], tmp_path, dpi=100, seed=1)
    service = ProcessingService(catalog_db, ocr=SyntheticOcrEngine(pages), clock=fixed_clock)
    assert service.check_ocr(tmp_path) == []
    empty = ProcessingService(catalog_db, ocr=SyntheticOcrEngine({}), clock=fixed_clock)
    assert empty.check_ocr(tmp_path) == [f"El OCR no encontró una boleta en {Path(sample.relative_path).name}."]


def test_ocr_self_check_without_images(catalog_db: Path, tmp_path: Path) -> None:
    service = ProcessingService(catalog_db, ocr=SyntheticOcrEngine({}), clock=fixed_clock)
    problems = service.check_ocr(tmp_path)
    assert len(problems) == 1
    assert "No hay imágenes de muestra" in problems[0]
    assert "no existe" in service.check_ocr(tmp_path / "no_existe")[0]
