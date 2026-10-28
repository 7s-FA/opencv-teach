#!/usr/bin/env bash
set -Eeuo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
export DOTNET_CLI_TELEMETRY_OPTOUT=1
SO101_DOTNET="${SO101_DOTNET:-$PWD/.tools/dotnet/dotnet}"
if [[ ! -x "$SO101_DOTNET" ]]; then SO101_DOTNET="$(command -v dotnet)"; fi
"$SO101_DOTNET" build SO101.WpfConsole.sln -c Release --nologo
SO101_CONTRACT_OUTPUT="$PWD/contracts/examples" "$SO101_DOTNET" test tests/So101.Console.Tests/So101.Console.Tests.csproj -c Release --no-build --nologo --logger 'trx;LogFileName=demo-tests.trx' --results-directory verification/test-results
"$SO101_DOTNET" publish src/So101.Console.Wpf/So101.Console.Wpf.csproj -c Release -r win-x64 --self-contained true -o artifacts/win-x64 --nologo
