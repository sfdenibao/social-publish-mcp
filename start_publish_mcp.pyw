"""Console-free scheduled-task supervisor for the existing local MCP service."""
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import subprocess
import sys
import traceback
from datetime import datetime

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / 'runtime'
RUNTIME.mkdir(exist_ok=True)
STAMP = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
FLAGS = subprocess.CREATE_NO_WINDOW


def existing_service():
    # Ownership matters: an unrelated listener must not count as our service.
    query = """
$ErrorActionPreference='Stop'
$listeners=@(Get-NetTCPConnection -LocalPort 5410 -State Listen -ErrorAction SilentlyContinue)
@($listeners | ForEach-Object {
  $owner=Get-CimInstance Win32_Process -Filter ('ProcessId='+$_.OwningProcess)
  [PSCustomObject]@{pid=$owner.ProcessId;command=$owner.CommandLine}
}) | ConvertTo-Json -Compress
"""
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', query],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        creationflags=FLAGS, check=True, timeout=30)
    rows = json.loads(result.stdout.strip() or '[]')
    if isinstance(rows, dict): rows = [rows]
    for row in rows:
        if 'publish_mcp.py' not in (row.get('command') or ''):
            raise RuntimeError('Port 5410 is occupied by another application')
    return int(rows[0]['pid']) if rows else None


def wait_existing(pid):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x00100000 | 0x1000, False, pid)
    if not handle: raise ctypes.WinError(ctypes.get_last_error())
    try:
        if kernel.WaitForSingleObject(handle, 0xFFFFFFFF) != 0:
            raise ctypes.WinError(ctypes.get_last_error())
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return code.value
    finally:
        kernel.CloseHandle(handle)


def main():
    pid = existing_service()
    if pid:
        print(f'Adopting existing MCP service pid={pid}', flush=True)
        return wait_existing(pid)
    with (RUNTIME / f'mcp-autostart-{STAMP}.out.log').open('ab') as out, \
         (RUNTIME / f'mcp-autostart-{STAMP}.err.log').open('ab') as err:
        child = subprocess.Popen([str(ROOT / '.venv/Scripts/python.exe'), '-u', 'publish_mcp.py'],
            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=out, stderr=err, creationflags=FLAGS)
        (RUNTIME / 'mcp.pid').write_text(str(child.pid), encoding='ascii')
        print(f'Started console-free MCP service pid={child.pid}', flush=True)
        return child.wait()


if __name__ == '__main__':
    with (RUNTIME / f'mcp-supervisor-{STAMP}.log').open('a', encoding='utf-8', buffering=1) as log:
        sys.stdout = sys.stderr = log
        try:
            raise SystemExit(main())
        except Exception:
            traceback.print_exc()
            raise SystemExit(1)
