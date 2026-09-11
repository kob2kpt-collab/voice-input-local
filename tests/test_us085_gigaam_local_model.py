# -*- coding: utf-8 -*-
r"""Регресс-тесты US-085: локальная модель GigaAM v3 E2E RNNT (Sber).

Пользователю, которому нельзя отправлять записи в облако, нужна современная
локальная модель распознавания наравне с Whisper и Parakeet. Выбрана GigaAM v3
E2E RNNT (ai-sage/GigaAM-v3) в ONNX-экспорте istupakov/gigaam-v3-onnx: тот же
onnx-asr, что уже обслуживает Parakeet, лицензия MIT, текст сразу с
пунктуацией и заглавными буквами (та же модель, что в «Тайпе» Сбера).
GigaAM Multilingual (5 языков, CTC без пунктуации) рассматривалась и заменена
этой моделью решением владельца. Модель NVIDIA Nemotron 3.5 ASR не добавлена:
onnx-asr её не поддерживает (нужен новый рантайм), а опубликованный WER на
русском втрое хуже whisper:large-v3-turbo (TASK-369).

Что закреплено тестами:

* AC 2 — модель заведена в каталог локальных моделей и видна наравне с
  Whisper и Parakeet (попадает в TRANSCRIPTION_MODELS/ALL_MODELS, карточка
  заполнена: язык, размер, пояснение про пунктуацию);
* AC 3 — движок "GigaAM" идёт по тем же путям, что "Parakeet": preload,
  диктовка, прогрессивная расшифровка файла (и диспетчер в исходнике, и
  фактический вызов методов);
* AC 5 — новый рантайм не нужен: загрузка через onnx_asr.load_model, файлы
  проверяются как у Parakeet (*.onnx), папка модели — ASCII без правки
  _ENGINE_DIRS; нижняя граница onnx-asr закреплена в замке;
* скачивание — из репозитория с четырьмя вариантами (~4,5 ГБ) берутся ТОЛЬКО
  int8-файлы e2e_rnnt и config.json (allow_patterns), размер для прогресса
  считается по тем же файлам, загрузка идёт с quantization="int8";
* AC 8 — прежние локальные модели и модель по умолчанию (whisper:small) не
  затронуты;
* ui.py — единственный хардкод "Parakeet" (недоступность live-режима)
  распространён на GigaAM.

Тест headless: сеть не вызывается, веса не скачиваются (snapshot_download,
HfApi и onnx_asr подменены).
Запуск (в venv приложения):
    .venv\Scripts\python.exe tests\test_us085_gigaam_local_model.py
"""
from __future__ import annotations

import ast
import fnmatch
import inspect
import os
import re
import sys
import tempfile
import types
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="vil-us085-")

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from voice_input_app import models as models_module  # noqa: E402
from voice_input_app.config import AppConfig  # noqa: E402
from voice_input_app.models import (  # noqa: E402
    ALL_MODELS,
    DEFAULT_MODEL_KEY,
    GIGAAM_MODELS,
    PARAKEET_MODELS,
    TRANSCRIPTION_MODELS,
    WHISPER_MODELS,
    ModelManager,
    ModelSpec,
    _missing_required_files,
    model_display_name,
)

KEY = "gigaam:v3-e2e-rnnt"
SPEC = GIGAAM_MODELS[KEY]
MODELS_SRC = (REPO_ROOT / "voice_input_app" / "models.py").read_text(encoding="utf-8")
UI_SRC = (REPO_ROOT / "voice_input_app" / "ui.py").read_text(encoding="utf-8")

