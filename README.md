# steam-hdmi-cec

HDMI-CEC TV control for a SteamOS box connected to a TV through a UGREEN
DisplayPort→HDMI adapter (CEC-Tunneling-over-AUX).

The TV turns on and switches to this PC when the machine boots, resumes from
suspend, or **when you press Home/Guide on any connected controller**. It goes to
standby when the machine suspends or shuts down. If there is an AV receiver on
the bus it is woken and switched to this input too; if there is not, nothing is
sent to it and nothing fails.

Every CEC message is sent as a **frame** — the raw bytes, straight to the kernel
through `/dev/cec0` — and every message the project can send is documented below,
byte for byte, with the reason it exists.

## Install

```sh
git clone https://github.com/ivanzgroot/steam-hdmi-cec.git
cd steam-hdmi-cec
sudo bash install.sh
```

That is the whole flow. The installer writes every file, creates the systemd
units, and enables and starts the services. It is idempotent — after a `git
pull`, run it again to update in place. Your `config.conf` is never overwritten.

**Requires `python3` and nothing else.** No `cec-ctl`, no `v4l-utils`, no
`pip install`, no venv. CEC is spoken directly to the kernel and the controller
watcher reads `/dev/input` and a netlink socket using only the standard library,
so this works on SteamOS's read-only root with no compiler.

---

## The frames

A CEC message is one to sixteen bytes:

```
[header] [opcode] [operand...]
```

The **header** packs two 4-bit logical addresses — the initiator in the high
nibble, the follower in the low nibble. `F` as a follower means broadcast.

```
0x40 0x04
 │ │   └── opcode 0x04, <Image View On>
 │ └────── follower 0, the TV
 └──────── initiator 4, us
```

Logical addresses are roles, not ports: `0` is always the TV, `5` is always the
audio system, and `4`/`8`/`11` are playback devices — which is what this PC
registers as.

Every frame below is shown as sent from logical address **4** with physical
address **3.0.0.0**. Your addresses will differ; the bytes are built at run time
from whatever the adapter reports. All of these are pinned in
`tests/test_frames.py`, so this table cannot drift from what the code sends.

### Frames a wake can send

| Bytes | Message | To | What it does, and why |
| --- | --- | --- | --- |
| `40 04` | `<Image View On>` | TV | "Wake up and show a picture." The standard, polite power-on. Sent **only when the TV reports that it is asleep.** |
| `4F 82 30 00` | `<Active Source>` | broadcast | "I am what you should be showing." The frame that actually switches the TV's input to this PC. The two operand bytes are our physical address. Broadcast, because every other source needs to know to stand down. Sent **only when the TV is showing something else**, or when we just woke it. |
| `45 70 30 00` | `<System Audio Mode Request>` | audio system | "Turn on and take the audio from this input." One frame that both powers a receiver on *and* selects its input, because it carries our physical address. Sent **only when a receiver actually answered a poll.** |
| `4F 86 30 00` | `<Set Stream Path>` | broadcast | "Switch to this port." Normally the TV's job to send; a source sending it is a nudge for TVs that want routing spelled out. **Off by default** — enable with `SEND_STREAM_PATH=1`. |
| `40 44 6D` | `<User Control Pressed>` `power-on` | TV | A remote-control power keypress. Some TVs ignore `<Image View On>` from a device they do not recognise but obey this, because it is what their own remote sends. **Off by default** — enable with `SEND_REMOTE_POWER_KEY=1`. |
| `40 45` | `<User Control Released>` | TV | Releases the key above, as a real remote would. Always paired with it. |

### Frames a standby sends

| Bytes | Message | To | What it does, and why |
| --- | --- | --- | --- |
| `40 36` | `<Standby>` | TV | "Go to sleep." Sent on suspend, shutdown and reboot. |
| `45 36` | `<Standby>` | audio system | The same, for a receiver — **only when one is present.** |
| `4F 36` | `<Standby>` | broadcast | Sleeps *everything* on the bus, including devices that are not yours. **Off by default** — enable with `STANDBY_BROADCAST=1`. |

### Questions the survey asks first

These are why the lists above are as short as they are. Each is a frame out and
a frame back.

