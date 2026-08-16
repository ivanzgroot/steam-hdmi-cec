"""End-to-end device enumeration and --detect, against a fake sysfs tree."""

import io
import os
import shutil
import sys
import tempfile
from contextlib import redirect_stdout

from _harness import Checks, load_watcher, mask_for

import cec_config

w = load_watcher()


def _run_detect(path):
    """run_detect now takes the resolved trigger codes rather than digging them
    out of the config itself, so the two callers cannot disagree about which
    codes are live."""
    config = cec_config.Config(path)
    codes, unknown = config.button_codes(w.KEY_NAMES)
    return w.run_detect(config, codes, unknown)

check = Checks()

root = tempfile.mkdtemp()
sysdir = os.path.join(root, "sys")
devdir = os.path.join(root, "dev")
os.makedirs(sysdir)
os.makedirs(devdir)


def make_device(event, name, keys, abs_bits=(), phys=""):
    base = os.path.join(sysdir, event, "device")
    os.makedirs(os.path.join(base, "capabilities"))
    with open(os.path.join(base, "name"), "w") as fh:
        fh.write(name + "\n")
    if phys:
        with open(os.path.join(base, "phys"), "w") as fh:
            fh.write(phys + "\n")
    with open(os.path.join(base, "capabilities", "key"), "w") as fh:
        fh.write(mask_for(keys, w) + "\n")
    with open(os.path.join(base, "capabilities", "abs"), "w") as fh:
        fh.write(mask_for(abs_bits, w) if abs_bits else "0")
    with open(os.path.join(devdir, event), "w") as fh:
        fh.write("")


BTN_SOUTH, BTN_MODE, KEY_HOMEPAGE, KEY_A, ABS_X = 0x130, 0x13C, 172, 30, 0x00

make_device("event0", "Power Button", {116})
make_device("event2", "AT Translated Set 2 keyboard", {KEY_A, KEY_HOMEPAGE})
make_device("event3", "Microsoft X-Box 360 pad", {BTN_SOUTH, BTN_MODE}, {ABS_X},
            phys="usb-0000:00:14.0-3/input0")
make_device("event10", "Sony Interactive Entertainment Wireless Controller",
            {BTN_SOUTH, BTN_MODE}, {ABS_X})
make_device("event11", "8BitDo Pro 2 Consumer Control", {KEY_HOMEPAGE})

w.SYS_INPUT = sysdir
w.DEV_INPUT = devdir

check.section("enumeration")
devices = w.list_input_devices()
check("finds every node", len(devices), 5)
check("sorted numerically, not lexically",
      [os.path.basename(d.path) for d in devices],
      ["event0", "event2", "event3", "event10", "event11"])

by_name = {d.name: d for d in devices}
pad = by_name["Microsoft X-Box 360 pad"]
check("name read from sysfs", "Microsoft X-Box 360 pad" in by_name, True)
check("key capabilities decoded", {BTN_SOUTH, BTN_MODE} <= pad.keys, True)
check("BTN_MODE present", BTN_MODE in pad.keys, True)
check("gamepad detected", pad.is_gamepad, True)
check("phys read", pad.phys, "usb-0000:00:14.0-3/input0")
check("power button is not a gamepad", by_name["Power Button"].is_gamepad, False)
check("consumer-control node is not a gamepad",
      by_name["8BitDo Pro 2 Consumer Control"].is_gamepad, False)
check("consumer-control node still has KEY_HOMEPAGE",
      KEY_HOMEPAGE in by_name["8BitDo Pro 2 Consumer Control"].keys, True)

check.section("--detect with default config")
cfgpath = os.path.join(root, "config.conf")
with open(cfgpath, "w") as fh:
    fh.write('BUTTON_CODES="BTN_MODE BTN_HOME KEY_HOMEPAGE"\nCOOLDOWN_SECONDS=2.5\n')

buf = io.StringIO()
with redirect_stdout(buf):
    rc = _run_detect(cfgpath)
out = buf.getvalue()

check("exit 0 when something is watched", rc, 0)
check("xbox pad watched", "Microsoft X-Box 360 pad" in out, True)
check("power button ignored", "no trigger button" in out, True)
check("counts watched devices", "5 input device(s), 4 watched." in out, True)
check("reports BTN_HOME as undefined", "not defined by this kernel" in out, True)
check("names the undefined entry", "BTN_HOME" in out, True)
check("shows trigger codes with numbers", "BTN_MODE(316)" in out, True)
check("shows KEY_HOMEPAGE for the consumer node", "KEY_HOMEPAGE(172)" in out, True)

check.section("--detect with GAMEPAD_ONLY=1")
with open(cfgpath, "w") as fh:
    fh.write('BUTTON_CODES="BTN_MODE KEY_HOMEPAGE"\nGAMEPAD_ONLY=1\n')
buf = io.StringIO()
with redirect_stdout(buf):
    _run_detect(cfgpath)
out = buf.getvalue()
check("gamepad-only narrows to real pads", "5 input device(s), 2 watched." in out, True)
check("explains why the keyboard was dropped", "GAMEPAD_ONLY=1" in out, True)

check.section("--detect when nothing matches")
with open(cfgpath, "w") as fh:
    fh.write('BUTTON_CODES="BTN_TRIGGER_HAPPY"\n')
buf = io.StringIO()
with redirect_stdout(buf):
    rc = _run_detect(cfgpath)
out = buf.getvalue()
check("exit 1 when nothing would trigger", rc, 1)
check("suggests --monitor", "--monitor" in out, True)
check("points at the config file", cfgpath in out, True)

check.section("--detect with no devices at all")
empty = tempfile.mkdtemp()
w.SYS_INPUT = empty
buf = io.StringIO()
with redirect_stdout(buf):
    rc = _run_detect(cfgpath)
check("exit 1 with no devices", rc, 1)
check("says so", "No input devices found" in buf.getvalue(), True)
w.SYS_INPUT = sysdir

check.section("main() dispatch")
buf = io.StringIO()
with redirect_stdout(buf):
    rc = w.main(["--detect", "--config", cfgpath])
check("main routes --detect", rc, 1)
check("main --detect produced output", "input device(s)" in buf.getvalue(), True)

try:
    with redirect_stdout(io.StringIO()):
        w.main(["--detect", "--monitor"])
    check("mutually exclusive modes rejected", "no SystemExit", "SystemExit")
except SystemExit as exc:
    check("mutually exclusive modes rejected", exc.code, 2)

try:
    buf = io.StringIO()
    with redirect_stdout(buf):
        w.main(["--version"])
    check("--version exits", "no SystemExit", "SystemExit")
except SystemExit as exc:
    check("--version exits 0", exc.code, 0)
    check("--version prints version", w.VERSION in buf.getvalue(), True)

shutil.rmtree(root, ignore_errors=True)
shutil.rmtree(empty, ignore_errors=True)

sys.exit(check.finish())
