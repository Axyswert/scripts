#requires -Version 5.0
<#
.SYNOPSIS
    Generates BLAKE3 and xxHash3-128 checksum manifests.

.DESCRIPTION
    Scans a directory for files (excluding existing checksum manifests) and
    generates BLAKE3 and xxHash3-128 checksums using the external tools
    `b3sum` and `xxhsum`.

    For each file, the script invokes `xxhsum -H2` and `b3sum` in separate
    processes, writing their output to buffered UTF-8 manifest streams.
    This design is tuned for mechanical (HDD) drives: the first hasher pays the
    disk I/O cost, and the second **typically** benefits from the operating
    system's page cache, so most files are read from disk only once in the
    common case. This behavior is not guaranteed and may vary based on system
    memory and load.

.PARAMETER Path
    Directory to scan. Defaults to current location.

.PARAMETER ManifestBase
    Base name for output files. Default: 'CHECKSUMS'.

.PARAMETER Recurse
    If set, scans subdirectories recursively. Skips ReparsePoints 
    (symlinks/junctions).

.PARAMETER Force
    Suppress confirmation prompt.

.EXAMPLE
    .\New-ChecksumManifest.ps1 -Recurse -Verbose
#>
[CmdletBinding()]
[OutputType('ChecksumManifestResult')]
param(
    [Parameter(Position = 0)]
    [ValidateScript({
        if ([string]::IsNullOrWhiteSpace($_)) {
            throw "Path must not be empty."
        }

        if ([System.Management.Automation.WildcardPattern]::ContainsWildcardCharacters($_)) {
            throw "Wildcards are not allowed in -Path."
        }

        if (-not (Test-Path -LiteralPath $_ -PathType Container)) {
            throw "Directory '$_' does not exist or is not a directory."
        }

        $true
    })]
    [string]$Path = '.',

    [Parameter(Position = 1)]
    [ValidateNotNullOrEmpty()]
    [string]$ManifestBase = 'CHECKSUMS',

    [switch]$Recurse,

    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-ManifestFiles {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Root,

        [Parameter(Mandatory = $true)]
        [string]$ExcludePattern,

        [switch]$Recurse
    )

    if (-not $Recurse) {
        Get-ChildItem -LiteralPath $Root -File -Force |
            Where-Object {
                $_.Name -notmatch $ExcludePattern -and
                -not ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint)
            }
        return
    }

    $rootDir = Get-Item -LiteralPath $Root
    $stack   = [System.Collections.Generic.Stack[System.IO.DirectoryInfo]]::new()
    $stack.Push([System.IO.DirectoryInfo]$rootDir)

    while ($stack.Count -gt 0) {
        $dir = $stack.Pop()

        try {
            $items = Get-ChildItem -LiteralPath $dir.FullName -Force -ErrorAction Stop
        }
        catch {
            Write-Warning "Could not access '$($dir.FullName)': $_"
            continue
        }

        foreach ($item in $items) {
            if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
                continue
            }

            if ($item.PSIsContainer) {
                $stack.Push([System.IO.DirectoryInfo]$item)
            }
            elseif ($item.Name -notmatch $ExcludePattern) {
                $item
            }
        }
    }
}

$targetPath = Convert-Path -LiteralPath $Path
$targetPathLen = $targetPath.Length

Write-Verbose ("Initialising in '{0}' (base: '{1}')" -f $targetPath, $ManifestBase)

if (-not (Get-Command b3sum  -ErrorAction SilentlyContinue)) { throw "Binary 'b3sum' not found." }
if (-not (Get-Command xxhsum -ErrorAction SilentlyContinue)) { throw "Binary 'xxhsum' not found." }

$tmpB3   = $null
$tmpXXH  = $null
$success = $false

