/* Path B: the effects after this slot process the split signal; A's output waits in the Split buffer.
   Param 0 Input: gain into path B (dB, below -60 = Mute). Bypassed = path B muted at the Merge. */
#include "route.h"

static struct { uint32_t last_seq; float g; int primed; } st;

static void pathb_init(const float *knobs, void *ctx)
{
    (void)knobs; (void)ctx;
    st.last_seq = ~0u;
    st.g = 1.0f;
    st.primed = 0;
}

static void pathb_process(float *io, uint16_t nframes, const float *knobs, float *aux,
                          const uint8_t *enabled, const uint32_t *beat_ms, float **extra)
{
    (void)aux; (void)beat_ms; (void)extra;
    struct route_state *s = route_current(&st.last_seq);
    if (!s) {
        st.primed = 0;
        return;
    }
    int n = nframes > MB_BLOCK ? MB_BLOCK : nframes;
    float target = mb_level_gain(knobs[0]);
    if (!st.primed) {
        st.g = target;
        st.primed = 1;
    }
    float g = st.g, inc = (target - g) / (float)n;
    for (int i = 0; i < n; i++) {
        g += inc;
        float l = s->buf[2 * i] * g, r = s->buf[2 * i + 1] * g;
        s->buf[2 * i] = io[2 * i];
        s->buf[2 * i + 1] = io[2 * i + 1];
        io[2 * i] = l;
        io[2 * i + 1] = r;
    }
    st.g = target;
    s->swap_seq = s->seq;
    s->b_on = *enabled != 0;
}

MB_MODULE(ROUTE_PATHB_ID, pathb_init, pathb_process);
