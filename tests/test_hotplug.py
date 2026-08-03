"""Netlink uevent parsing - the live add/remove path for hotplugged controllers."""

import sys

from _harness import Checks, load_daemon

w = load_daemon()
check = Checks()


class FakeLoop:
    def __init__(self):
        self.later = []
        self.readers = {}

    def call_later(self, delay, cb, *a):
        self.later.append((delay, cb, a))

    def add_reader(self, fd, cb, *a):
        self.readers[fd] = cb

    def remove_reader(self, fd):
        self.readers.pop(fd, None)

    def create_task(self, coro):
        coro.close()


class FakeSock:
    """Yields queued datagrams, then behaves like a drained non-blocking socket."""

    def __init__(self, messages):
        self.messages = list(messages)

    def recv(self, _n):
        if self.messages:
            return self.messages.pop(0)
        raise BlockingIOError()

    def fileno(self):
        return 3

    def close(self):
        pass


class FakeConfig:
    cooldown = 2.5
    dry_run = False
    gamepad_only = False
    trigger_codes = {316}
    rescan = 5.0
    notify_on_trigger = False
    notify_on_failure = True
    unknown_codes = []
    problems = []
    path = "<fake>"


class FakeDevice:
    def __init__(self, path):
        self.path = path
        self.name = "Fake Pad"
        self.fd = -1


def uevent(action, subsystem, devname=None):
    """Build a kernel uevent datagram: header, NUL, then NUL-separated KEY=VALUE."""
    parts = [("%s@/devices/fake" % action).encode(),
             b"ACTION=" + action.encode(),
             b"SUBSYSTEM=" + subsystem.encode()]
    if devname:
        parts.append(b"DEVNAME=" + devname.encode())
    parts.append(b"SEQNUM=1234")
    return b"\x00".join(parts) + b"\x00"


def make(messages, log=None):
    watcher = w.ControllerWatcher(FakeConfig(), log or (lambda m: None))
    watcher.loop = FakeLoop()
    watcher.netlink = FakeSock(messages)
    return watcher


check.section("uevent parsing")
wt = make([uevent("add", "input", "input/event7")])
wt._netlink_readable()
check("add on an input event node schedules retries",
      len(wt.loop.later), len(w.HOTPLUG_RETRY_DELAYS))
check("retries target the right path",
      {a[0] for _d, _cb, a in wt.loop.later}, {"/dev/input/event7"})
check("retry delays match the constant",
      [d for d, _cb, _a in wt.loop.later], list(w.HOTPLUG_RETRY_DELAYS))

for label, message in [
    ("non-input subsystem ignored", uevent("add", "usb", "bus/usb/001/004")),
    ("input but not an event node ignored", uevent("add", "input", "input/mouse0")),
    ("joydev node ignored (we read evdev)", uevent("add", "input", "input/js0")),
    ("missing DEVNAME ignored", uevent("add", "input")),
    ("change action does nothing", uevent("change", "input", "input/event7")),
    ("malformed datagram ignored", b"garbage-without-nuls"),
]:
    wt = make([message])
    wt._netlink_readable()
    check(label, wt.loop.later, [])

check.section("remove drops a watched device")
dropped = []
wt = make([uevent("remove", "input", "input/event7")], log=dropped.append)
device = FakeDevice("/dev/input/event7")
wt.open_devices[device.path] = device
wt._netlink_readable()
check("remove drops it", "/dev/input/event7" in wt.open_devices, False)
check("remove logged as unplugged", "unplugged" in dropped[-1], True)

wt = make([uevent("remove", "input", "input/event7")])
wt._netlink_readable()
check("remove of an unwatched device is a no-op", wt.open_devices, {})

check.section("batching and robustness")
wt = make([
    uevent("add", "input", "input/event7"),
    uevent("add", "usb", "bus/usb/001/004"),
    uevent("add", "input", "input/event8"),
])
wt._netlink_readable()
check("drains the whole queue in one wakeup",
      {a[0] for _d, _cb, a in wt.loop.later},
      {"/dev/input/event7", "/dev/input/event8"})

wt = make([uevent("add", "input", "/dev/input/event7")])
wt._netlink_readable()
check("absolute DEVNAME handled",
      {a[0] for _d, _cb, a in wt.loop.later}, {"/dev/input/event7"})


class ExplodingSock(FakeSock):
    def recv(self, _n):
        raise OSError("socket blew up")


wt = w.ControllerWatcher(FakeConfig(), lambda m: None)
wt.loop = FakeLoop()
wt.netlink = ExplodingSock([])
wt.loop.readers[3] = None
wt._netlink_readable()
check("socket error tears down netlink cleanly", wt.netlink, None)
check("reader unregistered on teardown", 3 in wt.loop.readers, False)

sys.exit(check.finish())
