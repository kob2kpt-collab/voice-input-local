from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .logger import get_logger

log = get_logger("updater")

# --- Машинная политика централизованного отключения обновлений (US-047) ---
# Администратор выставляет её через реестр (GPO / Kaspersky Security Center),
# чтобы встроенный апдейтер не беспокоил пользователей, когда обновлениями
# управляют централизованно. Ветка Policies недоступна обычному пользователю
# на запись, поэтому политику нельзя обойти из-под учётки пользователя.
POLICY_KEY = r"SOFTWARE\Policies\VoiceInputLocal"
POLICY_DISABLE_UPDATES_VALUE = "DisableUpdates"


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
    if os.name != "nt":
        return False
    try:
        import winreg

        access = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, POLICY_KEY, 0, access) as key:
            value, _typ = winreg.QueryValueEx(key, POLICY_DISABLE_UPDATES_VALUE)
    except FileNotFoundError:
        return False
    except OSError:
        log.debug("Чтение политики обновлений не удалось", exc_info=True)
        return False
    try:
        return int(value) != 0
    except (TypeError, ValueError):
        return False


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


def normalize_repo(value: str) -> str:
    value = (value or "").strip()
    value = value.removeprefix("https://github.com/").removeprefix("http://github.com/")
    value = value.removesuffix(".git").strip("/")
    if not value or "/" not in value:
        return ""
    parts = [p for p in value.split("/") if p]
    if len(parts) < 2:
        return ""
    return f"{parts[0]}/{parts[1]}"


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


def _request_json(url: str) -> dict:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "VoiceInputLocal-Updater",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 - user-configured public GitHub URL
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


def check_for_update(repo: str, current_version: str) -> UpdateInfo | None:
    repo = normalize_repo(repo)
    if not repo:
        return None
    api_url = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        release = _request_json(api_url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise RuntimeError("Релизы GitHub не найдены или репозиторий недоступен.") from exc
        raise RuntimeError(f"GitHub вернул ошибку {exc.code}: {exc.reason}") from exc
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Не удалось проверить обновления: {exc}") from exc

    tag = str(release.get("tag_name") or "").strip()
    latest_version = tag.lstrip("v")
    if not latest_version or not is_newer(latest_version, current_version):
        return None
    asset = _choose_asset(list(release.get("assets") or []))
    if asset is None:
        raise RuntimeError("В последнем GitHub Release нет подходящего файла .exe или .zip для скачивания.")
    name = str(asset.get("name") or "update")
    url = str(asset.get("browser_download_url") or "")
    return UpdateInfo(
        current_version=current_version,
        latest_version=latest_version,
        release_url=str(release.get("html_url") or f"https://github.com/{repo}/releases/latest"),
        release_notes=str(release.get("body") or ""),
        asset_name=name,
        asset_url=url,
        asset_size=int(asset.get("size") or 0),
        is_installer=name.lower().endswith(".exe"),
    )


def download_update(info: UpdateInfo, progress: Callable[[int, int], None] | None = None) -> Path:
    suffix = Path(info.asset_name).suffix or (".exe" if info.is_installer else ".zip")
    out_dir = Path(tempfile.gettempdir()) / "VoiceInputLocal" / "updates"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", info.asset_name) or f"VoiceInputLocalUpdate{suffix}"
    out_path = out_dir / safe_name
    headers = {"User-Agent": "VoiceInputLocal-Updater"}
    req = urllib.request.Request(info.asset_url, headers=headers)
    downloaded = 0
    with urllib.request.urlopen(req, timeout=60) as resp, out_path.open("wb") as fh:  # noqa: S310 - GitHub release asset URL
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
