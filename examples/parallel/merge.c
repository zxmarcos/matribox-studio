/* Merge: end of the parallel section, mixes path A and path B (see route.h).
   Params: 0 Level A, 1 Level B (dB, below -60 = Mute), 2 Pan A, 3 Pan B (-50 L .. 50 R, balance law),
   4 Phase B (0 Normal, 1 Invert). Bypassed = A + B at unity. */
#include "route.h"

static struct { uint32_t last_seq; float g[4]; int primed; } st;

static void balance(float level, float pan, float *gl, float *gr)
{
    float p = mb_clampf(pan * (1.0f / 50.0f), -1.0f, 1.0f);
    *gl = level * (p > 0.0f ? 1.0f - p : 1.0f);
    *gr = level * (p < 0.0f ? 1.0f + p : 1.0f);
}

static void merge_init(const float *knobs, void *ctx)
{
    (void)knobs; (void)ctx;
    st.last_seq = ~0u;
    st.primed = 0;
    for (int i = 0; i < 4; i++) st.g[i] = 1.0f;
}

static void merge_process(float *io, uint16_t nframes, const float *knobs, float *aux,
                          const uint8_t *enabled, const uint32_t *beat_ms, float **extra)
{
    (void)aux; (void)beat_ms; (void)extra;
    struct route_state *s = route_current(&st.last_seq);
    if (!s) {
        st.primed = 0;
        return;
    }
    int n = nframes > MB_BLOCK ? MB_BLOCK : nframes;
    int swapped = s->swap_seq == s->seq;
    const float *a = swapped ? s->buf : io;
    const float *b = swapped ? io : s->buf;

    float t[4];
    if (*enabled) {
        balance(mb_level_gain(knobs[0]), knobs[2], &t[0], &t[1]);
        balance(mb_level_gain(knobs[1]), knobs[3], &t[2], &t[3]);
        if (knobs[4] >= 0.5f) { t[2] = -t[2]; t[3] = -t[3]; }
    } else {
        t[0] = t[1] = t[2] = t[3] = 1.0f;
    }
    if (swapped && !s->b_on) t[2] = t[3] = 0.0f;
    if (!st.primed) {
        for (int k = 0; k < 4; k++) st.g[k] = t[k];
        st.primed = 1;
    }
    float g[4], inc[4];
    for (int k = 0; k < 4; k++) { g[k] = st.g[k]; inc[k] = (t[k] - g[k]) / (float)n; }
    for (int i = 0; i < n; i++) {
        for (int k = 0; k < 4; k++) g[k] += inc[k];
        float l = a[2 * i] * g[0] + b[2 * i] * g[2];
        float r = a[2 * i + 1] * g[1] + b[2 * i + 1] * g[3];
        io[2 * i] = l;
        io[2 * i + 1] = r;
    }
    for (int k = 0; k < 4; k++) st.g[k] = t[k];
}

MB_MODULE(ROUTE_MERGE_ID, merge_init, merge_process);
