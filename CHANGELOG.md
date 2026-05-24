# Changelog

## v4.4.0

### Облачные модели для расшифровки файлов (US-017)

- **TASK-051**: file_model_combo на вкладке «Файлы» получил группы «── Локальные ──» / «── Облачные ──». Cloud-модели без API-ключа отображаются серым с пометкой «(не настроено)». Выбор файловой модели сохраняется отдельно в config.file_selected_model.
- **TASK-052**: FileTranscribeWorker.run() получил ветку cloud — для cloud-ключей вызывается ModelManager.transcribe_with_fallback с автонарезкой через cloud_stt.split_and_transcribe.
- **TASK-053**: при превышении лимита размера файла провайдером (OpenAI 25 МБ, ElevenLabs 1024 МБ) показывается диалог с 3 кнопками: «Расшифровать через облако с автонарезкой», «Переключусь на локальную модель» (с подсветкой file_model_combo на 1.5 сек), «Отмена».
- **TASK-054**: pre-flight is_internet_available() перед стартом cloud-расшифровки файла. При недоступности — авто-fallback на cloud_fallback_model_key.
- **TASK-055**: сигнал fallback_applied(fallback_key, reason) в FileTranscribeWorker, подключённый к общему обработчику on_cloud_fallback_applied. Обновляет combo через refresh_available_models_combo(force_current=True).
- **TASK-056**: история сохраняет cloud-расшифровки. Pretty-fallback для имён моделей: если cloud-ключ удалён из настроек, отображается «OpenAI · whisper-1» вместо сырого «cloud:openai:whisper-1».
- **TASK-078**: прогрессивный прогресс при cloud-расшифровке файла. split_and_transcribe принимает on_chunk_done колбэк. FileTranscribeWorker эмитит block_ready по мере готовности каждого чанка — пользователь видит куски текста и прогресс «{percent}% · cloud · чанк {done}/{total}».
- **TASK-079**: отзывчивая отмена cloud-расшифровки. split_and_transcribe принимает cancel_check. При отмене pool.shutdown(cancel_futures=True), текущие in-flight дорабатываются, raise InterruptedError. Время отмены ≤30 секунд.
- **TASK-082**: READ_TIMEOUT cloud-чанков уменьшен со 120 до 30 секунд.
- **TASK-085**: per-chunk local fallback при ошибке cloud-чанка файла. При CloudRateLimit (429) — 1 retry с backoff 3 сек, далее перерасшифровка ТОЛЬКО упавшего чанка локально через cloud_fallback_model_key. Остальные cloud-чанки продолжают идти. Файл устойчив к rate limit, прогресс не теряется, таймкоды сохраняются. Диктовку не трогаем — там действует full fallback.

### Параллельная работа диктовки и расшифровки файла (US-019)

- **TASK-062..065**: матрица блокировок. Снята взаимоисключающая блокировка диктовка ↔ расшифровка файла для cloud-комбинаций (лок+cloud, cloud+лок, cloud+cloud — параллельно разрешено). Жёсткий запрет только при лок+лок. Геттеры is_dictation_busy(), is_file_busy(), dictation_uses_local(), file_uses_local() и предикаты _can_start_dictation(), _can_start_file_transcribe() — единый источник правды для матрицы.
- **TASK-066..068**: одновременный прогресс через изолированные виджеты (overlay + панель Файлы), изоляция отмены (Esc → только диктовка), защита от перемешивания результатов.
- **TASK-080**: on_file_transcription_progress не перезаписывает overlay, если идёт диктовка или активен result_preview. Это позволяет overlay показывать развёрнутый блок с текстом расшифровки диктовки и кнопкой «Скопировать» поверх параллельной cloud-расшифровки файла.
- **TASK-081, 084**: defensive hotkey re-register в 3 точках — в on_file_transcription_cancelled, on_file_transcription_done и сразу при клике cancel_file_transcription. Помогает если keyboard listener потерял Win32-хук во время длительной cloud-операции.
- **TASK-083**: расширенное логирование в toggle_recording / start_recording для диагностики блокировок hotkey.

### Известные ограничения v4.4.0

- Не реализовано в этой итерации (следующий релиз): timestamps + diarization для cloud-моделей (TASK-057..061 US-017), overlay model picker при попытке диктовки во время локальной расшифровки файла (TASK-069..074 US-019).
- Cloud-чанки не прерываются мгновенно (Python ThreadPoolExecutor не умеет прерывать запущенные потоки). Максимальное время «висения» отмены = READ_TIMEOUT = 30с.

## Lessons learned

### 2026-05-24 — повреждение файлов при последовательных Edit-операциях

При работе над US-017/US-019 обрезались `config.py`, `ui.py`,
`project-data.json` и `CLAUDE.md` — последовательные вызовы `Edit` на
больших файлах с кириллицей вели к произвольному обрыву содержимого
(середина UTF-8 символа, неожиданный EOF) без явной ошибки от
инструмента.

