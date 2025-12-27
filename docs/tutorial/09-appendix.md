# Chapter 9: Appendix and Reference

This appendix provides reference material, troubleshooting guides, and further reading.

## Complete Project Structure

```
veleiro-god/
├── src/god/
│   ├── __init__.py              # Package version
│   ├── cli.py                   # Typer CLI commands
│   │
│   ├── kvm/                     # KVM interface layer
│   │   ├── __init__.py
│   │   ├── bindings.py          # cffi C definitions
│   │   ├── capabilities.py      # KVM capability checking
│   │   ├── constants.py         # KVM constants
│   │   └── system.py            # KVMSystem class
│   │
│   ├── vm/                      # Virtual machine management
│   │   ├── __init__.py
│   │   ├── vm.py                # VirtualMachine class
│   │   ├── memory.py            # MemoryManager class
│   │   └── layout.py            # Address space layout
│   │
│   ├── vcpu/                    # Virtual CPU management
│   │   ├── __init__.py
│   │   ├── vcpu.py              # VCPU class
│   │   ├── runner.py            # VMRunner class
│   │   └── registers.py         # ARM64 register IDs
│   │
│   ├── devices/                 # Emulated devices
│   │   ├── __init__.py
│   │   ├── device.py            # Base Device class
│   │   ├── registry.py          # DeviceRegistry
│   │   ├── uart.py              # PL011 UART
│   │   ├── gic.py               # GIC setup
│   │   ├── timer.py             # Timer config
│   │   └── virtio/              # Virtio devices
│   │       ├── __init__.py
│   │       ├── mmio.py          # Virtio MMIO transport
│   │       ├── queue.py         # Virtqueue handling
│   │       ├── block.py         # virtio-blk
│   │       └── console.py       # virtio-console
│   │
│   └── boot/                    # Linux boot support
│       ├── __init__.py
│       ├── dtb.py               # Device Tree generation
│       ├── loader.py            # Kernel/initrd loader
│       └── protocol.py          # ARM64 boot protocol
│
├── tests/
│   ├── __init__.py
│   ├── guest_code/              # Test guest programs
│   │   ├── simple.S
│   │   ├── hello.S
│   │   └── Makefile
│   ├── test_kvm.py
│   ├── test_vm.py
│   ├── test_vcpu.py
│   └── test_devices.py
│
├── docs/
│   └── tutorial/                # This tutorial!
│       ├── 00-introduction.md
│       ├── 01-kvm-foundation.md
│       ├── 02-vm-creation-memory.md
│       ├── 03-vcpu-run-loop.md
│       ├── 04-serial-console.md
│       ├── 05-interrupt-controller.md
│       ├── 06-timer.md
│       ├── 07-virtio-devices.md
│       ├── 08-booting-linux.md
│       └── 09-appendix.md
│
├── pyproject.toml               # Project configuration
└── README.md
```

## KVM ioctl Reference

### System ioctls (on /dev/kvm)

| ioctl | Value | Description |
|-------|-------|-------------|
| KVM_GET_API_VERSION | 0x00 | Get API version (must be 12) |
| KVM_CREATE_VM | 0x01 | Create new VM, returns VM fd |
| KVM_CHECK_EXTENSION | 0x03 | Check if capability is supported |
| KVM_GET_VCPU_MMAP_SIZE | 0x04 | Get size of kvm_run mmap area |

### VM ioctls (on VM fd)

| ioctl | Description |
|-------|-------------|
| KVM_CREATE_VCPU | Create vCPU, returns vCPU fd |
| KVM_SET_USER_MEMORY_REGION | Register guest memory |
| KVM_CREATE_DEVICE | Create in-kernel device (GIC) |
| KVM_IRQ_LINE | Assert/deassert interrupt line |

### vCPU ioctls (on vCPU fd)

| ioctl | Description |
|-------|-------------|
| KVM_RUN | Run vCPU until exit |
| KVM_GET_ONE_REG | Get single register value |
| KVM_SET_ONE_REG | Set single register value |
| KVM_ARM_VCPU_INIT | Initialize vCPU (ARM64) |
| KVM_ARM_PREFERRED_TARGET | Get preferred CPU type |

## ARM64 Register Reference

### Core Register IDs

```python
# Format: KVM_REG_ARM64 | KVM_REG_SIZE_U64 | KVM_REG_ARM_CORE | (index * 2)

X0  = 0x6030_0000_0010_0000  # General purpose x0
X1  = 0x6030_0000_0010_0002  # General purpose x1
...
X30 = 0x6030_0000_0010_003C  # Link register (LR)
SP  = 0x6030_0000_0010_003E  # Stack pointer
PC  = 0x6030_0000_0010_0040  # Program counter
PSTATE = 0x6030_0000_0010_0042  # Processor state
```

### PSTATE Bits

