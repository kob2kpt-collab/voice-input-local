from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import policy
from .logger import get_logger

log = get_logger("updater")

# --- Машинная политика централизованного отключения обновлений (US-047) ---
# Администратор выставляет её через реестр (GPO / Kaspersky Security Center),
# чтобы встроенный апдейтер не беспокоил пользователей, когда обновлениями
# управляют централизованно. Ветка Policies недоступна обычному пользователю
# на запись, поэтому политику нельзя обойти из-под учётки пользователя.
#
# US-088: САМО ЧТЕНИЕ реестра переехало в voice_input_app/policy.py — там
# теперь единственная точка чтения ветки политик (к DisableUpdates добавились
# политики по облаку). Имена ниже оставлены как есть: на них ссылаются
# документация для ИТ и регресс-тест US-047.
POLICY_KEY = policy.POLICY_KEY
POLICY_DISABLE_UPDATES_VALUE = policy.VALUE_DISABLE_UPDATES


def updates_disabled_by_policy() -> bool:
    """Возвращает True, если встроенные обновления отключены машинной политикой.

    Читает HKLM\\SOFTWARE\\Policies\\VoiceInputLocal, значение
    DisableUpdates (DWORD). Ненулевое значение = обновления централизованно
    отключены. Политика имеет ПРИОРИТЕТ над пользовательской настройкой
    cfg.updates_enabled: при её включении встроенная проверка и скачивание
    обновлений не выполняются (обновлениями управляет ИТ, например через
    Kaspersky Security Center / GPO).

    На не-Windows, при отсутствии ключа/значения или любой ошибке чтения
    возвращает False — fail-open к обычному поведению приложения, чтобы
    сбой чтения реестра не «залипал» на отключённых обновлениях.
    """
    return policy.updates_disabled()


@dataclass(frozen=True)
class UpdateInfo:
    current_version: str
    latest_version: str
    release_url: str
    release_notes: str
    asset_name: str
    asset_url: str
    asset_size: int
    is_installer: bool


KIND_GITHUB = "github"
KIND_GITLAB = "gitlab"

# Хосты, которые распознаются как GitLab без явного указания. Внутренний GitLab
# компании (gitlab.corp.teleofis.ru) попадает сюда по подстроке "gitlab" в имени
# хоста. Если внутренний сервис назван иначе (например git.example.com), ссылку
# можно пометить вручную префиксом "gitlab:" — см. parse_repo.
_GITHUB_HOSTS = {"github.com", "www.github.com"}


@dataclass(frozen=True)
class RepoRef:
    """Разобранная ссылка на репозиторий выпусков.

    kind — "github" | "gitlab" | "" (не распознано);
    host — хост сервиса (для GitHub всегда github.com);
    path — путь проекта: "owner/repo" у GitHub, "group/subgroup/project" у
           GitLab (вложенные группы там обычное дело).
    """

    kind: str
    host: str
    path: str

    @property
    def ok(self) -> bool:
        return bool(self.kind and self.host and self.path)

    @property
    def web_url(self) -> str:
        return f"https://{self.host}/{self.path}"


