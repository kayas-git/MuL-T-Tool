/* generator.c - the function generator: a table of values -> DMA -> DAC.
 *
 * How a wave is made (the CPU does nothing while it plays):
 *
 *   table[0][100]  --DMA-->  DAC1  -->  pin A2  (PA4)   channel 1
 *   table[1][100]  --DMA-->  DAC2  -->  pin D12 (PA6)   channel 2
 *                   ^
 *                   a timer: every time it overflows, the DMA copies the
 *                   NEXT table value into the DAC, which outputs it.
 *                   TIM7 paces channel 1; TIM3 paces channel 2.
 *
 * A table holds ONE period of the wave (GEN_POINTS = 100 values). The
 * shape comes from a formula (sine, square, triangle, sawtooth) or, for the
 * "custom" wave, from 100 numbers the user drew on the PC. The DMA
 * is "circular": after the last value it starts again at the first one. So
 *
 *   frequency = 170 MHz / (PSC + 1) / (ARR + 1) / 100
 *
 *   170 MHz  = the timer's clock (TIM7 and TIM3 both get 170 MHz)
 *   PSC + 1  = the timer's prescaler: it divides the clock first
 *   ARR + 1  = the timer counts 0..ARR, then overflows: it divides again
 *   100      = table values per period
 *
 * Both PSC and ARR are whole numbers (at most 65535), so most frequencies
 * can only be made approximately. choose_timer() picks the closest one,
 * and the PC shows the real result. Example, 440 Hz:
 *   170 000 000 / 100 / 440 = 3863.6 timer ticks per value -> round to 3864
 *   (PSC = 0, ARR = 3863) -> 170 000 000 / 1 / 3864 / 100 = 439.96 Hz.
 *
 * Phase and "coupling" (channel 2 only):
 *   Coupled (the default): DAC2 listens to TIM7, the SAME timer as DAC1.
 *   Both DACs step through their tables together, value by value, so they
 *   always have exactly the same frequency. To make channel 2 start later
 *   in its period (a phase shift), we simply ROTATE its table: with 90
 *   degrees, its table starts at point 25 of the wave instead of point 0
 *   (90 / 360 x 100 = 25). One point is 360 / 100 = 3.6 degrees, so the
 *   phase is rounded to the nearest 3.6 degrees.
 *   Not coupled: DAC2 listens to TIM3, which has its own PSC/ARR, so
 *   channel 2 has its own frequency. The two waves then drift past each
 *   other, and a phase means nothing.
 *
 * Why at most 10 kHz? Then a DAC gets 10 000 x 100 = 1 000 000 new values
 * a second, the most this DAC (with its output buffer) is made for.
 * (CubeMX note: on the G4 the DAC must be written 32 bits at a time, so the
 * DMA's "peripheral data width" is Word. With Half Word it stops at once.)
 */
#include <math.h>

#include "main.h"
#include "generator.h"

extern DAC_HandleTypeDef hdac1, hdac2;
extern TIM_HandleTypeDef htim7, htim3;

#define TIMER_CLOCK 170000000UL   /* TIM7 and TIM3 count at 170 MHz */

static uint16_t table[GEN_CHANNELS][GEN_POINTS];  /* DAC values 0..4095 */
static uint8_t custom[GEN_CHANNELS][GEN_POINTS];  /* drawn shapes, 0..255 */
static GeneratorSettings settings[GEN_CHANNELS];
static uint16_t phase_deg;               /* channel 2 ahead of channel 1 */
static uint8_t coupled = 1;              /* 1 = channel 2 uses TIM7 too */
static uint32_t full_scale_mv = 3300;    /* a DAC outputs this at 4095 */

/* Each channel's DAC and timer, so the code can say dac[ch], timer[ch]. */
static DAC_HandleTypeDef *const dac[GEN_CHANNELS] = { &hdac1, &hdac2 };
static TIM_HandleTypeDef *const timer[GEN_CHANNELS] = { &htim7, &htim3 };

