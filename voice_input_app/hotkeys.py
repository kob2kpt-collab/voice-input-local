from __future__ import annotations

from collections.abc import Callable


def normalize_hotkey(display: str) -> str:
    text = display.strip().lower()
    text = text.replace("control", "ctrl")
    text = text.replace(" ", "")
    return text


class HotkeyService:
    def __init__(self, callback: Callable[[], None]) -> None:
        self.callback = callback
        self._handle = None
        self._hotkey = ""

    @property
    def hotkey(self) -> str:
        return self._hotkey

    def start(self, hotkey: str) -> None:
        new_hotkey = normalize_hotkey(hotkey)
        if not new_hotkey:
            raise RuntimeError("Горячая клавиша не задана. Нажмите поле и выберите новую комбинацию.")
        try:
            import keyboard

            # Register the new shortcut before removing the old one. If parsing
            # fails for a layout-specific key, the previous working shortcut
            # remains active and the UI can ask the user to choose another combo.
            new_handle = keyboard.add_hotkey(new_hotkey, self.callback, suppress=False, trigger_on_release=False)
        except Exception as exc:
            raise RuntimeError(
                "Не удалось зарегистрировать глобальную горячую клавишу. "
                "Нажмите подсвеченное поле и выберите другую комбинацию, например Ctrl+Alt+Space. "
                f"Детали: {exc}"
            ) from exc

        old_handle = self._handle
        self._handle = new_handle
        self._hotkey = new_hotkey
        if old_handle is not None:
            try:
                import keyboard

                keyboard.remove_hotkey(old_handle)
            except Exception:
                pass

    def stop(self) -> None:
        if self._handle is None:
            return
        try:
            import keyboard

            keyboard.remove_hotkey(self._handle)
        except Exception:
            pass
        self._handle = None
