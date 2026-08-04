param(
    [Parameter()]
    [ValidateSet("node", "bun", "deno")]
    [string] $Runtime = "node"
)

$ErrorActionPreference = "Stop"

# ------------------------------------------------------------
# Project paths
# ------------------------------------------------------------

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $projectRoot

$url = "http://127.0.0.1:3000/json"

$outputDirectory = Join-Path `
    $projectRoot `
    "data\pilot\http\repeated"

$logDirectory = Join-Path `
    $projectRoot `
    "results\logs\http_pilot"

New-Item -ItemType Directory -Force -Path $outputDirectory |
    Out-Null

New-Item -ItemType Directory -Force -Path $logDirectory |
    Out-Null

# ------------------------------------------------------------
# Pilot configuration
# ------------------------------------------------------------

$runtimeName = $Runtime
$connections = 10
$warmupSeconds = 5
$measurementSeconds = 20
$timeoutSeconds = 10
$pipelining = 1
$repetition = 1

$runId = "{0}_c{1:D3}_r{2:D2}" -f `
    $runtimeName,
    $connections,
    $repetition

# ------------------------------------------------------------
# Runtime configuration
# ------------------------------------------------------------

switch ($runtimeName) {
    "node" {
        $runtimePath = (Get-Command "node" -ErrorAction Stop).Source

        $runtimeArguments = @(
            "benchmarks/http/node/server.js"
        )
    }

    "bun" {
        $runtimePath = (Get-Command "bun" -ErrorAction Stop).Source

        $runtimeArguments = @(
            "benchmarks/http/bun/server.js"
        )
    }

    "deno" {
        $runtimePath = (Get-Command "deno" -ErrorAction Stop).Source

        $runtimeArguments = @(
            "run",
            "--allow-net=127.0.0.1:3000",
            "benchmarks/http/deno/server.js"
        )
    }
}

# ------------------------------------------------------------
# Output and log files
# ------------------------------------------------------------

$resultPath = Join-Path `
    $outputDirectory `
    "$runId.json"

$serverOutputLog = Join-Path `
    $logDirectory `
    "$runId-server-output.log"

$serverErrorLog = Join-Path `
    $logDirectory `
    "$runId-server-error.log"

$warmupErrorLog = Join-Path `
    $logDirectory `
    "$runId-warmup-error.log"

$measurementErrorLog = Join-Path `
    $logDirectory `
    "$runId-autocannon-error.log"

$filesToRemove = @(
    $resultPath,
    $serverOutputLog,
    $serverErrorLog,
    $warmupErrorLog,
    $measurementErrorLog
)

foreach ($file in $filesToRemove) {
    Remove-Item `
        -Path $file `
        -Force `
        -ErrorAction SilentlyContinue
}

# ------------------------------------------------------------
# Locate Autocannon
# ------------------------------------------------------------

$autocannonPath = Join-Path `
    $projectRoot `
    "node_modules\.bin\autocannon.cmd"

if (-not (Test-Path $autocannonPath)) {
    throw @"
The local Autocannon executable was not found:

$autocannonPath

Run 'npm install' from the project root.
"@
}

# ------------------------------------------------------------
# Server readiness function
# ------------------------------------------------------------

function Wait-ForServer {
    param(
        [Parameter(Mandatory)]
        [string] $Url,

        [int] $MaximumWaitSeconds = 15
    )

    $deadline = (Get-Date).AddSeconds($MaximumWaitSeconds)

    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest `
                -Uri $Url `
                -Method Get `
                -TimeoutSec 1 `
                -UseBasicParsing

            if ($response.StatusCode -eq 200) {
                return $true
            }
        }
        catch {
            # Server may still be starting.
        }

        Start-Sleep -Milliseconds 250
    }

    return $false
}

# ------------------------------------------------------------
# Run the pilot observation
# ------------------------------------------------------------

$serverProcess = $null

