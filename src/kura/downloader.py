"""Foreground aria2 adapter. No shell, config, hooks, daemon or RPC."""
import ctypes
import os
from pathlib import Path
import signal
import sys
import shutil
import subprocess

from .models import KuraError
from .safety import ensure_dir, private_file, regular_file
from .torrent import Torrent, verify

RESERVE = 64 * 1024**2


def parent_death_guard():
    """Linux/Android: terminate aria2 even if its supervising Python gets SIGKILL."""
    if not (sys.platform.startswith("linux") or sys.platform == "android"):
        raise KuraError("unsupported_platform", "This MVP requires native Termux or Linux")
    libc = ctypes.CDLL(None, use_errno=True)
    parent = os.getpid()

    def guard():
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
            os._exit(126)
        # Close the fork/prctl race if the parent already died.
        if os.getppid() != parent:
            os.kill(os.getpid(), signal.SIGTERM)
    return guard


def stop_child(proc):
    """Cleanup is not cancellable: retain supervision until the child is reaped."""
    previous = {sig: signal.signal(sig, signal.SIG_IGN) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        while True:
            try:
                if proc.poll() is None:
                    try:
                        proc.terminate()
                    except ProcessLookupError:
                        pass
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        try:
                            proc.kill()
                        except ProcessLookupError:
                            pass
                        proc.wait()
                break
            except KeyboardInterrupt:
                # Also resilient to a pending Python interrupt delivered before
                # SIG_IGN took effect (or injected by a test).
                continue
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def run(torrent: Torrent, directory: Path, state_root: Path, timeout: int = 1800, *,
        lock_fd: int | None = None) -> dict:
    binary = shutil.which("aria2c")
    if not binary:
        raise KuraError("downloader_missing", "Install aria2: pkg install aria2")
    ensure_dir(directory)
    payload = directory / torrent.name
    allocated = 0
    if payload.exists() or payload.is_symlink():
        regular_file(payload)
        stat = payload.stat()
        # st_size alone overcredits sparse files. Only already allocated blocks
        # within this exact validated payload may be reused during explicit resume.
        allocated = min(torrent.length, stat.st_size, stat.st_blocks * 512)
    required = torrent.length - allocated + RESERVE
    if shutil.disk_usage(directory).free < required:
        raise KuraError("insufficient_space", "Need remaining unallocated bytes plus 64 MiB reserve",
                        required_free_bytes=required, allocated_bytes=allocated)
    # Unique private descriptor outside the media directory; never stored in Git.
    descriptor = state_root / (torrent.infohash + ".torrent")
    if descriptor.exists() or descriptor.is_symlink():
        regular_file(descriptor)
        descriptor.unlink()
    with private_file(descriptor) as f:
        f.write(torrent.sanitized)
    args = [
        binary, "--no-conf=true", "--enable-rpc=false",
        "--enable-dht=false", "--enable-dht6=false", "--bt-enable-lpd=false",
        "--bt-exclude-tracker=*", "--bt-tracker=http://nyaa.tracker.wf:7777/announce",
        "--seed-time=0", "--bt-metadata-only=false", "--bt-save-metadata=false",
        "--check-integrity=true", "--allow-overwrite=false", "--auto-file-renaming=false",
        "--file-allocation=none", "--max-concurrent-downloads=1", "--max-tries=2",
        "--connect-timeout=15", "--timeout=30", "--bt-stop-timeout=120",
        "--bt-tracker-connect-timeout=10", "--bt-tracker-timeout=15",
        "--summary-interval=0", "--console-log-level=error", "--download-result=hide",
        "--listen-port=49160-49200", "--disable-ipv6=true",
        "--dir=" + str(directory), "--torrent-file=" + str(descriptor),
    ]
    # No inherited proxy, ARIA2 configuration, shell environment or stdin.
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(state_root),
           "TMPDIR": os.environ.get("TMPDIR", str(state_root)), "LANG": "C.UTF-8"}
    proc = None
    try:
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, env=env,
                                preexec_fn=parent_death_guard(),
                                pass_fds=(lock_fd,) if lock_fd is not None else ())
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise KuraError("download_timeout", "aria2 timed out; partial data preserved; retry with --resume") from e
        if code != 0:
            raise KuraError("download_failed", "aria2 did not complete; partial data preserved",
                            downloader_exit=code)
        return verify(torrent, directory)
    except OSError as e:
        raise KuraError("downloader_error", "Could not execute aria2") from e
    finally:
        if proc is not None:
            stop_child(proc)
        descriptor.unlink(missing_ok=True)
