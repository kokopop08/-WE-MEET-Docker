# WE-MEET API 문서 생성 (pdoc). 프로젝트 루트에서 실행.
#   pip install -e ".[docs]"   # pdoc 설치
#   docs/build.ps1
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
python -m pdoc wemeet -o docs/api $args
Write-Host "생성 완료 -> docs/api/index.html"
