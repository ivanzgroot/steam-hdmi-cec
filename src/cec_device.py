"""The CEC adapter itself: /dev/cecN, driven through the kernel's ioctl API.

This is the only module in the project that touches hardware, and every call it
makes goes through one method - CecDevice._ioctl. That single seam is what lets
the test suite run the whole wake state machine against a scripted fake adapter
on a machine with no CEC hardware, or no Linux.

The ABI here is linux/cec.h, which is stable public uapi. Rather than hardcode
the ioctl numbers, we compute them the way the kernel's own macros do, from the
sizes of the structures below - so if a structure is ever declared wrongly the
ioctl fails loudly with EINVAL instead of quietly reading the wrong bytes.
"""

import errno
import os
import struct
import time

try:
    import fcntl
except ImportError:
    # Not Linux. Everything in this module except the _ioctl call itself is
    # pure - the ABI layout, the ioctl numbers, the transmit-status decoding -
    # so the test suite can still exercise all of it against a scripted fake
    # adapter on a Windows or macOS dev box. Only real hardware needs fcntl.
    fcntl = None

from cec_frames import Frame, PHYS_ADDR_INVALID, format_phys_addr

DEFAULT_DEVICE = "/dev/cec0"

# --------------------------------------------------------------------------- ABI

# struct cec_msg
#   __u64 tx_ts, rx_ts; __u32 len, timeout, sequence, flags;
#   __u8 msg[16], reply, rx_status, tx_status,
#        tx_arb_lost_cnt, tx_nack_cnt, tx_low_drive_cnt, tx_error_cnt;
MSG_FMT = "=QQIIII16s7Bx"
MSG_SIZE = struct.calcsize(MSG_FMT)

# struct cec_caps { char driver[32], name[32]; __u32 available_log_addrs,
#                   capabilities, version; }
CAPS_FMT = "=32s32sIII"
CAPS_SIZE = struct.calcsize(CAPS_FMT)

# struct cec_log_addrs
LOG_ADDRS_FMT = "=4sHBBII15s4s4s4s48sx"
LOG_ADDRS_SIZE = struct.calcsize(LOG_ADDRS_FMT)

# Pinned so a wrong format string fails here, at import, and not as a confusing
# EINVAL from the kernel three layers down. tests/test_device.py asserts these
# against the values in linux/cec.h.
assert MSG_SIZE == 56, MSG_SIZE
assert CAPS_SIZE == 76, CAPS_SIZE
assert LOG_ADDRS_SIZE == 92, LOG_ADDRS_SIZE

_IOC_NONE, _IOC_WRITE, _IOC_READ = 0, 1, 2


def _ioc(direction, type_char, number, size):
    """The asm-generic _IOC encoding, plus the wrap into a signed 32-bit int
    that fcntl.ioctl expects for any request with the read+write bits set."""
    value = (direction << 30) | (size << 16) | (ord(type_char) << 8) | number
    if value >= 1 << 31:
        value -= 1 << 32
    return value


ADAP_G_CAPS = _ioc(_IOC_READ | _IOC_WRITE, "a", 0, CAPS_SIZE)
ADAP_G_PHYS_ADDR = _ioc(_IOC_READ, "a", 1, 2)
ADAP_G_LOG_ADDRS = _ioc(_IOC_READ, "a", 3, LOG_ADDRS_SIZE)
ADAP_S_LOG_ADDRS = _ioc(_IOC_READ | _IOC_WRITE, "a", 4, LOG_ADDRS_SIZE)
TRANSMIT = _ioc(_IOC_READ | _IOC_WRITE, "a", 5, MSG_SIZE)
RECEIVE = _ioc(_IOC_READ | _IOC_WRITE, "a", 6, MSG_SIZE)
S_MODE = _ioc(_IOC_WRITE, "a", 9, 4)

# Transmit result bits. The whole reason for this rewrite: the kernel says
# exactly what happened to a frame instead of a CLI printing a sentence about it.
TX_OK = 1 << 0
TX_ARB_LOST = 1 << 1
TX_NACK = 1 << 2
TX_LOW_DRIVE = 1 << 3
TX_ERROR = 1 << 4
TX_MAX_RETRIES = 1 << 5
TX_ABORTED = 1 << 6
TX_TIMEOUT = 1 << 7

