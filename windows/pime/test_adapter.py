"""Run only inside a disposable Windows VM with PIME's Python backend installed.

Requires an isolated %APPDATA%/TransIME/windows.json pointing to a synthetic
framed worker. This is a protocol/TSF-service-level test, not a real-app test.
"""

import time

from input_methods.transime_rime.transime_ime import TransimeTextService
from keycodes import VK_CONTROL, VK_RETURN
from textService import KeyEvent


class Client:
    isWindows8Above = False


def key(code, control=False, char_code=None):
    states = [0] * 256
    if control:
        states[VK_CONTROL] = 128
    return KeyEvent({"charCode": code if char_code is None else char_code,
                     "keyCode": code, "repeatCount": 1,
                     "scanCode": 0, "isExtended": False, "keyStates": states})


service = TransimeTextService(Client())
service.onActivate()
try:
    for character in "nihao":
        event = key(ord(character.upper()), char_code=ord(character))
        service.filterKeyDown(event)
        service.onKeyDown(event)
    print("preview:", repr(service.source))
    assert service.source, "Rime preview did not become available"
    assert any("\u4e00" <= character <= "\u9fff" for character in service.source), \
        "preview is not Chinese candidate text"
    source = service.source
    service.bridge.invalidate()
    event = key(VK_RETURN, control=True)
    assert service.filterKeyDown(event) is True
    assert service.onKeyDown(event) is True
    assert service.commitString == "", "unready shortcut committed text"
    assert service.source == source, "unready shortcut lost the composition"
    service.bridge.request(source)
    expected = "translated " + service.source
    deadline = time.monotonic() + 15
    while service.bridge.get_ready(service.source) is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert service.bridge.get_ready(service.source) == expected, "worker response missing"
    assert service.filterKeyDown(event) is True
    assert service.onKeyDown(event) is True
    assert service.commitString == expected, "Ctrl+Enter did not commit current translation"
    assert service.source == "", "committed source was not invalidated"
    print("PASS: Rime preview -> framed worker -> Ctrl+Enter commit")
finally:
    service.onDeactivate()
    if service.bridge:
        service.bridge.close()
