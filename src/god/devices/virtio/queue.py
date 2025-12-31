"""
Virtqueue implementation.

This module handles the virtqueue data structures: descriptor table,
available ring, and used ring. It provides methods to read descriptors
from guest memory and process I/O requests.
"""

import logging
import struct
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)

# Descriptor flags
VIRTQ_DESC_F_NEXT = 1       # Descriptor is chained
VIRTQ_DESC_F_WRITE = 2      # Buffer is write-only for device (device writes to it)
VIRTQ_DESC_F_INDIRECT = 4   # Buffer contains indirect descriptors

# Virtqueue size limits
VIRTQ_MAX_SIZE = 256        # Maximum queue size we support


@dataclass
class VirtqDesc:
    """
    A descriptor in the descriptor table.

    Each descriptor points to a buffer in guest memory and can be
    chained to form a scatter-gather list.
    """
    addr: int    # Guest physical address of buffer
    len: int     # Length of buffer in bytes
    flags: int   # VIRTQ_DESC_F_* flags
    next: int    # Index of next descriptor (if F_NEXT is set)

    @property
    def is_chained(self) -> bool:
        """Does this descriptor have a next descriptor?"""
        return bool(self.flags & VIRTQ_DESC_F_NEXT)

    @property
    def is_write(self) -> bool:
        """Should the device write to this buffer (vs read from it)?"""
        return bool(self.flags & VIRTQ_DESC_F_WRITE)


@dataclass
class VirtqUsedElem:
    """An element in the used ring."""
    id: int    # Descriptor chain head index
    len: int   # Total bytes written to write-able descriptors


# Memory access callback type
# Takes (guest_physical_address, size) -> bytes for reads
# Takes (guest_physical_address, data: bytes) -> None for writes
MemoryReader = Callable[[int, int], bytes]
MemoryWriter = Callable[[int, bytes], None]


