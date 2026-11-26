param([Parameter(Mandatory)][string]$Directory)

$ErrorActionPreference = 'Stop'
$destination = (New-Item -ItemType Directory -Force -Path $Directory).FullName
$version = '2.1.4'
$wheelName = "unicorn-$version-cp37-abi3-win_amd64.whl"
$sourceName = "unicorn-$version.tar.gz"
$dllHash = '010d85dce84b8f6ffaec3d5420f1b5f7768f859eb831be574c7da23e656ee8f1'

function Get-VerifiedFile([string]$Name, [string]$Uri, [string]$Sha256) {
    $file = Join-Path $destination $Name
    if ((Test-Path -LiteralPath $file -PathType Leaf) -and (Get-FileHash -LiteralPath $file).Hash -eq $Sha256) {
        return $file
    }
    $pending = Join-Path $destination ('download-' + [guid]::NewGuid())
    try {
        Invoke-WebRequest -Uri $Uri -OutFile $pending -TimeoutSec 120
        if ((Get-FileHash -LiteralPath $pending).Hash -ne $Sha256) { throw "Checksum mismatch for $Name" }
        Move-Item -LiteralPath $pending -Destination $file -Force
        return $file
    } finally {
        if (Test-Path -LiteralPath $pending) { Remove-Item -LiteralPath $pending -Force }
    }
}

$wheel = Get-VerifiedFile $wheelName "https://files.pythonhosted.org/packages/70/3d/de7be9bd1addabe6d8a1369381f8a080400c349850e978689c5e18287957/$wheelName" 'd7107500c64ce5c168fbff6bef9485b5db1350050036f4cea568650cf8bdbdf5'
$null = Get-VerifiedFile $sourceName "https://files.pythonhosted.org/packages/b2/1b/b4248aa8422e86de690cf8e85cf8feae4c33405a097d1ebe71570bdaa6f5/$sourceName" '00567a70e323f749b419cd86bee4f9115beab7ebba32194581c090cbb7c59cff'
$null = Get-VerifiedFile 'COPYING' "https://raw.githubusercontent.com/unicorn-engine/unicorn/$version/COPYING" '6f04ae8364d0079a192b14635f4b1da294ce18724c034c39a6a41d1b09df6100'

$dll = Join-Path $destination 'unicorn.dll'
if (!(Test-Path -LiteralPath $dll -PathType Leaf) -or (Get-FileHash -LiteralPath $dll).Hash -ne $dllHash) {
    $pending = Join-Path $destination ('runtime-' + [guid]::NewGuid())
    try {
        $archive = [System.IO.Compression.ZipFile]::OpenRead($wheel)
        try {
            $entry = $archive.GetEntry('unicorn/lib/unicorn.dll')
            if (!$entry) { throw 'Unicorn DLL missing from upstream archive' }
            [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $pending)
        } finally {
            $archive.Dispose()
        }
        if ((Get-FileHash -LiteralPath $pending).Hash -ne $dllHash) { throw 'Unicorn DLL checksum mismatch' }
        Move-Item -LiteralPath $pending -Destination $dll -Force
    } finally {
        if (Test-Path -LiteralPath $pending) { Remove-Item -LiteralPath $pending -Force }
    }
}
$destination
