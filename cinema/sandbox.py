"""Unprivileged Linux worker confinement. Failure is fatal on production Linux.

Landlock limits filesystem access; seccomp blocks process inspection, spawning,
Unix-domain connections, and signals to other processes. Network access remains
necessary for the configured media source and WebRTC grant.
"""
import ctypes
import errno
import os
import platform
import sys
import tempfile
from pathlib import Path


def protect_process():
    if sys.platform == "linux":
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(4, 0, 0, 0, 0):  # PR_SET_DUMPABLE: protect backend memory and /proc credentials.
            raise OSError(ctypes.get_errno(), "Cannot protect process credentials")


def confine_worker(local_file=None):
    if sys.platform != "linux":
        return  # Local macOS development only; the distributed image is Linux amd64.
    if platform.machine() not in {"x86_64", "aarch64"}:
        raise RuntimeError("Unsupported worker sandbox architecture")
    libc = ctypes.CDLL(None, use_errno=True)
    seccomp = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    protect_process()
    scratch = Path(os.environ.get("CINEMA_WORKER_TMP") or tempfile.mkdtemp(prefix="cinema-worker-")).resolve()
    os.environ["TMPDIR"] = str(scratch)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    tempfile.tempdir = str(scratch)
    sys.dont_write_bytecode = True
    if libc.prctl(38, 1, 0, 0, 0):  # PR_SET_NO_NEW_PRIVS
        raise OSError(ctypes.get_errno(), "Cannot initialize worker sandbox")
    abi = libc.syscall(444, 0, 0, 1)  # landlock_create_ruleset(..., VERSION)
    if abi < 1:
        raise RuntimeError("Worker requires Linux Landlock support (kernel 5.13+)")
    handled = (1 << 13) - 1
    if abi >= 2:
        handled |= 1 << 13  # REFER
    if abi >= 3:
        handled |= 1 << 14  # TRUNCATE
    if abi >= 5:
        handled |= 1 << 15  # IOCTL_DEV
    ruleset_attr = ctypes.c_uint64(handled)
    ruleset = libc.syscall(444, ctypes.byref(ruleset_attr), ctypes.sizeof(ruleset_attr), 0)
    if ruleset < 0:
        raise OSError(ctypes.get_errno(), "Cannot create worker filesystem rules")

    class PathRule(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]

    read = (1 << 2) | (1 << 3)

    def allow(path, access=read):
        path = Path(path)
        if not path.exists():
            return
        # File rules cannot carry directory-only access bits.
        if not path.is_dir():
            access &= (1 << 0) | (1 << 1) | (1 << 2) | (1 << 14) | (1 << 15)
        fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
        try:
            rule = PathRule(access & handled, fd)
            if libc.syscall(445, ruleset, 1, ctypes.byref(rule), 0):
                raise OSError(ctypes.get_errno(), "Cannot install worker filesystem rule")
        finally:
            os.close(fd)

    try:
        for path in {Path(__file__).resolve().parent, Path(sys.prefix) / "lib", Path(sys.base_prefix) / "lib",
                     Path(sys.executable).parent, Path("/usr/lib"), Path("/lib"), Path("/usr/share"),
                     Path("/etc/ssl/certs"), Path("/etc/resolv.conf"), Path("/etc/hosts"),
                     Path("/etc/nsswitch.conf"), Path("/etc/gai.conf"), Path("/etc/localtime"),
                     Path("/etc/ld.so.cache"), Path("/proc/self"), Path("/proc/thread-self"),
                     Path("/proc/cpuinfo"), Path("/proc/meminfo"), Path("/proc/sys"),
                     Path("/sys/devices"), Path("/sys/class/drm")}:
            allow(path)
        # Only the worker's own temporary directory is writable.
        allow(scratch, read | (1 << 1) | (1 << 4) | (1 << 5) | (1 << 7) | (1 << 8) |
              (1 << 12) | (1 << 13) | (1 << 14))
        for device in ("/dev/null", "/dev/random", "/dev/urandom", "/dev/dri"):
            allow(device, read | (1 << 1) | (1 << 15))
        if local_file:
            allow(Path(local_file).resolve())  # Only the operator's explicit probe file.
        if libc.syscall(446, ruleset, 0):
            raise OSError(ctypes.get_errno(), "Cannot enforce worker filesystem rules")
    finally:
        os.close(ruleset)

    class Comparison(ctypes.Structure):
        _fields_ = [("arg", ctypes.c_uint), ("op", ctypes.c_uint),
                    ("a", ctypes.c_uint64), ("b", ctypes.c_uint64)]

    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    seccomp.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int,
                                               ctypes.c_uint, ctypes.POINTER(Comparison)]
    seccomp.seccomp_attr_set.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32]
    seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
    context = seccomp.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not context:
        raise RuntimeError("Cannot initialize syscall filter")

    def deny(name, comparison=None, error=errno.EPERM):
        number = seccomp.seccomp_syscall_resolve_name(name.encode())
        if number >= 0:
            result = seccomp.seccomp_rule_add_array(context, 0x00050000 | error, number,
                        1 if comparison is not None else 0, ctypes.byref(comparison) if comparison is not None else None)
            if result:
                raise RuntimeError("Cannot install worker syscall rule")

    try:
        for name in ("ptrace", "process_vm_readv", "process_vm_writev", "pidfd_open", "pidfd_getfd",
                     "pidfd_send_signal", "kcmp", "fork", "vfork", "execve", "execveat", "unshare", "setns",
                     "mount", "umount2", "pivot_root", "chroot", "bpf", "perf_event_open", "keyctl",
                     "add_key", "request_key", "userfaultfd", "open_by_handle_at", "io_uring_setup", "tkill"):
            deny(name)
        deny("clone3", error=errno.ENOSYS)  # libc falls back to inspectable clone for threads.
        deny("clone", Comparison(0, 7, 0x10000, 0))  # Reject clone without CLONE_THREAD.
        for name in ("kill", "tgkill", "rt_sigqueueinfo", "rt_tgsigqueueinfo"):
            deny(name, Comparison(0, 1, os.getpid(), 0))
        deny("socket", Comparison(0, 4, 1, 0))  # No Unix-domain service/abstract sockets.
        if seccomp.seccomp_attr_set(context, 4, 1) or seccomp.seccomp_load(context):
            raise RuntimeError("Cannot enforce worker syscall filter")
    finally:
        seccomp.seccomp_release(context)
