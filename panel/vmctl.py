#!/usr/bin/env python3
"""vmctl — a persistent virtual machine control panel on top of minivmm.

The control plane (this file) never touches KVM. It only:
  * keeps a small "database" of VM definitions (a JSON file),
  * starts / stops the data plane (one minivmm process per VM),
  * observes those processes through ordinary OS interfaces (/proc),
  * enforces resource policies (admission control) before starting a VM.

Usage:
  vmctl.py create NAME --guest FILE [--mem MiB] [--vcpus N]
  vmctl.py run    NAME [--follow]
  vmctl.py list
  vmctl.py stop   NAME
  vmctl.py stats  (NAME | --pid PID) [--interval SEC] [--count N]
  vmctl.py resize NAME [--mem MiB] [--vcpus N] [--restart]
  vmctl.py logs   NAME [--lines N]
  vmctl.py delete NAME
"""
import argparse
import math
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE_BIN = ROOT / "core" / "minivmm"
STATE_DIR = Path(os.environ.get("VMCTL_HOME", ROOT / ".vmctl"))
STATE_FILE = STATE_DIR / "vms.json"

CLK_TCK = os.sysconf("SC_CLK_TCK")      # kernel ticks per second for utime/stime
HOST_CPUS = os.cpu_count() or 1

MIN_MEM_MB, MAX_MEM_MB = 2, 3072          # must match core/minivmm.c
MAX_VCPUS = 64
HOST_MEM_LIMIT = 0.80                     # running VMs may reserve <= 80% of host RAM
NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,31}$")


class VmctlError(Exception):
    pass


# --------------------------------------------------------------------------- #
# State ("database") helpers                                                    #
# --------------------------------------------------------------------------- #

def load_state():
    if not STATE_FILE.exists():
        return {}
    with open(STATE_FILE) as f:
        return json.load(f)


def save_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)           # atomic: never a half-written database


def get_vm(state, name):
    if name not in state:
        raise VmctlError(f"no VM named '{name}' (see: vmctl.py list)")
    return state[name]


def log_path(name):
    return STATE_DIR / f"{name}.log"


def stats_path(name):
    return STATE_DIR / f"{name}.stats.json"


def read_vm_stats(name):
    """The JSON that minivmm publishes every second (exit counters, state)."""
    try:
        with open(stats_path(name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# /proc readers: the VM is just a process, so ordinary OS tools can see it     #
# --------------------------------------------------------------------------- #

def process_start_ticks(pid):
    return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])


def is_running(vm):
    """True if vm['pid'] is alive *and* is really a minivmm (PIDs get reused)."""
    pid = vm.get("pid")
    if not pid:
        return False
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            argv = f.read().split(b"\0")
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().rsplit(")", 1)[1].split()
    except OSError:
        return False
    return (fields[0] != "Z" and bool(argv) and argv[0].endswith(b"minivmm")
            and (vm.get("start_ticks") is None or int(fields[19]) == vm["start_ticks"]))


def read_cpu_ticks(pid, tid=None):
    """Return utime + stime in ticks; comm may contain spaces and ')'."""
    path = Path(f"/proc/{pid}/stat" if tid is None else f"/proc/{pid}/task/{tid}/stat")
    fields = path.read_text().rsplit(")", 1)[1].split()
    return int(fields[11]) + int(fields[12])

def read_mem_status(pid):
    """VmRSS / VmSize / VmHWM (KiB) and Threads from /proc/<pid>/status."""
    out = {}
    with open(f"/proc/{pid}/status") as f:
        for line in f:
            key, _, value = line.partition(":")
            if key in ("VmRSS", "VmSize", "VmHWM"):
                out[key] = int(value.split()[0])          # value is "1234 kB"
            elif key == "Threads":
                out[key] = int(value)
    return out


def read_threads(pid):
    """{tid: comm} for every thread of the process (vcpu0, vcpu1, ...)."""
    threads = {}
    for tid in os.listdir(f"/proc/{pid}/task"):
        try:
            with open(f"/proc/{pid}/task/{tid}/comm") as f:
                threads[int(tid)] = f.read().strip()
        except OSError:
            pass                                          # thread just exited
    return threads


def host_mem_mb():
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) // 1024
    raise VmctlError("cannot read MemTotal from /proc/meminfo")


# --------------------------------------------------------------------------- #
# Policy / admission control                                                   #
# --------------------------------------------------------------------------- #

