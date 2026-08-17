# steam-hdmi-cec

**Console-style HDMI-CEC control for SteamOS.**

I run SteamOS on a desktop machine plugged into the TV through a UGREEN
DisplayPort→HDMI adapter, and I wanted it to behave like a console: press Home on
the controller, the TV comes on and switches to it. Close the lid on the evening,
the TV goes to sleep with it.

Nothing I tried did that reliably on this hardware, mostly because of the adapter
— see [Why suspend breaks CEC on these adapters](#why-suspend-breaks-cec-on-these-adapters),
which is the interesting part of this project. So I wrote my own.

---

## What it does

- **Press Home, the TV comes on.** Any connected controller works. Plug one in
  mid-session and it starts working immediately.
- **Boot and resume wake the TV.** Suspend, reboot and shutdown put it back to
  sleep.
- **Receivers are handled.** If there's an AV receiver on the bus it gets woken
  and switched to this input too. If there isn't, nothing is sent to one and
  nothing fails because of it.
- **It only sends what's actually needed.** If the TV is already on and already
  showing this PC, pressing Home sends nothing at all.
- **It survives suspend**, which on an adapter like mine is the whole problem.
- **No dependencies.** System `python3`, nothing else. No pip, no venv, no
  compiler, no `cec-ctl`.

---

## Requirements

| | |
| --- | --- |
| **OS** | SteamOS, or any Linux with systemd |
| **Python** | 3.6+, the system one is fine |
| **Hardware** | Any CEC adapter showing up as `/dev/cec0`. DisplayPort→HDMI adapters are what I built this for. |
| **Privileges** | Root, for `/dev/cec0`, `/dev/input/event*` and the DPCD register |

**Turn off Steam's own HDMI-CEC setting first.** I had both running at once for a
while and they compete for logical addresses on the bus — each one can reconfigure
the adapter out from under the other, and the result is that neither behaves
predictably. Let one of them own CEC.

---

## Install

```sh
git clone https://github.com/ivanzgroot/steam-hdmi-cec.git
cd steam-hdmi-cec
sudo bash install.sh
```

That's it. The installer writes the files, creates the systemd units, enables
everything and runs a wake straight away so you can see whether it worked.

Re-running it is safe — after a `git pull`, run it again to update in place. Your
`config.conf` is never overwritten.

```sh
sudo bash install.sh status             # services, adapter, tunneling, bus, logs
sudo bash install.sh uninstall          # remove services and units
sudo bash install.sh uninstall --purge  # ...and delete /etc/cec-hdmi too
bash install.sh selftest                # run the tests (no root, changes nothing)
```

---

## How it decides what to send

CEC is a little message bus shared by everything plugged into the TV. Roles are
fixed — the TV is always address `0`, an audio system is always `5` — and devices
talk in messages a few bytes long.

The obvious way to write this is to fire off the same handful of messages every
time. I did that first, and it's annoying: the TV flickers when it's already on,
and if you have no receiver, every single wake sends a request to address `5`
that nobody answers.

So instead, every wake starts by asking three questions:

```
   Is the TV on?              →  <Report Power Status>
   Who's on screen?           →  <Active Source>
   Is there a receiver?       →  a poll of address 5
```

and then sends only what the answers say is missing:

| Situation | What goes out |
| --- | --- |
| TV on, already showing this PC, no receiver | nothing at all |
| TV on, showing something else | one message — the input claim |
| TV asleep | wake, then claim |
| TV asleep, receiver present | wake, claim, receiver request |
| TV on and showing us, receiver pointed elsewhere | just the receiver request |
| No receiver on the bus | nothing is ever sent to address `5` |

Two rules fell out of using it:

- **A wake always re-claims the input.** If the TV was asleep, whatever it last
  said was on screen is stale information.
- **Claiming the input re-points the receiver too**, or you end up with picture
  from one input and sound from another.

You can watch it decide without touching the TV:

```sh
sudo /etc/cec-hdmi/cec-hook.py on --dry-run
```

---

## The messages

I wanted to be able to read the log and know exactly what was sent and why, so
every message this sends is documented here. A CEC message is one to sixteen
bytes:

```
[header] [opcode] [operand...]
```

The header packs two addresses — sender in the high nibble, recipient in the low
one. `F` as a recipient means broadcast.

```
0x40 0x04
 │ │   └── opcode 0x04, <Image View On>
 │ └────── recipient 0, the TV
 └──────── sender 4, this PC
```

Everything below is shown as sent from logical address **4** with physical address
**3.0.0.0**. Yours will differ — both are read off the adapter at run time. The
test suite asserts these bytes, so this table can't quietly drift from what the
code actually sends.

### Waking

| Bytes | Message | To | What it's for |
| --- | --- | --- | --- |
| `40 04` | `<Image View On>` | TV | Wake up and show a picture. Only sent when the TV says it's asleep. |
| `4F 82 30 00` | `<Active Source>` | broadcast | "I'm what you should be showing." This is the one that actually switches the input. The two operands are the physical address. Broadcast, so other sources know to stand down. |
| `45 70 30 00` | `<System Audio Mode Request>` | receiver | Turn on and take the audio from this input. It carries the physical address, which is why one message both powers a receiver on and picks the right input. Only sent when a receiver actually answers. |
| `4F 86 30 00` | `<Set Stream Path>` | broadcast | Select this HDMI port explicitly. Off by default — most TVs act on the claim above, but some want it spelled out. |
| `40 44 6D` | `<User Control Pressed>` `power-on` | TV | A remote-control power keypress. Off by default. Worth trying if your TV ignores the normal wake but responds to its own remote. |
| `40 45` | `<User Control Released>` | TV | Releases the key above. Always paired with it. |

### Standby

| Bytes | Message | To | What it's for |
| --- | --- | --- | --- |
| `40 36` | `<Standby>` | TV | Go to sleep. |
| `45 36` | `<Standby>` | receiver | Same, when one is there. |
| `4F 36` | `<Standby>` | broadcast | Sleeps everything on the bus, including things that aren't yours. Off by default for that reason. |

### The questions

These are why the lists above stay short.

| Bytes | Message | Answer | What it tells us |
| --- | --- | --- | --- |
| `40` | polling message | ACK / NACK | Is anything at address 0? Just a header byte, no opcode. This is how the TV gets found. |
| `45` | polling message | ACK / NACK | Same at address 5 — this is how it knows whether you have a receiver. |
| `40 8F` | `<Give Device Power Status>` | `<Report Power Status>` | On, asleep, or mid-transition. |
| `4F 85` | `<Request Active Source>` | `<Active Source>` | Which physical address is on screen. |
| `45 7D` | `<Give System Audio Mode Status>` | `<System Audio Mode Status>` | Whether the receiver already has our audio. |

### Also available

| Bytes | Message | What it's for |
| --- | --- | --- |
| `40 0D` | `<Text View On>` | Another way to wake, for TVs that prefer it. |
| `40 9D 30 00` | `<Inactive Source>` | Give the input back so the TV can return to whatever it showed before. |

Anything not here can be sent as raw bytes — see [Extra messages](#extra-messages).

---

## Commands

```sh
sudo /etc/cec-hdmi/cec-hook.py on              # wake and claim the input
sudo /etc/cec-hdmi/cec-hook.py on --dry-run    # decide and print, send nothing
sudo /etc/cec-hdmi/cec-hook.py off             # standby
sudo /etc/cec-hdmi/cec-hook.py status          # adapter, tunneling, bus state
sudo /etc/cec-hdmi/cec-hook.py scan            # everything answering on the bus
```

`scan` is the quickest way to see what you're actually working with:

```
devices answering on this bus:
   0  TV             on, "LG TV"
   4  Playback 1     us
   5  Audio System   on, "DENON-AVR"
```

For the controller side:

```sh
sudo /etc/cec-hdmi/cec-watch.py --detect    # which devices are watched, and why
sudo /etc/cec-hdmi/cec-watch.py --monitor   # live key events
sudo /etc/cec-hdmi/cec-watch.py --dry-run   # run without touching the TV
```

---

## Configuration

Everything lives in `/etc/cec-hdmi/config.conf` as plain `KEY=VALUE`. The file is
parsed, never executed, and a bad value falls back to its default with a note in
the log instead of stopping a wake.

CEC settings apply immediately — the hook re-reads the file every run. Controller
settings need a restart:

```sh
sudo systemctl restart cec-hdmi-controller.service
```

### Identity

| Key | Default | Meaning |
| --- | --- | --- |
| `CEC_DEVICE` | `/dev/cec0` | The CEC device node. |
| `OSD_NAME` | `SteamOS` | The name that shows up in the TV's input list. 14 characters max. |
| `DEVICE_TYPE` | `playback` | `playback`, `tuner` or `recorder`. TVs do treat these differently — if some streaming box works on your TV where this doesn't, matching its type is a cheap thing to try. |
| `CEC_VERSION` | `1.4` | `1.4` or `2.0`. Some TVs take a different path for 2.0 devices. |
| `VENDOR_ID` | *(empty)* | Optional 24-bit vendor ID, decimal. Some TVs unlock vendor-specific behaviour for IDs they recognise. |

### What a wake may send

| Key | Default | Meaning |
| --- | --- | --- |
| `WAKE_TV` | `1` | Allow `<Image View On>`. |
| `CLAIM_SOURCE` | `1` | Allow `<Active Source>`. |
| `WAKE_AUDIO` | `1` | Allow the receiver request, when one is present. |
| `SEND_STREAM_PATH` | `0` | Also send `<Set Stream Path>` before the claim. |
| `SEND_REMOTE_POWER_KEY` | `0` | Also send a remote power keypress before the wake. |

These control what a wake is *allowed* to consider. Turning one off means never
send this — not send it regardless.

### What a standby may send

| Key | Default | Meaning |
| --- | --- | --- |
| `STANDBY_TV` | `1` | Send `<Standby>` to the TV. |
| `STANDBY_AUDIO` | `1` | Send `<Standby>` to a receiver, when present. |
| `STANDBY_BROADCAST` | `0` | One broadcast `<Standby>` instead, sleeping the whole bus. |

### Timing

| Key | Default | Meaning |
| --- | --- | --- |
| `FORCE_ALL_FRAMES` | `0` | Skip the questions and send everything enabled, every time. For TVs that lie about their own state. |
| `FRAME_GAP_MS` | `100` | Milliseconds between messages. |
| `REPLY_TIMEOUT_MS` | `1200` | How long to wait for an answer. |
| `WAKE_ATTEMPTS` | `5` | Attempts before giving up. |
| `WAKE_SETTLE_MS` | `1500` | Pause before the confirmation check. Cosmetic — a wake the TV acknowledged is never downgraded just because the confirmation came back empty. |

### Controller

| Key | Default | Meaning |
| --- | --- | --- |
| `COOLDOWN_SECONDS` | `2.5` | Ignore further presses for this long after one fires. |
| `BUTTON_CODES` | `BTN_MODE BTN_HOME KEY_HOMEPAGE` | Which codes count as Home. Names or numbers, space- or comma-separated. |
| `GAMEPAD_ONLY` | `0` | Only watch devices advertising `BTN_GAMEPAD`. |
| `DRY_RUN` | `0` | Log "would trigger" instead of running the hook. |
| `RESCAN_SECONDS` | `5` | Backup rescan for hotplug; netlink normally makes it instant. |
| `NOTIFY_ON_TRIGGER` | `0` | Desktop notification after a successful wake. |
| `NOTIFY_ON_FAILURE` | `1` | Desktop notification when a wake fails. |
| `LOG_MAX_BYTES` | `1048576` | Rotate each log at this size. `0` turns rotation off. |
| `LOG_KEEP` | `2` | How many rotated logs to keep. |

### Extra messages

`EXTRA_WAKE_FRAMES` and `EXTRA_STANDBY_FRAMES` tack extra messages onto a plan.
Semicolon separated, and always best effort — nothing here can turn a successful
wake into a failed one.

```sh
EXTRA_WAKE_FRAMES="text-view-on; set-stream-path"
EXTRA_STANDBY_FRAMES="inactive-source"
```

Names you can use: `image-view-on`, `text-view-on`, `active-source`,
`inactive-source`, `set-stream-path`, `system-audio-mode-request`,
`user-control-pressed=<key>` (`power`, `power-on`, `power-off`, `power-toggle`),
`user-control-released`, `standby`, `standby=audio`, `standby=broadcast`.

For anything without a name — vendor-specific commands especially — use raw bytes:

```sh
EXTRA_WAKE_FRAMES="raw:40:44:6D"
```

The sender nibble always gets replaced with the real logical address, so only the
recipient nibble in that first byte matters. A message claiming to come from some
other device confuses the whole bus.

---

## Why suspend breaks CEC on these adapters

This is the part worth reading if you're troubleshooting, and it's why this
project exists at all.

I use a UGREEN DisplayPort→HDMI adapter. CEC worked fine from a cold boot, and
then stopped working the moment the machine had been suspended once. What made it
maddening is that nothing looked wrong: the adapter was still there, the physical
address still read back correctly, every status was healthy. The messages just
silently went nowhere.

The reason is that the CEC controller isn't in the PC at all. It's inside the
adapter, driven over the DisplayPort AUX channel. When the machine suspends, the
adapter loses power and its CEC block resets, which clears DPCD register `0x3001`
(`DP_CEC_TUNNELING_CONTROL`). The kernel never writes it back, because as far as
it's concerned nothing changed — same monitor, same EDID, nothing to reconfigure.

Unplugging the HDMI cable and plugging it back in fixed it every time. So the fix
here is to do that in software: read `0x3001`, and if it's cleared, write the
enable bit back.

Two things I got wrong before getting it right, both of which are now pinned by
tests so they can't come back:

1. **It has to run before every wake attempt**, not once when the service starts.
2. **It has to run *after* claiming a logical address, never before.** Claiming
   resets the adapter and clears the bit all over again. Getting that order
   backwards gives you something that works from a cold boot and fails after
   every single resume — which took me embarrassingly long to spot, because it
   looks like an intermittent fault rather than an ordering bug.

You can check the register any time:

```sh
sudo /etc/cec-hdmi/cec-hook.py status
```

If you're on a real HDMI port rather than an adapter, none of this applies to
you. There's no tunneling register, nothing to repair, and it's skipped with one
line in the log.

> **Everything CEC goes through `cec-hook.py`.** The controller watcher runs it
> as a subprocess instead of importing it — partly so a wake that takes tens of
> seconds can't stall the input loop, and partly so this fix lives in exactly one
> place and can't be bypassed by some future shortcut.

---

## The Home button

Steam owns controller input on SteamOS and can swallow the Guide button for its
own overlay. On my box it doesn't — raw input sees the press fine, in Gaming Mode
as well as Desktop Mode — but I wouldn't assume that's universal, so check yours:

```sh
sudo /etc/cec-hdmi/cec-watch.py --monitor
```

Press Home. If you get a line, you're fine:

```
14:22:31  /dev/input/event3  Microsoft X-Box 360 pad  BTN_MODE  PRESS  code=316  <-- would trigger a wake
```

Test it in Desktop Mode **and** with Gaming Mode on screen. SSH in for the second
one — `/dev/input` doesn't care which interface is in front, so you don't need to
be sitting at the TV. The monitor only reads; it never grabs a device, so Steam,
games and the overlay carry on unaffected while it's running.

To use a different button, note the `code=` number and put either the name or the
number in `BUTTON_CODES`:

```sh
BUTTON_CODES="BTN_MODE 316 KEY_HOMEPAGE"
```

A device gets watched if it advertises any one of them, so listing extras costs
nothing.

> `BTN_MODE` (316) is what basically every gamepad reports for Guide/Home.
> `KEY_HOMEPAGE` (172) is what some Bluetooth pads send on a separate
> keyboard-like node. `BTN_HOME` isn't defined by mainline Linux at all — it's
> kept in the default list for kernels that do define it, and ignored with a note
> where they don't.

### How it behaves

- **Any controller works**, not one hardcoded device.
- **Hotplug is live.** Pads connected or removed while it's running are picked up
  and dropped through the kernel netlink socket, with a periodic rescan as
  backup. No restart needed.
- **Presses are debounced**, which also soaks up the duplicate events you get
  because Steam mirrors a physical pad onto a virtual device — otherwise one
  press fires two wakes.
- **One wake at a time.** Presses during a wake get logged and ignored.

---

## Logs

```sh
tail -f /etc/cec-hdmi/cec-hook.log
tail -f /etc/cec-hdmi/cec-controller.log
journalctl -u cec-hdmi-controller.service -f
```

Every message is logged with its bytes, what it means, and why it was sent:

```
bus state:
  us            3.0.0.0  (logical address 4)
  TV            present, standby
  on screen     nobody answered
  audio system  present, system audio off
sending 3 frame(s), attempt 1/5
  -> 40:04             Image View On                Playback 1 -> TV
     why: the TV is standby
  -> 4F:82:30:00       Active Source                Playback 1 -> all  (3.0.0.0)
     why: nothing on the bus claimed to be on screen
  -> 45:70:30:00       System Audio Mode Request    Playback 1 -> Audio System  (3.0.0.0)
     why: wake the receiver and point it at this input
wake sent and acknowledged; the TV reports on
```

Both logs are size-capped and also go to the journal.

---

## Troubleshooting

### The Home button does nothing

1. `sudo bash install.sh status` — is `cec-hdmi-controller.service` active?
2. `sudo /etc/cec-hdmi/cec-watch.py --detect` — is your controller `WATCHED`?
   `WOULD FAIL - cannot open` means the service isn't running as root.
3. `sudo /etc/cec-hdmi/cec-watch.py --monitor` and press Home. No line at all
   means Steam is eating the button before anything else sees it. A line with an
   unexpected code means that code belongs in `BUTTON_CODES`.
4. `tail -f /etc/cec-hdmi/cec-controller.log` while pressing. A `cooldown` or
   `already in progress` line means detection is working fine and the debounce is
   doing its job.

### The button works but the TV doesn't wake

Detection and CEC are separate problems. Test CEC on its own:

```sh
sudo /etc/cec-hdmi/cec-hook.py status
sudo /etc/cec-hdmi/cec-hook.py on
tail -50 /etc/cec-hdmi/cec-hook.log
```

The log tells you which kind of failure it is:

| In the log | What it means |
| --- | --- |
| `not acknowledged` | Nothing is listening at that address. If tunneling shows as enabled, it's usually a TV whose CEC receiver is still waking up — the retries handle it. |
| `lost bus arbitration` / `low drive` | The bus was busy or noisy, not a missing device. Something else was talking at the same moment. |
| `line error` | Electrical. Suspect the cable or the adapter. |

### Nothing happens and the TV is already on

That's deliberate. Confirm with:

```sh
sudo /etc/cec-hdmi/cec-hook.py on --dry-run
```

If it says nothing is needed while the TV is visibly on the wrong input, your TV
is misreporting its state — some do. Set `FORCE_ALL_FRAMES=1` and it'll stop
asking and just send.

### The receiver doesn't wake

```sh
sudo /etc/cec-hdmi/cec-hook.py scan
```

If nothing shows at address 5, your receiver isn't answering polls and the audio
messages will never fire. Some receivers want a plain keypress instead:

```sh
EXTRA_WAKE_FRAMES="raw:45:44:6D"
```

### Testing without a TV

```sh
sudo systemctl stop cec-hdmi-controller.service
sudo /etc/cec-hdmi/cec-watch.py --dry-run
```

Presses get logged as `DRY RUN - would run` and the TV is never touched.

---

## What gets installed

Everything goes under `/etc`, because SteamOS's root filesystem is read-only and
`/etc` is the writable exception that survives updates.

| Path | What it is |
| --- | --- |
| `/etc/cec-hdmi/cec-hook.py` | Wake and standby. Surveys, plans, sends. |
| `/etc/cec-hdmi/cec-watch.py` | The controller watcher daemon. |
| `/etc/cec-hdmi/cec_frames.py` | The protocol: opcodes, addresses, message construction. |
| `/etc/cec-hdmi/cec_device.py` | The adapter: `/dev/cec0`. |
| `/etc/cec-hdmi/cec_dpcd.py` | The DisplayPort AUX fix. |
| `/etc/cec-hdmi/cec_control.py` | Survey, plan, send, retry. |
| `/etc/cec-hdmi/cec_config.py` | Configuration. |
| `/etc/cec-hdmi/cec_log.py` | Rotating logs. |
| `/etc/cec-hdmi/config.conf` | Your settings. Written once, never overwritten. |
| `/etc/cec-hdmi/config.conf.default` | The shipped defaults, refreshed every install so you can diff against them. |
| `/etc/cec-hdmi/*.log` | Wake and controller logs. |

| Service | When it fires |
| --- | --- |
| `cec-hdmi-power.service` | boot → wake; shutdown and reboot → standby |
| `cec-hdmi-sleep.service` | before suspend → standby |
| `cec-hdmi-resume.service` | after resume → wake |
| `cec-hdmi-controller.service` | always running; Home button → wake |

---

## Project layout

`install.sh` copies the files sitting next to it. It's not self-contained on
purpose, so a lone `install.sh` downloaded on its own tells you what's missing
instead of half-installing.

```
install.sh                    the installer
VERSION                       single source of truth for the version
CHANGELOG.md                  release history
src/cec_frames.py             the protocol: opcodes, addresses, messages
src/cec_device.py             the adapter: /dev/cec0
src/cec_dpcd.py               the DisplayPort AUX fix
src/cec_control.py            survey, plan, send, retry
src/cec_config.py             configuration
src/cec_log.py                rotating logs
src/cec-hook.py               entry point: on / off / status / scan
src/cec-watch.py              entry point: the controller watcher
config/config.conf.default    shipped defaults
systemd/*.service             the four unit files
tests/                        the test suite
```

---

## Development

```sh
bash tests/run.sh        # or: bash install.sh selftest
bash tests/run.sh -v     # verbose
```

No root, no install, nothing outside a temp directory gets touched.

Every hardware call goes through one method, `CecDevice._ioctl`, and the tests
replace exactly that — so the real message construction, the real packing and the
real decision logic all run against a scripted fake bus. Which means **the suite
runs anywhere python3 does**, including a laptop with no CEC hardware in it at
all. I write and test changes on a Windows machine and only then push them to the
SteamOS box.

| Suite | Covers |
| --- | --- |
| `test_frames.py` | the exact bytes of every message, addressing, config specs |
| `test_device.py` | structure sizes and ioctl numbers against `linux/cec.h`, transmit status, polling, question and answer |
| `test_plan.py` | the decision logic — every combination of TV state, active source and receiver presence |
| `test_dpcd.py` | connector and AUX lookup across three sysfs layouts, and the register itself |
| `test_config.py` | parsing, clamping, quoting, log rotation |
| `test_watcher.py` | debounce, raw input decoding, device selection, disconnects |
| `test_hotplug.py` | netlink uevent parsing |
| `test_detect.py` | device enumeration against a fake sysfs tree |
| `test_packaging.py` | version sync, installer coverage, unit files, shipped defaults, no third-party imports, the tunneling ordering rule |

There are more tests here than a project this size normally warrants, and the
reason is that everything in this layer fails *quietly*. A misread capability
bitmask doesn't crash anything — the daemon starts, reports itself perfectly
healthy, and silently decides your controller has no Home button. A CEC message
that's one byte wrong isn't rejected by anything either; it's a completely valid
message that just never does what you meant, and the only symptom is a TV sitting
there doing nothing.

---

## Changelog

Release history is in [CHANGELOG.md](CHANGELOG.md).
