"""The kernel ABI, pinned.

Everything this project does reaches the hardware through seven ioctls on
/dev/cec0. Their numbers encode the size of the structure they carry, so a
structure declared even one byte wrong produces a request number the kernel has
never heard of - and the symptom is an unhelpful EINVAL or ENOTTY from three
layers down, not a message about struct layout.

So the sizes and the resulting request numbers are asserted here against the
values in linux/cec.h. If a future kernel ever changes them, this suite says so
in one line instead of leaving you debugging a TV.
"""

import struct
import sys

from _harness import Checks, FakeAdapter

import cec_device as d
import cec_frames as f

check = Checks()

check.section("structure sizes match linux/cec.h")
check("struct cec_msg", d.MSG_SIZE, 56)
check("struct cec_caps", d.CAPS_SIZE, 76)
check("struct cec_log_addrs", d.LOG_ADDRS_SIZE, 92)

check.section("ioctl request numbers match linux/cec.h")
# Hand-computed from the header rather than from _ioc, so that a bug in the
# encoder cannot agree with itself.
for name, expected in (("ADAP_G_CAPS", 0xC04C6100),
                       ("ADAP_G_PHYS_ADDR", 0x80026101),
                       ("ADAP_G_LOG_ADDRS", 0x805C6103),
                       ("ADAP_S_LOG_ADDRS", 0xC05C6104),
                       ("TRANSMIT", 0xC0386105),
                       ("RECEIVE", 0xC0386106),
                       ("S_MODE", 0x40046109)):
    check("CEC_%s" % name, getattr(d, name) & 0xFFFFFFFF, expected)

# Requests with both direction bits set exceed 2^31 and must come back as
# negative ints, which is what fcntl.ioctl accepts.
check("read+write requests are signed", d.TRANSMIT < 0, True)
check("write-only requests stay positive", d.S_MODE > 0, True)

check.section("transmit status decoding")
result = d.TxResult(f.image_view_on(4), d.TX_OK)
check("OK is ok", result.ok, True)
check("OK is not a NACK", result.nacked, False)
check("OK reads as acknowledged", "acknowledged" in result.why(), True)

result = d.TxResult(f.image_view_on(4), d.TX_NACK | d.TX_MAX_RETRIES, nack_cnt=3)
check("NACK is not ok", result.ok, False)
check("NACK is detected", result.nacked, True)
check("NACK is not contention", result.contended, False)
check("NACK explains itself", "not acknowledged" in result.why(), True)
check("NACK reports its count", "nack x3" in result.why(), True)

# The distinction the old text-grep could not make: a busy or noisy bus is a
# different problem from a device that is not there, and wants a plain retry
# rather than the adapter being poked.
result = d.TxResult(f.image_view_on(4), d.TX_ARB_LOST, arb_lost_cnt=2)
check("arbitration loss is contention", result.contended, True)
check("arbitration loss is not a NACK", result.nacked, False)
result = d.TxResult(f.image_view_on(4), d.TX_LOW_DRIVE, low_drive_cnt=1)
check("low drive is contention", result.contended, True)

check.section("a transmit round-trips through the real packing code")
adapter = FakeAdapter(devices={f.LA_TV: {"power": f.POWER_ON}})
device = adapter.device()
check("caps were read", device.name, "DP-1")
check("physical address is read", device.read_phys_addr(), 0x3000)
device.configure(osd_name="SteamOS", device_type="playback")
check("a logical address was claimed", device.logical_addr, 4)

result = device.transmit(f.image_view_on(4))
check("the frame reached the bus", adapter.sent[-1].hex(), "40:04")
check("the TV acknowledged", result.ok, True)

check.section("claiming an address on an adapter that already has one")
# The bug that broke the first real install. The kernel's drm_dp_cec driver
# claims a logical address when it reads the EDID, so by the time this project
# opens /dev/cec0 the adapter is normally already configured - and
# CEC_ADAP_S_LOG_ADDRS on a configured adapter returns EBUSY. Going straight
# from configured to configured is not allowed.

# Already configured exactly as we want it: adopt it, write nothing.
adapter = FakeAdapter(devices={f.LA_TV: {}},
                      configured={"osd_name": "SteamOS", "version": 5, "primary": 4})
device = adapter.device()
check("the existing address is adopted",
      device.configure(osd_name="SteamOS", device_type="playback"), 4)
check("and nothing was written", adapter.configured, 0)
check("and it says so", device.adopted_existing, True)
check("no clear was issued either",
      [c for c in adapter.calls if c[0] == "clear"], [])

# Configured with a different identity: must be cleared, then set.
adapter = FakeAdapter(devices={f.LA_TV: {}},
                      configured={"osd_name": "SomethingElse", "version": 5,
                                  "primary": 4})
device = adapter.device()
check("a different OSD name forces a re-claim",
      device.configure(osd_name="SteamOS", device_type="playback"), 4)
check("the adapter was cleared first",
      [c[0] for c in adapter.calls if c[0] in ("clear", "configure")],
      ["clear", "configure"])
