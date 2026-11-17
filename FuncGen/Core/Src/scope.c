/* scope.c - measure, then send everything to the PC. Over and over.
 *
 * One round of scope_run():
 *   1. capture SAMPLES values on A0 (ADC1) and A1 (ADC2) at the same time,
 *      paced by TIM6 (the sample rate),
 *   2. measure the chip's supply voltage with ADC5 and the built-in
 *      reference (this makes the voltages accurate),
 *   3. read the frequency counter (TIM2) and the continuity pin,
 *   4. send one packet to the PC (it includes the generator's settings).
 * The PC window does the rest (trigger, drawing, multimeter numbers).
 *
 * The PC sends us three kinds of messages:
 *   - one byte 0..10: the new sample rate (an index into RATES[] below),
 *   - 'G' + 10 bytes: new generator settings,
 *   - 'W' + 101 bytes: a new custom wave shape (drawn on the PC).
 * See HAL_UART_RxCpltCallback. Every "letter" message ends with a check
 * byte, so adding another one later is easy.
 */
#include "main.h"
#include "scope.h"
#include "generator.h"

extern ADC_HandleTypeDef hadc1, hadc2, hadc5;
extern TIM_HandleTypeDef htim2, htim6;
extern UART_HandleTypeDef huart2;

#define SAMPLES 1000             /* samples per channel in one packet */
#define TIM6_CLOCK 10000000UL    /* TIM6 counts at 170 MHz / 17 = 10 MHz */

/* Sample rates the PC can choose (samples per second, per channel). */
static const uint32_t RATES[] = {
  1000, 2500, 5000, 10000, 25000, 50000,
  100000, 250000, 500000, 1000000, 2000000
};
#define RATE_COUNT (sizeof(RATES) / sizeof(RATES[0]))

/* ADC sampling time for each rate above (in ADC clock cycles, 42.5 MHz).
 * While "sampling", a tiny capacitor inside the ADC charges up to the pin
 * voltage through the source's resistance. A source like a resistor
 * divider (about 5 kOhm) needs a LONG time, or it reads too low. So we use
 * the longest time that still fits: sampling + 12.5 conversion cycles must
 * be shorter than one sample period. E.g. 50 kS/s = 20 us per sample,
 * 640.5 + 12.5 cycles = 15.4 us: fits. 2 MS/s = 0.5 us: only 6.5 fits. */
static const uint32_t SAMPLE_TIMES[] = {
  ADC_SAMPLETIME_640CYCLES_5, ADC_SAMPLETIME_640CYCLES_5,   /* 1k, 2.5k  */
  ADC_SAMPLETIME_640CYCLES_5, ADC_SAMPLETIME_640CYCLES_5,   /* 5k, 10k   */
  ADC_SAMPLETIME_640CYCLES_5, ADC_SAMPLETIME_640CYCLES_5,   /* 25k, 50k  */
  ADC_SAMPLETIME_247CYCLES_5,                               /* 100k: 6.1 us */
  ADC_SAMPLETIME_92CYCLES_5,                                /* 250k: 2.5 us */
  ADC_SAMPLETIME_47CYCLES_5,                                /* 500k: 1.4 us */
  ADC_SAMPLETIME_24CYCLES_5,                                /* 1M: 0.87 us  */
  ADC_SAMPLETIME_6CYCLES_5                                  /* 2M: 0.45 us  */
};

static uint16_t ch1[SAMPLES];             /* filled by DMA from ADC1 */
static uint16_t ch2[SAMPLES];             /* filled by DMA from ADC2 */
static volatile int adcs_done;            /* +1 when an ADC's DMA finishes */
static volatile uint8_t rate_index = 5;   /* start at 50 000 samples/s */
static uint8_t rx_byte;                   /* last byte received from the PC */
static uint8_t gen_command[10];           /* the 10 bytes after a 'G' */
static uint8_t shape_command[GEN_POINTS + 1];   /* the 101 bytes after a 'W' */
static volatile uint8_t receiving;        /* 'G' or 'W' while receiving its bytes, else 0 */
static volatile uint8_t gen_ready;        /* 1 = gen_command[] is complete */
static volatile uint8_t shape_ready;      /* 1 = shape_command[] is complete */

