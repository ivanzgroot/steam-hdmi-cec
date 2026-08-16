"""CEC protocol: logical addresses, opcodes, and the frames themselves.

A CEC frame is between one and sixteen bytes:

    [header] [opcode] [operand...]

The header byte packs two 4-bit logical addresses - the initiator in the high
nibble, the follower in the low nibble. A frame with *only* a header and no
opcode is a "polling message", which is how you ask whether anybody is
listening at an address without saying anything to them.

So "tell the TV to turn its picture on", sent by a playback device at logical
address 4, is exactly two bytes:

    0x40 0x04
     |  |  `-- opcode 0x04, <Image View On>
     |  `----- follower 0, the TV
     `-------- initiator 4, us

This module knows the protocol and nothing else: it has no file descriptors, no
ioctls and no hardware. Everything here is pure, which is what lets the test
suite check the exact bytes of every frame this project can send without a CEC
adapter anywhere in sight.
"""

# --------------------------------------------------------------------------- addresses

LA_TV = 0
LA_RECORD_1 = 1
LA_RECORD_2 = 2
LA_TUNER_1 = 3
LA_PLAYBACK_1 = 4
LA_AUDIOSYSTEM = 5
LA_TUNER_2 = 6
LA_TUNER_3 = 7
LA_PLAYBACK_2 = 8
LA_RECORD_3 = 9
LA_TUNER_4 = 10
LA_PLAYBACK_3 = 11
LA_BACKUP_1 = 12
LA_BACKUP_2 = 13
LA_SPECIFIC = 14
LA_UNREGISTERED = 15

# 15 is both "unregistered" as an initiator and "everybody" as a follower.
LA_BROADCAST = 15

LA_NAMES = {
    LA_TV: "TV",
    LA_RECORD_1: "Recorder 1",
    LA_RECORD_2: "Recorder 2",
    LA_TUNER_1: "Tuner 1",
    LA_PLAYBACK_1: "Playback 1",
    LA_AUDIOSYSTEM: "Audio System",
    LA_TUNER_2: "Tuner 2",
    LA_TUNER_3: "Tuner 3",
    LA_PLAYBACK_2: "Playback 2",
    LA_RECORD_3: "Recorder 3",
    LA_TUNER_4: "Tuner 4",
    LA_PLAYBACK_3: "Playback 3",
    LA_BACKUP_1: "Backup 1",
    LA_BACKUP_2: "Backup 2",
    LA_SPECIFIC: "Specific",
    LA_UNREGISTERED: "Broadcast",
}

LA_INVALID = 0xFF
PHYS_ADDR_INVALID = 0xFFFF

# --------------------------------------------------------------------------- opcodes

OP_FEATURE_ABORT = 0x00
OP_IMAGE_VIEW_ON = 0x04
OP_TEXT_VIEW_ON = 0x0D
OP_STANDBY = 0x36
OP_USER_CONTROL_PRESSED = 0x44
OP_USER_CONTROL_RELEASED = 0x45
OP_GIVE_OSD_NAME = 0x46
OP_SET_OSD_NAME = 0x47
OP_SYSTEM_AUDIO_MODE_REQUEST = 0x70
OP_SET_SYSTEM_AUDIO_MODE = 0x72
OP_GIVE_SYSTEM_AUDIO_MODE_STATUS = 0x7D
OP_SYSTEM_AUDIO_MODE_STATUS = 0x7E
OP_ROUTING_CHANGE = 0x80
OP_ROUTING_INFORMATION = 0x81
OP_ACTIVE_SOURCE = 0x82
OP_GIVE_PHYSICAL_ADDR = 0x83
OP_REPORT_PHYSICAL_ADDR = 0x84
OP_REQUEST_ACTIVE_SOURCE = 0x85
OP_SET_STREAM_PATH = 0x86
OP_DEVICE_VENDOR_ID = 0x87
OP_VENDOR_COMMAND = 0x89
OP_GIVE_DEVICE_VENDOR_ID = 0x8C
OP_INACTIVE_SOURCE = 0x9D
OP_CEC_VERSION = 0x9E
OP_GET_CEC_VERSION = 0x9F
OP_GIVE_DEVICE_POWER_STATUS = 0x8F
OP_REPORT_POWER_STATUS = 0x90
OP_VENDOR_COMMAND_WITH_ID = 0xA0

