/*
 * simple.S - Our first guest program
 *
 * This is a minimal ARM64 program that runs inside our virtual machine.
 * It does three things:
 *   1. Loads the magic value 0xDEADBEEF into register x0
 *   2. Stores it at memory address 0x40001000 (inside our RAM)
 *   3. Halts the CPU
 *
 * After execution, the VMM can verify the guest worked correctly by
 * reading memory at 0x40001000 and checking for 0xDEADBEEF.
 *
 * Building:
 *   aarch64-linux-gnu-as -o simple.o simple.S
 *   aarch64-linux-gnu-ld -nostdlib -static -Ttext=0x40080000 -o simple simple.o
 *   aarch64-linux-gnu-objcopy -O binary simple simple.bin
 */

    .global _start

_start:
    /*
     * Load 0xDEADBEEF into x0
     *
     * ARM64 can't load arbitrary 32-bit constants in one instruction.
     * We use mov + movk (move keep):
     *   mov  loads the lower 16 bits, zeros the upper bits
     *   movk loads 16 bits into a specific position, keeping other bits
     */
    mov     x0, #0xBEEF             /* x0 = 0x000000000000BEEF */
    movk    x0, #0xDEAD, lsl #16    /* x0 = 0x00000000DEADBEEF */

    /*
     * Load address 0x40001000 into x1
     *
     * This address is in our RAM region (RAM starts at 0x40000000).
     * We build it the same way as above.
     */
    mov     x1, #0x1000             /* x1 = 0x0000000000001000 */
    movk    x1, #0x4000, lsl #16    /* x1 = 0x0000000040001000 */

    /*
     * Store x0 at the address in x1
     *
     * str = Store Register
     * [x1] means "the memory location pointed to by x1"
     */
    str     x0, [x1]

    /*
     * Halt the system using PSCI
     *
     * On ARM64, we use PSCI (Power State Coordination Interface) to
     * control power states. PSCI is a standard interface between
     * the OS and firmware/hypervisor.
     *
     * To halt, we call PSCI_SYSTEM_OFF:
     * - Put function ID 0x84000008 in x0 (32-bit call convention)
     * - Execute HVC #0 (Hypervisor Call)
     *
     * KVM implements PSCI and will exit with KVM_EXIT_SYSTEM_EVENT.
     *
     * Note: PSCI_CPU_OFF (0x84000002) doesn't work well for a single
     * vCPU. SYSTEM_OFF properly signals shutdown intent.
     *
     * Note: WFI (Wait For Interrupt) doesn't exit on KVM - it just
     * waits for an interrupt that never comes.
     */
    mov     x0, #0x0008             /* Lower bits of PSCI_SYSTEM_OFF */
    movk    x0, #0x8400, lsl #16    /* x0 = 0x84000008 */
    hvc     #0                      /* Call hypervisor */

    /*
     * Infinite loop (should never be reached)
     *
     * This is a safety measure. If somehow execution continues
     * after hlt, we spin forever rather than running off into
     * random memory.
     */
    b       .
