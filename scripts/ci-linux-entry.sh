#!/usr/bin/env bash
# Inside the scripts/ci --linux container: $1 — Python version, $2 — suite | py39.
set -euo pipefail
version="$1"; what="$2"
cp -r /src "$HOME/work" && cd "$HOME/work"
# python3 of the run from root's folder: a link in the home would be the recorded interpreter, and
# collab trusts none that the user could change.
export PATH="/opt/pyv/$version:$PATH"
case "$what" in
    # The same parallel run as on the Mac, two workers.
    suite) TMPDIR="$(mktemp -d)" python3 scripts/ci_parallel.py --workers 2 ;;
    py39)
        # The part before the hand-over runs on macOS's 3.9: it must parse there, and install must start.
        python3 -c 'import ast, sys; ast.parse(open("collab").read(), feature_version=(3, 9)); print("parses on", sys.version.split()[0])'
        HOME="$(mktemp -d)" python3 collab install --dry-run --bin-dir "$(mktemp -d)" </dev/null | head -3 ;;
esac
