param([string]$OutputDirectory = "")
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $repoRoot '.data\clickonce-fixture' }
$destination = [System.IO.Path]::GetFullPath($OutputDirectory)
$workspacePrefix = [System.IO.Path]::GetFullPath($repoRoot).TrimEnd('\') + '\'
if (-not $destination.StartsWith($workspacePrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Fixture output must remain inside this repository.'
}
if (Test-Path -LiteralPath $destination) { throw 'Choose a fresh output directory; existing fixtures are not overwritten.' }
$framework = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319'
$compiler = Join-Path $framework 'csc.exe'
$mage = Join-Path ${env:ProgramFiles(x86)} 'Microsoft SDKs\Windows\v10.0A\bin\NETFX 4.8 Tools\mage.exe'
if (-not (Test-Path -LiteralPath $compiler) -or -not (Test-Path -LiteralPath $mage)) {
    throw 'The .NET Framework compiler and Windows SDK Mage are required to build this developer fixture.'
}
$versionDirectory = Join-Path $destination '1.0.0.0'
New-Item -ItemType Directory -Path $versionDirectory | Out-Null
$executable = Join-Path $versionDirectory 'ComputerUseClickOnceFixture.exe'
# csc's default embedded Win32 manifest identifies MyApplication.app. ClickOnce
# prefers that identity over managed metadata and rejects the generated reference.
& $compiler /nologo /target:winexe /platform:anycpu /nowin32manifest "/out:$executable" /reference:System.Windows.Forms.dll /reference:System.Drawing.dll /reference:System.Deployment.dll (Join-Path $PSScriptRoot 'ClickOnceFixture.cs')
if ($LASTEXITCODE -ne 0) { throw 'Fixture compilation failed.' }
$configuration = @'
<?xml version="1.0" encoding="utf-8"?>
<configuration><startup><supportedRuntime version="v4.0" sku=".NETFramework,Version=v4.8"/></startup></configuration>
'@
[IO.File]::WriteAllText($executable + '.config', $configuration, [Text.UTF8Encoding]::new($false))

# Create an ephemeral signing identity exported ONLY to this workspace. Do not
# add it to Windows certificate stores or change ClickOnce trust policy.
$rsa = [System.Security.Cryptography.RSACryptoServiceProvider]::new(2048)
$rsa.PersistKeyInCsp = $false
$request = [System.Security.Cryptography.X509Certificates.CertificateRequest]::new(
    'CN=Computer Use Local Test Fixture', $rsa,
    [System.Security.Cryptography.HashAlgorithmName]::SHA256,
    [System.Security.Cryptography.RSASignaturePadding]::Pkcs1)
$certificate = $request.CreateSelfSigned([DateTimeOffset]::Now.AddDays(-1), [DateTimeOffset]::Now.AddDays(14))
$pfx = Join-Path $destination 'fixture-signing-test-only.pfx'
[IO.File]::WriteAllBytes($pfx, $certificate.Export([System.Security.Cryptography.X509Certificates.X509ContentType]::Pfx, 'local-test-only'))
$certificate.Dispose()
$rsa.Dispose()

$applicationManifest = $executable + '.manifest'
& $mage -New Application -ToFile $applicationManifest -Name 'ComputerUseClickOnceFixture' -Version 1.0.0.0 -Processor msil -FromDirectory $versionDirectory -TrustLevel FullTrust -Algorithm sha256RSA
if ($LASTEXITCODE -ne 0) { throw 'Application manifest generation failed.' }
& $mage -Sign $applicationManifest -CertFile $pfx -Password 'local-test-only' -Algorithm sha256RSA
if ($LASTEXITCODE -ne 0) { throw 'Application manifest signing failed.' }
$deployment = Join-Path $destination 'ComputerUseClickOnceFixture.application'
$deploymentUri = ([Uri]$deployment).AbsoluteUri
& $mage -New Deployment -ToFile $deployment -Name 'ComputerUseClickOnceFixture' -Version 1.0.0.0 -Processor msil -Install true -Publisher 'Computer Use Local Test' -ProviderURL $deploymentUri -AppManifest $applicationManifest -AppCodeBase '1.0.0.0/ComputerUseClickOnceFixture.exe.manifest' -Algorithm sha256RSA
if ($LASTEXITCODE -ne 0) { throw 'Deployment manifest generation failed.' }
& $mage -Sign $deployment -CertFile $pfx -Password 'local-test-only' -Algorithm sha256RSA
if ($LASTEXITCODE -ne 0) { throw 'Deployment manifest signing failed.' }
& $mage -Verify $applicationManifest
if ($LASTEXITCODE -ne 0) { throw 'Application manifest signature verification failed.' }
& $mage -Verify $deployment
if ($LASTEXITCODE -ne 0) { throw 'Deployment manifest signature verification failed.' }
$verifier = Join-Path $destination 'VerifyClickOnceFixture.exe'
& $compiler /nologo /target:exe "/out:$verifier" /reference:System.Xml.dll /reference:System.Deployment.dll (Join-Path $PSScriptRoot 'VerifyClickOnceFixture.cs')
if ($LASTEXITCODE -ne 0) { throw 'Fixture verifier compilation failed.' }
$identityReport = & $verifier $deployment
if ($LASTEXITCODE -ne 0) { throw ('Fixture identity/hash verification failed: ' + $identityReport) }
$identityReport | Set-Content -LiteralPath (Join-Path $destination 'identity-verification.txt') -Encoding UTF8

[xml]$manifest = [IO.File]::ReadAllText($deployment)
$identity = $manifest.assembly.assemblyIdentity
$referenceText = $deploymentUri + '#' + $identity.name + ', Culture=neutral, PublicKeyToken=' + $identity.publicKeyToken + ', processorArchitecture=' + $identity.processorArchitecture
$reference = Join-Path $destination 'Computer Use ClickOnce Fixture.appref-ms'
[IO.File]::WriteAllText($reference, $referenceText, [Text.Encoding]::Unicode)
$report = [ordered]@{ deployment = $deployment; reference = $reference; executable = $executable;
    signature_verified = $true; clickonce_identity_and_hashes_verified = $true; certificate_store_changed = $false; security_policy_changed = $false;
    application_installed = $false; application_launched = $false;
    note = 'Signed local ClickOnce package only. Open the .application through Windows to test installation; normal Windows trust prompts may appear. No external server is used.' }
$report | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $destination 'fixture-build.json') -Encoding UTF8
$report | ConvertTo-Json