OPCODE_NAMES = {
    OP_FEATURE_ABORT: "Feature Abort",
    OP_IMAGE_VIEW_ON: "Image View On",
    OP_TEXT_VIEW_ON: "Text View On",
    OP_STANDBY: "Standby",
    OP_USER_CONTROL_PRESSED: "User Control Pressed",
    OP_USER_CONTROL_RELEASED: "User Control Released",
    OP_GIVE_OSD_NAME: "Give OSD Name",
    OP_SET_OSD_NAME: "Set OSD Name",
    OP_SYSTEM_AUDIO_MODE_REQUEST: "System Audio Mode Request",
    OP_SET_SYSTEM_AUDIO_MODE: "Set System Audio Mode",
    OP_GIVE_SYSTEM_AUDIO_MODE_STATUS: "Give System Audio Mode Status",
    OP_SYSTEM_AUDIO_MODE_STATUS: "System Audio Mode Status",
    OP_ROUTING_CHANGE: "Routing Change",
    OP_ROUTING_INFORMATION: "Routing Information",
    OP_ACTIVE_SOURCE: "Active Source",
    OP_GIVE_PHYSICAL_ADDR: "Give Physical Address",
    OP_REPORT_PHYSICAL_ADDR: "Report Physical Address",
    OP_REQUEST_ACTIVE_SOURCE: "Request Active Source",
    OP_SET_STREAM_PATH: "Set Stream Path",
    OP_DEVICE_VENDOR_ID: "Device Vendor ID",
    OP_VENDOR_COMMAND: "Vendor Command",
    OP_GIVE_DEVICE_VENDOR_ID: "Give Device Vendor ID",
    OP_INACTIVE_SOURCE: "Inactive Source",
    OP_CEC_VERSION: "CEC Version",
    OP_GET_CEC_VERSION: "Get CEC Version",
    OP_GIVE_DEVICE_POWER_STATUS: "Give Device Power Status",
    OP_REPORT_POWER_STATUS: "Report Power Status",
    OP_VENDOR_COMMAND_WITH_ID: "Vendor Command With ID",
}

# --------------------------------------------------------------------------- operands

POWER_ON = 0x00
POWER_STANDBY = 0x01
POWER_TO_ON = 0x02
POWER_TO_STANDBY = 0x03

POWER_NAMES = {
    POWER_ON: "on",
    POWER_STANDBY: "standby",
    POWER_TO_ON: "waking up",
    POWER_TO_STANDBY: "going to standby",
}

AUDIO_MODE_OFF = 0x00
AUDIO_MODE_ON = 0x01

# <User Control Pressed> operands. Remote-control keypresses, which is what a
# TV that ignores the tidy power messages will often still respond to.
UI_POWER = 0x40
UI_POWER_TOGGLE = 0x6B
UI_POWER_OFF = 0x6C
UI_POWER_ON = 0x6D

UI_COMMANDS = {
    "power": UI_POWER,
    "power-toggle": UI_POWER_TOGGLE,
    "power-off": UI_POWER_OFF,
    "power-on": UI_POWER_ON,
}

# --------------------------------------------------------------------------- physical addresses


def parse_phys_addr(text):
    """"3.0.0.0" -> 0x3000. Raises ValueError on anything malformed."""
    parts = str(text).strip().split(".")
    if len(parts) != 4:
        raise ValueError("physical address must be x.x.x.x, got %r" % (text,))
    value = 0
    for part in parts:
        nibble = int(part, 16)
        if not 0 <= nibble <= 0xF:
            raise ValueError("physical address nibble out of range in %r" % (text,))
        value = (value << 4) | nibble
    return value


