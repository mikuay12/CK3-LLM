param([Parameter(Mandatory)][string]$Directory)

$ErrorActionPreference = 'Stop'
$version = '17.26.2'
$name = "frida-core-devkit-$version-windows-x86_64.tar.xz"
$expected = '8277fd57e3f1b0fd17a048311e8f7da5a242cb2d5b219641e37b5abfe59638a0'
$destination = (New-Item -ItemType Directory -Force -Path $Directory).FullName
$archive = Join-Path $destination $name
if (!(Test-Path -LiteralPath $archive) -or (Get-FileHash -LiteralPath $archive).Hash -ne $expected) {
    curl.exe -fsSL --retry 2 --connect-timeout 20 --max-time 220 -o $archive "https://github.com/frida/frida/releases/download/$version/$name"
    if ($LASTEXITCODE -ne 0) { throw 'Frida devkit download failed' }
}
if ((Get-FileHash -LiteralPath $archive).Hash -ne $expected) { throw 'Frida devkit checksum mismatch' }
if (!(Test-Path -LiteralPath (Join-Path $destination 'frida-core.lib'))) {
    tar.exe -xf $archive -C $destination
    if ($LASTEXITCODE -ne 0) { throw 'Frida devkit extraction failed' }
}
foreach ($required in @('frida-core.h','frida-core.lib')) {
    if (!(Test-Path -LiteralPath (Join-Path $destination $required) -PathType Leaf)) { throw "Frida devkit missing $required" }
}
$license = Join-Path $destination 'COPYING'
$licenseExpected = '5ea1544b51a28bc823b03159190d4108f9fb4f4ef912389f5137c6d295e175b2'
if (!(Test-Path -LiteralPath $license -PathType Leaf) -or (Get-FileHash -LiteralPath $license).Hash -ne $licenseExpected) {
    Invoke-WebRequest -Uri "https://raw.githubusercontent.com/frida/frida-core/$version/COPYING" -OutFile $license -TimeoutSec 45
}
if ((Get-FileHash -LiteralPath $license).Hash -ne $licenseExpected) { throw 'Frida license checksum mismatch' }
$destination
