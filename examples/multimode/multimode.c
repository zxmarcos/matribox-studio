/* Multi Mode - example of a text-list knob (app patch "textlist"): one module, five modulation modes.
 * knobs[0] Mode 0..4 (Tremolo | Vibrato | Ring Mod | Bitcrush | Octave Fuzz), [1] Rate 0.1..10 Hz,
 *       [2] Depth 0..100, [3] Level -12..+12 dB
 * Without the app patch the pedal shows the Mode knob as the number 0..4.
 */
#include "matribox_module.h"

enum { TREMOLO, VIBRATO, RING_MOD, BITCRUSH, OCTAVE_FUZZ, MODES };

#define VIB_LEN    1024                 /* vibrato delay line (frames, power of two) */
#define VIB_CENTER 220.0f               /* ~5 ms */
#define TWO_PI     6.2831853f

static float vib[VIB_LEN * 2];          /* ER_IRAM state */
static uint32_t wpos;
static float phase, ring_phase, depth_s, gain_s, hold_l, hold_r, hold_n, lp_l, lp_r;
static int mode_s;

/* sine of ph in [0,1): parabolic approximation with one correction step (error < 0.1%) */
static float lfo_sin(float ph)
{
    float x = ph < 0.5f ? ph : ph - 1.0f;           /* -0.5..0.5 */
    float y = 8.0f * x - 16.0f * x * (x < 0.0f ? -x : x);
    return 0.225f * (y * (y < 0.0f ? -y : y) - y) + y;
}

static float fabsf_(float x) { return x < 0.0f ? -x : x; }

static int mode_of(const float *k)
{
    int m = (int)(k[0] + 0.5f);
    return m < 0 ? 0 : (m >= MODES ? MODES - 1 : m);
}

static void MultiModeInit(const float *knobs, void *ctx)
{
    (void)ctx;
    for (uint32_t i = 0; i < VIB_LEN * 2; i++) vib[i] = 0.0f;
    wpos = 0;
    phase = ring_phase = 0.0f;
    hold_l = hold_r = hold_n = lp_l = lp_r = 0.0f;
    depth_s = mb_clampf(knobs[2], 0.0f, 100.0f) * 0.01f;
    gain_s = mb_db_to_gain(mb_clampf(knobs[3], -12.0f, 12.0f));
    mode_s = mode_of(knobs);
}

static void MultiModeProcess(float *io, uint16_t nframes, const float *knobs, float *aux,
                             const uint8_t *enabled, const uint32_t *beat_ms, float **extra)
{
    (void)aux; (void)beat_ms; (void)extra;
    if (!*enabled)
        return;
    const int mode = mode_of(knobs);
    if (mode != mode_s) {                           /* new mode: start from a clean state */
        mode_s = mode;
        hold_n = lp_l = lp_r = 0.0f;
    }
    const float rate = mb_clampf(knobs[1], 0.1f, 10.0f) / MB_SAMPLE_RATE;
    const float depth_t = mb_clampf(knobs[2], 0.0f, 100.0f) * 0.01f;
    const float gain_t = mb_db_to_gain(mb_clampf(knobs[3], -12.0f, 12.0f));
    /* ring modulator carrier: 40 Hz .. 1 kHz from Rate */
    const float carrier = (40.0f + 96.0f * mb_clampf(knobs[1], 0.1f, 10.0f)) / MB_SAMPLE_RATE;

    for (uint16_t n = 0; n < nframes; n++) {
        depth_s += 0.002f * (depth_t - depth_s);
        gain_s += 0.002f * (gain_t - gain_s);
        phase += rate;
        if (phase >= 1.0f) phase -= 1.0f;
        float l = io[2 * n], r = io[2 * n + 1];
        float s = lfo_sin(phase);

        switch (mode) {
        case TREMOLO: {
            float g = 1.0f - depth_s * 0.5f * (1.0f + s);
            l *= g; r *= g;
            break;
        }
        case VIBRATO: {
            vib[2 * wpos] = l;
            vib[2 * wpos + 1] = r;
            float rp = (float)wpos - VIB_CENTER * (1.0f + 0.9f * depth_s * s);
            if (rp < 0.0f) rp += (float)VIB_LEN;
            uint32_t i0 = (uint32_t)rp & (VIB_LEN - 1), i1 = (i0 + 1) & (VIB_LEN - 1);
            float f = rp - (float)(uint32_t)rp;
            l = vib[2 * i0] + f * (vib[2 * i1] - vib[2 * i0]);
            r = vib[2 * i0 + 1] + f * (vib[2 * i1 + 1] - vib[2 * i0 + 1]);
            wpos = (wpos + 1) & (VIB_LEN - 1);
            break;
        }
        case RING_MOD: {
            ring_phase += carrier;
            if (ring_phase >= 1.0f) ring_phase -= 1.0f;
            float c = lfo_sin(ring_phase);
            float m = 1.0f - depth_s + depth_s * c;
            l *= m; r *= m;
            break;
        }
        case BITCRUSH: {
            /* sample-rate reduction (Rate) and quantisation (Depth) */
            hold_n += 1.0f;
            if (hold_n >= 1.0f + 0.05f * MB_SAMPLE_RATE * rate * 40.0f) {
                hold_n = 0.0f;
                float q = 2.0f + (1.0f - depth_s) * 254.0f;
                hold_l = (float)(int)(l * q) / q;
                hold_r = (float)(int)(r * q) / q;
            }
            l = hold_l; r = hold_r;
            break;
        }
        default: {                                  /* OCTAVE_FUZZ: full-wave rectifier + soft clip */
            float drive = 1.0f + 30.0f * depth_s;
            float ol = fabsf_(l) * drive, or_ = fabsf_(r) * drive;
            ol = ol / (1.0f + ol);
            or_ = or_ / (1.0f + or_);
            lp_l += 0.01f * (ol - lp_l);            /* remove the rectifier's DC */
            lp_r += 0.01f * (or_ - lp_r);
            l = 0.5f * (ol - lp_l);
            r = 0.5f * (or_ - lp_r);
            break;
        }
        }
        io[2 * n] = l * gain_s;
        io[2 * n + 1] = r * gain_s;
    }
}

MB_MODULE(0x04000070, MultiModeInit, MultiModeProcess);