/* The wave's shape at point i of the table, from -1 (bottom) to +1 (top). */
static float shape(uint8_t ch, int i)
{
  float x = (float)i / GEN_POINTS;      /* 0 .. 0.99: where in the period */
  switch (settings[ch].wave) {
    case WAVE_SQUARE:                   /* top for half, bottom for half */
      return (x < 0.5f) ? 1.0f : -1.0f;
    case WAVE_TRIANGLE:                 /* up for half, down for half */
      return (x < 0.5f) ? -1.0f + 4.0f * x : 3.0f - 4.0f * x;
    case WAVE_SAWTOOTH:                 /* slowly up, then drop at once */
      return -1.0f + 2.0f * (float)i / (GEN_POINTS - 1);
    case WAVE_CUSTOM:                   /* drawn on the PC: 0..255 -> -1..+1 */
      return custom[ch][i] / 127.5f - 1.0f;
    default:                            /* WAVE_SINE */
      return sinf(2.0f * 3.14159265f * x);
  }
}

/* How many table points channel 2 is shifted: phase x 100 / 360, rounded.
 * Only when coupled (otherwise the phase has no meaning). */
static int phase_points(void)
{
  if (!coupled) {
    return 0;
  }
  return ((phase_deg * GEN_POINTS + 180) / 360) % GEN_POINTS;
}

/* Fill one channel's table: volts = offset + (amplitude / 2) x shape, then
 * turn the volts into DAC values (0 V -> 0, full_scale_mv -> 4095).
 * "shift" rotates the table: table[i] gets the wave's point i + shift. */
static void fill_table(uint8_t ch, int shift)
{
  const GeneratorSettings *s = &settings[ch];
  for (int i = 0; i < GEN_POINTS; i++) {
    float mv = s->offset_mv
             + s->amplitude_mv / 2.0f * shape(ch, (i + shift) % GEN_POINTS);
    float value = mv * 4095.0f / full_scale_mv + 0.5f;   /* +0.5: round */
    if (value < 0.0f) {
      value = 0.0f;                     /* can't go below 0 V: "clipped" */
    }
    if (value > 4095.0f) {
      value = 4095.0f;                  /* or above the supply */
    }
    table[ch][i] = s->on ? (uint16_t)value : 0;   /* off: stay at 0 V */
  }
}

/* Choose PSC and ARR so that 170 MHz / (PSC+1) / (ARR+1) / 100 is as close
 * as possible to the channel's freq_hz. */
static void choose_timer(uint8_t ch)
{
  uint32_t freq_hz = settings[ch].freq_hz;
  /* Timer ticks for each table value, rounded: (PSC+1) x (ARR+1). */
  uint32_t ticks = (TIMER_CLOCK / GEN_POINTS + freq_hz / 2) / freq_hz;

  /* ARR + 1 can be at most 65536. Use the smallest prescaler that fits,
   * so ARR stays big and the rounding stays small. */
  uint32_t divide1 = ticks / 65536 + 1;               /* PSC + 1 */
  uint32_t divide2 = (ticks + divide1 / 2) / divide1; /* ARR + 1, rounded */

  settings[ch].prescaler = divide1 - 1;
  settings[ch].period = divide2 - 1;
}

/* Tell DAC2 which timer to listen to: TIM7 (coupled) or TIM3 (its own).
 * The same settings as MX_DAC2_Init() in main.c, except the trigger.
 * (The DAC only accepts a new trigger while it is stopped.) */
static void choose_dac2_trigger(void)
{
  DAC_ChannelConfTypeDef c = {0};
  c.DAC_HighFrequency = DAC_HIGH_FREQUENCY_INTERFACE_MODE_AUTOMATIC;
  c.DAC_DMADoubleDataMode = DISABLE;
  c.DAC_SignedFormat = DISABLE;
  c.DAC_SampleAndHold = DAC_SAMPLEANDHOLD_DISABLE;
  c.DAC_Trigger = coupled ? DAC_TRIGGER_T7_TRGO : DAC_TRIGGER_T3_TRGO;
  c.DAC_Trigger2 = DAC_TRIGGER_NONE;
  c.DAC_OutputBuffer = DAC_OUTPUTBUFFER_ENABLE;
  c.DAC_ConnectOnChipPeripheral = DAC_CHIPCONNECT_EXTERNAL;
  c.DAC_UserTrimming = DAC_TRIMMING_FACTORY;
  HAL_DAC_ConfigChannel(&hdac2, &c, DAC_CHANNEL_1);
}

/* Stop everything, make both tables, then start both channels at the same
 * moment. Every change goes through here, so the channels always stay in
 * step (channel 1 also restarts when only channel 2 changes: a tiny
 * hiccup, but it keeps the phase right). */
