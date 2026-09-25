$ErrorActionPreference = 'Stop'

$source = Join-Path $PSScriptRoot 'src\1541_onerom_universal_gui.py'
$output = Join-Path $PSScriptRoot 'dist'

py -m pip install --upgrade pip
py -m pip install -r (Join-Path $PSScriptRoot 'requirements.txt')
py -m pip install nuitka
py -m nuitka --mode=onefile --zig --enable-plugin=tk-inter --windows-console-mode=disable `
    --assume-yes-for-downloads --output-dir=$output `
    --output-filename=1541-OneROM-Universal-GUI-V1.0.0-GUI_converge01.exe $source

$exe = Join-Path $output '1541-OneROM-Universal-GUI-V1.0.0-GUI_converge01.exe'
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
    throw "Build failed: $exe was not created"
}

Get-FileHash -LiteralPath $exe -Algorithm SHA256
