/* 44.1 kHz <-> 48 kHz resampler so 48 kHz NAM captures run at their own rate.
 *
 * Two rational stages each way through 73.5 kHz, with small Kaiser-windowed sinc prototypes that stay in
 * fast memory (a single 160/147 stage needed 53 KB of polyphase tables in SDRAM and cost 225 us per
 * block on the pedal, bandwidth bound):
 *   up:   44.1k x5/3 (P1u: 120 taps @ 220.5 kHz, pass 20k / stop 28k)
 *         73.5k x32/49 (P2: 1280 taps @ 2.352 MHz, pass 20k / stop 28k, 70 dB)
 *   down: 48k x49/32 (P2), 73.5k x3/5 (P1d: 240 taps @ 220.5 kHz, pass 20k / stop 24.1k, 71 dB)
 * Whatever is left between 24 and 28 kHz can only land above 20 kHz. Latency about 1.36 ms.
 * Per 64-frame block the up path makes 69 or 70 samples; the down path always returns 64.
 */
#ifndef RS48_H
#define RS48_H

#define RS_MAX48 70                             /* 48k samples per 64-frame block, at most */

void rs48_design(void);                         /* builds the prototypes (once per Init) */
void rs48_reset(void);                          /* clears the histories */
int rs48_up(const float *in44, float *out48);   /* 64 in -> returns count (<= RS_MAX48) */
void rs48_down(const float *in48, int c, float *out44);   /* c in -> 64 out */

#endif
