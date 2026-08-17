"""Shared scaffolding for the test suites.

Everything the project ships targets Linux, but the parts worth testing are
pure: frame bytes, the wake plan, config parsing, sysfs bitmask decoding. A few
Unix-only attributes are stubbed here so the whole suite runs anywhere python3
does - including a Windows dev box - which is the point. You can validate a
change before pushing it to the SteamOS machine.

The one thing that genuinely needs hardware is the ioctl call itself, and every
one of those goes through CecDevice._ioctl. FakeAdapter below replaces exactly
that method and nothing else, so the tests drive the real transmit path, the
real struct packing and the real state machine against a scripted bus.
"""

import errno
import importlib.util
import os
import struct
import sys
import types

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(REPO_ROOT, "src")
HOOK_SRC = os.path.join(SRC_DIR, "cec-hook.py")
WATCH_SRC = os.path.join(SRC_DIR, "cec-watch.py")
CONFIG_SRC = os.path.join(REPO_ROOT, "config", "config.conf.default")
UNIT_DIR = os.path.join(REPO_ROOT, "systemd")
INSTALLER = os.path.join(REPO_ROOT, "install.sh")
VERSION_FILE = os.path.join(REPO_ROOT, "VERSION")

# Loading modules by path would otherwise drop a __pycache__ into src/.
sys.dont_write_bytecode = True


def _stub_unix_only():
    if "pwd" not in sys.modules:
        stub = types.ModuleType("pwd")
        stub.getpwuid = lambda uid: None
        sys.modules["pwd"] = stub
    if not hasattr(os, "O_NONBLOCK"):
        os.O_NONBLOCK = 0
    if not hasattr(os, "geteuid"):
        os.geteuid = lambda: 0


_stub_unix_only()
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