def validate_resources(mem_mb, vcpus, overcommit=False):
    if not MIN_MEM_MB <= mem_mb <= MAX_MEM_MB:
        raise VmctlError(f"--mem must be in [{MIN_MEM_MB}, {MAX_MEM_MB}] MiB")
    if not 1 <= vcpus <= MAX_VCPUS:
        raise VmctlError(f"--vcpus must be in [1, {MAX_VCPUS}]")
    if vcpus > HOST_CPUS and not overcommit:
        raise VmctlError(f"{vcpus} vCPUs > {HOST_CPUS} host CPUs (use --overcommit to allow)")


def admit(state, name, mem_mb):
    """Refuse to start a VM if running VMs would reserve too much host RAM."""
    reserved = sum(vm["mem_mb"] for n, vm in state.items() if n != name and is_running(vm))
    limit = int(host_mem_mb() * HOST_MEM_LIMIT)
    if reserved + mem_mb > limit:
        raise VmctlError(f"admission refused: {reserved} MiB already reserved + {mem_mb} MiB "
                         f"> {limit} MiB ({int(HOST_MEM_LIMIT * 100)}% of host RAM)")


# --------------------------------------------------------------------------- #
# Commands                                                                     #
# --------------------------------------------------------------------------- #

def cmd_create(args):
    state = load_state()
    if not NAME_RE.fullmatch(args.name):
        raise VmctlError("name must start with a letter and contain 1..32 letters, digits, '_' or '-'")
    if args.name in state:
        raise VmctlError(f"VM '{args.name}' already exists")
    guest = Path(args.guest).expanduser().resolve()
    if not guest.is_file():
        raise VmctlError(f"guest file does not exist: {guest}")
    validate_resources(args.mem, args.vcpus, args.overcommit)
    state[args.name] = dict(name=args.name, guest=str(guest), mem_mb=args.mem,
                           vcpus=args.vcpus, overcommit=args.overcommit, pid=None,
                           created_at=time.time(), started_at=None)
    save_state(state)
    print(f"created VM '{args.name}': {args.vcpus} vCPU, {args.mem} MiB, guest={guest.name}")

def start_vm(state, name):
    vm = get_vm(state, name)
    if is_running(vm):
        raise VmctlError(f"VM '{name}' is already running")
    if not CORE_BIN.is_file() or not os.access(CORE_BIN, os.X_OK):
        raise VmctlError("minivmm executable missing; run make first")
    if not Path(vm["guest"]).is_file():
        raise VmctlError(f"guest file does not exist: {vm['guest']}")
    validate_resources(vm["mem_mb"], vm["vcpus"], vm.get("overcommit", False))
    admit(state, name, vm["mem_mb"])
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    stats_path(name).unlink(missing_ok=True)  # never reuse counters from a previous run
    argv = [str(CORE_BIN), "--mem", str(vm["mem_mb"]), "--vcpus", str(vm["vcpus"]),
            "--stats", str(stats_path(name).resolve()), vm["guest"]]
    with log_path(name).open("w") as log:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True)
    vm["pid"] = proc.pid
    vm["started_at"] = time.time()
    vm["start_ticks"] = process_start_ticks(proc.pid)
    save_state(state)
    return proc

def cmd_run(args):
    state = load_state()
    proc = start_vm(state, args.name)
    finish_run(args, proc, state[args.name])


def finish_run(args, proc, vm):
    print(f"started VM '{args.name}' as pid {proc.pid} "
          f"({vm['vcpus']} vCPU, {vm['mem_mb']} MiB) — it is a normal host process")

    time.sleep(0.3)
    if proc.poll() is not None and proc.returncode != 0:
        print(f"VM exited immediately with code {proc.returncode}; log:", file=sys.stderr)
        print(log_path(args.name).read_text(), file=sys.stderr)
        sys.exit(1)

    if args.follow:                       # behave like `tail -f` until the VM ends
        try:
            with open(log_path(args.name)) as f:
                while True:
                    chunk = f.read()
                    if chunk:
                        print(chunk, end="", flush=True)
                    elif proc.poll() is not None:
                        break
                    else:
                        time.sleep(0.1)
        except KeyboardInterrupt:
            print(f"\n(detached; VM '{args.name}' is still running)")


def vm_status(name, vm):
    if is_running(vm):
        return "running"
    if vm.get("pid") is None:
        return "created"
    stats = read_vm_stats(name)
    return stats["state"] if stats else "exited"


def cmd_list(args):
    state = load_state()
    print(f"{'NAME':<32} {'STATE':<10} {'PID':>8} {'VCPUS':>6} {'MEM(MiB)':>9}  GUEST")
    for name, vm in sorted(state.items()):
        status = vm_status(name, vm)
        pid = str(vm["pid"]) if is_running(vm) else "-"
        print(f"{name:<32} {status:<10} {pid:>8} {vm['vcpus']:>6} {vm['mem_mb']:>9}  {Path(vm['guest']).name}")

