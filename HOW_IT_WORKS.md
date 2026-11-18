# How it works

Read the files in this order. Each one is short. The **new** parts (the
function generator) come first. The scope and meter parts are the same as in
the SCOPE project and are explained at the end.

1. `FuncGen/FuncGen.ioc`: what CubeMX sets up for us
2. `FuncGen/Core/Src/main.c`: the lines we added
3. `FuncGen/Core/Src/generator.c`: **table → DMA → DAC, the frequency maths,
   and the second channel with its phase**
4. `FuncGen/Core/Src/scope.c`: **receiving generator commands**, then measure and send
5. `pc_app/funcgen_window.py`: **the generator panel**, then the scope window

## 1. CubeMX did most of the setup (`FuncGen.ioc`)

The chip's "peripherals" are small hardware blocks that work **on their own**
once they are started. CubeMX writes all their set-up code into `main.c`
(`MX_DAC1_Init()`, `MX_TIM7_Init()`, ...). The generator uses these:

| Block | Set to | Job in the generator |
|-------|--------|----------------------|
| **DAC1** channel 1 (PA4 = A2) | 12 bit, output buffer on, started by "TIM7 trigger out" | turns a number 0..4095 into a voltage 0..VDDA |
| **TIM7** | counts at 170 MHz; PSC and ARR are changed by `generator.c` | the metronome: one tick = one new value |
| **DMA1 channel 3** | memory → DAC, **circular**, memory Half Word, peripheral **Word** | copies the next table value into the DAC on every tick |
| **DAC2** channel 1 (PA6 = D12) | like DAC1, started by "TIM7 trigger out" (`generator.c` switches it to TIM3 when not coupled) | generator CH2 |
| **TIM3** | counts at 170 MHz, "trigger out" = update; PSC and ARR set by `generator.c` | CH2's own metronome (only when not coupled) |
| **DMA1 channel 4** | the same as channel 3, for DAC2 | copies CH2's table into DAC2 |
| **USART2** | 4 250 000 baud, **FIFO on** | receives the PC's commands (and sends the scope data) |

**DAC** = Digital to Analog Converter, the opposite of the ADC.
**DMA** = Direct Memory Access, a helper that copies data while the CPU does
something else. **Circular** means: after the last table value it starts
again at the first one, forever.

The rest is the scope and meter from SCOPE:

| Block | Set to | Why |
|-------|--------|-----|
| ADC1 (PA0 = A0) / ADC2 (PA1 = A1) | 12 bit, started by TIM6 | scope CH1 / CH2 |
| ADC5 | reads VREFINT | supply voltage |
| TIM6 | counts at 10 MHz; its overflow starts ADC1 + ADC2 | sample rate |
| DMA1 ch1/ch2 | ADC result → array `ch1[]` / `ch2[]` | no CPU needed |
| TIM2 (PB3 = D3) | "PWM input" mode, 170 MHz | frequency counter |
| PB0 (A3) | input with pull-up | continuity |

The clock is 170 MHz, the fastest this chip can run. Compared with SCOPE:
USART2 → *Advanced Parameters* → *Fifo Mode* → **Enable** (why: see "Things
we learned the hard way"). And for the second channel, D12 changed from
SCOPE's fixed 2 kHz square (TIM3 PWM) to **DAC2_OUT1**: PA6 → *DAC2_OUT1*;
DAC2 → trigger *Timer 7 Trigger Out event*, DMA request *DAC2_CH1*
(circular, peripheral Word, memory Half Word); TIM3 → no PWM channel,
*Trigger Event Selection* = *Update Event*. The pins are labelled GEN_CH1
(PA4) and GEN_CH2 (PA6).

## 2. `main.c`

CubeMX's `main()` sets up everything, then runs our code:

```c
scope_init();
generator_start(scope_measure_vdda_mv());   /* 1 kHz sine on A2, D12 off */
while (1) {
    scope_run();          /* new generator command? measure once, send it */
}
```

`generator_start()` gets the measured supply voltage (VDDA, about 3.27 V),
because that is what the DAC outputs for the value 4095. It starts CH1
(A2) with a 1 kHz sine and leaves CH2 (D12) off, until the PC asks.

## 3. `generator.c`: the function generator

The whole idea in one picture:

```
   table[0][100]  --DMA-->  DAC1  -->  pin A2    (CH1)
                    ^
                    TIM7 tick: "copy the next value now"
```

Channel 2 is a copy of the same chain: `table[1]` → DMA → DAC2 → D12. Each
channel has its own table, settings and custom drawing (arrays with `[2]`,
indexed by the channel number `ch`: 0 = CH1, 1 = CH2). Parts a) to c) work
the same for both; e) explains how the two work together.