Файлы восстановлены: `config.py`, `ui.py` — из локального бекапа
пользователя; `project-data.json` и `CLAUDE.md` — через Python-запись
(`json.load → mutate → json.dump`, текстовый heredoc) из контекста
сессии.

Зафиксированные правила работы с большими файлами — в `CLAUDE.md`,
раздел «Безопасное редактирование файлов (для AI-агентов)»:
выбор инструмента по размеру файла, обязательный бекап `.bak` перед
серией правок, валидация после каждой нетривиальной правки
(`ast.parse` / `json.load`), стоп-сигнал при первом признаке обрезания.

## v4.3.0

### Багфиксы и доработки итерации (TASK-038..050, BUG-CL-01..03, BUG-04)

- **TASK-040, TASK-044**: Single-instance lock через `QLockFile` с `staleLockTime=30с`. Второй запуск выходит с MessageBox «Уже запущено», убирая баг с двумя overlay в трее.
- **TASK-039, TASK-049**: `refresh_cloud_models` теперь не делает HTTP при старте — реестр восстанавливается из `cfg.openai_stt_model_id`/`elevenlabs_stt_model_id`. После успешного discover модели регистрируются напрямую через `set_cloud_models` (без повторного HTTP). Combo моделей в настройках обновляется и при стартовом, и при ручном discover через общий метод.
- **TASK-041, TASK-043**: Убран захардкоженный fallback `whisper-1` в `discover_models` (мешал нестандартным провайдерам типа routerai/Groq). Combo больше не сохраняет остаточные модели от прошлого провайдера. Расширены STT-keywords. Лог первых 30 model id если фильтр не нашёл — для диагностики.
- **TASK-042**: Защита от `UnicodeEncodeError` при попадании кириллицы в API-ключ — `_validate_api_key_charset` возвращает понятное сообщение вместо traceback.
- **TASK-045**: Автозагрузка cloud-моделей через `start_initial_cloud_discover` при старте программы (`QTimer.singleShot(1500)`). Не блокирует UI. При ошибке — статус-бар + трей-уведомление; если выбранная cloud-модель недоступна — автопереключение на `cloud_fallback_model_key`.
- **TASK-046**: Combo «Fallback при сбое облака» в настройках показывает только установленные локальные модели (как dropdown «Диктовки»), не весь `TRANSCRIPTION_MODELS`.
- **TASK-047**: `setFocusPolicy(Qt.NoFocus)` + `setAutoDefault(False)` на кнопки записи/копирования/расшифровки — Space-без-Ctrl больше не активирует диктовку внутри окна программы. Глобальный хоткей `Ctrl+Space` работает по-прежнему.
- **TASK-048**: Новый класс `EditableClickToOpenComboBox` — клик в любую часть редактируемого combo «OpenAI Model»/«ElevenLabs Model» открывает popup (раньше работала только стрелка справа).
- **BUG-CL-01**: При cloud→локальная fallback combo на «Диктовке» теперь сразу переключается на локальную (`refresh_available_models_combo(force_current=True)`). Раньше cloud-модель оставалась выбранной, потому что `is_available=True`.
- **BUG-CL-02**: Длинные сообщения об ошибках cloud-подключения больше не растягивают окно — `status_label` с `setWordWrap(True)` и `SizePolicy(Ignored, Preferred)`.
- **BUG-CL-03**: Во время cloud-расшифровки статус-бар пишет «Отправляю в облако: …», а не «Финальная расшифровка локально…».
- **BUG-04**: Новый `NoScrollSpinBox` (по аналогии с `NoScrollComboBox` из US-001). Поле «Длина чанка для облака» больше не меняет значение при скролле страницы настроек.

### Основной функционал (US-015, US-016, US-021, US-032)

- **US-015**: Подключение облачных STT через OpenAI-совместимый API (OpenAI Whisper API, Groq и любой совместимый прокси). В настройках раздел «Облачные модели» с полями API Key, Base URL, выбор модели + кнопка «Проверить соединение и обновить список моделей». Динамический discover моделей через `GET /v1/models` с фильтрацией по STT (whisper / transcribe / scribe / stt / speech-to-text).
- **US-016**: Подключение ElevenLabs Speech-to-Text. Поле API Key, выбор модели (`scribe_v1`, `scribe_v1_experimental`), проверка соединения через `GET /v1/user`.
- **US-021** (минимально): Выпадающий список моделей на вкладке «Диктовка» разделён на группы «── Локальные ──» и «── Облачные ──». Облачные модели без настроенного ключа отображаются серым с пометкой «(не настроено)».
- **US-032** (новая): Длинные надиктовки (> 60 сек по умолчанию) автоматически нарезаются на чанки и отправляются в облако параллельно (до 3 потоков, overlap 0.3 с). Порог настраивается в настройках.
- Проактивная проверка интернета через TCP-connect к хосту провайдера перед каждым cloud-запросом.
- При сбое cloud (нет интернета, 401, 5xx, лимит) — автоматическое переключение `selected_model` на `cloud_fallback_model_key` (по умолчанию `whisper:small`) с уведомлением в статус-баре и трее.
- Новый модуль `voice_input_app/cloud_stt.py`: `transcribe_openai_compatible`, `transcribe_elevenlabs`, `verify_*_connection`, `discover_models`, `split_and_transcribe`, `is_internet_available`, типизированные исключения `CloudSttError`/`CloudAuthError`/`CloudPayloadTooLarge`/`CloudRateLimit`/`CloudServerError`/`CloudNetworkError`.
- Новая зависимость: `requests>=2.31`.

