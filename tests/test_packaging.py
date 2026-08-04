"""Guards the sync hazards created by shipping payload as separate files.

Splitting install.sh into src/, systemd/ and config/ buys tooling and testability
but costs a class of bug the single-file version could not have: a file that
exists in the repo but that the installer never copies, a unit that is never
enabled, or shipped defaults that drift from the code's own defaults. Nothing
about any of those fails loudly at install time - they just quietly do less than
you think. So they get asserted here.
"""

import os
import re
import sys

from _harness import (CONFIG_SRC, Checks, HOOK_SRC, INSTALLER, REPO_ROOT,
                      UNIT_DIR, load_daemon, read_version)

w = load_daemon()
check = Checks()

with open(INSTALLER, errors="replace") as fh:
    installer = fh.read()

check.section("version is stated in exactly one place")
version = read_version()
check("VERSION file is non-empty", bool(version), True)
check("daemon constant matches VERSION file", w.VERSION, version)
check("install.sh reads VERSION rather than hardcoding it",
      'VERSION="$(cat "$VERSION_SRC"' in installer, True)
check("install.sh has no hardcoded version literal",
      re.search(r'^VERSION="\d+\.\d+\.\d+"', installer, re.M), None)

check.section("installer copies every shipped file")
match = re.search(r'^PAYLOAD="\n(.*?)^"\s*$', installer, re.S | re.M)
check("PAYLOAD block found", match is not None, True)

substitutions = {
    "$SRC_DIR": "src",
    "$UNIT_SRC_DIR": "systemd",
    "$CONFIG_SRC": "config/config.conf.default",
    "$VERSION_SRC": "VERSION",
}
declared = set()
for line in (match.group(1).splitlines() if match else []):
    line = line.strip()
    if not line:
        continue
    for key, value in substitutions.items():
        line = line.replace(key, value)
    declared.add(line.lstrip("/"))

shipped = {"VERSION", "config/config.conf.default"}
for directory in ("src", "systemd"):
    full = os.path.join(REPO_ROOT, directory)
    for name in sorted(os.listdir(full)):
        # Regular files only: __pycache__ and friends are build artifacts, not
        # things the installer should be copying to /etc.
        if name.startswith((".", "__")) or not os.path.isfile(os.path.join(full, name)):
            continue
        shipped.add("%s/%s" % (directory, name))

check("every declared payload file exists",
      sorted(p for p in declared if not os.path.exists(os.path.join(REPO_ROOT, p))), [])
check("no shipped file is missing from PAYLOAD", sorted(shipped - declared), [])
check("no PAYLOAD entry is stale", sorted(declared - shipped), [])

check.section("unit files and the SERVICES list agree")
services_match = re.search(r'^SERVICES="([^"]+)"', installer, re.M)
check("SERVICES list found", services_match is not None, True)
services = set(services_match.group(1).split()) if services_match else set()
unit_files = {n[:-len(".service")] for n in os.listdir(UNIT_DIR) if n.endswith(".service")}
check("every unit file is in SERVICES", sorted(unit_files - services), [])
check("every SERVICES entry has a unit file", sorted(services - unit_files), [])
check("all four services present", len(services), 4)

check.section("units point at what the installer actually installs")
installed_paths = {"/etc/cec-hdmi/cec-hook.sh", "/etc/cec-hdmi/cec-controller-watch.py"}
for name in sorted(unit_files):
    with open(os.path.join(UNIT_DIR, name + ".service"), errors="replace") as fh:
        unit = fh.read()
    check("%s has [Unit]/[Service]/[Install]" % name,
          all(s in unit for s in ("[Unit]", "[Service]", "[Install]")), True)
    execs = re.findall(r"^Exec(?:Start|Stop)=(\S+)", unit, re.M)
    check("%s has an ExecStart" % name, bool(execs), True)
    check("%s only runs installed scripts" % name,
          sorted(set(execs) - installed_paths), [])
    check("%s is enabled by a WantedBy" % name, "WantedBy=" in unit, True)

