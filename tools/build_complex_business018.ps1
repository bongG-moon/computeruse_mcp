param([string]$OutputDirectory = '')
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $repoRoot ('.data\complex-business018-' + [DateTime]::Now.ToString('yyyyMMdd-HHmmss'))
}
$destination = [IO.Path]::GetFullPath($OutputDirectory)
$workspacePrefix = [IO.Path]::GetFullPath($repoRoot).TrimEnd('\') + '\'
if (-not $destination.StartsWith($workspacePrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Fixture output must remain inside this repository.'
}
if (Test-Path -LiteralPath $destination) { throw 'Choose a fresh output directory; existing fixtures are not overwritten.' }
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler)) { throw 'The local .NET Framework C# compiler is required.' }
New-Item -ItemType Directory -Path $destination | Out-Null
$manifestPath = Join-Path $destination 'ComplexBusiness018.manifest'
$manifest = @'
<?xml version="1.0" encoding="utf-8"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
  <assemblyIdentity version="0.18.0.0" processorArchitecture="*" name="ComputerUse.ComplexBusiness018" type="win32"/>
  <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3"><security><requestedPrivileges>
    <requestedExecutionLevel level="asInvoker" uiAccess="false"/>
  </requestedPrivileges></security></trustInfo>
  <compatibility xmlns="urn:schemas-microsoft-com:compatibility.v1"><application>
    <supportedOS Id="{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}"/>
  </application></compatibility>
  <application xmlns="urn:schemas-microsoft-com:asm.v3"><windowsSettings>
    <dpiAware xmlns="http://schemas.microsoft.com/SMI/2005/WindowsSettings">true/pm</dpiAware>
    <dpiAwareness xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">PerMonitorV2</dpiAwareness>
  </windowsSettings></application>
</assembly>
'@
[IO.File]::WriteAllText($manifestPath, $manifest, [Text.UTF8Encoding]::new($false))
$executable = Join-Path $destination 'ComplexBusiness018.exe'
& $compiler /nologo /target:winexe /platform:x64 "/win32manifest:$manifestPath" "/out:$executable" /reference:System.Windows.Forms.dll /reference:System.Drawing.dll /reference:System.Web.Extensions.dll (Join-Path $repoRoot 'fixtures\ComplexBusiness018.cs')
if ($LASTEXITCODE -ne 0) { throw 'Complex fixture compilation failed.' }
$configuration = @'
<?xml version="1.0" encoding="utf-8"?>
<configuration><startup><supportedRuntime version="v4.0" sku=".NETFramework,Version=v4.8"/></startup></configuration>
'@
[IO.File]::WriteAllText($executable + '.config', $configuration, [Text.UTF8Encoding]::new($false))

# This mode exercises only the independent data model: it never creates a Form
# or controls, launches a desktop app, or sends desktop input.
$selfTestPath = Join-Path $destination 'model-self-test.json'
$selfTest = Start-Process -FilePath $executable -ArgumentList @('--self-test', ('"' + $selfTestPath + '"')) -WindowStyle Hidden -PassThru
if (-not $selfTest.WaitForExit(15000)) {
    $selfTest.Kill()
    throw 'Fixture model self-test timed out.'
}
$selfTest.Refresh()
if ($selfTest.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $selfTestPath)) { throw 'Fixture model self-test failed.' }
$testResult = Get-Content -LiteralPath $selfTestPath -Raw | ConvertFrom-Json
if ($testResult.ok -ne $true -or $testResult.gui_launched -ne $false) { throw 'Fixture model self-test returned an invalid result.' }
$report = [ordered]@{
    executable = $executable
    sha256 = (Get-FileHash -LiteralPath $executable -Algorithm SHA256).Hash.ToLowerInvariant()
    source_sha256 = (Get-FileHash -LiteralPath (Join-Path $repoRoot 'fixtures\ComplexBusiness018.cs') -Algorithm SHA256).Hash.ToLowerInvariant()
    fixture_revision = 'selection-model-sync-1'
    audit_example = (Join-Path $destination 'actual-input-audit.jsonl')
    launch_arguments = @('--audit', (Join-Path $destination 'actual-input-audit.jsonl'), '--layout-variant', '0')
    requested_execution_level = 'asInvoker'
    dpi_awareness = 'PerMonitorV2'
    model_tests = $testResult.tests
    gui_event_binding_tested = $false
    application_launched = $false
    desktop_input_sent = $false
    note = 'Synthetic WinForms fixture only. This fixes a fixture-only stale ComboBox model/audit defect; compilation and model tests do not establish MCP or GUI acceptance. It does not reproduce a particular corporate application or third-party control library.'
}
$report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $destination 'fixture-build.json') -Encoding UTF8
$report | ConvertTo-Json -Depth 5
