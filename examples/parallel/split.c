/* Split: start of the parallel section (see route.h). Param 0 Mode: 0 Mono (L), 1 Mono (R), 2 Stereo. */
#include "route.h"

static struct route_state state;

static void split_init(const float *knobs, void *ctx)
{
    (void)knobs; (void)ctx;
    state.seq = 0;
    state.swap_seq = ~0u;
    state.toggle = 0xff;
    state.active = 0;
    state.b_on = 0;
    state.magic = ROUTE_MAGIC;
}

static void split_process(float *io, uint16_t nframes, const float *knobs, float *aux,
                          const uint8_t *enabled, const uint32_t *beat_ms, float **extra)
{
    (void)aux; (void)beat_ms; (void)extra;
    state.seq++;
    state.toggle = MB_HOST_BLOCK_TOGGLE;
    state.active = *enabled != 0;
    if (!state.active)
        return;
    int n = nframes > MB_BLOCK ? MB_BLOCK : nframes;
    int mode = (int)(knobs[0] + 0.5f);
    for (int i = 0; i < n; i++) {
        float l = io[2 * i], r = io[2 * i + 1];
        if (mode == 0) r = l;
        else if (mode == 1) l = r;
        io[2 * i] = state.buf[2 * i] = l;
        io[2 * i + 1] = state.buf[2 * i + 1] = r;
    }
}

__attribute__((section(".module_header"), used))
struct route_header mb_module_header = {
    { 0, {0, 0, 0, 0}, ROUTE_SPLIT_ID, __srom_base, split_init, split_process, MB_MAGIC }, &state };
