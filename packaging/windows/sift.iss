; Inno Setup script for SIFT — per-user install, no admin rights needed.
; Build:  iscc /DAppVersion=1.1.0 /DSourceDir=..\..\build\sift packaging\windows\sift.iss

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\build\sift"
#endif

[Setup]
AppId={{6B7E3B1C-4B9E-4F7D-9C55-0F2C7E1A9D21}
AppName=SIFT
AppVersion={#AppVersion}
AppPublisher=laghab
AppPublisherURL=https://github.com/laghab/sift
AppSupportURL=https://github.com/laghab/sift/issues
DefaultDirName={localappdata}\Programs\SIFT
DefaultGroupName=SIFT
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\..\dist
OutputBaseFilename=sift-{#AppVersion}-windows-setup
SetupIconFile=..\..\data\sift.ico
UninstallDisplayIcon={app}\sift.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\..\LICENSE
ChangesAssociations=yes

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "contextmenu"; Description: "Add ""Review with SIFT"" to the folder right-click menu"; GroupDescription: "Explorer:"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\SIFT"; Filename: "{app}\sift.exe"; Comment: "Review local files and find text"
Name: "{userdesktop}\SIFT"; Filename: "{app}\sift.exe"; Tasks: desktopicon

[Registry]
; right-click on a folder
Root: HKCU; Subkey: "Software\Classes\Directory\shell\sift"; ValueType: string; ValueName: ""; ValueData: "Review with SIFT"; Tasks: contextmenu; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\Directory\shell\sift"; ValueType: string; ValueName: "Icon"; ValueData: "{app}\sift.exe"; Tasks: contextmenu
Root: HKCU; Subkey: "Software\Classes\Directory\shell\sift\command"; ValueType: string; ValueName: ""; ValueData: """{app}\sift.exe"" ""%1"""; Tasks: contextmenu
; right-click on the empty space inside a folder
Root: HKCU; Subkey: "Software\Classes\Directory\Background\shell\sift"; ValueType: string; ValueName: ""; ValueData: "Review this folder with SIFT"; Tasks: contextmenu; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\Directory\Background\shell\sift"; ValueType: string; ValueName: "Icon"; ValueData: "{app}\sift.exe"; Tasks: contextmenu
Root: HKCU; Subkey: "Software\Classes\Directory\Background\shell\sift\command"; ValueType: string; ValueName: ""; ValueData: """{app}\sift.exe"" ""%V"""; Tasks: contextmenu

[Run]
Filename: "{app}\sift.exe"; Description: "Launch SIFT"; Flags: nowait postinstall skipifsilent