TX_NAMES = [
    (TX_OK, "acknowledged"),
    (TX_ARB_LOST, "lost bus arbitration"),
    (TX_NACK, "not acknowledged"),
    (TX_LOW_DRIVE, "low drive (bus contention)"),
    (TX_ERROR, "line error"),
    (TX_MAX_RETRIES, "gave up after max retries"),
    (TX_ABORTED, "aborted"),
    (TX_TIMEOUT, "timed out"),
]

RX_OK = 1 << 0
RX_TIMEOUT = 1 << 1
RX_FEATURE_ABORT = 1 << 2
RX_ABORTED = 1 << 3

MODE_INITIATOR = 0x1
MODE_FOLLOWER = 0x1 << 4

CAP_PHYS_ADDR = 1 << 0
CAP_LOG_ADDRS = 1 << 1
CAP_TRANSMIT = 1 << 2
CAP_MONITOR_ALL = 1 << 5
CAP_NEEDS_HPD = 1 << 6

# cec_log_addrs.primary_device_type / log_addr_type
PRIM_DEVTYPE_PLAYBACK = 4
PRIM_DEVTYPE_TUNER = 3
PRIM_DEVTYPE_RECORD = 1
LOG_ADDR_TYPE_PLAYBACK = 3
LOG_ADDR_TYPE_TUNER = 2
LOG_ADDR_TYPE_RECORD = 1
ALL_DEVTYPE_PLAYBACK = 1 << 4
ALL_DEVTYPE_TUNER = 1 << 5
ALL_DEVTYPE_RECORD = 1 << 6

# What we may register ourselves as. Playback is the honest choice for a games
# console and is what every streaming box on the market uses, which matters:
# TVs routinely treat device types differently, and being the same type as the
# device that already works on your TV is the cheapest thing to try.
DEVICE_TYPES = {
    "playback": (PRIM_DEVTYPE_PLAYBACK, LOG_ADDR_TYPE_PLAYBACK, ALL_DEVTYPE_PLAYBACK),
    "tuner": (PRIM_DEVTYPE_TUNER, LOG_ADDR_TYPE_TUNER, ALL_DEVTYPE_TUNER),
    "recorder": (PRIM_DEVTYPE_RECORD, LOG_ADDR_TYPE_RECORD, ALL_DEVTYPE_RECORD),
}

CEC_VERSION_1_4 = 5
CEC_VERSION_2_0 = 6

CEC_VERSIONS = {"1.4": CEC_VERSION_1_4, "2.0": CEC_VERSION_2_0}


class CecError(Exception):
    """Anything that stops us talking to the adapter at all."""


# --------------------------------------------------------------------------- results


class TxResult:
    """What became of one transmitted frame."""

    __slots__ = ("frame", "status", "nack_cnt", "arb_lost_cnt",
                 "low_drive_cnt", "error_cnt", "reply")

    def __init__(self, frame, status, nack_cnt=0, arb_lost_cnt=0,
                 low_drive_cnt=0, error_cnt=0, reply=None):
        self.frame = frame
        self.status = status
        self.nack_cnt = nack_cnt
        self.arb_lost_cnt = arb_lost_cnt
        self.low_drive_cnt = low_drive_cnt
        self.error_cnt = error_cnt
        self.reply = reply

    @property
    def ok(self):
        return bool(self.status & TX_OK)

    @property
    def nacked(self):
        """Nobody at that address answered. For a poll this is information, not
        a fault; for a directed message it means the device is not listening."""
        return bool(self.status & TX_NACK)

    @property
    def contended(self):
        """The bus was busy or noisy rather than the device being absent - worth
        an immediate plain retry, unlike a NACK which needs the adapter poking."""
        return bool(self.status & (TX_ARB_LOST | TX_LOW_DRIVE | TX_ERROR))

    def why(self):
        reasons = [text for bit, text in TX_NAMES if self.status & bit]
        detail = []
        if self.nack_cnt:
            detail.append("nack x%d" % self.nack_cnt)
        if self.arb_lost_cnt:
            detail.append("arb-lost x%d" % self.arb_lost_cnt)
        if self.low_drive_cnt:
            detail.append("low-drive x%d" % self.low_drive_cnt)
        if self.error_cnt:
            detail.append("error x%d" % self.error_cnt)
        text = ", ".join(reasons) or "status 0x%02X" % self.status
        return "%s%s" % (text, " [%s]" % ", ".join(detail) if detail else "")


# --------------------------------------------------------------------------- device


