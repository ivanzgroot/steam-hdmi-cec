"""The exact bytes of every frame this project can send.

These are the assertions the README's frame table is checked against. A CEC
message that is one byte wrong is not rejected by anything - it is a valid frame
that simply never does what you meant, and the only symptom is a TV that does
not react. So the bytes are pinned here, literally, next to what they mean.
"""

import sys

from _harness import Checks

import cec_frames as f

check = Checks()

check.section("physical addresses")
check("3.0.0.0 parses", f.parse_phys_addr("3.0.0.0"), 0x3000)
check("1.0.0.0 parses", f.parse_phys_addr("1.0.0.0"), 0x1000)
check("2.1.0.0 parses", f.parse_phys_addr("2.1.0.0"), 0x2100)
check("f.f.f.f parses", f.parse_phys_addr("f.f.f.f"), 0xFFFF)
check("0x3000 formats", f.format_phys_addr(0x3000), "3.0.0.0")
check("0x2100 formats", f.format_phys_addr(0x2100), "2.1.0.0")
check("None formats without raising", f.format_phys_addr(None), "?.?.?.?")

for bad in ("3.0.0", "3.0.0.0.0", "", "g.0.0.0", "3-0-0-0"):
    try:
        f.parse_phys_addr(bad)
        rejected = False
    except ValueError:
        rejected = True
    check("rejects malformed address %r" % bad, rejected, True)

check.section("frame bytes, sent from logical address 4 with phys 3.0.0.0")
LA, PHYS = 4, 0x3000

# Each of these is the whole reason the frame exists. The comment is the same
# sentence the README uses, so the two cannot drift apart silently.

# "Wake up and show a picture", directed at the TV.
check("image-view-on", f.image_view_on(LA).hex(), "40:04")

# "I am what you should be showing" - broadcast, carrying our physical address.
check("active-source", f.active_source(LA, PHYS).hex(), "4F:82:30:00")

# "Switch to this port" - broadcast, same payload, different opcode.
check("set-stream-path", f.set_stream_path(LA, PHYS).hex(), "4F:86:30:00")

# "Whoever is on screen, say so" - broadcast question.
check("request-active-source", f.request_active_source(LA).hex(), "4F:85")

# "Are you on?" - directed at the TV.
check("give-device-power-status", f.give_device_power_status(LA).hex(), "40:8F")

# "Go to sleep" - directed at the TV, and the broadcast variant.
check("standby to TV", f.standby(LA).hex(), "40:36")
check("standby broadcast", f.standby(LA, f.LA_BROADCAST).hex(), "4F:36")
check("standby to audio system", f.standby(LA, f.LA_AUDIOSYSTEM).hex(), "45:36")

# "Turn on and take the audio from this input" - to the audio system at 5,
# carrying our physical address, which is what selects the receiver's input.
check("system-audio-mode-request",
      f.system_audio_mode_request(LA, PHYS).hex(), "45:70:30:00")
check("give-system-audio-mode-status",
      f.give_system_audio_mode_status(LA).hex(), "45:7D")

# A remote-control power keypress, and its release.
check("user-control-pressed power-on",
      f.user_control_pressed(LA).hex(), "40:44:6D")
check("user-control-released", f.user_control_released(LA).hex(), "40:45")

# "I am no longer what you should be showing."
check("inactive-source", f.inactive_source(LA, PHYS).hex(), "40:9D:30:00")

# A poll is a header and nothing else.
check("poll the TV", f.poll(LA, f.LA_TV).hex(), "40")
check("poll the audio system", f.poll(LA, f.LA_AUDIOSYSTEM).hex(), "45")

check.section("addressing")
check("a poll is a poll", f.poll(LA, 0).is_poll, True)
check("image-view-on is not a poll", f.image_view_on(LA).is_poll, False)
check("active-source is broadcast", f.active_source(LA, PHYS).is_broadcast, True)
check("image-view-on is directed", f.image_view_on(LA).is_broadcast, False)
check("standby to audio is directed", f.standby(LA, 5).is_broadcast, False)
check("initiator is read back", f.image_view_on(LA).initiator, 4)
check("follower is read back", f.system_audio_mode_request(LA, PHYS).follower, 5)
check("opcode is read back", f.image_view_on(LA).opcode, 0x04)
check("a poll has no opcode", f.poll(LA, 0).opcode, None)

