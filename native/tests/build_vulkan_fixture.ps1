param([string]$RenderDocSource = 'D:\workspace\renderdoc')
$ErrorActionPreference = 'Stop'
$repo = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$build = Join-Path $repo '.build\vulkan_fixture'
New-Item -ItemType Directory -Force -Path $build | Out-Null
& cl /nologo /std:c++17 /EHsc /O2 /MT "/I$RenderDocSource\renderdoc\driver\vulkan\official" "/I$RenderDocSource\renderdoc\api\app" "$PSScriptRoot\vulkan_eid_fixture.cpp" "/Fo$build\fixture.obj" "/Fe$build\fixture.exe"
if ($LASTEXITCODE) { throw 'Fixture build failed. Run in an x64 VS developer shell.' }
$previous = @{}
foreach ($name in @('VK_LAYER_PATH', 'VK_IMPLICIT_LAYER_PATH', 'VK_INSTANCE_LAYERS')) {
    $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
try {
    $env:VK_LAYER_PATH = Join-Path $repo 'third_party\renderdoc'
    $env:VK_IMPLICIT_LAYER_PATH = $env:VK_LAYER_PATH
    $env:VK_INSTANCE_LAYERS = 'VK_LAYER_RENDERDOC_Capture'
    $captureOutput = & "$build\fixture.exe" "$repo\third_party\renderdoc\renderdoc.dll" "$build\eid_fixture"
    if ($LASTEXITCODE) { throw "Fixture capture failed: $LASTEXITCODE" }
} finally {
    foreach ($name in $previous.Keys) { [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process') }
}
$captureOutput | Write-Output
$capture = @($captureOutput | Where-Object { $_ -match '^Capture: (.+)$' } | ForEach-Object { $_.Substring(9) })
if ($capture.Count -ne 1 -or !(Test-Path -LiteralPath $capture[0])) { throw 'Fixture capture path missing' }
& "$repo\third_party\renderdoc\rdc_replay_export.exe" $capture[0] "$build\official.csv"
if ($LASTEXITCODE) { throw 'Official replay failed' }
& "$repo\third_party\renderdoc\renderdoccmd.exe" convert -f $capture[0] -o "$build\fixture.xml" -c xml
if ($LASTEXITCODE) { throw 'Structured export failed' }
& "$repo\runtime\python\python.exe" "$repo\analyzer\vulkan_offline_events.py" "$build\fixture.xml" "$build\offline.json" --compare "$build\official.csv"
if ($LASTEXITCODE) { throw 'Offline/official EID comparison failed' }
