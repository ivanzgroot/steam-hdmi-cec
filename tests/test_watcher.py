"""The controller watcher: bitmask decoding, device selection, debounce, events.

This layer fails quietly, which is why it is tested this hard. A misparsed
capability bitmask does not crash - the daemon starts, reports itself healthy,
and silently decides your controller has no Home button.
"""

import os
import struct
import sys
import tempfile

from _harness import Checks, load_watcher

w = load_watcher()
check = Checks()

EV_KEY, EV_ABS, EV_SYN = 0x01, 0x03, 0x00


def bits_of(value, base=0):
    return {base + i for i in range(value.bit_length()) if (value >> i) & 1}


check.section("struct layout")
# On the target (Linux x86_64) long is 8 bytes, so input_event is 24 bytes.
check("input_event size for LP64", struct.calcsize("=qqHHi"), 24)
check("EVENT_FMT follows the native long",
      w.EVENT_SIZE, struct.calcsize("l") * 2 + 8)
check("LONG_BITS follows the native long", w.LONG_BITS, struct.calcsize("l") * 8)

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
with open(header, "w") as handle:
    handle.write(
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


class FakeLoop:
    def __init__(self):
        self.tasks = 0
        self.readers = {}

    def create_task(self, coro):
        self.tasks += 1
        coro.close()          # never run; we only count launches
        return None

    def add_reader(self, fd, cb, *args):
        self.readers[fd] = (cb, args)

    def remove_reader(self, fd):
        self.readers.pop(fd, None)


class FakeConfig:
    def __init__(self, **kw):
        self.cooldown = kw.get("cooldown", 2.5)
        self.dry_run = kw.get("dry_run", False)
        self.gamepad_only = kw.get("gamepad_only", False)
        self.rescan = 5.0
        self.notify_on_trigger = False
        self.notify_on_failure = True
        self.problems = []
        self.path = "<fake>"


class FakeDevice:
    def __init__(self, path="/dev/input/event9", name="Fake Pad",
                 keys=None, is_gamepad=True):
        self.path = path
        self.name = name
        self.keys = keys if keys is not None else {304, 316}
        self.is_gamepad = is_gamepad
        self.has_abs = is_gamepad
        self.phys = ""
        self.event = os.path.basename(path)
        self.fd = -1


def make_watcher(codes=None, **cfgkw):
    lines = []
    watcher = w.ControllerWatcher(FakeConfig(**cfgkw), lines.append,
                                  codes if codes is not None else {316, 172})
    watcher.loop = FakeLoop()
    return watcher, lines


check.section("debounce / cooldown")
watcher, log = make_watcher(cooldown=2.5)
device = FakeDevice()

watcher._on_press(device, 316)
check("first press launches the hook", watcher.loop.tasks, 1)
check("first press logged as a trigger", "triggering wake" in log[-1], True)

watcher._on_press(device, 316)
check("a press while the hook runs launches nothing", watcher.loop.tasks, 1)
check("and says why", "already in progress" in log[-1], True)

watcher.hook_running = False       # hook finished, still inside the cooldown
watcher._on_press(device, 316)
check("a press inside the cooldown launches nothing", watcher.loop.tasks, 1)
check("the cooldown skip is logged", "cooldown" in log[-1], True)
check("and names the device", "/dev/input/event9" in log[-1], True)

watcher.last_trigger -= 3.0        # pretend the cooldown elapsed
watcher._on_press(device, 316)
check("a press after the cooldown launches", watcher.loop.tasks, 2)

check.section("the cooldown is global across devices")
# Steam mirrors a physical pad onto a virtual uinput device, so one button press
# arrives twice on two different nodes. Debouncing per device would wake twice.
watcher, _ = make_watcher(cooldown=2.5)
watcher._on_press(FakeDevice("/dev/input/event4", "Physical Pad"), 316)
watcher.hook_running = False       # isolate the cooldown from the in-flight guard
watcher._on_press(FakeDevice("/dev/input/event5", "Steam virtual pad"), 316)
check("the mirrored virtual device is debounced too", watcher.loop.tasks, 1)

check.section("dry run")
watcher, log = make_watcher(dry_run=True)
watcher._on_press(device, 316)
check("dry run launches nothing", watcher.loop.tasks, 0)
check("dry run says it would run", "DRY RUN - would run" in log[-1], True)
check("dry run leaves no in-flight flag", watcher.hook_running, False)
watcher.last_trigger -= 99
watcher._on_press(device, 316)
check("dry run still keeps cooldown accounting", len(log), 2)

check.section("zero cooldown")
watcher, _ = make_watcher(cooldown=0.0)
watcher._on_press(device, 316)
watcher.hook_running = False
watcher._on_press(device, 316)
check("cooldown 0 allows back-to-back presses", watcher.loop.tasks, 2)

check.section("device selection")
watcher, _ = make_watcher()
check("a gamepad with BTN_MODE matches",
      watcher._watchable(FakeDevice(keys={304, 316})), [316])
check("no trigger code means not watched",
      watcher._watchable(FakeDevice(keys={304, 305})), None)
check("a KEY_HOMEPAGE-only node matches",
      watcher._watchable(FakeDevice(keys={172}, is_gamepad=False)), [172])
check("both codes are reported sorted",
      watcher._watchable(FakeDevice(keys={172, 316})), [172, 316])

watcher, _ = make_watcher(gamepad_only=True)
check("GAMEPAD_ONLY=1 keeps a real gamepad",
      watcher._watchable(FakeDevice(keys={316}, is_gamepad=True)), [316])
check("GAMEPAD_ONLY=1 drops a non-gamepad Home node",
      watcher._watchable(FakeDevice(keys={172}, is_gamepad=False)), None)

check.section("raw input_event decoding")


def feed(watcher, events):
    """Push encoded input_events through a pipe into watcher._readable."""
    read_fd, write_fd = os.pipe()
    device = FakeDevice()
    device.fd = read_fd
    watcher.open_devices[device.path] = device
    os.write(write_fd, b"".join(
        struct.pack(w.EVENT_FMT, 0, 0, t, c, v) for t, c, v in events))
    os.close(write_fd)
    watcher._readable(device.path)
    try:
        os.close(read_fd)
    except OSError:
        pass


for label, events, want in [
    ("key-down on a trigger code fires", [(EV_KEY, 316, 1)], 1),
    ("key-up is ignored", [(EV_KEY, 316, 0)], 0),
    ("autorepeat is ignored", [(EV_KEY, 316, 2)], 0),
    ("a non-trigger button is ignored", [(EV_KEY, 304, 1)], 0),
    ("EV_ABS with the same code is ignored", [(EV_ABS, 316, 1)], 0),
    ("KEY_HOMEPAGE fires", [(EV_KEY, 172, 1)], 1),
    ("a trigger is found in a multi-event batch",
     [(EV_SYN, 0, 0), (EV_KEY, 304, 1), (EV_KEY, 316, 1), (EV_SYN, 0, 0)], 1),
]:
    watcher, _ = make_watcher()
    feed(watcher, events)
    check(label, watcher.loop.tasks, want)

check.section("disconnect handling")
watcher, log = make_watcher()
device = FakeDevice()
read_fd, write_fd = os.pipe()
device.fd = read_fd
watcher.open_devices[device.path] = device
os.close(write_fd)                 # writer gone -> read returns b"" (EOF)
watcher._readable(device.path)
check("EOF drops the device", device.path in watcher.open_devices, False)
check("EOF is logged", "end of file" in log[-1], True)

watcher, log = make_watcher()
device = FakeDevice()
device.fd = 9999                   # invalid fd -> OSError on read
watcher.open_devices[device.path] = device
watcher._readable(device.path)
check("a read error drops the device", device.path in watcher.open_devices, False)
check("a read error is logged", "read error" in log[-1], True)

watcher, _ = make_watcher()
watcher._readable("/dev/input/event-not-open")
check("an unknown path is a no-op", watcher.loop.tasks, 0)

sys.exit(check.finish())
