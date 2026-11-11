; Inno Setup script — Hermes Controller (per-user install, no admin/UAC prompt)
; AppVersion comes from version.iss, generated from version.py by tools\sync_version.py (build.bat runs it).
#include "version.iss"
#define AppName "Hermes Controller"
#define AppExe "HermesController.exe"

[Setup]
AppId={{6E1B3C2A-4F7D-4C1E-9B8A-2D5F7A9C1E40}
AppName={#AppName}
AppVersion={#AppVersion}
VersionInfoVersion={#AppVersion}
AppPublisher=warrenjbg
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=HermesController-Setup-{#AppVersion}
SetupIconFile=..\assets\app.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; The app answers WM_CLOSE with a 'Stop Strata?' prompt, so don't let Setup close it: wait on its mutex.
AppMutex=HermesController.Singleton
CloseApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "..\dist\HermesController\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; AppUserModelID: "warrenjbg.HermesController"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon; AppUserModelID: "warrenjbg.HermesController"

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

; Settings and logs live in %APPDATA%\HermesController and are kept on uninstall (whitelist, last load time).
