; Inno Setup script — Agent Start (per-user install, no admin/UAC prompt)
; AppVersion comes from version.iss, generated from version.py by tools\sync_version.py (build.bat runs it).
#include "version.iss"
#define AppName "Agent Start"
#define AppExe "AgentStart.exe"

[Setup]
AppId={{6E1B3C2A-4F7D-4C1E-9B8A-2D5F7A9C1E40}
AppName={#AppName}
AppVersion={#AppVersion}
VersionInfoVersion={#AppVersion}
AppPublisher=warrenjbg
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; Same AppId as the old "Hermes Controller": upgrading replaces it. New folder + shortcuts, old ones removed below.
UsePreviousAppDir=no
UsePreviousGroup=no
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=AgentStart-Setup-{#AppVersion}
SetupIconFile=..\assets\app.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; The app answers WM_CLOSE with a 'Stop Strata?' prompt, so don't let Setup close it: wait on its mutex.
AppMutex=AgentStart.Singleton,HermesController.Singleton
CloseApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "..\dist\AgentStart\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
Type: filesandordirs; Name: "{localappdata}\Programs\Hermes Controller"
Type: filesandordirs; Name: "{userprograms}\Hermes Controller"
Type: files; Name: "{autodesktop}\Hermes Controller.lnk"

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; AppUserModelID: "warrenjbg.AgentStart"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon; AppUserModelID: "warrenjbg.AgentStart"

[Registry]
; agentstart://game-off: the "Turn off Strata" button of the game toast starts AgentStart.exe with the URL, which hands
; it to the running app (named event) and exits. Per-user (HKCU\Software\Classes), removed on uninstall.
Root: HKCU; Subkey: "Software\Classes\agentstart"; ValueType: string; ValueName: ""; ValueData: "URL:Agent Start"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\agentstart"; ValueType: string; ValueName: "URL Protocol"; ValueData: ""
Root: HKCU; Subkey: "Software\Classes\agentstart\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#AppExe},0"
Root: HKCU; Subkey: "Software\Classes\agentstart\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExe}"" ""%1"""

[Run]
; In-app update (updater.launch_installer passes /RELAUNCH=1): start the new version when the silent install ends.
Filename: "{app}\{#AppExe}"; Flags: nowait; Check: ShouldRelaunch
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

; Settings and logs live in %APPDATA%\AgentStart and are kept on uninstall (whitelist, last load time).

[Code]
function ShouldRelaunch: Boolean;
begin
  Result := ExpandConstant('{param:RELAUNCH|0}') = '1';
end;

{ In-app update (updater.launch_installer): the app releases its single-instance mutex, starts this installer with
  /WAITPID=<its pid> and exits. Wait here, before any file is touched, for that process to be gone. If it is still
  running after 30 s, stop cleanly with an error (logged via /LOG) instead of overwriting files in use. }
const
  SYNCHRONIZE = $00100000;
  WAIT_TIMEOUT = $00000102;

function OpenProcess(dwDesiredAccess: Cardinal; bInheritHandle: Boolean; dwProcessId: Cardinal): THandle;
  external 'OpenProcess@kernel32.dll stdcall';
function WaitForSingleObject(hHandle: THandle; dwMilliseconds: Cardinal): Cardinal;
  external 'WaitForSingleObject@kernel32.dll stdcall';
function CloseHandle(hObject: THandle): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Pid: Cardinal;
  H: THandle;
begin
  Result := '';
  Pid := StrToIntDef(ExpandConstant('{param:WAITPID|0}'), 0);
  if Pid = 0 then
    Exit;
  H := OpenProcess(SYNCHRONIZE, False, Pid);
  if H = 0 then
    Exit;                                   { already gone }
  try
    if WaitForSingleObject(H, 30000) = WAIT_TIMEOUT then
      Result := 'Agent Start did not close within 30 seconds. Close it and run the update again.';
  finally
    CloseHandle(H);
  end;
end;

{ Uninstall: remove "Start with Windows" (written by the app, not by Setup, so Setup does not know about it). }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'Agent Start');
    RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run', 'Agent Start');
  end;
end;
