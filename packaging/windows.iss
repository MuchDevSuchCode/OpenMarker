; The Windows installer (Inno Setup 6). Built by packaging/build.py, which passes the version:
;   ISCC /DAppVersion=0.1.0 packaging\windows.iss
;
; Installs for the current user only, so it never asks for administrator rights.
; Your documents and settings live in %USERPROFILE%\openmarker and are left alone by
; updates; uninstalling offers to delete them.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6F0C5B0E-6B2B-4C0B-9A53-4E6C2B7D5A41}
AppName=OpenMarker
AppVersion={#AppVersion}
AppVerName=OpenMarker {#AppVersion}
AppPublisher=OpenMarker
AppPublisherURL=https://github.com/MuchDevSuchCode/OpenMarker
AppSupportURL=https://github.com/MuchDevSuchCode/OpenMarker/issues
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\OpenMarker
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
DisableDirPage=auto
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
WizardStyle=modern
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\OpenMarker.exe
UninstallDisplayName=OpenMarker
Compression=lzma2/max
SolidCompression=yes
OutputDir=..\dist
OutputBaseFilename=OpenMarker-{#AppVersion}-windows-setup
; The running app holds this; setup and uninstall wait for it to go (after asking the app to quit, below).
AppMutex=OpenMarkerRunning
CloseApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked
Name: "startup"; Description: "Start OpenMarker when I sign in (it syncs and sends in the background)"; Flags: unchecked

[InstallDelete]
; Files of an older version that the new one no longer has.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\OpenMarker\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\OpenMarker"; Filename: "{app}\OpenMarker.exe"; Comment: "Your paper tablet's library, on this computer"
Name: "{autodesktop}\OpenMarker"; Filename: "{app}\OpenMarker.exe"; Tasks: desktopicon
Name: "{userstartup}\OpenMarker"; Filename: "{app}\OpenMarker.exe"; Parameters: "--background"; Tasks: startup

[Run]
Filename: "{app}\OpenMarker.exe"; Description: "Start OpenMarker"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[Code]
// Ask a running copy to stop, and give it a moment to do so.
procedure QuitRunningApp();
var
  Program_: String;
  Code, Waited: Integer;
begin
  Program_ := ExpandConstant('{app}\OpenMarker.exe');
  if not CheckForMutexes('OpenMarkerRunning') or not FileExists(Program_) then
    exit;
  Exec(Program_, '--quit', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Waited := 0;
  while CheckForMutexes('OpenMarkerRunning') and (Waited < 100) do
  begin
    Sleep(100);
    Waited := Waited + 1;
  end;
end;

function HasSsh(): Boolean;
begin
  Result := FileExists(ExpandConstant('{sys}\OpenSSH\ssh.exe')) or (FileSearch('ssh.exe', GetEnv('PATH')) <> '');
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  QuitRunningApp();
  Result := '';
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and not HasSsh() and not WizardSilent() then
    MsgBox('OpenMarker reaches the tablet with the OpenSSH client, which was not found on this computer.' + #13#10#13#10 +
           'Add it under Settings > System > Optional features > OpenSSH Client. OpenMarker is installed and will work once it is there.',
           mbInformation, MB_OK);
end;

function InitializeUninstall(): Boolean;
begin
  QuitRunningApp();
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Data: String;
begin
  if CurUninstallStep <> usPostUninstall then
    exit;
  Data := ExpandConstant('{%USERPROFILE}\openmarker');
  if DirExists(Data) and not UninstallSilent() then
    if MsgBox('Also delete your OpenMarker data?' + #13#10#13#10 +
              'That is the copy of your documents, their version history, the search index and your settings, in ' + Data + '.' + #13#10 +
              'Nothing on the tablet is touched. Choose No to keep it for a later install.',
              mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      DelTree(Data, True, True, True);
end;