def load_entry_point(path, name):
    """Import one of the hyphenated entry points, whose filenames are not valid
    Python identifiers and so cannot go through a plain import."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_watcher():
    return load_entry_point(WATCH_SRC, "cec_watch_entry")


def read_version():
    with open(VERSION_FILE) as handle:
        return handle.read().strip()


# --------------------------------------------------------------------------- fake bus


class FakeAdapter:
    """A CEC bus in a dictionary.

    Replaces CecDevice._ioctl, so every layer above it - struct packing, reply
    handling, transmit-status decoding, the whole wake plan - is the real code.

    devices maps logical address -> dict of answers:
        power:      a CEC power status byte, or None to not answer at all
        audio_mode: 0/1 for an audio system, or None
        osd_name:   bytes
    Any address not in the dict NACKs, which is how "there is no receiver on
    this bus" is expressed.
    """

    def __init__(self, devices=None, phys_addr=0x3000, logical_addr=4,
                 adapter_name="DP-1", active_source=None, caps=0x4,
                 configured=None):
        import cec_device as dev

        self.dev = dev
        self.devices = devices or {}
        self.phys_addr = phys_addr
        self.logical_addr = logical_addr
        self.adapter_name = adapter_name
        self.active_source = active_source
        self.caps = caps

        # What the adapter already holds when we arrive. The kernel's own
        # drm_dp_cec driver claims an address when it reads the EDID, so a real
        # adapter is usually already configured before this project opens it -
        # and CEC_ADAP_S_LOG_ADDRS returns EBUSY on one that is. None means
        # unconfigured; a dict means configured with those settings.
        self.configured_state = configured

        self.sent = []          # every Frame handed to transmit, in order
        self.configured = 0     # how many times a logical address was claimed
        self.calls = []         # ("configure"|"transmit"|..., detail) call order
        self.pending = []       # frames queued for the next receive()

    # -- the seam

    def ioctl(self, request, payload):
        if request == self.dev.ADAP_G_CAPS:
            packed = struct.pack(self.dev.CAPS_FMT, b"fake",
                                 self.adapter_name.encode(), 1, self.caps, 0)
            payload[:] = packed
            return 0
        if request == self.dev.ADAP_G_PHYS_ADDR:
            payload[:] = struct.pack("=H", self.phys_addr)
            return 0
        if request == self.dev.S_MODE:
            return 0
        if request == self.dev.ADAP_S_LOG_ADDRS:
            return self._set_log_addrs(payload)
        if request == self.dev.ADAP_G_LOG_ADDRS:
            return self._get_log_addrs(payload)
        if request == self.dev.TRANSMIT:
            return self._transmit(payload)
        if request == self.dev.RECEIVE:
            return self._receive(payload)
        raise OSError(22, "unexpected ioctl 0x%X" % (request & 0xFFFFFFFF))

    # -- adapter state

    def _log_addrs_blob(self, count=1, state=None):
        state = state or {}
        addr = self.logical_addr if count else 0xFF
        name = state.get("osd_name", "SteamOS").encode()[:14]
        return struct.pack(self.dev.LOG_ADDRS_FMT,
                           bytes([addr, 0xFF, 0xFF, 0xFF]), 1 << addr if count else 0,
                           state.get("version", 5), count, 0xFFFFFF, 0, name,
                           bytes([state.get("primary", 4), 0, 0, 0]),
                           b"\x03\0\0\0", b"\x10\0\0\0", b"\0" * 48)

    def _get_log_addrs(self, payload):
        if self.configured_state is None:
            payload[:] = self._log_addrs_blob(count=0)
        else:
            payload[:] = self._log_addrs_blob(count=1, state=self.configured_state)
        return 0

    def _set_log_addrs(self, payload):
        fields = struct.unpack(self.dev.LOG_ADDRS_FMT, bytes(payload))
        count = fields[3]
        if not count:
            self.calls.append(("clear", None))
            self.configured_state = None
            payload[:] = self._log_addrs_blob(count=0)
            return 0

        # The kernel refuses to set logical addresses on an adapter that is
        # already configured. Clearing first is the only legal route.
        if self.configured_state is not None:
            raise OSError(errno.EBUSY, "Device or resource busy")

        self.configured += 1
        self.calls.append(("configure", self.logical_addr))
        self.configured_state = {
            "osd_name": fields[6].split(b"\0")[0].decode(),
            "version": fields[2],
            "primary": fields[7][0],
        }
        payload[:] = self._log_addrs_blob(count=1, state=self.configured_state)
        return 0

    # -- frames

    def _transmit(self, payload):
        import cec_frames as frames

        fields = struct.unpack(self.dev.MSG_FMT, bytes(payload))
        length, msg, reply_op = fields[2], fields[6], fields[7]
        data = msg[:length]
        frame = frames.Frame(data)
        self.sent.append(frame)
        self.calls.append(("transmit", frame.hex()))

        follower = frame.follower
        known = follower in self.devices or follower == frames.LA_BROADCAST
        tx_status = self.dev.TX_OK if known else self.dev.TX_NACK
        nack_cnt = 0 if known else 1

        reply_bytes, rx_status, rx_len = b"", 0, 0
        if reply_op and known:
            answer = self._answer(frame, reply_op)
            if answer is not None:
                reply_bytes = answer
                rx_status = self.dev.RX_OK
                rx_len = len(answer)

        payload[:] = struct.pack(
            self.dev.MSG_FMT, 0, 0, rx_len or length, 0, 0, 0,
            (reply_bytes or data).ljust(16, b"\0"), reply_op,
            rx_status, tx_status, 0, nack_cnt, 0, 0)
        return 0

    def _answer(self, frame, reply_op):
        import cec_frames as frames

        device = self.devices.get(frame.follower, {})
        header = bytes([(frame.follower << 4) | frame.initiator])

        if reply_op == frames.OP_REPORT_POWER_STATUS:
            power = device.get("power")
            if power is None:
                return None
            return header + bytes([frames.OP_REPORT_POWER_STATUS, power])
        if reply_op == frames.OP_SET_SYSTEM_AUDIO_MODE:
            return header + bytes([frames.OP_SET_SYSTEM_AUDIO_MODE, 1])
        if reply_op == frames.OP_SYSTEM_AUDIO_MODE_STATUS:
            mode = device.get("audio_mode")
            if mode is None:
                return None
            return header + bytes([frames.OP_SYSTEM_AUDIO_MODE_STATUS, int(mode)])
        if reply_op == frames.OP_SET_OSD_NAME:
            name = device.get("osd_name")
            if not name:
                return None
            return header + bytes([frames.OP_SET_OSD_NAME]) + name
        return None

    def _receive(self, payload):
        """Only <Active Source> broadcasts arrive this way, in answer to a
        <Request Active Source> - a broadcast question answered by a broadcast,
        so there is no transmit for it to ride back on."""
        import cec_frames as frames

        # ETIMEDOUT by symbol, not by number: it is 110 on Linux and 138 on
        # Windows, and this suite is meant to run on both.
        if self.active_source is None:
            raise OSError(errno.ETIMEDOUT, "timed out")
        data = bytes([0x0F, frames.OP_ACTIVE_SOURCE,
                      (self.active_source >> 8) & 0xFF, self.active_source & 0xFF])
        # Answered once per request, like a real bus.
        self.active_source_consumed = True
        payload[:] = struct.pack(self.dev.MSG_FMT, 0, 0, len(data), 0, 0, 0,
                                 data.ljust(16, b"\0"), 0, self.dev.RX_OK, 0,
                                 0, 0, 0, 0)
        return 0

    # -- construction

    def device(self, path="/dev/cec0"):
        """A CecDevice wired to this fake, already opened."""
        import cec_device

        fake = self

        class _Device(cec_device.CecDevice):
            def _ioctl(self, request, payload):
                return fake.ioctl(request, payload)

            def open(self):
                self.fd = 3
                self._read_caps()
                return self

            def close(self):
                self.fd = -1

        return _Device(path).open()


class FakeDpcd:
    """Stands in for the DisplayPort tunneling fix, recording call order.

    The order is the point: claiming a logical address resets the adapter and
    clears DPCD 0x3001, so every enable must come after every configure. Tests
    assert against .calls to pin that.
    """

    def __init__(self, available=True):
        self.available = available
        self.aux_path = "/dev/drm_dp_aux0" if available else None
        self.connector = "/sys/class/drm/card0-DP-1" if available else None
        self.calls = []
        self.enabled = False

    def ensure_enabled(self):
        self.calls.append("enable")
        self.enabled = True
        return True

    def reprobe_connector(self):
        self.calls.append("reprobe")
        self.enabled = False       # a re-probe drops the bit, like real hardware
        return True

    def status(self):
        return (1 if self.enabled else 0), "fake tunneling"

    def connector_status(self):
        return "connected"


def make_controller(adapter, config=None, dpcd=None, log=None):
    """A Controller wired to a FakeAdapter, opened without touching hardware."""
    import cec_control

    controller = cec_control.Controller(config or make_config(), log or (lambda _m: None))
    controller.device = adapter.device()
    controller.dpcd = dpcd or FakeDpcd()
    controller._claim_address()
    return controller


def make_config(**overrides):
    """A Config carrying the shipped defaults, with keyword overrides applied.

    Timing is dialled right down: these tests exercise decisions, not delays,
    and the defaults would otherwise spend real seconds waiting for replies a
    fake bus answers instantly.
    """
    import cec_config

    config = cec_config.Config("<no such config file>")
    config.problems = []
    config.frame_gap_ms = 0
    config.reply_timeout_ms = 20
    config.wake_settle_ms = 0
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


# --------------------------------------------------------------------------- checks


class Checks:
    """Tiny assertion recorder - keeps output readable without pulling in a test
    framework the SteamOS box would have to have installed."""

    def __init__(self):
        self.failures = []
        self.passed = 0

    def section(self, title):
        print("== %s ==" % title)

    def __call__(self, label, got, want):
        if got == want:
            self.passed += 1
            print("  ok   %s" % label)
        else:
            self.failures.append(label)
            print("  FAIL %s\n         got:  %r\n         want: %r" % (label, got, want))

    def finish(self):
        print()
        if self.failures:
            print("%d of %d checks FAILED: %s"
                  % (len(self.failures), len(self.failures) + self.passed,
                     ", ".join(self.failures)))
            return 1
        print("%d checks passed" % self.passed)
        return 0


def mask_for(bits, module):
    """Render bit numbers the way the kernel renders a capabilities bitmap: one
    unsigned long per word, most significant first, "%lx" so nothing is zero
    padded. Word size follows the module so the fixture stays honest on both
    LP64 and 32-bit hosts."""
    word_bits = module.LONG_BITS
    value = 0
    for bit in bits:
        value |= 1 << bit
    words = (max(bits) // word_bits + 1) if bits else 1
    return " ".join(
        "%x" % ((value >> (index * word_bits)) & ((1 << word_bits) - 1))
        for index in range(words - 1, -1, -1)
    )
