/* Feature "clonetabs" (needs "clonedual"): the effect list of the Clone menu gets the tabs "Native" and
 * "NAM A2" next to "ALL", each one listing the slots whose data is a factory capture or a NAM blob.
 *
 * The effect-select screen is the same for every menu: tab t lists the entries with
 * FUN_0001ebd2(menu, i) == t - 1, FUN_000270c8(menu) gives the number of tabs after ALL and FUN_0001eb96 the
 * tab name. The pick still maps the row back to the entry index, here the Clone slot 0-9, so presets, ids
 * and the editor protocol do not change. The hooks below answer these three for menu 11; the pokes of
 * apppatch.py stop the screen from treating menu 11 as a single-tab menu (sub #0xb; cmp #5 -> sub #0xc;
 * cmp #4, so only menus 12-15 are left out).
 *
 * The UI never had an empty tab (picking in one would fall to slot 0), so the tabs follow what the slots
 * hold: only the types present get a tab, in the order factory, NAM.
 */
#include <stdint.h>

#define MENU_CLONE  11
#define CLONE_SLOTS 10
#define TAB_COUNTS  0x202381a8u     /* FUN_000270c8: u16 count[menu] at +0x8a4 */

#define subgroup    ((int (*)(int, int))0x1b691u)   /* FUN_0001b690(menu, idx): byte +0x11 of the entry */

int clone_entry(int menu, int idx);                 /* clone_dual.c: 1 when slot idx holds a NAM blob */

static unsigned clone_types(void)                   /* bit 0: some factory slot, bit 1: some NAM slot */
{
    unsigned t = 0;
    for (int i = 0; i < CLONE_SLOTS && t != 3; i++)
        t |= 1u << clone_entry(MENU_CLONE, i);
    return t;
}

/* FUN_0001ebd2(menu, idx): subgroup (tab - 1) of an entry; 0 for the menus 5, 6 and 11 */
__attribute__((section(".hook.clone_subgroup"), used))
int clone_subgroup(int menu, int idx)
{
    if (menu == MENU_CLONE)
        return (unsigned)idx < CLONE_SLOTS && clone_entry(menu, idx) && (clone_types() & 1);
    if (menu == 5 || menu == 6)
        return 0;
    return subgroup(menu, idx);
}

/* FUN_000270c8(menu): number of tabs after ALL */
__attribute__((section(".hook.clone_tabs"), used))
int clone_tab_count(int menu)
{
    if (menu == MENU_CLONE) {
        unsigned t = clone_types();
        return (t & 1) + (t >> 1);
    }
    return *(volatile uint16_t *)(TAB_COUNTS + 0x8a4 + 2 * menu);
}

/* FUN_0001eb96(menu, idx, buf, len) for menu 11: name of the tab of slot idx */
__attribute__((used))
int clone_tab_name(int menu, int idx, char *buf, int len)
{
    const char *s = clone_entry(menu, idx) ? "NAM A2" : "Native";
    int i = 0;
    for (; i < len - 1 && s[i]; i++)
        buf[i] = s[i];
    if (len > 0)
        buf[i] = 0;
    return 0;
}

__attribute__((naked, section(".hook.clone_tabname"), used))
void clone_tabname_hook(void)               /* FUN_0001eb96 */
{
    __asm volatile(
        "cmp r0, #11\n"
        "bne 1f\n"
        "b.w clone_tab_name\n"
        "1:\n"
        "push {r4, r5}\n"               /* replaced: push {r4, r5}; cmp r0, #5 */
        "cmp r0, #5\n"
        "ldr ip, =0x0001eb9b\n"         /* ldr and bx keep the flags; ip is rewritten right there */
        "bx ip\n"
        ".ltorg\n");
}
