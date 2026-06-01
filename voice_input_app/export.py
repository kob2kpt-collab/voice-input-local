"""US-024: экспорт расшифровок из истории в форматах TXT, Markdown и PDF.

Модуль не зависит от Qt — чистые функции, строящие содержимое файла из
HistoryItem. UI вызывает build_* и сам пишет результат на диск через
QFileDialog. PDF собирается через fpdf2 со встроенным кириллическим TTF
(см. paths.export_font_path)."""

from __future__ import annotations

import json
import re
from datetime import datetime

from .audio_files import format_duration
from .history import HistoryItem
from .paths import export_font_path

# Расширения, поддерживаемые экспортом
EXPORT_FORMATS = ("txt", "md", "pdf")


def _pretty_datetime(created_at: str) -> str:
    """ISO-строку created_at → человекочитаемая дата/время. При сбое — как есть."""
    try:
        dt = datetime.fromisoformat(created_at)
        return dt.strftime("%d.%m.%Y %H:%M")
    except (ValueError, TypeError):
        return created_at or ""


def _title_for(item: HistoryItem) -> str:
    """Заголовок записи: имя файла для расшифровки файла, иначе «Диктовка»."""
    if (item.file_name or "").strip():
        return item.file_name.strip()
    return "Диктовка" if item.source == "dictation" else "Расшифровка"


def _segments(item: HistoryItem) -> list[dict]:
    """Распарсить segments_json в список блоков. Пустой список при отсутствии/ошибке."""
    raw = (item.segments_json or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


def _body_plain(item: HistoryItem) -> str:
    """Текст расшифровки для TXT — чистый item.text."""
    return (item.text or "").strip()


def _body_markdown(item: HistoryItem) -> str:
    """Текст расшифровки для MD: если есть структурированные сегменты —
    форматируем с тайм-кодами и спикерами; иначе берём item.text как есть
    (для расшифровки файла он уже может содержать разметку)."""
    segs = _segments(item)
    has_speakers = any((s.get("speaker") or "").strip() for s in segs)
    has_times = any(("start" in s or "end" in s) for s in segs)
    if segs and (has_speakers or has_times):
        lines = []
        for s in segs:
            prefix = ""
            if has_times:
                prefix += f"`[{format_duration(float(s.get('start', 0.0)))}–{format_duration(float(s.get('end', 0.0)))}]` "
            sp = (s.get("speaker") or "").strip()
            if sp:
                prefix += f"**{sp}:** "
            txt = (s.get("text") or "").strip()
            if txt:
                lines.append(prefix + txt)
        if lines:
            return "\n\n".join(lines)
    return (item.text or "").strip()


# ---------------------------------------------------------------------------
# TXT
# ---------------------------------------------------------------------------

def build_txt(item: HistoryItem) -> str:
    """Чистый текст расшифровки + раздел «Резюме», если оно есть (AC2)."""
    parts = [_body_plain(item)]
    summary = (item.summary or "").strip()
    if summary:
        parts.append("")
        parts.append("=" * 40)
        parts.append("РЕЗЮМЕ")
        parts.append("=" * 40)
        parts.append(summary)
    return "\n".join(parts).strip() + "\n"


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def build_md(item: HistoryItem) -> str:
    """Форматированный Markdown: заголовок с датой, тело с тайм-кодами/спикерами
    (если есть) и раздел «Резюме» (AC3, AC5)."""
    title = _title_for(item)
    when = _pretty_datetime(item.created_at)
    lines = [f"# {title}", ""]
    meta = []
    if when:
        meta.append(f"**Дата:** {when}")
    if (item.model_key or "").strip():
        meta.append(f"**Модель:** {item.model_key}")
    if meta:
        lines.append("  ".join(meta))
        lines.append("")
    lines.append("## Расшифровка")
    lines.append("")
    lines.append(_body_markdown(item))
    summary = (item.summary or "").strip()
    if summary:
        lines.append("")
        lines.append("## Резюме")
        lines.append("")
        lines.append(summary)
    return "\n".join(lines).strip() + "\n"


# ---------------------------------------------------------------------------
# PDF (fpdf2 + кириллический TTF)
# ---------------------------------------------------------------------------

def build_pdf(item: HistoryItem) -> bytes:
    """PDF с заголовком, датой, текстом расшифровки и summary (AC4, AC5).

    Требует fpdf2 (ImportError пробрасывается наверх — UI покажет подсказку).
    Кириллица — через встроенный TTF DejaVuSans."""
    from fpdf import FPDF

    font_path = str(export_font_path())

    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.add_font("DejaVu", "", font_path)
    pdf.add_font("DejaVu", "B", font_path)

    title = _title_for(item)
    when = _pretty_datetime(item.created_at)

    pdf.set_font("DejaVu", "B", 16)
    pdf.multi_cell(0, 9, title)
    pdf.ln(1)

    meta_bits = []
    if when:
        meta_bits.append(f"Дата: {when}")
    if (item.model_key or "").strip():
        meta_bits.append(f"Модель: {item.model_key}")
    if meta_bits:
        pdf.set_font("DejaVu", "", 10)
        pdf.set_text_color(110, 110, 110)
        pdf.multi_cell(0, 6, "   ".join(meta_bits))
        pdf.set_text_color(0, 0, 0)
    pdf.ln(3)

    pdf.set_font("DejaVu", "B", 12)
    pdf.multi_cell(0, 7, "Расшифровка")
    pdf.ln(1)
    pdf.set_font("DejaVu", "", 11)
    body = _body_plain(item) or "(пусто)"
    pdf.multi_cell(0, 6, body)

    summary = (item.summary or "").strip()
    if summary:
        pdf.ln(4)
        pdf.set_font("DejaVu", "B", 12)
        pdf.multi_cell(0, 7, "Резюме")
        pdf.ln(1)
        pdf.set_font("DejaVu", "", 11)
        pdf.multi_cell(0, 6, summary)

    out = pdf.output()
    return bytes(out)


# ---------------------------------------------------------------------------
# Имя файла
# ---------------------------------------------------------------------------

_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize(name: str) -> str:
    """Убрать недопустимые для Windows символы из имени файла."""
    name = _INVALID_FILENAME_CHARS.sub("_", name)
    name = name.strip().strip(".")  # Windows не любит пробелы/точки в конце
    name = re.sub(r"\s+", " ", name)
    return name[:120] or "transcript"


def suggest_filename(item: HistoryItem, ext: str) -> str:
    """Предложить имя файла на основе даты и заголовка записи (AC6).

    Формат: «YYYY-MM-DD_HH-MM_<заголовок>.<ext>». Если разобрать дату не
    удалось — только заголовок."""
    ext = ext.lstrip(".").lower()
    stamp = ""
    try:
        dt = datetime.fromisoformat(item.created_at)
        stamp = dt.strftime("%Y-%m-%d_%H-%M")
    except (ValueError, TypeError):
        stamp = ""

    title = _title_for(item)
    # У расшифровки файла отбросим исходное расширение из имени
    if "." in title:
        title = title.rsplit(".", 1)[0]

    base = f"{stamp}_{title}" if stamp else title
    return f"{_sanitize(base)}.{ext}"
