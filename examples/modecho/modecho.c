/* Mod Echo - example third-party module: stereo delay with LFO-modulated read head.
 * knobs[0] Mix 0..100, [1] Time 20..4000 ms (or note division 0..10 when synced), [2] Feedback 0..100,
 *       [3] Depth 0..100, [4] Rate 0.1..10 Hz, [5] Sync 0/1
 */
#include "matribox_module.h"
#include "sine_tab.h"            /* const -> ER_SROM */

#define MAX_FRAMES   176400      /* 4 s @ 44.1 kHz, same as the factory delays */
#define MOD_SAMPLES  176.0f      /* max +/- 4 ms of modulation */

MB_SRAM static float dline[MAX_FRAMES * 2];   /* ER_SRAM, 1,411,200 bytes */

static uint32_t wpos;                         /* ER_IRAM state */
static float phase, time_s, mix_s, fb_s, depth_s, lp_l, lp_r;
static uint8_t primed;                        /* first Process after Init snaps to the real tempo */

static float lfo_sin(float ph)                /* ph in [0,1) */
{
    float x = ph * 256.0f;
    int i = (int)x;
    float f = x - (float)i;
    return sine_tab[i] + f * (sine_tab[i + 1] - sine_tab[i]);
}

static float target_delay(const float *k, const uint32_t *beat_ms)
{
    float ms = k[5] >= 0.5f ? mb_sync_ms(k[1], beat_ms) : k[1];
    return mb_clampf(ms, 20.0f, 3990.0f) * (MB_SAMPLE_RATE / 1000.0f);
}

static void ModEchoInit(const float *knobs, void *ctx)
{
    static const uint32_t init_beat = 500;   /* tempo is unknown in Init; first Process corrects it */
    (void)ctx;
    for (uint32_t i = 0; i < MAX_FRAMES * 2; i++) dline[i] = 0.0f;
    wpos = 0;
    primed = 0;
    phase = 0.0f;
    lp_l = lp_r = 0.0f;
    time_s = target_delay(knobs, &init_beat);
    mix_s = knobs[0] * 0.01f;
    fb_s = knobs[2] * 0.0095f;
    depth_s = knobs[3] * 0.01f;
}

static void ModEchoProcess(float *io, uint16_t nframes, const float *knobs, float *aux,
                           const uint8_t *enabled, const uint32_t *beat_ms, float **extra)
{
    (void)aux; (void)extra;
    const float mix_t = mb_clampf(knobs[0], 0.0f, 100.0f) * 0.01f;
    const float fb_t = mb_clampf(knobs[2], 0.0f, 100.0f) * 0.0095f;
    const float depth_t = mb_clampf(knobs[3], 0.0f, 100.0f) * 0.01f;
    const float rate = mb_clampf(knobs[4], 0.1f, 10.0f) / MB_SAMPLE_RATE;
    const float time_t = target_delay(knobs, beat_ms);
    const int on = *enabled != 0;
    if (!primed) { time_s = time_t; primed = 1; }

    for (uint16_t n = 0; n < nframes; n++) {
        /* one-pole parameter smoothing (no zipper noise) */
        time_s += 0.0005f * (time_t - time_s);
        mix_s += 0.002f * (mix_t - mix_s);
        fb_s += 0.002f * (fb_t - fb_s);
        depth_s += 0.002f * (depth_t - depth_s);
        phase += rate;
        if (phase >= 1.0f) phase -= 1.0f;

        float d = time_s + MOD_SAMPLES * depth_s * (1.0f + lfo_sin(phase)) + 1.0f;
        float rp = (float)wpos - d;
        if (rp < 0.0f) rp += (float)MAX_FRAMES;
        uint32_t i0 = (uint32_t)rp;
        float f = rp - (float)i0;
        uint32_t i1 = i0 + 1 == MAX_FRAMES ? 0 : i0 + 1;
        float wl = dline[2 * i0] + f * (dline[2 * i1] - dline[2 * i0]);
        float wr = dline[2 * i0 + 1] + f * (dline[2 * i1 + 1] - dline[2 * i0 + 1]);

        /* darker repeats: one-pole low-pass in the feedback path */
        lp_l += 0.35f * (wl - lp_l);
        lp_r += 0.35f * (wr - lp_r);

        float xl = io[2 * n], xr = io[2 * n + 1];
        float inl = on ? xl : 0.0f, inr = on ? xr : 0.0f;   /* bypass keeps the trails */
        dline[2 * wpos] = inl + fb_s * lp_l;
        dline[2 * wpos + 1] = inr + fb_s * lp_r;
        if (++wpos == MAX_FRAMES) wpos = 0;

        io[2 * n] = xl + mix_s * wl;
        io[2 * n + 1] = xr + mix_s * wr;
    }
}

MB_MODULE(0x0B000070, ModEchoInit, ModEchoProcess);