def parse_repo(value: str) -> RepoRef:
    """Разобрать значение настройки «ссылка на репозиторий».

    Понимает четыре формы:

    * "owner/repo"                       — GitHub (форма, сохранённая старыми
                                            версиями; AC 5 — не теряется);
    * "https://github.com/owner/repo"    — GitHub;
    * "https://gitlab.corp.example.ru/group/project" — GitLab по имени хоста;
    * "gitlab:https://git.example.ru/g/p" — явное указание сервиса, если хост
                                            называется не по шаблону.

    Нераспознанное значение возвращается с пустым kind (AC 6) — вызывающий код
    показывает понятное сообщение и не трогает сохранённую настройку.
    """
    text = (value or "").strip()
    if not text:
        return RepoRef("", "", "")

    forced = ""
    for prefix in (KIND_GITHUB, KIND_GITLAB):
        if text.lower().startswith(prefix + ":") and not text.lower().startswith(prefix + "://"):
            forced = prefix
            text = text[len(prefix) + 1:].strip()
            break

    host = ""
    path = text
    if "://" in text:
        scheme, rest = text.split("://", 1)
        if scheme.lower() not in ("http", "https"):
            return RepoRef("", "", "")
        host, _, path = rest.partition("/")
    elif text.lower().startswith("www.") or (
        "/" in text and "." in text.split("/", 1)[0]
    ):
        # Ссылка без схемы: "gitlab.example.ru/group/project".
        host, _, path = text.partition("/")

    host = host.strip().lower().rstrip("/")
    # Служебные хвосты ссылок на страницу выпусков и .git — не часть пути проекта.
    path = path.strip().strip("/")
    for tail in ("/-/releases", "/-/tags", "/releases/latest", "/releases", "/tags"):
        if path.lower().endswith(tail):
            path = path[: -len(tail)]
            break
    path = path.removesuffix(".git").strip("/")
    if not path or "/" not in path:
        return RepoRef("", "", "")

    if forced == KIND_GITHUB or (not forced and (not host or host in _GITHUB_HOSTS)):
        # Без хоста — это старая форма owner/repo, она всегда означала GitHub.
        parts = [p for p in path.split("/") if p]
        if len(parts) < 2:
            return RepoRef("", "", "")
        return RepoRef(KIND_GITHUB, "github.com", f"{parts[0]}/{parts[1]}")
    if forced == KIND_GITLAB or KIND_GITLAB in host:
        return RepoRef(KIND_GITLAB, host, path)
    return RepoRef("", host, path)


def normalize_repo(value: str) -> str:
    """Привести настройку к каноническому виду для хранения.

    GitHub сохраняется прежней короткой формой "owner/repo" — так настройка,
    записанная старой версией, не меняется при первом же сохранении. GitLab
    сохраняется полной ссылкой: без хоста его не найти.

    Нераспознанное значение возвращается пустым — вызывающий код (ui.save_settings)
    по этому признаку оставляет прежнюю настройку и показывает сообщение (AC 6).
    """
    ref = parse_repo(value)
    if not ref.ok:
        return ""
    if ref.kind == KIND_GITHUB:
        return ref.path
    return ref.web_url


def repo_hint(value: str) -> str:
    """Понятное объяснение, почему ссылка не подошла. "" — всё в порядке."""
    if not (value or "").strip():
        return ("Укажите ссылку на репозиторий с выпусками — например "
                "https://github.com/your-org/voice-input-local или "
                "https://gitlab.corp.teleofis.ru/group/voice-input-local.")
    if parse_repo(value).ok:
        return ""
    return (f"Ссылка «{value.strip()}» не распознана. Ожидается ссылка на репозиторий "
            "GitHub или GitLab, например https://github.com/your-org/voice-input-local "
            "или https://gitlab.corp.teleofis.ru/group/voice-input-local. Если внутренний "
            "сервис называется иначе, добавьте в начало «gitlab:».")


def _version_tuple(value: str) -> tuple[int, ...]:
    value = (value or "").strip().lower().lstrip("v")
    # Keep numeric parts only. 4.2.0-beta -> (4, 2, 0)
    parts = re.findall(r"\d+", value)
    if not parts:
        return (0,)
    return tuple(int(p) for p in parts[:4])


def is_newer(latest: str, current: str) -> bool:
    a = list(_version_tuple(latest))
    b = list(_version_tuple(current))
    n = max(len(a), len(b))
    a.extend([0] * (n - len(a)))
    b.extend([0] * (n - len(b)))
    return tuple(a) > tuple(b)


