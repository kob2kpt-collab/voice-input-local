from __future__ import annotations

from collections.abc import Callable


def normalize_hotkey(display: str) -> str:
    text = display.strip().lower()
    text = text.replace("control", "ctrl")
    text = text.replace(" ", "")
    return text


class HotkeyService:
    """Глобальная горячая клавиша с двумя режимами (US-026).

    - mode="toggle" (по умолчанию): одно срабатывание на нажатие комбинации
      вызывает on_trigger (старт/стоп по очереди — логика на стороне UI).
    - mode="ptt" (Push-to-Talk): удержание комбинации = запись, отпускание =
      стоп и расшифровка.

    Реализация PTT (фикс): низкоуровневый глобальный хук keyboard.hook + проверка
    keyboard.is_pressed(combo) на каждом событии. Два add_hotkey на одну
    комбинацию (обычный + trigger_on_release=True) в библиотеке keyboard для
    составных комбинаций (например Ctrl+Space) ненадёжны: нажатие ловится, а
    отпускание — нет. Хук отслеживает переходы «не нажато↔нажато» сам.

    Безопасность перерегистрации (см. CLAUDE.md): новые хэндлы регистрируются
    ДО снятия старых. Если регистрация новой комбинации провалилась — старые
    хэндлы остаются рабочими, UI может попросить выбрать другую комбинацию.
    """

    def __init__(
        self,
        on_trigger: Callable[[], None],
        on_press: Callable[[], None] | None = None,
        on_release: Callable[[], None] | None = None,
    ) -> None:
        self.on_trigger = on_trigger
        self.on_press = on_press if on_press is not None else on_trigger
        self.on_release = on_release if on_release is not None else (lambda: None)
        # Совместимость: старый код мог обращаться к self.callback.
        self.callback = on_trigger
        # Хэндлы как список кортежей (kind, handle): kind in {"hotkey", "hook"}.
        self._handles: list = []
        self._hotkey = ""
        self._mode = "toggle"
        # Состояние PTT-хука.
        self._ptt_combo = ""
        self._ptt_down = False

    @property
    def hotkey(self) -> str:
        return self._hotkey

    @property
    def mode(self) -> str:
        return self._mode

    def start(self, hotkey: str, mode: str = "toggle") -> None:
        new_hotkey = normalize_hotkey(hotkey)
        if not new_hotkey:
            raise RuntimeError("Горячая клавиша не задана. Нажмите поле и выберите новую комбинацию.")
        mode = "ptt" if str(mode).lower() == "ptt" else "toggle"
        new_handles: list = []
        try:
            import keyboard

            if mode == "ptt":
                # Сбрасываем состояние и ставим глобальный хук на все события.
                self._ptt_combo = new_hotkey
                self._ptt_down = False
                handle = keyboard.hook(self._ptt_on_event)
                new_handles.append(("hook", handle))
            else:
                handle = keyboard.add_hotkey(new_hotkey, self.on_trigger, suppress=False, trigger_on_release=False)
                new_handles.append(("hotkey", handle))
        except Exception as exc:
            # Откатываем частично зарегистрированные хэндлы этого вызова.
            self._remove_handles(new_handles)
            raise RuntimeError(
                "Не удалось зарегистрировать глобальную горячую клавишу. "
                "Нажмите подсвеченное поле и выберите другую комбинацию, например Ctrl+Alt+Space. "
                f"Детали: {exc}"
            ) from exc

        old_handles = self._handles
        self._handles = new_handles
        self._hotkey = new_hotkey
        self._mode = mode
        self._remove_handles(old_handles)

    def _ptt_on_event(self, event=None) -> None:  # noqa: ANN001
        # Вызывается из потока слушателя keyboard на каждое событие клавиатуры.
        # is_pressed отражает состояние ПОСЛЕ обработки текущего события, поэтому
        # отпускание любого участника комбинации даёт pressed=False.
        try:
            import keyboard

            pressed = bool(keyboard.is_pressed(self._ptt_combo))
        except Exception:
            return
        if pressed and not self._ptt_down:
            self._ptt_down = True
            try:
                self.on_press()
            except Exception:
                pass
        elif (not pressed) and self._ptt_down:
            self._ptt_down = False
            try:
                self.on_release()
            except Exception:
                pass

    def _remove_handles(self, handles: list) -> None:
        if not handles:
            return
        try:
            import keyboard
        except Exception:
            return
        for kind, handle in handles:
            try:
                if kind == "hook":
                    keyboard.unhook(handle)
                else:
                    keyboard.remove_hotkey(handle)
            except Exception:
                pass

    def stop(self) -> None:
        if not self._handles:
            return
        self._remove_handles(self._handles)
        self._handles = []
        self._ptt_down = False
