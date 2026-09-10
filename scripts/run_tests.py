# -*- coding: utf-8 -*-
"""Прогнать все тесты проекта одной командой (US-089, AC 4).

В проекте нет pytest: тесты — самостоятельные скрипты, каждый со своим _run().
Так сложилось намеренно (не нужна лишняя зависимость в окружении сборки), но
для релизного сценария нужен один вход, который вернёт ненулевой код, если
хоть один файл упал, — иначе выпуск опубликуется поверх сломанных проверок.

Запуск:

    .venv\\Scripts\\python.exe scripts\\run_tests.py

Код возврата: 0 — всё прошло, 1 — есть падения.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO_ROOT / "tests"
def _force_utf8_output() -> None:
    """Печатать по-русски можно на любой консоли.

    Windows отдаёт скрипту кодировку консоли (cp1252/cp866), и обычный print с
    кириллицей роняет процесс UnicodeEncodeError. Из-за этого сборка 4.22.0
    упала на шаге тестов: все проверки прошли, а запускальщик умер на итоговой
    строке — и выпуск не опубликовался. Понижаем непечатаемые символы вместо
    падения; на UTF-8-консоли ничего не меняется.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


_force_utf8_output()


def main() -> int:
    files = sorted(TESTS_DIR.glob("test_*.py"))
    if not files:
        print("Тестов не найдено в %s" % TESTS_DIR)
        return 1

    failed: list[tuple[str, str]] = []
    started = time.time()
    for path in files:
        name = path.name
        run = subprocess.run(
            [sys.executable, str(path)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        ok = run.returncode == 0
        print("%-46s %s" % (name, "OK" if ok else "ПАДЕНИЕ"), flush=True)
        if not ok:
            failed.append((name, (run.stdout or "") + (run.stderr or "")))

    print("\nФайлов: %d, упало: %d, за %.1f с" % (len(files), len(failed), time.time() - started))
    for name, output in failed:
        print("\n" + "=" * 70)
        print("ПАДЕНИЕ: %s" % name)
        print("=" * 70)
        print(output.strip()[-4000:])
    _report_to_ci(failed, len(files))
    return 1 if failed else 0


def _report_to_ci(failed: list[tuple[str, str]], total: int) -> None:
    """Продублировать причину падения туда, где её видно без доступа к журналу.

    Журнал сборки в GitHub Actions открывается только после входа в аккаунт, и
    у выпуска 4.22.1 гейт заблокировал публикацию с единственным словом
    «Process completed with exit code 1» — понять, какой тест упал, было
    невозможно ни по странице сборки, ни по API. Гейт, который останавливает
    выпуск, обязан называть причину.

    Пишем в два места, доступных без авторизации: аннотации задания
    (workflow-команда ::error, её отдаёт API проверок) и сводку задания
    ($GITHUB_STEP_SUMMARY, она видна на странице запуска).
    """
    import os

    if not os.environ.get("GITHUB_ACTIONS"):
        return

    for name, output in failed:
        tail = (output or "").strip()[-800:]
        # В аннотации переводы строк экранируются, иначе сообщение обрежется.
        escaped = tail.replace("%", "%25").replace("\r", "").replace("\n", "%0A")
        print("::error title=Упал тест %s::%s" % (name, escaped), flush=True)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    lines = ["## Тесты: файлов %d, упало %d\n" % (total, len(failed))]
    for name, output in failed:
        lines.append("### %s\n" % name)
        lines.append("```\n%s\n```\n" % (output or "").strip()[-3000:])
    try:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
    except OSError:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
