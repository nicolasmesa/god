"""
ARM64 Linux kernel image handling.

This module parses the ARM64 kernel Image header to extract
boot parameters like text_offset and image_size.
"""

import struct
from dataclasses import dataclass
from pathlib import Path


class KernelError(Exception):
    """Exception raised for kernel-related errors."""

    pass


# ARM64 kernel magic number: "ARM\x64" in little-endian
ARM64_MAGIC = 0x644D5241


@dataclass
class KernelImage:
    """
    Parsed ARM64 kernel image.

    The ARM64 kernel Image has a 64-byte header containing
    boot parameters. This class parses that header and provides
    the information needed to load and boot the kernel.

    Attributes:
        path: Path to the kernel Image file
        text_offset: Offset from RAM base where kernel should be loaded
        image_size: Size of the kernel image in bytes
        flags: Kernel flags (endianness, page size, etc.)
        data: Raw kernel image bytes
    """

    path: Path
    text_offset: int
    image_size: int
    flags: int
    data: bytes

    @classmethod
    def load(cls, path: str | Path) -> "KernelImage":
        """
        Load and parse an ARM64 kernel image.

        Args:
            path: Path to the kernel Image file

        Returns:
            Parsed KernelImage

        Raises:
            KernelError: If the file is not a valid ARM64 kernel
            FileNotFoundError: If the file doesn't exist
        """
        path = Path(path)

        with open(path, "rb") as f:
            data = f.read()

        if len(data) < 64:
            raise KernelError(f"File too small ({len(data)} bytes) - not a valid kernel")

        # Parse the 64-byte header
        # struct arm64_image_header {
        #     uint32_t code0;        // offset 0
        #     uint32_t code1;        // offset 4
        #     uint64_t text_offset;  // offset 8
        #     uint64_t image_size;   // offset 16
        #     uint64_t flags;        // offset 24
        #     uint64_t res2;         // offset 32
        #     uint64_t res3;         // offset 40
        #     uint64_t res4;         // offset 48
        #     uint32_t magic;        // offset 56
        #     uint32_t res5;         // offset 60
        # };

        (
            code0,
            code1,
            text_offset,
            image_size,
            flags,
            res2,
            res3,
            res4,
            magic,
            res5,
        ) = struct.unpack("<IIQQQQQQ II", data[:64])

        # Verify magic number
        if magic != ARM64_MAGIC:
            raise KernelError(
                f"Invalid magic number: 0x{magic:08x} "
                f"(expected 0x{ARM64_MAGIC:08x} 'ARM\\x64')"
            )

        # Handle text_offset = 0
        # When text_offset is 0 and flags bit 3 is set, the kernel
        # can be loaded at any address. We use 0 (load at RAM base).
        # If flags bit 3 is not set, fall back to the default 512KB offset.
        if text_offset == 0:
            if flags & 0x8:  # Bit 3 = physical placement independent
                text_offset = 0  # Can load at RAM base
            else:
                text_offset = 0x80000  # 512 KB default

        # Sanity check image_size
        if image_size == 0:
            # Use actual file size
            image_size = len(data)

        return cls(
            path=path,
            text_offset=text_offset,
            image_size=image_size,
            flags=flags,
            data=data,
        )

    @property
    def is_little_endian(self) -> bool:
        """Check if kernel is little-endian."""
        return (self.flags & 1) == 0

    @property
    def page_size(self) -> int | None:
        """
        Get kernel's expected page size.

        Returns:
            Page size in bytes, or None if unspecified
        """
        ps = (self.flags >> 1) & 0x3
        if ps == 0:
            return None  # Unspecified
        elif ps == 1:
            return 4096  # 4 KB
        elif ps == 2:
            return 16384  # 16 KB
        elif ps == 3:
            return 65536  # 64 KB
        return None

    def __repr__(self) -> str:
        return (
            f"KernelImage(path={self.path}, "
            f"text_offset=0x{self.text_offset:x}, "
            f"image_size={self.image_size} bytes)"
        )
