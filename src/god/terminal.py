"""
Terminal mode management for raw/cooked mode switching.

When our VMM runs, we need the terminal in raw mode so:
- Characters are sent immediately (no line buffering)
- Special keys work (Ctrl+C, arrow keys, etc.)
- We can restore the terminal properly on exit

This module provides a context manager for safe terminal handling.
"""

import atexit
import sys
import termios
import tty
from typing import TextIO


class TerminalMode:
    """
    Manages terminal mode switching between raw and cooked modes.

    Raw mode sends characters immediately without buffering.
    Cooked mode (default) buffers until Enter and handles special keys.

    Usage:
        with TerminalMode(sys.stdin) as term:
            # Terminal is in raw mode here
            while True:
                if term.has_input():
                    char = term.read_char()
                    # process char...

    The terminal is automatically restored to its original mode on exit,
    even if an exception occurs.
    """

    def __init__(self, stream: TextIO = sys.stdin):
        """
        Create a terminal mode manager.

        Args:
            stream: The terminal stream to manage (default: stdin)
        """
        self._stream = stream
        self._fd = stream.fileno()
        self._original_attrs: list | None = None
        self._in_raw_mode = False

    def enter_raw_mode(self) -> None:
        """
        Switch the terminal to raw mode.

        In raw mode:
        - Input is available immediately (no buffering)
        - No echo of typed characters
        - Ctrl+C doesn't generate SIGINT
        - Special keys send escape sequences

        The original mode is saved and will be restored by exit_raw_mode()
        or automatically when the context manager exits.
        """
        if self._in_raw_mode:
            return

        # Save current terminal attributes
        self._original_attrs = termios.tcgetattr(self._fd)

        # Register cleanup handler in case of unexpected exit
        atexit.register(self._cleanup)

        # Switch to cbreak mode (not full raw mode)
        # We want:
        # - Input: unbuffered, no echo (guest handles echo)
        # - Output: normal translation (\n -> \r\n)
        #
        # tty.setraw() is too aggressive - it disables output processing
        # which causes newlines to not return to column 0 (staircase effect).
        # tty.setcbreak() is closer but still echoes input.
        #
        # We manually set the flags we need:
        new_attrs = termios.tcgetattr(self._fd)
        # Input flags: disable nothing special
        # new_attrs[0] = input flags (unchanged)
        # Output flags: keep OPOST for output processing (\n -> \r\n)
        # new_attrs[1] = output flags (unchanged)
        # Control flags: unchanged
        # new_attrs[2] = control flags (unchanged)
        # Local flags: disable ICANON (line buffering), ECHO, ISIG
        new_attrs[3] &= ~(termios.ICANON | termios.ECHO | termios.ISIG)
        # Special characters: set VMIN=1, VTIME=0 for immediate read
        new_attrs[6][termios.VMIN] = 1
        new_attrs[6][termios.VTIME] = 0
        termios.tcsetattr(self._fd, termios.TCSANOW, new_attrs)

        self._in_raw_mode = True

    def exit_raw_mode(self) -> None:
        """
        Restore the terminal to its original mode.

        This should be called before exiting, but the context manager
        and atexit handler will call it automatically if needed.
        """
        if not self._in_raw_mode:
            return

        if self._original_attrs is not None:
            # Restore original terminal attributes
            # TCSADRAIN waits for output to drain before applying
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._original_attrs)

        # Unregister cleanup handler
        try:
            atexit.unregister(self._cleanup)
        except Exception:
            pass

        self._in_raw_mode = False

    def _cleanup(self) -> None:
        """Cleanup handler for atexit - restores terminal mode."""
        self.exit_raw_mode()

    @property
    def fd(self) -> int:
        """Get the file descriptor for use with select()."""
        return self._fd

    @property
    def in_raw_mode(self) -> bool:
        """Check if terminal is currently in raw mode."""
        return self._in_raw_mode

    def read_char(self) -> bytes:
        """
        Read a single character from the terminal.

        Returns:
            A single byte as a bytes object.

        Note: This blocks if no input is available. Use select() to
        check for input first.
        """
        import os
        return os.read(self._fd, 1)

    def __enter__(self) -> "TerminalMode":
        """Enter context manager - switch to raw mode."""
        self.enter_raw_mode()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Exit context manager - restore terminal mode."""
        self.exit_raw_mode()