def format_phys_addr(value):
    """0x3000 -> "3.0.0.0"."""
    if value is None:
        return "?.?.?.?"
    return "%x.%x.%x.%x" % ((value >> 12) & 0xF, (value >> 8) & 0xF,
                            (value >> 4) & 0xF, value & 0xF)


def la_name(addr):
    return LA_NAMES.get(addr, "LA %s" % addr)


# --------------------------------------------------------------------------- the frame


class Frame:
    """One CEC message, as the exact bytes that go on the wire.

    Construct through the module-level builders below rather than directly -
    they are the single place that knows each message's correct follower and
    operand layout, so a malformed frame cannot be built by accident.
    """

    __slots__ = ("data", "reply", "note")

    def __init__(self, data, reply=None, note=""):
        data = bytes(data)
        if not 1 <= len(data) <= 16:
            raise ValueError("a CEC frame is 1..16 bytes, got %d" % len(data))
        self.data = data
        # Opcode we expect back. The kernel will wait for it and hand it to us
        # in the same call, which is how a query becomes a single operation.
        self.reply = reply
        # Why this frame is being sent, in plain words, for the log.
        self.note = note

    # -- accessors

    @property
    def initiator(self):
        return (self.data[0] >> 4) & 0xF

    @property
    def follower(self):
        return self.data[0] & 0xF

    @property
    def opcode(self):
        return self.data[1] if len(self.data) > 1 else None

    @property
    def operands(self):
        return self.data[2:]

    @property
    def is_poll(self):
        """A header and nothing else: "is anyone home at this address?"."""
        return len(self.data) == 1

    @property
    def is_broadcast(self):
        return self.follower == LA_BROADCAST

    # -- rendering

    def hex(self):
        return ":".join("%02X" % b for b in self.data)

    def opcode_name(self):
        if self.opcode is None:
            return "Polling Message"
        return OPCODE_NAMES.get(self.opcode, "Opcode 0x%02X" % self.opcode)

    def describe(self):
        """One line: bytes, who to who, what it means. This is what the logs
        show, and it is deliberately readable without the spec next to you."""
        route = "%s -> %s" % (la_name(self.initiator),
                              "all" if self.is_broadcast else la_name(self.follower))
        text = "%-17s %-28s %s" % (self.hex(), self.opcode_name(), route)
        detail = self._operand_detail()
        if detail:
            text += "  (%s)" % detail
        return text

    def _operand_detail(self):
        operands = self.operands
        if self.opcode in (OP_ACTIVE_SOURCE, OP_SET_STREAM_PATH,
                           OP_REPORT_PHYSICAL_ADDR, OP_SYSTEM_AUDIO_MODE_REQUEST,
                           OP_INACTIVE_SOURCE) and len(operands) >= 2:
            return format_phys_addr((operands[0] << 8) | operands[1])
        if self.opcode == OP_REPORT_POWER_STATUS and operands:
            return POWER_NAMES.get(operands[0], "0x%02X" % operands[0])
        if self.opcode == OP_SET_SYSTEM_AUDIO_MODE and operands:
            return "on" if operands[0] == AUDIO_MODE_ON else "off"
        if self.opcode == OP_USER_CONTROL_PRESSED and operands:
            for name, code in UI_COMMANDS.items():
                if code == operands[0]:
                    return name
            return "ui 0x%02X" % operands[0]
        return ""

    def __eq__(self, other):
        return isinstance(other, Frame) and self.data == other.data

    def __repr__(self):
        return "Frame(%s)" % self.hex()


def _header(initiator, follower):
    if not 0 <= initiator <= 15 or not 0 <= follower <= 15:
        raise ValueError("logical addresses are 0..15")
    return (initiator << 4) | follower


def _phys_operands(phys):
    if not 0 <= phys <= 0xFFFF:
        raise ValueError("physical address out of range: %r" % (phys,))
    return [(phys >> 8) & 0xFF, phys & 0xFF]


# --------------------------------------------------------------------------- builders
#
# One function per message this project sends, each carrying the reason it
# exists. The follower is fixed by the spec here rather than left to the caller,
# because getting it wrong is silent: a <Active Source> sent to one device
# instead of broadcast is a valid frame that simply never does anything.


