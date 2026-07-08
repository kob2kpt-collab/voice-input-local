#define MyAppName "Voice Input Local"
#define MyAppExeName "VoiceInputLocal.exe"
#ifndef MyAppVersion
#define MyAppVersion "4.17.2"
#endif

[Setup]
AppId={{E2F24D29-1774-4F64-9A34-4D2B6E9F4C41}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=Voice Input Local
; US-056: версия в метаданных установщика, иначе «Версия файла» = 0.0.0.0.
; {#MyAppVersion} должен быть числовым x.x.x (Inno дополнит до x.x.x.0).
VersionInfoVersion={#MyAppVersion}
VersionInfoProductVersion={#MyAppVersion}
VersionInfoProductName={#MyAppName}
VersionInfoCompany=Voice Input Local
DefaultDirName={autopf}\VoiceInputLocal
DefaultGroupName=Voice Input Local
DisableProgramGroupPage=yes
OutputDir=..\release
OutputBaseFilename=VoiceInputLocalSetup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile=..\voice_input_app\assets\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Ярлыки:"; Flags: unchecked

[Files]
Source: "..\dist\VoiceInputLocal\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Voice Input Local"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\Voice Input Local"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Запустить Voice Input Local"; Flags: nowait postinstall skipifsilent

[Dirs]
; US-057: сигнальные маркеры лежат в %ProgramData%\VoiceInputLocal; даём Users
; право изменять/удалять файлы, чтобы приложение (под пользователем) и установщик
; (под SYSTEM) могли обмениваться сигнальными файлами. Применяется при установке;
; на циклах откладывания папку создаёт приложение (владелец → полный доступ).
Name: "{commonappdata}\VoiceInputLocal"; Permissions: users-modify

[Code]
{ US-048 + US-055 + US-057: обработка централизованного обновления во время }
{ активной работы пользователя, с возвратом реального статуса в KSC. }
{                                                                             }
{ Обмен через маркеры в %ProgramData%\VoiceInputLocal: }
{  - busy.lock            — приложение занято (US-048); }
{  - update-pending.flag  — установщик -> приложению: «покажи окно выбора»; }
{  - update-declined.flag — приложение -> установщику: «пользователь отклонил». }
{ «Согласие» отдельного маркера не требует: приложение снимает busy.lock и }
{ закрывается, и следующая попытка видит простой и ставит обновление. }
{                                                                             }
{ Коды возврата (US-055) для KSC: }
{  0   — обновление установлено; }
{  100 — отклонено пользователем; }
{  101 — отложено (приложение занято, ожидает решения пользователя); }
{  прочие ненулевые — стандартные ошибки Inno Setup. }

const
  EXIT_DECLINED_USER = 100;
  EXIT_DEFERRED_BUSY = 101;

{ Штатный InitializeSetup=False даёт generic-код, поэтому для «отклонено»/ }
{ «отложено» выходим кастомным кодом через ExitProcess (ничего ещё не изменено). }
procedure ExitProcess(uExitCode: Cardinal);
  external 'ExitProcess@kernel32.dll stdcall';

function DataDir(): String;
begin
  Result := ExpandConstant('{commonappdata}\VoiceInputLocal');
end;

function MarkerExists(const Name: String): Boolean;
begin
  Result := FileExists(DataDir() + '\' + Name);
end;

procedure ClearMarker(const Name: String);
var
  Path: String;
begin
  Path := DataDir() + '\' + Name;
  if FileExists(Path) then
    DeleteFile(Path);
end;

procedure WritePendingMarker();
begin
  ForceDirectories(DataDir());
  SaveStringToFile(DataDir() + '\update-pending.flag', '1', False);
end;

function InitializeSetup(): Boolean;
begin
  Result := True;

  if not MarkerExists('busy.lock') then
  begin
    { Приложение простаивает — ставим тихо (CloseApplications=yes закроет }
    { простаивающий экземпляр). Чистим сигнальные маркеры. }
    ClearMarker('update-pending.flag');
    ClearMarker('update-declined.flag');
    Exit;
  end;

  { Приложение занято. }
  if MarkerExists('update-declined.flag') then
  begin
    { Пользователь отклонил в этот цикл: сообщаем KSC «отклонено» и готовим }
    { повторный показ окна (снимаем declined, снова пишем pending). }
    Log('VoiceInputLocal: централизованное обновление отклонено пользователем.');
    ClearMarker('update-declined.flag');
    WritePendingMarker();
    ExitProcess(EXIT_DECLINED_USER);
  end;

  { Занят, решения ещё нет: сигналим приложению показать окно и откладываем. }
  Log('VoiceInputLocal занят — сигнал приложению, обновление отложено.');
  WritePendingMarker();
  ExitProcess(EXIT_DEFERRED_BUSY);
end;