| Bytes | Message | Answer | What it tells us |
| --- | --- | --- | --- |
| `40` | polling message | ACK or NACK | Is anything holding logical address 0? A header byte with no opcode. This is how the TV is found. |
| `45` | polling message | ACK or NACK | The same for address 5 — **this is how the project knows whether you have a receiver.** No answer means no receiver, and every audio frame is skipped. |
| `40 8F` | `<Give Device Power Status>` | `<Report Power Status>` | Is the TV on, off, or mid-transition? An already-on TV needs no wake frame. |
| `4F 85` | `<Request Active Source>` | `<Active Source>` | Which physical address is on screen right now? If it is ours, no claim is needed. Broadcast question, broadcast answer. |
| `45 7D` | `<Give System Audio Mode Status>` | `<System Audio Mode Status>` | Is the receiver already handling our audio? |

### Also available, via config

| Bytes | Message | Purpose |
| --- | --- | --- |
| `40 0D` | `<Text View On>` | Like `<Image View On>`, for TVs that prefer it. |
| `40 9D 30 00` | `<Inactive Source>` | "I am no longer what you should be showing", so the TV can fall back to its previous input. |

Anything not listed here can still be sent as raw bytes — see
[Escape hatches](#escape-hatches).

---

## How a wake decides what to send

A wake surveys the bus, then sends only what the answers say is missing. This is
the core of the design, and the reason a wake is usually one or two frames
rather than four.

```
  ask: is the TV on?          40 8F  ->  <Report Power Status>
  ask: who is on screen?      4F 85  ->  <Active Source>
  ask: is there a receiver?   45     ->  ACK or NACK
                    │
                    ▼
        ┌───────────────────────┐
        │ TV asleep?            │──yes──▶  40 04           wake it
        └───────────────────────┘
        ┌───────────────────────┐
        │ someone else on screen│──yes──▶  4F 82 30 00     claim the input
        │ (or we just woke it)  │
        └───────────────────────┘
        ┌───────────────────────┐
        │ receiver present AND  │──yes──▶  45 70 30 00     wake + switch it
        │ (audio off OR we just │
        │  claimed the input)   │
        └───────────────────────┘
```

What that means in practice:

| Situation | Frames sent |
| --- | --- |
| TV on, already showing this PC, no receiver | **none at all** |
| TV on, showing the Blu-ray player | `4F 82 30 00` — one frame |
| TV asleep | `40 04`, `4F 82 30 00` |
| TV asleep, receiver present | `40 04`, `4F 82 30 00`, `45 70 30 00` |
| TV on and showing us, receiver on but pointed elsewhere | `45 70 30 00` |
| Anything, with no receiver on the bus | nothing is ever sent to address 5 |

Two details worth knowing:

**A wake always re-claims the input.** If the TV was asleep, whatever it last
reported as the active source is stale, so the claim goes out regardless.

**Claiming the input also re-points the receiver.** Otherwise you get picture
from one input and sound from another.

You can see the decision without touching the TV:

```sh
sudo /etc/cec-hdmi/cec-hook.py on --dry-run
```

If your TV lies about its state — some report "on" while displaying nothing —
set `FORCE_ALL_FRAMES=1` and every enabled frame is sent unconditionally, the way
this project behaved before it learned to ask questions.

---

## Commands

```sh
sudo bash install.sh                    # install or update
sudo bash install.sh status             # services, adapter, tunneling, bus, logs
sudo bash install.sh uninstall          # remove services and units
sudo bash install.sh uninstall --purge  # ...and delete /etc/cec-hdmi entirely
bash install.sh selftest                # run the test suite (no root, changes nothing)
bash install.sh --help                  # full usage
```

Drive CEC by hand at any time:

```sh
sudo /etc/cec-hdmi/cec-hook.py on              # wake and claim the input
sudo /etc/cec-hdmi/cec-hook.py on --dry-run    # decide and print, send nothing
sudo /etc/cec-hdmi/cec-hook.py off             # standby
sudo /etc/cec-hdmi/cec-hook.py status          # adapter, tunneling bit, bus state
sudo /etc/cec-hdmi/cec-hook.py scan            # every device answering on the bus
```

`scan` is the quickest way to find out what you actually have:

```
devices answering on this bus:
   0  TV             on, "LG TV"
   4  Playback 1     us
   5  Audio System   on, "DENON-AVR"
```

Inspect controller detection:

```sh
sudo /etc/cec-hdmi/cec-watch.py --detect    # what is watched, and why
sudo /etc/cec-hdmi/cec-watch.py --monitor   # live key events
sudo /etc/cec-hdmi/cec-watch.py --dry-run   # run without touching the TV
```

---

## What gets installed

Everything lives under `/etc`, because SteamOS's root filesystem is read-only
(`steamos-readonly`) and `/etc` is the writable, update-persistent exception.

| Path | Purpose |
| --- | --- |
| `/etc/cec-hdmi/cec-hook.py` | The wake/standby entry point. Surveys, plans, sends. |
| `/etc/cec-hdmi/cec-watch.py` | The controller watcher daemon. |
| `/etc/cec-hdmi/cec_frames.py` | Protocol: opcodes, addresses, frame construction. |
| `/etc/cec-hdmi/cec_device.py` | The adapter: `/dev/cec0` ioctls. |
| `/etc/cec-hdmi/cec_dpcd.py` | The DisplayPort tunneling fix. |
| `/etc/cec-hdmi/cec_control.py` | Survey, plan, send, retry. |
| `/etc/cec-hdmi/cec_config.py` | Config parsing. |
| `/etc/cec-hdmi/cec_log.py` | Rotating logs. |
| `/etc/cec-hdmi/config.conf` | Your settings. Written once, never overwritten. |
| `/etc/cec-hdmi/config.conf.default` | Shipped defaults, rewritten every install so you can diff after an upgrade. |
| `/etc/cec-hdmi/cec-hook.log` | Wake/standby log. |
| `/etc/cec-hdmi/cec-controller.log` | Controller watcher log. |

Services:

| Unit | Fires |
| --- | --- |
| `cec-hdmi-power.service` | boot → wake; shutdown/reboot → standby |
| `cec-hdmi-sleep.service` | before suspend → standby |
| `cec-hdmi-resume.service` | after resume → wake |
| `cec-hdmi-controller.service` | always running; controller Home button → wake |

---

## Configuration

Edit `/etc/cec-hdmi/config.conf`. Everything above the controller section takes
effect immediately — `cec-hook.py` re-reads the file on every run. Only the
controller keys need:

```sh
sudo systemctl restart cec-hdmi-controller.service
```

Plain `KEY=VALUE`, parsed by Python and **never executed**. A bad value falls back
to its default, records a note in the log, and the wake proceeds.

### Adapter and identity

| Key | Default | Meaning |
| --- | --- | --- |
| `CEC_DEVICE` | `/dev/cec0` | The CEC device node. |
| `OSD_NAME` | `SteamOS` | The name this PC shows in the TV's input list. Max 14 characters. |
| `DEVICE_TYPE` | `playback` | `playback`, `tuner` or `recorder`. TVs genuinely treat device types differently — if a streaming box works on your TV when this does not, matching its type is a cheap thing to try. |
| `CEC_VERSION` | `1.4` | `1.4` or `2.0`. Some TVs take a different code path for 2.0 devices. |
| `VENDOR_ID` | *(empty)* | Optional 24-bit vendor ID, decimal. Some TVs unlock vendor-specific behaviour for IDs they recognise. |

### What a wake may send

| Key | Default | Meaning |
| --- | --- | --- |
| `WAKE_TV` | `1` | Allow `<Image View On>`. |
| `CLAIM_SOURCE` | `1` | Allow `<Active Source>`. |
| `WAKE_AUDIO` | `1` | Allow `<System Audio Mode Request>`, when a receiver is present. |
| `SEND_STREAM_PATH` | `0` | Also send `<Set Stream Path>` before the claim. |
| `SEND_REMOTE_POWER_KEY` | `0` | Also send a remote power keypress before the wake. |

These say which frames a wake may *consider*. Turning one off means "never send
this", not "send it anyway".

### What a standby may send

| Key | Default | Meaning |
| --- | --- | --- |
| `STANDBY_TV` | `1` | Send `<Standby>` to the TV. |
| `STANDBY_AUDIO` | `1` | Send `<Standby>` to a receiver, when present. |
| `STANDBY_BROADCAST` | `0` | Send one broadcast `<Standby>` instead. Sleeps everything on the bus. |

### Timing and retries

| Key | Default | Meaning |
| --- | --- | --- |
| `FORCE_ALL_FRAMES` | `0` | `1` = skip the survey, send every enabled frame every time. |
| `FRAME_GAP_MS` | `100` | Milliseconds between consecutive frames. |
| `REPLY_TIMEOUT_MS` | `1200` | How long to wait for an answer to a question. |
| `WAKE_ATTEMPTS` | `5` | Survey-and-send attempts before giving up. |
| `WAKE_SETTLE_MS` | `1500` | Pause before the confirmation check. Cosmetic — a wake the TV acknowledged is never downgraded because the confirmation came back empty. |

### Escape hatches

`EXTRA_WAKE_FRAMES` and `EXTRA_STANDBY_FRAMES` append frames to a plan.
Semicolon-separated, always best effort — nothing here can turn an otherwise
successful wake into a failure.

Symbolic names:

```sh
EXTRA_WAKE_FRAMES="text-view-on; set-stream-path"
EXTRA_WAKE_FRAMES="user-control-pressed=power-on; user-control-released"
EXTRA_STANDBY_FRAMES="inactive-source"
```

Available: `image-view-on`, `text-view-on`, `active-source`, `inactive-source`,
`set-stream-path`, `system-audio-mode-request`, `user-control-pressed=<key>`
(`power`, `power-on`, `power-off`, `power-toggle`), `user-control-released`,
`standby`, `standby=audio`, `standby=broadcast`.

Or raw bytes, for anything with no name here — **vendor-specific commands above
all**, which have no symbolic form and cannot be expressed any other way:

```sh
EXTRA_WAKE_FRAMES="raw:40:44:6D"
```

The first byte's initiator nibble is replaced with our real logical address, so
only its destination nibble matters. A frame claiming to come from another device
is the one mistake that confuses a whole bus.

### Controller watcher

| Key | Default | Meaning |
| --- | --- | --- |
| `COOLDOWN_SECONDS` | `2.5` | Ignore further Home presses for this long after one fires. |
| `BUTTON_CODES` | `BTN_MODE BTN_HOME KEY_HOMEPAGE` | Which codes count as Home. Names or numbers, space- or comma-separated. |
| `GAMEPAD_ONLY` | `0` | `1` = only watch devices advertising `BTN_GAMEPAD`. |
| `DRY_RUN` | `0` | `1` = log "would trigger" instead of running the hook. |
| `RESCAN_SECONDS` | `5` | Safety-net rescan for hotplug; netlink normally makes this instant. |
| `NOTIFY_ON_TRIGGER` | `0` | Desktop notification after a successful wake. |
| `NOTIFY_ON_FAILURE` | `1` | Desktop notification when a wake fails. |
| `LOG_MAX_BYTES` | `1048576` | Rotate each log at this size. `0` disables rotation. |
| `LOG_KEEP` | `2` | How many rotated logs to keep. |

---

## The suspend/resume fix

This is the non-obvious part of the project, and the reason nothing else should
ever reimplement CEC sending.

The CEC controller is not in the PC. It lives inside the DP→HDMI dongle and is
driven over the DisplayPort AUX channel. On suspend the dongle loses power and
its CEC block resets, clearing DPCD register `0x3001`
(`DP_CEC_TUNNELING_CONTROL`). The kernel never rewrites it, because the EDID has
not changed and it sees no reason to reconfigure the adapter.

The result is a failure that looks exactly like working hardware: the adapter
enumerates, the physical address still reads back fine, every status looks
healthy — and every directed CEC frame is silently NACKed.

`cec_dpcd.py` reads `0x3001` over `/dev/drm_dp_auxN` before each wake attempt and
rewrites it to `0x01` if it is clear. That is the software equivalent of
reseating the HDMI cable.

Two rules protect it, and both are pinned by tests:

1. **It is applied before every wake attempt**, not once at startup.
2. **It is applied *after* any logical-address reconfiguration, never before.**
   Claiming a logical address resets the adapter and clears the bit again.
   Getting this order wrong produces a wake that works from a cold boot and fails
   after every resume — which is the exact bug this file exists to kill.

Check it any time:

```sh
sudo /etc/cec-hdmi/cec-hook.py status
```

**Any new code path that needs a wake must run `cec-hook.py on`.** Do not
duplicate the CEC sequence elsewhere; it exists in exactly one place so this fix
cannot be lost. The controller watcher deliberately runs it as a subprocess
rather than importing it, for that reason and because a wake can block for tens
of seconds while the input loop has to keep reading.

If you are on a direct HDMI port rather than a DisplayPort adapter, there is no
tunneling bit, nothing to fix, and everything above is skipped with one line in
the log.

---

## Before you trust the Home button: check Steam is not eating it

Steam owns controller input on SteamOS and may consume or remap the Guide/Home
button for its own overlay. **Verify on your hardware before relying on this:**

```sh
sudo /etc/cec-hdmi/cec-watch.py --monitor
```

Press Home on your controller. If you see a line for it, raw evdev can see the
button and everything here works:

```
14:22:31  /dev/input/event3  Microsoft X-Box 360 pad  BTN_MODE  PRESS  code=316  <-- would trigger a wake
```

Run it in Desktop Mode **and** with Gaming Mode on screen. SSH in for the Gaming
Mode test — `/dev/input` does not care which UI is in front.

`--monitor` is a passive reader. It never calls `EVIOCGRAB`, so it does not steal
input from Steam, games, or the overlay while it runs.

### Changing the trigger button

Run `--monitor`, press your button, note the `code=` number, and put either the
name or the number in `BUTTON_CODES`:

```sh
BUTTON_CODES="BTN_MODE 316 KEY_HOMEPAGE"
```

A device is watched if it advertises at least one of these codes, so listing
extra codes costs nothing.

> **On `BTN_HOME`:** mainline Linux `input-event-codes.h` does not define it —
> `BTN_MODE` (316) is what essentially every gamepad reports for Guide/Home, and
> `KEY_HOMEPAGE` (172) is what some Bluetooth pads emit on a separate
> keyboard-like node. `BTN_HOME` is kept in the default list for kernels that do
> define it; when they do not, it is ignored with a one-line note.

## How it behaves

- **All controllers.** Any connected device advertising a trigger code works.
- **Hotplug.** Controllers plugged in or removed while the service runs are
  picked up and dropped live via the kernel netlink uevent socket, backed up by a
  periodic rescan. No restart needed.
- **Debounced.** After a trigger, further presses are ignored for
  `COOLDOWN_SECONDS`. This also absorbs the duplicate events you get when Steam
  mirrors a physical pad onto a virtual uinput device.
- **Non-blocking.** The hook runs as a subprocess while the input loop keeps
  reading.
- **One wake at a time.** While a wake is in flight, further presses are logged
  and ignored rather than starting a second sequence.

---

## Logs

```sh
tail -f /etc/cec-hdmi/cec-hook.log
tail -f /etc/cec-hdmi/cec-controller.log
journalctl -u cec-hdmi-controller.service -f
```

Every frame is logged with its bytes, its meaning, and why it was sent:

```
bus state:
  us            3.0.0.0  (logical address 4)
  TV            present, standby
  on screen     nobody answered
  audio system  present, system audio off
sending 3 frame(s), attempt 1/5
  -> 40:04             Image View On                Playback 1 -> TV
     why: the TV is asleep
  -> 4F:82:30:00       Active Source                Playback 1 -> all  (3.0.0.0)
     why: the TV is showing something else
  -> 45:70:30:00       System Audio Mode Request    Playback 1 -> Audio System  (3.0.0.0)
     why: wake the receiver and point it at this input
wake sent and acknowledged; the TV reports on
```

---

## Troubleshooting

### The Home button does nothing

1. `sudo bash install.sh status` — is `cec-hdmi-controller.service` active?
2. `sudo /etc/cec-hdmi/cec-watch.py --detect` — is your controller listed as
   `WATCHED`? `WOULD FAIL - cannot open` means the service is not running as root.
3. `sudo /etc/cec-hdmi/cec-watch.py --monitor` — press Home. No line at all means
   Steam is consuming the button before evdev sees it; a line with an unexpected
   code means you need to add that code to `BUTTON_CODES`.
4. `tail -f /etc/cec-hdmi/cec-controller.log` while pressing — a `cooldown` or
   `already in progress` line means detection works and the debounce is doing its
   job.

### The button is detected but the TV does not wake

Detection and CEC are separate. Test CEC on its own:

```sh
sudo /etc/cec-hdmi/cec-hook.py status
sudo /etc/cec-hdmi/cec-hook.py on
tail -50 /etc/cec-hdmi/cec-hook.log
```

The log names the exact failure now rather than reporting a generic one:

- **`not acknowledged`** — nothing is listening at that address. If tunneling
  shows as enabled, the TV's CEC receiver is probably still waking; the retry
  escalation handles it.
- **`lost bus arbitration`** or **`low drive`** — the bus was busy or noisy, not a
  missing device. Another device was talking at the same moment.
- **`line error`** — electrical. Suspect the cable or adapter.

### Nothing happens and the TV is already on

That is the design. Check with `--dry-run`:

```sh
sudo /etc/cec-hdmi/cec-hook.py on --dry-run
```

If it says it would send nothing and your TV is nevertheless showing the wrong
input, your TV is misreporting its state — set `FORCE_ALL_FRAMES=1`.

### The receiver does not wake

```sh
sudo /etc/cec-hdmi/cec-hook.py scan
```

If nothing is listed at address 5, your receiver is not answering polls and
`WAKE_AUDIO` will never fire. Some receivers use a different address or want a
plain keypress instead:

```sh
EXTRA_WAKE_FRAMES="raw:45:44:6D"
```

### Testing without the TV

```sh
sudo systemctl stop cec-hdmi-controller.service
sudo /etc/cec-hdmi/cec-watch.py --dry-run
```

Button presses are logged as `DRY RUN - would run` and the TV is never touched.

---

## Repo layout

`install.sh` is a plain installer: it copies the files that live next to it. It
is **not** self-contained, so running a lone `install.sh` downloaded on its own
will tell you what is missing rather than half-installing.

```
install.sh                    the installer (shell only)
VERSION                       single source of truth for the version
src/cec_frames.py             protocol: opcodes, addresses, frame construction
src/cec_device.py             the adapter: /dev/cec0 ioctls
src/cec_dpcd.py               the DisplayPort tunneling fix
src/cec_control.py            survey, plan, send, retry
src/cec_config.py             config parsing
src/cec_log.py                rotating logs
src/cec-hook.py               entry point: on / off / status / scan
src/cec-watch.py              entry point: the controller watcher daemon
config/config.conf.default    shipped defaults
systemd/*.service             the four unit files
tests/                        test suite, run by `install.sh selftest`
```

## Development

```sh
bash install.sh selftest      # or: bash tests/run.sh
bash tests/run.sh -v          # verbose
```

No root, no install, nothing outside a temp directory is touched. Every ioctl
goes through one method — `CecDevice._ioctl` — and the suite replaces exactly
that, driving the real transmit path, the real struct packing and the real
decision logic against a scripted fake bus. So **it runs anywhere python3 does**,
including a Windows dev box: you can validate a change on a laptop before pushing
it to the SteamOS machine.

| Suite | Covers |
| --- | --- |
| `test_frames.py` | the exact bytes of every frame, addressing, config frame specs |
| `test_device.py` | struct sizes and ioctl numbers against `linux/cec.h`, transmit-status decoding, polling, question/answer round-trips |
| `test_plan.py` | the survey-and-decide state machine: every combination of TV state, active source and receiver presence, and the frames each produces |
| `test_dpcd.py` | connector and AUX-device lookup across three sysfs layouts, and the register read/write |
| `test_config.py` | config parsing, clamping, quoting, log rotation |
| `test_watcher.py` | debounce, raw `input_event` decoding, device selection, disconnects |
| `test_hotplug.py` | netlink uevent parsing (add / remove / malformed) |
| `test_detect.py` | device enumeration and `--detect`, against a fake sysfs tree |
| `test_packaging.py` | version sync, installer copies every shipped file, units match `SERVICES`, shipped defaults match the code's defaults, no third-party imports, the tunneling ordering invariant |

The suite exists because this layer fails *quietly*. A misparsed capability
bitmask does not crash — the daemon starts, reports itself healthy, and silently
decides your controller has no Home button. The kernel writes those bitmaps with
`%lx` per `long`, **unpadded**, so word size must come from `sizeof(long)` and
never from how long the hex words look; getting that wrong shifts every button
number. `test_watcher.py` pins it.

Likewise a CEC frame that is one byte wrong is not rejected by anything. It is a
valid frame that simply never does what you meant, and the only symptom is a TV
that does not react. `test_frames.py` pins every byte.

---

## Upgrading from 2.x

Version 3 replaced `cec-ctl` subprocesses with CEC frames sent straight to the
kernel. The behaviour and the four triggers are unchanged; the configuration is
not.

**The old `CEC_WAKE_COMMANDS` / `CEC_STANDBY_COMMANDS` / `CEC_AUDIO_COMMANDS`
keys no longer exist.** They held `cec-ctl` command-line arguments, which have no
meaning now. Their replacements are the `WAKE_*` / `STANDBY_*` switches above:
instead of listing which messages to send, you say which the wake is *allowed* to
send, and it works out which are actually needed.

Run the installer, then diff your config against the new defaults:

```sh
sudo bash install.sh
diff /etc/cec-hdmi/config.conf /etc/cec-hdmi/config.conf.default
```

Your old keys will simply be ignored. The closest equivalent of the old
always-send-everything behaviour is `FORCE_ALL_FRAMES=1`.

`cec-hook.sh` is now `cec-hook.py` and `cec-controller-watch.py` is now
`cec-watch.py`; the installer updates the systemd units for you. `v4l-utils` is
no longer required and can be removed if nothing else on the box uses it.