Push-Location -LiteralPath $targetPath
try {
    $excludePattern = '^(?i)' + [regex]::Escape($ManifestBase) + '\..+$'
    $files = @(Get-ManifestFiles -Root '.' -ExcludePattern $excludePattern -Recurse:$Recurse)
    $count = $files.Count

    if ($count -eq 0) {
        Write-Warning "No files found to hash. Exiting."
        return
    }

    if ($PSCmdlet.MyInvocation.BoundParameters['Verbose']) {
        $stats = $files |
                 Group-Object {
                     if ([string]::IsNullOrEmpty($_.Extension)) { '<none>' }
                     else { $_.Extension.ToLowerInvariant() }
                 } |
                 Sort-Object Count -Descending |
                 Select-Object @{N='Extension'; E = { $_.Name }}, Count

        $statsStr = $stats | ForEach-Object { "{0,-10} : {1}" -f $_.Extension, $_.Count }
        Write-Verbose "Found $count files:`n$($statsStr -join "`n")"
    }

    if (-not $Force) {
        $msg  = if ($Recurse) { " (recursive)" } else { "" }
        $resp = Read-Host ("Generate checksum manifests in '{0}'{1} for {2} file(s)? [Y/n]" -f $targetPath, $msg, $count)
        if ($resp -and -not $resp.Trim().StartsWith('y', [System.StringComparison]::InvariantCultureIgnoreCase)) {
            Write-Warning "Aborted by user."
            return
        }
    }

    $manifestB3  = Join-Path $targetPath ("{0}.b3"     -f $ManifestBase)
    $manifestXXH = Join-Path $targetPath ("{0}.xxh128" -f $ManifestBase)
    $tmpB3       = "$manifestB3.tmp"
    $tmpXXH      = "$manifestXXH.tmp"

    Remove-Item -LiteralPath $tmpB3, $tmpXXH -ErrorAction SilentlyContinue

    $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    $swB3  = $null
    $swXXH = $null

    $originalEncoding = [Console]::OutputEncoding
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8

    try {
        $swB3  = [System.IO.StreamWriter]::new($tmpB3,  $false, $utf8NoBom)
        $swXXH = [System.IO.StreamWriter]::new($tmpXXH, $false, $utf8NoBom)
        $sortedFiles = $files | Sort-Object { $_.FullName.ToLowerInvariant() }
        $i = 0

        foreach ($f in $sortedFiles) {
            $i++
            if ($i % 100 -eq 0) {
                Write-Progress -Activity "Hashing" -Status "$i / $count" -PercentComplete (($i / $count) * 100)
            }

            $relPath = $f.FullName.Substring($targetPathLen).TrimStart('\', '/').Replace('\', '/')

            $outXXH = & xxhsum -H2 -- $relPath
            if ($LASTEXITCODE -ne 0) { throw "xxhsum failed on '$relPath'" }
            $swXXH.WriteLine($outXXH)

            $outB3 = & b3sum -- $relPath
            if ($LASTEXITCODE -ne 0) { throw "b3sum failed on '$relPath'" }
            $swB3.WriteLine($outB3)
        }
    }
    finally {
        [Console]::OutputEncoding = $originalEncoding

        if ($null -ne $swB3)  { $swB3.Dispose();  $swB3  = $null }
        if ($null -ne $swXXH) { $swXXH.Dispose(); $swXXH = $null }
        Write-Progress -Activity "Hashing" -Completed
    }

    Write-Verbose "Verifying line counts..."
    
    $countXXH = [System.Linq.Enumerable]::Count([System.IO.File]::ReadLines($tmpXXH))
    $countB3  = [System.Linq.Enumerable]::Count([System.IO.File]::ReadLines($tmpB3))

    if ($countXXH -ne $count) { throw "xxHash3 mismatch: expected $count, got $countXXH" }
    if ($countB3  -ne $count) { throw "BLAKE3 mismatch: expected $count, got $countB3" }

    Move-Item -LiteralPath $tmpXXH -Destination $manifestXXH -Force
    Move-Item -LiteralPath $tmpB3  -Destination $manifestB3  -Force
    
    $success = $true

    [pscustomobject]@{
        PSTypeName     = 'ChecksumManifestResult'
        Path           = $targetPath
        FileCount      = $count
        Recursive      = $Recurse
        Blake3Manifest = $manifestB3
        Xxh128Manifest = $manifestXXH
    }
}
finally {
    if (-not $success) {
        $pathsToRemove = @()
        if ($tmpB3)  { $pathsToRemove += $tmpB3 }
        if ($tmpXXH) { $pathsToRemove += $tmpXXH }

        if ($pathsToRemove.Count -gt 0) {
            Remove-Item -LiteralPath $pathsToRemove -ErrorAction SilentlyContinue
        }
    }

    Pop-Location
}