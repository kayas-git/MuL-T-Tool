# How it works

Read the files in this order. Each one is short.

1. `Scope/Scope.ioc`: what CubeMX sets up for us
2. `Scope/Core/Src/main.c`: the 3 lines we added
3. `Scope/Core/Src/test_signals.c`: the sine and square wave
4. `Scope/Core/Src/scope.c`: measure, then send
5. `pc_app/scope_window.py`: the PC window

## 1. CubeMX did most of the setup (`Scope.ioc`)

The chip's "peripherals" are small hardware blocks that work **on their own**
once they are started. CubeMX writes all their set-up code into `main.c`
(`MX_ADC1_Init()`, `MX_TIM6_Init()`, ...). We used:

| Block | Set to | Why |
|-------|--------|-----|
| ADC1 (PA0 = A0) | 12 bit, started by TIM6 (sampling time set per rate in `scope.c`) | scope CH1 |
| ADC2 (PA1 = A1) | same | scope CH2 |
| ADC5 | reads VREFINT, slow sampling (640.5 cycles) | supply voltage |
| TIM6 | counts at 10 MHz; its overflow starts ADC1 + ADC2 | sample rate |
| DMA1 ch1/ch2 | ADC result → array `ch1[]` / `ch2[]` | no CPU needed |
| DAC1 (PA4 = A2) + TIM7 + DMA1 ch3 | 100 000 values/s from a table, forever | test sine |
| TIM3 ch1 (PA6 = D12) | PWM 2 kHz, 25 % | test square |
| TIM2 (PB3 = D3) | "PWM input" mode, 170 MHz | frequency counter |
| PB0 (A3) | input with pull-up | continuity |
| USART2 | 4 250 000 baud | to the PC via the ST-LINK USB |

The clock is 170 MHz, the fastest this chip can run.

**ADC** = Analog to Digital Converter. It turns a voltage into a number:
0 V → 0, VDDA (about 3.3 V) → 4095. **DMA** = Direct Memory Access. It is a
helper that copies data while the CPU does something else.

## 2. `main.c`

CubeMX's `main()` sets up everything, then runs our code:

```c
test_signals_start();   /* sine on A2, square on D12 */
scope_init();
while (1) {
    scope_run();          /* measure once and send it to the PC */
}
```

## 3. `test_signals.c`: things to measure

- **Sine:** at start-up we fill a table with 100 points of one sine period.
  TIM7 ticks 100 000 times a second. On every tick, the DMA copies the next
  table value into the DAC, which outputs that voltage. 100 000 / 100 =
  1 kHz. The DMA is "circular", so after the last point it starts again.
  After `HAL_DAC_Start_DMA()` the CPU never touches it again.
- **Square:** TIM3 counts 0 → 4999 and starts again, 2000 times a second. The
  pin is high while the count is below 1250, so it is high 25 % of the time.
  This is "PWM" (pulse-width modulation): pure hardware, no code at all.

## 4. `scope.c`: one round of `scope_run()`

### a) Capture: `capture()`

```c
__HAL_TIM_SET_AUTORELOAD(&htim6, TIM6_CLOCK / RATES[index] - 1);
HAL_ADC_Start_DMA(&hadc1, (uint32_t *)ch1, SAMPLES);
HAL_ADC_Start_DMA(&hadc2, (uint32_t *)ch2, SAMPLES);
HAL_TIM_Base_Start(&htim6);
```

TIM6 counts at 10 MHz. For 50 000 samples/s it overflows every 200 counts
(ARR = 199). Each overflow starts **both** ADCs at the same moment, so CH1 and
CH2 line up exactly. Each result goes by DMA into `ch1[]` / `ch2[]`. After
1000 samples, the DMA calls `HAL_ADC_ConvCpltCallback()`, which adds 1 to
`adcs_done`. When both are done, the capture is finished.

Why a timer and not a loop? A timer is exact to 1/10 000 000 s, and the
fastest rate (2 MS/s = one sample every 0.5 µs) is far too fast for C code
anyway.

### b) Supply voltage: `measure_vdda_mv()`

The ADC measures **relative** to its supply, VDDA. From USB that is "about
3.3 V" (here 3.27 V). To get real volts we must know it exactly. The chip has
a fixed reference inside, VREFINT (≈ 1.21 V). ST measured it at the factory
with exactly 3.0 V supply and stored the number at `VREFINT_CAL_ADDR`. So:

```
VDDA = 3.0 V × factory_reading / reading_now
```

The PC then converts each sample: `volts = sample × VDDA / 4095`.

### c) Frequency counter: `read_frequency_counter()`

TIM2 counts at 170 MHz. In "PWM input" mode the hardware does everything:
- at each **rising** edge on D3: copy the count into CCR2 (= the period), then
  restart from 0;
- at each **falling** edge: copy the count into CCR1 (= the time it was high).

We only read them. The PC computes `frequency = 170 000 000 / period` and
`duty = high / period`. If no edge came for 1.5 s, we send 0 ("no signal").

### d) Continuity