static void restart_all(void)
{
  /* 1. Stop the timers and the DMAs, so we can safely change the tables. */
  for (int ch = 0; ch < GEN_CHANNELS; ch++) {
    HAL_TIM_Base_Stop(timer[ch]);
    HAL_DAC_Stop_DMA(dac[ch], DAC_CHANNEL_1);
  }

  /* 2. New tables (channel 2 rotated by the phase) and timer speeds. */
  fill_table(0, 0);
  fill_table(1, phase_points());
  for (int ch = 0; ch < GEN_CHANNELS; ch++) {
    choose_timer(ch);
    /* HAL_TIM_Base_Init() writes PSC and ARR into the timer (it keeps the
     * other settings CubeMX made) and resets its count to 0. */
    timer[ch]->Init.Prescaler = settings[ch].prescaler;
    timer[ch]->Init.Period = settings[ch].period;
    HAL_TIM_Base_Init(timer[ch]);
  }
  if (coupled) {            /* channel 2 really runs on TIM7's numbers */
    settings[1].prescaler = settings[0].prescaler;
    settings[1].period = settings[0].period;
  }
  choose_dac2_trigger();

  /* 3. Go: each DMA copies its table to its DAC, one value per timer tick.
   *    The timers start last, one right after the other. */
  for (int ch = 0; ch < GEN_CHANNELS; ch++) {
    HAL_DAC_Start_DMA(dac[ch], DAC_CHANNEL_1, (uint32_t *)table[ch],
                      GEN_POINTS, DAC_ALIGN_12B_R);
  }
  HAL_TIM_Base_Start(&htim7);
  if (!coupled) {
    HAL_TIM_Base_Start(&htim3);         /* coupled: TIM3 is not needed */
  }
}

static uint32_t limit(uint32_t value, uint32_t low, uint32_t high)
{
  if (value < low) {
    return low;
  }
  return value > high ? high : value;
}

void generator_set(uint8_t ch, uint8_t wave, uint32_t freq_hz,
                   uint32_t amplitude_mv, uint32_t offset_mv, uint8_t on)
{
  if (ch >= GEN_CHANNELS) {
    return;
  }
  GeneratorSettings *s = &settings[ch];
  s->wave = wave <= WAVE_CUSTOM ? wave : WAVE_SINE;
  s->freq_hz = limit(freq_hz, GEN_MIN_HZ, GEN_MAX_HZ);
  s->amplitude_mv = limit(amplitude_mv, 0, GEN_MAX_MV);
  s->offset_mv = limit(offset_mv, 0, GEN_MAX_MV);
  s->on = on ? 1 : 0;
  restart_all();
}

void generator_set_custom(uint8_t ch, const uint8_t *points)
{
  if (ch >= GEN_CHANNELS) {
    return;
  }
  uint8_t check = 0;
  for (int i = 0; i < GEN_POINTS; i++) {
    custom[ch][i] = points[i];
    check += points[i];
  }
  settings[ch].custom_check = check;
  if (settings[ch].wave == WAVE_CUSTOM) {   /* playing it now: new table */
    restart_all();
  }
}

void generator_set_phase(uint32_t phase, uint8_t couple)
{
  phase_deg = phase % 360;
  coupled = couple ? 1 : 0;
  restart_all();            /* also "aligns" both channels */
}

void generator_start(uint32_t vdda_mv)
{
  full_scale_mv = vdda_mv;
  /* Until the user draws one, the custom wave is 4 steps, like stairs:
   * 0, 85, 170, 255 (25 points each). */
  uint8_t stairs[GEN_POINTS];
  for (int i = 0; i < GEN_POINTS; i++) {
    stairs[i] = (i / 25) * 85;
  }
  for (int ch = 0; ch < GEN_CHANNELS; ch++) {
    generator_set_custom(ch, stairs);
  }
  /* Both: 1 kHz sine, 2 V peak to peak, around 1.65 V (0.65 V to 2.65 V).
   * Channel 1 on, channel 2 off until the user switches it on. */
  generator_set(1, WAVE_SINE, 1000, 2000, 1650, 0);
  generator_set(0, WAVE_SINE, 1000, 2000, 1650, 1);
}

const GeneratorSettings *generator_settings(uint8_t ch)
{
  return &settings[ch < GEN_CHANNELS ? ch : 0];
}

uint16_t generator_phase(void)
{
  return phase_deg;
}

uint8_t generator_coupled(void)
{
  return coupled;
}