| Bit | Name | Description |
|-----|------|-------------|
| 0-3 | M | Mode (EL and SP select) |
| 6 | F | FIQ mask |
| 7 | I | IRQ mask |
| 8 | A | Async abort mask |
| 9 | D | Debug mask |
| 28 | V | Overflow flag |
| 29 | C | Carry flag |
| 30 | Z | Zero flag |
| 31 | N | Negative flag |

### PSTATE Mode Values

| Value | Mode | Description |
|-------|------|-------------|
| 0b0000 | EL0t | EL0 with SP_EL0 |
| 0b0100 | EL1t | EL1 with SP_EL0 |
| 0b0101 | EL1h | EL1 with SP_EL1 |
| 0b1000 | EL2t | EL2 with SP_EL0 |
| 0b1001 | EL2h | EL2 with SP_EL2 |

## Memory Layout Reference

```
0x00000000 - 0x08000000 : Reserved
0x08000000 - 0x08010000 : GIC Distributor (64 KB)
0x080A0000 - 0x080C0000 : GIC Redistributor (128 KB per CPU)
0x09000000 - 0x09001000 : UART (PL011)
0x0A000000 - 0x0A008000 : Virtio devices (4 KB each)
0x40000000 - 0x80000000 : RAM (configurable, default 1 GB)
```

## Interrupt Numbers

### SGIs (0-15)
Software Generated Interrupts for inter-processor communication.

### PPIs (16-31)
Per-CPU interrupts:
- 27: Virtual timer
- 30: Physical timer

### SPIs (32+)
Shared Peripheral Interrupts:
- 33 (SPI 1): UART
- 48-55 (SPI 16-23): Virtio devices

## Troubleshooting Guide

### "KVM device not found"

```bash
# Check if KVM module is loaded
lsmod | grep kvm

# Load KVM modules (if needed)
sudo modprobe kvm
sudo modprobe kvm_arm  # or kvm_intel/kvm_amd on x86
```

### "Permission denied" on /dev/kvm

```bash
# Add yourself to kvm group
sudo usermod -aG kvm $USER

# Log out and back in, or:
newgrp kvm
```

### VM exits immediately

- Check PSTATE is valid (EL1h = 0x05)
- Verify PC points to valid memory
- Ensure memory regions are registered

### No serial output

- Verify UART address (0x09000000)
- Check guest is writing to correct address
- Ensure UART device is registered

### Linux hangs at boot

- Check GIC is created before vCPUs
- Verify timer is configured
- Add `debug` to kernel command line
- Use `earlycon=pl011,0x09000000`

### Kernel panic

- Check Device Tree is valid
- Verify all required nodes exist
- Look at panic message for clues

## Further Reading

### Documentation

- [KVM Documentation](https://www.kernel.org/doc/html/latest/virt/kvm/index.html)
- [ARM Architecture Reference Manual](https://developer.arm.com/documentation/ddi0487/latest/)
- [Device Tree Specification](https://www.devicetree.org/specifications/)
- [Virtio Specification](https://docs.oasis-open.org/virtio/virtio/v1.2/)

### Source Code to Study

- **QEMU**: The reference VMM implementation
  - `hw/arm/virt.c`: QEMU's virt machine
  - `target/arm/kvm64.c`: ARM64 KVM support

- **Firecracker**: Amazon's minimal VMM
  - Written in Rust, very clean code
  - Focus on security and simplicity

- **Cloud Hypervisor**: Intel's VMM
  - Modern Rust implementation
  - Good virtio implementation

- **Linux KVM**: Kernel side
  - `arch/arm64/kvm/`: ARM64 KVM code
  - `virt/kvm/kvm_main.c`: Core KVM

### Books

- "Understanding the Linux Kernel" by Bovet & Cesati
- "Linux Device Drivers" by Corbet et al.
- "Computer Organization and Design: ARM Edition" by Patterson & Hennessy

## Future Improvements

### Phase 9+: What's Next?

**SMP (Symmetric Multi-Processing)**
- Create multiple vCPUs
- Handle IPIs (Inter-Processor Interrupts)
- PSCI for CPU hotplug

**Networking**
- virtio-net device
- TAP device integration
- Bridge networking

**Storage Improvements**
- QCOW2 format support
- Copy-on-write
- Snapshots

**Graphics**
- virtio-gpu
- Frame buffer
- VNC/Spice

**Performance**
- io_uring for disk I/O
- vhost-user for device offload
- Huge pages

**Live Migration**
- Save VM state
- Transfer over network
- Resume on another host

## Acknowledgments

This project was built to learn. Thanks to:

- The Linux KVM developers
- The QEMU project
- ARM for detailed documentation
- The open source virtualization community

## Conclusion

You've built a Virtual Machine Monitor from scratch! You now understand:

- How hardware virtualization works
- The KVM API
- ARM64 architecture
- Device emulation
- How operating systems boot

This knowledge applies far beyond just VMMs. You understand systems at a deeper level than most developers ever will.

Keep building, keep learning!
