/* generator.h - the function generator on A2 (PA4, the DAC output).
 *
 * It makes a sine, square, triangle, sawtooth or "custom" wave (drawn by
 * the user on the PC), 0 to 3.3 V only. The PC chooses the wave, the
 * frequency (1 Hz to 10 kHz), the amplitude (peak to peak) and the offset
 * (the middle level). See generator.c.
 */
#ifndef GENERATOR_H
#define GENERATOR_H

#include <stdint.h>

/* The 5 wave shapes. The numbers are what the PC sends. */
enum { WAVE_SINE = 0, WAVE_SQUARE = 1, WAVE_TRIANGLE = 2, WAVE_SAWTOOTH = 3,
       WAVE_CUSTOM = 4 };

#define GEN_POINTS   100      /* values in the table = points per period */
#define GEN_MIN_HZ   1
#define GEN_MAX_HZ   10000    /* 10 kHz x 100 points = 1 000 000 values/s */
#define GEN_MAX_MV   3300     /* amplitude and offset: 0 .. 3300 mV */

/* The current settings (the PC shows them). */
typedef struct {
  uint8_t  wave;           /* WAVE_SINE ... WAVE_CUSTOM */
  uint32_t freq_hz;        /* the frequency the PC asked for */
  uint16_t amplitude_mv;   /* peak to peak, in millivolts */
  uint16_t offset_mv;      /* middle level, in millivolts */
  uint16_t prescaler;      /* TIM7 PSC register: it divides by PSC + 1 */
  uint16_t period;         /* TIM7 ARR register: it divides by ARR + 1 */
  uint8_t  custom_check;   /* the custom shape's 100 bytes added up (low 8
                              bits): lets the PC see which shape we have */
} GeneratorSettings;

/* Start with a 1 kHz sine. vdda_mv = the real supply voltage (the DAC's
 * full scale), so that the millivolts come out right. */
void generator_start(uint32_t vdda_mv);

/* Change the wave. Values out of range are limited to the allowed range. */
void generator_set(uint8_t wave, uint32_t freq_hz,
                   uint32_t amplitude_mv, uint32_t offset_mv);

/* A new custom shape: GEN_POINTS values, 0 = bottom .. 255 = top. It is
 * scaled by the amplitude and offset like the other waves. If the custom
 * wave is playing, the output changes at once. */
void generator_set_custom(const uint8_t *points);

const GeneratorSettings *generator_settings(void);

#endif
