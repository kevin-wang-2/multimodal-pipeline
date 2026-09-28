param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-f]{40}$')]
    [string]$ExpectedSha,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^https://')]
    [string]$RepositoryUrl,

    [Parameter(Mandatory = $true)]
    [string]$Token
)

$ErrorActionPreference = 'Stop'
$Repo = 'C:\mmp\repo'
$Python = 'C:\ProgramData\miniconda3\envs\mmp-node\python.exe'
$Task = 'mmp-node'
$HealthUri = 'http://127.0.0.1:8765/health'
$GitPrefix = @('-c', 'safe.directory=C:/mmp/repo', '-C', $Repo)

function Invoke-Git {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    & git @GitPrefix @Arguments
    if ($LASTEXITCODE -ne 0) { throw "git $Arguments failed with exit code $LASTEXITCODE" }
}

function Invoke-AuthenticatedGit {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    & git @GitPrefix -c "http.extraHeader=Authorization: token $Token" @Arguments
    if ($LASTEXITCODE -ne 0) { throw "authenticated git operation failed with exit code $LASTEXITCODE" }
}

function Install-Node {
    & $Python -m pip install -q -e "$Repo\node" --no-warn-script-location
    if ($LASTEXITCODE -ne 0) { throw "pip install failed with exit code $LASTEXITCODE" }
}

function Restart-Node {
    & schtasks.exe /End /TN $Task 2>$null | Out-Null
    Get-CimInstance Win32_Process |
        Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match '(mmp_broker\.main|-m engines\.)' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    & schtasks.exe /Run /TN $Task | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "failed to start scheduled task $Task" }
}

function Wait-Healthy {
    $deadline = (Get-Date).AddSeconds(90)
    do {
        Start-Sleep -Seconds 3
        try {
            $health = Invoke-RestMethod -Uri $HealthUri -TimeoutSec 5
            if ($health.status -eq 'ok') { return }
        } catch {
            Write-Host "waiting for $HealthUri"
        }
    } while ((Get-Date) -lt $deadline)
    throw "mmp-node did not become healthy within 90 seconds"
}

if (-not (Test-Path "$Repo\.git")) { throw "$Repo is not a git checkout" }
if ((Invoke-Git status --porcelain).Count -ne 0) { throw "$Repo has local changes; refusing deployment" }

$PreviousSha = (Invoke-Git rev-parse HEAD | Select-Object -Last 1).Trim()
Invoke-AuthenticatedGit fetch --quiet $RepositoryUrl 'main:refs/remotes/origin/main'
$RemoteSha = (Invoke-Git rev-parse origin/main | Select-Object -Last 1).Trim()
if ($RemoteSha -ne $ExpectedSha) { throw "origin/main is $RemoteSha, expected $ExpectedSha" }
Invoke-Git merge-base --is-ancestor $PreviousSha $ExpectedSha

try {
    Invoke-Git checkout --quiet main
    Invoke-Git merge --ff-only $ExpectedSha
    Install-Node
    Restart-Node
    Wait-Healthy
    Write-Host "deployed $ExpectedSha (previous $PreviousSha)"
} catch {
    $DeployError = $_
    Write-Warning "deployment failed; rolling back to $PreviousSha"
    Invoke-Git reset --hard $PreviousSha
    Install-Node
    Restart-Node
    Wait-Healthy
    throw $DeployError
}
