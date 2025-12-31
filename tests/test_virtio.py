"""
Test virtio device implementation by simulating guest behavior.
"""

import struct
import pytest
from god.devices.virtio import VirtioConsole
from god.devices.virtio.mmio import (
    VIRTIO_MMIO_MAGIC_VALUE,
    VIRTIO_MMIO_VERSION,
    VIRTIO_MMIO_DEVICE_ID,
    VIRTIO_MMIO_STATUS,
    VIRTIO_MMIO_QUEUE_SEL,
    VIRTIO_MMIO_QUEUE_NUM,
    VIRTIO_MMIO_QUEUE_NUM_MAX,
    VIRTIO_MMIO_QUEUE_DESC_LOW,
    VIRTIO_MMIO_QUEUE_DRIVER_LOW,
    VIRTIO_MMIO_QUEUE_DEVICE_LOW,
    VIRTIO_MMIO_QUEUE_READY,
    VIRTIO_MMIO_QUEUE_NOTIFY,
    VIRTIO_MMIO_INTERRUPT_STATUS,
    VIRTIO_MMIO_INTERRUPT_ACK,
    VIRTIO_MAGIC,
    VIRTIO_DEV_CONSOLE,
    VIRTIO_STATUS_ACKNOWLEDGE,
    VIRTIO_STATUS_DRIVER,
    VIRTIO_STATUS_FEATURES_OK,
    VIRTIO_STATUS_DRIVER_OK,
)
from god.devices.virtio.queue import VIRTQ_DESC_F_NEXT, VIRTQ_DESC_F_WRITE


class FakeMemory:
    """Fake memory for testing."""

    def __init__(self, size: int = 0x100000):
        self._data = bytearray(size)

    def read(self, addr: int, size: int) -> bytes:
        return bytes(self._data[addr:addr + size])

    def write(self, addr: int, data: bytes):
        self._data[addr:addr + len(data)] = data


class TestVirtioMMIO:
    """Test basic MMIO register access."""

    def test_magic_value(self):
        memory = FakeMemory()
        console = VirtioConsole(base_address=0x0a000000, memory=memory)

        magic = console.read(VIRTIO_MMIO_MAGIC_VALUE, 4)
        assert magic == VIRTIO_MAGIC

    def test_version(self):
        memory = FakeMemory()
        console = VirtioConsole(base_address=0x0a000000, memory=memory)

        version = console.read(VIRTIO_MMIO_VERSION, 4)
        assert version == 2  # Virtio 1.0+

    def test_device_id(self):
        memory = FakeMemory()
        console = VirtioConsole(base_address=0x0a000000, memory=memory)

        device_id = console.read(VIRTIO_MMIO_DEVICE_ID, 4)
        assert device_id == VIRTIO_DEV_CONSOLE

    def test_queue_num_max(self):
        memory = FakeMemory()
        console = VirtioConsole(base_address=0x0a000000, memory=memory)

        queue_num_max = console.read(VIRTIO_MMIO_QUEUE_NUM_MAX, 4)
        assert queue_num_max == 256


