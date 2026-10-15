; Inno Setup script: builds dist\ESRMS-Setup-<version>.exe from dist\ESRMS_Desktop.exe.
; Build: packaging\build.ps1 -Installer   (or: ISCC packaging\ESRMS-Setup.iss)
#define AppName "ESRMS"
#define AppFullName "ESRMS - Emergency Response and Safety Management System"
#ifndef AppVersion
  #define AppVersion "1.0.1"
#endif
#define AppExe "ESRMS_Desktop.exe"

[Setup]
AppId={{6B1E4C2A-9F3D-4E7B-A8C5-3D2F1E0B9A71}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=ESRMS
DefaultDirName={autopf}\ESRMS
DefaultGroupName=ESRMS
DisableProgramGroupPage=yes
; Installs for the current user without admin rights; users may choose "all users" instead.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=ESRMS-Setup-{#AppVersion}
SetupIconFile=esrms.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppFullName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
VersionInfoVersion={#AppVersion}

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "taskkill.exe"; Parameters: "/f /im {#AppExe}"; Flags: runhidden; RunOnceId: "KillESRMS"

[Code]
// User data (%LOCALAPPDATA%\ESRMS: logs, sign-ins, .env) is kept unless the user asks to remove it.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and DirExists(ExpandConstant('{localappdata}\ESRMS')) then
    if MsgBox('Also delete ESRMS settings, saved sign-ins and logs?', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      DelTree(ExpandConstant('{localappdata}\ESRMS'), True, True, True);
end;
