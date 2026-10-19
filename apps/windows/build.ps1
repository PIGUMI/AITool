# Builds Faraway.exe with the C# compiler that ships with Windows (csc.exe). No extra install needed.
# Usage:  powershell -File apps\windows\build.ps1      -> dist\Faraway.exe
# (This file is ASCII-only on purpose: Windows PowerShell 5.1 misreads UTF-8 without BOM.)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent (Split-Path -Parent $here)
$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path $csc)) { throw "csc.exe not found: $csc" }
$out = Join-Path $root 'dist'
New-Item -ItemType Directory -Force $out | Out-Null
& $csc /nologo /target:winexe /optimize+ /utf8output "/out:$out\Faraway.exe" "/win32icon:$here\faraway.ico" `
  /reference:System.dll /reference:System.Drawing.dll /reference:System.Windows.Forms.dll /reference:System.Security.dll /reference:System.Web.Extensions.dll "$here\Faraway.cs"
if ($LASTEXITCODE -ne 0) { throw "Build failed (exit code $LASTEXITCODE)" }
Get-Item "$out\Faraway.exe" | Select-Object FullName, Length
