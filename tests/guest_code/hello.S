/*
 * hello.S - Hello World for our VMM
 *
 * This program prints "Hello, World!" to the PL011 UART and halts.
 *
 * How it works:
 * 1. Load the UART base address (0x09000000) into x1
 * 2. Load the message address into x2
 * 3. For each character:
 *    a. Read the Flag Register to check if TX FIFO has room
 *    b. Write the character to the Data Register
 * 4. When we hit the null terminator, halt
 *
 * The UART is at 0x09000000 (defined in our layout.py as UART.base).
 * We write to offset 0x000 (Data Register) to send characters.
 * We read from offset 0x018 (Flag Register) to check status.
 */

    .global _start

    /* UART addresses and offsets */
    .equ UART_BASE, 0x09000000
    .equ UART_DR,   0x000       /* Data Register - write chars here */
    .equ UART_FR,   0x018       /* Flag Register - check status here */
    .equ UART_FR_TXFF, (1 << 5) /* Transmit FIFO Full flag */

_start:
    /*
     * Load UART base address into x1.
     *
     * We can't just do "mov x1, #0x09000000" because ARM immediate
     * encoding can't represent this value directly. Instead we use:
     * - mov x1, #0x0000        (sets x1 to 0)
     * - movk x1, #0x0900, lsl #16  (inserts 0x0900 at bits 16-31)
     *
     * Result: x1 = 0x09000000
     */
    mov     x1, #0x0000
    movk    x1, #0x0900, lsl #16

    /*
     * Load message address into x2.
     *
     * ADR calculates a PC-relative address. This works because the
     * message is in the same code segment, close enough for the
     * +/- 1MB range of ADR.
     */
    adr     x2, message

print_loop:
    /*
     * Load next character into w0.
     *
     * LDRB loads a single byte. The [x2], #1 syntax means:
     * - Load from address in x2
     * - Then increment x2 by 1 (post-increment)
     *
     * We use w0 (32-bit) instead of x0 (64-bit) because we're
     * only dealing with bytes, and LDRB zero-extends to 32 bits.
     */
    ldrb    w0, [x2], #1

    /*
     * Check for null terminator.
     *
     * CBZ = "Compare and Branch if Zero"
     * If w0 is 0 (null byte), jump to 'done'.
     */
    cbz     w0, done

wait_tx_ready:
    /*
     * Check if transmit FIFO has room.
     *
     * Read the Flag Register and check the TXFF (TX FIFO Full) bit.
     * If TXFF is set (1), the FIFO is full and we need to wait.
     * If TXFF is clear (0), we can send.
     *
     * In our emulator, TXFF is always 0 (we have "instant" transmission),
     * but real code should check anyway.
     */
    ldr     w3, [x1, #UART_FR]      /* Read Flag Register */
    tst     w3, #UART_FR_TXFF       /* Test the TXFF bit */
    b.ne    wait_tx_ready           /* If set (NE = not zero), keep waiting */

    /*
     * Send the character.
     *
     * Write to the Data Register. The UART will transmit the byte.
     * In our emulator, this causes a VM exit and we print the char.
     */
    str     w0, [x1, #UART_DR]

    /* Loop back for next character */
    b       print_loop

done:
    /*
     * Shutdown using PSCI (Power State Coordination Interface).
     *
     * On ARM64 KVM, neither WFI nor HLT cause a VM exit:
     * - WFI just sleeps forever waiting for an interrupt (which never comes)
     * - HLT is a debug instruction that causes an exception
     *
     * Instead, ARM64 guests use PSCI to request shutdown. PSCI is a
     * firmware interface - we make a "hypervisor call" (HVC) with the
     * PSCI function ID in x0.
     *
     * PSCI_SYSTEM_OFF = 0x84000008 (using 32-bit calling convention)
     *
     * When KVM sees this HVC call, it recognizes it as PSCI and returns
     * KVM_EXIT_SYSTEM_EVENT to our VMM.
     */
    mov     x0, #0x0008             /* Lower 16 bits of PSCI_SYSTEM_OFF */
    movk    x0, #0x8400, lsl #16    /* x0 = 0x84000008 */
    hvc     #0                       /* Hypervisor call - triggers VM exit */

    /*
     * Infinite loop as a safety net.
     * We should never reach here - hvc should cause a VM exit.
     * But if something goes wrong, spin rather than execute garbage.
     */
spin:
    b       spin

    /*
     * The message to print.
     *
     * .align 4 ensures the string starts on a 4-byte boundary.
     * .asciz is "ASCII with Zero" - adds a null terminator.
     */
    .align 4
message:
    .asciz "Hello, World!\n"
