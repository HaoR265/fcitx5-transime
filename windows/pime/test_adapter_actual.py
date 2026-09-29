"""Disposable-VM service test using the configured real TransIME worker.

Run this with PIME's embedded Python as the interactive test user. This checks
the Rime preview, asynchronous worker bridge, stale-result protection, and the
Ctrl+Enter commit path. It is not a substitute for a real-application test.
"""

import os
import time
from pathlib import Path

from input_methods.rime.rime_ime import rime
from input_methods.transime_rime.transime_ime import TransimeTextService
from keycodes import VK_CONTROL, VK_RETURN
from textService import KeyEvent


class Client:
    isWindows8Above = True


def key(code, control=False, char_code=None):
    states = [0] * 256
    if control:
        states[VK_CONTROL] = 128
    return KeyEvent({"charCode": code if char_code is None else char_code,
                     "keyCode": code, "repeatCount": 1,
                     "scanCode": 0, "isExtended": False,
                     "keyStates": states})


service = TransimeTextService(Client())
service.onActivate()
try:
    assert service.bridge is not None, "real worker configuration was not loaded"
    schema = os.environ.get("TRANSIME_TEST_SCHEMA", "luna_pinyin")
    inputs = {"luna_pinyin": "nihao", "double_pinyin_mspy": "nihk"}
    assert schema in inputs, "unsupported test schema"
    assert rime.select_schema(service.session_id, schema.encode("ascii")), \
        "selected schema was not deployed"
    for character in inputs[schema]:
        event = key(ord(character.upper()), char_code=ord(character))
        service.filterKeyDown(event)
        service.onKeyDown(event)
    assert service.source, "Rime preview did not become available"
    assert any("\u4e00" <= character <= "\u9fff" for character in service.source), \
        "preview is not Chinese candidate text"

    source = service.source
    service.bridge.invalidate()
    shortcut = key(VK_RETURN, control=True)
    assert service.filterKeyDown(shortcut) is True
    assert service.onKeyDown(shortcut) is True
    assert service.commitString == "", "unready shortcut committed text"
    assert service.source == source, "unready shortcut lost the composition"

    service.bridge.request(source)
    deadline = time.monotonic() + 90
    translated = None
    while time.monotonic() < deadline:
        translated = service.bridge.get_ready(source)
        if translated is not None:
            break
        time.sleep(0.05)
    assert translated and translated != source, "actual worker response missing"

    assert service.filterKeyDown(shortcut) is True
    assert service.onKeyDown(shortcut) is True
    assert service.commitString == translated, "Ctrl+Enter did not commit translation"
    assert service.source == "", "committed source was not invalidated"
    result = "PASS actual adapter [{}]: {!r} -> {!r}".format(schema, source, translated)
    print(result)
    Path(__file__).with_suffix(".result.txt").write_text(result + "\n", encoding="utf-8")
finally:
    service.onDeactivate()
    if service.bridge:
        service.bridge.close()
