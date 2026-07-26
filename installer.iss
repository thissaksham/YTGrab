; YTGrab installer — bundles the fast (onedir) build so startup stays quick.
; Bundles ffmpeg/ffprobe (stable) and downloads the latest yt-dlp during setup,
; so the app opens ready-to-use with no first-launch dependency download.
; Build needs: dist\YTGrab\ (onedir), deps\ffmpeg.exe, deps\ffprobe.exe.
; Build: iscc installer.iss
#define AppName "YTGrab"
#define AppVersion "1.5.0"
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
Root: HKCU; Subkey: "Environment"; ValueType: string; ValueName: "YTDLP"; ValueData: "{localappdata}\YTGrab\bin\yt-dlp.exe"; Flags: preservestringtype uninsdeletevalue; Tasks: envvars
Root: HKCU; Subkey: "Environment"; ValueType: string; ValueName: "FFMPEG"; ValueData: "{localappdata}\YTGrab\bin\ffmpeg.exe"; Flags: preservestringtype uninsdeletevalue; Tasks: envvars
Root: HKCU; Subkey: "Environment"; ValueType: string; ValueName: "FFPROBE"; ValueData: "{localappdata}\YTGrab\bin\ffprobe.exe"; Flags: preservestringtype uninsdeletevalue; Tasks: envvars

[Files]
; the entire onedir build (exe + _internal folder)
Source: "dist\YTGrab\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
; bundled stable tools -> app's data\bin so deps are ready without downloading
Source: "deps\ffmpeg.exe"; DestDir: "{localappdata}\YTGrab\bin"; Flags: ignoreversion
Source: "deps\ffprobe.exe"; DestDir: "{localappdata}\YTGrab\bin"; Flags: ignoreversion
; latest yt-dlp + node (JS challenge runtime) downloaded to {tmp} during setup
; (below); each copied if its download succeeded, else the app fetches on first run
Source: "{tmp}\yt-dlp.exe"; DestDir: "{localappdata}\YTGrab\bin"; Flags: external ignoreversion skipifsourcedoesntexist
Source: "{tmp}\node.exe"; DestDir: "{localappdata}\YTGrab\bin"; Flags: external ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent


[Code]
// Fetch the latest yt-dlp into {tmp} right before files are copied. Works in
// both interactive and silent installs; the [Files] entry above copies it in
// (and is skipped if this failed -- the app then fetches yt-dlp on first run).
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then begin
    try
      DownloadTemporaryFile(
        'https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe',
        'yt-dlp.exe', '', nil);
    except
      Log('yt-dlp download failed (app will fetch it on first run): '
          + GetExceptionMessage);
    end;
    try
      DownloadTemporaryFile(
        'https://nodejs.org/dist/latest-v22.x/win-x64/node.exe',
        'node.exe', '', nil);
    except
      Log('node download failed (app will fetch it on first run): '
          + GetExceptionMessage);
    end;
  end;
end;