def poll(initiator, follower):
    """Header only. ACKed if a device holds that address, NACKed if not - the
    entire basis of "is there a receiver on this bus?"."""
    return Frame([_header(initiator, follower)],
                 note="probe %s" % la_name(follower))


def image_view_on(initiator, follower=LA_TV):
    """"Wake up and show a picture." The standard, polite way to power on a TV.
    Directed, so the TV either acknowledges it or it does not."""
    return Frame([_header(initiator, follower), OP_IMAGE_VIEW_ON],
                 note="wake the TV")


def user_control_pressed(initiator, follower=LA_TV, ui=UI_POWER_ON):
    """A remote-control keypress. Some TVs ignore <Image View On> from a device
    they do not recognise but obey this, because it is what their own remote
    sends. Paired with a release, as a real remote would."""
    return Frame([_header(initiator, follower), OP_USER_CONTROL_PRESSED, ui],
                 note="press the remote's power-on key")


def user_control_released(initiator, follower=LA_TV):
    return Frame([_header(initiator, follower), OP_USER_CONTROL_RELEASED],
                 note="release the remote key")


def active_source(initiator, phys):
    """"I am what you should be showing." Broadcast, because every device on the
    bus needs to know the active source changed - the TV switches input to us
    and any other source stands down."""
    return Frame([_header(initiator, LA_BROADCAST), OP_ACTIVE_SOURCE]
                 + _phys_operands(phys),
                 note="claim the TV's input")


def inactive_source(initiator, phys):
    """"I am no longer what you should be showing." Sent before we go away, so
    the TV can fall back to whatever it was showing before us."""
    return Frame([_header(initiator, LA_TV), OP_INACTIVE_SOURCE]
                 + _phys_operands(phys),
                 note="give up the TV's input")


def set_stream_path(initiator, phys):
    """"Switch to this port." Normally the TV sends this; a source sending it is
    a nudge for TVs that want the routing spelled out rather than inferred from
    <Active Source>. Broadcast by the spec."""
    return Frame([_header(initiator, LA_BROADCAST), OP_SET_STREAM_PATH]
                 + _phys_operands(phys),
                 note="ask for the HDMI port to be selected")


def request_active_source(initiator):
    """"Whoever is the active source, say so." Broadcast question; the answer
    arrives as an <Active Source> broadcast from whichever device it is. This is
    how we find out whether the TV is already showing us."""
    return Frame([_header(initiator, LA_BROADCAST), OP_REQUEST_ACTIVE_SOURCE],
                 reply=OP_ACTIVE_SOURCE,
                 note="ask who is currently on screen")


def give_device_power_status(initiator, follower=LA_TV):
    """"Are you on?" The reply is <Report Power Status>. Asking first is what
    lets a wake send nothing at all when the TV is already awake."""
    return Frame([_header(initiator, follower), OP_GIVE_DEVICE_POWER_STATUS],
                 reply=OP_REPORT_POWER_STATUS,
                 note="ask %s whether it is on" % la_name(follower))


def report_power_status(initiator, follower, status):
    return Frame([_header(initiator, follower), OP_REPORT_POWER_STATUS, status])


def standby(initiator, follower=LA_TV):
    """"Go to sleep." Directed at the TV by default; broadcast (follower 15)
    puts everything on the bus to sleep, which is a config choice rather than a
    default because it will also switch off a receiver someone else is using."""
    return Frame([_header(initiator, follower), OP_STANDBY],
                 note="send %s to standby"
                      % ("everything" if follower == LA_BROADCAST else la_name(follower)))


def system_audio_mode_request(initiator, phys):
    """"Turn on and take the audio from this input." Sent to the audio system at
    logical address 5. The physical address is the payload, and it is what tells
    a receiver which of its inputs to select - so this one message both powers
    the receiver on and switches it to us."""
    return Frame([_header(initiator, LA_AUDIOSYSTEM), OP_SYSTEM_AUDIO_MODE_REQUEST]
                 + _phys_operands(phys),
                 reply=OP_SET_SYSTEM_AUDIO_MODE,
                 note="wake the receiver and switch it to this input")


