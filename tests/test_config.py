"""Config parsing and log rotation.

The config used to be read by two languages at once - sourced by bash and parsed
by Python - which meant every default was written twice and kept in step by a
test. It is now read here and nowhere else, so these checks are about parsing
being tolerant rather than about two implementations agreeing.

Tolerant is the requirement. A typo in this file must never stop the TV coming
on: bad values fall back to their defaults, record a problem, and the wake
proceeds.
"""

import os
import sys
import tempfile

from _harness import Checks

import cec_config
import cec_log

check = Checks()
scratch = tempfile.mkdtemp()


def write_config(text, name="test.conf"):
    path = os.path.join(scratch, name)
    with open(path, "w") as handle:
        handle.write(text)
    return path


check.section("basic parsing")
path = write_config(
    "# a comment\n"
    "\n"
    "COOLDOWN_SECONDS=3.5\n"
    'OSD_NAME="Living Room"\n'
    "GAMEPAD_ONLY=0\n"
    "DRY_RUN=yes\n"
    "LOG_MAX_BYTES=2048   # inline comment\n"
    "export LOG_KEEP=3\n"
    "NOTIFY_ON_FAILURE='1'\n"
    "this line is not an assignment\n"
    "RESCAN_SECONDS=banana\n"
)
config = cec_config.Config(path)
check("float value", config.cooldown, 3.5)
check("double-quoted string with a space", config.osd_name, "Living Room")
check("yes is true", config.dry_run, True)
check("0 is false", config.gamepad_only, False)
check("single-quoted bool", config.notify_on_failure, True)
check("inline comment stripped", config.log_max_bytes, 2048)
check("export prefix accepted", config.log_keep, 3)
check("bad number falls back to the default",
      config.rescan, float(cec_config.DEFAULTS["RESCAN_SECONDS"]))
check("the non-assignment line is reported",
      any("not a KEY=VALUE" in p for p in config.problems), True)
check("the bad number is reported",
      any("banana" in p for p in config.problems), True)

check.section("quoting")
path = write_config(
    'EXTRA_WAKE_FRAMES="image-view-on; active-source"\n'
    "OSD_NAME=Bare\n"
    'VENDOR_ID=""\n'
)
config = cec_config.Config(path)
check("semicolons survive quoting",
      config.extra_wake_frames, "image-view-on; active-source")
check("unquoted values work", config.osd_name, "Bare")
check("an empty quoted value is empty", config.vendor_id, "")

# The old parser took the first closing quote it found, so a value merely
# containing a quote was silently truncated. Only a matched pair counts now.
path = write_config('OSD_NAME=say "hi" there\n')
check("a value containing quotes is not truncated",
      cec_config.Config(path).osd_name, 'say "hi" there')

check.section("clamping keeps nonsense out of the wake path")
path = write_config(
    "COOLDOWN_SECONDS=-5\n"
    "FRAME_GAP_MS=999999\n"
    "WAKE_ATTEMPTS=0\n"
    "REPLY_TIMEOUT_MS=1\n"
)
config = cec_config.Config(path)
check("negative cooldown clamps to zero", config.cooldown, 0.0)
check("an absurd frame gap clamps to the maximum", config.frame_gap_ms, 5000)
check("zero attempts clamps to one", config.wake_attempts, 1)
check("a tiny reply timeout clamps to the minimum", config.reply_timeout_ms, 100)
check("every clamp is reported", len(config.problems) >= 4, True)

check.section("booleans")
for text, expected in (("1", True), ("yes", True), ("true", True), ("on", True),
                       ("0", False), ("no", False), ("false", False),
                       ("off", False), ("", False)):
    path = write_config("WAKE_TV=%s\n" % text)
    check("WAKE_TV=%r" % text, cec_config.Config(path).wake_tv, expected)

path = write_config("WAKE_TV=maybe\n")
config = cec_config.Config(path)
check("a nonsense boolean falls back to the default", config.wake_tv, True)
check("and says so", any("yes/no" in p for p in config.problems), True)

check.section("a missing config is not an error")
config = cec_config.Config(os.path.join(scratch, "definitely-missing.conf"))
check("defaults are used", config.cooldown, 2.5)
check("the wake still knows what to do", config.wake_tv, True)
check("the receiver is still handled", config.wake_audio, True)
check("it is reported once", len(config.problems), 1)

check.section("unknown keys are kept, not dropped")
path = write_config("SOMETHING_FUTURE=42\nWAKE_TV=1\n")
config = cec_config.Config(path)
check("an unrecognised key survives parsing", config.values.get("SOMETHING_FUTURE"), "42")
check("and does not become a problem",
      any("SOMETHING_FUTURE" in p for p in config.problems), False)

check.section("button codes resolve against the running kernel")
KEY_NAMES = {"BTN_MODE": 316, "KEY_HOMEPAGE": 172, "BTN_START": 0x13B}
path = write_config('BUTTON_CODES="BTN_MODE BTN_HOME KEY_HOMEPAGE"\n')
codes, unknown = cec_config.Config(path).button_codes(KEY_NAMES)
check("known names resolve", codes, {316, 172})
check("BTN_HOME is reported, not fatal", unknown, ["BTN_HOME"])

path = write_config("BUTTON_CODES='0x13c, 172,BTN_START'\n")
codes, _ = cec_config.Config(path).button_codes(KEY_NAMES)
check("hex, decimal and names mix freely", codes, {316, 172, 0x13B})

path = write_config("BUTTON_CODES='NONSENSE_ONLY'\n")
config = cec_config.Config(path)
codes, unknown = config.button_codes(KEY_NAMES)
check("unresolvable codes fall back to BTN_MODE", codes, {316})
check("the fallback is reported",
      any("resolved to nothing" in p for p in config.problems), True)

check.section("log rotation")
logpath = os.path.join(scratch, "t.log")
log = cec_log.Logger(logpath, max_bytes=200, keep=2, echo=False)
for index in range(60):
    log("line %03d padded out to force rotation" % index)
check(".1 exists", os.path.exists(logpath + ".1"), True)
check(".2 exists", os.path.exists(logpath + ".2"), True)
check(".3 does not (keep=2)", os.path.exists(logpath + ".3"), False)
# Rotation is check-then-write, so the live log may overshoot by the one line
# that crossed the cap - bounded, which is all a size cap needs to be.
check("the live log stays bounded near the cap", os.path.getsize(logpath) < 400, True)
with open(logpath) as handle:
    check("lines start with a timestamp", handle.readline()[:1].isdigit(), True)

nolog = os.path.join(scratch, "u.log")
log = cec_log.Logger(nolog, max_bytes=0, keep=2, echo=False)
for index in range(60):
    log("line %03d padded out, rotation disabled" % index)
check("max_bytes=0 disables rotation", os.path.exists(nolog + ".1"), False)

sys.exit(check.finish())
