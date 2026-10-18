/* Patch for the original app (V1.2.2): parameters shown as a list of texts that comes from the effect library,
 * so a new effect can have a "Mode" knob (or any other selector) with its own option names.
 *
 * Library parameter record (24 bytes): {i16 head, u16 name, f32 min, max, step, default, u8 idx, f1, f2, f3}.
 * head is -1 in all 866 factory parameters and the app never reads it. Here head >= 0 means "text list": it is
 * the offset, in the entry's string table, of a string "Option 0|Option 1|...". The value (min 0, step 1) is
 * the option index; values past the last option show the last one.
 *
 * Every parameter value the UI shows goes through FUN_00021f94(value s0, slot r0, param r1, buf r2, size r3)
 * (main screen knobs, chain edit knobs, EXP...; three wrappers tail-call it). Its first instruction is
 * replaced by a b.w to fmt_hook: text-list parameters are formatted here, everything else goes on to the
 * original code. An unpatched app shows the plain number for these parameters (use f3 = 0x15, "%.0f").
 */
#include <stdint.h>

#define APP_STATE   0x202381a8u     /* global settings + current preset (FUN_000399f0's DAT_00039cc4) */
#define SLOT(s)     ((const volatile uint8_t *)(APP_STATE + 0x268 + 0x44 * (s)))   /* on, menu, s16 index, id */
#define SLOTS       12
#define MENUS       16

/* FUN_0001b150(menu, index): library entry + 8 (= &id), or 0; index past the menu's end gives entry 0 */
#define lib_entry   ((const uint8_t *(*)(int, int))0x1b151u)

#define SEPARATOR   '|'
#define MAX_TEXT    1024            /* sanity bound for the option string */

/* round a non-negative float (bits) to the nearest integer, without the FPU (the patch is soft-float) */
static unsigned round_index(uint32_t bits)
{
    if (bits >> 31)
        return 0;
    int e = (int)((bits >> 23) & 0xff) - 127;
    if (e < -1)
        return 0;
    if (e > 15)
        return 0xffff;
    uint32_t m = (bits & 0x7fffff) | 0x800000;      /* value = m * 2^(e - 23) */
    return ((m >> (22 - e)) + 1) >> 1;              /* (2 * value + 1) / 2 */
}

/* 1 = formatted into buf; 0 = not a text-list parameter (the original code formats it) */
int text_value(unsigned slot, unsigned param, char *buf, unsigned size, uint32_t value)
{
    size &= 0xff;
    if (slot >= SLOTS || !size)
        return 0;
    const volatile uint8_t *s = SLOT(slot);
    int menu = (int8_t)s[1];
    if (menu < 0 || menu >= MENUS)
        return 0;
    int index = (menu == 5 || menu == 6 || menu == 11) ? 0 : (int16_t)(s[2] | s[3] << 8);   /* as the app */
    const uint8_t *e = lib_entry(menu, index);
    if (!e)
        return 0;
    unsigned n = e[8];
    if (param >= n)
        return 0;
    const uint8_t *rec = e + 0x10 + 0x18 * param;
    int head = (int16_t)(rec[0] | rec[1] << 8);
    if (head < 0)
        return 0;
    const uint8_t *strings = e + 0x10 + 0x18 * n;
    const uint8_t *end = e - 8 + (e[-4] | e[-3] << 8 | e[-2] << 16 | (uint32_t)e[-1] << 24);   /* header size */
    const char *p = (const char *)strings + head;
    if ((const uint8_t *)p >= end)
        return 0;
    if (end > (const uint8_t *)p + MAX_TEXT)
        end = (const uint8_t *)p + MAX_TEXT;

    /* skip to option k (or the last one) */
    unsigned k = round_index(value);
    for (const char *q = p; k && (const uint8_t *)q < end && *q; q++)
        if (*q == SEPARATOR) {
            p = q + 1;
            k--;
        }
    unsigned i = 0;
    while (i + 1 < size && (const uint8_t *)p + i < end && p[i] && p[i] != SEPARATOR) {
        buf[i] = p[i];
        i++;
    }
    buf[i] = 0;
    return 1;
}

/* replaces the first instruction of FUN_00021f94 (push.w {r4-r9, lr}); returns 0 like the original */
__attribute__((naked, section(".hook.fmt"), used))
void fmt_hook(void)
{
    __asm volatile(
        "push {r0-r4, lr}\n"
        ".inst.w 0xee104a10\n"          /* vmov r4, s0: the value */
        "sub sp, #8\n"
        "str r4, [sp]\n"                /* 5th argument */
        "bl text_value\n"
        "add sp, #8\n"
        ".inst.w 0xee004a10\n"          /* vmov s0, r4 */
        "cmp r0, #0\n"
        "pop {r0-r4, lr}\n"
        "itt ne\n"
        "movne r0, #0\n"
        "bxne lr\n"
        "push.w {r4-r9, lr}\n"
        "ldr ip, =0x00021f99\n"         /* rest of FUN_00021f94 */
        "bx ip\n"
        ".ltorg\n");
}
