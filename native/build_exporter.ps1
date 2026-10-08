param([string]$RenderDocSource = 'D:\workspace\renderdoc')
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
$build = Join-Path $repo '.build\exporter'
$runtime = Join-Path $repo 'third_party\renderdoc'
New-Item -ItemType Directory -Force -Path $build | Out-Null
# Replay C++ ABI must match the bundled DLL, including its exact header revision.
$version = (& "$runtime\renderdoccmd.exe" version) -join "`n"
if ($version -notmatch 'built from ([0-9a-f]{40})') { throw 'Cannot determine RenderDoc revision' }
$revision = $Matches[1]
('#define EXPECTED_RENDERDOC_COMMIT "{0}"' -f $revision) | Set-Content "$build\export_version.h" -Encoding Ascii
& git -C $RenderDocSource archive $revision --output="$build\headers.tar" renderdoc/api/replay
if ($LASTEXITCODE) { throw 'Matching source revision is missing' }
& tar -xf "$build\headers.tar" -C $build
if ($LASTEXITCODE) { throw 'Header extraction failed' }
$exports = & dumpbin /nologo /exports "$runtime\renderdoc.dll"
$names = foreach ($line in $exports) {
    if ($line -match '^\s+\d+\s+[0-9A-F]+\s+[0-9A-F]+\s+(\S+)') { $Matches[1] }
}
if (!$names) { throw 'No DLL exports found' }
@('LIBRARY renderdoc', 'EXPORTS') + $names | Set-Content "$build\renderdoc.def" -Encoding Ascii
& lib /nologo /machine:x64 "/def:$build\renderdoc.def" "/out:$build\renderdoc.lib"
if ($LASTEXITCODE) { throw 'Import library build failed' }
& cl /nologo /std:c++17 /EHsc /O2 /MT /DRENDERDOC_PLATFORM_WIN32 /DNOMINMAX "/I$build" "/I$build\renderdoc\api\replay" "$PSScriptRoot\rdc_replay_export.cpp" "/Fo$build\export.obj" "/Fe$runtime\rdc_replay_export.exe" /link "$build\renderdoc.lib" "/IMPLIB:$build\export.lib"
if ($LASTEXITCODE) { throw 'Exporter build failed' }
