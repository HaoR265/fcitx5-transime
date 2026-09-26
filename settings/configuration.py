"""Loss-minimizing Linux/Fcitx config editing for the settings window."""
from __future__ import annotations

import os
import fcntl
from pathlib import Path
import re
import tempfile

DEFAULT_VALUES = {"Enabled": False, "EnableHistory": False, "ContextAware": False,
                  "ContinuousComposition": True, "ShowModeHints": False}
DEFAULT_KEYS = {"TranslateKey": ["Control+Return"], "ClearHistoryKey": ["Control+Alt+BackSpace"],
                "ToggleKey": [], "ChineseCommitKey": ["space"],
                "RawCommitKey": ["Return", "KP_Enter"], "LiteralSpaceKey": ["Shift+space"]}
REQUIRED_KEYS = {"ChineseCommitKey", "RawCommitKey", "LiteralSpaceKey"}
MODIFIERS = ("Control", "Alt", "Shift", "Super")
MOD_ALIASES = {m.lower(): m for m in MODIFIERS} | {"ctrl": "Control"}
NAMED_KEYS = {k.lower(): k for k in ("space", "Return", "KP_Enter", "BackSpace", "Escape", "Tab",
              "Delete", "Insert", "Home", "End", "Left", "Right", "Up", "Down", "Page_Up", "Page_Down")}
NAMED_KEYS.update({"enter": "Return", "esc": "Escape", "backspace": "BackSpace"})
SECTION = re.compile(r"^\s*\[([^\]\r\n]+)\]\s*$")


class SettingsError(ValueError):
    """A configuration issue which the UI should display to the user."""


def normalize_key(value):
    if not isinstance(value, str) or not value.strip():
        raise SettingsError("快捷键不能为空；禁用可选功能快捷键时，请清空整个列表。")
    parts = [part.strip() for part in value.split("+")]
    modifiers = set()
    for part in parts[:-1]:
        modifier = MOD_ALIASES.get(part.lower())
        if modifier is None or modifier in modifiers:
            raise SettingsError(f"未知或重复的修饰键：{value}")
        modifiers.add(modifier)
    symbol = parts[-1]
    if symbol.lower() in NAMED_KEYS:
        symbol = NAMED_KEYS[symbol.lower()]
    elif re.fullmatch(r"[fF](?:[1-9]|[12][0-9]|3[0-5])", symbol):
        symbol = symbol.upper()
    elif re.fullmatch(r"[a-zA-Z0-9]", symbol):
        # Fcitx uppercases letters, preserving Shift with Ctrl/Alt/Super.
        if symbol.isalpha():
            symbol = symbol.upper()
            if modifiers == {"Shift"}:
                modifiers.discard("Shift")
    else:
        raise SettingsError(f"不支持的按键名称：{value}。请使用字母、数字、功能键或标准导航键名称。")
    if symbol == "space" and "Control" in modifiers:
        raise SettingsError("Control+space 保留给输入模式切换，不能绑定其他功能。")
    if symbol not in {"space", "Return", "KP_Enter"} and not re.fullmatch(r"F(?:[1-9]|[12][0-9]|3[0-5])", symbol):
        if not modifiers.intersection({"Control", "Alt", "Super"}):
            raise SettingsError(f"{value} 会拦截普通输入或编辑，请增加 Control、Alt 或 Super 等修饰键。")
    return "+".join([m for m in MODIFIERS if m in modifiers] + [symbol])


def _lines(raw):
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise SettingsError("配置文件必须为 UTF-8。") from exc
    return text.splitlines(keepends=True)


def _records(lines):
    section = ""
    for index, line in enumerate(lines):
        match = SECTION.match(line.rstrip("\r\n"))
        if match:
            section = match.group(1)
            yield index, section, None, None
        elif "=" in line and not line.lstrip().startswith(("#", ";")):
            key, value = line.split("=", 1)
            yield index, section, key.strip(), value.strip()