A3 has an internal pull-up resistor (≈ 40 kΩ to 3.3 V), so it reads 1. When a
wire connects it to GND, it reads 0 → `continuity = 1` → the PC beeps.

### e) Send: `send_packet()`

One packet = 20-byte header + 1000 CH1 samples + 1000 CH2 samples + checksum
(4022 bytes). The table at the top of `send_packet()` lists every byte. The
**start marker** `A5 5A` lets the PC find the start. The **checksum** (sum of
all bytes) lets it throw away a damaged packet.

At 4 250 000 baud, 4022 bytes take about 9.5 ms. That is why the screen
updates at most about 90 times a second.

### f) Changing the sample rate

The PC sends **one byte**: the index into `RATES[]`. `HAL_UART_Receive_IT()`
receives it in the background and calls `HAL_UART_RxCpltCallback()`, which
stores it in `rate_index`. If that happens in the middle of a slow capture,
`capture()` stops early and the next round uses the new rate.

## 5. `scope_window.py`: the PC side

- `find_port()`: finds the ST-LINK by its USB vendor id 0x0483.
- `read_packets()`: finds `A5 5A`, checks the length and checksum, and turns
  the samples into volts.
- The screen shows 500 of the 1000 samples, across 10 divisions.
  So time/div = 50 samples / sample rate. ← / → send a new rate index to the
  board.
- `find_edge()`: looks for the first sample where the trigger channel
  crosses the level (e.g. sample i−1 below 1.6 V, sample i above).
- `triggered_window()`: cuts the 500 samples to show around that edge:
  100 before it (20 % of the screen) and 400 after. If there is no edge,
  it returns `None`.

  **Edges near the end of a packet.** Say the edge is at sample 900 of a
  1000-sample packet. There are only 100 samples after it, not 400, so this
  packet can't show it. It's not lost: when the next packet arrives,
  `triggered_window()` glues the old and new packet together
  (`old + new`, 2000 samples) and looks again near the joint. The board
  starts the next capture right after sending one, so only about 10 ms are
  missing at the joint. The two searches cover different samples, so every
  edge is checked exactly once:
  ```
  packet:      |0 ...... 100 ............. 600 ...... 999|
  step 1 (this packet alone): edges at 100..600
  step 2 (old + new glued):   old 601..999 and new 0..99
  ```
  We only glue packets with the same rate and consecutive packet counters
  (the board numbers them), so we never glue two unrelated captures.
- `new_packet()`: the trigger modes.
  - **Auto**: with a trigger, show the window around it. Without one,
    show the first 500 samples anyway.
  - **Normal**: only a triggered window replaces `self.view`. Without a
    trigger, the old picture stays.
  - **Single**: like Normal, but after the first trigger
    `single_armed = False`, and then new packets are ignored ("STOPPED").
    S sets `single_armed = True` again.
  - **C** (clear) puts an empty picture (`"ch": [[], []]`) in `self.view`.
    Nothing special is needed after that: each mode simply replaces it the
    next time it would draw something new.

  The min/max/avg/freq numbers are computed from the 500 samples on screen,
  so in Normal/Single they belong to the frozen picture.
- `draw_meter()`: the average of CH1 is the DC voltage. Frequency and duty
  come from the board's TIM2 numbers.
- `resistance()`: the ohmmeter. It is only maths on that same A0 average,
  so the board needed no change. With the divider 3V3 → R_known → A0 →
  R_unknown → GND, the current is the same in both resistors, so
  `R_unknown = R_known × V / (VDDA − V)`. It uses the measured VDDA (from
  VREFINT), not "3.3 V". Near VDDA it says "open", near 0 V "short", and
  outside R_known ÷ 10 … × 10 it adds "less accurate". R_known comes from
  `--rref` (default 10 000).
- `make_beep()`: builds a 0.1 s, 1 kHz tone from a sine and loops it while
  continuity = 1.

## Things we learned the hard way

- **G4 DAC + DMA needs 32-bit writes.** With "Half Word" peripheral width,
  the DMA reported a transfer error on the first write and stopped, and the DAC
  output stayed flat. With "Word" in CubeMX it works.
- **The ADC can't read a pin that a timer is driving.** PA6 (D12) is a timer
  output, so ADC2 reads ~0 there. That's why you connect D12 → A1 with a wire.
- **VREFINT needs a long sampling time** (640.5 cycles); with a short one it
  reads wrong.
- **Resistors read ~10 % low with a short sampling time.** The ADC holds a
  tiny capacitor that has to charge through your circuit. A 10k + 10k divider
  looks like a 5 kΩ source, and with 6.5 cycles the capacitor never got
  full, so 9.9 kΩ read as 9 kΩ. Now `scope.c` has a `SAMPLE_TIMES[]` table
  next to `RATES[]`: each rate gets the longest sampling time that still
  fits in one sample period (sampling + 12.5 conversion cycles at 42.5 MHz).
  That is 640.5 cycles up to 50 kS/s and 6.5 at 2 MS/s.
  `set_sampling_time()` applies it with `HAL_ADC_ConfigChannel()` whenever
  the rate changes.