def _request_json(url: str, *, kind: str = KIND_GITHUB):
    """Запрос к API сервиса выпусков. Возвращает разобранный JSON.

    US-078 (AC 8): репозитории считаются открытыми, токен для GitLab не
    хранится и не отправляется. Для GitHub переменная окружения GITHUB_TOKEN
    по-прежнему подхватывается — она нужна разработчику, чтобы не упираться в
    лимит запросов, и у пользователей не задана.
    """
    headers = {"User-Agent": "VoiceInputLocal-Updater"}
    if kind == KIND_GITHUB:
        headers["Accept"] = "application/vnd.github+json"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
        if token:
            headers["Authorization"] = f"Bearer {token}"
    else:
        headers["Accept"] = "application/json"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 - настроенный пользователем адрес репозитория
        raw = resp.read().decode("utf-8")
    return json.loads(raw)


def _choose_asset(assets: list[dict]) -> dict | None:
    if not assets:
        return None
    candidates = [a for a in assets if a.get("browser_download_url")]
    if not candidates:
        return None

    def score(asset: dict) -> tuple[int, int]:
        name = str(asset.get("name") or "").lower()
        installer = name.endswith(".exe") and any(x in name for x in ["setup", "installer", "install"])
        portable = name.endswith(".zip") and any(x in name for x in ["portable", "voiceinputlocal", "voice-input-local"])
        generic_exe = name.endswith(".exe")
        generic_zip = name.endswith(".zip")
        if installer:
            return (4, int(asset.get("size") or 0))
        if generic_exe:
            return (3, int(asset.get("size") or 0))
        if portable:
            return (2, int(asset.get("size") or 0))
        if generic_zip:
            return (1, int(asset.get("size") or 0))
        return (0, int(asset.get("size") or 0))

    candidates.sort(key=score, reverse=True)
    best = candidates[0]
    return best if score(best)[0] > 0 else None


def gitlab_assets(release: dict) -> list[dict]:
    """Файлы выпуска GitLab в том же виде, что и у GitHub.

    US-078 (AC 3): установщик ищется по ссылкам выпуска (assets.links) —
    именно так публикуются наши сборки, в том числе через реестр пакетов.
    Архивы исходников (assets.sources) сюда НЕ попадают: это не установщик, и
    отдать их пользователю под видом обновления нельзя.

    GitLab не сообщает размер файла по ссылке, поэтому size = 0 — прогресс
    скачивания в этом случае считается по заголовку Content-Length ответа.
    """
    links = ((release.get("assets") or {}).get("links")) or []
    out: list[dict] = []
    for link in links:
        if not isinstance(link, dict):
            continue
        # direct_asset_url ведёт к файлу напрямую (без страницы-редиректа).
        url = str(link.get("direct_asset_url") or link.get("url") or "")
        if not url:
            continue
        name = str(link.get("name") or "").strip() or url.rstrip("/").rsplit("/", 1)[-1]
        out.append({"name": name, "browser_download_url": url, "size": 0})
    return out


def _release_to_info(ref: RepoRef, release: dict, current_version: str, assets: list[dict]) -> "UpdateInfo | None":
    """Общая для обоих сервисов сборка UpdateInfo (AC 4: ведут себя одинаково)."""
    tag = str(release.get("tag_name") or "").strip()
    latest_version = tag.lstrip("v")
    if not latest_version or not is_newer(latest_version, current_version):
        return None
    asset = _choose_asset(assets)
    if asset is None:
        raise RuntimeError(
            "В последнем выпуске нет подходящего файла .exe или .zip для скачивания."
        )
    name = str(asset.get("name") or "update")
    if ref.kind == KIND_GITHUB:
        release_url = str(release.get("html_url") or "") or f"{ref.web_url}/releases/latest"
        notes = str(release.get("body") or "")
    else:
        release_url = str(((release.get("_links") or {}).get("self")) or "") or f"{ref.web_url}/-/releases"
        notes = str(release.get("description") or "")
    return UpdateInfo(
        current_version=current_version,
        latest_version=latest_version,
        release_url=release_url,
        release_notes=notes,
        asset_name=name,
        asset_url=str(asset.get("browser_download_url") or ""),
        asset_size=int(asset.get("size") or 0),
        is_installer=name.lower().endswith(".exe"),
    )


