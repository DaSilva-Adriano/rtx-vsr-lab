# Build vsr_ngx.dll (x64, /MT) against RTX Video SDK 1.1
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Repo = Split-Path -Parent $Root
$Sdk = $env:NV_RTX_VIDEO_SDK
if (-not $Sdk -or -not (Test-Path $Sdk)) {
    $Sdk = "C:\VSR\RTX_Video_SDK_v1.1.0"
}
if (-not (Test-Path "$Sdk\include\nvsdk_ngx.h")) {
    throw "RTX Video SDK not found at $Sdk (set NV_RTX_VIDEO_SDK)"
}

$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
if (-not (Test-Path $vswhere)) {
    throw "vswhere.exe not found. Install Visual Studio 2022 Build Tools with the C++ workload."
}
$vs = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vs) {
    throw "MSVC x64 tools not installed. Install VS Build Tools workload VCTools."
}
$vsdev = Join-Path $vs "Common7\Tools\VsDevCmd.bat"
if (-not (Test-Path $vsdev)) {
    throw "VsDevCmd.bat not found under $vs"
}

$OutDir = Join-Path $Repo "vsr_lab"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$Dll = Join-Path $OutDir "_vsr_ngx.dll"

$cmd = @"
call `"$vsdev`" -arch=amd64 -host_arch=amd64 >nul
cd /d `"$Root`"
cl /nologo /O2 /LD /MT /EHsc /std:c++17 /W3 /DNDEBUG /DUNICODE /D_UNICODE /DWIN32_LEAN_AND_MEAN /DVSR_NGX_EXPORTS /I`"$Sdk\include`" vsr_ngx.cpp /link /LIBPATH:`"$Sdk\lib\Windows\x64`" nvsdk_ngx_s.lib d3d11.lib dxgi.lib user32.lib advapi32.lib shell32.lib ole32.lib /DLL /OUT:`"$Dll`"
if errorlevel 1 exit /b 1
del /q vsr_ngx.obj vsr_ngx.exp vsr_ngx.lib 2>nul
echo Built $Dll
"@

$bat = Join-Path $env:TEMP "vsr_ngx_build.bat"
Set-Content -Path $bat -Value $cmd -Encoding ASCII
& cmd.exe /c $bat
if ($LASTEXITCODE -ne 0) { throw "cl.exe failed ($LASTEXITCODE)" }
if (-not (Test-Path $Dll)) { throw "DLL was not produced: $Dll" }
Write-Host "OK $Dll"
