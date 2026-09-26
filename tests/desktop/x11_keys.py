"""Physical function-key injection for the VM-only private X11 test harness.

Unlike xdotool keysym injection, this sends no inferred modifiers. It still
passes through the X server, focused toolkit widget and Fcitx frontend.
"""
import ctypes
import os
import subprocess


def function_key(name, expected_window):
    """Press/release F6 or F7 only after validating focus and level-zero map."""
    if (os.environ.get("TRANSIME_PRIVATE_X11") != "1"
            or subprocess.run(["systemd-detect-virt", "--quiet"]).returncode):
        raise RuntimeError("Physical test keys require the private VM harness")
    if name not in ("F6", "F7"):
        raise ValueError("Only F6/F7 are supported by this test helper")
    x11 = ctypes.CDLL("libX11.so.6")
    xtst = ctypes.CDLL("libXtst.so.6")
    pointer = ctypes.c_void_p
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XOpenDisplay.restype = pointer
    x11.XCloseDisplay.argtypes = [pointer]
    x11.XStringToKeysym.argtypes = [ctypes.c_char_p]
    x11.XStringToKeysym.restype = ctypes.c_ulong
    x11.XKeysymToKeycode.argtypes = [pointer, ctypes.c_ulong]
    x11.XKeysymToKeycode.restype = ctypes.c_ubyte
    x11.XkbKeycodeToKeysym.argtypes = [pointer, ctypes.c_ubyte, ctypes.c_int, ctypes.c_int]
    x11.XkbKeycodeToKeysym.restype = ctypes.c_ulong
    x11.XGetInputFocus.argtypes = [pointer, ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int)]
    x11.XQueryTree.argtypes = [pointer, ctypes.c_ulong,
                              ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
                              ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)), ctypes.POINTER(ctypes.c_uint)]
    x11.XQueryTree.restype = ctypes.c_int
    x11.XFree.argtypes = [pointer]
    x11.XSync.argtypes = [pointer, ctypes.c_int]
    xtst.XTestFakeKeyEvent.argtypes = [pointer, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
    xtst.XTestFakeKeyEvent.restype = ctypes.c_int
    display = x11.XOpenDisplay(None)
    if not display:
        raise RuntimeError("Cannot open the private X display")
    try:
        focus, revert = ctypes.c_ulong(), ctypes.c_int()
        x11.XGetInputFocus(display, ctypes.byref(focus), ctypes.byref(revert))
        # GTK may focus a child X window. Accept only this exact fixture's
        # ancestry; never permit PointerRoot/None or the X server root.
        target = int(expected_window)
        current = focus.value
        matched = False
        for _ in range(32):
            if current in (0, 1):
                break
            root, parent = ctypes.c_ulong(), ctypes.c_ulong()
            children, count = ctypes.POINTER(ctypes.c_ulong)(), ctypes.c_uint()
            ok = x11.XQueryTree(display, current, ctypes.byref(root), ctypes.byref(parent),
                               ctypes.byref(children), ctypes.byref(count))
            if children:
                x11.XFree(children)
            if not ok or current == root.value:
                break
            if current == target:
                matched = True
                break
            if parent.value == current:
                break
            current = parent.value
        if not matched:
            raise RuntimeError("Refusing test key: focus is not the owned fixture window")
        symbol = x11.XStringToKeysym(name.encode("ascii"))
        code = x11.XKeysymToKeycode(display, symbol)
        if not code or x11.XkbKeycodeToKeysym(display, code, 0, 0) != symbol:
            raise RuntimeError("Requested function key is not a level-zero mapping")
        pressed = xtst.XTestFakeKeyEvent(display, code, 1, 0)
        released = xtst.XTestFakeKeyEvent(display, code, 0, 0)
        x11.XSync(display, 0)
        if not pressed or not released:
            raise RuntimeError("XTest rejected function-key injection")
    finally:
        x11.XCloseDisplay(display)
