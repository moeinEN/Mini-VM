# minivmm — A minimal KVM virtual machine monitor

A small KVM VMM and a persistent Python control panel. The core runs flat
32-bit x86 guests without an operating system or BIOS. The panel launches
one host process per VM and reads resource usage from `/proc`.

## Build and verify

On Ubuntu 22.04 or newer:

```sh
sudo apt install build-essential nasm python3 linux-libc-dev htop strace qemu-system-x86 qemu-utils
./check_env.sh
make
make test          # hello: 54 IO exits + 1 HLT
make check         # /proc parsing, policy, validation and external-PID monitoring
make check-kvm     # real KVM device I/O, 1/4 vCPUs, lifecycle and cold resize
```

An x86-64 Linux host with accessible `/dev/kvm` is required for `make test`
and `make check-kvm`. `make check` does not require KVM. The runtime uses
only the C library, pthreads, Linux KVM headers and the Python standard library.

## Control panel

Run commands from this directory. Guest paths are stored as absolute paths.

```sh
python3 panel/vmctl.py create web --guest guests/cpu_burn.bin --mem 64 --vcpus 2
python3 panel/vmctl.py run web
python3 panel/vmctl.py list
python3 panel/vmctl.py stats web -n 5
python3 panel/vmctl.py resize web --vcpus 4 --restart
python3 panel/vmctl.py stop web
python3 panel/vmctl.py logs web
python3 panel/vmctl.py delete web
python3 panel/vmctl.py stats --pid PID -n 5
```

`run --follow` streams the log; Ctrl+C detaches the panel while the VM keeps
running. Logs and exit counters are reset at each start. SIGTERM requests a
graceful VM stop; the panel waits up to five seconds before SIGKILL. `delete`
refuses a running VM. State is stored in `.vmctl/`, or in `VMCTL_HOME`.
State-changing commands use a file lock so concurrent admission checks cannot
reserve the same capacity. PID identity includes kernel process start ticks.

Memory is restricted to 2..3072 MiB and vCPUs to 1..64. More vCPUs than host
logical CPUs require `--overcommit`. The total reservation of running VMs
must fit within 80% of host `MemTotal`; `--overcommit` does not bypass this.
Resize preserves omitted values. A running VM requires `--restart`; resizing
is cold and loses its old guest execution state and RAM contents.

`stats` prints interval CPU usage with 100% per fully busy host logical CPU,
RSS, VSZ, high-water RSS, thread count, exit rate and per-thread usage. A
vanishing process ends sampling cleanly. `--pid` requires no VM definition
and has no VM-exit JSON file. `ps` CPU percentages use a different averaging
window, so they can differ from interval measurements.

## Files

- `core/minivmm.c`: KVM setup, run loop, serial/I/O emulation, atomic exit counts.
- `panel/vmctl.py`: lifecycle, resource policy, monitoring and cold resize.
- `guests/hello.asm`, `cpu_burn.asm`, `mem_touch.asm`: supplied workloads.
- `guests/io_probe.asm`: regression guest for IN, heartbeat and REP OUTSB.
- `tests/`: policy and real-KVM regression tests.

Some host kernels also create `kvm-nx-lpage-re`, an idle KVM maintenance
thread. On these kernels, minivmm thread counts are vCPUs + 2.

`make sweep` builds optional heartbeat-frequency variants. To shorten the
pause between memory-touch progress messages, build `mem_touch.asm` with a
smaller `DELAY_LOOPS` value:

```sh
nasm -f bin -DDELAY_LOOPS=0x100000 -o guests/mem_touch_fast.bin guests/mem_touch.asm
```
