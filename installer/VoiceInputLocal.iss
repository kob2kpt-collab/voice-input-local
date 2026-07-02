#define MyAppName "Voice Input Local"
#define MyAppExeName "VoiceInputLocal.exe"
#ifndef MyAppVersion
#define MyAppVersion "4.15.0"
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