static uint32_t period_ticks, high_ticks; /* frequency counter results */
static uint32_t last_edge_ms;             /* when it last saw a signal */

/* ---------- start-up ---------- */

void scope_init(void)
{
  /* Calibrate each ADC once (the chip measures its own small errors). */
  HAL_ADCEx_Calibration_Start(&hadc1, ADC_SINGLE_ENDED);
  HAL_ADCEx_Calibration_Start(&hadc2, ADC_SINGLE_ENDED);
  HAL_ADCEx_Calibration_Start(&hadc5, ADC_SINGLE_ENDED);

  /* TIM2 in "PWM input" mode (set in CubeMX): at every rising edge on D3 it
   * copies its count to CCR2 (= period) and restarts from 0; at every
   * falling edge it copies the count to CCR1 (= high time). */
  HAL_TIM_IC_Start(&htim2, TIM_CHANNEL_1);
  HAL_TIM_IC_Start(&htim2, TIM_CHANNEL_2);

  HAL_UART_Receive_IT(&huart2, &rx_byte, 1);  /* wait for a byte from the PC */
}

/* ---------- interrupt callbacks (HAL calls these) ---------- */

void HAL_ADC_ConvCpltCallback(ADC_HandleTypeDef *hadc)
{
  (void)hadc;
  adcs_done++;              /* the DMA has filled ch1 or ch2 */
}

/* Messages from the PC (numbers low byte first):
 *   0..10  one byte: the sample rate
 *   'G'  wave (1 byte)  frequency Hz (4)  amplitude mV (2)  offset mV (2)  check
 *   'W'  100 shape points (1 byte each, 0 = bottom, 255 = top)  check
 * check (1 byte) = the bytes between the letter and the check added up
 * (only the low 8 bits).
 * This runs in an interrupt, so it only stores the bytes. scope_run()
 * applies them later. Each letter has its own buffer, so a 'W' followed
 * at once by a 'G' (that is what the PC sends) does not overwrite it. */
void HAL_UART_RxCpltCallback(UART_HandleTypeDef *huart)
{
  if (receiving == 'G') {
    gen_ready = 1;          /* all bytes after 'G' have arrived */
  } else if (receiving == 'W') {
    shape_ready = 1;        /* all bytes after 'W' have arrived */
  } else if (rx_byte == 'G') {
    receiving = 'G';        /* receive the next 10 bytes into gen_command[] */
    HAL_UART_Receive_IT(huart, gen_command, sizeof(gen_command));
    return;
  } else if (rx_byte == 'W') {
    receiving = 'W';        /* receive the next 101 bytes into shape_command[] */
    HAL_UART_Receive_IT(huart, shape_command, sizeof(shape_command));
    return;
  } else if (rx_byte < RATE_COUNT) {
    rate_index = rx_byte;   /* the PC chose a new sample rate */
  }
  receiving = 0;
  HAL_UART_Receive_IT(huart, &rx_byte, 1);    /* wait for the next byte */
}

void HAL_UART_ErrorCallback(UART_HandleTypeDef *huart)
{
  receiving = 0;                              /* forget a half message */
  HAL_UART_Receive_IT(huart, &rx_byte, 1);    /* just start listening again */
}

static uint32_t get16(const uint8_t *p) { return p[0] | (p[1] << 8); }
static uint32_t get32(const uint8_t *p) { return get16(p) | (get16(p + 2) << 16); }

/* Copy a received message (so the next one may come in), and check it:
 * the last byte must be the sum of the others. 1 = good. */
static int copy_and_check(uint8_t *copy, const uint8_t *received, int length)
{
  uint8_t check = 0;
  for (int i = 0; i < length; i++) {
    copy[i] = received[i];
  }
  for (int i = 0; i < length - 1; i++) {
    check += copy[i];
  }
  return check == copy[length - 1];
}

/* If complete messages arrived, check them and use them. A damaged one is
 * ignored (the PC sends it again when it sees the board didn't change). */
static void apply_commands(void)
{
  uint8_t c[GEN_POINTS + 1];

  if (shape_ready) {        /* first the shape: the 'G' after it may select it */
    shape_ready = 0;
    if (copy_and_check(c, shape_command, sizeof(shape_command))) {
      generator_set_custom(c);
    }
  }
  if (gen_ready) {
    gen_ready = 0;
    if (copy_and_check(c, gen_command, sizeof(gen_command))) {
      generator_set(c[0], get32(c + 1), get16(c + 5), get16(c + 7));
    }
  }
}

