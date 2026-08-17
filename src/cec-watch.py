#!/usr/bin/env python3
"""cec-watch - wake the TV when a controller's Home/Guide button is pressed.

Watches every connected input device that advertises one of the configured
trigger codes, picks hotplugged controllers up and dropped ones down live,
debounces, and runs `cec-hook.py on`.

It never speaks CEC itself. cec-hook owns all of that, including the DisplayPort
tunneling fix that post-suspend wakes depend on, and it stays a separate process
on purpose: a wake can block for tens of seconds while the retry escalation
runs, and the input loop has to keep reading throughout.

Standard library only - no evdev, no pyudev, no pip, no venv. Devices are
enumerated through sysfs, read as raw struct input_event from /dev/input/event*,
and hotplug arrives on a netlink uevent socket. That keeps a read-only SteamOS
root with no compiler viable.

Modes:
    (no args)   run as a daemon; what cec-hdmi-controller.service does
    --detect    list input devices and which ones would be watched, then exit
    --monitor   print key events live, to find out what your Home button sends
"""

import argparse
import asyncio
import os
import pwd
import re
import signal
import socket
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cec_config import CEC_DIR, Config, DEFAULT_CONFIG          # noqa: E402
from cec_log import Logger                                      # noqa: E402

VERSION = "1.0.0"

HOOK_SCRIPT = os.path.join(CEC_DIR, "cec-hook.py")
DEFAULT_LOG = os.path.join(CEC_DIR, "cec-controller.log")

SYS_INPUT = "/sys/class/input"
DEV_INPUT = "/dev/input"
KERNEL_CODE_HEADER = "/usr/include/linux/input-event-codes.h"

EV_KEY = 0x01
BTN_GAMEPAD = 0x130

# struct input_event { struct timeval time; __u16 type; __u16 code; __s32 value; }
# 24 bytes on x86_64 (LP64): two 8-byte longs, two u16, one s32.
EVENT_FMT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FMT)

# BITS_PER_LONG as seen by this process; the unit sysfs capability bitmaps use.
LONG_BITS = struct.calcsize("l") * 8

NETLINK_KOBJECT_UEVENT = 15
UEVENT_KERNEL_GROUP = 1

# Delays after a kernel "add" uevent before trying to open the node: the kernel
# announces the device before udev has applied its root:input 0660 permissions.
HOTPLUG_RETRY_DELAYS = (0.3, 1.0, 3.0)

# Enough of linux/input-event-codes.h to resolve the names anyone would
# reasonably put in BUTTON_CODES. Augmented at runtime from the real header when
# it is present. Order matters: the first name listed for a code is the one used
# when printing that code back out.
BASE_KEY_NAMES = {
    "BTN_SOUTH": 0x130, "BTN_A": 0x130, "BTN_GAMEPAD": 0x130,
    "BTN_EAST": 0x131, "BTN_B": 0x131,
    "BTN_C": 0x132,
    "BTN_NORTH": 0x133, "BTN_X": 0x133,
    "BTN_WEST": 0x134, "BTN_Y": 0x134,
    "BTN_Z": 0x135,
    "BTN_TL": 0x136, "BTN_TR": 0x137, "BTN_TL2": 0x138, "BTN_TR2": 0x139,
    "BTN_SELECT": 0x13A, "BTN_START": 0x13B, "BTN_MODE": 0x13C,
    "BTN_THUMBL": 0x13D, "BTN_THUMBR": 0x13E,
    "BTN_DPAD_UP": 0x220, "BTN_DPAD_DOWN": 0x221,
    "BTN_DPAD_LEFT": 0x222, "BTN_DPAD_RIGHT": 0x223,
    "BTN_TRIGGER_HAPPY": 0x2C0,
    "KEY_HOME": 102, "KEY_POWER": 116, "KEY_MENU": 139, "KEY_SLEEP": 142,
    "KEY_WAKEUP": 143, "KEY_BACK": 158, "KEY_HOMEPAGE": 172,
}

_DEFINE_RE = re.compile(
    r"^#define\s+((?:KEY|BTN)_\w+)\s+(0x[0-9a-fA-F]+|\d+|(?:KEY|BTN)_\w+)\s*(?:/\*|$)"
)


