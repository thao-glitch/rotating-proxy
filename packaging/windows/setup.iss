; Rotating Proxy -- Windows installer (Inno Setup 6)
;
; CI compiles it with:
;   iscc /DAppVersion=1.0.0 packaging\windows\setup.iss
;
; Everything installs per-user (no UAC), so double-clicking the setup
; file is a Next-Next-Finish that ends with the panel already launched
; -- state lives in %APPDATA%\Rotating Proxy, never in the install dir.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{7A3F2C51-9B4E-4D6A-8E1C-2F5B0A9D4C77}}
AppName=Rotating Proxy
AppVersion={#AppVersion}
AppPublisher=Rotating Proxy contributors
DefaultDirName={autopf}\Rotating Proxy
DisableProgramGroupPage=yes
; per-user install: no elevation prompt, works on locked-down machines
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
OutputDir={#SourcePath}..\..\dist\installer
OutputBaseFilename=RotatingProxy-{#AppVersion}-win64
SetupIconFile={#SourcePath}..\..\packaging\icons\icon.ico
UninstallDisplayIcon={app}\rotating-proxy.exe
UninstallDisplayName=Rotating Proxy
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ChangesAssociations=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}:"

[Files]
Source: "{#SourcePath}..\..\dist\rotating-proxy\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{autoprograms}\Rotating Proxy"; Filename: "{app}\rotating-proxy.exe"
Name: "{autodesktop}\Rotating Proxy"; Filename: "{app}\rotating-proxy.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\rotating-proxy.exe"; Description: "{cm:LaunchProgram,Rotating Proxy}"; Flags: nowait postinstall skipifsilent
