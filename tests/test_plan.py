"""The decision this project is now built around: which frames are needed?

The old design sent the same four messages every time. This one surveys the bus
and sends only what the answers say is missing, so the assertions here are all
of the same shape - put the bus in a state, ask for a wake, and check the exact
list of frames that comes out.

The empty plan is the one worth staring at. "TV already on, already showing us,
nothing to send" is not an optimisation; it is the difference between pressing
Home on a controller and having nothing at all happen to a TV you are already
watching.
"""

import sys

from _harness import Checks, FakeAdapter, FakeDpcd, make_config, make_controller

import cec_frames as f

check = Checks()

TV_ON = {"power": f.POWER_ON}
TV_ASLEEP = {"power": f.POWER_STANDBY}
TV_WAKING = {"power": f.POWER_TO_ON}
TV_SILENT = {"power": None}

US = 0x3000
SOMEONE_ELSE = 0x1000


def plan_for(devices, active_source=None, phys=US, **config_overrides):
    """The wake plan for a given bus, as a list of hex strings."""
    adapter = FakeAdapter(devices=devices, active_source=active_source, phys_addr=phys)
    controller = make_controller(adapter, config=make_config(**config_overrides))
    state = controller.survey()
    return [planned.frame.hex() for planned in controller.plan_wake(state)], state


def standby_for(devices, **config_overrides):
    adapter = FakeAdapter(devices=devices)
    controller = make_controller(adapter, config=make_config(**config_overrides))
    state = controller.survey(full=False)
    return [planned.frame.hex() for planned in controller.plan_standby(state)]


check.section("nothing to do")
# The whole point. TV awake, we are on screen, no receiver: not one frame.
plan, state = plan_for({f.LA_TV: TV_ON}, active_source=US)
check("an awake TV already showing us needs no frames", plan, [])
check("the survey saw the TV as awake", state.tv_awake, True)
check("the survey saw us on screen", state.we_are_on_screen, True)

# A TV mid-transition is waking up on its own; sending another wake at it is
# noise, not help.
plan, _ = plan_for({f.LA_TV: TV_WAKING}, active_source=US)
check("a TV that is already waking up needs no frames", plan, [])

check.section("only the source switch")
# Exactly the case in the brief: TV is on, something else is on screen. One
# frame goes out, and it is the source claim.
plan, _ = plan_for({f.LA_TV: TV_ON}, active_source=SOMEONE_ELSE)
check("an awake TV showing another input needs one frame", plan, ["4F:82:30:00"])
check("and that frame is <Active Source>", len(plan), 1)

check.section("a sleeping TV")
plan, _ = plan_for({f.LA_TV: TV_ASLEEP})
check("wake then claim", plan, ["40:04", "4F:82:30:00"])

# Having just woken the TV, whatever it previously reported as the active source
# is stale - so the claim goes out even if the survey said we were on screen.
plan, _ = plan_for({f.LA_TV: TV_ASLEEP}, active_source=US)
check("a wake always re-claims, even if we were the last source",
      plan, ["40:04", "4F:82:30:00"])

# A TV that answers a poll but not a power query gets the full treatment: not
# knowing is a reason to send, not a reason to skip.
plan, _ = plan_for({f.LA_TV: TV_SILENT})
check("a TV that will not report its power gets woken anyway",
      plan, ["40:04", "4F:82:30:00"])

check.section("a receiver, only when one is actually there")
# This is what made the old design log a failure on every single wake for anyone
# without a receiver: address 5 was asked unconditionally and always NACKed.
plan, state = plan_for({f.LA_TV: TV_ON}, active_source=US)
check("no receiver on the bus means no frame to address 5",
      [p for p in plan if p.startswith("45")], [])
check("the survey knows there is no receiver", state.audio_present, False)
check("and says so", any("no receiver" in n for n in state.notes), True)

plan, state = plan_for({f.LA_TV: TV_ON, f.LA_AUDIOSYSTEM: {"audio_mode": 0}},
                       active_source=US)