/* ---------- 1. capture both channels ---------- */

/* Set the sampling time of A0 and A1 for this rate (table SAMPLE_TIMES). */
static void set_sampling_time(uint8_t index)
{
  static int current = -1;              /* only change it when needed */
  if (current == index) {
    return;
  }
  ADC_ChannelConfTypeDef c = {0};       /* same as CubeMX, except the time */
  c.Rank = ADC_REGULAR_RANK_1;
  c.SingleDiff = ADC_SINGLE_ENDED;
  c.OffsetNumber = ADC_OFFSET_NONE;
  c.SamplingTime = SAMPLE_TIMES[index];
  c.Channel = ADC_CHANNEL_1;            /* A0 on ADC1 */
  HAL_ADC_ConfigChannel(&hadc1, &c);
  c.Channel = ADC_CHANNEL_2;            /* A1 on ADC2 */
  HAL_ADC_ConfigChannel(&hadc2, &c);
  current = index;
}

/* Returns 1 when ch1/ch2 are full, 0 if the PC changed the rate meanwhile. */
static int capture(uint8_t index)
{
  set_sampling_time(index);             /* the ADCs are stopped here */

  /* TIM6 overflows (and starts both ADCs) every TIM6_CLOCK/rate ticks. */
  __HAL_TIM_SET_AUTORELOAD(&htim6, TIM6_CLOCK / RATES[index] - 1);
  __HAL_TIM_SET_COUNTER(&htim6, 0);

  adcs_done = 0;
  HAL_ADC_Start_DMA(&hadc1, (uint32_t *)ch1, SAMPLES);  /* armed, waiting... */
  HAL_ADC_Start_DMA(&hadc2, (uint32_t *)ch2, SAMPLES);
  HAL_TIM_Base_Start(&htim6);                           /* ...go! */

  while (adcs_done < 2 && rate_index == index) {
    /* wait: the DMA copies every result into ch1/ch2 by itself */
  }

  HAL_TIM_Base_Stop(&htim6);
  HAL_ADC_Stop_DMA(&hadc1);
  HAL_ADC_Stop_DMA(&hadc2);
  return adcs_done == 2;
}

/* ---------- 2. supply voltage from the built-in reference ---------- */

/* The chip has a fixed internal reference voltage, VREFINT (about 1.21 V).
 * At the factory, ST measured it with a 3.0 V supply and stored the
 * reading in memory at VREFINT_CAL_ADDR. If we read a smaller number now,
 * our supply is higher than 3.0 V, and so on. Result in millivolts. */
uint32_t scope_measure_vdda_mv(void)
{
  uint32_t sum = 0;
  for (int i = 0; i < 16; i++) {            /* average 16 readings */
    HAL_ADC_Start(&hadc5);
    HAL_ADC_PollForConversion(&hadc5, 10);
    sum += HAL_ADC_GetValue(&hadc5);
  }
  uint32_t vrefint = sum / 16;
  if (vrefint == 0) {
    return 3300;                            /* should never happen */
  }
  return VREFINT_CAL_VREF * (uint32_t)(*VREFINT_CAL_ADDR) / vrefint;
}

/* ---------- 3. frequency counter ---------- */

static void read_frequency_counter(void)
{
  /* The CC2 flag is set at each rising edge: a full period was measured. */
  if (__HAL_TIM_GET_FLAG(&htim2, TIM_FLAG_CC2)) {
    period_ticks = HAL_TIM_ReadCapturedValue(&htim2, TIM_CHANNEL_2) + 1;
    high_ticks = HAL_TIM_ReadCapturedValue(&htim2, TIM_CHANNEL_1) + 1;
    last_edge_ms = HAL_GetTick();
  } else if (HAL_GetTick() - last_edge_ms > 1500) {
    period_ticks = 0;                       /* no edge for 1.5 s: no signal */
    high_ticks = 0;
  }
}

/* ---------- 4. send one packet ---------- */

