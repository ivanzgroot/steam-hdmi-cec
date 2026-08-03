"""Config parsing, sysfs bitmask decoding, key-name resolution, log rotation."""

import os
import struct
import sys
import tempfile

from _harness import Checks, load_daemon

w = load_daemon()
check = Checks()

check.section("struct layout")
# On the target (Linux x86_64) long is 8 bytes, so input_event is 24 bytes.
check("input_event size for LP64", struct.calcsize("=qqHHi"), 24)
check("EVENT_FMT follows the native long",
      w.EVENT_SIZE, struct.calcsize("l") * 2 + 8)
check("LONG_BITS follows the native long", w.LONG_BITS, struct.calcsize("l") * 8)


def bits_of(value, base=0):
    return {base + i for i in range(value.bit_length()) if (value >> i) & 1}


check.section("parse_bitmap (kernel writes %lx per word, MSW first, unpadded)")
# BTN_MODE = 0x13c = 316 -> word 4 (316//64), bit 60 (316%64)
check("BTN_MODE only", w.parse_bitmap("1000000000000000 0 0 0 0", 64), {316})
# Regression: an 8-hex-char lower word must NOT be read as 32-bit words. Real
# masks look exactly like this because the kernel formats them with "%lx".
mixed = "1000000000000000 0 0 7cdb0000 0"
check("unpadded lower word keeps 64-bit scale",
      w.parse_bitmap(mixed, 64), {316} | bits_of(0x7CDB0000, 64))
check("not misread as 32-bit words", 188 in w.parse_bitmap(mixed, 64), False)
check("empty", w.parse_bitmap("", 64), set())
check("single word", w.parse_bitmap("d", 64), {0, 2, 3})
# KEY_HOMEPAGE = 172 -> word 2, bit 44
check("KEY_HOMEPAGE 172", w.parse_bitmap("100000000000 0 0", 64), {172})
check("garbage word skipped", w.parse_bitmap("zz 1", 64), {0})

check.section("key name resolution")
check("BTN_MODE", w.KEY_NAMES.get("BTN_MODE"), 0x13C)
check("KEY_HOMEPAGE", w.KEY_NAMES.get("KEY_HOMEPAGE"), 172)
check("BTN_HOME absent from mainline codes", "BTN_HOME" in w.KEY_NAMES, False)
check("code_name(316)", w.code_name(316), "BTN_MODE")
check("code_name(172)", w.code_name(172), "KEY_HOMEPAGE")
check("code_name unknown", w.code_name(9999), "code_9999")
check("fmt_codes", w.fmt_codes([316, 172]), "BTN_MODE(316), KEY_HOMEPAGE(172)")
check("fmt_codes empty", w.fmt_codes([]), "none")

check.section("header augmentation (aliases resolve)")
scratch = tempfile.mkdtemp()
header = os.path.join(scratch, "input-event-codes.h")
with open(header, "w") as fh:
    fh.write(
        "#define KEY_RESERVED 0\n"
        "#define BTN_SOUTH\t\t0x130\n"
        "#define BTN_A\t\t\tBTN_SOUTH\n"
        "#define BTN_MODE\t\t0x13c\n"
        "#define KEY_FANCY_NEW\t\t0x2f0  /* trailing comment */\n"
        "#define KEY_MAX\t\t\t0x2ff\n"
        "#define KEY_CNT\t\t\t(KEY_MAX+1)\n"
        "#define SOMETHING_ELSE\t\t5\n"
    )
saved = w.KERNEL_CODE_HEADER
w.KERNEL_CODE_HEADER = header
names = w.load_key_names()
w.KERNEL_CODE_HEADER = saved
check("header adds new code", names.get("KEY_FANCY_NEW"), 0x2F0)
check("alias resolved", names.get("BTN_A"), 0x130)
check("_MAX skipped", "KEY_MAX" in names, False)
check("_CNT skipped", "KEY_CNT" in names, False)
check("non KEY/BTN ignored", "SOMETHING_ELSE" in names, False)
check("base entries survive", names.get("KEY_HOMEPAGE"), 172)
check("missing header is not fatal", isinstance(w.load_key_names(), dict), True)

check.section("config parsing")
cfgpath = os.path.join(scratch, "test.conf")
with open(cfgpath, "w") as fh:
    fh.write(
        "# a comment\n"
        "\n"
        "COOLDOWN_SECONDS=3.5\n"
        'BUTTON_CODES="BTN_MODE BTN_HOME KEY_HOMEPAGE"\n'
        "GAMEPAD_ONLY=0\n"
        "DRY_RUN=yes\n"
        "LOG_MAX_BYTES=2048   # inline comment\n"
        "export LOG_KEEP=3\n"
        "NOTIFY_ON_FAILURE='1'\n"
        "this line is not an assignment\n"
        "RESCAN_SECONDS=banana\n"
    )
cfg = w.Config(cfgpath)
check("float value", cfg.cooldown, 3.5)
check("bool yes", cfg.dry_run, True)
check("bool 0", cfg.gamepad_only, False)
check("single-quoted bool", cfg.notify_on_failure, True)
check("inline comment stripped", cfg.log_max_bytes, 2048)
check("export prefix", cfg.log_keep, 3)
check("bad number falls back to default", cfg.rescan, float(w.DEFAULTS["RESCAN_SECONDS"]))
check("trigger codes resolved", cfg.trigger_codes, {316, 172})
check("BTN_HOME reported unknown, not fatal", cfg.unknown_codes, ["BTN_HOME"])
check("problems recorded", any("banana" in p for p in cfg.problems), True)

check.section("config edge cases")
with open(cfgpath, "w") as fh:
    fh.write("BUTTON_CODES='0x13c, 172,BTN_START'\nCOOLDOWN_SECONDS=-5\n")
cfg2 = w.Config(cfgpath)
check("numeric + comma separated + name", cfg2.trigger_codes, {316, 172, 0x13B})
check("negative cooldown clamped", cfg2.cooldown, 0.0)

with open(cfgpath, "w") as fh:
    fh.write("BUTTON_CODES='NONSENSE_ONLY'\n")
check("unresolvable codes fall back to BTN_MODE", w.Config(cfgpath).trigger_codes, {316})

missing = w.Config(os.path.join(scratch, "definitely-missing.conf"))
check("missing config uses defaults", missing.cooldown, 2.5)
check("missing config still has trigger codes", missing.trigger_codes, {316, 172})

check.section("logger rotation")
logpath = os.path.join(scratch, "t.log")
log = w.Logger(logpath, max_bytes=200, keep=2, echo=False)
for i in range(60):
    log("line %03d padded out to force rotation" % i)
check(".1 exists", os.path.exists(logpath + ".1"), True)
check(".2 exists", os.path.exists(logpath + ".2"), True)
check(".3 does not (keep=2)", os.path.exists(logpath + ".3"), False)
# Rotation is check-then-write, so the live log may overshoot by the one line
# that crossed the cap - bounded, which is all a size cap needs to be.
check("live log bounded near cap", os.path.getsize(logpath) < 400, True)
with open(logpath) as fh:
    check("timestamp+tag format", fh.readline()[:1].isdigit(), True)

nolog = os.path.join(scratch, "u.log")
log2 = w.Logger(nolog, max_bytes=0, keep=2, echo=False)
for i in range(60):
    log2("line %03d padded out, rotation disabled" % i)
check("max_bytes=0 disables rotation", os.path.exists(nolog + ".1"), False)

sys.exit(check.finish())
