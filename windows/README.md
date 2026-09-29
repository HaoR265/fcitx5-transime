# Windows native input: validation boundary

The Linux Fcitx 5 add-on in this repository is not a Windows input method.
Installing Weasel or PIME alone does not enable TransIME on Windows.

## Verified VM prerequisites (2026-09-26 to 2026-09-29)

In an isolated Windows 11 VM, we installed official Weasel 0.17.4 and the
official, signed PIME 1.3.0 stable release. PIME's optional Rime component
registered a native TSF input profile. Its Python implementation is at
`PIME/python/input_methods/rime/rime_ime.py` and writes Rime user data under
`%APPDATA%/PIME/Rime`. These are third-party components, not bundled here.

PIME exposes a Python `TextService` with `filterKeyDown`, `onKeyDown`,
`setCompositionString`, `setCandidateList`, and `setCommitString`. The
`pime/` directory now contains an **experimental TSF service** using that API.
It inherits PIME's installed Rime engine, reads the current Rime commit
preview, asks the existing framed TransIME worker on a background thread,
and accepts Ctrl+Enter only when a response still matches that preview.
Ordinary Rime input remains available without the worker.

This is a narrow prototype, **not full Windows compatibility**. It does not yet
preserve the project's ordered mixed-language paragraph draft or implement
its full key behavior. It was syntax-checked and exercised against PIME's
Rime service and a synthetic worker in the Win11 VM. In a clean Windows 10
22H2 VM, the adapter also loaded the actual offline CTranslate2 model and
committed a translation in Notepad: typing `nihao` showed Rime's `你好`
candidate, and Ctrl+Enter inserted `Hello.`. This verifies that specific
end-to-end path; it does not validate other keys, mixed paragraphs, or a
second application.

On 2026-09-29 the same Windows 10 VM also passed a Microsoft double-pinyin
route: with Rime's `double_pinyin_mspy` schema selected, `nihk` produced the
`你好` candidate and Ctrl+Enter inserted `Hello.` in Notepad. The configured
real-worker service test passed with the same schema and key sequence. The
schema came from [rime/rime-double-pinyin](https://github.com/rime/rime-double-pinyin)
at commit `01a13287cbd27819be1c34fa1ddc1b3643d5001b` and was installed only
in the disposable VM's Rime user directory. It is GPL-3.0 licensed and is not
included in this repository. This establishes one double-pinyin scheme on one
Windows application, not general double-pinyin or Linux Rime support.

On 2026-09-30, a separate Windows 11 VM passed the configured actual-worker
service test with `luna_pinyin`: Rime supplied a Chinese preview and the
offline model returned `Hello.`. This is **not** a real-application pass. The
Notepad input-profile selection could not be confirmed, so no Win11 Notepad
translation result is claimed. The VM also exposed a deployment prerequisite:
its older system `MSVCP140.dll` caused the model process to crash with
`0xc0000005`. A newer Microsoft-signed runtime DLL, scoped to that VM's Python
directory, made the model work. No Microsoft DLL is distributed here; a proper
Windows package must declare and verify its supported Visual C++ runtime.

## Prototype setup inside a disposable Windows VM

Install official PIME with its Rime component first. Copy `pime/ime.json`,
`pime/transime_ime.py`, and `pime/worker_bridge.py` into
`C:\Program Files (x86)\PIME\python\input_methods\transime_rime\`.
Do **not** copy `test_adapter.py`, `test_adapter_actual.py`, or a fake worker into
a user installation.
Create `%APPDATA%\TransIME\windows.json` with a local, explicit worker
command, for example:

```json
{
  "worker_command": [
    "C:\\Path\\To\\Python311\\python.exe",
    "C:\\Path\\To\\TransIME\\worker\\transime_worker.py",
    "--model-dir", "C:\\Path\\To\\OfflineModel",
    "--threads", "1"
  ]
}
```

The worker and model must already exist locally. No network fallback or model
download is performed by the adapter. Re-register PIME's 64-bit and 32-bit
`PIMETextService.dll` as administrator after adding the metadata, then restart
PIMELauncher and select `TransIME (experimental)` in Windows input profiles.
This procedure changes only the VM; it is not a supported installer.

## Required before labeling Windows supported

1. Implement a TSF adapter with a complete ordered mixed-language paragraph
   draft. Punctuation must stay in the draft. Shift/letters, Backspace,
   candidate selection, arrows, Space, Enter, Ctrl+Enter and Esc must follow
   the same documented TransIME behavior as the Linux adapter.
2. Connect the offline worker without blocking the TSF event loop. Copy
   candidate, paragraph, focus, and configuration versions into every request;
   discard stale responses and fail closed without losing the draft.
3. Keep personal Rime/user dictionaries outside the source tree. Supply an
   isolated settings and import path, rather than copying a user's profile.
4. Package and uninstall the adapter without replacing the Windows built-in
   input method or silently changing the user's default input profile.
5. Test in Windows 10 and Windows 11 VMs in at least Notepad and a second
   real text application, including translation with an actual local model.

Until those checks pass, Windows remains experimental rather than a supported
product installation. The Windows 10 Notepad result establishes one working
route through Rime, PIME, the offline worker, and a real text application.
