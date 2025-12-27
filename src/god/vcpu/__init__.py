"""
Virtual CPU management package.

This package provides classes for creating and managing virtual CPUs
that can execute guest code.
"""

from .vcpu import VCPU, VCPUError
from . import registers

__all__ = ["VCPU", "VCPUError", "registers"]
