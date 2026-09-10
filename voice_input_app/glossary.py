"""Хранилище словаря терминов отдельным файлом (US-076).

Зачем отдельный файл. Раньше словарь лежал в `config.json` полем
`postprocess_glossary`. Из-за этого готовые настройки, которые раздают
коллегам, невозможно было подложить, не затерев словарь, который человек
собирал под себя. Теперь настройки и словарь разъехались: `config.json`
подменяется свободно, `dictionary.json` остаётся пользовательским.

Формат файла:

```json
{"version": 1, "entries": [{"term": "...", "distortions": "...",
                            "context": "...", "exclusions": "...",
                            "enabled": true}]}
```

Читается и «голый» список записей — так проще отдать словарь коллеге и так же
принять файл, собранный вручную.

Отсутствующий или повреждённый файл НЕ мешает работе (AC 5): возвращается
пустой словарь и признак `status`, по которому интерфейс показывает причину.
Повреждённый файл не перезаписывается молча — иначе одна опечатка в JSON
стоила бы человеку всех терминов.
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .logger import get_logger
from .paths import config_path, dictionary_path

log = get_logger("glossary")

FORMAT_VERSION = 1

# Поля записи словаря. Порядок — как в таблице на вкладке «Словарь».
TEXT_FIELDS = ("term", "distortions", "context", "exclusions")

STATUS_OK = "ok"            # файл прочитан
STATUS_MISSING = "missing"  # файла нет — это нормальное состояние до первого термина
STATUS_BROKEN = "broken"    # файл есть, но не разбирается


@dataclass
class LoadResult:
    """Результат чтения словаря: записи + почему их может не быть."""

    entries: list[dict] = field(default_factory=list)
    status: str = STATUS_MISSING
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status != STATUS_BROKEN


def path() -> Path:
    return dictionary_path()


def normalize_entry(raw: Any) -> dict | None:
    """Привести запись к нашему виду. None — запись непригодна.

    Запись без непустого `term` бессмысленна: подставлять нечего. Отсутствие
    ключа `enabled` означает «включён» — так записи из старых версий (US-044,
    до появления тумблера в US-046) продолжают работать.
    """
    if not isinstance(raw, dict):
        return None
    entry: dict = {}
    for name in TEXT_FIELDS:
        value = raw.get(name, "")
        entry[name] = "" if value is None else str(value).strip()
    if not entry["term"]:
        return None
    entry["enabled"] = bool(raw.get("enabled", True))
    return entry


def normalize_entries(raw: Any) -> list[dict]:
    """Отобрать пригодные записи из чего угодно, что пришло из файла."""
    if isinstance(raw, dict):
        raw = raw.get("entries", [])
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[dict] = []
    for item in raw:
        entry = normalize_entry(item)
        if entry is not None:
            out.append(entry)
    return out


def load(source: Path | None = None) -> LoadResult:
    """Прочитать словарь. Ошибка чтения не бросается наружу (AC 5)."""
    target = Path(source) if source is not None else path()
    if not target.exists():
        return LoadResult(entries=[], status=STATUS_MISSING, detail="")
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        log.warning("Файл словаря %s не читается: %s", target, exc)
        return LoadResult(entries=[], status=STATUS_BROKEN, detail=str(exc))
    entries = normalize_entries(raw)
    log.info("Словарь прочитан: %s, записей %d", target.name, len(entries))
    return LoadResult(entries=entries, status=STATUS_OK, detail="")


def load_entries() -> list[dict]:
    """Только записи — для мест, где причина пустого словаря не важна."""
    return load().entries


def save(entries: Any, destination: Path | None = None) -> None:
    """Записать словарь. Пишем через временный файл: обрыв записи (батарея,
    антивирус) не должен оставить пользователя с обрезанным JSON."""
    target = Path(destination) if destination is not None else path()
    payload = {"version": FORMAT_VERSION, "entries": normalize_entries(entries)}
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, target)
    log.info("Словарь сохранён: %s, записей %d", target.name, len(payload["entries"]))


def merge_entries(existing: Any, incoming: Any) -> list[dict]:
    """Добавить записи к существующим, не плодя дубликаты.

    Совпадением считается одинаковый термин без учёта регистра — при импорте
    присланного словаря запись человека остаётся его: свои поля (контекст,
    исключения, тумблер) он настраивал сам.
    """
    result = normalize_entries(existing)
    known = {e["term"].casefold() for e in result}
    for entry in normalize_entries(incoming):
        if entry["term"].casefold() in known:
            continue
        known.add(entry["term"].casefold())
        result.append(entry)
    return result


def export_to(destination: Path, entries: Any = None) -> int:
    """Сохранить словарь в указанный файл. Возвращает число записей."""
    data = normalize_entries(entries) if entries is not None else load_entries()
    save(data, destination=Path(destination))
    return len(data)


def import_from(source: Path) -> LoadResult:
    """Прочитать словарь из присланного файла (не трогая текущий)."""
    return load(Path(source))


# ---------- перенос словаря из config.json (AC 2) ----------

LEGACY_FIELD = "postprocess_glossary"


def migrate_from_config(source: Path | None = None) -> int:
    """Однократно перенести словарь из config.json в отдельный файл.

    Возвращает число перенесённых записей (0 — переносить было нечего).

    Повторный перенос невозможен по построению: как только `dictionary.json`
    существует, config.json больше не читается. Это же правило защищает AC 3 —
    подложенный файл настроек со СВОИМ словарём не затирает словарь человека.

    Ключ `postprocess_glossary` из config.json не вычищается здесь: поля с таким
    именем в AppConfig больше нет, поэтому достаточно сохранить настройки — файл
    перезапишется без него. Это делает вызывающий код (`MainWindow.__init__`)
    сразу после переноса. Отдельная правка чужого файла в обход AppConfig только
    добавила бы способ его испортить.
    """
    target = path()
    if target.exists():
        return 0
    cfg_file = Path(source) if source is not None else config_path()
    if not cfg_file.exists():
        return 0
    try:
        data = json.loads(cfg_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        log.warning("Перенос словаря пропущен: config.json не читается (%s)", exc)
        return 0
    if not isinstance(data, dict):
        return 0
    entries = normalize_entries(data.get(LEGACY_FIELD, []))
    if not entries:
        return 0
    # Резервная копия настроек до переноса: словарь у человека мог собираться
    # месяцами, и вернуть его должно быть возможно даже при нашей ошибке.
    try:
        shutil.copyfile(cfg_file, cfg_file.with_suffix(".pre-dictionary.bak"))
    except OSError:
        log.warning("Не удалось сделать резервную копию config.json перед переносом словаря", exc_info=True)
    save(entries)
    log.info("US-076: словарь перенесён из config.json в %s, записей %d", target.name, len(entries))
    return len(entries)