class TestVirtioConsole:
    """Test virtio console functionality."""

    def setup_method(self):
        """Set up test fixtures."""
        self.memory = FakeMemory(0x100000)
        self.output = bytearray()
        self.irq_raised = False

        def output_callback(data: bytes):
            self.output.extend(data)

        def irq_callback():
            self.irq_raised = True

        self.console = VirtioConsole(
            base_address=0x0a000000,
            memory=self.memory,
            output_callback=output_callback,
            irq_callback=irq_callback,
        )

    def _init_device(self):
        """Simulate guest device initialization."""
        # Reset
        self.console.write(VIRTIO_MMIO_STATUS, 4, 0)

        # Acknowledge
        self.console.write(VIRTIO_MMIO_STATUS, 4, VIRTIO_STATUS_ACKNOWLEDGE)

        # Driver
        self.console.write(
            VIRTIO_MMIO_STATUS, 4,
            VIRTIO_STATUS_ACKNOWLEDGE | VIRTIO_STATUS_DRIVER
        )

        # Features OK
        self.console.write(
            VIRTIO_MMIO_STATUS, 4,
            VIRTIO_STATUS_ACKNOWLEDGE | VIRTIO_STATUS_DRIVER | VIRTIO_STATUS_FEATURES_OK
        )

        # Driver OK
        self.console.write(
            VIRTIO_MMIO_STATUS, 4,
            VIRTIO_STATUS_ACKNOWLEDGE | VIRTIO_STATUS_DRIVER |
            VIRTIO_STATUS_FEATURES_OK | VIRTIO_STATUS_DRIVER_OK
        )

    def _setup_queue(self, queue_idx: int, queue_size: int, desc_addr: int, avail_addr: int, used_addr: int):
        """Set up a virtqueue."""
        # Select queue
        self.console.write(VIRTIO_MMIO_QUEUE_SEL, 4, queue_idx)

        # Set queue size
        self.console.write(VIRTIO_MMIO_QUEUE_NUM, 4, queue_size)

        # Set addresses
        self.console.write(VIRTIO_MMIO_QUEUE_DESC_LOW, 4, desc_addr & 0xFFFFFFFF)
        self.console.write(VIRTIO_MMIO_QUEUE_DRIVER_LOW, 4, avail_addr & 0xFFFFFFFF)
        self.console.write(VIRTIO_MMIO_QUEUE_DEVICE_LOW, 4, used_addr & 0xFFFFFFFF)

        # Mark ready
        self.console.write(VIRTIO_MMIO_QUEUE_READY, 4, 1)

    def _write_descriptor(self, addr: int, buf_addr: int, buf_len: int, flags: int, next_idx: int):
        """Write a descriptor to memory."""
        desc = struct.pack("<QIHH", buf_addr, buf_len, flags, next_idx)
        self.memory.write(addr, desc)

    def _write_avail_ring(self, addr: int, idx: int, entries: list[int]):
        """Write available ring to memory."""
        # flags (u16) + idx (u16)
        header = struct.pack("<HH", 0, idx)
        self.memory.write(addr, header)

        # ring entries
        for i, entry in enumerate(entries):
            entry_data = struct.pack("<H", entry)
            self.memory.write(addr + 4 + i * 2, entry_data)

    def _read_used_ring(self, addr: int) -> tuple[int, list[tuple[int, int]]]:
        """Read used ring from memory. Returns (idx, [(id, len), ...])."""
        header = self.memory.read(addr, 4)
        flags, idx = struct.unpack("<HH", header)

        entries = []
        for i in range(idx):
            entry_data = self.memory.read(addr + 4 + i * 8, 8)
            entry_id, entry_len = struct.unpack("<II", entry_data)
            entries.append((entry_id, entry_len))

        return idx, entries

    def test_device_initialization(self):
        """Test that device initialization sequence works."""
        self._init_device()

        # Check status
        status = self.console.read(VIRTIO_MMIO_STATUS, 4)
        expected = (
            VIRTIO_STATUS_ACKNOWLEDGE |
            VIRTIO_STATUS_DRIVER |
            VIRTIO_STATUS_FEATURES_OK |
            VIRTIO_STATUS_DRIVER_OK
        )
        assert status == expected

    def test_queue_setup(self):
        """Test queue configuration."""
        self._init_device()

        # Set up TX queue (queue 1)
        self._setup_queue(
            queue_idx=1,
            queue_size=16,
            desc_addr=0x10000,
            avail_addr=0x11000,
            used_addr=0x12000,
        )

        # Verify queue is ready
        self.console.write(VIRTIO_MMIO_QUEUE_SEL, 4, 1)
        ready = self.console.read(VIRTIO_MMIO_QUEUE_READY, 4)
        assert ready == 1

    def test_console_output_simple(self):
        """Test guest writing simple message to console."""
        # Initialize device
        self._init_device()

        # Set up TX queue (queue 1)
        self._setup_queue(
            queue_idx=1,
            queue_size=16,
            desc_addr=0x10000,
            avail_addr=0x11000,
            used_addr=0x12000,
        )

        # Write "Hello" to data buffer
        message = b"Hello, virtio!"
        self.memory.write(0x20000, message)

        # Set up descriptor pointing to our message
        self._write_descriptor(
            addr=0x10000,      # Descriptor 0
            buf_addr=0x20000,  # Buffer address
            buf_len=len(message),
            flags=0,           # No chaining, read-only
            next_idx=0,
        )

        # Add descriptor to available ring
        self._write_avail_ring(
            addr=0x11000,
            idx=1,        # One entry
            entries=[0],  # Descriptor 0
        )

        # Notify queue 1 (TX)
        self.console.write(VIRTIO_MMIO_QUEUE_NOTIFY, 4, 1)

        # Check output
        assert bytes(self.output) == message
        assert self.irq_raised

    def test_console_output_chained(self):
        """Test guest writing with chained descriptors."""
        self._init_device()

        # Set up TX queue
        self._setup_queue(
            queue_idx=1,
            queue_size=16,
            desc_addr=0x10000,
            avail_addr=0x11000,
            used_addr=0x12000,
        )

        # Write two parts of message to different buffers
        part1 = b"Hello, "
        part2 = b"world!"
        self.memory.write(0x20000, part1)
        self.memory.write(0x21000, part2)

        # Set up chained descriptors
        self._write_descriptor(
            addr=0x10000,      # Descriptor 0
            buf_addr=0x20000,
            buf_len=len(part1),
            flags=VIRTQ_DESC_F_NEXT,  # Chained
            next_idx=1,
        )
        self._write_descriptor(
            addr=0x10000 + 16,  # Descriptor 1
            buf_addr=0x21000,
            buf_len=len(part2),
            flags=0,           # End of chain
            next_idx=0,
        )

        # Add chain head to available ring
        self._write_avail_ring(
            addr=0x11000,
            idx=1,
            entries=[0],  # Chain starts at descriptor 0
        )

        # Notify
        self.console.write(VIRTIO_MMIO_QUEUE_NOTIFY, 4, 1)

        # Check output - should be concatenated
        assert bytes(self.output) == part1 + part2

    def test_console_multiple_requests(self):
        """Test multiple requests in sequence."""
        self._init_device()

        # Set up TX queue
        self._setup_queue(
            queue_idx=1,
            queue_size=16,
            desc_addr=0x10000,
            avail_addr=0x11000,
            used_addr=0x12000,
        )

        # Write two separate messages
        msg1 = b"First\n"
        msg2 = b"Second\n"
        self.memory.write(0x20000, msg1)
        self.memory.write(0x21000, msg2)

        # Two separate descriptors (not chained)
        self._write_descriptor(
            addr=0x10000,      # Descriptor 0
            buf_addr=0x20000,
            buf_len=len(msg1),
            flags=0,
            next_idx=0,
        )
        self._write_descriptor(
            addr=0x10000 + 16,  # Descriptor 1
            buf_addr=0x21000,
            buf_len=len(msg2),
            flags=0,
            next_idx=0,
        )

        # Add both to available ring
        self._write_avail_ring(
            addr=0x11000,
            idx=2,        # Two entries
            entries=[0, 1],
        )

        # Notify
        self.console.write(VIRTIO_MMIO_QUEUE_NOTIFY, 4, 1)

        # Check output
        assert bytes(self.output) == msg1 + msg2

        # Check used ring was updated
        used_idx, entries = self._read_used_ring(0x12000)
        assert used_idx == 2
        assert len(entries) == 2

    def test_interrupt_acknowledge(self):
        """Test interrupt acknowledgment."""
        self._init_device()

        # Set up TX queue
        self._setup_queue(
            queue_idx=1,
            queue_size=16,
            desc_addr=0x10000,
            avail_addr=0x11000,
            used_addr=0x12000,
        )

        # Send a message
        self.memory.write(0x20000, b"test")
        self._write_descriptor(
            addr=0x10000,
            buf_addr=0x20000,
            buf_len=4,
            flags=0,
            next_idx=0,
        )
        self._write_avail_ring(addr=0x11000, idx=1, entries=[0])
        self.console.write(VIRTIO_MMIO_QUEUE_NOTIFY, 4, 1)

        # Interrupt should be pending
        int_status = self.console.read(VIRTIO_MMIO_INTERRUPT_STATUS, 4)
        assert int_status == 1  # VIRTIO_INT_USED_RING

        # Acknowledge the interrupt
        self.console.write(VIRTIO_MMIO_INTERRUPT_ACK, 4, 1)

        # Interrupt should be cleared
        int_status = self.console.read(VIRTIO_MMIO_INTERRUPT_STATUS, 4)
        assert int_status == 0

    def test_device_reset(self):
        """Test device reset clears state."""
        self._init_device()

        # Set up queue
        self._setup_queue(
            queue_idx=1,
            queue_size=16,
            desc_addr=0x10000,
            avail_addr=0x11000,
            used_addr=0x12000,
        )

        # Reset device
        self.console.write(VIRTIO_MMIO_STATUS, 4, 0)

        # Status should be 0
        status = self.console.read(VIRTIO_MMIO_STATUS, 4)
        assert status == 0

        # Queue should not be ready anymore
        self.console.write(VIRTIO_MMIO_QUEUE_SEL, 4, 1)
        ready = self.console.read(VIRTIO_MMIO_QUEUE_READY, 4)
        assert ready == 0

    def test_console_input(self):
        """Test sending input to guest."""
        self._init_device()

        # Set up RX queue (queue 0)
        self._setup_queue(
            queue_idx=0,
            queue_size=16,
            desc_addr=0x10000,
            avail_addr=0x11000,
            used_addr=0x12000,
        )

        # Create a write descriptor (device writes to guest)
        self._write_descriptor(
            addr=0x10000,
            buf_addr=0x30000,  # Where guest wants input
            buf_len=64,
            flags=VIRTQ_DESC_F_WRITE,  # Device writes here
            next_idx=0,
        )

        # Make buffer available
        self._write_avail_ring(addr=0x11000, idx=1, entries=[0])

        # Send input to guest
        input_data = b"typed input"
        self.console.send_input(input_data)

        # Check data was written to guest buffer
        received = self.memory.read(0x30000, len(input_data))
        assert received == input_data

        # Check used ring
        used_idx, entries = self._read_used_ring(0x12000)
        assert used_idx == 1
        assert entries[0][0] == 0  # Descriptor 0
        assert entries[0][1] == len(input_data)  # Bytes written
