/*
 * Parallel signal path for the Matribox II Pro, built from three ordinary chain modules:
 *
 *   ... -> [Split] -> A effects -> [Path B] -> B effects -> [Merge] -> ...
 *
 * Split   copies the signal into its own buffer and publishes that state through its module header.
 * Path B  swaps the chain signal with the buffer: the B effects get the split signal, A's output is parked.
 * Merge   mixes A and B (level, pan, phase) back into the chain. Without Path B, B is the dry split signal.
 *
 * The app runs the slots one after the other on one buffer (FUN_00031e18), so no app change is needed.
 * Path B and Merge find the Split through the app's table of loaded modules and only act when Split ran
 * earlier in the same audio block (block toggle + Split sequence number): a missing, bypassed or
 * misplaced Split leaves them transparent. Addresses below are those of app V1.2.2.
 */
#ifndef ROUTE_H
#define ROUTE_H

#include "matribox_module.h"

#define MB_HOST_MODULES      ((const volatile uint32_t *)0x20241f44u) /* header of each loaded module */
#define MB_HOST_SLOTS        12
#define MB_HOST_BLOCK_TOGGLE (*(const volatile uint8_t *)0x20234025u) /* flips at every audio block */
#define MB_ITCM_POOL         0x00058000u
#define MB_ITCM_POOL_END     0x00078000u
#define MB_OCRAM_POOL        0x20204000u
#define MB_OCRAM_POOL_END    0x20234000u
#define MB_BLOCK             64

#define ROUTE_SPLIT_ID 0x01000070u
#define ROUTE_PATHB_ID 0x01000071u
#define ROUTE_MERGE_ID 0x01000072u
#define ROUTE_MAGIC    0x31535452u   /* "RTS1" */

struct route_state {
    uint32_t magic;
    uint32_t seq;          /* incremented by Split on every Process call */
    uint32_t swap_seq;     /* == seq when Path B swapped during this block */
    uint8_t toggle;        /* host block toggle seen by Split */
    uint8_t active;        /* Split enabled in that block */
    uint8_t b_on;          /* Path B enabled in that block */
    uint8_t pad;
    float buf[2 * MB_BLOCK];
};

/* Split's header: the standard one followed by a pointer to its state */
struct route_header {
    struct mb_module_header h;
    struct route_state *state;
};

static inline struct route_state *route_find(void)
{
    for (int i = 0; i < MB_HOST_SLOTS; i++) {
        uint32_t a = MB_HOST_MODULES[i];
        if (a < MB_ITCM_POOL || a > MB_ITCM_POOL_END - sizeof(struct route_header) || (a & 3))
            continue;
        const struct route_header *r = (const struct route_header *)a;
        if (r->h.magic != MB_MAGIC || r->h.effect_id != ROUTE_SPLIT_ID)
            continue;
        uint32_t s = (uint32_t)r->state;
        if (s < MB_OCRAM_POOL || s > MB_OCRAM_POOL_END - sizeof(struct route_state) || (s & 3))
            continue;
        if (((struct route_state *)s)->magic == ROUTE_MAGIC)
            return (struct route_state *)s;
    }
    return 0;
}

/* The Split's state if it ran (enabled) earlier in this very block, else 0. *last_seq is per caller. */
static inline struct route_state *route_current(uint32_t *last_seq)
{
    struct route_state *s = route_find();
    if (!s)
        return 0;
    uint32_t seq = s->seq;
    int fresh = seq != *last_seq && s->toggle == MB_HOST_BLOCK_TOGGLE && s->active;
    *last_seq = seq;
    return fresh ? s : 0;
}

#endif
