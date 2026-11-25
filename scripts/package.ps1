param(
    [string]$Target = 'x86_64-pc-windows-msvc'
)

$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$binary = Join-Path $project "target/$Target/release/ck3-llm.exe"
if (!(Test-Path -LiteralPath $binary -PathType Leaf)) {
    throw "Build the release executable first at $binary"
}

$output = Join-Path $project 'dist'
New-Item -ItemType Directory -Force -Path $output | Out-Null
$package = Join-Path $output 'ck3-llm-windows-x64.zip'
$pending = Join-Path $output ('package-' + [guid]::NewGuid() + '.zip')

Push-Location -LiteralPath $project
try {
    $features = @()
    if ($env:CK3_FRIDA_DEVKIT) { $features = @('--features', 'host-bridge') }
    $raw = cargo metadata --locked --format-version 1 --filter-platform $Target @features
    if ($LASTEXITCODE -ne 0) { throw 'cargo metadata failed' }
    $metadata = $raw | ConvertFrom-Json

    $archive = [System.IO.Compression.ZipFile]::Open($pending, 'Create')
    try {
        [void][System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive, $binary, 'ck3-llm.exe')
        [void][System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive, (Join-Path $project 'README.md'), 'README.md')
        if ($env:CK3_FRIDA_DEVKIT) {
            $fridaLicense = Join-Path $env:CK3_FRIDA_DEVKIT 'COPYING'
            [void][System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive, $fridaLicense, 'third-party/frida-core-17.26.2/COPYING')
        }
        $index = [System.Collections.Generic.List[string]]::new()
        foreach ($crate in $metadata.packages) {
            if (!$crate.source) { continue }
            $name = "$($crate.name)-$($crate.version)"
            $index.Add("$name  $($crate.license)")
            $root = Split-Path -Parent $crate.manifest_path
            $licenseFiles = @(Get-ChildItem -LiteralPath $root -File -Recurse | Where-Object {
                $_.Name -match '^(LICENSE|LICENCE|COPYING|NOTICE|OFL|UFL)([-_.].*|$)'
            })
            if ($crate.license_file) {
                $listed = Get-Item -LiteralPath (Join-Path $root $crate.license_file) -ErrorAction SilentlyContinue
                if ($listed -and $licenseFiles.FullName -notcontains $listed.FullName) { $licenseFiles += $listed }
            }
            foreach ($file in $licenseFiles) {
                $relative = [System.IO.Path]::GetRelativePath($root, $file.FullName).Replace('\', '/')
                if ($relative.StartsWith('../')) { continue }
                [void][System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive, $file.FullName, "third-party/$name/$relative")
            }
        }
        $entry = $archive.CreateEntry('third-party/INDEX.txt')
        $writer = [System.IO.StreamWriter]::new($entry.Open())
        try { $writer.WriteLine(($index -join "`n")) } finally { $writer.Dispose() }
    } finally {
        $archive.Dispose()
    }

    Move-Item -LiteralPath $pending -Destination $package -Force
    Get-FileHash -LiteralPath $package -Algorithm SHA256
} finally {
    if (Test-Path -LiteralPath $pending) { Remove-Item -LiteralPath $pending -Force }
    Pop-Location
}