def stop_vm(state, name, timeout=5.0):
    vm = get_vm(state, name)
    if not is_running(vm):
        print(f"VM '{name}' is not running")
        return False
    pid = vm["pid"]
    try:
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + timeout
        while is_running(vm) and time.monotonic() < deadline:
            time.sleep(0.05)
        if is_running(vm):
            os.kill(pid, signal.SIGKILL)
            deadline = time.monotonic() + 2
            while is_running(vm) and time.monotonic() < deadline:
                time.sleep(0.05)
            if is_running(vm):
                raise VmctlError(f"VM '{name}' did not terminate after SIGKILL")
    except ProcessLookupError:
        pass
    save_state(state)
    print(f"stopped VM '{name}' (pid {pid})")
    return True

def cmd_stop(args):
    state = load_state()
    if stop_vm(state, args.name):
        lines = log_path(args.name).read_text().splitlines()
        tail = [l for l in lines if l.strip()][-12:]
        print("\n".join(tail))


def cmd_stats(args):
    if (args.name is None) == (args.pid is None):
        raise VmctlError("provide exactly one of NAME or --pid PID")
    if not math.isfinite(args.interval) or args.interval <= 0 or args.count < 0:
        raise VmctlError("interval must be positive and finite; count must be nonnegative")
    if args.pid is not None:
        if args.pid <= 0:
            raise VmctlError("PID must be positive")
        pid = args.pid
        vm = None
        print(f"Process pid {pid}, host has {HOST_CPUS} CPUs (100% = one host CPU)")
    else:
        vm = get_vm(load_state(), args.name)
        if not is_running(vm):
            raise VmctlError(f"VM '{args.name}' is not running")
        pid = vm["pid"]
        print(f"VM '{args.name}': pid {pid}, {vm['vcpus']} vCPU, {vm['mem_mb']} MiB allocated, "
              f"host has {HOST_CPUS} CPUs (100% = one host CPU)")
    def snapshot():
        ticks = read_cpu_ticks(pid)
        stamp = time.monotonic()
        mem = read_mem_status(pid)
        threads = {}
        for tid, comm in read_threads(pid).items():
            try:
                threads[tid] = (comm, read_cpu_ticks(pid, tid))
            except (FileNotFoundError, ProcessLookupError):
                pass
        return stamp, ticks, mem, threads
    try:
        identity = process_start_ticks(pid)
        prev_time, prev_ticks, _, prev_threads = snapshot()
    except OSError as e:
        raise VmctlError(f"cannot monitor pid {pid}: {e.strerror}") from e
    prev_stats = read_vm_stats(args.name) if vm else None
    print(f"{'time':>8} {'CPU%':>8} {'RSS(MiB)':>10} {'VSZ(MiB)':>10} {'HWM(MiB)':>10} {'THR':>5} {'exits/s':>10}  per-thread CPU%", flush=True)
    n = 0
    try:
        while args.count == 0 or n < args.count:
            time.sleep(args.interval)
            try:
                if process_start_ticks(pid) != identity or (vm is not None and not is_running(vm)):
                    break
                now, ticks, mem, threads = snapshot()
            except (FileNotFoundError, ProcessLookupError):
                break
            if "VmSize" not in mem:  # process became a zombie
                break
            dt = now - prev_time
            cpu = max(0, ticks - prev_ticks) / CLK_TCK / dt * 100
            per_thread = []
            for tid, (comm, value) in sorted(threads.items()):
                if tid in prev_threads:
                    pcpu = max(0, value - prev_threads[tid][1]) / CLK_TCK / dt * 100
                    per_thread.append(f"{comm}={pcpu:.0f}")
            stats = read_vm_stats(args.name) if vm else None
            rate = "-"
            if stats and prev_stats:
                elapsed = stats["uptime_s"] - prev_stats["uptime_s"]
                if elapsed > 0:
                    rate = f"{max(0, stats['total_exits'] - prev_stats['total_exits']) / elapsed:.1f}"
            print(f"{time.strftime('%H:%M:%S'):>8} {cpu:8.1f} {mem.get('VmRSS', 0)/1024:10.1f} "
                  f"{mem.get('VmSize', 0)/1024:10.1f} {mem.get('VmHWM', 0)/1024:10.1f} "
                  f"{mem.get('Threads', 0):5d} {rate:>10}  {' '.join(per_thread)}", flush=True)
            prev_time, prev_ticks, prev_threads = now, ticks, threads
            if stats:
                prev_stats = stats
            n += 1
    except KeyboardInterrupt:
        pass
    if vm:
        stats = read_vm_stats(args.name)
        if stats:
            print("VM exits so far (userspace-visible): total=" + str(stats['total_exits']) + "  " +
                  "  ".join(f"{k}={v}" for k, v in stats['exits'].items()))

