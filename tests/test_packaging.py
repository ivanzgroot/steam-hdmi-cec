"""Guards the sync hazards created by shipping the payload as separate files.

Splitting the project into src/, systemd/ and config/ buys tooling and
testability but costs a class of bug a single file could not have: a module that
exists in the repo but that the installer never copies, a unit that is never
enabled, a shipped config that has drifted from the code's defaults. None of
those fail loudly at install time - they just quietly do less than you think.

Also guarded here: the handful of invariants that make this project work at all,
asserted against the source text so that a refactor cannot quietly drop one.
"""

import os
import re
import sys

from _harness import (Checks, CONFIG_SRC, HOOK_SRC, INSTALLER, REPO_ROOT,
                      SRC_DIR, UNIT_DIR, WATCH_SRC, load_watcher, read_version)

import cec_config
import cec_dpcd

check = Checks()

with open(INSTALLER, errors="replace") as handle:
    installer = handle.read()


def source_of(name):
    with open(os.path.join(SRC_DIR, name), errors="replace") as handle:
        return handle.read()


PYTHON_FILES = sorted(name for name in os.listdir(SRC_DIR) if name.endswith(".py"))
SOURCES = {name: source_of(name) for name in PYTHON_FILES}

check.section("the version is stated in one place")
version = read_version()
check("the VERSION file is non-empty", bool(version), True)
check("cec-hook agrees", re.search(r'^VERSION = "([^"]+)"', SOURCES["cec-hook.py"], re.M).group(1),
      version)
check("cec-watch agrees", re.search(r'^VERSION = "([^"]+)"', SOURCES["cec-watch.py"], re.M).group(1),
      version)
check("install.sh reads VERSION rather than hardcoding it",
      'VERSION="$(cat "$VERSION_SRC"' in installer, True)
check("install.sh has no hardcoded version literal",
      re.search(r'^VERSION="\d+\.\d+\.\d+"', installer, re.M), None)

check.section("the installer copies every shipped file")
match = re.search(r'^PAYLOAD="\n(.*?)^"\s*$', installer, re.S | re.M)
check("the PAYLOAD block was found", match is not None, True)

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

check.section("entry points and modules are distinguished")
entry_match = re.search(r'^ENTRY_POINTS="([^"]+)"', installer, re.M)
check("the ENTRY_POINTS list was found", entry_match is not None, True)
entry_points = set(entry_match.group(1).split()) if entry_match else set()
# A hyphen is not valid in a Python identifier, so a hyphenated name can only
# ever be run, never imported - which makes it exactly the set of entry points.
hyphenated = {name for name in PYTHON_FILES if "-" in name}
check("every hyphenated file is an entry point", sorted(hyphenated - entry_points), [])
check("every entry point is a real file", sorted(entry_points - set(PYTHON_FILES)), [])
check("entry points have a python3 shebang",
      sorted(name for name in entry_points
             if not SOURCES[name].startswith("#!/usr/bin/env python3")), [])
check("importable modules have no shebang",
      sorted(name for name in PYTHON_FILES
             if name not in entry_points and SOURCES[name].startswith("#!")), [])

check.section("unit files and the SERVICES list agree")
services_match = re.search(r'^SERVICES="([^"]+)"', installer, re.M)
check("the SERVICES list was found", services_match is not None, True)
services = set(services_match.group(1).split()) if services_match else set()
unit_files = {n[:-len(".service")] for n in os.listdir(UNIT_DIR) if n.endswith(".service")}
check("every unit file is in SERVICES", sorted(unit_files - services), [])
check("every SERVICES entry has a unit file", sorted(services - unit_files), [])
check("all four services are present", len(services), 4)

check.section("units point at what the installer actually installs")
installed_paths = {"/etc/cec-hdmi/" + name for name in entry_points}
for name in sorted(unit_files):
    with open(os.path.join(UNIT_DIR, name + ".service"), errors="replace") as handle:
        unit = handle.read()
    check("%s has [Unit]/[Service]/[Install]" % name,
          all(section in unit for section in ("[Unit]", "[Service]", "[Install]")), True)
    execs = re.findall(r"^Exec(?:Start|Stop)=(\S+)", unit, re.M)
    check("%s has an ExecStart" % name, bool(execs), True)
    check("%s only runs installed entry points" % name,
          sorted(set(execs) - installed_paths), [])
    check("%s is enabled by a WantedBy" % name, "WantedBy=" in unit, True)

with open(os.path.join(UNIT_DIR, "cec-hdmi-controller.service"), errors="replace") as handle:
    controller = handle.read()
check("the controller service is long-running, not oneshot",
      "Type=simple" in controller, True)
check("the controller service restarts on failure",
      "Restart=on-failure" in controller, True)
check("the controller service is wanted by multi-user.target",
      "WantedBy=multi-user.target" in controller, True)

# The standby path blocks the suspend, so its budget must stay small - and the
# wake paths must stay generous enough for the retry escalation to finish.
with open(os.path.join(UNIT_DIR, "cec-hdmi-sleep.service"), errors="replace") as handle:
    sleep_unit = handle.read()
check("the sleep unit keeps a short timeout",
      int(re.search(r"TimeoutStartSec=(\d+)", sleep_unit).group(1)) <= 20, True)
with open(os.path.join(UNIT_DIR, "cec-hdmi-resume.service"), errors="replace") as handle:
    resume_unit = handle.read()
