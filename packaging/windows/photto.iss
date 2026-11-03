; Inno Setup script for photto — per-user install, no admin rights needed.
; Build:  iscc /DAppVersion=1.1.0 /DSourceDir=..\..\build\photto packaging\windows\photto.iss

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\build\photto"
#endif

[Setup]
AppId={{6B7E3B1C-4B9E-4F7D-9C55-0F2C7E1A9D21}
AppName=photto
AppVersion={#AppVersion}
AppPublisher=laghab
AppPublisherURL=https://github.com/laghab/photto
AppSupportURL=https://github.com/laghab/photto/issues
DefaultDirName={localappdata}\Programs\photto
DefaultGroupName=photto
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\..\dist
OutputBaseFilename=photto-setup-{#AppVersion}
SetupIconFile=..\..\data\photto.ico
UninstallDisplayIcon={app}\photto.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\..\LICENSE
ChangesAssociations=yes

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "contextmenu"; Description: "Add ""Sort with photto"" to the folder right-click menu"; GroupDescription: "Explorer:"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\photto"; Filename: "{app}\photto.exe"; Comment: "Swipe through files: keep, favorite or trash"
Name: "{userdesktop}\photto"; Filename: "{app}\photto.exe"; Tasks: desktopicon

[Registry]
; right-click on a folder
Root: HKCU; Subkey: "Software\Classes\Directory\shell\photto"; ValueType: string; ValueName: ""; ValueData: "Sort with photto"; Tasks: contextmenu; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\Directory\shell\photto"; ValueType: string; ValueName: "Icon"; ValueData: "{app}\photto.exe"; Tasks: contextmenu
Root: HKCU; Subkey: "Software\Classes\Directory\shell\photto\command"; ValueType: string; ValueName: ""; ValueData: """{app}\photto.exe"" ""%1"""; Tasks: contextmenu
; right-click on the empty space inside a folder
Root: HKCU; Subkey: "Software\Classes\Directory\Background\shell\photto"; ValueType: string; ValueName: ""; ValueData: "Sort this folder with photto"; Tasks: contextmenu; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\Directory\Background\shell\photto"; ValueType: string; ValueName: "Icon"; ValueData: "{app}\photto.exe"; Tasks: contextmenu
Root: HKCU; Subkey: "Software\Classes\Directory\Background\shell\photto\command"; ValueType: string; ValueName: ""; ValueData: """{app}\photto.exe"" ""%V"""; Tasks: contextmenu

[Run]
Filename: "{app}\photto.exe"; Description: "Launch photto"; Flags: nowait postinstall skipifsilent
