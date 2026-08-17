# steam-hdmi-cec

**Console-style HDMI-CEC control for SteamOS.**

Your TV turns on and switches to the PC when you press Home on a controller.
It goes to standby when the machine sleeps. If there is an AV receiver in the
chain, it wakes and follows. Nothing to launch, nothing to configure after the
first install — it simply behaves the way a games console does.

Built specifically for machines where CEC arrives through a DisplayPort→HDMI
adapter, which is the configuration that usually does not work. See
[The DisplayPort AUX fix](#the-displayport-aux-fix).

---

## Features

- **Wake on Home button.** Any connected controller's Home/Guide button wakes
  the TV and claims its input. Multi-controller, hotplug-aware, debounced.
- **Wake on boot and resume.** The TV comes on with the machine, including
  after suspend.
- **Standby on sleep.** Suspend, reboot or shut down, and the TV follows.
- **Receiver support.** An AV receiver on the bus is woken and switched to this
  input automatically. No receiver on the bus means nothing is sent to one, and
  nothing fails.
- **Sends only what is needed.** Every action begins by asking the bus what
  state it is in. A TV that is already awake and already showing this PC
  receives no messages at all.
- **Survives suspend.** The DisplayPort CEC tunneling register is repaired
  before every wake, which is what makes resume work on adapter-based setups.
- **No dependencies.** System `python3` and nothing else. No `pip`, no venv, no
  `cec-ctl`, no compiler.
- **Readable logs.** Every message is recorded with its raw bytes, its meaning,
  and the reason it was sent.

---

## Requirements

| | |
| --- | --- |
| **OS** | SteamOS, or any Linux with systemd |
| **Python** | 3.6 or newer — the system interpreter is fine |
| **Hardware** | Any CEC adapter exposing `/dev/cec0`. DisplayPort→HDMI adapters using CEC-Tunneling-over-AUX are explicitly supported. |
| **Privileges** | Root, for `/dev/cec0`, `/dev/input/event*` and the DPCD register |

If you are running SteamOS on a desktop machine, turn **off** Steam's own
HDMI-CEC setting first. Two CEC controllers on one bus compete for logical
addresses and will make both behave unpredictably.

---

## Install

```sh
git clone https://github.com/ivanzgroot/steam-hdmi-cec.git
cd steam-hdmi-cec
sudo bash install.sh
```

That is the whole flow. The installer writes every file, creates the systemd
units, enables the services and runs a wake immediately so you can see it work.

It is safe to re-run. After a `git pull`, run it again to update in place — your
`config.conf` is never overwritten.

```sh
sudo bash install.sh status             # services, adapter, tunneling, bus, logs
sudo bash install.sh uninstall          # remove services and units
sudo bash install.sh uninstall --purge  # ...and delete /etc/cec-hdmi entirely
bash install.sh selftest                # run the test suite (no root, changes nothing)
```

---

## How it works

CEC is a small message bus shared by everything plugged into your TV. Devices
have fixed roles — the TV is always address `0`, an audio system is always `5` —
and they talk in short messages a few bytes long.

Every wake begins with three questions.

```
   Is the TV on?              →  <Report Power Status>
   Who is on screen?          →  <Active Source>
   Is there a receiver?       →  a poll of address 5
```

The answers decide what gets sent. Nothing more goes out than the situation
calls for:

| Situation | What is sent |
| --- | --- |
| TV on, already showing this PC, no receiver | nothing at all |
| TV on, showing another input | one message — the source claim |
| TV asleep | wake, then claim |
| TV asleep, receiver present | wake, claim, then the receiver request |
| TV on and showing us, receiver pointed elsewhere | the receiver request alone |
| No receiver on the bus | nothing is ever addressed to `5` |

Two rules shape the rest:

- **A wake always re-claims the input.** If the TV was asleep, whatever it last
  reported as being on screen is stale.
- **Claiming the input also re-points the receiver**, otherwise you get picture
  from one input and sound from another.

You can watch the decision without touching the TV:

```sh
sudo /etc/cec-hdmi/cec-hook.py on --dry-run
```

---

## The messages

A CEC message is one to sixteen bytes:

```
[header] [opcode] [operand...]
```

The header packs two addresses — the sender in the high nibble, the recipient in
the low nibble. `F` as a recipient means broadcast.

```
0x40 0x04
 │ │   └── opcode 0x04, <Image View On>
 │ └────── recipient 0, the TV
 └──────── sender 4, this PC
```

Every message below is shown as sent from logical address **4** with physical
address **3.0.0.0**. Yours will differ — both are read from the adapter at run
time. These bytes are asserted in the test suite, so this table always matches
what the code sends.

### Waking

| Bytes | Message | To | Purpose |
| --- | --- | --- | --- |
| `40 04` | `<Image View On>` | TV | Wake up and show a picture. Sent only when the TV reports that it is asleep. |
| `4F 82 30 00` | `<Active Source>` | broadcast | "I am what you should be showing." The message that switches the TV's input. The two operands are the physical address. Broadcast, so other sources know to stand down. |
| `45 70 30 00` | `<System Audio Mode Request>` | receiver | Turn on and take the audio from this input. Carries the physical address, so one message both powers the receiver on and selects its input. Sent only when a receiver answers. |
| `4F 86 30 00` | `<Set Stream Path>` | broadcast | Select this HDMI port explicitly. Off by default; some TVs want the routing spelled out. |
| `40 44 6D` | `<User Control Pressed>` `power-on` | TV | A remote-control power keypress. Off by default; some TVs ignore the standard wake but obey what their own remote sends. |
| `40 45` | `<User Control Released>` | TV | Releases the key above. Always paired with it. |

### Standby

| Bytes | Message | To | Purpose |
| --- | --- | --- | --- |
| `40 36` | `<Standby>` | TV | Go to sleep. |
| `45 36` | `<Standby>` | receiver | The same, when one is present. |
| `4F 36` | `<Standby>` | broadcast | Sleeps everything on the bus, including devices that are not yours. Off by default. |

### Questions

These are why the lists above stay short.

| Bytes | Message | Answer | What it establishes |
| --- | --- | --- | --- |
| `40` | polling message | ACK / NACK | Is anything at address 0? A header byte with no opcode — this is how the TV is found. |
| `45` | polling message | ACK / NACK | The same at address 5. This is how the project knows whether you have a receiver. |
| `40 8F` | `<Give Device Power Status>` | `<Report Power Status>` | On, asleep, or mid-transition. |
| `4F 85` | `<Request Active Source>` | `<Active Source>` | Which physical address is on screen. |
| `45 7D` | `<Give System Audio Mode Status>` | `<System Audio Mode Status>` | Whether the receiver already has our audio. |

### Also available

| Bytes | Message | Purpose |
| --- | --- | --- |
| `40 0D` | `<Text View On>` | An alternative wake, for TVs that prefer it. |
| `40 9D 30 00` | `<Inactive Source>` | Release the input so the TV can fall back to what it showed before. |

Anything not listed here can be sent as raw bytes — see
[Extra messages](#extra-messages).

---

## Commands

```sh
sudo /etc/cec-hdmi/cec-hook.py on              # wake and claim the input
sudo /etc/cec-hdmi/cec-hook.py on --dry-run    # decide and print, send nothing
sudo /etc/cec-hdmi/cec-hook.py off             # standby
sudo /etc/cec-hdmi/cec-hook.py status          # adapter, tunneling, bus state
sudo /etc/cec-hdmi/cec-hook.py scan            # every device answering on the bus
```

`scan` is the fastest way to see what you actually have:

```
devices answering on this bus:
   0  TV             on, "LG TV"
   4  Playback 1     us
   5  Audio System   on, "DENON-AVR"
```

Controller inspection:

```sh
sudo /etc/cec-hdmi/cec-watch.py --detect    # which devices are watched, and why
sudo /etc/cec-hdmi/cec-watch.py --monitor   # live key events
sudo /etc/cec-hdmi/cec-watch.py --dry-run   # run without touching the TV
```

---

## Configuration

Everything lives in `/etc/cec-hdmi/config.conf` as plain `KEY=VALUE`. The file is
parsed, never executed, and a bad value falls back to its default with a note in
the log rather than stopping a wake.

Changes to the CEC settings take effect immediately — the hook re-reads the file
on every run. Controller settings need:

```sh
sudo systemctl restart cec-hdmi-controller.service
```

### Identity

| Key | Default | Meaning |
| --- | --- | --- |
| `CEC_DEVICE` | `/dev/cec0` | The CEC device node. |
| `OSD_NAME` | `SteamOS` | The name shown in the TV's input list. Maximum 14 characters. |
| `DEVICE_TYPE` | `playback` | `playback`, `tuner` or `recorder`. TVs treat device types differently; if a streaming box works where this does not, matching its type is worth trying. |
| `CEC_VERSION` | `1.4` | `1.4` or `2.0`. Some TVs take a different path for 2.0 devices. |
| `VENDOR_ID` | *(empty)* | Optional 24-bit vendor ID, decimal. Some TVs unlock vendor-specific behaviour for IDs they recognise. |

### What a wake may send

| Key | Default | Meaning |
| --- | --- | --- |
| `WAKE_TV` | `1` | Allow `<Image View On>`. |
| `CLAIM_SOURCE` | `1` | Allow `<Active Source>`. |
| `WAKE_AUDIO` | `1` | Allow the receiver request, when a receiver is present. |
| `SEND_STREAM_PATH` | `0` | Also send `<Set Stream Path>` before the claim. |
| `SEND_REMOTE_POWER_KEY` | `0` | Also send a remote power keypress before the wake. |

These say which messages a wake may *consider*. Turning one off means never send
this — not send it regardless.

### What a standby may send

| Key | Default | Meaning |
| --- | --- | --- |
| `STANDBY_TV` | `1` | Send `<Standby>` to the TV. |
| `STANDBY_AUDIO` | `1` | Send `<Standby>` to a receiver, when present. |
| `STANDBY_BROADCAST` | `0` | Send one broadcast `<Standby>` instead, sleeping everything on the bus. |

### Timing

| Key | Default | Meaning |
| --- | --- | --- |
| `FORCE_ALL_FRAMES` | `0` | Skip the questions and send every enabled message every time. For TVs that misreport their own state. |
| `FRAME_GAP_MS` | `100` | Milliseconds between consecutive messages. |
| `REPLY_TIMEOUT_MS` | `1200` | How long to wait for an answer. |
| `WAKE_ATTEMPTS` | `5` | Attempts before giving up. |
| `WAKE_SETTLE_MS` | `1500` | Pause before the confirmation check. Cosmetic — a wake the TV acknowledged is never downgraded because confirmation came back empty. |

### Controller

| Key | Default | Meaning |
| --- | --- | --- |
| `COOLDOWN_SECONDS` | `2.5` | Ignore further presses for this long after one fires. |
| `BUTTON_CODES` | `BTN_MODE BTN_HOME KEY_HOMEPAGE` | Which codes count as Home. Names or numbers, space- or comma-separated. |
| `GAMEPAD_ONLY` | `0` | Only watch devices advertising `BTN_GAMEPAD`. |
| `DRY_RUN` | `0` | Log "would trigger" instead of running the hook. |
| `RESCAN_SECONDS` | `5` | Safety-net rescan for hotplug; netlink normally makes this instant. |
| `NOTIFY_ON_TRIGGER` | `0` | Desktop notification after a successful wake. |
| `NOTIFY_ON_FAILURE` | `1` | Desktop notification when a wake fails. |
| `LOG_MAX_BYTES` | `1048576` | Rotate each log at this size. `0` disables rotation. |
| `LOG_KEEP` | `2` | How many rotated logs to keep. |

### Extra messages

`EXTRA_WAKE_FRAMES` and `EXTRA_STANDBY_FRAMES` append to a plan. Semicolon
separated, always best effort — nothing here can turn a successful wake into a
failure.

```sh
EXTRA_WAKE_FRAMES="text-view-on; set-stream-path"
EXTRA_STANDBY_FRAMES="inactive-source"
```

Available names: `image-view-on`, `text-view-on`, `active-source`,
`inactive-source`, `set-stream-path`, `system-audio-mode-request`,
`user-control-pressed=<key>` (`power`, `power-on`, `power-off`,
`power-toggle`), `user-control-released`, `standby`, `standby=audio`,
`standby=broadcast`.

Raw bytes cover anything without a name — vendor-specific commands in
particular:

```sh
EXTRA_WAKE_FRAMES="raw:40:44:6D"
```

The sender nibble is always replaced with the real logical address, so only the
recipient nibble matters. A message claiming to come from another device
confuses the entire bus.

---

## The DisplayPort AUX fix

This is the part that makes adapter-based setups work, and it is worth
understanding if you are troubleshooting.

On these machines the CEC controller is not in the PC. It lives inside the
DisplayPort→HDMI adapter and is driven over the DisplayPort AUX channel. When the
machine suspends, the adapter loses power and its CEC block resets, clearing DPCD
register `0x3001` — `DP_CEC_TUNNELING_CONTROL`. The kernel never rewrites it,
because the EDID has not changed and nothing looks like it needs reconfiguring.

The result is a failure that looks exactly like working hardware. The adapter
enumerates. The physical address reads back correctly. Every status is healthy.
And every message is silently refused.

The fix is to write the enable bit back — the software equivalent of reseating
the HDMI cable. Two rules protect it, both asserted by the test suite:

1. **It is applied before every wake attempt**, not once at startup.
2. **It is applied after any logical-address change, never before.** Claiming an
   address resets the adapter and clears the bit again. Reversing that order
   produces a wake that works from a cold boot and fails after every resume.

Check the register at any time:

```sh
sudo /etc/cec-hdmi/cec-hook.py status
```

On a direct HDMI port there is no tunneling register, nothing to repair, and
this is skipped with a single line in the log.

> **All CEC goes through `cec-hook.py`.** The controller watcher runs it as a
> subprocess rather than importing it — partly so a wake that blocks for tens of
> seconds cannot stall the input loop, and partly so this fix exists in exactly
> one place and cannot be bypassed.

---

## The Home button

Steam owns controller input on SteamOS and can consume the Guide button for its
own overlay. Confirm your hardware before relying on this:

```sh
sudo /etc/cec-hdmi/cec-watch.py --monitor
```

Press Home. A line means raw input can see it and everything works:

```
14:22:31  /dev/input/event3  Microsoft X-Box 360 pad  BTN_MODE  PRESS  code=316  <-- would trigger a wake
```

Test in Desktop Mode **and** with Gaming Mode on screen — SSH in for the second,
since `/dev/input` does not care which interface is in front. The monitor is a
passive reader and never grabs a device, so Steam, games and the overlay are
unaffected while it runs.

To use a different button, note the `code=` number and add either the name or the
number to `BUTTON_CODES`:

```sh
BUTTON_CODES="BTN_MODE 316 KEY_HOMEPAGE"
```

A device is watched if it advertises any one of these, so listing extras costs
nothing.

> `BTN_MODE` (316) is what essentially every gamepad reports for Guide/Home.
> `KEY_HOMEPAGE` (172) is what some Bluetooth pads emit on a separate
> keyboard-like node. `BTN_HOME` is not defined by mainline Linux; it stays in
> the default list for kernels that do define it, and is ignored with a note
> where they do not.

### Behaviour

- **Any controller works** — anything advertising a trigger code, not one
  hardcoded device.
- **Hotplug is live.** Controllers connected or removed while running are picked
  up and dropped through the kernel netlink socket, with a periodic rescan as
  backup. No restart needed.
- **Presses are debounced**, which also absorbs the duplicate events Steam
  generates by mirroring a physical pad onto a virtual device.
- **One wake at a time.** Further presses during a wake are logged and ignored.

---

## Logs

```sh
tail -f /etc/cec-hdmi/cec-hook.log
tail -f /etc/cec-hdmi/cec-controller.log
journalctl -u cec-hdmi-controller.service -f
```

Every message is recorded with its bytes, its meaning and its reason:

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
   `WOULD FAIL - cannot open` means the service is not running as root.
3. `sudo /etc/cec-hdmi/cec-watch.py --monitor` — press Home. No line means Steam
   is consuming the button first. An unexpected code means it belongs in
   `BUTTON_CODES`.
4. `tail -f /etc/cec-hdmi/cec-controller.log` while pressing. A `cooldown` or
   `already in progress` line means detection works and the debounce is doing its
   job.

### The button is detected but the TV does not wake

Detection and CEC are independent. Test CEC alone:

```sh
sudo /etc/cec-hdmi/cec-hook.py status
sudo /etc/cec-hdmi/cec-hook.py on
tail -50 /etc/cec-hdmi/cec-hook.log
```

The log names the exact failure:

| In the log | Meaning |
| --- | --- |
| `not acknowledged` | Nothing is listening at that address. With tunneling enabled, usually a TV whose CEC receiver is still waking — the retry escalation handles it. |
| `lost bus arbitration` / `low drive` | The bus was busy or noisy, not a missing device. Something else was talking at the same moment. |
| `line error` | Electrical. Suspect the cable or the adapter. |

### Nothing happens and the TV is already on

That is the design. Confirm with:

```sh
sudo /etc/cec-hdmi/cec-hook.py on --dry-run
```

If it reports that nothing is needed while the TV is visibly on the wrong input,
your TV is misreporting its state. Set `FORCE_ALL_FRAMES=1`.

### The receiver does not wake

```sh
sudo /etc/cec-hdmi/cec-hook.py scan
```

Nothing listed at address 5 means your receiver is not answering polls, and the
audio messages will never fire. Some receivers prefer a plain keypress:

```sh
EXTRA_WAKE_FRAMES="raw:45:44:6D"
```

### Testing without a TV

```sh
sudo systemctl stop cec-hdmi-controller.service
sudo /etc/cec-hdmi/cec-watch.py --dry-run
```

Presses are logged as `DRY RUN - would run` and the TV is never touched.

---

## What gets installed

Everything lives under `/etc`, because SteamOS's root filesystem is read-only and
`/etc` is the writable, update-persistent exception.

| Path | Purpose |
| --- | --- |
| `/etc/cec-hdmi/cec-hook.py` | Wake and standby. Surveys, plans, sends. |
| `/etc/cec-hdmi/cec-watch.py` | The controller watcher daemon. |
| `/etc/cec-hdmi/cec_frames.py` | Protocol: opcodes, addresses, message construction. |
| `/etc/cec-hdmi/cec_device.py` | The adapter: `/dev/cec0`. |
| `/etc/cec-hdmi/cec_dpcd.py` | The DisplayPort AUX fix. |
| `/etc/cec-hdmi/cec_control.py` | Survey, plan, send, retry. |
| `/etc/cec-hdmi/cec_config.py` | Configuration. |
| `/etc/cec-hdmi/cec_log.py` | Rotating logs. |
| `/etc/cec-hdmi/config.conf` | Your settings. Written once, never overwritten. |
| `/etc/cec-hdmi/config.conf.default` | Shipped defaults, refreshed every install so you can diff. |
| `/etc/cec-hdmi/*.log` | Wake and controller logs. |

| Service | Fires |
| --- | --- |
| `cec-hdmi-power.service` | boot → wake; shutdown and reboot → standby |
| `cec-hdmi-sleep.service` | before suspend → standby |
| `cec-hdmi-resume.service` | after resume → wake |
| `cec-hdmi-controller.service` | always running; Home button → wake |

---

## Project layout

`install.sh` copies the files beside it. It is not self-contained, so a lone
`install.sh` downloaded on its own reports what is missing rather than
half-installing.

```
install.sh                    the installer
VERSION                       single source of truth for the version
CHANGELOG.md                  release history
src/cec_frames.py             protocol: opcodes, addresses, messages
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

No root, no install, nothing outside a temporary directory is touched.

Every hardware call passes through a single method, `CecDevice._ioctl`, and the
test harness replaces exactly that — driving the real message construction, the
real packing and the real decision logic against a scripted bus. The suite
therefore **runs anywhere python3 does**, including a Windows or macOS laptop, so
a change can be validated before it reaches the target machine.

| Suite | Covers |
| --- | --- |
| `test_frames.py` | the exact bytes of every message, addressing, configuration specs |
| `test_device.py` | structure sizes and ioctl numbers against `linux/cec.h`, transmit status, polling, question and answer |
| `test_plan.py` | the decision logic: every combination of TV state, active source and receiver presence |
| `test_dpcd.py` | connector and AUX lookup across three sysfs layouts, and the register itself |
| `test_config.py` | parsing, clamping, quoting, log rotation |
| `test_watcher.py` | debounce, raw input decoding, device selection, disconnects |
| `test_hotplug.py` | netlink uevent parsing |
| `test_detect.py` | device enumeration against a fake sysfs tree |
| `test_packaging.py` | version sync, installer coverage, unit files, shipped defaults, no third-party imports, the tunneling ordering rule |

The suite is thorough because this layer fails *quietly*. A misread capability
bitmask does not crash — the daemon starts, reports itself healthy, and silently
decides your controller has no Home button. A CEC message one byte wrong is not
rejected by anything; it is a valid message that simply never does what you
meant, and the only symptom is a TV that does not react.

---

## Changelog

Release history is in [CHANGELOG.md](CHANGELOG.md).
