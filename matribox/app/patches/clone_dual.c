/* Feature "clonedual": the Clone menu runs two engines, chosen per slot by what the slot holds.
 *
 * Menu 11 of the library gets the factory Clone at entry 0 and the NAM A2 module (examples/nam_clone,
 * build_clone.py --dual) at entry 1. The app picks a library entry as cat_entries[menu][idx], with idx past
 * the end -> entry 0, in three places:
 *   FUN_0001b538(slot, menu, idx)  copies the module ELF to the staging buffer (chain reload, mode 3)
 *   FUN_0001b4f4(slot, menu, idx)  memory check before inserting a block
 *   FUN_0001b598(slot, menu, idx)  memory check before replacing a block
 * For menu 11, idx is the Clone slot. The hooks below turn it into 1 when the slot's data starts with the
 * NAM blob magic ("NAM2", nam/a2blob.h) and into 0 otherwise, so a factory capture keeps the factory
 * engine and its memory budget. With a single entry in menu 11 (any other base), idx 1 still falls back
 * to entry 0: the patch changes nothing there.
 *
 * Each hook replaces the first 4 bytes of its function with a b.w here, runs the replaced instructions
 * and jumps back. The literal those instructions load (0x202364c8, the word holding the library index) is
 * the same in the three functions.
 */
#include <stdint.h>

#define MENU_CLONE  11
#define CLONE_SLOTS 10
#define A2B_MAGIC   0x324D414Eu     /* "NAM2" */

/* FUN_000249ec(slot): mapped flash address of the slot's data (0x60000000 + offset), or a zero block */
#define clone_data  ((uint32_t (*)(int))0x249edu)

__attribute__((used))
int clone_entry(int menu, int idx)
{
    if (menu != MENU_CLONE)
        return idx;
    if ((unsigned)idx >= CLONE_SLOTS)
        return 0;
    return *(const volatile uint32_t *)clone_data(idx) == A2B_MAGIC;
}

#define PICK_ENTRY                                                                      \
        "push {r0, r1, r3, lr}\n"                                                       \
        "mov r0, r1\n"                  /* menu */                                      \
        "mov r1, r2\n"                  /* idx */                                       \
        "bl clone_entry\n"                                                              \
        "mov r2, r0\n"                                                                  \
        "pop {r0, r1, r3, lr}\n"

__attribute__((naked, section(".hook.clone_stage"), used))
void clone_stage_hook(void)                 /* FUN_0001b538 */
{
    __asm volatile(
        PICK_ENTRY
        "ldr r3, =0x202364c8\n"         /* replaced: ldr r3, [pc, #0x50]; push {r4, lr} */
        "push {r4, lr}\n"
        "ldr ip, =0x0001b53d\n"
        "bx ip\n"
        ".ltorg\n");
}

__attribute__((naked, section(".hook.clone_insert"), used))
void clone_insert_hook(void)                /* FUN_0001b4f4 */
{
    __asm volatile(
        PICK_ENTRY
        "mov ip, r0\n"                  /* replaced: mov ip, r0; ldr r0, [pc, #0x3c] */
        "ldr r0, =0x202364c8\n"
        "ldr r3, =0x0001b4f9\n"         /* r3 is reloaded right there */
        "bx r3\n"
        ".ltorg\n");
}

__attribute__((naked, section(".hook.clone_replace"), used))
void clone_replace_hook(void)               /* FUN_0001b598 */
{
    __asm volatile(
        PICK_ENTRY
        "mov ip, r0\n"                  /* replaced: mov ip, r0; ldr r0, [pc, #0x3c] */
        "ldr r0, =0x202364c8\n"
        "ldr r3, =0x0001b59d\n"
        "bx r3\n"
        ".ltorg\n");
}
