#!/usr/bin/env bash
# WE-MEET API 문서 생성 (pdoc). 프로젝트 루트에서 실행.
#   pip install -e ".[docs]"   # pdoc 설치
#   bash docs/build.sh
set -e
cd "$(dirname "$0")/.."
python -m pdoc wemeet -o docs/api "$@"
echo "생성 완료 -> docs/api/index.html"
