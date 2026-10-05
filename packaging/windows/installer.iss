; Inno Setup 6 script - built by packaging/build.py:
;   iscc /DAppVersion=x.y.z /DSourceDir=<onedir build> /DOutputDir=<dist> installer.iss
;
; Updates: a newer setup.exe installs over the existing installation (same AppId).
; DJ Manager downloads it from GitHub releases and runs it with /SILENT; the [Run]
; entry starts the app again afterwards. Per-user install, so no admin rights or UAC
; prompt are needed - neither for the first install nor for updates.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\build\onedir-dist\DJManager"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist"
#endif

[Setup]
; Never change the AppId - it identifies the installation for updates and uninstall.
AppId={{B64B5D85-A46B-43F8-B46B-9A768FA3B929}
AppName=DJ Manager
AppVersion={#AppVersion}
AppVerName=DJ Manager {#AppVersion}
AppPublisher=DJ Manager
DefaultDirName={autopf}\DJ Manager
DefaultGroupName=DJ Manager
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=DJManager-{#AppVersion}-setup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\DJManager.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; Remove files of the previous version so stale libraries never mix with new ones.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; Tells DJ Manager it was installed (self-update then uses the installer).
Source: "installed.marker"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\DJ Manager"; Filename: "{app}\DJManager.exe"
Name: "{group}\Uninstall DJ Manager"; Filename: "{uninstallexe}"
Name: "{autodesktop}\DJ Manager"; Filename: "{app}\DJManager.exe"; Tasks: desktopicon

[Run]
; Runs after interactive installs (checkbox) and after silent self-updates.
Filename: "{app}\DJManager.exe"; Description: "{cm:LaunchProgram,DJ Manager}"; Flags: nowait postinstall