check.section("a wake from a different logical address renumbers correctly")
check("image-view-on from LA 8", f.image_view_on(8).hex(), "80:04")
check("active-source from LA 8", f.active_source(8, PHYS).hex(), "8F:82:30:00")
check("active-source from LA 11 at 1.0.0.0",
      f.active_source(11, 0x1000).hex(), "BF:82:10:00")

check.section("frames describe themselves")
described = f.image_view_on(LA).describe()
check("names the opcode", "Image View On" in described, True)
check("names the route", "Playback 1 -> TV" in described, True)
check("shows the bytes", "40:04" in described, True)

described = f.active_source(LA, PHYS).describe()
check("broadcast renders as 'all'", "-> all" in described, True)
check("decodes the physical address operand", "3.0.0.0" in described, True)

described = f.system_audio_mode_request(LA, PHYS).describe()
check("names the audio system", "Audio System" in described, True)

check("power status operand is decoded",
      "on" in f.report_power_status(0, LA, f.POWER_ON).describe(), True)
check("standby power status is decoded",
      "standby" in f.report_power_status(0, LA, f.POWER_STANDBY).describe(), True)
check("user-control operand is decoded",
      "power-on" in f.user_control_pressed(LA).describe(), True)

check.section("config frame specs")
check("symbolic name", f.parse_frame_spec("image-view-on", LA, PHYS).hex(), "40:04")
check("underscores are accepted",
      f.parse_frame_spec("image_view_on", LA, PHYS).hex(), "40:04")
check("case is ignored",
      f.parse_frame_spec("Image-View-On", LA, PHYS).hex(), "40:04")
check("whitespace is trimmed",
      f.parse_frame_spec("  active-source  ", LA, PHYS).hex(), "4F:82:30:00")
check("argument form",
      f.parse_frame_spec("user-control-pressed=power-off", LA, PHYS).hex(), "40:44:6C")
check("standby=broadcast", f.parse_frame_spec("standby=broadcast", LA, PHYS).hex(), "4F:36")
check("standby=audio", f.parse_frame_spec("standby=audio", LA, PHYS).hex(), "45:36")
check("empty entry yields nothing", f.parse_frame_spec("   ", LA, PHYS), None)

check.section("raw frames")
check("raw hex with colons", f.parse_frame_spec("raw:40:44:6D", LA, PHYS).hex(), "40:44:6D")
check("raw hex with spaces", f.parse_frame_spec("raw:40 44 6D", LA, PHYS).hex(), "40:44:6D")
# The initiator nibble is always overwritten with our real address: a frame
# claiming to come from another device is the one mistake that confuses a whole
# bus, and it is exactly the mistake a hand-written raw frame invites.
check("raw frame's initiator is forced to ours",
      f.parse_frame_spec("raw:00:04", LA, PHYS).hex(), "40:04")
check("raw frame from LA 8 is renumbered",
      f.parse_frame_spec("raw:F0:04", 8, PHYS).hex(), "80:04")
check("raw frame keeps its destination nibble",
      f.parse_frame_spec("raw:05:36", LA, PHYS).hex(), "45:36")

for bad in ("nonsense", "user-control-pressed=nosuchkey", "raw:zz"):
    try:
        f.parse_frame_spec(bad, LA, PHYS)
        rejected = False
    except ValueError:
        rejected = True
    check("rejects %r" % bad, rejected, True)

check.section("frame lists")
frames = f.parse_frame_list("image-view-on; active-source", LA, PHYS)
check("two entries", [x.hex() for x in frames], ["40:04", "4F:82:30:00"])
check("blank entries are skipped",
      [x.hex() for x in f.parse_frame_list("image-view-on;;  ; ", LA, PHYS)], ["40:04"])
check("an empty list is empty", f.parse_frame_list("", LA, PHYS), [])

check.section("frames refuse to be malformed")
for builder, label in ((lambda: f.Frame(b""), "empty frame"),
                       (lambda: f.Frame(b"\0" * 17), "17-byte frame"),
                       (lambda: f.image_view_on(16), "logical address 16"),
                       (lambda: f.active_source(4, 0x1FFFF), "oversized phys addr")):
    try:
        builder()
        rejected = False
    except ValueError:
        rejected = True
    check("rejects %s" % label, rejected, True)

sys.exit(check.finish())