class SettingsConfig:
    def __init__(self, config_file: Path):
        self.path = Path(config_file)
        self.backup_path = None
        self._load()

    def _read(self):
        if self.path.is_symlink():
            raise SettingsError("拒绝覆盖符号链接配置文件。")
        try:
            return self.path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise SettingsError(f"无法读取配置：{exc}") from exc

    def _load(self):
        self._original = self._read()
        self._lines = _lines(self._original or b"")
        self.values = dict(DEFAULT_VALUES)
        self.keylists = {name: list(keys) for name, keys in DEFAULT_KEYS.items()}
        found_sections = set()
        entries = {name: {} for name in DEFAULT_KEYS}
        for _, section, key, value in _records(self._lines):
            if section in DEFAULT_KEYS:
                found_sections.add(section)
                if key is not None and key.isdecimal():
                    entries[section][int(key)] = value
            elif section == "" and key in DEFAULT_VALUES:
                if value not in ("True", "False"):
                    raise SettingsError(f"配置项 {key} 必须为 True 或 False。")
                self.values[key] = value == "True"
        for section in found_sections:
            self.keylists[section] = [value for _, value in sorted(entries[section].items())]

    def save(self, values, keylists):
        if not isinstance(values, dict) or not isinstance(keylists, dict):
            raise SettingsError("设置与快捷键必须为字典。")
        if set(values) - DEFAULT_VALUES.keys() or set(keylists) - DEFAULT_KEYS.keys():
            raise SettingsError("存在未知的设置项或快捷键组。")
        combined_values = {**self.values, **values}
        if any(type(value) is not bool for value in combined_values.values()):
            raise SettingsError("开关值必须为布尔值。")
        combined_keys = {**self.keylists, **keylists}
        normalized = {}
        used = {}
        for name, keys in combined_keys.items():
            if not isinstance(keys, (list, tuple)) or len(keys) > 32:
                raise SettingsError(f"{name} 需要一个最多含 32 项的快捷键列表。")
            if name in REQUIRED_KEYS and not keys:
                raise SettingsError(f"{name} 必须保留至少一个快捷键。")
            normalized[name] = []
            for key in keys:
                canonical = normalize_key(key)
                if canonical in used:
                    raise SettingsError(f"快捷键 {canonical} 重复用于 {used[canonical]} 与 {name}。")
                used[canonical] = name
                normalized[name].append(canonical)
        if self._read() != self._original:
            raise SettingsError("配置已被其他程序修改。请重新打开设置后再保存，避免覆盖外部修改。")

        replacements = {}
        known_roots = set()
        existing_sections = set()
        section_headers = {}
        for index, section, key, _ in _records(self._lines):
            if section == "" and key in combined_values:
                replacements[index] = f"{key}={'True' if combined_values[key] else 'False'}\n"
                known_roots.add(key)
            if section in normalized:
                existing_sections.add(section)
                if key is None:
                    section_headers.setdefault(section, index)
                elif key.isdecimal():
                    replacements[index] = ""
        first_section = next((i for i, line in enumerate(self._lines) if SECTION.match(line.rstrip("\r\n"))), len(self._lines))
        new_roots = "".join(f"{name}={'True' if value else 'False'}\n" for name, value in combined_values.items() if name not in known_roots)
        output = []
        for index in range(len(self._lines) + 1):
            if index == first_section:
                if output and not output[-1].endswith("\n"):
                    output.append("\n")
                output.append(new_roots)
            if index == len(self._lines):
                break
            output.append(replacements.get(index, self._lines[index]))
            for section, header_index in section_headers.items():
                if index == header_index:
                    if output[-1] and not output[-1].endswith("\n"):
                        output.append("\n")
                    output.extend(f"{n}={key}\n" for n, key in enumerate(normalized[section]))
        for section in normalized:
            if section in existing_sections:
                continue
            output.append("\n[" + section + "]\n")
            output.extend(f"{n}={key}\n" for n, key in enumerate(normalized[section]))
        rendered = "".join(output).encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup = None
        temporary = None
        try:
            if self._original is not None:
                descriptor, backup_name = tempfile.mkstemp(prefix=self.path.name + ".backup-", dir=self.path.parent)
                backup = Path(backup_name)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(self._original)
                    stream.flush()
                    os.fsync(stream.fileno())
            descriptor, temporary_name = tempfile.mkstemp(prefix="." + self.path.name + "-", dir=self.path.parent)
            temporary = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(rendered)
                stream.flush()
                os.fsync(stream.fileno())
            # Lock the stable parent inode, not the file replaced below. This
            # serializes our settings writers without leaving a lock file.
            # Uncooperative external editors can still race an atomic replace;
            # content checks detect observed edits, not a filesystem CAS.
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                fcntl.flock(directory, fcntl.LOCK_EX)
                if self._read() != self._original:
                    raise SettingsError("保存期间配置发生外部修改；未覆盖，请重新打开设置。")
                os.replace(temporary, self.path)
                self.backup_path = backup
                self._load()
            finally:
                os.close(directory)
        except OSError as exc:
            raise SettingsError(f"保存配置失败：{exc}") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return {"path": str(self.path), "backup_path": str(backup) if backup else None, "reload_required": True}
