"""Experimental native Windows TSF service via an existing PIME Rime install.

This first bridge supports ordinary Rime input plus an explicit, ready-only
Ctrl+Enter translation commit. It does not yet implement the Linux mixed draft.
"""

import json
import os

from keycodes import VK_CONTROL, VK_RETURN, VK_ESCAPE
from input_methods.rime.rime_ime import RimeTextService, RimeContext, rime
from .worker_bridge import WorkerBridge


def _configuration():
    path = os.path.join(os.environ["APPDATA"], "TransIME", "windows.json")
    try:
        # Windows PowerShell 5 writes a UTF-8 BOM by default. Accept it while
        # continuing to reject malformed JSON and invalid commands below.
        with open(path, encoding="utf-8-sig") as stream:
            data = json.load(stream)
    except (OSError, ValueError):
        return None
    return data.get("worker_command") if isinstance(data, dict) else None


class TransimeTextService(RimeTextService):
    def __init__(self, client):
        super().__init__(client)
        self.bridge = None
        self.source = ""
        self.translate_key_down = False
        command = _configuration()
        if command:
            try:
                self.bridge = WorkerBridge(command)
            except ValueError:
                pass

    def _preview(self):
        if not self.session_id:
            return ""
        context = RimeContext()
        if not rime.get_context(self.session_id, context):
            return ""
        try:
            raw = context.commit_text_preview
            return raw.decode("utf-8") if raw and context.composition.length else ""
        finally:
            rime.free_context(context)

    def _refresh(self):
        source = self._preview()
        if source == self.source:
            return
        self.source = source
        if self.bridge is not None:
            if source and len(source.encode("utf-8")) <= 4096:
                self.bridge.request(source)
            else:
                self.bridge.invalidate()

    def filterKeyDown(self, keyEvent):
        self.translate_key_down = (keyEvent.keyCode == VK_RETURN and
                                   keyEvent.isKeyDown(VK_CONTROL))
        if self.translate_key_down:
            return True
        return super().filterKeyDown(keyEvent)

    def onKeyDown(self, keyEvent):
        if self.translate_key_down:
            self._refresh()
            translated = self.bridge.get_ready(self.source) if self.bridge else None
            if translated:
                self.setCommitString(translated)
                rime.clear_composition(self.session_id)
                self.source = ""
                self.bridge.invalidate()
                self.setCompositionString("")
                self.setShowCandidates(False)
            return True
        result = super().onKeyDown(keyEvent)
        self._refresh()
        return result

    def filterKeyUp(self, keyEvent):
        if keyEvent.keyCode == VK_RETURN and self.translate_key_down:
            return True
        return super().filterKeyUp(keyEvent)

    def onKeyUp(self, keyEvent):
        if keyEvent.keyCode == VK_RETURN and self.translate_key_down:
            self.translate_key_down = False
            return True
        result = super().onKeyUp(keyEvent)
        self._refresh()
        return result

    def onDeactivate(self):
        self.source = ""
        if self.bridge is not None:
            self.bridge.invalidate()
        super().onDeactivate()

    def onCompositionTerminated(self, forced):
        self.source = ""
        if self.bridge is not None:
            self.bridge.invalidate()
        super().onCompositionTerminated(forced)