try {
    Write-Host ""
    Write-Host "HTTP pilot observation: $runId"
    Write-Host "Starting the $runtimeName HTTP server..."

    $serverProcess = Start-Process `
        -FilePath $runtimePath `
        -ArgumentList $runtimeArguments `
        -WorkingDirectory $projectRoot `
        -RedirectStandardOutput $serverOutputLog `
        -RedirectStandardError $serverErrorLog `
        -PassThru `
        -WindowStyle Hidden

    if (-not (Wait-ForServer -Url $url)) {
        $serverDetails = Get-Content `
            -Path $serverErrorLog `
            -Raw `
            -ErrorAction SilentlyContinue

        throw @"
The $runtimeName server did not become ready within 15 seconds.

$serverDetails
"@
    }

    if ($serverProcess.HasExited) {
        throw "The $runtimeName server exited before the benchmark."
    }

    Write-Host "Server ready."
    Write-Host "Running $warmupSeconds-second warm-up..."

    $originalErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"

    try {
        & $autocannonPath `
            --connections $connections `
            --duration $warmupSeconds `
            --pipelining $pipelining `
            --timeout $timeoutSeconds `
            --no-progress `
            $url `
            1> $null `
            2> $warmupErrorLog

        $warmupExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $originalErrorActionPreference
    }

    if ($warmupExitCode -ne 0) {
        $warmupDetails = Get-Content `
            -Path $warmupErrorLog `
            -Raw `
            -ErrorAction SilentlyContinue

        throw @"
The Autocannon warm-up failed with exit code $warmupExitCode.

$warmupDetails
"@
    }

    if ($serverProcess.HasExited) {
        throw "The $runtimeName server exited during warm-up."
    }

    Write-Host "Warm-up completed."
    Write-Host "Running $measurementSeconds-second measured test..."

    $originalErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"

    try {
        $jsonOutput = & $autocannonPath `
            --connections $connections `
            --duration $measurementSeconds `
            --pipelining $pipelining `
            --timeout $timeoutSeconds `
            --json `
            --no-progress `
            $url `
            2> $measurementErrorLog

        $measurementExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $originalErrorActionPreference
    }

    if ($measurementExitCode -ne 0) {
        $measurementDetails = Get-Content `
            -Path $measurementErrorLog `
            -Raw `
            -ErrorAction SilentlyContinue

        throw @"
The measured Autocannon run failed with exit code $measurementExitCode.

$measurementDetails
"@
    }

    $jsonText = $jsonOutput -join [Environment]::NewLine

    if ([string]::IsNullOrWhiteSpace($jsonText)) {
        throw "Autocannon returned an empty JSON result."
    }

    try {
        $parsedResult = $jsonText | ConvertFrom-Json
    }
    catch {
        throw "Autocannon produced invalid JSON: $($_.Exception.Message)"
    }

    if ($parsedResult.connections -ne $connections) {
        throw "The result contains an unexpected connection count."
    }

    $utf8WithoutBom = [System.Text.UTF8Encoding]::new($false)

    [System.IO.File]::WriteAllText(
        $resultPath,
        $jsonText,
        $utf8WithoutBom
    )

    Write-Host ""
    Write-Host "Pilot observation completed successfully."
    Write-Host "Result saved to:"
    Write-Host $resultPath

    Write-Host ""
    Write-Host "Result summary:"
    Write-Host "Runtime:            $runtimeName"
    Write-Host "Connections:        $($parsedResult.connections)"
    Write-Host "Duration:           $($parsedResult.duration) seconds"
    Write-Host "Requests/second:    $($parsedResult.requests.average)"
    Write-Host "Mean latency:       $($parsedResult.latency.mean) ms"
    Write-Host "Median latency:     $($parsedResult.latency.p50) ms"
    Write-Host "P99 latency:        $($parsedResult.latency.p99) ms"
    Write-Host "Total requests:     $($parsedResult.requests.total)"
    Write-Host "Errors:             $($parsedResult.errors)"
    Write-Host "Timeouts:           $($parsedResult.timeouts)"
    Write-Host "Non-2xx responses: $($parsedResult.non2xx)"
}
catch {
    Write-Error "HTTP pilot observation failed: $($_.Exception.Message)"
}
finally {
    if ($serverProcess -and -not $serverProcess.HasExited) {
        Write-Host ""
        Write-Host "Stopping the $runtimeName server..."

        Stop-Process `
            -Id $serverProcess.Id `
            -Force `
            -ErrorAction SilentlyContinue

        Wait-Process `
            -Id $serverProcess.Id `
            -ErrorAction SilentlyContinue
    }
}