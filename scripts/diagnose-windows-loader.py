"""Read PE imports and resolve system DLL entries; never launch the inspected app.

This is failure evidence, not a substitute for a successful native startup test.
pefile is already installed by the Windows PyInstaller build.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import sys

import pefile


def inspect(executable: Path) -> dict:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LoadLibraryExW.argtypes = [wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD]
    kernel.LoadLibraryExW.restype = wintypes.HMODULE
    kernel.GetProcAddress.argtypes = [wintypes.HMODULE, ctypes.c_void_p]
    kernel.GetProcAddress.restype = ctypes.c_void_p
    kernel.GetModuleFileNameW.argtypes = [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
    kernel.GetModuleFileNameW.restype = wintypes.DWORD
    kernel.GetSystemDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    kernel.FreeLibrary.argtypes = [wintypes.HMODULE]

    class ActivationContext(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.ULONG), ("dwFlags", wintypes.DWORD),
                    ("lpSource", wintypes.LPCWSTR), ("wProcessorArchitecture", wintypes.WORD),
                    ("wLangId", wintypes.WORD), ("lpAssemblyDirectory", wintypes.LPCWSTR),
                    ("lpResourceName", ctypes.c_void_p), ("lpApplicationName", wintypes.LPCWSTR),
                    ("hModule", wintypes.HMODULE)]

    kernel.CreateActCtxW.argtypes = [ctypes.POINTER(ActivationContext)]
    kernel.CreateActCtxW.restype = wintypes.HANDLE
    kernel.ActivateActCtx.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_size_t)]
    kernel.DeactivateActCtx.argtypes = [wintypes.DWORD, ctypes.c_size_t]
    kernel.ReleaseActCtx.argtypes = [wintypes.HANDLE]
    system = ctypes.create_unicode_buffer(32768)
    if not kernel.GetSystemDirectoryW(system, len(system)):
        raise ctypes.WinError(ctypes.get_last_error())
    report = {"executable": str(executable), "manifest": None, "dlls": []}
    with pefile.PE(str(executable), fast_load=True) as pe:
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY[entry] for entry in
                                  ("IMAGE_DIRECTORY_ENTRY_IMPORT", "IMAGE_DIRECTORY_ENTRY_RESOURCE")])
        resources = pe.DIRECTORY_ENTRY_RESOURCE.entries if hasattr(pe, "DIRECTORY_ENTRY_RESOURCE") else []
        for entry in resources:
            if entry.id == 24:  # RT_MANIFEST, process manifest resource ID 1.
                for resource in entry.directory.entries:
                    if resource.id == 1:
                        data = resource.directory.entries[0].data.struct
                        report["manifest"] = pe.get_data(data.OffsetToData, data.Size).decode("utf-8", errors="replace")
        context = ActivationContext(cbSize=ctypes.sizeof(ActivationContext), dwFlags=8,
                                    lpSource=str(executable), lpResourceName=1)
        handle = kernel.CreateActCtxW(ctypes.byref(context))
        cookie = ctypes.c_size_t()
        active = handle != ctypes.c_void_p(-1).value
        if active:
            active = bool(kernel.ActivateActCtx(handle, ctypes.byref(cookie)))
        report["activation_context"] = "active" if active else f"unavailable:{ctypes.get_last_error()}"
        try:
            for dependency in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []):
                name = dependency.dll.decode("ascii")
                if Path(name).name != name:
                    raise ValueError("Expected a system DLL name")
                item = {"name": name, "missing_entries": []}
                report["dlls"].append(item)
                # Python itself requests Common Controls v6. Without an EXE manifest,
                # inspect System32 exports directly so Python cannot hide that difference.
                if name.lower() == "comctl32.dll" and not active:
                    library = Path(system.value) / name
                    item.update(path=str(library), inspection="System32 exports without an executable activation context")
                    with pefile.PE(str(library)) as dll:
                        symbols = dll.DIRECTORY_ENTRY_EXPORT.symbols
                        exports = {entry.name for entry in symbols} | {entry.ordinal for entry in symbols}
                        item["missing_entries"] = [entry.name.decode("ascii") if entry.name else entry.ordinal
                                                   for entry in dependency.imports if (entry.name or entry.ordinal) not in exports]
                    continue
                module = kernel.LoadLibraryExW(name, None, 0x800)  # LOAD_LIBRARY_SEARCH_SYSTEM32
                if not module:
                    item["load_error"] = ctypes.get_last_error()
                    continue
                try:
                    location = ctypes.create_unicode_buffer(32768)
                    kernel.GetModuleFileNameW(module, location, len(location))
                    item.update(path=location.value, inspection="GetProcAddress under executable activation context")
                    for entry in dependency.imports:
                        address = ctypes.cast(ctypes.c_char_p(entry.name), ctypes.c_void_p) if entry.name else ctypes.c_void_p(entry.ordinal)
                        if not kernel.GetProcAddress(module, address):
                            item["missing_entries"].append(entry.name.decode("ascii") if entry.name else entry.ordinal)
                finally:
                    kernel.FreeLibrary(module)
        finally:
            if active:
                kernel.DeactivateActCtx(0, cookie)
            if handle != ctypes.c_void_p(-1).value:
                kernel.ReleaseActCtx(handle)
    return report


if __name__ == "__main__":
    if sys.platform != "win32":
        raise SystemExit("Windows only")
    output = Path(sys.argv[1])
    # A fresh interpreter per EXE avoids a previous inspection's loaded SxS DLLs.
    if len(sys.argv) == 4 and sys.argv[2] == "--one":
        print(json.dumps(inspect(Path(sys.argv[3]).resolve()), ensure_ascii=True))
    else:
        import subprocess
        reports = []
        for path in sys.argv[2:]:
            result = subprocess.run([sys.executable, __file__, str(output), "--one", path],
                                    check=True, capture_output=True, text=True, timeout=30)
            reports.append(json.loads(result.stdout))
        output.write_text(json.dumps(reports, indent=2) + "\n", encoding="utf-8")
        for report in reports:
            print(json.dumps({"executable": report["executable"], "activation_context": report["activation_context"],
                              "manifest_present": report["manifest"] is not None,
                              "unresolved": [dll for dll in report["dlls"] if dll["missing_entries"] or "load_error" in dll]}), flush=True)
