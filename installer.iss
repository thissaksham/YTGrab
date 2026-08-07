; YTGrab installer — ships the fast (onedir) build so startup stays quick.
; Deps (yt-dlp, ffmpeg, node) are fetched by the app on first launch, same as the
; portable build -- so the installer stays lean (no bundled ~90 MB ffmpeg).
; Build needs: dist\YTGrab\ (onedir).
; Build: iscc installer.iss
#define AppName "YTGrab"
#define AppVersion "1.14.1"
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
; the in-app updater runs this silently; CloseApplications force-closes the running
; instance so files can be replaced. No AppMutex -- it would make silent setup abort
; instead of updating while the app is still running.
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "envvars"; Description: "Set YTDLP, FFMPEG and FFPROBE environment variables (point to this app's tools, for scripts and the command line)"; GroupDescription: "Advanced:"; Flags: unchecked

[Registry]
; user env vars pointing at the app-managed (auto-updated) binaries in data\bin
Root: HKCU; Subkey: "Environment"; ValueType: string; ValueName: "YTDLP"; ValueData: "{localappdata}\YTGrab\bin\yt-dlp.exe"; Flags: preservestringtype uninsdeletevalue; Tasks: envvars
Root: HKCU; Subkey: "Environment"; ValueType: string; ValueName: "FFMPEG"; ValueData: "{localappdata}\YTGrab\bin\ffmpeg.exe"; Flags: preservestringtype uninsdeletevalue; Tasks: envvars
Root: HKCU; Subkey: "Environment"; ValueType: string; ValueName: "FFPROBE"; ValueData: "{localappdata}\YTGrab\bin\ffprobe.exe"; Flags: preservestringtype uninsdeletevalue; Tasks: envvars

[Files]
; the entire onedir build (exe + _internal folder); deps are fetched on first run
Source: "dist\YTGrab\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
; plain entry (NOT postinstall) so it runs in silent installs too -> the app
; relaunches itself after an in-app update. postinstall entries only run when the
; interactive Finished page is shown, so they never fire during a silent update.
Filename: "{app}\{#AppExe}"; Flags: nowait
