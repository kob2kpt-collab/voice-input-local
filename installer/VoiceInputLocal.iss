#define MyAppName "Voice Input Local"
#define MyAppExeName "VoiceInputLocal.exe"
#ifndef MyAppVersion
#define MyAppVersion "4.17.4"
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
{ US-048 + US-055 + US-057 + US-058: обработка централизованного обновления во }
{ время активной работы пользователя И при простое, с возвратом статуса в KSC. }
{                                                                             }
{ Обмен через маркеры в %ProgramData%\VoiceInputLocal: }
{  - busy.lock            — приложение занято (US-048); }
{  - update-pending.flag  — установщик -> приложению: «покажи окно выбора» (занят); }
{  - update-close.flag    — установщик -> приложению: «закройся для тихого }
{                           обновления при простое» (US-058). }
{ Решение пользователя (обновить сейчас / отложить) приложение обрабатывает }
{ само: «Закрыть и обновить» снимает busy.lock и закрывается (следующая попытка }
{ видит простой и ставит обновление), «Отклонить» просто продолжает работу. }
{                                                                             }
{ Коды возврата (US-055) для KSC: }
{  0   — обновление установлено; }
{  101 — отложено (приложение занято); }
{  прочие ненулевые — стандартные ошибки Inno Setup. }

const
  EXIT_DEFERRED_BUSY = 101;
  { US-058: WinAPI-константы для проверки, залочен ли .exe работающим приложением. }
  GENERIC_READ = $80000000;
  OPEN_EXISTING = 3;
  INVALID_HANDLE_VALUE = $FFFFFFFF;
  { US-058: сколько ждать самозакрытия приложения по сигналу update-close (мс). }
  CLOSE_WAIT_TIMEOUT_MS = 20000;

{ Штатный InitializeSetup=False даёт generic-код, поэтому для «отложено» }
{ выходим кастомным кодом через ExitProcess (ничего ещё не изменено). }
procedure ExitProcess(uExitCode: Cardinal);
  external 'ExitProcess@kernel32.dll stdcall';

{ US-058: открыть файл эксклюзивно на чтение удаётся только если приложение }
{ закрыто — у работающего процесса .exe залочен как загруженный образ. }
function CreateFileW(lpFileName: String; dwDesiredAccess, dwShareMode,
  lpSecurityAttributes, dwCreationDisposition, dwFlagsAndAttributes,
  hTemplateFile: Cardinal): Cardinal;
  external 'CreateFileW@kernel32.dll stdcall';
function CloseHandle(hObject: Cardinal): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';

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

procedure WriteMarker(const Name: String);
begin
  ForceDirectories(DataDir());
  SaveStringToFile(DataDir() + '\' + Name, '1', False);
end;

procedure WritePendingMarker();
begin
  WriteMarker('update-pending.flag');
end;

{ US-058: .exe свободен (приложение закрыто или ещё не установлено)? }
function ExeIsFree(): Boolean;
var
  h: Cardinal;
  ExePath: String;
begin
  ExePath := ExpandConstant('{app}\VoiceInputLocal.exe');
  if not FileExists(ExePath) then
  begin
    Result := True;  { файла нет — первичная установка, считаем «свободен» }
    Exit;
  end;
  h := CreateFileW(ExePath, GENERIC_READ, 0, 0, OPEN_EXISTING, 0, 0);
  if h = INVALID_HANDLE_VALUE then
  begin
    Result := False;  { залочен работающим приложением }
    Exit;
  end;
  CloseHandle(h);
  Result := True;
end;

{ US-058: ждём самозакрытия приложения по сигналу update-close до таймаута. }
function WaitForExeFree(TimeoutMs: Integer): Boolean;
var
  Waited: Integer;
begin
  Waited := 0;
  while Waited < TimeoutMs do
  begin
    if ExeIsFree() then
    begin
      Result := True;
      Exit;
    end;
    Sleep(1000);
    Waited := Waited + 1000;
  end;
  Result := ExeIsFree();
end;

function InitializeSetup(): Boolean;
begin
  Result := True;

  if not MarkerExists('busy.lock') then
  begin
    { Приложение простаивает или закрыто. US-058: НЕ полагаемся на }
    { CloseApplications кросс-сессионно — установщик под SYSTEM (сессия 0) не }
    { закроет трей-приложение пользовательской сессии через Restart Manager }
    { (подтверждено тестом v4.17.3: обновление вставало только при закрытом app). }
    ClearMarker('update-pending.flag');
    if ExeIsFree() then
    begin
      { приложение не запущено — ставим сразу }
      ClearMarker('update-close.flag');
      Exit;
    end;
    { приложение открыто и простаивает — просим его закрыться и ждём в этом же }
    { прогоне (single-pass). Приложение покажет сообщение, оставит фоновый }
    { релончер и закроется; после установки релончер перезапустит его. }
    Log('VoiceInputLocal открыт и простаивает — сигнал update-close, жду закрытия.');
    WriteMarker('update-close.flag');
    if WaitForExeFree(CLOSE_WAIT_TIMEOUT_MS) then
    begin
      ClearMarker('update-close.flag');
      Exit;  { закрылось — ставим тихо }
    end;
    { Не закрылось за таймаут (например, старая версия без обработчика US-058) — }
    { откладываем; KSC повторит задачу позже. }
    ClearMarker('update-close.flag');
    Log('VoiceInputLocal не закрылся за таймаут — обновление отложено.');
    ExitProcess(EXIT_DEFERRED_BUSY);
  end;

  { Приложение занято — US-057: сигналим приложению показать окно выбора и }
  { откладываем обновление (единый код). Решение (обновить/отложить) приложение }
  { обрабатывает само; отдельного кода «отклонено» в KSC нет. }
  ClearMarker('update-close.flag');
  Log('VoiceInputLocal занят — сигнал приложению, обновление отложено.');
  WritePendingMarker();
  ExitProcess(EXIT_DEFERRED_BUSY);
end;
