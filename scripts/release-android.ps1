<#
.SYNOPSIS
  Publish a built APK: powershell.exe -NoProfile -File .\scripts\release-android.ps1 -Notes 'What is new'

.DESCRIPTION
  Publishes the highest versioned APK from the Android release output to dist/apk (or -StagePath).
  Verifies the embedded version and signature, installs immutable files, and switches latest.json last.
  With PowerShell 7 and -DeltaClasspathFile, also builds and verifies bsdiff patches from archived APKs.
  -BuildAndPublish preserves the original Windows build-and-publish workflow (PowerShell 7).

  -Prebuild (no publication): run it in a task's Android worktree before the merge, e.g.
    pwsh -File .\scripts\release-android.ps1 -Prebuild -AndroidPath <Android worktree>
  It works out the version the next real release will get (the live release directory is only read), runs the whole unit-test
  suite (which records the green mark for the commit) and builds the release APK with that version. Gradle's build cache keeps
  every task output, so the real release of the same commit and version after the fast-forward merge takes everything from the
  cache (R8, lint, resources) and its Gradle stage drops from minutes to seconds. If another version is published in between,
  the cache simply misses and the release builds the usual way.
#>
[CmdletBinding()]
param(
    [string]$Notes,
    [switch]$Prebuild,
    [switch]$BuildAndPublish,
    [string]$DeltaClasspathFile,
    [string]$VersionName,
    [string]$StagePath,
    [string]$AndroidPath,
    [string]$HubUrl
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$production = 'B:\Music\.apk'
$publishExisting = -not ($Prebuild -or $BuildAndPublish)
if (-not $publishExisting -and $PSVersionTable.PSVersion.Major -lt 7) { throw 'The legacy Gradle workflow requires PowerShell 7.' }
if ($publishExisting -and $VersionName) { throw 'VersionName comes from the built APK; do not pass -VersionName.' }
if ($DeltaClasspathFile -and (-not $publishExisting -or $PSVersionTable.PSVersion.Major -lt 7)) { throw '-DeltaClasspathFile requires PowerShell 7 and a prebuilt APK.' }
if ($DeltaClasspathFile) { $DeltaClasspathFile = (Resolve-Path -LiteralPath $DeltaClasspathFile).Path }
$customStage = -not [string]::IsNullOrWhiteSpace($StagePath)
$staging = $publishExisting -or $customStage
$destination = if ($customStage) { [IO.Path]::GetFullPath($StagePath).TrimEnd('\') } elseif ($publishExisting) { Join-Path $root 'dist\apk' } else { $production }
if ($staging -and ($destination -ieq $production -or $destination.StartsWith($production + '\', [StringComparison]::OrdinalIgnoreCase))) { throw 'Staging directory must be outside the live release directory.' }
if ($staging) {
    $ancestor = $destination
    while ($ancestor) {
        if ((Test-Path -LiteralPath $ancestor) -and ((Get-Item -Force -LiteralPath $ancestor).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Staging path must not pass through a junction or symbolic link.' }
        $parent = Split-Path -Parent $ancestor
        if ($parent -eq $ancestor) { break }
        $ancestor = $parent
    }
}
if (-not $AndroidPath) { $AndroidPath = Join-Path $root 'android' }
if (-not $staging -and -not $Prebuild -and [IO.Path]::GetFullPath($AndroidPath).TrimEnd('\') -ine [IO.Path]::GetFullPath((Join-Path $root 'android')).TrimEnd('\')) { throw 'Custom AndroidPath is permitted only in staging or with -Prebuild.' }
if ($Prebuild -and $HubUrl) { throw '-HubUrl is meaningless with -Prebuild.' }
if (-not $staging -and $HubUrl) { throw 'Custom HubUrl is permitted only in staging.' }
if (-not $HubUrl) { $HubUrl = 'http://localhost:8080' }
$HubUrl = $HubUrl.TrimEnd('/')
$AndroidPath = [IO.Path]::GetFullPath($AndroidPath)
$Notes = "$Notes".Trim()
if (-not $Prebuild -and ($Notes.Length -eq 0 -or $Notes.Length -gt 2000)) { throw 'Notes must be 1-2000 characters.' }
if (-not $publishExisting) {
    $env:GRADLE_USER_HOME = 'B:\AndroidEmu\gradle-home'
    $env:JAVA_HOME = 'B:\AndroidEmu\gradle-home\jdks\jdk-21.0.12.1+1'
    $env:PATH = ($env:PATH -split ';' | Where-Object { $_ -notmatch '"' }) -join ';'
}
$java = if ($env:JAVA_HOME) { Join-Path $env:JAVA_HOME 'bin\java.exe' } else { 'java.exe' }
$sdk = if ($env:ANDROID_HOME) { $env:ANDROID_HOME } elseif ($env:ANDROID_SDK_ROOT) { $env:ANDROID_SDK_ROOT } elseif ($publishExisting) { Join-Path $env:LOCALAPPDATA 'Android\Sdk' } else { 'B:\AndroidEmu\Sdk' }
$tools = Get-ChildItem -LiteralPath (Join-Path $sdk 'build-tools') -Directory | Where-Object {
    $_.Name -match '^\d+\.\d+\.\d+$' -and (Test-Path (Join-Path $_.FullName 'aapt.exe')) -and (Test-Path (Join-Path $_.FullName 'apksigner.bat'))
} | Sort-Object { [version]$_.Name } -Descending | Select-Object -First 1
if (-not $tools) { throw 'Install Android SDK Build Tools with aapt and apksigner, and set ANDROID_HOME.' }
$aapt = Join-Path $tools.FullName 'aapt.exe'
$apksigner = Join-Path $tools.FullName 'apksigner.bat'
$legacyName = '^HomeMusic-(\d+\.\d+\.\d+)-(\d+)\.apk$'
$versionedName = '^app-homemusic-release-(\d+\.\d+\.\d+)\.apk$'
$packageId = 'app.homemusic.selfhost'
$maxApk = 128MB          # client limits (UpdateDelta.kt)
$maxDeltaApk = 32MB
$maxPatch = 16MB
$patchBases = 6

function Invoke-Checked([string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Exe failed with exit code $LASTEXITCODE" }
}
function Git-Value([string]$Path, [string[]]$Arguments) {
    $value = & git -C $Path @Arguments
    if ($LASTEXITCODE -ne 0) { throw 'Git failed' }
    return ($value -join "`n").Trim()
}
# Wall-clock time per stage (leaf stages only, so the rows do not overlap); printed as a table when the run ends.
$timings = [ordered]@{}
$total = [Diagnostics.Stopwatch]::StartNew()
function Measure-Stage([string]$Name, [scriptblock]$Body) {
    $watch = [Diagnostics.Stopwatch]::StartNew()
    try { & $Body } finally {
        $watch.Stop()
        if (-not $timings.Contains($Name)) { $timings[$Name] = @{ seconds = 0.0; count = 0 } }
        $timings[$Name].seconds += $watch.Elapsed.TotalSeconds
        $timings[$Name].count++
    }
}
function Write-Timings {
    $rows = @($timings.Keys | ForEach-Object { [pscustomobject]@{ Stage = $_; Seconds = [Math]::Round($timings[$_].seconds, 1); Calls = $timings[$_].count } })
    $measured = ($rows | Measure-Object Seconds -Sum).Sum
    $rows += [pscustomobject]@{ Stage = 'other (script glue)'; Seconds = [Math]::Round($total.Elapsed.TotalSeconds - $measured, 1); Calls = '' }
    $rows += [pscustomobject]@{ Stage = 'TOTAL'; Seconds = [Math]::Round($total.Elapsed.TotalSeconds, 1); Calls = '' }
    Write-Host 'Stage timings:'
    $rows | Format-Table -AutoSize | Out-String | Write-Host
}
# Functions that also run inside the parallel workers (ForEach-Object -Parallel starts clean runspaces), defined once as text.
$workerFunctions = @'
function Sha([string]$Path) { (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant() }
function Invoke-Delta([string]$Java, [string]$Classpath, [string[]]$ToolArguments) {
    $output = & $Java '-Xmx2g' '-cp' $Classpath 'io.sigpipe.jbsdiff.ui.CLI' @ToolArguments 2>&1
    if ($LASTEXITCODE -ne 0) { throw "bsdiff tool failed with exit code ${LASTEXITCODE}: $output" }
}
# Rebuilds the target from the base and the patch exactly as the phone does and compares it with the target hash.
function Test-Roundtrip([string]$Java, [string]$Classpath, [string]$Temp, [string]$Base, [string]$PatchFile, [string]$TargetSha, [long]$TargetSize) {
    $restored = Join-Path $Temp ('restored-' + [guid]::NewGuid().ToString('N') + '.apk')
    try {
        Invoke-Delta $Java $Classpath @('patch', $Base, $restored, $PatchFile)
        if ((Get-Item -LiteralPath $restored).Length -ne $TargetSize -or (Sha $restored) -ne $TargetSha) { throw "Delta roundtrip failed for $PatchFile" }
    } finally { Remove-Item -LiteralPath $restored -Force -ErrorAction SilentlyContinue }
}
'@
. ([scriptblock]::Create($workerFunctions))
function Hash([string]$Path) { Measure-Stage 'sha256' { Sha $Path } }
function Check-Source {
    if (-not $staging) {
        if ((Git-Value $root @('branch','--show-current')) -ne 'master' -or (Git-Value $AndroidPath @('branch','--show-current')) -ne 'homemusic') { throw 'Production release requires master and homemusic branches.' }
        $rootChanges = @(git -C $root status --porcelain --untracked-files=normal | Where-Object { $_ -ne '?? .codex-attachments/' })
        if ($LASTEXITCODE -ne 0 -or $rootChanges.Count -gt 0 -or (Git-Value $AndroidPath @('status','--porcelain','--untracked-files=normal'))) { throw 'Sources must be clean, including untracked files.' }
    }
}
# Identity of the Android sources being released: the commit, plus a content hash when a rehearsal runs on a dirty tree.
function Source-Id {
    $head = Git-Value $AndroidPath @('rev-parse','HEAD')
    $status = Git-Value $AndroidPath @('status','--porcelain','--untracked-files=normal')
    if (-not $status) { return $head }
    $sha = [Security.Cryptography.SHA256]::Create()
    $feed = { param([byte[]]$b) $null = $sha.TransformBlock($b, 0, $b.Length, $null, 0) }
    & $feed ([Text.Encoding]::UTF8.GetBytes((& git -C $AndroidPath diff HEAD --binary 2>$null | Out-String)))
    foreach ($file in (& git -C $AndroidPath ls-files --others --exclude-standard | Sort-Object)) {
        & $feed ([Text.Encoding]::UTF8.GetBytes($file))
        & $feed ([IO.File]::ReadAllBytes((Join-Path $AndroidPath $file)))
    }
    $null = $sha.TransformFinalBlock([byte[]]::new(0), 0, 0)
    return "$head+dirty-" + (($sha.Hash | ForEach-Object { $_.ToString('x2') }) -join '').Substring(0, 16)
}
# True when a complete, successful run of the whole unit-test suite is recorded for this exact commit on a clean tree.
# build.gradle writes the record into the git directory shared by all worktrees, so a run in a task worktree counts after a fast-forward merge.
function Test-TestsRecorded {
    $head = Git-Value $AndroidPath @('rev-parse','HEAD')
    if ((Source-Id) -ne $head) { return $false }   # dirty tree: never
    $common = Git-Value $AndroidPath @('rev-parse','--git-common-dir')
    if (-not [IO.Path]::IsPathRooted($common)) { $common = Join-Path $AndroidPath $common }
    return (Test-Path -LiteralPath (Join-Path (Join-Path $common 'homemusic-tests-passed') $head) -PathType Leaf)
}
function Info([string]$Path) {
    Measure-Stage 'apk-info (copy+aapt+apksigner)' { Info-Raw $Path }
}
function Info-Raw([string]$Path) {
    $inspect = Join-Path $temp ("inspect-" + [guid]::NewGuid().ToString("N") + ".apk")
    [IO.File]::Copy($Path, $inspect)   # aapt cannot open a path with Cyrillic characters
    try {
        $badging = & $aapt dump badging $inspect
        if ($LASTEXITCODE -ne 0) { throw 'aapt failed' }
        $package = $badging | Select-String "^package: name='([^']+)' versionCode='([0-9]+)' versionName='([^']+)'" | Select-Object -First 1
        if (-not $package) { throw 'Invalid APK package metadata' }
        $null = $package.Line -match "^package: name='([^']+)' versionCode='([0-9]+)' versionName='([^']+)'"
        $pkg, $code, $version = $Matches[1], [long]$Matches[2], $Matches[3]
        $cert = & $apksigner verify --verbose --print-certs $inspect
        if ($LASTEXITCODE -ne 0) { throw 'APK signature verification failed' }
        if (-not ($cert | Select-String '^Number of signers: 1$')) { throw 'APK must have one signer' }
        $line = $cert | Select-String '^(?:Signer #1|V[0-9.]+ Signer):? certificate SHA-256 digest: ([0-9a-fA-F]+)$' | Select-Object -First 1
        if (-not $line) { throw 'APK certificate missing' }
        $null = $line.Line -match 'digest: ([0-9a-fA-F]+)$'
        return @{ package=$pkg; code=$code; version=$version; cert=$Matches[1].ToLowerInvariant() }
    } finally { Remove-Item -LiteralPath $inspect -Force -ErrorAction SilentlyContinue }
}
# aapt + apksigner verdicts of APKs that are already on disk. An APK is immutable and its verdict depends only on its bytes
# and the build-tools version, so the verdict is cached by SHA-256: every release otherwise re-verified all earlier APKs.
$infoCachePath = if ($publishExisting) { Join-Path $env:LOCALAPPDATA 'HomeMusic\release-cache\apk-info.json' } else { 'B:\AndroidEmu\release-cache\apk-info.json' }
$infoCache = @{}
$infoCacheDirty = $false
if (Test-Path -LiteralPath $infoCachePath) {
    try {
        $cached = Get-Content -Raw -Encoding utf8 -LiteralPath $infoCachePath | ConvertFrom-Json
        foreach ($entry in $cached.PSObject.Properties) {
            $v = $entry.Value
            if ($v.package -is [string] -and $v.version -is [string] -and $v.cert -match '^[0-9a-f]{64}$' -and $v.code -is [ValueType]) {
                $infoCache[$entry.Name] = @{ package=$v.package; code=[long]$v.code; version=$v.version; cert=$v.cert }
            }
        }
    } catch { Write-Warning 'APK info cache is unreadable and is ignored.'; $infoCache = @{} }
}
function Info-Key([string]$Sha) { "$Sha|$($tools.Name)" }
function Info-Cached([string]$Path, [string]$Sha) {
    $key = Info-Key $Sha
    if ($infoCache.ContainsKey($key)) { return $infoCache[$key] }
    $result = Info $Path
    $script:infoCache[$key] = $result
    $script:infoCacheDirty = $true
    return $result
}
function Save-InfoCache {
    if (-not $infoCacheDirty) { return }
    $tmp = "$infoCachePath.$([guid]::NewGuid().ToString('N')).tmp"
    try {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $infoCachePath) | Out-Null
        [IO.File]::WriteAllText($tmp, ($infoCache | ConvertTo-Json -Depth 4), [Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $tmp -Destination $infoCachePath -Force
        $script:infoCacheDirty = $false
    } catch { Write-Warning "APK info cache was not saved: $($_.Exception.Message)" }
    finally { if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue } }
}
# The single universal APK is named by the AGP output metadata of this very build, never by a wildcard.
function Resolve-ReleaseApk([string]$Dir, [long]$Code, [string]$Name) {
    $metaPath = Join-Path $Dir 'output-metadata.json'
    if (-not (Test-Path -LiteralPath $metaPath)) { throw 'Build output metadata missing.' }
    $meta = Get-Content -Raw -Encoding utf8 -LiteralPath $metaPath | ConvertFrom-Json
    $elements = @($meta.elements)
    if ($meta.applicationId -ne $packageId -or $meta.variantName -ne 'homemusicRelease' -or $meta.artifactType.type -ne 'APK') { throw 'Build metadata describes another artifact.' }
    if ($elements.Count -ne 1) { throw 'Expected exactly one release APK in build metadata.' }
    $element = $elements[0]
    if ($element.type -ne 'SINGLE' -or @($element.filters).Count -ne 0) { throw 'Release APK is not universal (ABI or density split).' }
    if ([long]$element.versionCode -ne $Code -or $element.versionName -ne $Name) { throw 'Build metadata version differs from the requested release.' }
    $file = [string]$element.outputFile
    if ($file -ne [IO.Path]::GetFileName($file) -or $file -notmatch '^[A-Za-z0-9._-]+\.apk$') { throw 'Unsafe output file name in build metadata.' }
    $path = Join-Path $Dir $file
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Release APK named by build metadata is missing: $file" }
    return $path
}
function Find-VersionedApk([string]$Dir) {
    $file = Get-ChildItem -LiteralPath $Dir -File -Filter 'app-homemusic-release-*.apk' |
        Where-Object { $_.Name -cmatch $versionedName } |
        Sort-Object { $_.Name -match $versionedName | Out-Null; [version]$Matches[1] } -Descending |
        Select-Object -First 1
    if (-not $file) { throw "No versioned APK in $Dir. Build :app:releaseHomemusicRelease before publishing." }
    return $file
}
# Copies a file that a concurrent build might still be writing: the source must not change while it is copied.
function Copy-Stable([string]$Source, [string]$Target) {
    $before = Get-Item -LiteralPath $Source
    $beforeHash = Hash $Source
    [IO.File]::Copy($Source, $Target)
    $after = Get-Item -LiteralPath $Source
    if ($before.Length -ne $after.Length -or $before.LastWriteTimeUtc -ne $after.LastWriteTimeUtc -or (Hash $Target) -ne $beforeHash) { throw 'Release APK changed while it was being copied.' }
    (Get-Item -LiteralPath $Target).IsReadOnly = $true
}
function Install-Immutable([string]$Source, [string]$Target) {
    if (Test-Path -LiteralPath $Target) {
        if ((Hash $Source) -ne (Hash $Target)) { throw "Immutable artifact collision: $Target" }
        return
    }
    $tmp = "$Target.$([guid]::NewGuid().ToString('N')).tmp"
    try {
        [IO.File]::Copy($Source, $tmp, $false)
        (Get-Item -LiteralPath $tmp).IsReadOnly = $false
        [IO.File]::SetLastWriteTimeUtc($tmp, [DateTime]::UtcNow)
        [IO.File]::Move($tmp, $Target)
        (Get-Item -LiteralPath $Target).IsReadOnly = $true
    } finally { if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force } }
}
function Check-Live([long]$Code, [string]$Sha) {
    $live = Invoke-RestMethod -Uri "$HubUrl/app/latest.json" -TimeoutSec 15
    if ($live.versionCode -ne $Code -or $live.sha256 -ine $Sha) { throw 'Published release does not match the Hub response.' }
}
# Every file a manifest promises exists on disk with the promised bytes (a repeat run after a finished release).
function Test-Published($Manifest) {
    $legacy = Join-Path $destination $Manifest.file
    if (-not (Test-Path -LiteralPath $legacy) -or (Hash $legacy) -ine $Manifest.sha256) { throw 'Published APK is missing or differs from latest.json; fix manually.' }
    $entries = @($Manifest.full) + @($Manifest.patches) | Where-Object { $_ }
    foreach ($entry in $entries) {
        $path = Join-Path (Join-Path $destination 'artifacts') ([IO.Path]::GetFileName([string]$entry.url))
        if (-not (Test-Path -LiteralPath $path) -or (Get-Item -LiteralPath $path).Length -ne $entry.size -or (Hash $path) -ine $entry.sha256) { throw "Published artifact is missing or damaged: $($entry.url)" }
    }
}
# Reads and validates latest.json of a release directory; $null when there is none.
function Read-Latest([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try { $value = Get-Content -Raw -Encoding utf8 -LiteralPath $Path | ConvertFrom-Json } catch { throw 'Invalid latest.json; release stopped.' }
    if ($value.versionCode -isnot [long] -and $value.versionCode -isnot [int]) { throw 'Invalid versionCode in latest.json.' }
    if ($value.versionCode -lt 1 -or $value.versionName -notmatch '^\d+\.\d+\.\d+$' -or
        ($value.file -cnotmatch $legacyName -and $value.file -cnotmatch $versionedName)) { throw 'Invalid existing release metadata.' }
    return $value
}
# The version of the next release. One function for the real release and for -Prebuild, so that they cannot disagree (a
# different version means a different BuildConfig and the Gradle cache of the prebuild would be useless). Files above the
# published versionCode are leftovers of a failed run that the release deletes before this point; they are ignored here
# so that -Prebuild, which never deletes anything, gets the same number.
function Get-NextVersion($Latest) {
    $maxCode = if ($Latest) { [long]$Latest.versionCode } else { 0 }
    Get-ChildItem -LiteralPath $destination -File | ForEach-Object {
        if ($_.Name -match $legacyName -and (-not $Latest -or [long]$Matches[2] -le [long]$Latest.versionCode)) { $maxCode = [Math]::Max($maxCode, [long]$Matches[2]) }
    }
    $code = $maxCode + 1
    if ($code -gt 2100000000) { throw 'versionCode limit exceeded' }
    $name = $VersionName
    if (-not $name) {
        if ($Latest) { $parts = $Latest.versionName.Split('.'); $name = "$($parts[0]).$($parts[1]).$([long]$parts[2] + 1)" }
        else { $name = '1.0.0' }
    }
    if ($name -notmatch '^\d+\.\d+\.\d+$') { throw 'VersionName must have major.minor.patch form.' }
    if ($Latest -and [version]$name -le [version]$Latest.versionName) { throw "VersionName $name must be newer than the published $($Latest.versionName)." }
    return @{ code = $code; name = $name }
}
# A tracked file that is LF in the index but CRLF in the working copy looks clean to git (core.autocrlf=input) yet changes the
# Gradle cache keys of everything that reads it, so the build cache of a prebuild made in another folder is missed.
function Warn-EolDrift {
    $drift = @(& git -C $AndroidPath ls-files --eol | Where-Object { $_ -match '^i/lf\s+w/crlf' -and $_ -notmatch 'attr/\S*\s+eol=crlf' })
    if ($drift.Count -gt 0) { Write-Warning ("{0} tracked file(s) have CRLF in the working copy but LF in git, which breaks Gradle cache reuse; recreate them from HEAD (delete + git checkout -- <file>): {1}" -f $drift.Count, (($drift | Select-Object -First 5 | ForEach-Object { ($_ -split '\t')[-1] }) -join ', ')) }
}
function Test-CacheEnabled {
    $file = Join-Path $AndroidPath 'gradle.properties'
    return (Test-Path -LiteralPath $file) -and [bool](Select-String -LiteralPath $file -Pattern '^\s*org\.gradle\.caching\s*=\s*true\s*$' -Quiet)
}

if ($Prebuild) {
    # No publication and no lock: the release directory is only read (a release switching latest.json at this moment at worst
    # makes the prebuild use a stale number, and then the cache misses - nothing else is affected).
    if (Git-Value $AndroidPath @('status','--porcelain','--untracked-files=normal')) { throw 'Prebuild needs a clean Android tree (commit everything first): the test record is written only for a clean tree.' }
    if (-not (Test-CacheEnabled)) { Write-Warning 'org.gradle.caching=true is missing in gradle.properties: the prebuild will not speed the release up.' }
    try {
        $latest = Read-Latest (Join-Path $destination 'latest.json')
        $head = Git-Value $AndroidPath @('rev-parse','HEAD')
        if ($latest -and $latest.androidCommit -eq $head) { Write-Host "These sources are already published as $($latest.versionName) ($($latest.versionCode)); nothing to prebuild."; return }
        $next = Get-NextVersion $latest
        Write-Host "Prebuild: commit $($head.Substring(0, 12)), the next release will be $($next.name) ($($next.code))."
        Push-Location $AndroidPath
        try { Measure-Stage 'gradle (tests + release build)' { Invoke-Checked '.\gradlew.bat' @('testHomemusicDebugUnitTest', 'assembleHomemusicRelease', "-PhmVersionCode=$($next.code)", "-PhmVersionName=$($next.name)") } } finally { Pop-Location }
        Write-Host "Prebuild done for $($next.name) ($($next.code)). After the fast-forward merge the release of this commit takes the build from the Gradle cache; another published version in between makes it miss (correct, just slower)."
    } finally { Write-Timings }
    return
}

if (-not $publishExisting) { Check-Source }
New-Item -ItemType Directory -Force -Path $destination | Out-Null
try { $lock = [IO.File]::Open((Join-Path $destination '.release.lock'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
catch { throw 'Another release is running for this directory.' }
$scratchRoot = if ($publishExisting) { [IO.Path]::GetTempPath() } else { 'B:\AndroidEmu\tmp' }
New-Item -ItemType Directory -Force -Path $scratchRoot | Out-Null
$temp = Join-Path $scratchRoot ("homemusic-release-" + [guid]::NewGuid().ToString('N'))
try {
    New-Item -ItemType Directory -Path $temp | Out-Null
    $latestPath = Join-Path $destination 'latest.json'
    $latest = $null
    $latestHash = if (Test-Path -LiteralPath $latestPath) { Hash $latestPath } else { $null }
    if ($latestHash) { $latest = Read-Latest $latestPath }
    if (-not $publishExisting -and $latest.file -cmatch $versionedName) { throw 'This directory uses versioned APKs; publish a prebuilt APK instead.' }
    $sourceId = if (-not $publishExisting) { Source-Id }
    if (-not $publishExisting -and $latest -and $latest.androidCommit -eq $sourceId) {
        # Same sources as the published release: nothing to build, only prove that the published files are intact.
        Test-Published $latest
        if (-not $staging) { Check-Live $latest.versionCode $latest.sha256 }
        Write-Host "Already published: $($latest.versionName) ($($latest.versionCode)) from these sources; nothing changed."
        return
    }
    # Files of an earlier run that failed before latest.json was switched were never offered to anyone: reuse their numbers.
    if (-not $publishExisting -and $latest) {
        Get-ChildItem -LiteralPath $destination -File | Where-Object { $_.Name -match $legacyName -and [long]$Matches[2] -gt [long]$latest.versionCode } | ForEach-Object {
            Write-Host "Removing unpublished leftover of a failed run: $($_.Name)"
            Remove-Item -LiteralPath $_.FullName -Force
        }
    }
    $previousCert = $null
    if ($latest) {
        $old = Join-Path $destination $latest.file
        if (-not (Test-Path -LiteralPath $old)) { throw 'Previous APK missing.' }
        if ($latest.sha256 -and (Hash $old) -ine $latest.sha256) { throw 'Previous APK hash mismatch.' }
        $oldSha = if ($latest.sha256) { ([string]$latest.sha256).ToLowerInvariant() } else { Hash $old }
        $oldInfo = Info-Cached $old $oldSha
        if ($oldInfo.package -ne $packageId -or $oldInfo.code -ne $latest.versionCode -or $oldInfo.version -ne $latest.versionName) { throw 'Previous APK metadata mismatch (updates require the same package ID).' }
        $previousCert = $oldInfo.cert
    }
    $outputDir = Join-Path $AndroidPath 'app\build\outputs\apk\homemusic\release'
    $classpathFile = $DeltaClasspathFile
    if ($publishExisting) {
        $selected = Find-VersionedApk $outputDir
        $sourceApk = $selected.FullName
        $downloadName = $selected.Name
        $null = $downloadName -cmatch $versionedName
        $VersionName = $Matches[1]
    } else {
        $next = Get-NextVersion $latest
        $code, $VersionName = $next.code, $next.name
        $classpathFile = Join-Path $temp 'classpath.txt'
        $gradleTasks = @('assembleHomemusicRelease', ':app:writeHomeMusicDeltaClasspath')
        if (-not (Test-TestsRecorded)) { $gradleTasks = @('testHomemusicDebugUnitTest') + $gradleTasks }
        Warn-EolDrift
        Push-Location $AndroidPath
        try { Measure-Stage 'gradle' { Invoke-Checked '.\gradlew.bat' ($gradleTasks + @("-PhmVersionCode=$code", "-PhmVersionName=$VersionName", "-PhmDeltaClasspathFile=$classpathFile")) } } finally { Pop-Location }
        $sourceApk = Resolve-ReleaseApk $outputDir $code $VersionName
        $downloadName = "HomeMusic-$VersionName-$code.apk"
    }
    $apk = Join-Path $temp 'release.apk'
    Copy-Stable $sourceApk $apk
    $info = Info $apk
    if ($publishExisting) { $code = $info.code }
    if ($info.package -ne $packageId -or $code -lt 1 -or $code -gt 2100000000 -or $info.code -ne $code -or $info.version -cne $VersionName -or ($previousCert -and $info.cert -ne $previousCert)) { throw 'Release APK metadata or certificate mismatch.' }
    if ($publishExisting -and $latest) {
        if ($latest.file -ceq $downloadName -and $latest.versionCode -eq $code -and $latest.sha256 -eq (Hash $apk)) {
            Test-Published $latest
            Write-Host "Already published: $VersionName ($code) in $destination."
            return
        }
        if ($code -le $latest.versionCode -or [version]$VersionName -le [version]$latest.versionName) { throw 'Release version and versionCode must be newer than latest.json.' }
    }
    # DEX stored uncompressed (build.gradle) keeps patches several times smaller; a compressed DEX would silently lose that.
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($apk)
    try { $dex = @($zip.Entries | Where-Object { $_.FullName -match '^classes\d*\.dex$' }) } finally { $zip.Dispose() }
    if ($dex.Count -eq 0) { throw 'The release APK contains no DEX.' }
    if (@($dex | Where-Object { $_.CompressedLength -ne $_.Length }).Count -gt 0) { Write-Warning 'DEX is stored compressed: patches will be about three times larger (see packaging.dex in app/build.gradle).' }
    $sha = Hash $apk
    $infoCache[(Info-Key $sha)] = $info     # the next release verifies this APK as a base without running aapt/apksigner again
    $infoCacheDirty = $true
    $size = (Get-Item -LiteralPath $apk).Length
    if ($size -gt $maxApk) { throw 'APK exceeds client limit.' }
    $artifacts = Join-Path $destination 'artifacts'
    New-Item -ItemType Directory -Force $artifacts | Out-Null
    $fullName = "apk-$sha.apk"
    $patches = @()
    $candidates = @()
    if ($classpathFile) {
        $classpath = (Get-Content -Raw -LiteralPath $classpathFile).Trim()
        if (-not $classpath) { throw 'Delta classpath is empty.' }
        $candidates = Get-ChildItem -LiteralPath $destination -File | Where-Object {
            ($_.Name -match $legacyName -and [long]$Matches[2] -lt $code) -or
            ($_.Name -cmatch $versionedName -and [version]$Matches[1] -lt [version]$VersionName)
        } | Sort-Object { [version]([regex]::Match($_.Name, '\d+\.\d+\.\d+').Value) } -Descending
    }
    # Phase 1 (serial, cheap): which earlier APKs get a patch.
    $work = @()
    foreach ($candidate in $candidates) {
        if ($candidate.Length -gt $maxDeltaApk -or $size -gt $maxDeltaApk) { Write-Host "Skip $($candidate.Name): above the delta size limit."; continue }
        $baseSha = Hash $candidate.FullName
        $baseInfo = Info-Cached $candidate.FullName $baseSha
        if ($candidate.Name -match $legacyName -and $baseInfo.code -ne [long]$Matches[2]) { continue }
        if ($baseInfo.code -lt 1 -or $baseInfo.code -ge $code -or ($latest -and $baseInfo.code -gt $latest.versionCode)) { continue }
        if ($baseInfo.package -ne 'app.homemusic' -or $baseInfo.cert -ne $info.cert) { Write-Host "Skip $($candidate.Name): another package or signature."; continue }
        if ($work.Sha -contains $baseSha) { continue }
        $work += [pscustomobject]@{ Name = $candidate.Name; Path = $candidate.FullName; Sha = $baseSha; Info = $baseInfo }
        if ($work.Count -ge $patchBases) { break }
    }
    Save-InfoCache
    # Phase 2 (parallel, one worker per base): bsdiff, then the phone's own restore, compared byte for byte with the release.
    # The patch bytes depend only on (base, release), so running the bases side by side changes nothing in them.
    $results = @()
    if ($work.Count -gt 0) {
        Measure-Stage 'patches: diff + restore (parallel)' {
            $script:results = @($work | ForEach-Object -ThrottleLimit $work.Count -Parallel {
                $item = $_
                . ([scriptblock]::Create($using:workerFunctions))
                $outcome = [pscustomobject]@{ Item = $item; Error = $null; Skip = $null; Patch = $null; Size = 0; Sha = $null }
                try {
                    $patch = Join-Path $using:temp ("delta-$($item.Info.code).bin")
                    Invoke-Delta $using:java $using:classpath @('diff', $item.Path, $using:apk, $patch)
                    $patchSize = (Get-Item -LiteralPath $patch).Length
                    if ($patchSize -ge $using:size -or $patchSize -gt $using:maxPatch) { $outcome.Skip = "patch $patchSize B does not save traffic."; return $outcome }
                    Test-Roundtrip $using:java $using:classpath $using:temp $item.Path $patch $using:sha $using:size
                    if ((Sha $item.Path) -ne $item.Sha) { throw "Base APK changed during release: $($item.Name)" }
                    $outcome.Patch = $patch; $outcome.Size = $patchSize; $outcome.Sha = Sha $patch
                } catch { $outcome.Error = $_.Exception.Message }
                $outcome
            })
        }
    }
    $failed = @($results | Where-Object { $_.Error })
    if ($failed.Count -gt 0) { throw (($failed | ForEach-Object { "$($_.Item.Name): $($_.Error)" }) -join '; ') }
    foreach ($r in ($results | Sort-Object { $_.Item.Info.code } -Descending)) {
        if ($r.Skip) { Write-Host "Skip $($r.Item.Name): $($r.Skip)"; continue }
        $patchName = "patch-$($r.Sha).bsdiff"
        Install-Immutable $r.Patch (Join-Path $artifacts $patchName)
        $patches += @{ baseVersionCode=$r.Item.Info.code; baseSha256=$r.Item.Sha; format='bsdiff40-v1'; size=$r.Size; sha256=$r.Sha; url="/app/artifacts/$patchName" }
        Write-Host "Delta from $($r.Item.Info.version) ($($r.Item.Info.code)): $($r.Size) bytes (full $size); roundtrip OK"
    }
    if (-not $publishExisting) {
        Check-Source
        if ((Source-Id) -ne $sourceId) { throw 'Android sources changed during release.' }
    }
    Install-Immutable $apk (Join-Path $artifacts $fullName)
    Install-Immutable $apk (Join-Path $destination $downloadName)
    if ($staging -and $HubUrl -eq 'http://localhost:8080') {
        Write-Host 'Staging: the Hub download check is skipped (no -HubUrl).'
    } else {
        # Before the switch, prove through the Hub's own routes that clients will get exactly these bytes.
        $downloads = @([pscustomobject]@{ Url = "/app/artifacts/$fullName"; Size = $size; Sha = $sha; Base = $null }) + @($patches | ForEach-Object {
            $p = $_
            [pscustomobject]@{ Url = $p.url; Size = $p.size; Sha = $p.sha256; Base = ($work | Where-Object { $_.Info.code -eq $p.baseVersionCode } | Select-Object -First 1).Path }
        })
        $verdicts = @()
        Measure-Stage 'hub: download + restore (parallel)' {
            $script:verdicts = @($downloads | ForEach-Object -ThrottleLimit $downloads.Count -Parallel {
                $d = $_
                . ([scriptblock]::Create($using:workerFunctions))
                $problem = $null
                $file = Join-Path $using:temp ('hub-' + [guid]::NewGuid().ToString('N'))
                try {
                    try { Invoke-WebRequest -Uri ($using:HubUrl + $d.Url) -OutFile $file -TimeoutSec 300 -MaximumRedirection 0 | Out-Null }
                    catch { throw "Hub cannot serve $($d.Url) (is the Hub with /app/artifacts deployed?): $($_.Exception.Message)" }
                    if ((Get-Item -LiteralPath $file).Length -ne $d.Size -or (Sha $file) -ne $d.Sha) { throw "Hub served wrong bytes for $($d.Url)" }
                    if ($d.Base) { Test-Roundtrip $using:java $using:classpath $using:temp $d.Base $file $using:sha $using:size }
                } catch { $problem = $_.Exception.Message }
                finally { Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue }
                [pscustomobject]@{ Url = $d.Url; Error = $problem }
            })
        }
        $bad = @($verdicts | Where-Object { $_.Error })
        if ($bad.Count -gt 0) { throw (($bad | ForEach-Object { $_.Error }) -join '; ') }
        Write-Host 'Hub serves the full APK and every patch; patches rebuild the exact APK from the served bytes.'
    }
    $currentLatestHash = if (Test-Path -LiteralPath $latestPath) { Hash $latestPath } else { $null }
    if ($latestHash -cne $currentLatestHash) { throw 'latest.json changed during release.' }
    $manifest = @{ versionCode=$code; versionName=$VersionName; notes=$Notes; url='/app/latest.apk'; file=$downloadName; size=$size; sha256=$sha; full=@{ url="/app/artifacts/$fullName"; size=$size; sha256=$sha }; patches=$patches }
    if ($sourceId) { $manifest.androidCommit = $sourceId }
    $manifestTemp = Join-Path $destination ('.latest-' + [guid]::NewGuid().ToString('N') + '.tmp')
    $manifestBackup = Join-Path $destination ('.previous-' + [guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::WriteAllText($manifestTemp, ($manifest | ConvertTo-Json -Depth 6), [Text.UTF8Encoding]::new($false))
        if (Test-Path -LiteralPath $latestPath) { [IO.File]::Replace($manifestTemp, $latestPath, $manifestBackup) } else { [IO.File]::Move($manifestTemp, $latestPath) }
    } finally {
        if (Test-Path -LiteralPath $manifestTemp) { Remove-Item -LiteralPath $manifestTemp -Force }
        if (Test-Path -LiteralPath $manifestBackup) { Remove-Item -LiteralPath $manifestBackup -Force }
    }
    if (-not $staging) { Check-Live $code $sha }
    Save-InfoCache
    # Artifacts of older releases stay a week for downloads that began before the switch.
    $keep = @($fullName) + @($patches | ForEach-Object { [IO.Path]::GetFileName($_.url) })
    $cutoff = (Get-Date).AddDays(-7)
    if (-not $publishExisting) {
        Get-ChildItem -LiteralPath $artifacts -File | Where-Object { $_.Name -match '^(apk|patch)-[0-9a-f]{64}\.(apk|bsdiff)$' -and $keep -notcontains $_.Name -and $_.LastWriteTime -lt $cutoff } | Remove-Item -Force
    }
    Write-Host "Release $VersionName ($code): $size bytes, $($patches.Count) patch(es); APK signature and SHA-256 verified. Destination: $destination"
} finally {
    Write-Timings
    $lock.Dispose()
    if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Recurse -Force }
}
