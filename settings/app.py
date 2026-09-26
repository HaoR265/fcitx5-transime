#!/usr/bin/env python3
"""Native settings window. No IM restart or model download on launch."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QThread, Signal, Qt, QTimer
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QHBoxLayout, QHeaderView, QKeySequenceEdit, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPushButton,
    QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)
from settings.configuration import SettingsConfig
from settings.dictionaries import DictionaryManager


class Job(QThread):
    result = Signal(object, str)

    def __init__(self, action, parent):
        super().__init__(parent)
        self.action = action

    def run(self):
        try:
            self.result.emit(self.action(), "")
        except Exception as exc:
            self.result.emit(None, str(exc))


class KeyEditor(QWidget):
    """Edit a list, rather than silently discarding secondary bindings."""
    def __init__(self, keys, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.text = QLineEdit("; ".join(keys))
        self.text.setPlaceholderText("不绑定；多个按键用分号分隔")
        self.text.setAccessibleName("快捷键列表")
        layout.addWidget(self.text)
        button = QPushButton("录制…")
        button.clicked.connect(self.record)
        layout.addWidget(button)

    def record(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("录制快捷键")
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("按下组合键；确定后替换此功能的按键列表。"))
        editor = QKeySequenceEdit(dialog)
        if hasattr(editor, "setMaximumSequenceLength"):
            editor.setMaximumSequenceLength(1)
        layout.addWidget(editor)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.Accepted:
            value = editor.keySequence().toString(QKeySequence.PortableText)
            value = value.replace("Ctrl+", "Control+").replace("Space", "space")
            self.text.setText(value)

    def keys(self):
        return [part.strip() for part in self.text.text().split(";") if part.strip()]


class SettingsWindow(QMainWindow):
    def __init__(self, config_home: Path, data_home: Path, session_actions=False):
        super().__init__()
        self.setWindowTitle("TransIME 设置")
        self.resize(800, 620)
        self.setMinimumSize(680, 520)
        self.config = SettingsConfig(Path(config_home) / "conf" / "transime.conf")
        self.dictionary = DictionaryManager(Path(data_home))
        self.job = None
        self.session_actions = session_actions
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        title = QLabel("TransIME 设置")
        font = title.font()
        font.setPointSize(font.pointSize() + 5)
        font.setBold(True)
        title.setFont(font)
        outer.addWidget(title)
        subtitle = QLabel("在这里调整输入体验。输入时不重复显示模式提示。")
        subtitle.setWordWrap(True)
        outer.addWidget(subtitle)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)
        self.flags = {}
        self.keys = {}
        self._input_tab()
        self._keys_tab()
        self._dictionary_tab()
        self._translation_tab()
        self.status = QLabel("更改将在保存后写入；不会自动重启输入法。")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        outer.addWidget(self.status)
        actions = QHBoxLayout()
        self.reload_button = QPushButton("重新加载输入法…")
        self.reload_button.clicked.connect(self.reload_input_method)
        self.reload_button.setEnabled(self.session_actions)
        actions.addWidget(self.reload_button)
        actions.addStretch()
        self.save_button = QPushButton("保存设置")
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self.save)
        actions.addWidget(self.save_button)
        outer.addLayout(actions)
        self.refresh_packs()

    def tab(self, name):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        self.tabs.addTab(widget, name)
        return layout

    @staticmethod
    def note(layout, text):
        label = QLabel(text)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(label)
        return label

    def flag(self, layout, key, label):
        check = QCheckBox(label)
        check.setChecked(str(self.config.values.get(key, "False")).lower() == "true")
        self.flags[key] = check
        layout.addWidget(check)

    def _input_tab(self):
        layout = self.tab("输入")
        self.flag(layout, "Enabled", "启用 TransIME 翻译")
        form = QFormLayout()
        self.mode = QComboBox()
        self.mode.addItems(["整段编辑（中文、英文与标点保留在同一草稿）", "原生拼音（按原有方式选择并提交候选）"])
        self.mode.setCurrentIndex(0 if str(self.config.values.get("ContinuousComposition", "True")).lower() == "true" else 1)
        form.addRow("输入方式", self.mode)
        layout.addLayout(form)
        self.flag(layout, "ShowModeHints", "在输入面板中显示模式与操作提示")
        self.note(layout, "关闭提示不会隐藏正在输入的文字或候选。当前输入方式只在设置中查看。")
        self.note(layout, "整段编辑中的提交方式可在“快捷键”中修改。此版本连接 Linux 的 Fcitx5 全拼；Windows 原生输入接口尚未实现。")
        layout.addStretch()

    def _keys_tab(self):
        layout = self.tab("快捷键")
        self.note(layout, "可直接录制组合键，也可编辑按键名称。多个绑定用分号分隔；冲突会在保存前提示。")
        form = QFormLayout()
        names = [("TranslateKey", "提交英文翻译"), ("ChineseCommitKey", "提交中文／混合草稿"),
                 ("RawCommitKey", "提交原始输入"), ("LiteralSpaceKey", "插入空格"),
                 ("ToggleKey", "开关翻译功能"), ("ClearHistoryKey", "清除翻译上下文")]
        for key, label in names:
            editor = KeyEditor(self.config.keylists[key])
            editor.text.setAccessibleName(label)
            self.keys[key] = editor
            form.addRow(label, editor)
        layout.addLayout(form)
        self.note(layout, "整段编辑的自定义按键需要新版 TransIME 拼音桥接组件。旧版组件不会因保存设置而自动升级。Ctrl+Space 为段内切换保留。")
        layout.addStretch()

    def _dictionary_tab(self):
        layout = self.tab("词库")
        self.note(layout, "只管理你主动导入的独立词包，不读取个人学习词库。支持 UTF-8 的 TXT、CSV、TSV。")
        self.pack_list = QListWidget()
        self.pack_list.setAccessibleName("已导入词包")
        layout.addWidget(self.pack_list, 1)
        self.empty = QLabel("还没有导入词包。选择自己的文件，先预览，再决定是否导入。")
        self.empty.setWordWrap(True)
        layout.addWidget(self.empty)
        actions = QHBoxLayout()
        self.dictionary_buttons = []
        for name, callback in [("导入词包…", self.choose_dictionary), ("导出…", self.export_dictionary), ("移除…", self.remove_dictionary)]:
            button = QPushButton(name)
            button.clicked.connect(callback)
            self.dictionary_buttons.append(button)
            actions.addWidget(button)
        actions.addStretch()
        self.load_packs_button = QPushButton("加载词包…")
        self.load_packs_button.setEnabled(self.session_actions)
        self.load_packs_button.clicked.connect(self.load_dictionary_packs)
        actions.addWidget(self.load_packs_button)
        layout.addLayout(actions)
        self.note(layout, "CSV／TSV 表头：word、pinyin；可选 score、english。拼音示例：hou'liang'zi。英文释义目前只保存与导出，不参与翻译。")

    def _translation_tab(self):
        layout = self.tab("翻译")
        self.note(layout, "当前引擎：本地 OPUS-MT 中译英。它不是大语言模型；上下文仅用于有限的术语一致性处理。")
        self.flag(layout, "EnableHistory", "允许翻译引擎使用本次会话的历史")
        self.flag(layout, "ContextAware", "使用历史辅助术语一致性")
        self.note(layout, "这些设置不会上传文字。开启历史不代表模型能理解整段对话，也不保证专业术语或标识符翻译正确。")
        self.note(layout, "更大模型仅用于独立实验，目前未达到替换标准。实验验收通过前，不会替换当前模型，也不会在这里提供尚不可用的模型选项。")
        layout.addStretch()

    def save(self):
        values = {key: widget.isChecked() for key, widget in self.flags.items()}
        values["ContinuousComposition"] = self.mode.currentIndex() == 0
        try:
            self.config.save(values, {key: widget.keys() for key, widget in self.keys.items()})
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "未保存", str(exc))
            return False
        self.status.setText("设置已保存。请在结束当前输入后重新加载输入法；新版控制项需要对应的新版插件与桥接组件。")
        return True

    def reload_input_method(self):
        if not self.session_actions:
            return
        if QMessageBox.question(self, "重新加载输入法", "重新加载可能中断尚未提交的草稿。请先结束输入。现在重新加载吗？") != QMessageBox.Yes:
            return
        executable = shutil.which("gdbus")
        if not executable:
            QMessageBox.warning(self, "无法重新加载", "找不到 gdbus。设置仍已保存在磁盘，下次启动输入法时读取。")
            return
        def action():
            state = subprocess.run([executable, "call", "--session", "--dest", "org.freedesktop.DBus",
                "--object-path", "/org/freedesktop/DBus", "--method", "org.freedesktop.DBus.NameHasOwner",
                "org.fcitx.Fcitx5"], capture_output=True, text=True, timeout=5)
            if state.returncode or "true" not in state.stdout:
                raise ValueError("当前桌面会话没有可连接的 Fcitx5。下次启动时会读取设置。")
            result = subprocess.run([executable, "call", "--session", "--dest", "org.fcitx.Fcitx5",
                "--object-path", "/controller", "--method", "org.fcitx.Fcitx.Controller1.ReloadAddonConfig",
                "transime"], capture_output=True, text=True, timeout=5)
            if result.returncode:
                raise ValueError("重新加载请求失败；设置文件仍然保留。")
            return "TransIME 设置重载请求已完成，请在输入框中确认。词包请另用“加载词包”。"
        self.run_job(action, lambda result: self.status.setText(result))

    def load_dictionary_packs(self):
        if not self.session_actions:
            return
        if QMessageBox.question(self, "加载词包", "请先结束当前输入。现在让拼音引擎重新读取独立词包吗？\n这不会重启输入法，也不会修改个人学习词库。") != QMessageBox.Yes:
            return
        executable = shutil.which("gdbus")
        if not executable:
            QMessageBox.warning(self, "尚未加载", "缺少 gdbus。词包已保存在磁盘；结束输入后退出并重新打开输入法即可加载。普通“重新加载设置”不足以读取新词包。")
            return
        def action():
            owner = subprocess.run([executable, "call", "--session", "--dest", "org.freedesktop.DBus",
                "--object-path", "/org/freedesktop/DBus", "--method", "org.freedesktop.DBus.NameHasOwner",
                "org.fcitx.Fcitx5"], capture_output=True, text=True, timeout=5)
            if owner.returncode or "true" not in owner.stdout:
                raise ValueError("当前会话没有运行 Fcitx5。词包已保存；不会为刷新词库自动启动另一个输入法进程。")
            result = subprocess.run([executable, "call", "--session", "--dest", "org.fcitx.Fcitx5",
                "--object-path", "/controller", "--method", "org.fcitx.Fcitx.Controller1.SetConfig",
                "fcitx://config/addon/pinyin/dictmanager", "<@a{sv} {}>"], capture_output=True, text=True, timeout=10)
            if result.returncode:
                raise ValueError("词库刷新请求失败。已保存的词包不会丢失；结束输入后退出并重新打开输入法，或检查当前会话的 Fcitx5 是否运行。")
            return "词库刷新请求已完成。请在输入框中输入词条，确认候选；个人学习词库未修改。"
        self.run_job(action, lambda result: self.status.setText(result))

    def run_job(self, action, callback):
        if self.job is not None:
            return
        self.job = Job(action, self)
        self.job_output = (None, "")
        self.job_callback = callback
        for button in self.dictionary_buttons + [self.save_button, self.reload_button, self.load_packs_button]:
            button.setEnabled(False)
        self.status.setText("正在处理…")
        self.job.result.connect(self.receive_job_result)
        self.job.finished.connect(self.job_finished)
        self.job.start()

    def job_result(self, result, error, callback):
        if error:
            self.status.setText("操作未完成。")
            QMessageBox.warning(self, "操作未完成", error)
        else:
            callback(result)

    def receive_job_result(self, result, error):
        self.job_output = (result, error)

    def job_finished(self):
        result, error = self.job_output
        callback = self.job_callback
        self.job.deleteLater()
        self.job = None
        for button in self.dictionary_buttons + [self.save_button, self.reload_button]:
            button.setEnabled(True)
        self.reload_button.setEnabled(self.session_actions)
        self.load_packs_button.setEnabled(self.session_actions)
        self.job_result(result, error, callback)

    def closeEvent(self, event):
        if self.job is not None:
            self.status.setText("请等待当前文件操作完成后再关闭窗口。")
            event.ignore()
        else:
            event.accept()

    def refresh_packs(self):
        self.pack_list.clear()
        try:
            packs = self.dictionary.list_packs()
        except ValueError as exc:
            self.empty.setText(str(exc))
            self.empty.show()
            return
        for pack in packs:
            label = f"{pack['name']}  ·  {pack['count']} 条" if pack["status"] == "ready" else "词包校验失败：" + pack["id"]
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, pack["id"])
            self.pack_list.addItem(item)
        self.empty.setVisible(not packs)

    def choose_dictionary(self):
        filename, _ = QFileDialog.getOpenFileName(self, "选择词包", "", "词包 (*.txt *.csv *.tsv)")
        if filename:
            self.run_job(lambda: self.dictionary.preview_file(filename), lambda report: QTimer.singleShot(0, lambda: self.preview_dictionary(filename, report)))

    def preview_dictionary(self, filename, report):
        dialog = QDialog(self)
        dialog.setWindowTitle("预览词包")
        dialog.resize(720, 460)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel(f"共 {report['count']} 条，显示前 100 条。确认后编译并校验，不会合并个人词库。"))
        name = QLineEdit(Path(filename).stem)
        form = QFormLayout()
        form.addRow("词包名称", name)
        layout.addLayout(form)
        entries = report["entries"][:100]
        table = QTableWidget(len(entries), 3)
        table.setHorizontalHeaderLabels(["词语", "拼音", "英文释义（仅保存）"])
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        for row, entry in enumerate(entries):
            for col, key in enumerate(("word", "pinyin", "english")):
                table.setItem(row, col, QTableWidgetItem(str(entry[key])))
        layout.addWidget(table, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("导入词包")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.Accepted:
            pack_name = name.text()
            # The preview job must be fully released before starting compilation.
            QTimer.singleShot(0, lambda: self.run_job(lambda: self.dictionary.import_file(filename, pack_name, expected_sha256=report["sha256"]), self.dictionary_changed))
        else:
            self.status.setText("已取消导入，词库没有变化。")

    def dictionary_changed(self, report):
        self.refresh_packs()
        self.status.setText("词包已更新。请结束输入后点击“加载词包”；普通重新加载设置不会读取新词包。个人学习词库未读取或修改。")

    def selected_pack(self):
        item = self.pack_list.currentItem()
        if item is None:
            self.status.setText("请先选择一个已导入词包。")
            return None
        return item.data(Qt.UserRole)

    def export_dictionary(self):
        pack_id = self.selected_pack()
        if pack_id:
            filename, _ = QFileDialog.getSaveFileName(self, "导出为新文件（不覆盖已有文件）", "词包.tsv", "TSV (*.tsv)")
            if filename:
                self.run_job(lambda: self.dictionary.export_pack(pack_id, filename), lambda result: self.status.setText("已导出词包：" + result["path"]))

    def remove_dictionary(self):
        pack_id = self.selected_pack()
        if pack_id and QMessageBox.question(self, "移除独立词包", "只移除选中的导入词包，个人学习词库不会改变。建议先导出备份。继续吗？") == QMessageBox.Yes:
            self.run_job(lambda: self.dictionary.remove_pack(pack_id), self.dictionary_changed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-home", type=Path, default=Path(os.environ.get("FCITX_CONFIG_HOME", str(Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "fcitx5"))))
    parser.add_argument("--data-home", type=Path, default=Path(os.environ.get("FCITX_DATA_HOME", str(Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "fcitx5"))))
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("TransIME 设置")
    try:
        window = SettingsWindow(args.config_home, args.data_home,
            session_actions=(args.config_home == parser.get_default("config_home") and args.data_home == parser.get_default("data_home")))
    except (ValueError, OSError) as exc:
        QMessageBox.critical(None, "无法打开设置", str(exc))
        return 1
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