def give_system_audio_mode_status(initiator):
    """"Receiver, are you already handling the audio?" Lets a wake skip the
    request above when the receiver is on and already pointed at us."""
    return Frame([_header(initiator, LA_AUDIOSYSTEM), OP_GIVE_SYSTEM_AUDIO_MODE_STATUS],
                 reply=OP_SYSTEM_AUDIO_MODE_STATUS,
                 note="ask the receiver whether system audio is on")


def give_osd_name(initiator, follower):
    return Frame([_header(initiator, follower), OP_GIVE_OSD_NAME],
                 reply=OP_SET_OSD_NAME,
                 note="ask %s for its name" % la_name(follower))


def give_device_vendor_id(initiator, follower):
    return Frame([_header(initiator, follower), OP_GIVE_DEVICE_VENDOR_ID],
                 reply=OP_DEVICE_VENDOR_ID,
                 note="ask %s for its vendor ID" % la_name(follower))


def raw(initiator, spec):
    """An arbitrary frame from config, for messages this module has no builder
    for - vendor-specific commands above all. The initiator nibble is always
    overwritten with our real address, because a frame claiming to come from
    somebody else is the one mistake that confuses an entire bus."""
    data = bytearray(spec)
    if not data:
        raise ValueError("a raw frame needs at least a header byte")
    data[0] = (initiator << 4) | (data[0] & 0x0F)
    return Frame(bytes(data), note="raw frame from config")


# --------------------------------------------------------------------------- config parsing


def parse_frame_spec(spec, initiator, phys):
    """Turn one config entry into a Frame.

    Accepts a symbolic name with optional argument, or "raw:" followed by hex
    bytes. "{phys}" anywhere in an argument becomes our physical address:

        image-view-on
        active-source
        user-control-pressed=power-on
        standby=broadcast
        raw:40:44:6D

    Returns None for an empty entry so blank list items are simply skipped.
    """
    spec = spec.strip()
    if not spec:
        return None

    lowered = spec.lower()
    if lowered.startswith("raw:"):
        payload = spec[4:].replace(":", " ").replace(",", " ").split()
        try:
            return raw(initiator, bytes(int(b, 16) for b in payload))
        except ValueError as exc:
            raise ValueError("bad raw frame %r: %s" % (spec, exc))

    name, _, argument = lowered.partition("=")
    name = name.strip().replace("_", "-")
    argument = argument.strip()

    if name == "image-view-on":
        return image_view_on(initiator)
    if name == "text-view-on":
        return Frame([_header(initiator, LA_TV), OP_TEXT_VIEW_ON],
                     note="wake the TV (text mode)")
    if name == "active-source":
        return active_source(initiator, phys)
    if name == "inactive-source":
        return inactive_source(initiator, phys)
    if name == "set-stream-path":
        return set_stream_path(initiator, phys)
    if name == "system-audio-mode-request":
        return system_audio_mode_request(initiator, phys)
    if name == "user-control-pressed":
        ui = UI_COMMANDS.get(argument or "power-on")
        if ui is None:
            raise ValueError("unknown user-control key %r (known: %s)"
                             % (argument, ", ".join(sorted(UI_COMMANDS))))
        return user_control_pressed(initiator, ui=ui)
    if name == "user-control-released":
        return user_control_released(initiator)
    if name == "standby":
        follower = LA_BROADCAST if argument in ("broadcast", "all", "15") else LA_TV
        if argument == "audio":
            follower = LA_AUDIOSYSTEM
        return standby(initiator, follower)

    raise ValueError("unknown frame %r" % spec)


def parse_frame_list(text, initiator, phys):
    """A ";"-separated config value into a list of Frames."""
    frames = []
    for entry in str(text).split(";"):
        frame = parse_frame_spec(entry, initiator, phys)
        if frame is not None:
            frames.append(frame)
    return frames