check("a receiver with system audio off is asked to turn on",
      plan, ["45:70:30:00"])
check("the survey found the receiver", state.audio_present, True)

plan, _ = plan_for({f.LA_TV: TV_ON, f.LA_AUDIOSYSTEM: {"audio_mode": 1}},
                   active_source=US)
check("a receiver already handling our audio is left alone", plan, [])

# Switching the TV to us must switch the receiver too, or you get picture from
# one input and sound from another.
plan, _ = plan_for({f.LA_TV: TV_ON, f.LA_AUDIOSYSTEM: {"audio_mode": 1}},
                   active_source=SOMEONE_ELSE)
check("claiming the source re-points the receiver as well",
      plan, ["4F:82:30:00", "45:70:30:00"])

plan, _ = plan_for({f.LA_TV: TV_ASLEEP, f.LA_AUDIOSYSTEM: {"audio_mode": 0}})
check("the full cold start", plan, ["40:04", "4F:82:30:00", "45:70:30:00"])

check.section("a receiver that does not answer questions")
# audio_mode None: present, but silent about its state. Ask anyway - the request
# is idempotent and a receiver that stays quiet is not a receiver that is on.
plan, _ = plan_for({f.LA_TV: TV_ON, f.LA_AUDIOSYSTEM: {"audio_mode": None}},
                   active_source=US)
check("a silent receiver is still asked", plan, ["45:70:30:00"])

check.section("the switches turn frames off, not on")
plan, _ = plan_for({f.LA_TV: TV_ASLEEP}, wake_tv=False)
check("WAKE_TV=0 never wakes the TV", plan, ["4F:82:30:00"])
plan, _ = plan_for({f.LA_TV: TV_ASLEEP}, claim_source=False)
check("CLAIM_SOURCE=0 never claims the input", plan, ["40:04"])
plan, _ = plan_for({f.LA_TV: TV_ASLEEP, f.LA_AUDIOSYSTEM: {"audio_mode": 0}},
                   wake_audio=False)
check("WAKE_AUDIO=0 never touches the receiver",
      plan, ["40:04", "4F:82:30:00"])

plan, _ = plan_for({f.LA_TV: TV_ASLEEP}, send_stream_path=True)
check("SEND_STREAM_PATH=1 adds the routing nudge",
      plan, ["40:04", "4F:86:30:00", "4F:82:30:00"])
plan, _ = plan_for({f.LA_TV: TV_ASLEEP}, send_remote_power_key=True)
check("SEND_REMOTE_POWER_KEY=1 adds a keypress and its release",
      plan, ["40:44:6D", "40:45", "40:04", "4F:82:30:00"])

check.section("FORCE_ALL_FRAMES restores the old unconditional behaviour")
plan, _ = plan_for({f.LA_TV: TV_ON, f.LA_AUDIOSYSTEM: {"audio_mode": 1}},
                   active_source=US, force_all_frames=True)
check("everything enabled is sent regardless of state",
      plan, ["40:04", "4F:82:30:00", "45:70:30:00"])
# Even forced, a receiver that is not on the bus is not invented.
plan, _ = plan_for({f.LA_TV: TV_ON}, active_source=US, force_all_frames=True)
check("but a receiver that is not there is still not addressed",
      plan, ["40:04", "4F:82:30:00"])

check.section("extra frames from config")
plan, _ = plan_for({f.LA_TV: TV_ON}, active_source=US,
                   extra_wake_frames="raw:40:44:6D")
check("EXTRA_WAKE_FRAMES is appended", plan, ["40:44:6D"])
plan, _ = plan_for({f.LA_TV: TV_ASLEEP},
                   extra_wake_frames="text-view-on; set-stream-path")
check("and comes after the planned frames",
      plan, ["40:04", "4F:82:30:00", "40:0D", "4F:86:30:00"])
