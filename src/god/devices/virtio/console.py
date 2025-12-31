"""
Virtio console device.

A paravirtualized console that's more efficient than UART because
it can batch multiple characters per notification.
"""

import logging
import sys
from typing import TYPE_CHECKING, Callable

from god.devices.virtio.mmio import VirtioMMIODevice, VIRTIO_DEV_CONSOLE

if TYPE_CHECKING:
    from god.memory import Memory

logger = logging.getLogger(__name__)

# Virtio console feature bits
VIRTIO_CONSOLE_F_SIZE = 0        # Console has configurable size
VIRTIO_CONSOLE_F_MULTIPORT = 1   # Console has multiple ports
VIRTIO_CONSOLE_F_EMERG_WRITE = 2 # Emergency write supported

# Queue indices
VIRTIO_CONSOLE_QUEUE_RX = 0   # Receive queue (VMM → guest)
VIRTIO_CONSOLE_QUEUE_TX = 1   # Transmit queue (guest → VMM)


class VirtioConsole(VirtioMMIODevice):
    """
    Virtio console device.

    Provides a serial console using virtio for efficient batched I/O.
    Has two queues:
      - Queue 0 (RX): VMM writes input to guest
      - Queue 1 (TX): Guest writes output to VMM
    """

    def __init__(
        self,
        base_address: int,
        memory: "Memory",
        irq_callback: Callable[[], None] | None = None,
        output_callback: Callable[[bytes], None] | None = None,
    ):
        """
        Create a virtio console.

        Args:
            base_address: Base address in guest physical memory
            memory: Guest memory for virtqueue access
            irq_callback: Called when device wants to raise an interrupt
            output_callback: Called with output data from guest
        """
        super().__init__(
            base_address=base_address,
            num_queues=2,  # RX and TX
            memory=memory,
            irq_callback=irq_callback,
        )

        self._output_callback = output_callback or self._default_output
        self._input_buffer = bytearray()  # Buffered input for guest

    @property
    def name(self) -> str:
        return "virtio-console"

    @property
    def device_id(self) -> int:
        return VIRTIO_DEV_CONSOLE

    @property
    def device_features(self) -> int:
        # We support emergency write (simple early boot output)
        return (1 << VIRTIO_CONSOLE_F_EMERG_WRITE)

    def _default_output(self, data: bytes):
        """Default output handler - write to stdout."""
        try:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        except Exception:
            pass

    def queue_notify(self, queue_index: int):
        """Handle queue notification from guest."""
        if queue_index == VIRTIO_CONSOLE_QUEUE_TX:
            self._process_tx_queue()
        elif queue_index == VIRTIO_CONSOLE_QUEUE_RX:
            # Guest made RX buffers available - try to fill them
            self._process_rx_queue()

    def _process_tx_queue(self):
        """
        Process the transmit queue (guest → VMM).

        Read data from descriptors and output it.
        """
        queue = self._queues[VIRTIO_CONSOLE_QUEUE_TX]

        if not queue.ready:
            logger.warning("TX queue not ready")
            return

        # Process all available requests
        while True:
            desc_head = queue.get_next_request()
            if desc_head is None:
                break

            # Follow the descriptor chain and collect output
            output_data = bytearray()
            chain = queue.follow_chain(desc_head)

            for desc in chain:
                # TX descriptors should be read-only (device reads from them)
                if desc.is_write:
                    logger.warning("TX descriptor unexpectedly marked as write")
                    continue

                # Read data from guest buffer
                data = self._memory.read(desc.addr, desc.len)
                output_data.extend(data)

            # Output the data
            if output_data:
                self._output_callback(bytes(output_data))

            # Mark the request as complete
            queue.put_used(desc_head, 0)

        # Raise interrupt to tell guest we processed some data
        self.raise_interrupt()

    def _process_rx_queue(self):
        """
        Process the receive queue (VMM → guest).

        If we have buffered input, write it to guest buffers.
        """
        if not self._input_buffer:
            return  # Nothing to send

        queue = self._queues[VIRTIO_CONSOLE_QUEUE_RX]

        if not queue.ready:
            return

        # Process available RX buffers
        while self._input_buffer:
            desc_head = queue.get_next_request()
            if desc_head is None:
                break  # No more buffers available

            # Follow the descriptor chain and fill buffers
            bytes_written = 0
            chain = queue.follow_chain(desc_head)

            for desc in chain:
                # RX descriptors should be write-only (device writes to them)
                if not desc.is_write:
                    logger.warning("RX descriptor not marked as write")
                    continue

                # How much can we write to this buffer?
                to_write = min(len(self._input_buffer), desc.len)
                if to_write == 0:
                    break

                # Write data to guest buffer
                data = bytes(self._input_buffer[:to_write])
                self._memory.write(desc.addr, data)
                del self._input_buffer[:to_write]
                bytes_written += to_write

            # Mark the request as complete
            queue.put_used(desc_head, bytes_written)

        # Raise interrupt to tell guest data is available
        if self._input_buffer or True:  # Always raise for now
            self.raise_interrupt()

    def send_input(self, data: bytes):
        """
        Send input data to the guest.

        Data is buffered until guest provides RX buffers.
        """
        self._input_buffer.extend(data)
        self._process_rx_queue()