check("the resume unit allows time for the escalation",
      int(re.search(r"TimeoutStartSec=(\d+)", resume_unit).group(1)) >= 120, True)

check.section("the shipped config matches the code's defaults")
values, problems = cec_config.read_config(CONFIG_SRC)
check("config.conf.default parses cleanly", problems, [])
for key, expected in sorted(cec_config.DEFAULTS.items()):
    check("%s matches DEFAULTS" % key, values.get(key), expected)

declared_keys = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", open(CONFIG_SRC).read(), re.M))
check("the config ships no key nothing reads",
      sorted(declared_keys - set(cec_config.DEFAULTS)), [])
check("the config ships every key the code knows",
      sorted(set(cec_config.DEFAULTS) - declared_keys), [])

check.section("no third-party dependency has crept in")
# The entire premise of the install: system python3 and nothing else. SteamOS
# has a read-only root, no compiler and an unpopulated pacman keyring, so a pip
# dependency is the single most likely thing to break a one-command install.
for name, text in sorted(SOURCES.items()):
    imported = set(re.findall(r"^import (\w+)", text, re.M))
    imported |= set(re.findall(r"^from (\w+) import", text, re.M))
    local = {stem[:-3] if stem.endswith(".py") else stem for stem in PYTHON_FILES}
    local |= {n.replace("-", "_") for n in local}
    third_party = sorted(m for m in imported
                         if m not in sys.stdlib_module_names and m not in local)
    check("%s imports only the standard library" % name, third_party, [])

# Mentioning the old tooling in a comment is fine and often useful; executing it
# is not. So look for the string as an argument - quoted - rather than anywhere
# in the file, and for the installer's dependency check rather than the word.
check("no module executes cec-ctl",
      sorted(name for name, text in SOURCES.items()
             if '"cec-ctl"' in text or "'cec-ctl'" in text), [])
check("the installer does not require cec-ctl",
      "command -v cec-ctl" in installer, False)
check("the installer still requires python3",
      "command -v python3" in installer, True)

check.section("the DisplayPort tunneling fix is intact")
dpcd = SOURCES["cec_dpcd.py"]
check("the register address is the DisplayPort one",
      cec_dpcd.DPCD_CEC_TUNNELING_CONTROL, 0x3001)
check("the enable bit is still written", "write_tunneling" in dpcd, True)
check("the connector re-probe is still available", "reprobe_connector" in dpcd, True)

control = SOURCES["cec_control.py"]
# The ordering invariant, asserted on the source itself as well as on behaviour
# in test_plan.py: claiming a logical address clears 0x3001, so the enable must
# come after the claim. Reversing these two lines produces a wake that works
# from a cold boot and NACKs after every resume.
claim = re.search(r"def _claim_address\(self\):(.*?)\n    # ", control, re.S)
check("_claim_address was found", claim is not None, True)
body = claim.group(1) if claim else ""
check("it configures the adapter", "self.device.configure(" in body, True)
check("it enables tunneling", "self.dpcd.ensure_enabled()" in body, True)
check("and it enables tunneling AFTER configuring",
      body.index("self.dpcd.ensure_enabled()") > body.index("self.device.configure("),
      True)

check.section("only cec-hook speaks CEC")
watcher = SOURCES["cec-watch.py"]
check("the watcher does not import the frame layer",
      "cec_frames" in watcher or "cec_device" in watcher, False)
check("the watcher shells out to cec-hook instead",
      'HOOK_SCRIPT, "on"' in watcher, True)
check("cec-hook is the only entry point that opens the adapter",
      "Controller(" in SOURCES["cec-hook.py"], True)

check.section("the hook still handles every verb")
hook = SOURCES["cec-hook.py"]
check("the four actions are declared",
      re.search(r'choices=\("on", "off", "status", "scan"\)', hook) is not None, True)
for action in ("on", "off", "status", "scan"):
    check("cec-hook handles '%s'" % action, '"%s"' % action in hook, True)
check("--dry-run is available", "--dry-run" in hook, True)

check.section("no prose pasted into the sources")
# A real incident on the previous version: assistant prose got pasted into
# cec-hook.sh mid-line, turning
#     printf '%s' "$pa"
# into
#     printf '%s' "$pa"If you want, I can also give you a minimal test ...
# which is *valid shell* - printf reuses "%s" for every extra argument - so
# `bash -n` passed and the corruption reached the TV as a malformed physical
# address. Syntax checking cannot catch this, so look for the shape of it.
PROSE = re.compile(
    r"\b(?:if you want|I can also give you|let me know if|here'?s (?:a|the) "
    r"(?:version|updated)|would you like me to)\b", re.I)

with open(CONFIG_SRC, errors="replace") as handle:
    config_text = handle.read()
for label, text in sorted(SOURCES.items()) + [("config.conf.default", config_text),
                                              ("install.sh", installer)]:
    hits = [(n, line.strip()[:90])
            for n, line in enumerate(text.splitlines(), 1) if PROSE.search(line)]
    check("no assistant prose in %s" % label, hits, [])

check.section("the watcher module still loads")
module = load_watcher()
check("it exposes a main", callable(getattr(module, "main", None)), True)
check("it knows where the hook lives", module.HOOK_SCRIPT.endswith("cec-hook.py"), True)

sys.exit(check.finish())
