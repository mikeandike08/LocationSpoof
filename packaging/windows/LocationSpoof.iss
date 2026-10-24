; Inno Setup script for the Windows installer. Built by packaging/build.py after PyInstaller:
;   iscc /DAppVersion=1.2.3 packaging\windows\LocationSpoof.iss
; Installs per-user by default (no admin prompt); the first page offers an all-users install.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "LocationSpoof"
#define AppExe "LocationSpoof.exe"
#define Root "..\.."

[Setup]
AppId={{8EF17105-73D1-4F3F-A40D-DE7B75AC13FA}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppName}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#Root}\dist\installer
OutputBaseFilename={#AppName}-Setup-{#AppVersion}
SetupIconFile={#Root}\build\icons\LocationSpoof.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Close a running copy (via Restart Manager) before replacing its files.
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#Root}\dist\LocationSpoof\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; The tray app may still be running; stop it so its files can be removed.
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM {#AppExe}"; Flags: runhidden; RunOnceId: "StopLocationSpoof"

[Code]
// LocationSpoof talks to iPhones through Apple's driver (Apple Mobile Device Service), which
// Apple doesn't allow to be bundled. Offer to get it if it's missing.
function AppleDriverInstalled: Boolean;
begin
  Result := RegKeyExists(HKLM, 'SYSTEM\CurrentControlSet\Services\Apple Mobile Device Service');
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ErrorCode: Integer;
begin
  if (CurStep = ssPostInstall) and (not WizardSilent) and (not AppleDriverInstalled) then
  begin
    if MsgBox('LocationSpoof needs Apple''s iPhone driver, which isn''t installed yet.' + #13#10#13#10 +
              'Install the free "Apple Devices" app from the Microsoft Store (or iTunes), then open it once ' +
              'with your iPhone plugged in.' + #13#10#13#10 + 'Open the Microsoft Store page now?',
              mbConfirmation, MB_YESNO) = IDYES then
      ShellExec('open', 'https://apps.microsoft.com/detail/9np83lwlpz9k', '', '', SW_SHOWNORMAL, ewNoWait, ErrorCode);
  end;
end;