## v4.2.1

- BUG-01: Выпадающие списки в настройках больше не прокручиваются колёсиком мыши — скролл всегда идёт по странице, кроме момента, когда dropdown открыт.
- BUG-02: Объединение коротких 1–3 секундных сегментов Whisper при диаризации в более естественные реплики.
- QUA-01: Улучшено качество диаризации — MFCC-фичи, k-means++ с рестартами, 3 прохода сглаживания.
- SUM-01..04: Локальная суммаризация расшифровок через llama-cpp-python (Phi-3.5 Mini Q4, CPU). UI для суммаризации на вкладках Файлы и История, настраиваемый системный промпт.
- API-01..04: REST API с очередью и async-режимом (FastAPI/uvicorn, опционально).
- UX-01: Кнопка «Открыть папку моделей» в настройках.
- Исправлен белый фон вкладки настроек на Windows (QScrollArea viewport palette). Решение и рекомендации — в README и комментариях `_settings_tab()`.
- Убраны кнопки «Проверить микрофон», «Настройки микрофона Windows», «Настройки звука Windows» из настроек. Оставлены: Автонастройка, Логи, Модели, Обновления.
- Горизонтальная прокрутка убрана из настроек.

## v4.2.0

- Added GitHub Releases updater infrastructure.
- Added Settings fields: automatic update checks and GitHub `owner/repo` repository.
- Added manual **Проверить обновления** button.
- Added background update check worker and update download worker.
- Added installer build support through Inno Setup: `installer/VoiceInputLocal.iss` and `build_installer.bat`.
- Added GitHub Actions workflow for tag-based Windows release builds.
- Added `release/latest.json.template` for teams that want a static update manifest later.
- Existing dictation, file transcription, diarization, overlay and microphone behavior remains unchanged.

## v4.1.3
- Исправлен регресс v4.1.2: если плавающая плашка становилась foreground window после перетаскивания, результат без активного поля ввода снова корректно показывается под плашкой.
- Логика защиты от двойной вставки теперь отличает главное окно приложения от плавающей плашки.
- Убраны обводки у самой плашки и preview-блока, чтобы вокруг индикатора не было ореола.

## v4.1.2
- Плавающая плашка теперь запоминает положение после перетаскивания.
- При следующем запуске плашка возвращается туда, где пользователь оставил её в прошлый раз.
- Если монитор или разрешение изменились, сохранённая позиция безопасно возвращается в видимую область экрана.

## v4.1.1
- Диаризация переведена в Vibe-like progressive flow: ASR-сегменты сразу эмитятся в UI со speaker label, если функция включена.
- Для Whisper сохранено объединение коротких сегментов в реплики; speaker назначается перед отправкой блока в UI.
- Для Parakeet сохранены естественные крупные блоки; speaker назначается на каждый готовый фрагмент без ожидания конца файла.
- Исправлено состояние кнопок «Проверить микрофон» и «Автонастройка микрофона»: кнопки корректно возвращаются к исходному тексту через 3–5 секунд.
- Добавлены логи `Diarization timeline started/ready` и `File ASR segment emitted with speaker`.

## v4.1

- Added a stronger local diarization assignment layer for file transcripts: audio is clustered on speech windows and speaker labels are applied to Whisper and Parakeet blocks by timeline overlap.
- Whisper file transcription now merges short 1-3 second raw segments into larger utterance-like blocks, closer to Parakeet's natural chunking.
- Parakeet segmentation is preserved and not aggressively merged.
- Parakeet file processing keeps 24-second natural chunks even when timestamp display is enabled.
- ASR blocks are still emitted progressively; speaker labels refresh the transcript afterward when diarization is enabled.
- Added logs for Whisper merge counts and stronger diarization diagnostics.

## v4.0.3

