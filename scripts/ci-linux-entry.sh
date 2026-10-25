#!/usr/bin/env bash
# Inside the scripts/ci --linux container: $1 — Python version, $2 — suite | py39.
set -euo pipefail
version="$1"; what="$2"
cp -r /src "$HOME/work" && cd "$HOME/work"
# A worktree: its .git names the repository where the checkout lives; here it is at /git.
if [ -n "${CI_GIT_COMMON:-}" ]; then
    printf 'gitdir: /git/%s\n' "$(sed -n "s|^gitdir: $CI_GIT_COMMON/||p" .git)" > .git
fi
# python3 of the run from root's folder: a link in the home would be the recorded interpreter, and
# collab trusts none that the user could change.
export PATH="/opt/pyv/$version:$PATH"
case "$what" in
    # The same parallel run as on the Mac, two workers.
    suite) TMPDIR="$(mktemp -d)" python3 scripts/ci_parallel.py --workers 2 ;;
    py39)
        # The part before the hand-over runs on macOS's 3.9: it must parse there, and install must start.
        python3 -c 'import ast, sys; ast.parse(open("collab").read(), feature_version=(3, 9)); print("parses on", sys.version.split()[0])'
        # Its whole output first, then three lines: «| head -3» closed the pipe on a longer one, and under
        # pipefail an install that went well was red.
        out="$(mktemp)"
        HOME="$(mktemp -d)" python3 collab install --dry-run --bin-dir "$(mktemp -d)" </dev/null >"$out" 2>&1 || { cat "$out"; exit 1; }
        head -3 "$out" ;;
esac
