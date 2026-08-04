"""cec-hook.sh's configurable command runner.

The wake and standby sequences now come out of config.conf as ";"-separated
cec-ctl argument sets, which is real parsing logic living in shell. Rather than
grep the script for strings, this pulls the two functions straight out of
src/cec-hook.sh and runs them in bash with cec-ctl and sleep stubbed out, so
what is asserted here is the code that ships.

No root, no CEC hardware, nothing outside the shell it spawns.
"""

import re
import shutil
import subprocess
import sys

from _harness import Checks, HOOK_SRC

with open(HOOK_SRC, errors="replace") as fh:
    HOOK = fh.read()


def extract(name):
    """Lift one shell function out of cec-hook.sh, closing brace included."""
    match = re.search(r"^%s\(\) \{.*?^\}" % re.escape(name), HOOK, re.S | re.M)
    if not match:
        raise SystemExit("cec-hook.sh no longer defines %s()" % name)
    return match.group(0)


def hook_default(key):
    """The fallback value cec-hook.sh uses when config.conf does not set KEY."""
    block = re.search(r"^# --- config defaults.*?$\n(.*?)^# --- end config defaults",
                      HOOK, re.S | re.M)
    match = re.search(r"^%s=(.*)$" % re.escape(key), block.group(1) if block else "", re.M)
    if not match:
        raise SystemExit("cec-hook.sh has no default for %s" % key)
    value = match.group(1).strip()
    if value[:1] in ("'", '"'):
        value = value[1:value.find(value[0], 1)]
    return value


FUNCTIONS = extract("has_commands") + "\n\n" + extract("run_cec_commands")

# cec-ctl and sleep become shell functions, so the real ones are never reached
# and every invocation is recorded in order. FAIL_ON makes the Nth cec-ctl exit
# non-zero, which is how the stop-at-first-failure behaviour gets tested.
PREAMBLE = """
set -uo pipefail
CALLS=0
cec-ctl() {
    CALLS=$((CALLS + 1))
    echo "CALL $*"
    [ "$CALLS" = "${FAIL_ON:-0}" ] && return 3
    return 0
}
sleep() { echo "SLEEP $1"; }
"""


def run(list_value, phys="", delay=None, fail_on=None):
    """Run run_cec_commands over list_value; returns the recorded lines."""
    env = []
    if delay is not None:
        env.append("CEC_COMMAND_DELAY=%s" % shell_quote(delay))
    if fail_on is not None:
        env.append("FAIL_ON=%d" % fail_on)
    script = "".join([
        PREAMBLE,
        FUNCTIONS,
        "\n",
        "\n".join(env),
        "\nrun_cec_commands %s %s\n" % (shell_quote(list_value), shell_quote(phys)),
        'echo "STATUS $?"\n',
    ])
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    return done.stdout.strip().splitlines()


def shell_quote(value):
    return "'%s'" % str(value).replace("'", "'\\''")


def has_commands(value):
    script = PREAMBLE + FUNCTIONS + "\nhas_commands %s\n" % shell_quote(value)
    return subprocess.run(["bash", "-c", script], capture_output=True).returncode == 0


if shutil.which("bash") is None:
    print("bash not found - skipping cec-hook.sh behaviour tests")
    sys.exit(0)

check = Checks()

check.section("shipped wake sequence")
# Pins the four messages, their order and their arguments: these are exactly
# what the hardcoded chain sent before the sequence became configurable, and a
# TV that stopped waking after an upgrade would start right here.
check("default wake list runs the documented four commands",
      run(hook_default("CEC_WAKE_COMMANDS"), phys="3.0.0.0"),
      ["CALL -v -s -t0 --cec-version-1.4 --user-control-pressed=ui-cmd=power-on-function",
       "SLEEP 1",
       "CALL -v -s -t0 --cec-version-1.4 --image-view-on",
       "SLEEP 1",
       "CALL -v -s -t0 --cec-version-1.4 --set-stream-path=phys-addr=3.0.0.0",
       "SLEEP 1",
       "CALL -v -s --cec-version-1.4 --active-source=phys-addr=3.0.0.0",
       "STATUS 0"])
check("default standby list sends one directed standby",
      run(hook_default("CEC_STANDBY_COMMANDS")),
      ["CALL --to 0 --standby", "STATUS 0"])
check("verbose stays on (the NACK check greps cec-ctl's output)",
      "-v" in hook_default("CEC_WAKE_COMMANDS").split(), True)

check.section("list splitting")
check("single command, no delay before it",
      run("--to 0 --standby"), ["CALL --to 0 --standby", "STATUS 0"])
check("blank entries and stray separators are skipped",
      run("  ; --image-view-on ;; --standby ;  "),
      ["CALL --image-view-on", "SLEEP 1", "CALL --standby", "STATUS 0"])
check("arguments are not glob-expanded",
      run("--to 0 *"), ["CALL --to 0 *", "STATUS 0"])
check("every {phys_addr} in an entry is substituted",
      run("--set-stream-path=phys-addr={phys_addr} --x={phys_addr}", phys="1.0.0.0"),
      ["CALL --set-stream-path=phys-addr=1.0.0.0 --x=1.0.0.0", "STATUS 0"])
check("unsubstituted placeholder when no address is passed",
      run("--active-source=phys-addr={phys_addr}"),
      ["CALL --active-source=phys-addr=", "STATUS 0"])

check.section("delay")
check("CEC_COMMAND_DELAY is honoured",
      run("--a; --b", delay="0.5"), ["CALL --a", "SLEEP 0.5", "CALL --b", "STATUS 0"])
check("0 still runs both commands",
      run("--a; --b", delay=0), ["CALL --a", "SLEEP 0", "CALL --b", "STATUS 0"])
check("a non-numeric delay warns and falls back to 1",
      run("--a; --b", delay="banana"),
      ["WARNING: CEC_COMMAND_DELAY='banana' is not a number, using 1",
       "CALL --a", "SLEEP 1", "CALL --b", "STATUS 0"])

check.section("failure handling")
check("a failing command stops the sequence and keeps its exit code",
      run("--a; --b; --c", fail_on=2),
      ["CALL --a", "SLEEP 1", "CALL --b",
       "ERROR: 'cec-ctl --b' exited 3", "STATUS 3"])
check("an empty list is an error, not a silent success",
      run("  ;  "),
      ["ERROR: no cec-ctl commands configured (empty command list)", "STATUS 1"])

check.section("has_commands")
check("real command", has_commands("--standby"), True)
check("empty string", has_commands(""), False)
check("separators only", has_commands(" ; ;  "), False)

sys.exit(check.finish())
