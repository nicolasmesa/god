"""
KVM interface layer.

This package provides Python bindings for the KVM (Kernel-based Virtual Machine)
API, allowing us to create and manage virtual machines from Python.

Main classes:
- KVMSystem: Represents /dev/kvm and provides system-level operations
"""

from .system import KVMError, KVMSystem

__all__ = ["KVMSystem", "KVMError"]
