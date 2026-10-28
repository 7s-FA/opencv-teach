$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
$env:DOTNET_CLI_TELEMETRY_OPTOUT = '1'
dotnet build SO101.WpfConsole.sln -c Release --nologo
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$env:SO101_CONTRACT_OUTPUT = Join-Path (Get-Location) 'contracts/examples'
dotnet test tests/So101.Console.Tests/So101.Console.Tests.csproj -c Release --no-build --nologo
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
dotnet publish src/So101.Console.Wpf/So101.Console.Wpf.csproj -c Release -r win-x64 --self-contained true -o artifacts/win-x64 --nologo
exit $LASTEXITCODE