check("and this was a real claim", device.adopted_existing, False)

# A different device type is also a real change.
adapter = FakeAdapter(devices={f.LA_TV: {}},
                      configured={"osd_name": "SteamOS", "version": 5, "primary": 4})
device = adapter.device()
device.configure(osd_name="SteamOS", device_type="tuner")
check("a different device type forces a re-claim",
      [c[0] for c in adapter.calls if c[0] in ("clear", "configure")],
      ["clear", "configure"])

# A different CEC version likewise.
adapter = FakeAdapter(devices={f.LA_TV: {}},
                      configured={"osd_name": "SteamOS", "version": 5, "primary": 4})
device = adapter.device()
device.configure(osd_name="SteamOS", cec_version="2.0")
check("a different CEC version forces a re-claim",
      [c[0] for c in adapter.calls if c[0] in ("clear", "configure")],
      ["clear", "configure"])

# Unconfigured: straight to a claim, no pointless clear.
adapter = FakeAdapter(devices={f.LA_TV: {}}, configured=None)
device = adapter.device()
check("an unconfigured adapter is claimed directly", device.configure(), 4)
check("with no clear first",
      [c[0] for c in adapter.calls if c[0] in ("clear", "configure")], ["configure"])

# force=True re-claims even when the configuration already matches.
adapter = FakeAdapter(devices={f.LA_TV: {}},
                      configured={"osd_name": "SteamOS", "version": 5, "primary": 4})
device = adapter.device()
device.configure(osd_name="SteamOS", force=True)
check("force=True re-claims regardless",
      [c[0] for c in adapter.calls if c[0] in ("clear", "configure")],
      ["clear", "configure"])

check.section("ioctl errors become messages, not tracebacks")
# Under systemd an unhandled OSError is a Python traceback in the journal,
# which says where the call was made but not what it means.
adapter = FakeAdapter(devices={f.LA_TV: {}})
device = adapter.device()


def explode(_request, _payload):
    raise OSError(16, "Device or resource busy")


device._ioctl = explode
try:
    device.read_phys_addr()
    raised = "nothing"
except d.CecError as exc:
    raised = "CecError"
    message = str(exc)
except OSError:
    raised = "OSError"
    message = ""
check("a failed ioctl raises CecError", raised, "CecError")
check("the message names the operation", "physical address" in message, True)
check("and carries the errno", "errno 16" in message, True)

check.section("polling is how devices are discovered")
adapter = FakeAdapter(devices={f.LA_TV: {}, f.LA_AUDIOSYSTEM: {}})
device = adapter.device()
device.configure()
check("the TV answers", device.poll(f.LA_TV), True)
check("the receiver answers", device.poll(f.LA_AUDIOSYSTEM), True)
check("nothing answers at 9", device.poll(9), False)
check("a poll is one byte", len(adapter.sent[-1].data), 1)

adapter = FakeAdapter(devices={f.LA_TV: {}})
device = adapter.device()
device.configure()
check("with no receiver, address 5 does not answer",
      device.poll(f.LA_AUDIOSYSTEM), False)

check.section("a question and its answer are one operation")
adapter = FakeAdapter(devices={f.LA_TV: {"power": f.POWER_STANDBY}})
device = adapter.device()
device.configure()
result = device.transmit(f.give_device_power_status(4))
check("the reply came back", result.reply is not None, True)
check("the reply is a power report", result.reply.opcode, f.OP_REPORT_POWER_STATUS)
check("the TV says standby", result.reply.operands[0], f.POWER_STANDBY)

# A TV that declines to answer is normal, not an error. Nothing may crash.
adapter = FakeAdapter(devices={f.LA_TV: {"power": None}})
device = adapter.device()
device.configure()
result = device.transmit(f.give_device_power_status(4))
check("a silent TV still transmits ok", result.ok, True)
check("a silent TV yields no reply", result.reply, None)

check.section("an unconfigured adapter reports no physical address")
adapter = FakeAdapter(phys_addr=f.PHYS_ADDR_INVALID)
device = adapter.device()
check("0xFFFF becomes None rather than an address", device.read_phys_addr(), None)

check.section("packing survives a full-length frame")
big = f.Frame(bytes([0x40, 0xA0]) + b"\x01" * 14)
check("16 bytes is allowed", len(big.data), 16)
adapter = FakeAdapter(devices={f.LA_TV: {}})
device = adapter.device()
device.configure()
device.transmit(big)
check("all 16 bytes reached the bus", adapter.sent[-1].data, big.data)

check.section("the message struct packs and unpacks symmetrically")
packed = struct.pack(d.MSG_FMT, 1, 2, 3, 4, 5, 6, b"\x40\x04" + b"\0" * 14,
                     0x90, 1, 1, 0, 0, 0, 0)
check("packed size is right", len(packed), d.MSG_SIZE)
fields = struct.unpack(d.MSG_FMT, packed)
check("len field round-trips", fields[2], 3)
check("reply field round-trips", fields[7], 0x90)
check("msg bytes round-trip", fields[6][:2], b"\x40\x04")

sys.exit(check.finish())
