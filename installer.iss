; Inno Setup Script for NearBeam
; Bundles NearBeam into a professional Windows Setup Installer

#define MyAppName "NearBeam"
#define MyAppVersion "1.5.0"
#define MyAppPublisher "Tanumay Goswami"
#define MyAppURL "https://github.com/tanumay-deb/NearBeam"
#define MyAppExeName "NearBeam.exe"

[Setup]
AppId={{D37E7791-53C2-4DC6-A976-C9E6A7F2D884}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
OutputDir=dist
OutputBaseFilename=NearBeam-v{#MyAppVersion}-Setup
SetupIconFile=assets\icon.ico
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
UninstallDisplayIcon={app}\{#MyAppExeName}
; Close a running (tray) NearBeam or legacy LANDrop so its locked exe can be replaced
CloseApplications=force

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "contextmenu"; Description: "Add ""Send with NearBeam"" to Windows Explorer right-click menu"; GroupDescription: "Windows Explorer Integration:"
Name: "autostart"; Description: "Automatically launch NearBeam on Windows startup"; GroupDescription: "Startup Options:"

[InstallDelete]
; Clean up legacy LANDrop files and shortcuts on upgrade
Type: files; Name: "{app}\LANDrop.exe"
Type: files; Name: "{autoprograms}\LANDrop.lnk"
Type: files; Name: "{autodesktop}\LANDrop.lnk"

[Files]
Source: "dist\NearBeam\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "assets\icon.ico"; DestDir: "{app}\assets"; Flags: ignoreversion
Source: "assets\icon.png"; DestDir: "{app}\assets"; Flags: ignoreversion
Source: "assets\checkmark.png"; DestDir: "{app}\assets"; Flags: ignoreversion
Source: "allow_firewall.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "README.md"; DestDir: "{app}"; Flags: isreadme ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\assets\icon.ico"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\assets\icon.ico"; Tasks: desktopicon
Name: "{userappdata}\Microsoft\Windows\SendTo\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--send"; IconFilename: "{app}\assets\icon.ico"; Tasks: contextmenu

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "LANDrop"; Flags: deletevalue
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#MyAppName}"; ValueData: """{app}\{#MyAppExeName}"" --minimized"; Tasks: autostart; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Classes\*\shell\NearBeam"; ValueType: string; ValueName: ""; ValueData: "Send with NearBeam"; Tasks: contextmenu; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\*\shell\NearBeam"; ValueType: string; ValueName: "Icon"; ValueData: """{app}\assets\icon.ico"""; Tasks: contextmenu; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\*\shell\NearBeam\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" --send ""%1"""; Tasks: contextmenu; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\Directory\shell\NearBeam"; ValueType: string; ValueName: ""; ValueData: "Send with NearBeam"; Tasks: contextmenu; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\Directory\shell\NearBeam"; ValueType: string; ValueName: "Icon"; ValueData: """{app}\assets\icon.ico"""; Tasks: contextmenu; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\Directory\shell\NearBeam\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" --send ""%1"""; Tasks: contextmenu; Flags: uninsdeletekey

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