- Completed the v4 file UX patch: timestamps are now displayed only when **Точные таймкоды** is enabled.
- File ASR segments are emitted immediately even when diarization is enabled; diarization now runs as post-processing and refreshes speaker labels afterward.
- Added transcription logs for file options, immediate ASR segment emission, diarization start/finish and speaker label application.
- Kept v4.0.2 Whisper model policy: Distil is removed from the main model list, Large v3 Turbo is available, and only Russian-capable Whisper models remain.


## v4.0.1

- Fixed Whisper language handling: the selected UI language is now normalized and passed explicitly to all Whisper dictation and file transcription paths.
- Added detailed transcription logs for Whisper language/task arguments so language regressions can be diagnosed from logs.
- File transcription now emits visible blocks as soon as each Whisper/Parakeet segment is ready when diarization is disabled.
- Microphone test and microphone autodetect now show temporary button states instead of blocking popup dialogs.
- Selecting a model in the Dictation tab immediately makes it active and starts background preload; the extra “use selected model” button was removed from that tab.
- Preload now supports both Whisper and Parakeet models and chains to the newly selected model after any in-flight preload finishes.

## v4.0

- Added file-only options for **Точные таймкоды** and **Определять говорящих**. Both are visible out of the box and disabled by default.
- Added additional downloadable model entries in the Models tab: VAD for stable timestamps and Sortformer Diarization v2.1.
- Added structured transcript storage for file jobs: start, end, optional speaker label and text are saved to history alongside plain text.
- File transcript blocks can now show speaker labels like `Спикер 1` when diarization is enabled.
- Whisper file transcription can request word timestamps and split longer segments into smaller timestamped blocks when stable timestamps are enabled.
- Parakeet file transcription uses shorter chunks when precise timestamp mode is enabled.
- Added safe local segment-level speaker assignment fallback so diarization does not affect dictation or break ASR if the external Sortformer runtime path is unavailable.

## v3.9

- Added progressive file transcription UX: percent progress, processed time vs total duration and visible transcript blocks while the file is still being processed.
- File transcript blocks now show time ranges like `[00:12–00:27]` as they appear in the result area.
- Added compact overlay progress for file jobs, e.g. `Файл · 42%`.
- Added first-launch microphone autodetection: the app tries available input devices, chooses an openable microphone and saves it locally.
- Added Settings button **Автонастройка микрофона** to rerun microphone detection manually.
- Dictation remains blocked while file transcription or microphone autodetection is running to avoid ASR/audio resource conflicts.

## v3.8.1

- Fixed Whisper model validation for Systran/faster-whisper and faster-distil-whisper repositories: built-in Whisper models now use per-model manifests with `vocabulary.json` instead of the old incorrect `vocabulary.txt` requirement.
- Added clearer validation errors that list the missing model files instead of a generic incomplete-download message.
- Added optional Hugging Face token support for model downloads through the Settings tab or `HF_TOKEN` / `HUGGINGFACE_HUB_TOKEN` environment variables.
- The user-provided token is not embedded into the application archive; it is stored only in the local `config.json` on the user's machine after being entered in Settings.

## v3.8

- Added separate **Files** tab for local transcription of audio/video files.
- File transcription does not auto-paste and does not auto-copy to clipboard; the user copies the result manually.
- Dictation and file transcription are mutually exclusive so microphone input and long file jobs do not conflict.
- File results are saved to history with source metadata and original file name.
- Added safe media preparation through PyAV into temporary 16 kHz mono WAV for Whisper/Parakeet.
- Added file-job cancel flow: the result is ignored when cancellation is requested.

## v3.7

- Added safe download progress for Hugging Face models: spinner and percent in the Models table.
- Disabled Hugging Face/tqdm console progress bars in GUI/windowed builds to avoid stdout/stderr crashes.
- Added highlighted Hotkey field and helper text when the selected shortcut cannot be registered.
- Kept the previous working hotkey active if a newly selected combination is invalid.
- Added WDM-KS microphone fallback after WASAPI/DirectSound for machines where headsets fail through default backends.
- Added a friendly message for recordings shorter than 1 second.

## v3.6

- Added Windows WASAPI shared-first microphone capture for better compatibility with online meetings.
- Added setting: meeting compatibility mode for Zoom/Teams/Meet/browser calls.
- Added microphone fallback attempts across selected device, WASAPI default device, system default device and other WASAPI inputs.
- Added clearer user-facing message when recording is unavailable because the microphone is busy or not shareable.
- Added detailed audio diagnostics to `app.log`: devices, host APIs, sample rates and every open attempt.
- Fixed double insertion when the cursor is inside the app's own Dictation tab.
- Added app icon assets and wired them into the main window, tray icon and PyInstaller build.
- Updated `build_exe.bat` to include icon/resources and remind users to run from `dist\VoiceInputLocal`, not `build`.

## v3.5.3

- Added microphone device selection.
- Added microphone access test and shortcut to Windows microphone privacy settings.
