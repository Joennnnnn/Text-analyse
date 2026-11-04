#!/bin/sh
# 自动挑选解释器：优先仓库内 .venv，其次 python3，最后 python
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export PYTHONPATH="$DIR${PYTHONPATH:+:$PYTHONPATH}"
if [ -x "$DIR/.venv/bin/python" ]; then
  exec "$DIR/.venv/bin/python" -m chipdoc "$@"
elif command -v python3 >/dev/null 2>&1; then
  exec python3 -m chipdoc "$@"
elif command -v python >/dev/null 2>&1; then
  exec python -m chipdoc "$@"
else
  echo "找不到 python3 / python，请先安装 Python 3.11+" >&2
  exit 127
fi
