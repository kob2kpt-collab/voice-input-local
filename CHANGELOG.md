# Changelog

## v4.2.2

- Replaced the primary Windows global hotkey backend with native `RegisterHotKey` / `WM_HOTKEY` handling, with the previous `keyboard` backend retained as a fallback.
- Added hotkey registration and hotkey event logging to make OBS/softphone conflicts diagnosable.
- Restricted regular recording fallback to the selected microphone or system default input instead of probing unrelated microphones.
- Kept microphone test and microphone autodetect flows intact: the test uses the same safer recording path, while autodetect can still scan available microphones.

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
