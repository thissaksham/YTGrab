; YTGrab installer — bundles the fast (onedir) build so startup stays quick.
; Build: iscc installer.iss  (after building the onedir into dist\YTGrab\)
#define AppName "YTGrab"
#define AppVersion "1.1.1"
#define AppExe "YTGrab.exe"

[Setup]
AppId={{7E2C1A94-YTGR-4B3A-9F21-YTGRABAPP001}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Saksham Srivastava
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; per-user install by default -> no admin prompt, install dir stays writable
; so history.json / cookies / deps can live right beside the app
PrivilegesRequiredOverridesAllowed=dialog commandline
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=YTGrab-Setup-{#AppVersion}
SetupIconFile=ytgrab.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
ChangesEnvironment=yes

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "envvars"; Description: "Set YTDLP, FFMPEG and FFPROBE environment variables (point to this app's tools, for scripts and the command line)"; GroupDescription: "Advanced:"; Flags: unchecked

[Registry]
; user env vars pointing at the app-managed (auto-updated) binaries in data\bin
Root: HKCU; Subkey: "Environment"; ValueType: string; ValueName: "YTDLP"; ValueData: "{app}\data\bin\yt-dlp.exe"; Flags: preservestringtype uninsdeletevalue; Tasks: envvars
Root: HKCU; Subkey: "Environment"; ValueType: string; ValueName: "FFMPEG"; ValueData: "{app}\data\bin\ffmpeg.exe"; Flags: preservestringtype uninsdeletevalue; Tasks: envvars
Root: HKCU; Subkey: "Environment"; ValueType: string; ValueName: "FFPROBE"; ValueData: "{app}\data\bin\ffprobe.exe"; Flags: preservestringtype uninsdeletevalue; Tasks: envvars

[Files]
; the entire onedir build (exe + _internal folder)
Source: "dist\YTGrab\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; remove app data (deps, profile, config, history) that the app wrote beside itself
Type: filesandordirs; Name: "{app}\data"