# Что лежит в istupakov/gigaam-v3-onnx (имена и порядок величин размеров в байтах).
REPO_FILES = {
    ".gitattributes": 1_590, "LICENSE.txt": 1_070, "README.md": 1_500, "config.json": 135,
    "v3_ctc.onnx": 885_000_000, "v3_ctc.int8.onnx": 225_000_000, "v3_ctc.yaml": 1_010, "v3_vocab.txt": 198,
    "v3_rnnt_encoder.onnx": 885_000_000, "v3_rnnt_encoder.int8.onnx": 225_000_000,
    "v3_rnnt_decoder.onnx": 3_330_000, "v3_rnnt_decoder.int8.onnx": 842_000,
    "v3_rnnt_joint.onnx": 1_440_000, "v3_rnnt_joint.int8.onnx": 367_000, "v3_rnnt.yaml": 1_150,
    "v3_e2e_ctc.onnx": 886_000_000, "v3_e2e_ctc.int8.onnx": 225_000_000, "v3_e2e_ctc.yaml": 899,
    "v3_e2e_ctc_vocab.txt": 2_010,
    "v3_e2e_rnnt_encoder.onnx": 885_000_000, "v3_e2e_rnnt_encoder.int8.onnx": 225_000_000,
    "v3_e2e_rnnt_decoder.onnx": 4_600_000, "v3_e2e_rnnt_decoder.int8.onnx": 1_160_000,
    "v3_e2e_rnnt_joint.onnx": 2_710_000, "v3_e2e_rnnt_joint.int8.onnx": 688_000,
    "v3_e2e_rnnt.yaml": 1_040, "v3_e2e_rnnt_vocab.txt": 13_400,
}


def _allowed(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in SPEC.allow_patterns)


# --- AC 2: модель в каталоге, карточка заполнена ------------------------------

def test_gigaam_registered_next_to_whisper_and_parakeet() -> None:
    assert SPEC.engine == "GigaAM"
    assert SPEC.repo_id == "istupakov/gigaam-v3-onnx"
    assert SPEC.loader_name == "gigaam-v3-e2e-rnnt"
    # Карточка: язык, размер, пояснение — выбор делается ДО скачивания (AC 4).
    assert "русск" in SPEC.language_hint
    assert re.search(r"\d", SPEC.size_hint), "в размере нет числа: %r" % SPEC.size_hint
    assert "пунктуац" in SPEC.note, "карточка обязана говорить, что модель ставит пунктуацию"
    assert "английск" in SPEC.note, "карточка обязана предупреждать про чисто английскую речь"
    # Видна там же, где Whisper и Parakeet.
    assert KEY in TRANSCRIPTION_MODELS and KEY in ALL_MODELS
    assert model_display_name(KEY) == "GigaAM — v3 E2E RNNT"


# --- AC 8: прежние модели и модель по умолчанию не тронуты --------------------

def test_previous_local_models_untouched() -> None:
    assert DEFAULT_MODEL_KEY == "whisper:small"
    assert set(WHISPER_MODELS) == {
        "whisper:tiny", "whisper:base", "whisper:small",
        "whisper:medium", "whisper:large-v3", "whisper:large-v3-turbo",
    }
    assert set(PARAKEET_MODELS) == {"parakeet:v2", "parakeet:v3"}
    assert set(TRANSCRIPTION_MODELS) == set(WHISPER_MODELS) | set(PARAKEET_MODELS) | set(GIGAAM_MODELS)
    # Новые поля ModelSpec необязательны: прежние записи без них корректны.
    for spec in list(WHISPER_MODELS.values()) + list(PARAKEET_MODELS.values()):
        assert spec.allow_patterns == () and spec.quantization is None
    positional = ModelSpec("k", "E", "n", "r", "l", "lang", "size", "note")
    assert positional.allow_patterns == () and positional.quantization is None


# --- AC 5: существующий стек — файлы, папка, доступность ----------------------

def test_required_files_checked_like_parakeet() -> None:
    parakeet = PARAKEET_MODELS["parakeet:v3"]
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)
        assert _missing_required_files(SPEC, path) == _missing_required_files(parakeet, path) != []
        (path / "v3_e2e_rnnt_encoder.int8.onnx").write_bytes(b"onnx" * 64)
        (path / "config.json").write_text('{"version": "v3"}', encoding="utf-8")
        assert _missing_required_files(SPEC, path) == []
        assert _missing_required_files(parakeet, path) == []