def load_key_names():
    """BASE_KEY_NAMES, plus every KEY_*/BTN_* the running system's headers define."""
    names = dict(BASE_KEY_NAMES)
    aliases = []
    try:
        with open(KERNEL_CODE_HEADER, "r", errors="replace") as handle:
            for line in handle:
                match = _DEFINE_RE.match(line.strip())
                if not match:
                    continue
                name, value = match.groups()
                if name.endswith(("_MAX", "_CNT")):
                    continue
                if value.startswith(("KEY_", "BTN_")):
                    aliases.append((name, value))
                else:
                    names.setdefault(name, int(value, 0))
    except OSError:
        pass
    for name, target in aliases:
        if target in names:
            names.setdefault(name, names[target])
    return names


KEY_NAMES = load_key_names()

CODE_NAMES = {}
for _name, _code in KEY_NAMES.items():
    CODE_NAMES.setdefault(_code, _name)


def code_name(code):
    return CODE_NAMES.get(code, "code_%d" % code)


def fmt_codes(codes):
    return ", ".join("%s(%d)" % (code_name(c), c) for c in codes) or "none"


# --------------------------------------------------------------------------- devices


def _read_sysfs(path):
    try:
        with open(path, "r", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def parse_bitmap(text, word_bits=LONG_BITS):
    """Decode a sysfs capabilities bitmask into the set of bit numbers set in it.

    The kernel writes these as one unsigned long per word, most significant word
    first, space separated, formatted with plain "%lx" - so words are NOT zero
    padded ("7cdb000000000000 0 0 0 0") and their text length says nothing about
    the word size. It is always BITS_PER_LONG, which for a native process is
    sizeof(long) * 8. Getting this wrong shifts every button number, and does so
    silently: the daemon starts, reports itself healthy, and quietly decides
    your controller has no Home button.
    """
    words = text.split()
    if not words:
        return set()
    bits = set()
    for index, word in enumerate(reversed(words)):
        try:
            value = int(word, 16)
        except ValueError:
            continue
        base = index * word_bits
        offset = 0
        while value:
            if value & 1:
                bits.add(base + offset)
            value >>= 1
            offset += 1
    return bits


class InputDevice:
    __slots__ = ("event", "path", "name", "phys", "keys", "has_abs", "is_gamepad", "fd")

    def __init__(self, event):
        base = os.path.join(SYS_INPUT, event, "device")
        self.event = event
        self.path = os.path.join(DEV_INPUT, event)
        self.name = _read_sysfs(os.path.join(base, "name")) or event
        self.phys = _read_sysfs(os.path.join(base, "phys"))
        self.keys = parse_bitmap(_read_sysfs(os.path.join(base, "capabilities", "key")))
        self.has_abs = bool(parse_bitmap(_read_sysfs(os.path.join(base, "capabilities", "abs"))))
        self.is_gamepad = BTN_GAMEPAD in self.keys and self.has_abs
        self.fd = -1


def list_input_devices():
    try:
        entries = os.listdir(SYS_INPUT)
    except OSError:
        return []

    def sort_key(entry):
        try:
            return int(entry[len("event"):])
        except ValueError:
            return 1 << 30

    devices = []
    for entry in sorted((e for e in entries if e.startswith("event")), key=sort_key):
        device = InputDevice(entry)
        if os.path.exists(device.path):
            devices.append(device)
    return devices


# --------------------------------------------------------------------------- notify


def _session_targets():
    """(uid, passwd entry) for every logged-in user with a session bus."""
    targets = []
    try:
        entries = os.listdir("/run/user")
    except OSError:
        return targets
    for entry in entries:
        if not entry.isdigit():
            continue
        uid = int(entry)
        if uid < 1000 or not os.path.exists("/run/user/%d/bus" % uid):
            continue
        try:
            targets.append((uid, pwd.getpwuid(uid)))
        except KeyError:
            continue
    return targets


async def notify(log, summary, body, urgency="normal"):
    """Best-effort desktop notification. Shows up in Desktop Mode; gamescope
    generally swallows it in Gaming Mode, so nothing here is load-bearing."""
    for uid, entry in _session_targets():
        env = {
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/%d/bus" % uid,
            "XDG_RUNTIME_DIR": "/run/user/%d" % uid,
            "DISPLAY": ":0",
            "HOME": entry.pw_dir,
            "USER": entry.pw_name,
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        }
        try:
            proc = await asyncio.create_subprocess_exec(
                "notify-send", "-a", "cec-hdmi", "-u", urgency, summary, body,
                env=env, user=uid, group=entry.pw_gid,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=10)
        except (OSError, asyncio.TimeoutError, ValueError, TypeError) as exc:
            log("notify: could not notify %s: %s" % (entry.pw_name, exc))


# --------------------------------------------------------------------------- watcher


class ControllerWatcher:
    def __init__(self, config, log, trigger_codes):
        self.config = config
        self.log = log
        self.trigger_codes = trigger_codes
        self.open_devices = {}
        self.open_failures = {}
        self.last_trigger = float("-inf")
        self.hook_running = False
        self.loop = None
        self.netlink = None
        self.stopping = None

    # -- device bookkeeping

    def _watchable(self, device):
        matched = sorted(self.trigger_codes & device.keys)
        if not matched:
            return None
        if self.config.gamepad_only and not device.is_gamepad:
            return None
        return matched

    def _add(self, device):
        if device.path in self.open_devices:
            return
        matched = self._watchable(device)
        if matched is None:
            return
        try:
            fd = os.open(device.path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as exc:
            if self.open_failures.get(device.path) != exc.errno:
                self.open_failures[device.path] = exc.errno
                self.log("WARNING: cannot open %s [%s]: %s"
                         % (device.path, device.name, exc.strerror))
            return
        self.open_failures.pop(device.path, None)
        device.fd = fd
        self.open_devices[device.path] = device
        self.loop.add_reader(fd, self._readable, device.path)
        self.log("watching %s [%s] trigger codes: %s"
                 % (device.path, device.name, fmt_codes(matched)))

    def _drop(self, path, reason):
        device = self.open_devices.pop(path, None)
        if device is None:
            return
        try:
            self.loop.remove_reader(device.fd)
        except (OSError, ValueError):
            pass
        try:
            os.close(device.fd)
        except OSError:
            pass
        self.log("stopped watching %s [%s] (%s)" % (path, device.name, reason))

    def _scan(self):
        present = set()
        for device in list_input_devices():
            present.add(device.path)
            self._add(device)
        for path in list(self.open_devices):
            if path not in present:
                self._drop(path, "device gone")
        for path in list(self.open_failures):
            if path not in present:
                self.open_failures.pop(path, None)

    def _try_add_path(self, path):
        if path in self.open_devices or not os.path.exists(path):
            return
        event = os.path.basename(path)
        if not os.path.isdir(os.path.join(SYS_INPUT, event)):
            return
        self._add(InputDevice(event))

    # -- event handling

    def _readable(self, path):
        device = self.open_devices.get(path)
        if device is None:
            return
        try:
            data = os.read(device.fd, EVENT_SIZE * 64)
        except BlockingIOError:
            return
        except OSError as exc:
            self._drop(path, "read error: %s" % exc.strerror)
            return
        if not data:
            self._drop(path, "end of file")
            return
        for offset in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
            _sec, _usec, etype, code, value = struct.unpack_from(EVENT_FMT, data, offset)
            # value 1 is key-down; 2 is autorepeat, 0 is key-up - ignore both.
            if etype == EV_KEY and value == 1 and code in self.trigger_codes:
                self._on_press(device, code)

    def _on_press(self, device, code):
        where = "%s [%s] %s" % (device.path, device.name, code_name(code))
        if self.hook_running:
            self.log("%s: wake already in progress, ignoring" % where)
            return
        now = time.monotonic()
        elapsed = now - self.last_trigger
        if elapsed < self.config.cooldown:
            self.log("%s: within %.1fs cooldown (%.1fs left), ignoring"
                     % (where, self.config.cooldown, self.config.cooldown - elapsed))
            return
        self.last_trigger = now
        if self.config.dry_run:
            self.log("%s: DRY RUN - would run '%s on'" % (where, HOOK_SCRIPT))
            return
        self.log("%s: triggering wake ('%s on')" % (where, HOOK_SCRIPT))
        self.hook_running = True
        self.loop.create_task(self._run_hook())

    async def _run_hook(self):
        """Run cec-hook out of band.

        A wake can take tens of seconds when the tunneling fix and the retry
        escalation kick in, and the input loop has to keep reading throughout -
        which is why this stays a subprocess rather than an import.
        """
        started = time.monotonic()
        status = -1
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, HOOK_SCRIPT, "on", "--quiet",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            status = await proc.wait()
        except OSError as exc:
            self.log("ERROR: could not run '%s on': %s" % (HOOK_SCRIPT, exc))
        finally:
            self.hook_running = False

        took = time.monotonic() - started
        if status == 0:
            self.log("wake finished OK in %.1fs" % took)
            if self.config.notify_on_trigger:
                await notify(self.log, "TV woken", "HDMI-CEC wake sent from the controller.")
        else:
            self.log("ERROR: wake failed (exit %d) after %.1fs - see %s"
                     % (status, took, os.path.join(CEC_DIR, "cec-hook.log")))
            if self.config.notify_on_failure:
                await notify(self.log, "HDMI-CEC wake failed",
                             "The TV did not acknowledge. See cec-hook.log.", "critical")

    # -- hotplug

    def _start_netlink(self):
        try:
            sock = socket.socket(socket.AF_NETLINK, socket.SOCK_DGRAM, NETLINK_KOBJECT_UEVENT)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
            sock.bind((0, UEVENT_KERNEL_GROUP))
            sock.setblocking(False)
        except OSError as exc:
            self.log("hotplug: netlink uevent socket unavailable (%s); relying on the "
                     "%.0fs rescan instead" % (exc, self.config.rescan))
            return
        self.netlink = sock
        self.loop.add_reader(sock.fileno(), self._netlink_readable)
        self.log("hotplug: listening on the kernel netlink uevent socket")

    def _netlink_readable(self):
        while True:
            try:
                data = self.netlink.recv(1 << 16)
            except (BlockingIOError, InterruptedError):
                return
            except OSError as exc:
                self.log("hotplug: netlink read failed (%s), falling back to rescans" % exc)
                try:
                    self.loop.remove_reader(self.netlink.fileno())
                    self.netlink.close()
                except (OSError, ValueError):
                    pass
                self.netlink = None
                return
            action = devname = subsystem = None
            for field in data.split(b"\x00"):
                if field.startswith(b"ACTION="):
                    action = field[7:].decode("utf-8", "replace")
                elif field.startswith(b"DEVNAME="):
                    devname = field[8:].decode("utf-8", "replace")
                elif field.startswith(b"SUBSYSTEM="):
                    subsystem = field[10:].decode("utf-8", "replace")
            if subsystem != "input" or not devname:
                continue
            # DEVNAME is a POSIX path relative to /dev, e.g. "input/event7".
            path = devname if devname.startswith("/") else "/dev/" + devname
            if not path.startswith(DEV_INPUT + "/event"):
                continue
            if action == "add":
                # The kernel announces the node before udev chmods it, so retry.
                for delay in HOTPLUG_RETRY_DELAYS:
                    self.loop.call_later(delay, self._try_add_path, path)
            elif action == "remove":
                self._drop(path, "unplugged")

    # -- lifecycle

    async def _rescan_loop(self):
        while True:
            await asyncio.sleep(self.config.rescan)
            # Never let one bad scan kill the task: it is the fallback that
            # keeps hotplug working if the netlink socket is gone.
            try:
                self._scan()
            except OSError as exc:
                self.log("WARNING: device rescan failed: %s" % exc)

    def stop(self, reason):
        self.log("stopping (%s)" % reason)
        if not self.stopping.done():
            self.stopping.set_result(None)

    async def run(self):
        self.loop = asyncio.get_running_loop()
        self.stopping = self.loop.create_future()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                self.loop.add_signal_handler(sig, self.stop, signal.Signals(sig).name)
            except (NotImplementedError, RuntimeError):
                pass

        self.log("cec-watch %s starting (config: %s)" % (VERSION, self.config.path))
        for problem in self.config.problems:
            self.log("config: %s" % problem)
        self.log("trigger codes: %s" % fmt_codes(sorted(self.trigger_codes)))
        self.log("cooldown: %.1fs | gamepad-only: %s | dry-run: %s"
                 % (self.config.cooldown,
                    "yes" if self.config.gamepad_only else "no",
                    "yes" if self.config.dry_run else "no"))

        self._start_netlink()
        self._scan()
        if not self.open_devices:
            self.log("no controllers with a trigger button are connected yet - waiting "
                     "(run with --detect to see what is present)")

        rescan = self.loop.create_task(self._rescan_loop())
        try:
            await self.stopping
        finally:
            rescan.cancel()
            for path in list(self.open_devices):
                self._drop(path, "shutting down")
            if self.netlink is not None:
                try:
                    self.loop.remove_reader(self.netlink.fileno())
                    self.netlink.close()
                except (OSError, ValueError):
                    pass
        return 0


# --------------------------------------------------------------------------- detect


def run_detect(config, trigger_codes, unknown_codes):
    """List every input device and say exactly which ones the daemon watches."""
    print("cec-watch %s" % VERSION)
    print("config:         %s" % config.path)
    for problem in config.problems:
        print("  config note:  %s" % problem)
    print("trigger codes:  %s" % fmt_codes(sorted(trigger_codes)))
    if unknown_codes:
        print("  not defined by this kernel, ignored: %s" % ", ".join(unknown_codes))
    print("gamepad-only:   %s" % ("yes" if config.gamepad_only else "no"))
    print("cooldown:       %.1fs%s"
          % (config.cooldown, "  (DRY RUN - hook will not be called)"
             if config.dry_run else ""))
    if os.geteuid() != 0:
        print("\nNOTE: not running as root; /dev/input/event* is usually root:input 0660,")
        print("      so 'readable' below will say no. Re-run with sudo for a true picture.")
    print()

    devices = list_input_devices()
    if not devices:
        print("No input devices found under %s." % SYS_INPUT)
        return 1

    watched = 0
    for device in devices:
        matched = sorted(trigger_codes & device.keys)
        try:
            fd = os.open(device.path, os.O_RDONLY | os.O_NONBLOCK)
            os.close(fd)
            readable, why = True, ""
        except OSError as exc:
            readable, why = False, " (%s)" % exc.strerror

        if not matched:
            verdict = "ignored - no trigger button"
        elif config.gamepad_only and not device.is_gamepad:
            verdict = "ignored - GAMEPAD_ONLY=1 and this is not a gamepad"
        elif not readable:
            verdict = "WOULD FAIL - cannot open%s" % why
        else:
            verdict = "WATCHED"
            watched += 1

        print("%-22s %s" % (device.path, device.name))
        if device.phys:
            print("    phys:      %s" % device.phys)
        print("    gamepad:   %-4s readable: %s%s"
              % ("yes" if device.is_gamepad else "no", "yes" if readable else "no", why))
        print("    triggers:  %s" % fmt_codes(matched))
        print("    -> %s" % verdict)
        print()

    print("%d input device(s), %d watched." % (len(devices), watched))
    if watched == 0:
        print()
        print("Nothing would trigger a wake. Things to try:")
        print("  * connect a controller and re-run")
        print("  * sudo %s --monitor   then press Home and see what code appears"
              % os.path.abspath(sys.argv[0]))
        print("  * add that code to BUTTON_CODES in %s" % config.path)
        return 1
    return 0


# --------------------------------------------------------------------------- monitor


def run_monitor(config, trigger_codes):
    """Print every key event from every readable input device.

    This is the tool for the open question Steam raises on SteamOS: whether the
    Guide/Home button is visible on the raw evdev node while Steam is running,
    or whether Steam consumes it first. Run it in Desktop Mode and again with
    Gaming Mode on screen (over SSH - /dev/input does not care which UI is in
    front), press Home, and see whether an event shows up.

    Nothing here grabs a device (no EVIOCGRAB), so Steam's own input handling,
    the overlay and games are unaffected while this runs.
    """
    import selectors

    if os.geteuid() != 0:
        print("NOTE: not running as root; most /dev/input/event* nodes will be "
              "unreadable. Re-run with sudo.\n")

    print("cec-watch %s - live key monitor" % VERSION)
    print("Passive read only, no EVIOCGRAB: Steam keeps full control of input.")
    print("Press buttons on your controller. Ctrl-C to stop.\n")

    selector = selectors.DefaultSelector()
    watching = {}

    def refresh():
        present = set()
        for device in list_input_devices():
            present.add(device.path)
            if device.path in watching or not device.keys:
                continue
            try:
                fd = os.open(device.path, os.O_RDONLY | os.O_NONBLOCK)
            except OSError:
                continue
            device.fd = fd
            watching[device.path] = device
            selector.register(fd, selectors.EVENT_READ, device)
            triggers = sorted(trigger_codes & device.keys)
            print("+ %-22s %s%s"
                  % (device.path, device.name,
                     "   [has trigger: %s]" % fmt_codes(triggers) if triggers else ""))
        for path in list(watching):
            if path not in present:
                device = watching.pop(path)
                try:
                    selector.unregister(device.fd)
                    os.close(device.fd)
                except (OSError, KeyError, ValueError):
                    pass
                print("- %-22s %s" % (path, device.name))

    refresh()
    if not watching:
        print("No readable input devices with key capabilities found.")
        return 1
    print()

    last_refresh = time.monotonic()
    try:
        while True:
            for key, _mask in selector.select(timeout=2.0):
                device = key.data
                try:
                    data = os.read(device.fd, EVENT_SIZE * 64)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    data = b""
                if not data:
                    try:
                        selector.unregister(device.fd)
                        os.close(device.fd)
                    except (OSError, KeyError, ValueError):
                        pass
                    watching.pop(device.path, None)
                    continue
                for offset in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
                    _s, _u, etype, code, value = struct.unpack_from(EVENT_FMT, data, offset)
                    if etype != EV_KEY:
                        continue
                    action = {0: "release", 1: "PRESS", 2: "repeat"}.get(value, str(value))
                    mark = "  <-- would trigger a wake" if (
                        value == 1 and code in trigger_codes) else ""
                    print("%s  %-22s %-28s %-16s %-7s code=%d%s"
                          % (time.strftime("%H:%M:%S"), device.path, device.name[:28],
                             code_name(code), action, code, mark))
            now = time.monotonic()
            if now - last_refresh >= 2.0:
                last_refresh = now
                refresh()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        for device in watching.values():
            try:
                os.close(device.fd)
            except OSError:
                pass
        selector.close()
    return 0


# --------------------------------------------------------------------------- main


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="cec-watch",
        description="Wake the TV over HDMI-CEC when a controller's Home/Guide "
                    "button is pressed.",
        epilog="With no mode flag it runs as a daemon, which is what "
               "cec-hdmi-controller.service does.",
    )
    parser.add_argument("--detect", action="store_true",
                        help="list input devices and which would be watched, then exit")
    parser.add_argument("--monitor", action="store_true",
                        help="print key events live to find your Home button's code")
    parser.add_argument("--dry-run", action="store_true",
                        help="log 'would trigger' instead of running cec-hook "
                             "(overrides DRY_RUN in the config)")
    parser.add_argument("--config", default=DEFAULT_CONFIG, metavar="PATH",
                        help="config file to read (default: %(default)s)")
    parser.add_argument("--log", default=DEFAULT_LOG, metavar="PATH",
                        help="log file to append to (default: %(default)s)")
    parser.add_argument("--version", action="version", version="cec-watch " + VERSION)
    args = parser.parse_args(argv)

    if args.detect and args.monitor:
        parser.error("--detect and --monitor are mutually exclusive")

    config = Config(args.config)
    if args.dry_run:
        config.dry_run = True
    trigger_codes, unknown_codes = config.button_codes(KEY_NAMES)

    if args.detect:
        return run_detect(config, trigger_codes, unknown_codes)
    if args.monitor:
        return run_monitor(config, trigger_codes)

    log = Logger(args.log, tag="cec-watch",
                 max_bytes=config.log_max_bytes, keep=config.log_keep)
    if unknown_codes:
        log("config: BUTTON_CODES entries this kernel does not define, ignored: %s"
            % ", ".join(unknown_codes))
    try:
        return asyncio.run(ControllerWatcher(config, log, trigger_codes).run())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
