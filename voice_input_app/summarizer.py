"""Local summarization engine using llama-cpp-python (CPU-friendly).

SUM-04: Supports GGUF models from the Models tab catalog.
The user downloads a summary model (e.g. Qwen3 1.7B Q4_K_M) via the Models tab,
then selects it for summarization.  All processing is local.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from .logger import get_logger

log = get_logger("summarizer")

DEFAULT_SUMMARY_PROMPT = (
    "Ты — помощник для составления кратких итогов деловых разговоров. "
    "Проанализируй расшифровку и составь краткое резюме на том же языке, что и расшифровка. "
    "Включи:\n"
    "1. Основные темы обсуждения\n"
    "2. Ключевые решения и договорённости\n"
    "3. Задачи и ответственные (если упомянуты)\n"
    "4. Важные цифры и даты (если упомянуты)\n\n"
    "Будь краток и конкретен. Не добавляй информацию, которой нет в расшифровке."
)

_llm_instance = None
_llm_model_path: str | None = None
_llm_lock = threading.Lock()


def _get_llm(model_path: Path):
    """Lazy-load the LLM instance.  Reloads if model_path changed."""
    global _llm_instance, _llm_model_path

    path_str = str(model_path)
    if _llm_instance is not None and _llm_model_path == path_str:
        return _llm_instance

    with _llm_lock:
        if _llm_instance is not None and _llm_model_path == path_str:
            return _llm_instance

        if not model_path.is_file():
            raise RuntimeError(
                "Модель суммаризации не найдена. Загрузите её через "
                "вкладку «Модели»."
            )

        try:
            from llama_cpp import Llama
        except ImportError:
            raise RuntimeError(
                "Библиотека llama-cpp-python не установлена.\n\n"
                "На Windows без C++ компилятора используйте готовый wheel:\n"
                "pip install llama-cpp-python "
                "--extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu\n\n"
                "Или установите Visual Studio Build Tools и выполните:\n"
                "pip install llama-cpp-python"
            )

        log.info("Loading summarization LLM from %s", model_path)
        _llm_instance = Llama(
            model_path=path_str,
            n_ctx=4096,
            n_threads=4,
            n_gpu_layers=0,  # CPU only
            verbose=False,
        )
        _llm_model_path = path_str
        log.info("Summarization LLM loaded successfully")
        return _llm_instance


def unload_llm() -> None:
    """Release LLM from memory."""
    global _llm_instance, _llm_model_path
    with _llm_lock:
        _llm_instance = None
        _llm_model_path = None


def summarize(
    text: str,
    *,
    model_path: Path,
    system_prompt: str = "",
    max_tokens: int = 1024,
) -> str:
    """Generate a summary of the given transcript text.

    Args:
        text: The transcript text to summarize.
        model_path: Full path to the GGUF model file.
        system_prompt: Custom system prompt. Uses DEFAULT_SUMMARY_PROMPT if empty.
        max_tokens: Maximum tokens in the summary response.

    Returns:
        The generated summary text.
    """
    if not text.strip():
        return ""

    prompt = system_prompt.strip() if system_prompt.strip() else DEFAULT_SUMMARY_PROMPT

    # Truncate input if too long (keep ~3000 tokens worth of text)
    max_chars = 12000
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n[...текст обрезан из-за ограничений модели...]"

    llm = _get_llm(model_path)

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": f"Расшифровка:\n\n{text}\n\n/no_think"},
    ]

    log.info("Generating summary: input_chars=%d max_tokens=%d model=%s", len(text), max_tokens, model_path.name)
    response = llm.create_chat_completion(
        messages=messages,
        max_tokens=max_tokens,
        temperature=0.3,
        top_p=0.9,
    )

    result = ""
    if response and "choices" in response and response["choices"]:
        choice = response["choices"][0]
        if "message" in choice and "content" in choice["message"]:
            result = choice["message"]["content"].strip()

    log.info("Summary generated: output_chars=%d", len(result))
    return result
