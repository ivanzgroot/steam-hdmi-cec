"""ControllerWatcher's debounce state machine, device selection and event decoding."""

import os
import struct
import sys

from _harness import Checks, load_daemon

w = load_daemon()
check = Checks()

EV_KEY, EV_ABS, EV_SYN = 0x01, 0x03, 0x00


class FakeLoop:
    def __init__(self):
        self.tasks = 0
        self.readers = {}

    def create_task(self, coro):
        self.tasks += 1
        coro.close()          # never run; we only count launches
        return None

    def add_reader(self, fd, cb, *a):
        self.readers[fd] = (cb, a)

    def remove_reader(self, fd):
        self.readers.pop(fd, None)


class FakeConfig:
    def __init__(self, **kw):
        self.cooldown = kw.get("cooldown", 2.5)
        self.dry_run = kw.get("dry_run", False)
        self.gamepad_only = kw.get("gamepad_only", False)
        self.trigger_codes = kw.get("trigger_codes", {316, 172})
        self.rescan = 5.0
        self.notify_on_trigger = False
        self.notify_on_failure = True
        self.unknown_codes = []
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


def make_watcher(**cfgkw):
    lines = []
    watcher = w.ControllerWatcher(FakeConfig(**cfgkw), lines.append)
    watcher.loop = FakeLoop()
    return watcher, lines


check.section("debounce / cooldown")
wt, log = make_watcher(cooldown=2.5)
dev = FakeDevice()

wt._on_press(dev, 316)
check("first press launches hook", wt.loop.tasks, 1)
check("first press logged as trigger", "triggering wake" in log[-1], True)

wt._on_press(dev, 316)
check("press while hook running does not launch", wt.loop.tasks, 1)
check("press while hook running logged", "already in progress" in log[-1], True)

wt.hook_running = False           # hook finished, still inside the cooldown
wt._on_press(dev, 316)
check("press inside cooldown does not launch", wt.loop.tasks, 1)
check("cooldown skip logged", "cooldown" in log[-1], True)
check("cooldown skip names the device", "/dev/input/event9" in log[-1], True)

wt.last_trigger -= 3.0            # pretend the cooldown elapsed
wt._on_press(dev, 316)
check("press after cooldown launches", wt.loop.tasks, 2)

check.section("cooldown is global across devices")
wt2, _ = make_watcher(cooldown=2.5)
wt2._on_press(FakeDevice("/dev/input/event4", "Physical Pad"), 316)
wt2.hook_running = False          # isolate the cooldown from the in-flight guard
wt2._on_press(FakeDevice("/dev/input/event5", "Steam virtual pad"), 316)
check("mirrored virtual device is debounced too", wt2.loop.tasks, 1)

check.section("dry run")
wt3, log3 = make_watcher(dry_run=True)
wt3._on_press(dev, 316)
check("dry run launches nothing", wt3.loop.tasks, 0)
check("dry run says would run", "DRY RUN - would run" in log3[-1], True)
check("dry run leaves no in-flight flag", wt3.hook_running, False)
wt3.last_trigger -= 99
wt3._on_press(dev, 316)
check("dry run still respects cooldown accounting", len(log3), 2)

check.section("zero cooldown")
wt4, _ = make_watcher(cooldown=0.0)
wt4._on_press(dev, 316)
wt4.hook_running = False
wt4._on_press(dev, 316)
check("cooldown 0 allows back-to-back", wt4.loop.tasks, 2)

check.section("device selection")
wt5, _ = make_watcher()
check("gamepad with BTN_MODE matches", wt5._watchable(FakeDevice(keys={304, 316})), [316])
check("no trigger code -> not watched", wt5._watchable(FakeDevice(keys={304, 305})), None)
check("KEY_HOMEPAGE-only node matches",
      wt5._watchable(FakeDevice(keys={172}, is_gamepad=False)), [172])
check("both codes reported sorted", wt5._watchable(FakeDevice(keys={172, 316})), [172, 316])

wt6, _ = make_watcher(gamepad_only=True)
check("GAMEPAD_ONLY=1 keeps real gamepad",
      wt6._watchable(FakeDevice(keys={316}, is_gamepad=True)), [316])
check("GAMEPAD_ONLY=1 drops non-gamepad Home node",
      wt6._watchable(FakeDevice(keys={172}, is_gamepad=False)), None)

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
    ("key-down on trigger code fires", [(EV_KEY, 316, 1)], 1),
    ("key-up ignored", [(EV_KEY, 316, 0)], 0),
    ("autorepeat ignored", [(EV_KEY, 316, 2)], 0),
    ("non-trigger button ignored", [(EV_KEY, 304, 1)], 0),
    ("EV_ABS with same code ignored", [(EV_ABS, 316, 1)], 0),
    ("KEY_HOMEPAGE fires", [(EV_KEY, 172, 1)], 1),
    ("trigger found in a multi-event batch",
     [(EV_SYN, 0, 0), (EV_KEY, 304, 1), (EV_KEY, 316, 1), (EV_SYN, 0, 0)], 1),
]:
    watcher, _ = make_watcher()
    feed(watcher, events)
    check(label, watcher.loop.tasks, want)

check.section("disconnect handling")
wt14, log14 = make_watcher()
d = FakeDevice()
rfd, wfd = os.pipe()
d.fd = rfd
wt14.open_devices[d.path] = d
os.close(wfd)                      # writer gone -> read returns b"" (EOF)
wt14._readable(d.path)
check("EOF drops the device", d.path in wt14.open_devices, False)
check("EOF logged", "end of file" in log14[-1], True)

wt15, log15 = make_watcher()
d2 = FakeDevice()
d2.fd = 9999                       # invalid fd -> OSError on read
wt15.open_devices[d2.path] = d2
wt15._readable(d2.path)
check("read error drops the device", d2.path in wt15.open_devices, False)
check("read error logged", "read error" in log15[-1], True)

wt16, _ = make_watcher()
wt16._readable("/dev/input/event-not-open")
check("unknown path is a no-op", wt16.loop.tasks, 0)

sys.exit(check.finish())