# A typo in the config must never stop a wake.
plan, _ = plan_for({f.LA_TV: TV_ASLEEP}, extra_wake_frames="not-a-real-frame")
check("a bad extra frame is dropped, not fatal", plan, ["40:04", "4F:82:30:00"])

check.section("physical address flows into every frame")
plan, _ = plan_for({f.LA_TV: TV_ASLEEP, f.LA_AUDIOSYSTEM: {"audio_mode": 0}},
                   phys=0x2100)
check("a different HDMI port changes the payload",
      plan, ["40:04", "4F:82:21:00", "45:70:21:00"])

check.section("standby")
check("with no receiver, only the TV is told",
      standby_for({f.LA_TV: {}}), ["40:36"])
check("with a receiver, both are told",
      standby_for({f.LA_TV: {}, f.LA_AUDIOSYSTEM: {}}), ["40:36", "45:36"])
check("STANDBY_AUDIO=0 leaves the receiver alone",
      standby_for({f.LA_TV: {}, f.LA_AUDIOSYSTEM: {}}, standby_audio=False), ["40:36"])
check("STANDBY_TV=0 leaves the TV alone",
      standby_for({f.LA_TV: {}, f.LA_AUDIOSYSTEM: {}}, standby_tv=False), ["45:36"])
check("STANDBY_BROADCAST=1 sends one frame to everybody",
      standby_for({f.LA_TV: {}, f.LA_AUDIOSYSTEM: {}}, standby_broadcast=True),
      ["4F:36"])
check("EXTRA_STANDBY_FRAMES is appended",
      standby_for({f.LA_TV: {}}, extra_standby_frames="inactive-source"),
      ["40:36", "40:9D:30:00"])

check.section("standby asks the bus as little as possible")
# It runs Before=sleep.target with a fifteen second budget. Anything it asks is
# time the machine spends not suspending, so it polls for presence and stops.
adapter = FakeAdapter(devices={f.LA_TV: {"power": f.POWER_ON}})
controller = make_controller(adapter, config=make_config())
controller.standby()
opcodes = [frame.opcode for frame in adapter.sent]
check("no power query on the standby path",
      f.OP_GIVE_DEVICE_POWER_STATUS in opcodes, False)
check("no active-source query on the standby path",
      f.OP_REQUEST_ACTIVE_SOURCE in opcodes, False)
check("the standby frame did go out", f.OP_STANDBY in opcodes, True)

check.section("the tunneling bit is enabled after every reconfiguration")
# The invariant the whole project turns on. Claiming a logical address resets
# the DisplayPort adapter and clears DPCD 0x3001, so an enable that happens
# before a claim is an enable that gets thrown away - producing a wake that
# works cold and fails after resume.
adapter = FakeAdapter(devices={f.LA_TV: TV_ASLEEP})
dpcd = FakeDpcd()
controller = make_controller(adapter, config=make_config(), dpcd=dpcd)
dpcd.calls = []
adapter.calls = []
controller.wake()

order = [call for call in adapter.calls if call[0] in ("configure", "clear")]
check("the wake claimed no further addresses on a clean run", order, [])
check("tunneling was enabled before sending", dpcd.calls[0], "enable")

# And on the escalation path, where a re-claim really does happen.
adapter = FakeAdapter(devices={})          # nothing answers; forces retries
dpcd = FakeDpcd()
controller = make_controller(adapter, config=make_config(wake_attempts=3), dpcd=dpcd)
dpcd.calls = []
merged = []
original_claim = controller._claim_address
controller._claim_address = lambda: (merged.append("configure"), original_claim())[1]
original_enable = dpcd.ensure_enabled
dpcd.ensure_enabled = lambda: (merged.append("enable"), original_enable())[1]
controller.wake()

check("a re-claim happened during escalation", "configure" in merged, True)
for index, entry in enumerate(merged):
    if entry == "configure":
        following = merged[index + 1:index + 2]
        check("configure at %d is followed by an enable" % index, following, ["enable"])

sys.exit(check.finish())
