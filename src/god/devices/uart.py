"""
PL011 UART emulation.

The PL011 is ARM's standard UART (Universal Asynchronous Receiver/Transmitter),
part of the PrimeCell peripheral family. It's what Linux uses when you specify
"console=ttyAMA0" on the kernel command line.

We emulate just enough for console output:
- Writes to the Data Register (DR) print characters to stdout
- Reads from the Flag Register (FR) report transmitter ready

Reference: ARM PrimeCell UART (PL011) Technical Reference Manual
"""

import sys
from typing import TextIO

from god.vm.layout import UART
from .device import Device


class PL011UART(Device):
    """
    PL011 UART (serial port) emulator.

    This provides basic serial console functionality:
    - Writes to DR output characters to the host terminal
    - Reads from FR return status (transmit ready, receive empty)

    The real PL011 has FIFOs, interrupts, DMA, and baud rate configuration.
    We skip most of that complexity - our "transmission" is instant (we just
    print to stdout), so we always report the transmit FIFO as empty.

    Usage:
        uart = PL011UART()
        registry.register(uart)
    """

    # ==========================================================================
    # Register Offsets
    # ==========================================================================
    # These are offsets from the base address (0x09000000)

    DR = 0x000      # Data Register - read/write serial data
    RSR = 0x004     # Receive Status Register / Error Clear Register
    FR = 0x018      # Flag Register - status flags
    ILPR = 0x020    # IrDA Low-Power Counter Register (not used)
    IBRD = 0x024    # Integer Baud Rate Divisor
    FBRD = 0x028    # Fractional Baud Rate Divisor
    LCR_H = 0x02C   # Line Control Register (data format: bits, parity, etc.)
    CR = 0x030      # Control Register (enable/disable UART)
    IFLS = 0x034    # Interrupt FIFO Level Select
    IMSC = 0x038    # Interrupt Mask Set/Clear
    RIS = 0x03C     # Raw Interrupt Status
    MIS = 0x040     # Masked Interrupt Status
    ICR = 0x044     # Interrupt Clear Register
    DMACR = 0x048   # DMA Control Register

    # ==========================================================================
    # Flag Register (FR) Bits
    # ==========================================================================
    # These tell the guest about the UART's current status

    FR_TXFE = 1 << 7  # Transmit FIFO Empty (1 = all data sent)
    FR_RXFF = 1 << 6  # Receive FIFO Full (1 = can't receive more)
    FR_TXFF = 1 << 5  # Transmit FIFO Full (1 = can't send more)
    FR_RXFE = 1 << 4  # Receive FIFO Empty (1 = no data to read)
    FR_BUSY = 1 << 3  # UART Busy transmitting

    # ==========================================================================
    # Control Register (CR) Bits
    # ==========================================================================

    CR_UARTEN = 1 << 0  # UART Enable
    CR_TXE = 1 << 8     # Transmit Enable
    CR_RXE = 1 << 9     # Receive Enable

    def __init__(
        self,
        output: TextIO = sys.stdout,
        base_address: int | None = None,
        size: int | None = None,
    ):
        """
        Create a PL011 UART.

        Args:
            output: Where to write output characters. Defaults to stdout.
                    You can pass a StringIO for testing.
            base_address: MMIO base address. Defaults to layout.UART.base.
            size: MMIO region size. Defaults to layout.UART.size.
        """
        self._output = output
        self._base_address = base_address if base_address is not None else UART.base
        self._size = size if size is not None else UART.size

        # Internal register state
        # Most of these are write-only or we ignore them, but we store
        # them in case the guest reads them back
        self._cr = 0          # Control Register
        self._lcr_h = 0       # Line Control Register
        self._ibrd = 0        # Integer Baud Rate Divisor
        self._fbrd = 0        # Fractional Baud Rate Divisor
        self._imsc = 0        # Interrupt Mask
        self._ris = 0         # Raw Interrupt Status

        # Receive buffer for future input support
        # Characters injected via inject_input() go here
        self._rx_buffer: list[int] = []

    @property
    def name(self) -> str:
        return "PL011 UART"

    @property
    def base_address(self) -> int:
        return self._base_address

    @property
    def size(self) -> int:
        return self._size

    def read(self, offset: int, size: int) -> int:
        """Handle a read from the UART."""

        if offset == self.DR:
            # Data Register read - return received character (if any)
            if self._rx_buffer:
                return self._rx_buffer.pop(0)
            return 0

        elif offset == self.FR:
            # Flag Register - tell guest about our status
            flags = 0

            # Transmit FIFO is always empty - we send instantly
            flags |= self.FR_TXFE

            # Receive FIFO is empty unless we have buffered input
            if not self._rx_buffer:
                flags |= self.FR_RXFE

            return flags

        elif offset == self.RSR:
            # Receive Status Register - no errors to report
            return 0

        elif offset == self.CR:
            return self._cr

        elif offset == self.LCR_H:
            return self._lcr_h

        elif offset == self.IBRD:
            return self._ibrd

        elif offset == self.FBRD:
            return self._fbrd

        elif offset == self.IMSC:
            return self._imsc

        elif offset == self.RIS:
            return self._ris

        elif offset == self.MIS:
            # Masked Interrupt Status = RIS & IMSC
            return self._ris & self._imsc

        else:
            # Unknown register - return 0
            # Real hardware might return different values, but 0 is safe
            return 0

    def write(self, offset: int, size: int, value: int):
        """Handle a write to the UART."""

        if offset == self.DR:
            # Data Register write - output the character!
            # Bottom 8 bits are the character to send
            char = value & 0xFF
            self._output.write(chr(char))
            self._output.flush()  # Make sure it appears immediately

        elif offset == self.RSR:
            # Writing to RSR clears error flags (we have none)
            pass

        elif offset == self.CR:
            self._cr = value

        elif offset == self.LCR_H:
            self._lcr_h = value

        elif offset == self.IBRD:
            self._ibrd = value

        elif offset == self.FBRD:
            self._fbrd = value

        elif offset == self.IMSC:
            self._imsc = value

        elif offset == self.ICR:
            # Interrupt Clear Register - clear specified interrupts
            self._ris &= ~value

        # Other registers are read-only or not important for basic operation

    def reset(self):
        """Reset the UART to initial state."""
        self._cr = 0
        self._lcr_h = 0
        self._ibrd = 0
        self._fbrd = 0
        self._imsc = 0
        self._ris = 0
        self._rx_buffer.clear()

    def inject_input(self, data: bytes):
        """
        Inject input data into the receive buffer.

        This simulates receiving data on the serial port. The guest
        can then read these bytes from the Data Register.

        Args:
            data: Bytes to make available to the guest.
        """
        for byte in data:
            self._rx_buffer.append(byte)
