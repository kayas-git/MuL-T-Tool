/* generator.h - a two-channel function generator.
 *
 *   channel 1 (GEN_CH1): A2  (PA4, DAC1 output 1), timed by TIM7
 *   channel 2 (GEN_CH2): D12 (PA6, DAC2 output 1), timed by TIM3,
 *                        or by TIM7 too when "coupled" to channel 1
 *
 * Each channel makes a sine, square, triangle, sawtooth or "custom" wave
 * (drawn by the user on the PC), 0 to 3.3 V only. The PC chooses the
 * wave, the frequency (1 Hz to 10 kHz), the amplitude (peak to peak), the
 * offset (the middle level) and on/off for each channel, plus the phase of
 * channel 2. See generator.c.
 */
#ifndef GENERATOR_H
#define GENERATOR_H

#include <stdint.h>

/* The 5 wave shapes. The numbers are what the PC sends. */
enum { WAVE_SINE = 0, WAVE_SQUARE = 1, WAVE_TRIANGLE = 2, WAVE_SAWTOOTH = 3,
       WAVE_CUSTOM = 4 };

#define GEN_CHANNELS 2        /* channel numbers are 0 (CH1) and 1 (CH2) */
#define GEN_POINTS   100      /* values in the table = points per period */
#define GEN_MIN_HZ   1
#define GEN_MAX_HZ   10000    /* 10 kHz x 100 points = 1 000 000 values/s */
#define GEN_MAX_MV   3300     /* amplitude and offset: 0 .. 3300 mV */

/* The current settings of one channel (the PC shows them). */
typedef struct {
  uint8_t  wave;           /* WAVE_SINE ... WAVE_CUSTOM */
  uint8_t  on;             /* 1 = output on, 0 = off (the pin stays at 0 V) */
  uint32_t freq_hz;        /* the frequency the PC asked for */
  uint16_t amplitude_mv;   /* peak to peak, in millivolts */
  uint16_t offset_mv;      /* middle level, in millivolts */
  uint16_t prescaler;      /* timer PSC register: it divides by PSC + 1 */
  uint16_t period;         /* timer ARR register: it divides by ARR + 1 */
  uint8_t  custom_check;   /* the custom shape's 100 bytes added up (low 8
                              bits): lets the PC see which shape we have */
} GeneratorSettings;

/* Start both channels (CH1: 1 kHz sine, CH2: off). vdda_mv = the real
 * supply voltage (the DAC's full scale), so the millivolts come out right. */
void generator_start(uint32_t vdda_mv);

/* Change one channel (ch = 0 or 1). Values out of range are limited to
 * the allowed range. */
void generator_set(uint8_t ch, uint8_t wave, uint32_t freq_hz,
                   uint32_t amplitude_mv, uint32_t offset_mv, uint8_t on);

/* A new custom shape for one channel: GEN_POINTS values, 0 = bottom ..
 * 255 = top. It is scaled by the amplitude and offset like the other
 * waves. If the custom wave is playing, the output changes at once. */
void generator_set_custom(uint8_t ch, const uint8_t *points);

/* Channel 2's phase (0..359 degrees, ahead of channel 1) and coupling.
 * coupled = 1: channel 2 plays at channel 1's frequency, shifted by the
 * phase. coupled = 0: channel 2 has its own frequency (no fixed phase).
 * Both channels restart together, so this also "aligns" them. */
void generator_set_phase(uint32_t phase_deg, uint8_t coupled);

const GeneratorSettings *generator_settings(uint8_t ch);
uint16_t generator_phase(void);      /* degrees */
uint8_t generator_coupled(void);     /* 1 = coupled */

#endif