/* Packet (numbers are little-endian, low byte first):
 *   0  2 bytes  start marker A5 5A
 *   2  4 bytes  sample rate (samples per second, per channel)
 *   6  2 bytes  number of samples per channel (SAMPLES)
 *   8  2 bytes  supply voltage VDDA in mV (from VREFINT)
 *  10  4 bytes  frequency counter period, in 170 MHz ticks (0 = no signal)
 *  14  4 bytes  frequency counter high time, in 170 MHz ticks
 *  18  1 byte   continuity: 1 = probe A3 touches GND
 *  19  1 byte   packet counter
 *  The generator on A2 (see generator.h):
 *  20  1 byte   wave: 0 sine, 1 square, 2 triangle, 3 sawtooth, 4 custom
 *  21  1 byte   table points (GEN_POINTS)
 *  22  4 bytes  frequency asked for, Hz
 *  26  2 bytes  TIM7 prescaler PSC
 *  28  2 bytes  TIM7 period ARR  (real frequency = 170 MHz/(PSC+1)/(ARR+1)/points)
 *  30  2 bytes  amplitude, mV peak to peak
 *  32  2 bytes  offset (middle level), mV
 *  34  1 byte   custom shape check (its 100 bytes added up, low 8 bits)
 *  35  SAMPLES x 2 bytes  channel 1 (A0), 0..4095
 *      SAMPLES x 2 bytes  channel 2 (A1), 0..4095
 *  end 2 bytes  checksum: sum of all bytes after the start marker
 */
static void put16(uint8_t *p, uint32_t v) { p[0] = v; p[1] = v >> 8; }
static void put32(uint8_t *p, uint32_t v) { put16(p, v); put16(p + 2, v >> 16); }

static uint16_t sum_bytes(const void *data, int length)
{
  const uint8_t *p = data;
  uint16_t sum = 0;
  for (int i = 0; i < length; i++) {
    sum += p[i];
  }
  return sum;
}

static void send_packet(uint32_t rate, uint32_t vdda_mv, uint8_t continuity)
{
  static uint8_t counter;
  uint8_t header[35];
  uint8_t checksum[2];
  const GeneratorSettings *gen = generator_settings();

  header[0] = 0xA5;
  header[1] = 0x5A;
  put32(header + 2, rate);
  put16(header + 6, SAMPLES);
  put16(header + 8, vdda_mv);
  put32(header + 10, period_ticks);
  put32(header + 14, high_ticks);
  header[18] = continuity;
  header[19] = counter++;
  header[20] = gen->wave;
  header[21] = GEN_POINTS;
  put32(header + 22, gen->freq_hz);
  put16(header + 26, gen->prescaler);
  put16(header + 28, gen->period);
  put16(header + 30, gen->amplitude_mv);
  put16(header + 32, gen->offset_mv);
  header[34] = gen->custom_check;

  /* The STM32 stores uint16_t low byte first, so ch1/ch2 can be sent as is. */
  uint16_t sum = sum_bytes(header + 2, sizeof(header) - 2)
               + sum_bytes(ch1, sizeof(ch1)) + sum_bytes(ch2, sizeof(ch2));
  put16(checksum, sum);

  HAL_UART_Transmit(&huart2, header, sizeof(header), 100);
  HAL_UART_Transmit(&huart2, (uint8_t *)ch1, sizeof(ch1), 100);
  HAL_UART_Transmit(&huart2, (uint8_t *)ch2, sizeof(ch2), 100);
  HAL_UART_Transmit(&huart2, checksum, sizeof(checksum), 100);
}

/* ---------- the whole round ---------- */

void scope_run(void)
{
  apply_commands();         /* new generator settings or shape from the PC? */
  uint8_t index = rate_index;
  if (!capture(index)) {
    return;                 /* the PC changed the rate: start again */
  }
  uint32_t vdda_mv = scope_measure_vdda_mv();
  read_frequency_counter();
  /* The pull-up keeps A3 high. Touching GND through a wire pulls it low. */
  uint8_t continuity =
      HAL_GPIO_ReadPin(CONTINUITY_GPIO_Port, CONTINUITY_Pin) == GPIO_PIN_RESET;

  send_packet(RATES[index], vdda_mv, continuity);
  HAL_GPIO_TogglePin(LD2_GPIO_Port, LD2_Pin);   /* blinks: "I am alive" */
}