def test_model_dir_is_ascii_without_engine_dirs_entry() -> None:
    mm = ModelManager()
    path = mm.model_path(KEY)
    assert path.parent.name == "gigaam"
    assert path.name == "gigaam_v3-e2e-rnnt"
    assert "GigaAM" not in ModelManager._ENGINE_DIRS, "ASCII-имя движка не нуждается в _ENGINE_DIRS"


def test_availability_follows_local_model_rules() -> None:
    mm = ModelManager()
    assert mm.is_transcription_model(KEY)
    assert not mm.is_installed(KEY), "во временном LOCALAPPDATA модели быть не может"
    assert not mm.is_available(KEY), "до скачивания модель нельзя выбрать — как любую локальную кроме whisper:small"
    assert mm.installed_status(KEY) == "Не загружена"
    assert mm.is_available(DEFAULT_MODEL_KEY)


def test_onnx_asr_floor_pinned_in_lock() -> None:
    req = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    match = re.search(r"^onnx-asr\[cpu,hub\]>=(\d+)\.(\d+)", req, re.M)
    assert match, "в requirements.txt нет onnx-asr[cpu,hub]"
    floor = (int(match.group(1)), int(match.group(2)))
    assert floor >= (0, 8), "имя gigaam-v3-e2e-rnnt появилось в onnx-asr 0.8.0"
    lock = (REPO_ROOT / "requirements.lock.txt").read_text(encoding="utf-8")
    pinned = re.search(r"^onnx-asr==(\d+)\.(\d+)\.(\d+)", lock, re.M)
    assert pinned, "onnx-asr не закреплён в requirements.lock.txt"
    assert (int(pinned.group(1)), int(pinned.group(2))) >= floor, "замок закрепляет onnx-asr старее нижней границы"


# --- Скачивание: только int8-файлы e2e_rnnt из репозитория с четырьмя вариантами ---

def test_allow_patterns_pick_only_int8_e2e_rnnt_files() -> None:
    picked = sorted(name for name in REPO_FILES if _allowed(name))
    assert picked == sorted([
        "config.json", "v3_e2e_rnnt.yaml", "v3_e2e_rnnt_decoder.int8.onnx",
        "v3_e2e_rnnt_encoder.int8.onnx", "v3_e2e_rnnt_joint.int8.onnx", "v3_e2e_rnnt_vocab.txt",
    ]), picked
    total = sum(size for name, size in REPO_FILES.items() if _allowed(name))
    assert total < 300_000_000, "скачивается больше 300 МБ — в выборку попали лишние файлы"
    assert sum(REPO_FILES.values()) > 4_000_000_000, "контрольный расчёт: весь репозиторий — гигабайты"
    assert SPEC.quantization == "int8", "int8-файлы бесполезны без quantization='int8' при загрузке"


def test_repo_size_counts_only_allowed_files() -> None:
    class _Sibling:
        def __init__(self, name: str, size: int) -> None:
            self.rfilename, self.size = name, size

    class _Info:
        siblings = [_Sibling(name, size) for name, size in REPO_FILES.items()]

    class _FakeApi:
        def __init__(self, token=None) -> None:
            pass

        def model_info(self, repo_id, files_metadata=False):
            return _Info()

    original = models_module.HfApi
    models_module.HfApi = _FakeApi
    try:
        only = models_module._repo_size_bytes(SPEC.repo_id, allow_patterns=SPEC.allow_patterns)
        whole = models_module._repo_size_bytes(SPEC.repo_id)
    finally:
        models_module.HfApi = original
    assert only == sum(size for name, size in REPO_FILES.items() if _allowed(name))
    assert whole == sum(REPO_FILES.values()), "без allow_patterns считается весь репозиторий, как раньше"