def _latest_release(ref: RepoRef) -> dict:
    """Последний выпуск репозитория. Ошибки приводятся к понятному тексту."""
    if ref.kind == KIND_GITHUB:
        api_url = f"https://api.github.com/repos/{ref.path}/releases/latest"
    else:
        # GitLab адресует проект путём с экранированным слэшем. Список выпусков
        # приходит отсортированным по дате, самый свежий — первый.
        project = urllib.parse.quote(ref.path, safe="")
        api_url = f"https://{ref.host}/api/v4/projects/{project}/releases"
    service = "GitHub" if ref.kind == KIND_GITHUB else "GitLab"
    try:
        data = _request_json(api_url, kind=ref.kind)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise RuntimeError(
                f"Выпуски не найдены или репозиторий недоступен: {ref.web_url}"
            ) from exc
        if exc.code in (401, 403):
            raise RuntimeError(
                f"{service} не дал доступ к выпускам ({exc.code}). Репозиторий должен быть открыт на чтение."
            ) from exc
        raise RuntimeError(f"{service} вернул ошибку {exc.code}: {exc.reason}") from exc
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Не удалось проверить обновления: {exc}") from exc

    if isinstance(data, list):
        if not data:
            raise RuntimeError(f"В репозитории {ref.web_url} ещё нет выпусков.")
        data = data[0]
    if not isinstance(data, dict):
        raise RuntimeError(f"{service} вернул неожиданный ответ на запрос выпусков.")
    return data


def check_for_update(repo: str, current_version: str) -> UpdateInfo | None:
    """Проверить наличие нового выпуска в GitHub или GitLab (US-078).

    Сервис выбирается по домену ссылки, дальше всё общее: сравнение версий,
    выбор файла установщика, скачивание и запуск (AC 4).
    """
    ref = parse_repo(repo)
    if not ref.ok:
        if (repo or "").strip():
            # AC 6: непонятная ссылка объясняется, а не молча гасит проверку.
            raise RuntimeError(repo_hint(repo))
        return None
    log.info("Проверка обновлений: сервис=%s репозиторий=%s", ref.kind, ref.web_url)
    release = _latest_release(ref)
    assets = (
        list(release.get("assets") or [])
        if ref.kind == KIND_GITHUB
        else gitlab_assets(release)
    )
    return _release_to_info(ref, release, current_version, assets)


def download_update(info: UpdateInfo, progress: Callable[[int, int], None] | None = None) -> Path:
    suffix = Path(info.asset_name).suffix or (".exe" if info.is_installer else ".zip")
    out_dir = Path(tempfile.gettempdir()) / "VoiceInputLocal" / "updates"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", info.asset_name) or f"VoiceInputLocalUpdate{suffix}"
    out_path = out_dir / safe_name
    headers = {"User-Agent": "VoiceInputLocal-Updater"}
    req = urllib.request.Request(info.asset_url, headers=headers)
    downloaded = 0
    with urllib.request.urlopen(req, timeout=60) as resp, out_path.open("wb") as fh:  # noqa: S310 - адрес файла выпуска
        total = int(resp.headers.get("Content-Length") or info.asset_size or 0)
        while True:
            chunk = resp.read(1024 * 512)
            if not chunk:
                break
            fh.write(chunk)
            downloaded += len(chunk)
            if progress:
                progress(downloaded, total)
    if progress:
        progress(downloaded, downloaded if downloaded and not info.asset_size else (info.asset_size or downloaded))
    return out_path


def launch_update_file(path: Path) -> None:
    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    if os.name == "nt":
        # ShellExecute avoids keeping the update child attached to this GUI process.
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])
