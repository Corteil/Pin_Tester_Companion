# Pin Tester companion

A desktop GUI and command-line tool for the [Pin Tester](https://github.com/Corteil/Pin_Tester)
Tildagon badge app. Plug the badge into a computer over USB and drive or inspect all nine
hexpansion port pins (LS_A-LS_E, HS_F-HS_I) from there: set modes, run PWM and LED fades, watch
live levels, switch ports.

It talks to the badge app's plain-text remote protocol over the badge's USB serial port, so the
badge app has to be **running with Remote: ON**: open Pin Tester, go to the STATUS tile and press
CONFIRM until it says `Remote: ON`. (That switch is deliberate -- with it OFF the badge ignores
everything, so pins can never change remotely by surprise.)

Needs the [Pin Tester badge app](https://github.com/Corteil/Pin_Tester) at **version 4 or newer** (version 5 for the blink high/low timing sliders)
(remote protocol 2-3: `PING`, `DUMP`, `PORT`, `RESET`, `PORTINFO` and the `ANALOG` mode). Install it
on the badge first (from the Tildagon app store, or see that repo's README for sideloading); an
older version will connect but answer `ERR bad command` to the newer commands.

## Install

```sh
pip install pyserial          # the only dependency; tkinter ships with Python (python3-tk on Debian)
python3 -m pintester gui      # from this folder
# or: pip install .  and then just  pintester gui
```

No badge handy? Add `--sim` to anything to use a built-in simulated badge.

A desktop launcher is included: copy `pin-tester-companion.desktop` to `~/Desktop` (and
`~/.local/share/applications` for the menu), make it executable, and edit the two `/home/pi/...`
paths in it if you keep the folder elsewhere. Right-click it for *Open with simulated badge*.

## GUI

`pintester gui` (or `./pintester.sh gui`):

- **Device / Connect** -- pick the badge's serial port (auto-detected) or the demo simulator. The
  status chip shows *Connected*, or amber *Badge not answering* when Remote is still OFF -- it
  keeps retrying, so just switch Remote ON on the badge.
- **Hexpansion port 1-6** -- changes the badge's port (pins reset for the new connector). Shows
  whether the badge's detect line says a hexpansion is plugged in.
- **Hexpansion card** -- name, VID/PID and unique ID from the hexpansion's EEPROM header, plus tags for
  the detect line, the EEPROM (address, size, page size), the mounted filesystem (files, app) and every
  I2C address that answered -- all of them, wrapping onto extra rows if there are many (with an
  "I2C: N devices" count tag once there are more than 6). It rescans the I2C bus when you change port or plug/unplug something
  (and on **Rescan I2C**); otherwise it only refreshes the cheap cached view.
- **One row per pin** -- live level dot (green high, red low, amber glow for LED/PWM output
  strength), a mode selector, a one-line description, and controls for that mode:
  - *Output*: one dropdown entry with a **Low / High slide switch** (choosing Output starts low)
  - *Auto*: the badge's OUTPUT: AUTO blink, with separate **high** and **low** time sliders
    (250 ms - 5 s, log scale)
  - *LED*: one dropdown entry with an **On / Off slide switch** and a brightness slider (choosing LED
    starts it off)
  - *LED breathe*: brightness (peak) and cycle time (0.2-10 s)
  - *PWM*: frequency (10 Hz-100 kHz) and duty slider
  - *Analog in*: live voltage readout and bar (0-3.1 V). Offered only on pins that have an ADC
    channel -- HS pins on ports 4, 5 and 6 -- which carry an `ADC1 ch2`-style tag. Ports 1-3 and
    all LS pins can't do analog.
- **The mode dropdown reflects the pin**: it only lists what that pin type can do (LS pins get the LED
  modes, HS pins get PWM and, on ports 4-6, Analog in), and for a pin left in inspect only it shows what
  the pin turned out to be -- *Inspect: input*, *output*, *peripheral*, *PWM output* or *LED mode*.
- **Inspect only** is read-only: it shows how a pin is *already* configured (direction, level,
  pull, routed to a peripheral) without touching it -- safe on a port something else is using.
- **Inspect only / Modify** -- the window starts in *Inspect only*: every pin control is locked, so
  you can look but can't change anything by accident. Switch to *Modify* to unlock them. Switching
  sends **nothing** to the badge -- no pin is touched until you then pick a mode or move a slider.
  It drops back to *Inspect only* on disconnect and when you change port.
- **Reset all pins** (Modify only) -- hand every pin back. **Live** -- pause or resume polling.
- **Console** -- type raw protocol lines (`SET HS_F PWM 1000 50`), with Up/Down history.

## Command line

```sh
pintester ports                       # list serial ports
pintester dump                        # full table for the current port  (--json for scripts)
pintester status                      # just the live levels
pintester info [--scan]               # hexpansion EEPROM / I2C details  (--json for scripts)
pintester inspect HS_F                # fresh read-only snapshot
pintester in LS_C --latch             # input that remembers ever-high / ever-low
pintester out LS_D on                 # on | off
pintester out LS_D auto 500 1500      # auto (blink): 500 ms high, 1.5 s low (default 250 / 250; 'blink' also works)
pintester pwm HS_G 5000 70            # 5 kHz, 70 % (frequency snaps to 10,20,50,100... steps)
pintester led LS_A on 40              # LED at 40 %      (off | on [%] | breathe [seconds])
pintester led LS_B breathe 1.5
pintester analog HS_F                 # read a voltage (ports 4-6 only); --watch for a live bar, --json
pintester inspect-only all            # put pins in inspect only (releases them; alias: hands-off)
pintester port 3                      # change port; no argument shows the current one
pintester reset
pintester monitor                     # live table until Ctrl-C
pintester raw SET HS_F ON             # any protocol line, as-is
pintester shell                       # interactive prompt with tab completion and history
```

`-d /dev/ttyACM0` picks a port; the default auto-detects the badge (Espressif USB). In the shell,
lowercase words are the commands above, and an UPPERCASE first word is sent to the badge as a raw
protocol line (`GET HS_G`, `DUMP`...). Exit codes: 0 ok, 1 error (message on stderr).

## Protocol (badge side)

One command per line over the USB serial port; all replies are lines starting with a known word,
everything else on the port (REPL noise, tracebacks) is ignored.

| Command | Reply |
|---|---|
| `PING` | `PONG proto=3` |
| `STATUS` | `STATUS LS_A=1 LS_B=0 ...` |
| `GET <pin>` | `GET <pin> <level>` |
| `DUMP` | `DUMP port=1 present=1 detect=1 proto=3 frame=<ms>`, nine `PIN <name> mode=... level=... key=value...` lines, `END` |
| `PORTINFO [SCAN]` | `PORTINFO port=.. present=.. detect=.. eeprom=.. files=.. app=.. scan=.. [addr=.. uid=.. size=.. page=.. ver=.. vid=.. pid=..]`, then optional `NAME ...`, `I2C <decimal addrs>`, `SCANERR ...`, `END`; `SCAN` does a fresh I2C bus scan first |
| `INSPECT [<pin>]` | `INSPECT <pin> func=... dir=... level=... [pull=...]` lines, `END` |
| `PORT [n]` | `PORT n`, or `OK PORT n` when changing |
| `RESET` | `OK RESET` |
| `SET <pin> IN\|LATCH\|OFF\|ON\|INSPECT` | `OK <pin> <mode>` |
| `SET <pin> AUTO [hi_ms [lo_ms]]` | blink with the given high / low times (default 250 / 250; resending while blinking updates in place). `DUMP` reports `hi=`, `lo=` and `tog=` (toggle count) |
| `SET <pin> PWM [hz [duty]]` | HS pins; re-sending while already PWM updates in place |
| `SET <pin> ANALOG` | HS pins on ports 4-6 only (`ERR ANALOG needs...` otherwise); `GET <pin>` then returns millivolts, and `DUMP` adds `mv=` / `raw=` |
| `SET <pin> LEDOFF\|LEDON [pct]` | LS pins; `pct` is the brightness setting |
| `SET <pin> BREATHE [n [pct]]` | LS pins; cycle time `n` x 0.1 s, optional peak brightness |
| any error | `ERR <reason>` |

## Notes

- **The blink times can also be set on the badge itself** (OUTPUT: AUTO tile: F picks high/low, UP/DOWN
  change it); the GUI sliders and the badge buttons edit the same two values.
- **Blink timing is only accurate from about 250 ms per phase.** The badge redraws its screen
  between updates and that blocks everything for most of each ~180 ms frame, so shorter high/low
  times are irregular and short pulses can be missed entirely. Measured on a real badge, edges
  achieved vs expected: 250/250 ms 99-102 %, 100/100 ms 0-21 %, 20/20 ms 11-14 %. The GUI sliders
  therefore start at 250 ms; the CLI and protocol accept 10 ms and up but print a warning below 250.
  For fast square waves on an HS pin use **PWM** (accurate from 10 Hz to 100 kHz).
- **Analog reconfigures the pin** (unlike *Inspect only*): it puts the pin in analog mode, so don't
  use it on a pin a hexpansion is driving -- the GPS hexpansion on port 5, for example, probably
  uses its HS pins for a serial link. Ports 4 and 5 use ADC2, which the ESP32 can't read reliably
  while its radio is on (the badge uses ESP-NOW), so readings there may fail or be noisy; port 6
  uses ADC1 and is unaffected. The DUMP of every HS pin includes `adc=` where one exists.
- The serial port is opened with DTR/RTS held low: asserting them can reset an ESP32 or drop it
  into its bootloader, which would kill the running app.
- Don't run `mpremote` against the badge while this is connected -- it sends Ctrl-C, which
  stops the app.
- The GUI keeps all serial traffic on a worker thread, so a slow or missing badge never freezes
  the window.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

They run against the built-in simulator -- no badge or display needed.