with open(os.path.join(UNIT_DIR, "cec-hdmi-controller.service"), errors="replace") as fh:
    controller = fh.read()
check("controller service is long-running, not oneshot", "Type=simple" in controller, True)
check("controller service restarts on failure", "Restart=on-failure" in controller, True)
check("controller service is wanted by multi-user.target",
      "WantedBy=multi-user.target" in controller, True)

check.section("shipped defaults match the code's own defaults")
values, problems = w.read_config(CONFIG_SRC)
check("config.conf.default parses cleanly", problems, [])
for key, expected in sorted(w.DEFAULTS.items()):
    check("%s matches DEFAULTS" % key, values.get(key), expected)

# The other half of the config is owned by cec-hook.sh, which carries its own
# fallbacks for the case where config.conf is missing or predates a key. Those
# live in a marked block so they can be read back and compared here - a hook
# default that silently disagrees with the shipped config means the same
# install behaves differently depending on how old its config.conf is.
with open(HOOK_SRC, errors="replace") as fh:
    hook = fh.read()

defaults_block = re.search(
    r"^# --- config defaults.*?$\n(.*?)^# --- end config defaults", hook, re.S | re.M)
check("cec-hook.sh has a marked config-defaults block", defaults_block is not None, True)

hook_defaults = {}
for line in (defaults_block.group(1).splitlines() if defaults_block else []):
    assignment = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line)
    if not assignment:
        continue
    value = assignment.group(2).strip()
    if value[:1] in ("'", '"'):
        value = value[1:value.find(value[0], 1)]
    hook_defaults[assignment.group(1)] = value

check("hook declares the CEC command keys",
      sorted(k for k in hook_defaults if k.startswith("CEC_")),
      ["CEC_AUDIO_COMMANDS", "CEC_COMMAND_DELAY", "CEC_STANDBY_COMMANDS",
       "CEC_WAKE_COMMANDS"])
for key, expected in sorted(hook_defaults.items()):
    check("%s matches cec-hook.sh's fallback" % key, values.get(key), expected)

declared_keys = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", open(CONFIG_SRC).read(), re.M))
known_keys = set(w.DEFAULTS) | set(hook_defaults)
check("config ships no key nothing reads", sorted(declared_keys - known_keys), [])
check("config ships every key the code knows", sorted(known_keys - declared_keys), [])

check.section("payload scripts are runnable as installed")
check("cec-hook.sh has a shebang", hook.startswith("#!"), True)
for action in ("on", "off", "dpcd-status"):
    check("cec-hook.sh handles '%s'" % action,
          re.search(r"^\s*%s\)" % re.escape(action), hook, re.M) is not None, True)
check("cec-hook.sh keeps the DPCD tunneling fix",
      "DPCD_CEC_TUNNELING_CONTROL=12289" in hook, True)
check("cec-hook.sh still rewrites the enable bit",
      "enable_cec_tunneling" in hook, True)
check("wake sends the configured list, not a hardcoded chain",
      'run_cec_commands "$CEC_WAKE_COMMANDS"' in hook, True)
check("standby sends the configured list",
      'run_cec_commands "$CEC_STANDBY_COMMANDS"' in hook, True)
check("the audio/AVR request runs only after a wake the TV acknowledged",
      re.search(r'log "Wake sequence sent OK[^\n]*\n\s*wake_audio_system', hook) is not None,
      True)

with open(os.path.join(REPO_ROOT, "src", "cec-controller-watch.py"), errors="replace") as fh:
    daemon = fh.read()
check("daemon has a python3 shebang", daemon.startswith("#!/usr/bin/env python3"), True)
check("daemon shells out to cec-hook.sh rather than speaking CEC",
      'HOOK_SCRIPT, "on"' in daemon, True)
check("daemon imports no third-party module",
      sorted(m for m in re.findall(r"^import (\w+)", daemon, re.M)
             if m not in sys.stdlib_module_names), [])

sys.exit(check.finish())
