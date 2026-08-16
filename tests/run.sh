#!/usr/bin/env bash
# Runs every test suite, plus syntax checks on everything that ships.
# No root, no install, nothing outside a temp directory is touched.
#
#   bash tests/run.sh          quiet: one line per suite
#   bash tests/run.sh -v       verbose: full output from every suite

set -uo pipefail

TESTS_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$TESTS_DIR")"

VERBOSE=0
[ "${1:-}" = "-v" ] || [ "${1:-}" = "--verbose" ] && VERBOSE=1

PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then
            PYTHON="$candidate"
            break
        fi
    done
fi
if [ -z "$PYTHON" ]; then
    echo "ERROR: no python3 found" >&2
    exit 1
fi

failed=0
total=0

run() {
    local label="$1"
    shift
    total=$((total + 1))
    printf '%-38s ' "$label"
    local output status
    output="$("$@" 2>&1)"
    status=$?
    if [ "$status" -eq 0 ]; then
        # Suites end with "N checks passed"; surface the count. Syntax checks
        # are silent on success, so there is nothing to append for those.
        if [ -n "$output" ]; then
            printf 'PASS  %s\n' "$(printf '%s' "$output" | tail -n 1)"
        else
            printf 'PASS\n'
        fi
        [ "$VERBOSE" -eq 1 ] && [ -n "$output" ] && printf '%s\n' "$output" | sed 's/^/    /'
    else
        printf 'FAIL\n'
        printf '%s\n' "$output" | sed 's/^/    /'
        failed=$((failed + 1))
    fi
    return 0
}

echo "python: $("$PYTHON" --version 2>&1)  ($PYTHON)"
echo

# -B keeps __pycache__ out of the repo (the harness sets dont_write_bytecode,
# but only after Python has already cached the import of the harness itself).
for suite in "$TESTS_DIR"/test_*.py; do
    run "$(basename "$suite")" "$PYTHON" -B "$suite"
done

run "install.sh syntax" bash -n "$REPO_ROOT/install.sh"

# compile() rather than py_compile: the same syntax check, but it does not leave
# a __pycache__ directory sitting in src/.
for source in "$REPO_ROOT"/src/*.py; do
    run "$(basename "$source") compiles" "$PYTHON" -B -c \
        'import sys; compile(open(sys.argv[1], encoding="utf-8").read(), sys.argv[1], "exec")' \
        "$source"
done

# The config is no longer sourced by bash, so what matters is that the shipped
# defaults parse with the project's own parser and produce a usable Config.
run "config.conf.default is loadable" "$PYTHON" -B -c '
import sys
sys.path.insert(0, sys.argv[1])
import cec_config
config = cec_config.Config(sys.argv[2])
assert not config.problems, config.problems
assert config.osd_name and config.device and config.wake_attempts >= 1
assert config.cooldown >= 0 and config.log_keep >= 0
' "$REPO_ROOT/src" "$REPO_ROOT/config/config.conf.default"

echo
if [ "$failed" -ne 0 ]; then
    echo "$failed of $total FAILED"
    exit 1
fi
echo "all $total checks passed"
