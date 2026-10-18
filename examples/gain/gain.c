/* Gain Boost - the tutorial effect (docs/effects/tutorial.md): a boost with soft clipping and a tone control.
 * knobs[0] Gain 0..24 dB, [1] Tone 0..100 (dark .. bright), [2] Level -61..+6 dB (-61 shows as "Mute")
 */
#include "matribox_module.h"

#define TWO_PI_OVER_LN2 9.0647202f       /* 2*pi / ln(2): exp(-w) = 2^(-w / ln 2) */

/* Every static variable lands in ER_IRAM (OCRAM). The loader does not clear it: Init sets each one. */
static float gain_s, level_s, coef_s;    /* smoothed gain, level and low-pass coefficient */
static float lp_l, lp_r;                 /* tone filter state, one per channel */

/* drive curve: rational approximation of tanh, linear for small signals, flat beyond |x| = 3 */
static float soft_clip(float x)
{
    x = mb_clampf(x, -3.0f, 3.0f);
    return x * (27.0f + x * x) / (27.0f + 9.0f * x * x);
}

/* one-pole low-pass coefficient for the Tone knob: cutoff 800 Hz (0) .. 12.8 kHz (100) */
static float tone_coef(float tone)
{
    float fc = 800.0f * mb_exp2f(mb_clampf(tone, 0.0f, 100.0f) * 0.04f);
    return 1.0f - mb_exp2f(-TWO_PI_OVER_LN2 * fc / MB_SAMPLE_RATE);
}

static void GainInit(const float *knobs, void *ctx)
{
    (void)ctx;
    gain_s = mb_db_to_gain(knobs[0]);
    level_s = mb_level_gain(knobs[2]);
    coef_s = tone_coef(knobs[1]);
    lp_l = lp_r = 0.0f;
}

static void GainProcess(float *io, uint16_t nframes, const float *knobs, float *aux,
                        const uint8_t *enabled, const uint32_t *beat_ms, float **extra)
{
    (void)aux; (void)beat_ms; (void)extra;
    if (!*enabled)                       /* bypass: leave the signal as it came in */
        return;

    /* targets once per block; the per-sample smoothing below removes zipper noise when a knob turns */
    const float gain_t = mb_db_to_gain(mb_clampf(knobs[0], 0.0f, 24.0f));
    const float level_t = mb_level_gain(mb_clampf(knobs[2], -61.0f, 6.0f));
    const float coef_t = tone_coef(knobs[1]);

    for (uint16_t n = 0; n < nframes; n++) {
        gain_s += 0.002f * (gain_t - gain_s);
        level_s += 0.002f * (level_t - level_s);
        coef_s += 0.002f * (coef_t - coef_s);

        float l = soft_clip(io[2 * n] * gain_s);          /* interleaved stereo: L, R, L, R ... */
        float r = soft_clip(io[2 * n + 1] * gain_s);
        lp_l += coef_s * (l - lp_l);
        lp_r += coef_s * (r - lp_r);
        io[2 * n] = lp_l * level_s;                        /* processed in place */
        io[2 * n + 1] = lp_r * level_s;
    }
}

MB_MODULE(0x01000080, GainInit, GainProcess);