def test_download_requests_only_allowed_files_and_installs() -> None:
    mm = ModelManager()
    captured: dict = {}

    def fake_snapshot_download(**kwargs):
        captured.update(kwargs)
        local_dir = Path(kwargs["local_dir"])
        for name in REPO_FILES:
            if kwargs.get("allow_patterns") is None or any(fnmatch.fnmatch(name, p) for p in kwargs["allow_patterns"]):
                (local_dir / name).write_bytes(b"x" * 64)
        return str(local_dir)

    original_snapshot = models_module.snapshot_download
    original_size = models_module._repo_size_bytes
    models_module.snapshot_download = fake_snapshot_download
    models_module._repo_size_bytes = lambda repo_id, token=None, allow_patterns=(): 1_000
    try:
        final = mm.download(KEY)
    finally:
        models_module.snapshot_download = original_snapshot
        models_module._repo_size_bytes = original_size
    assert captured["repo_id"] == SPEC.repo_id
    assert captured["allow_patterns"] == list(SPEC.allow_patterns)
    downloaded = sorted(p.name for p in final.iterdir())
    assert "v3_e2e_rnnt_encoder.int8.onnx" in downloaded and "config.json" in downloaded
    assert "v3_e2e_rnnt_encoder.onnx" not in downloaded, "скачан fp32-энкодер на 885 МБ"
    assert "v3_ctc.int8.onnx" not in downloaded, "скачан чужой вариант модели"
    assert mm.is_installed(KEY) and mm.is_available(KEY)
    assert mm.installed_status(KEY) == "Загружена"


def test_load_passes_int8_quantization_to_onnx_asr() -> None:
    mm = ModelManager()
    path = mm.model_path(KEY)
    path.mkdir(parents=True, exist_ok=True)
    (path / "v3_e2e_rnnt_encoder.int8.onnx").write_bytes(b"x" * 64)
    (path / "config.json").write_text('{"version": "v3"}', encoding="utf-8")
    calls: list[tuple] = []
    fake = types.ModuleType("onnx_asr")
    fake.load_model = lambda *args, **kwargs: calls.append((args, kwargs)) or object()  # type: ignore[attr-defined]
    saved = sys.modules.get("onnx_asr")
    sys.modules["onnx_asr"] = fake
    try:
        mm._load_parakeet(SPEC)
    finally:
        if saved is None:
            sys.modules.pop("onnx_asr", None)
        else:
            sys.modules["onnx_asr"] = saved
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == SPEC.loader_name and Path(args[1]) == path
    assert kwargs == {"quantization": "int8"}, kwargs


# --- AC 3: диспетчер — GigaAM идёт по путям Parakeet ---------------------------

def _engine_comparisons(src: str) -> tuple[list[tuple[int, str]], list[tuple[int, tuple[str, ...]]]]:
    """(строки с `... .engine == "<x>"`, строки с `... .engine in (<...>)`)."""
    eq: list[tuple[int, str]] = []
    member: list[tuple[int, tuple[str, ...]]] = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        left = node.left
        # `spec.engine == ...` в models.py и `engine = ...; if engine in (...)` в ui.py.
        is_engine = (isinstance(left, ast.Attribute) and left.attr == "engine") or (
            isinstance(left, ast.Name) and left.id == "engine"
        )
        if not is_engine:
            continue
        right = node.comparators[0]
        if isinstance(node.ops[0], ast.Eq) and isinstance(right, ast.Constant):
            eq.append((node.lineno, str(right.value)))
        elif isinstance(node.ops[0], ast.In) and isinstance(right, ast.Tuple):
            member.append((node.lineno, tuple(str(e.value) for e in right.elts if isinstance(e, ast.Constant))))
    return eq, member