The CPU only **prepares** things. Once it is started, the wave plays on its
own, and the CPU is free to run the scope.

### a) The shapes: `shape()`

Each wave is described from −1 (bottom) to +1 (top), at position `x` in the
period (0 = start, 0.99 = end):

| Wave | Formula | |
|------|---------|--|
| sine | `sin(2π·x)` | smooth |
| square | `+1` for the first half, `−1` for the second | jumps |
| triangle | `−1 + 4x` (up), then `3 − 4x` (down) | straight lines |
| sawtooth | from −1 to +1 across the 100 points, then drop | ramp |
| custom | `custom[i] / 127.5 − 1` (the drawn byte 0 … 255 → −1 … +1) | anything |

The **custom** wave is the one you draw on the PC. `generator_set_custom()`
stores its 100 bytes in `custom[]`, adds them up into `custom_check` (sent
back to the PC, so it can see that the board really has its drawing), and
refills the table if the custom wave is playing. Because it goes through
the same −1 … +1 → `fill_table()` path, amplitude and offset work on it like
on every other wave. That was the simplest choice: no special case. At
start-up, `generator_start()` puts in a "stairs" shape (0, 85, 170, 255).

### b) The lookup table: `fill_table()`

For each of the 100 points:

```
millivolts = offset + amplitude / 2 × shape      (e.g. 1650 + 1000 × sin)
DAC value  = millivolts × 4095 / VDDA_mV          (rounded)
```

So 2.0 V peak to peak around 1.65 V gives 0.65 V … 2.65 V, which is about
the numbers 813 … 3316 (with VDDA = 3.27 V). Values below 0 or above 4095 are **clamped**: the wave
is cut flat ("clipped"), just like a real generator at its limits.

This is a **lookup table**: we calculate the 100 values once (with slow
`sinf()`), and the hardware just reads them again and again.

### c) The speed: `choose_timer()`

TIM7 divides the 170 MHz clock twice: first by `PSC + 1` (the prescaler),
then by `ARR + 1` (it counts 0 … ARR). Each overflow is one tick, and 100
ticks are one period:

```
frequency = 170 000 000 / (PSC + 1) / (ARR + 1) / 100
```

Backwards, for a wanted frequency:

```c
ticks   = round(1 700 000 / freq);     /* (PSC+1) × (ARR+1) we need     */
divide1 = ticks / 65536 + 1;           /* PSC+1: smallest that fits     */
divide2 = round(ticks / divide1);      /* ARR+1                         */
```

Both are 16-bit registers, so each divider is at most 65 536. Above 26 Hz,
`ticks` already fits, so the prescaler stays 1 and only ARR changes. Below
that, the prescaler helps. Using the smallest prescaler keeps ARR large, so
the rounding is small.

**Example, 440 Hz:** 1 700 000 / 440 = 3863.6 → 3864 ticks → PSC + 1 = 1,
ARR + 1 = 3864 → real 170 000 000 / 3864 / 100 = **439.959 Hz**.
**Example, 1 Hz:** 1 700 000 ticks → PSC + 1 = 1 700 000 / 65 536 + 1 = 26,
ARR + 1 = 1 700 000 / 26 = 65 384.6 → 65 385 → **0.999991 Hz**.

The rounding grows with the frequency, because the counts get fewer: 7000 Hz
needs 242.86 counts → 243 → 6995.9 Hz (−0.06 %). The board sends PSC and ARR
to the PC, and the PC calculates and shows that real frequency.