class CecDevice:
    """An open CEC adapter.

    Use as a context manager:

        with CecDevice("/dev/cec0") as dev:
            dev.configure("SteamOS", "playback")
            dev.transmit(frames.image_view_on(dev.logical_addr))
    """

    def __init__(self, path=DEFAULT_DEVICE):
        self.path = path
        self.fd = -1
        self.logical_addr = None
        self.physical_addr = None
        self.caps = 0
        self.driver = ""
        self.name = ""

    # -- the one seam; tests replace this and nothing else

    def _ioctl(self, request, payload):
        if fcntl is None:
            raise CecError("ioctl is only available on Linux")
        return fcntl.ioctl(self.fd, request, payload)

    # -- lifecycle

    def open(self):
        try:
            self.fd = os.open(self.path, os.O_RDWR)
        except OSError as exc:
            raise CecError("cannot open %s: %s" % (self.path, exc.strerror))
        try:
            self._read_caps()
            # Follower mode as well as initiator: broadcasts such as
            # <Active Source> are addressed to everybody, and without it the
            # kernel would only hand us direct replies to our own frames.
            self._ioctl(S_MODE, struct.pack("=I", MODE_INITIATOR | MODE_FOLLOWER))
        except Exception:
            self.close()
            raise
        return self

    def close(self):
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1

    def __enter__(self):
        return self.open()

    def __exit__(self, *_exc):
        self.close()
        return False

    @staticmethod
    def wait_for_device(path=DEFAULT_DEVICE, timeout=20.0, interval=0.5):
        """Boot races the adapter's creation; give it a moment to appear."""
        deadline = time.monotonic() + timeout
        while not os.path.exists(path):
            if time.monotonic() >= deadline:
                return False
            time.sleep(interval)
        return True

    # -- adapter state

    def _read_caps(self):
        buf = bytearray(CAPS_SIZE)
        self._ioctl(ADAP_G_CAPS, buf)
        driver, name, _avail, caps, _version = struct.unpack(CAPS_FMT, bytes(buf))
        self.driver = driver.split(b"\0")[0].decode("utf-8", "replace")
        self.name = name.split(b"\0")[0].decode("utf-8", "replace")
        self.caps = caps

    def read_phys_addr(self):
        """Our own physical address, as the kernel derived it from the EDID.

        0xFFFF means unconfigured - normally that the display is not connected
        or has not been probed yet. It is returned as None so that a caller
        cannot mistake it for a real address.
        """
        buf = bytearray(2)
        self._ioctl(ADAP_G_PHYS_ADDR, buf)
        (value,) = struct.unpack("=H", bytes(buf))
        self.physical_addr = None if value == PHYS_ADDR_INVALID else value
        return self.physical_addr

    def read_log_addrs(self):
        """Our claimed logical address, or None while unconfigured."""
        buf = bytearray(LOG_ADDRS_SIZE)
        self._ioctl(ADAP_G_LOG_ADDRS, buf)
        fields = struct.unpack(LOG_ADDRS_FMT, bytes(buf))
        addrs, _mask, _version, count = fields[0], fields[1], fields[2], fields[3]
        self.logical_addr = addrs[0] if count else None
        return self.logical_addr

    def configure(self, osd_name="SteamOS", device_type="playback",
                  cec_version="1.4", vendor_id=None):
        """Claim a logical address on the bus.

        This is the expensive operation in CEC - the adapter polls candidate
        addresses to find a free one - so it is done once per run and never per
        frame. It is also destructive on a DisplayPort adapter: see cec_dpcd,
        whose tunneling bit this clears as a side effect. Anything that calls
        this must re-enable tunneling afterwards, never before.
        """
        primary, addr_type, all_types = DEVICE_TYPES.get(
            device_type, DEVICE_TYPES["playback"])
        version = CEC_VERSIONS.get(str(cec_version), CEC_VERSION_1_4)

        name = osd_name.encode("utf-8", "replace")[:14]
        payload = struct.pack(
            LOG_ADDRS_FMT,
            b"\xff\xff\xff\xff",                 # log_addr[4], kernel fills these in
            0,                                    # log_addr_mask, likewise
            version,
            1,                                    # num_log_addrs: we want exactly one
            int(vendor_id) if vendor_id else 0xFFFFFF,
            0,                                    # flags
            name,
            bytes([primary, 0, 0, 0]),
            bytes([addr_type, 0, 0, 0]),
            bytes([all_types, 0, 0, 0]),
            b"\0" * 48,                           # features
        )
        buf = bytearray(payload)
        self._ioctl(ADAP_S_LOG_ADDRS, buf)

        fields = struct.unpack(LOG_ADDRS_FMT, bytes(buf))
        addrs, count = fields[0], fields[3]
        self.logical_addr = addrs[0] if count else None
        if self.logical_addr is None or self.logical_addr > 15:
            raise CecError("the adapter could not claim a logical address "
                           "(nothing on the bus answered)")
        return self.logical_addr

    def clear_log_addrs(self):
        """Release our logical address. Used only by the re-init escalation."""
        payload = struct.pack(LOG_ADDRS_FMT, b"\xff\xff\xff\xff", 0,
                              CEC_VERSION_1_4, 0, 0xFFFFFF, 0, b"", b"\0" * 4,
                              b"\0" * 4, b"\0" * 4, b"\0" * 48)
        self._ioctl(ADAP_S_LOG_ADDRS, bytearray(payload))
        self.logical_addr = None

    # -- frames

    def transmit(self, frame, reply_timeout_ms=1000):
        """Put one frame on the wire and report exactly what happened to it.

        When the frame declares an expected reply the kernel waits for it and
        hands it back in the same call, so a question and its answer are one
        operation rather than a transmit plus a hopeful read.
        """
        wants_reply = frame.reply is not None
        timeout = reply_timeout_ms if wants_reply else 0
        payload = struct.pack(
            MSG_FMT,
            0, 0,                                   # tx_ts, rx_ts: kernel fills in
            len(frame.data),
            timeout,
            0,                                      # sequence
            0,                                      # flags
            frame.data.ljust(16, b"\0"),
            frame.reply or 0,
            0, 0, 0, 0, 0, 0,                       # statuses and counters
        )
        buf = bytearray(payload)
        try:
            self._ioctl(TRANSMIT, buf)
        except OSError as exc:
            if exc.errno == errno.ENODEV:
                raise CecError("the CEC adapter disappeared mid-transmit")
            raise CecError("transmit failed: %s" % exc.strerror)

        fields = struct.unpack(MSG_FMT, bytes(buf))
        length, msg = fields[2], fields[6]
        rx_status, tx_status = fields[8], fields[9]
        arb_lost, nack, low_drive, error = fields[10], fields[11], fields[12], fields[13]

        reply = None
        if wants_reply and (rx_status & RX_OK) and length >= 2:
            reply = Frame(msg[:length])

        return TxResult(frame, tx_status, nack_cnt=nack, arb_lost_cnt=arb_lost,
                        low_drive_cnt=low_drive, error_cnt=error, reply=reply)

    def receive(self, timeout_ms=1000):
        """Wait for any frame the adapter hands us. None on timeout."""
        payload = struct.pack(MSG_FMT, 0, 0, 0, timeout_ms, 0, 0,
                              b"\0" * 16, 0, 0, 0, 0, 0, 0, 0)
        buf = bytearray(payload)
        try:
            self._ioctl(RECEIVE, buf)
        except OSError as exc:
            if exc.errno in (errno.ETIMEDOUT, errno.EAGAIN):
                return None
            raise CecError("receive failed: %s" % exc.strerror)

        fields = struct.unpack(MSG_FMT, bytes(buf))
        length, msg, rx_status = fields[2], fields[6], fields[8]
        if not (rx_status & RX_OK) or length < 1:
            return None
        return Frame(msg[:length])

    def wait_for(self, opcode, timeout_ms=1500):
        """Watch the bus for a specific broadcast.

        Needed because a question broadcast to everybody - "who is the active
        source?" - is answered by another broadcast rather than by a directed
        reply, so there is no transmit call for the answer to arrive on.
        """
        deadline = time.monotonic() + timeout_ms / 1000.0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            frame = self.receive(timeout_ms=max(50, int(remaining * 1000)))
            if frame is not None and frame.opcode == opcode:
                return frame

    def poll(self, address):
        """Is anything holding this logical address? A poll is a bare header
        byte: acknowledged means occupied, not acknowledged means free."""
        from cec_frames import poll as poll_frame
        result = self.transmit(poll_frame(self.logical_addr or 15, address))
        return result.ok

    # -- description

    def describe(self):
        flags = []
        if self.caps & CAP_TRANSMIT:
            flags.append("transmit")
        if self.caps & CAP_MONITOR_ALL:
            flags.append("monitor-all")
        if self.caps & CAP_NEEDS_HPD:
            flags.append("needs-hpd")
        return "%s [%s/%s] phys=%s logical=%s caps=%s" % (
            self.path, self.driver, self.name,
            format_phys_addr(self.physical_addr),
            self.logical_addr if self.logical_addr is not None else "-",
            ",".join(flags) or "none")
