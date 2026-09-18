"""OCR (ticket #15, spec §34, §43).

OCR activates only when needed: image sources, scanned PDFs, or low
extracted text density (the parser marks these needs_ocr). The engine is
configurable and CPU-only; when OCR is disabled or the engine is missing,
failures are actionable rather than dead ends.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Protocol


class OCRProvider(Protocol):
    name: str

    def available(self) -> bool: ...

    def extract_text(self, path: Path) -> str: ...


class TesseractProvider:
    name = "tesseract"

    def available(self) -> bool:
        return shutil.which("tesseract") is not None

    def extract_text(self, path: Path) -> str:
        import pytesseract
        from PIL import Image

        image = Image.open(path)
        return pytesseract.image_to_string(image) or ""


class NullProvider:
    name = "none"

    def available(self) -> bool:
        return False

    def extract_text(self, path: Path) -> str:
        raise RuntimeError("OCR engine is not available")


_PROVIDERS = {"tesseract": TesseractProvider, "none": NullProvider}


def get_ocr_provider(engine: str | None) -> OCRProvider:
    cls = _PROVIDERS.get(engine or "none", NullProvider)
    return cls()


def ocr_enabled(conn) -> bool:  # noqa: ANN001
    row = conn.execute(
        "SELECT value FROM settings WHERE key = 'ocr_enabled'"
    ).fetchone()
    return row is not None and row["value"] == "1"


def set_ocr_enabled(conn, enabled: bool, engine: str = "tesseract") -> None:  # noqa: ANN001
    conn.execute(
        "INSERT INTO settings (key, value) VALUES ('ocr_enabled', ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        ("1" if enabled else "0",),
    )
    conn.execute(
        "INSERT INTO settings (key, value) VALUES ('ocr_engine', ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (engine,),
    )
    conn.commit()


def ocr_document(path: Path, engine: str | None) -> str:
    """Extract text via OCR or raise an actionable error (§43)."""
    provider = get_ocr_provider(engine)
    if not provider.available():
        raise OCRError(
            f"OCR is enabled but the configured engine '{provider.name}' is not "
            "installed on this machine.\nActions: [Install tesseract] "
            "[Retry] [Disable OCR]"
        )
    try:
        return provider.extract_text(path)
    except OCRError:
        raise
    except Exception as e:  # noqa: BLE001
        raise OCRError(f"OCR failed on {path.name}: {e}\nActions: [Retry] [Disable OCR]")


class OCRError(Exception):
    pass


def ocr_output_path(parsed_dir: Path, document_id: str, page: int | None) -> Path:
    """OCR output preserves page references where possible (§34)."""
    suffix = f"_p{page}" if page else ""
    return parsed_dir / f"{document_id}{suffix}.ocr.txt"
