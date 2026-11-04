"""Test-only, credential-free diagnostics for Windows detached fixture launches."""

import ctypes
import json
import sys
from contextlib import contextmanager
from ctypes import wintypes as wt

from slack_codex_bridge import windows


class BasicLimits(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                ("flags", wt.DWORD), ("minimum", ctypes.c_size_t), ("maximum", ctypes.c_size_t),
                ("active", wt.DWORD), ("affinity", ctypes.c_size_t),
                ("priority", wt.DWORD), ("scheduling", wt.DWORD)]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [("basic", BasicLimits), ("io", ctypes.c_ulonglong * 6),
                ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]


def job_context():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wt.HANDLE
    kernel.IsProcessInJob.argtypes = [wt.HANDLE, wt.HANDLE, ctypes.POINTER(wt.BOOL)]
    kernel.IsProcessInJob.restype = wt.BOOL
    inside = wt.BOOL()
    if not kernel.IsProcessInJob(kernel.GetCurrentProcess(), None, ctypes.byref(inside)):
        return {"job_membership_error": ctypes.get_last_error()}
    result = {"in_job": bool(inside.value)}
    if inside.value:
        limits = ExtendedLimits()
        kernel.QueryInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                     wt.DWORD, ctypes.POINTER(wt.DWORD)]
        kernel.QueryInformationJobObject.restype = wt.BOOL
        if kernel.QueryInformationJobObject(None, 9, ctypes.byref(limits), ctypes.sizeof(limits), None):
            flags = limits.basic.flags
            result.update(job_limit_flags=flags, breakaway_ok=bool(flags & 0x800),
                          silent_breakaway_ok=bool(flags & 0x1000), kill_on_close=bool(flags & 0x2000))
        else:
            result["job_query_error"] = ctypes.get_last_error()
    return result


@contextmanager
def restrictive_job(process):
    """Own only this gated test child; never alter the caller's or runner's job."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
    kernel.CreateJobObjectW.restype = wt.HANDLE
    kernel.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
    kernel.SetInformationJobObject.restype = wt.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
    kernel.AssignProcessToJobObject.restype = wt.BOOL
    kernel.CloseHandle.argtypes = [wt.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # Kill only this test job's processes on close; forbid breakaway.
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel.AssignProcessToJobObject(job, wt.HANDLE(int(process._handle))):
            raise ctypes.WinError(ctypes.get_last_error())
        yield
    finally:
        kernel.CloseHandle(job)


@contextmanager
def trace_launch():
    original = windows.subprocess.Popen

    def observed(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        except OSError as error:
            # Never log command arguments, environment, exception text or credentials.
            evidence = job_context()
            evidence.update(winerror=getattr(error, "winerror", None), errno=error.errno,
                            creationflags=kwargs.get("creationflags", 0))
            print("P3_LAUNCH_DIAGNOSTIC=" + json.dumps(evidence, sort_keys=True), file=sys.stderr)
            raise

    windows.subprocess.Popen = observed
    try:
        yield
    finally:
        windows.subprocess.Popen = original
