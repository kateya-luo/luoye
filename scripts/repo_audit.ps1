[CmdletBinding()]
param(
    [string]$Root = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'
$rootPath = (Resolve-Path -LiteralPath $Root).Path
$failures = [System.Collections.Generic.List[string]]::new()

function Get-RepoRelativePath {
    param([Parameter(Mandatory = $true)][string]$Path)
    return $Path.Substring($rootPath.Length).TrimStart([char[]]'\/')
}

$gitDirectory = Join-Path $rootPath '.git'
if (Test-Path -LiteralPath $gitDirectory) {
    $candidatePaths = & git -c core.quotepath=false -C $rootPath ls-files --cached --others --exclude-standard
    if ($LASTEXITCODE -ne 0) {
        throw 'git ls-files failed'
    }
    $files = @($candidatePaths | ForEach-Object {
        Get-Item -LiteralPath (Join-Path $rootPath $_)
    } | Where-Object { -not $_.PSIsContainer })
}
else {
    $files = Get-ChildItem -LiteralPath $rootPath -Recurse -Force -File | Where-Object {
        $_.FullName -notmatch '[\\/]\.git([\\/]|$)' -and
        $_.FullName -notmatch '[\\/]\.github-release-assets([\\/]|$)'
    }
}

$forbiddenExtensions = @(
    '.db', '.sqlite', '.sqlite3', '.pcm', '.wav', '.mp3', '.m4a',
    '.pem', '.key', '.p12', '.pfx'
)
$textExtensions = @(
    '.c', '.h', '.cpp', '.hpp', '.py', '.js', '.jsx', '.ts', '.tsx',
    '.json', '.md', '.txt', '.yml', '.yaml', '.toml', '.ini', '.cfg',
    '.conf', '.env', '.example', '.sh', '.ps1', '.bat', '.html', '.css',
    '.xml', '.csv'
)

$privateKeyPattern = 'BEGIN ' + '(RSA |EC |OPENSSH )?PRIVATE KEY'
$githubTokenPattern = 'gh' + '[pousr]_[A-Za-z0-9]{20,}'
$awsAccessPattern = 'AK' + 'IA[0-9A-Z]{16}'
$providerKeyPattern = 'sk-' + '[A-Za-z0-9_-]{24,}'
$secretPatterns = @(
    @{ Name = 'private-key'; Pattern = $privateKeyPattern },
    @{ Name = 'github-token'; Pattern = $githubTokenPattern },
    @{ Name = 'aws-access-key'; Pattern = $awsAccessPattern },
    @{ Name = 'provider-api-key'; Pattern = $providerKeyPattern }
)

$totalBytes = [int64]0
$largest = $null

foreach ($file in $files) {
    $relative = Get-RepoRelativePath -Path $file.FullName
    $parts = $relative -split '[\\/]'
    $totalBytes += $file.Length
    if ($null -eq $largest -or $file.Length -gt $largest.Length) {
        $largest = $file
    }

    foreach ($part in $parts) {
        if ($part -in @('node_modules', 'data', 'dist', '__pycache__', '.pytest_cache', 'managed_components')) {
            $failures.Add("generated-directory: $relative")
            break
        }
        if ($part -match '^build(-.*)?$') {
            $failures.Add("build-directory: $relative")
            break
        }
    }

    if ($file.Name -eq '.env' -or $file.Name -like '.env.*' -and $file.Name -ne '.env.example') {
        $failures.Add("local-environment: $relative")
    }
    if ($forbiddenExtensions -contains $file.Extension.ToLowerInvariant() -or $file.Name -match '\.(db|sqlite)(-|$)') {
        $failures.Add("runtime-or-secret-file: $relative")
    }
    if ($file.Length -gt 95MB) {
        $failures.Add("oversized-file: $relative")
    }

    $extension = $file.Extension.ToLowerInvariant()
    if ($file.Length -le 2MB -and ($textExtensions -contains $extension -or $file.Name -in @('Dockerfile', 'Makefile', 'CMakeLists.txt'))) {
        try {
            $content = Get-Content -LiteralPath $file.FullName -Raw
            foreach ($entry in $secretPatterns) {
                if ($content -match $entry.Pattern) {
                    $failures.Add("possible-$($entry.Name): $relative")
                }
            }
        }
        catch {
            $failures.Add("unreadable-text-file: $relative")
        }
    }
}

$uniqueFailures = $failures | Sort-Object -Unique
if ($uniqueFailures.Count -gt 0) {
    Write-Host 'Repository audit FAILED:' -ForegroundColor Red
    $uniqueFailures | ForEach-Object { Write-Host "  $_" }
    exit 1
}

$largestRelative = if ($null -ne $largest) {
    Get-RepoRelativePath -Path $largest.FullName
} else {
    '(none)'
}

Write-Host 'Repository audit PASS' -ForegroundColor Green
Write-Host ("Files: {0}" -f $files.Count)
Write-Host ("Tracked candidate size: {0:N2} MiB" -f ($totalBytes / 1MB))
Write-Host ("Largest file: {0} ({1:N2} MiB)" -f $largestRelative, $(if ($largest) { $largest.Length / 1MB } else { 0 }))