def test_models_dispatch_treats_gigaam_as_parakeet() -> None:
    eq, member = _engine_comparisons(MODELS_SRC)
    assert not [line for line, value in eq if value == "Parakeet"], (
        "остались ветки `engine == \"Parakeet\"` без GigaAM: %s" % [line for line, value in eq if value == "Parakeet"])
    pairs = [line for line, values in member if set(values) == {"Parakeet", "GigaAM"}]
    # Четыре точки: проверка файлов, preload, диктовка, прогрессивная расшифровка файла.
    assert len(pairs) >= 4, "GigaAM подключён не во всех диспетчерских точках: %s" % pairs
    for line in pairs:
        window = "\n".join(MODELS_SRC.splitlines()[line - 1:line + 3])
        assert "parakeet" in window.lower() or "PARAKEET_MODEL_SUFFIXES" in window, (
            "ветка на строке %d не переиспользует код Parakeet" % line)


def test_ui_live_mode_notice_covers_gigaam() -> None:
    eq, member = _engine_comparisons(UI_SRC)
    assert not [line for line, value in eq if value == "Parakeet"], "в ui.py остался хардкод engine == \"Parakeet\""
    assert any(set(values) == {"Parakeet", "GigaAM"} for _, values in member), "ui.py не знает, что у GigaAM нет live-режима"
    assert 'setText("Для Parakeet live-режим' not in UI_SRC, "текст уведомления зашит на Parakeet"


def test_manager_actually_calls_parakeet_paths_for_gigaam() -> None:
    """Не только исходник: реальные вызовы уходят в _load_parakeet/_transcribe_parakeet*."""
    mm = ModelManager()
    calls: list[str] = []
    mm.is_available = lambda key: True  # type: ignore[method-assign]
    mm._load_parakeet = lambda spec: calls.append("load:" + spec.key) or object()  # type: ignore[method-assign]
    mm._transcribe_parakeet = lambda spec, wav_path, **kw: calls.append("dictation:" + spec.key) or "текст"  # type: ignore[method-assign]
    mm._transcribe_parakeet_progressive = lambda spec, wav_path, cfg, **kw: calls.append("file:" + spec.key) or "текст"  # type: ignore[method-assign]
    mm._load_whisper = lambda *a, **k: calls.append("WHISPER")  # type: ignore[method-assign]
    mm._transcribe_whisper = lambda *a, **k: calls.append("WHISPER") or ""  # type: ignore[method-assign]
    mm._transcribe_whisper_progressive = lambda *a, **k: calls.append("WHISPER") or ""  # type: ignore[method-assign]
    cfg = AppConfig()
    wav = Path(tempfile.gettempdir()) / "us085-fake.wav"

    mm.preload(KEY, cfg)
    assert mm.transcribe(KEY, wav, cfg) == "текст"

    progressive = getattr(mm, "transcribe_file_progressive")
    params = inspect.signature(progressive).parameters
    kwargs = {}
    if "duration_seconds" in params:
        kwargs["duration_seconds"] = 1.0
    for name in ("progress_callback", "block_callback"):
        if name in params:
            kwargs[name] = None
    if "cancel_check" in params:
        kwargs["cancel_check"] = lambda: False
    assert progressive(KEY, wav, cfg, **kwargs) == "текст"

    assert calls == ["load:" + KEY, "dictation:" + KEY, "file:" + KEY], calls


def _run() -> None:
    tests = [
        test_gigaam_registered_next_to_whisper_and_parakeet,
        test_previous_local_models_untouched,
        test_required_files_checked_like_parakeet,
        test_model_dir_is_ascii_without_engine_dirs_entry,
        test_availability_follows_local_model_rules,
        test_onnx_asr_floor_pinned_in_lock,
        test_allow_patterns_pick_only_int8_e2e_rnnt_files,
        test_repo_size_counts_only_allowed_files,
        test_download_requests_only_allowed_files_and_installs,
        test_load_passes_int8_quantization_to_onnx_asr,
        test_models_dispatch_treats_gigaam_as_parakeet,
        test_ui_live_mode_notice_covers_gigaam,
        test_manager_actually_calls_parakeet_paths_for_gigaam,
    ]
    for test in tests:
        test()
        print("PASS: %s" % test.__name__)
    print("US-085 regression: ALL PASS")


if __name__ == "__main__":
    _run()