**Why 100 points and at most 10 kHz?** 10 kHz × 100 = 1 000 000 values a
second, the most this DAC is made for with its output buffer on (the buffer
lets the pin drive a wire and an input without the voltage sagging). Keeping
100 points always is the simplest design: one table size, only the timer
changes. (A real generator would use fewer points at high frequencies, or a
"DDS" phase accumulator. That's a nice next project.)

### d) Changing the settings: `generator_set()` and `restart_all()`

`generator_set(ch, wave, freq, amp, offset, on)` limits every number to its
range (1..10 000 Hz, 0..3300 mV, wave 0..4), so a bad command can't do
anything strange, stores it in `settings[ch]`, and calls `restart_all()`.
That restarts **both** channels (shortened here):

```c
for (ch = 0; ch < 2; ch++) {                    /* 1. stop the metronomes */
    HAL_TIM_Base_Stop(timer[ch]);               /*    (TIM7, TIM3)        */
    HAL_DAC_Stop_DMA(dac[ch], DAC_CHANNEL_1);   /*    and the copying     */
}
fill_table(0, 0);                               /* 2. new tables; CH2's   */
fill_table(1, phase_points());                  /*    turned by the phase */
choose_timer(0);  choose_timer(1);              /*    new PSC / ARR, then */
HAL_TIM_Base_Init(...);                         /*    write them (count 0) */
choose_dac2_trigger();                          /*    DAC2: TIM7 or TIM3? */
HAL_DAC_Start_DMA(...);  /* both */             /* 3. go                  */
HAL_TIM_Base_Start(&htim7);
if (!coupled) HAL_TIM_Base_Start(&htim3);
```

Stopping first means the DMA never sends a half-new table. The waves pause
for a few microseconds, then restart at the beginning of their tables.
A channel that is **off** gets a table full of zeros (≈ 0 V), so nothing
else has to change, and switching it on is just a new table.

`generator_settings(ch)`, `generator_phase()` and `generator_coupled()` let
`scope.c` read the current settings, to send them to the PC.

### e) Two channels: coupling and phase

**Coupled** (the default): DAC2 listens to **TIM7**, the same metronome as
DAC1. On every TIM7 tick **both** DMAs copy their next value, so the two
channels always have exactly the same frequency (CH2's own frequency is
not used; TIM3 isn't even started). Both start together from a stopped
timer, so point 0 of `table[0]` and point 0 of `table[1]` always come out
at the same moment.

The **phase** is then simply **what is in** CH2's table. For 90°,
`fill_table(1, 25)` fills it with the wave starting 25 points later:
`table[1][i] = wave(i + 25)` (wrapping round after point 99). When CH1 is at
the start of its period, CH2 is already a quarter period further: CH2 is
**90° ahead** of CH1.

```c
shift = (phase_deg * 100 + 180) / 360;   /* degrees -> points, rounded */
```

100 points per period = 360°, so one point is **3.6°**: that is the
resolution. 90° = 25 points exactly; 45° = 12.5 → 13 points = **46.8°**.
(A finer phase would need a bigger table, and that would lower the top
frequency.)

**Not coupled**: `choose_dac2_trigger()` switches DAC2 to **TIM3**, which
gets CH2's own PSC / ARR from `choose_timer(1)` and is started too. Now CH2
has its own frequency, and its table is not turned (a fixed phase between
two different frequencies means nothing, so the PC shows "phase n/a"). A
DAC only accepts a new trigger while it is stopped, which is why this
happens inside `restart_all()`. It uses the same settings as CubeMX's
`MX_DAC2_Init()`, except the trigger.

**Align**: `generator_set_phase(phase, coupled)` (the `'P'` command) also
calls `restart_all()`, so sending it again restarts both channels together.
The PC's **R** key does exactly that.

## 4. `scope.c`: commands in, measurements out

### a) Receiving commands

The PC sends four kinds of messages over the same USB serial link
(ch = 0 for CH1, 1 for CH2):

| Message | Bytes | Size |
|---------|-------|------|
| rate | 1 byte, 0 … 10: the sample rate (as in SCOPE) | 1 byte |
| generator | `'G'`, ch, wave, frequency Hz (4), amplitude mV (2), offset mV (2), on (1), check | 13 bytes |
| shape | `'W'`, ch, 100 points (0 = bottom … 255 = top), check | 103 bytes |
| phase | `'P'`, CH2's phase in degrees (2), coupled (1), check | 5 bytes |

The numbers are sent "low byte first". The check byte is the sum of the
bytes after the letter, keeping only the low 8 bits. `'G'` is 71, `'P'` 80
and `'W'` 87, so none can be confused with a rate byte (0 … 10).

`HAL_UART_RxCpltCallback()` runs (in an interrupt) after each receive.
`receiving` remembers which message we are in the middle of:

```c
if (receiving == 'G') {          /* the 12 bytes after 'G' arrived */
    gen_ready = 1;
} else if (receiving == 'W') {   /* the 102 bytes after 'W' arrived */
    shape_ready = 1;
} else if (receiving == 'P') {   /* the 4 bytes after 'P' arrived */
    phase_ready = 1;
} else if (rx_byte == 'G') {     /* a command starts: get 12 more bytes */
    receiving = 'G';
    HAL_UART_Receive_IT(huart, gen_command, sizeof(gen_command));
    return;
} else if (rx_byte == 'W') {     /* a shape starts: get 102 more bytes */
    receiving = 'W';
    HAL_UART_Receive_IT(huart, shape_command, sizeof(shape_command));
    return;
} else if (rx_byte == 'P') {     /* phase + coupling: get 4 more bytes */
    receiving = 'P';
    HAL_UART_Receive_IT(huart, phase_command, sizeof(phase_command));
    return;
} else if (rx_byte < RATE_COUNT) {
    rate_index = rx_byte;        /* a rate byte, as before */
}
receiving = 0;
HAL_UART_Receive_IT(huart, &rx_byte, 1);
```

Each letter has **its own buffer**. To play a drawing, the PC sends `'W'`
and then at once `'G'` (wave = custom); with one shared buffer, the `'G'`
could overwrite the shape before it was used. Two messages with the
**same** letter right after each other (a `'G'` for CH1 and one for CH2)
would still overwrite each other, so the PC sends them a moment apart.

Interrupt code should be short, so it only sets the `..._ready` flags.
`scope_run()` calls `apply_commands()` at its start. `copy_and_check()`
copies the bytes and checks the check byte (a damaged message is ignored).
The shape is applied first, then the `'G'` (which may select it), then
the `'P'`. If a byte
is lost (an error), `HAL_UART_ErrorCallback()` forgets the half message and
listens for single bytes again.

**Adding a new message later** (for example for a logic analyzer mode):
choose a new letter, give it a buffer and a `..._ready` flag, add one
`else if` in the callback and one `if` in `apply_commands()`.

### b) Send: `send_packet()`

One packet = 54-byte header + 1000 CH1 samples + 1000 CH2 samples + checksum
(4056 bytes). Bytes 20 … 53 are new: the table points, CH2's phase and
coupling, and then (`put_channel()`) one 15-byte block per generator
channel: wave, on, asked frequency, PSC, ARR, amplitude, offset and the
custom shape's check sum. The PC uses them to show
what the board is **really** doing, not just what it asked for. The table
at the top of `send_packet()` lists every byte. The **start marker** `A5 5A`
lets the PC find the start; the **checksum** lets it throw away a damaged
packet. At 4 250 000 baud a packet takes about 9.5 ms.

### c) The scope and meter parts (unchanged from SCOPE)

- **Capture** `capture()`: TIM6 counts at 10 MHz; each overflow starts ADC1
  and ADC2 at the same moment. The results go by DMA into `ch1[]` / `ch2[]`.
  After 1000 samples, `HAL_ADC_ConvCpltCallback()` counts it as done. If a
  new rate byte arrives during a slow capture, it stops early.
- **Supply voltage** `scope_measure_vdda_mv()`: the ADC measures relative to
  VDDA (≈ 3.27 V). The chip's fixed reference VREFINT was measured at the
  factory at exactly 3.0 V, so `VDDA = 3.0 V × factory_reading /
  reading_now`. (Now also used by the generator, for the DAC's full scale.)
- **Frequency counter**: TIM2 at 170 MHz in "PWM input" mode copies the
  period (rising to rising edge on D3) and the high time into CCR2 / CCR1.
- **Continuity**: A3 has a pull-up; a wire to GND makes it read 0.
- **Sampling time**: `set_sampling_time()` picks, for each rate, the longest
  ADC sampling time that fits (see "learned the hard way").

## 5. `funcgen_window.py`: the PC side

### The generator panel

- `generator_command(ch, settings)` builds the 13 bytes:
  `struct.pack(...)` packs the numbers low byte first, then the check byte
  is added. `phase_command(phase, coupled)` builds the 5-byte `'P'`.
- `read_packets()` reads both channel blocks with `read_channel()`, so
  `packet["gen"]` is a list `[CH1, CH2]`, plus `packet["phase"]` and
  `packet["coupled"]`.
- `real_frequency(gen)`: the same formula as the board,
  `170 MHz / (PSC+1) / (ARR+1) / points`, using the PSC and ARR the board
  sent. The difference from the asked frequency is shown in %.
- `self.gen` holds the settings we **want**, one dict per channel, and
  `self.pair` CH2's phase and coupling. At start they are `None`. The first
  packet tells us what the board is doing, and we take that over
  (`check_generator()`), so the window never changes the board by surprise.
- `self.edit` is the channel (0 or 1) the generator keys change; **Y**
  switches it.
- Keys (`key()`): **G** next wave, **[ / ]** the next lower/higher value from
  `GEN_STEPS` (1-2-5 steps), **X** on/off, **F / A / O / P** start typing a
  number (`entry_key()`: digits, Backspace, Enter, Esc). Each change calls
  `change_generator()`, which sends the edited channel's `'G'`. **L** flips
  the coupling and **R** sends the same `'P'` again (align).
- **Lost command?** Every packet, `check_generator()` compares the board's
  settings with ours. If they differ for more than 1 second (a damaged
  command, or the board was reset), it sends them again, but at most
  **one** `'G'` at a time (the board has one `'G'` buffer, see above); the
  other channel follows a second later. Amplitude and offset are rounded to
  whole mV before sending, the same as the board stores them, so they
  compare equal.
- `real_phase(phase)` rounds to whole table points like the board:
  `int(phase × 100 / 360 + 0.5) × 3.6`. (Python's `round()` would turn
  12.5 into 12, the board makes it 13.)
- `draw_generator()` draws two green lines per channel under the scope,
  and "CLIPPED" when `offset ± amplitude/2` goes outside 0 … VDDA. While
  CH2 is coupled, its line shows the phase instead of its own frequency.

### Drawing a wave (D)

- **D** copies the edited channel's custom shape into `self.drawing` (100 numbers,
  0 … 255), and `draw_canvas()` shows it instead of the scope.
- `mouse()` passes every mouse position (button held) to `draw_at(x, y)`.
  The x position becomes a point number 0 … 99, the y position a value
  0 … 255 (bottom … top of the grid). The mouse only reports where it is a
  few hundred times a second, so a fast mouse skips points. `draw_at()`
  fills them in with a straight line from the last position. That is how a
  mouse path of any length becomes **exactly 100 points** (resampling).
- **Enter** (`draw_key()`): `shape_command()` builds `'W'` + 100 bytes +
  check (`shape_command(ch, ...)`, 103 bytes with the channel byte), then
  the usual `'G'` with wave = custom is sent right after.
- **Lost shape?** `check_generator()` compares the board's shape check
  (the last byte of that channel's header block) with the sum of our drawing, and sends `'W'` again if
  they differ. Rate bytes are re-sent too, if packets arrive at the wrong
  rate for over a second (a lost `'W'` could leave stray bytes behind).

### Automatic measurements and cursors (K)

- `measurements(signal, rate)` works on the 500 samples on screen:
  min, max, Vpp = max − min, avg = sum / count,
  rms = √(sum of v² / count), and `signal_frequency()` for f
  (count upward crossings of the middle, with a little hysteresis so noise
  doesn't count twice; f = rate × (crossings − 1) / samples between the
  first and last).
- The time cursors are stored as **sample positions** (0 … 500), the
  voltage cursors as **volts**. So they stay on the signal when you change
  time/div or volts/div. dt = (t2 − t1) / sample rate. Example: 250 samples
  at 50 kS/s = 5 ms, 1/dt = 200 Hz.
- `mouse()`: on a click, the cursor line nearest to the mouse (within
  10 pixels) is grabbed; while the button is held it follows the mouse.
  `y_of()` turns volts into a pixel row; the drag does the opposite.
- **Track mode**: `self.cursor_mode` is `"off"`, `"manual"` or `"track"`
  (K goes to the next one). In track mode `draw_cursors()` sets, **every
  frame**, `V1 = signal[t1]` and `V2 = signal[t2]`, where `signal` is the
  trigger channel's 500 samples on screen (`t` is rounded to a whole sample).
  So V1 / V2 are simply "the sample under the time cursor". `mouse()` then
  only lets you grab t1 / t2. Switching back to manual keeps the last
  tracked voltages, so you can fine-tune them by hand.
- **Max / min between the cursors** (`gated_peaks()`): "gated" means it
  only looks at part of the signal, the samples from the left-hand cursor
  to the right-hand one (`sorted(...)`, so t1 and t2 may be in any order).
  `max(part, key=...)` gives the **index** of the highest sample, so we know
  both its voltage and its time: `(index − first) / sample rate`. If
  t1 = t2 the part is one sample; on a cleared screen it returns `None` and
  nothing is drawn. The ▼ / ▲ markers are drawn after the box, so the box
  can't hide them.

### The scope and meter (unchanged from SCOPE)

- `find_port()` finds the ST-LINK (USB vendor id 0x0483). `read_packets()`
  finds `A5 5A`, checks length and checksum, and turns samples into volts:
  `volts = sample × VDDA / 4095`.
- The screen shows 500 of the 1000 samples across 10 divisions, so
  time/div = 50 samples / sample rate. ← / → send a new rate byte.
- `find_edge()` and `triggered_window()`: find where the trigger channel
  crosses the level and cut the 500 samples so that edge sits at 20 % of the
  screen. Edges near the end of a packet are found by gluing it to the next
  packet (only consecutive packets with the same rate).
- `new_packet()`: the trigger modes **Auto** (always draw), **Normal** (only
  draw with a trigger) and **Single** (draw once, then freeze until **S**).
  **C** puts an empty picture on screen.
- `draw_meter()` / `resistance()`: DC volts are the average of CH1;
  `R_unknown = R_known × V / (VDDA − V)`. `make_beep()` makes the continuity
  tone.

### The buttons (a second way to press the keys)

- `buttons()` returns the list of buttons for the current situation as
  (label, key name, pygame key). The labels are built every frame, so they
  can show the state ("Mode: Auto", "Output ON"). While drawing or typing,
  only the buttons that make sense there are returned.
- `draw_buttons()` draws them in two columns in the right panel and
  remembers each button's rectangle. `click_button()` checks those
  rectangles when the mouse button goes down.
- A click does **not** have its own code for each action. `press_key()`
  makes a fake key event and hands it to `key()`, the same function the
  keyboard uses. So a button and its key can never behave differently, and
  a new key only needs one extra line in `buttons()`.
- In `mouse()` the button check comes first, and only for a mouse-down, so
  a click on a button never starts a cursor drag or draws on the wave, and
  dragging a cursor over the panel never presses a button.

## Things we learned the hard way

- **The USART FIFO must be on for the multi-byte commands.** At 4 250 000 baud
  a byte arrives every 2.35 µs. Without the FIFO, the UART holds only one
  byte. After the `'G'` the interrupt had to start a new receive
  (`HAL_UART_Receive_IT`), and that took longer than 2.35 µs. The next byte
  overwrote the unread one ("overrun error"). The command was lost, and its
  leftover bytes were read as rate bytes. Rate changes always worked, because
  they are single bytes with long gaps. The fix was one CubeMX setting:
  USART2 → *Fifo Mode: Enable*. Now the UART keeps up to 8 bytes while the
  CPU catches up. We found it by sending the same bytes slowly (1 every
  5 ms, it worked) and by reading `command[]` out of the chip with
  STM32CubeProgrammer.
- **G4 DAC + DMA needs 32-bit writes.** With "Half Word" peripheral width,
  the DMA reported a transfer error on the first write and stopped, and the DAC
  output stayed flat. With "Word" in CubeMX it works.
- **The DAC doesn't quite reach 0 V.** With the output buffer on, the value
  0 gives about 0.04 V. The top reaches the supply (3.27 V).
- **The scope can't check the absolute frequency.** The generator's TIM7 and
  the scope's TIM6 run from the same internal clock. If that clock is 0.5 %
  fast, both are, and the scope still shows "1000 Hz". The measurement does
  prove that the PSC/ARR maths is right: within 300 ppm for every test.
- **The ADC can't read a pin that a timer is driving.** In SCOPE, PA6 (D12)
  was a timer output, so ADC2 read ~0 there. Now D12 is DAC2's output, but
  the scope's CH2 input is A1, so you still connect D12 → A1 with a wire.
- **VREFINT needs a long sampling time** (640.5 cycles); with a short one it
  reads wrong.
- **Resistors read ~10 % low with a short sampling time.** The ADC's tiny
  sampling capacitor has to charge through your circuit. So `scope.c` has a
  `SAMPLE_TIMES[]` table next to `RATES[]`: each rate gets the longest
  sampling time that still fits in one sample period (640.5 cycles up to
  50 kS/s, 6.5 at 2 MS/s).