def cmd_resize(args):
    state = load_state()
    vm = get_vm(state, args.name)
    mem = vm["mem_mb"] if args.mem is None else args.mem
    vcpus = vm["vcpus"] if args.vcpus is None else args.vcpus
    overcommit = args.overcommit or vm.get("overcommit", False)
    validate_resources(mem, vcpus, overcommit)
    running = is_running(vm)
    if running and not args.restart:
        raise VmctlError("cold resize requires stopping the VM; use --restart")
    if running:
        admit(state, args.name, mem)  # reject before interrupting a healthy VM
        stop_vm(state, args.name)
    old_mem, old_vcpus = vm["mem_mb"], vm["vcpus"]
    vm.update(mem_mb=mem, vcpus=vcpus, overcommit=overcommit)
    save_state(state)
    print(f"resized VM '{args.name}': memory {old_mem} -> {mem} MiB, vCPUs {old_vcpus} -> {vcpus}")
    if running:
        proc = start_vm(state, args.name)
        time.sleep(0.3)
        if proc.poll() is not None and proc.returncode != 0:
            raise VmctlError(f"restart failed; see logs for '{args.name}'")
        print(f"restarted VM '{args.name}' as pid {proc.pid}")

def cmd_logs(args):
    state = load_state()
    get_vm(state, args.name)
    path = log_path(args.name)
    if not path.exists():
        raise VmctlError(f"VM '{args.name}' has never run")
    lines = path.read_text().splitlines()
    print("\n".join(lines[-args.lines:]))


def cmd_delete(args):
    state = load_state()
    vm = get_vm(state, args.name)
    if is_running(vm):
        raise VmctlError(f"VM '{args.name}' is running; stop it first")
    del state[args.name]
    save_state(state)
    for p in (log_path(args.name), stats_path(args.name)):
        p.unlink(missing_ok=True)
    print(f"deleted VM '{args.name}'")


# --------------------------------------------------------------------------- #

def build_parser():
    p = argparse.ArgumentParser(prog="vmctl.py", description="control panel for minivmm")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("create", help="define a new VM")
    s.add_argument("name")
    s.add_argument("--guest", required=True, help="raw guest image (.bin)")
    s.add_argument("--mem", type=int, default=64, help="RAM in MiB (default 64)")
    s.add_argument("--vcpus", type=int, default=1, help="number of vCPUs (default 1)")
    s.add_argument("--overcommit", action="store_true", help="allow more vCPUs than host CPUs")
    s.set_defaults(func=cmd_create)

    s = sub.add_parser("run", help="start a VM in the background")
    s.add_argument("name")
    s.add_argument("-f", "--follow", action="store_true", help="stream the guest's serial output")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("list", help="list VMs")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("stop", help="stop a running VM")
    s.add_argument("name")
    s.set_defaults(func=cmd_stop)

    s = sub.add_parser("stats", help="monitor CPU / memory / exits of a running VM")
    s.add_argument("name", nargs="?")
    s.add_argument("-p", "--pid", type=int, help="monitor any process instead (e.g. qemu)")
    s.add_argument("-i", "--interval", type=float, default=1.0, help="seconds between samples")
    s.add_argument("-n", "--count", type=int, default=0, help="number of samples (0 = until Ctrl+C)")
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser("resize", help="change the memory / vCPUs of a VM")
    s.add_argument("name")
    s.add_argument("--mem", type=int)
    s.add_argument("--vcpus", type=int)
    s.add_argument("--overcommit", action="store_true")
    s.add_argument("--restart", action="store_true", help="stop, resize and start a running VM")
    s.set_defaults(func=cmd_resize)

    s = sub.add_parser("logs", help="show the VM's serial output and exit table")
    s.add_argument("name")
    s.add_argument("-n", "--lines", type=int, default=40)
    s.set_defaults(func=cmd_logs)

    s = sub.add_parser("delete", help="forget a stopped VM")
    s.add_argument("name")
    s.set_defaults(func=cmd_delete)
    return p


def main():
    args = build_parser().parse_args()
    try:
        if args.cmd in {"create", "run", "stop", "resize", "delete"}:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            with (STATE_DIR / ".lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                if args.cmd == "run":
                    # The follow loop must not hold the database lock.
                    state = load_state()
                    proc = start_vm(state, args.name)
                    fcntl.flock(lock, fcntl.LOCK_UN)
                    finish_run(args, proc, state[args.name])
                else:
                    args.func(args)
        else:
            args.func(args)
    except (VmctlError, OSError, ValueError) as e:
        print(f"vmctl: error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
