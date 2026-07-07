#define MyAppName "Voice Input Local"
#define MyAppExeName "VoiceInputLocal.exe"
#ifndef MyAppVersion
#define MyAppVersion "4.17.1"
#endif

[Setup]
AppId={{E2F24D29-1774-4F64-9A34-4D2B6E9F4C41}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=Voice Input Local
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

[Code]
{ US-048: не прерывать активную работу пользователя централизованным обновлением. }
{ Пока приложение занято (запись/диктовка/расшифровка файла/суммаризация), оно }
{ держит маркер %ProgramData%\VoiceInputLocal\busy.lock и периодически обновляет }
{ его. Если маркер присутствует — откладываем установку (Setup завершается, }
{ ничего не заменив и не закрыв приложение); система развёртывания (Kaspersky }
{ Security Center / GPO) повторит задачу позже, когда пользователь освободится. }
{ Устаревший маркер после аварийного завершения снимает само приложение при }
{ следующем запуске, поэтому обновление не блокируется навсегда. }
{ Тихая установка при СВОБОДНОМ приложении проходит как обычно: маркера нет, }
{ CloseApplications=yes корректно закрывает простаивающий экземпляр. }

function BusyMarkerExists(): Boolean;
begin
  Result := FileExists(ExpandConstant('{commonappdata}\VoiceInputLocal\busy.lock'));
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  if BusyMarkerExists() then
  begin
    Log('VoiceInputLocal занят активной работой — обновление отложено (busy.lock присутствует).');
    Result := False;
  end;
end;