class Virtqueue:
    """
    A single virtqueue.

    Manages the descriptor table, available ring, and used ring.
    Provides methods to get new requests and mark them complete.
    """

    def __init__(
        self,
        index: int,
        memory_read: MemoryReader,
        memory_write: MemoryWriter,
    ):
        """
        Create a virtqueue.

        Args:
            index: Queue index (0, 1, 2, ...)
            memory_read: Callback to read guest memory
            memory_write: Callback to write guest memory
        """
        self.index = index
        self._memory_read = memory_read
        self._memory_write = memory_write

        # Queue configuration (set during device setup)
        self.num = 0              # Queue size (number of descriptors)
        self.ready = False        # Is queue configured and ready?
        self.desc_addr = 0        # Descriptor table address
        self.avail_addr = 0       # Available ring address (driver area)
        self.used_addr = 0        # Used ring address (device area)

        # Runtime state
        self._last_avail_idx = 0  # Last available index we processed

    def reset(self):
        """Reset queue to initial state."""
        self.num = 0
        self.ready = False
        self.desc_addr = 0
        self.avail_addr = 0
        self.used_addr = 0
        self._last_avail_idx = 0

    # ─────────────────────────────────────────────────────────────
    # Reading from guest memory
    # ─────────────────────────────────────────────────────────────

    def _read_u16(self, addr: int) -> int:
        """Read a 16-bit value from guest memory."""
        data = self._memory_read(addr, 2)
        return struct.unpack("<H", data)[0]

    def _read_u32(self, addr: int) -> int:
        """Read a 32-bit value from guest memory."""
        data = self._memory_read(addr, 4)
        return struct.unpack("<I", data)[0]

    def _read_u64(self, addr: int) -> int:
        """Read a 64-bit value from guest memory."""
        data = self._memory_read(addr, 8)
        return struct.unpack("<Q", data)[0]

    def _write_u16(self, addr: int, value: int):
        """Write a 16-bit value to guest memory."""
        data = struct.pack("<H", value)
        self._memory_write(addr, data)

    def _write_u32(self, addr: int, value: int):
        """Write a 32-bit value to guest memory."""
        data = struct.pack("<I", value)
        self._memory_write(addr, data)

    # ─────────────────────────────────────────────────────────────
    # Descriptor table operations
    # ─────────────────────────────────────────────────────────────

    def read_descriptor(self, index: int) -> VirtqDesc:
        """
        Read a descriptor from the descriptor table.

        Descriptor layout (16 bytes):
          Offset 0:  addr (uint64_t)
          Offset 8:  len (uint32_t)
          Offset 12: flags (uint16_t)
          Offset 14: next (uint16_t)
        """
        if index >= self.num:
            raise ValueError(f"Descriptor index {index} >= queue size {self.num}")

        # Each descriptor is 16 bytes
        desc_offset = index * 16
        desc_base = self.desc_addr + desc_offset

        addr = self._read_u64(desc_base + 0)
        length = self._read_u32(desc_base + 8)
        flags = self._read_u16(desc_base + 12)
        next_idx = self._read_u16(desc_base + 14)

        return VirtqDesc(addr=addr, len=length, flags=flags, next=next_idx)

    def follow_chain(self, head: int) -> list[VirtqDesc]:
        """
        Follow a descriptor chain starting at 'head'.

        Returns list of descriptors in chain order.
        Raises ValueError if chain is malformed (too long, cycle).
        """
        chain = []
        visited = set()
        idx = head

        while True:
            if idx in visited:
                raise ValueError(f"Descriptor chain cycle detected at {idx}")
            if len(chain) > self.num:
                raise ValueError(f"Descriptor chain too long (>{self.num})")

            visited.add(idx)
            desc = self.read_descriptor(idx)
            chain.append(desc)

            if desc.is_chained:
                idx = desc.next
            else:
                break

        return chain

    # ─────────────────────────────────────────────────────────────
    # Available ring operations
    # ─────────────────────────────────────────────────────────────

    def _avail_idx(self) -> int:
        """Read the available ring idx (where guest will write next)."""
        # Available ring layout:
        #   Offset 0: flags (uint16_t)
        #   Offset 2: idx (uint16_t)
        #   Offset 4: ring[0] (uint16_t)
        #   ...
        return self._read_u16(self.avail_addr + 2)

    def _avail_ring_entry(self, ring_index: int) -> int:
        """Read an entry from the available ring."""
        # ring[] starts at offset 4
        return self._read_u16(self.avail_addr + 4 + ring_index * 2)

    def has_new_requests(self) -> bool:
        """Check if there are new requests in the available ring."""
        return self._last_avail_idx != self._avail_idx()

    def get_next_request(self) -> int | None:
        """
        Get the next request (descriptor chain head) from the available ring.

        Returns the descriptor head index, or None if no new requests.
        Updates internal tracking of processed requests.
        """
        avail_idx = self._avail_idx()

        if self._last_avail_idx == avail_idx:
            return None  # No new requests

        # Get the descriptor head from the ring
        ring_index = self._last_avail_idx % self.num
        desc_head = self._avail_ring_entry(ring_index)

        # Mark this entry as processed
        self._last_avail_idx += 1

        logger.debug(
            f"Queue {self.index}: got request, head={desc_head}, "
            f"avail_idx={avail_idx}, processed={self._last_avail_idx}"
        )

        return desc_head

    # ─────────────────────────────────────────────────────────────
    # Used ring operations
    # ─────────────────────────────────────────────────────────────

    def _used_idx(self) -> int:
        """Read the used ring idx."""
        # Used ring layout:
        #   Offset 0: flags (uint16_t)
        #   Offset 2: idx (uint16_t)
        #   Offset 4: ring[0] (struct { uint32_t id; uint32_t len; })
        #   ...
        return self._read_u16(self.used_addr + 2)

    def _set_used_idx(self, value: int):
        """Write the used ring idx."""
        self._write_u16(self.used_addr + 2, value & 0xFFFF)

    def _write_used_ring_entry(self, ring_index: int, desc_id: int, length: int):
        """Write an entry to the used ring."""
        # Each used ring entry is 8 bytes: id (u32) + len (u32)
        entry_addr = self.used_addr + 4 + ring_index * 8
        self._write_u32(entry_addr, desc_id)
        self._write_u32(entry_addr + 4, length)

    def put_used(self, desc_head: int, bytes_written: int):
        """
        Mark a descriptor chain as used (completed).

        Args:
            desc_head: Head of the descriptor chain that was processed
            bytes_written: Total bytes written to write-able descriptors
        """
        used_idx = self._used_idx()
        ring_index = used_idx % self.num

        # Write the used entry
        self._write_used_ring_entry(ring_index, desc_head, bytes_written)

        # Increment the used index (this publishes the entry to the guest)
        self._set_used_idx(used_idx + 1)

        logger.debug(
            f"Queue {self.index}: completed request, head={desc_head}, "
            f"bytes_written={bytes_written}, used_idx={used_idx + 1}"
        )
