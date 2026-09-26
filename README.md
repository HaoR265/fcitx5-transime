# TransIME

本地中译英输入扩展 · Fcitx5 · 源码 Alpha

TransIME 在中文输入的候选阶段提供本地英文翻译，允许把中文、英文片段和标点保留在同一段草稿中，再由用户明确选择提交中文、原始输入或译文。设置、词包和快捷键通过独立窗口管理，默认不反复显示模式提示。

**这是开发者源码预览，不是开箱即用的安装包。** 当前输入接口面向 Linux / Fcitx5 原生全拼，需要匹配版本的拼音桥接。Windows、Rime、双拼与 Wayland 全面兼容尚未完成。请先在可丢弃虚拟机中试用，不替换日常输入法。

## 已实现的功能

- 段落内中英混输，标点不自动提交；空格提交当前中文/混合原文，Enter 提交原始输入，Ctrl+Enter 提交就绪的英文，Escape 取消。
- 独立设置窗口，可调整功能键、输入策略和模式提示。
- 用户自行导入、预览、编译、导出和删除独立词包；不附带个人词库或学习记录。
- 本地 OPUS-MT / CTranslate2 翻译工作进程；推理与输入主循环分离。
- 实验性 Qt6/X11 前端补丁，用于修复同一字段切窗后面板草稿丢失；必须单独构建、加载并启用，不能仅安装主插件。

词包中的英文释义目前不接入翻译模型。现有上下文功能是有限的术语辅助，不是通用对话理解。译文仍可能误译专业术语、数字或标识符，请核对后提交。

## 从源码开始

在 Linux **测试虚拟机**准备 CMake、C++17 编译器及 Python 3.11+。下列命令只构建状态核心并运行合成数据测试，不安装输入法、不下载模型、不重启桌面服务：

```sh
cmake -S . -B build/core -DCMAKE_BUILD_TYPE=Release
cmake --build build/core -j2
ctest --test-dir build/core --output-on-failure
python3 tools/run_public_checks.py
```

设置窗口另外需要 PySide6。在虚拟机中使用独立目录试运行：

```sh
python3 settings/app.py --config-home /var/tmp/transime-preview/config --data-home /var/tmp/transime-preview/data
```

它只显示设置，不会自动安装输入法引擎。这类显式隔离路径禁止向日常会话发送重载请求。

完整引擎构建与安装仍需精确匹配 Fcitx/LibIME/Pinyin SDK；本 Alpha **没有通过任意新机器的一键完整安装验收**。SDK 和打包脚本作为开发材料提供，其历史工作区布局要求见 [构建范围](docs/building.md)，不要直接在日常桌面运行激活脚本。

## 支持与验证

已验证的组合是 Kali 测试虚拟机、Fcitx5 5.1.21、隔离 Pinyin 5.1.12 桥接；Qt 切窗补丁针对 fcitx5-qt 5.1.14 / Qt 6.10.2 / X11。桌面回归 Qt 21/21、GTK 16/16，六项焦点保护专项重复 6/6 通过。这些是特定环境测试，不是所有应用的兼容性保证，也不是翻译准确率。

当前源码包的独立目录检查见 [验证记录](docs/validation.md)。本仓库不包含模型权重、私有构建库、原始桌面日志、用户数据或开发者安装备份。

## 文档

- [设置窗口](docs/settings-window.md) · [词包格式](docs/dictionary-format.md) · [输入与快捷键](docs/input-controls.md)
- [构建范围](docs/building.md) · [Qt 草稿补丁](docs/qt-draft.md) · [已知限制与路线图](docs/roadmap.md)
- [隐私与安全](SECURITY.md) · [贡献指南](CONTRIBUTING.md)

## 许可证

原创代码、原创文档及人工示例按 **LGPL-2.1-or-later** 提供，见 [LICENSE](LICENSE)。文件自身声明优先；Qt 前端补丁按 BSD-3-Clause 提供。第三方依赖及模型保留各自许可证，详见 [第三方声明](THIRD_PARTY_NOTICES.md)。这是源码包，不分发这些依赖的二进制或模型权重。
