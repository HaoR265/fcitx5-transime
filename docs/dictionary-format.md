# 自选词包

用户在设置中选择自己的文件，先预览全部校验结果，再导入。项目不打包、读取或迁移开发者的个人词库。示例只包含为演示编写的合成词条。

每个导入词包独立管理，可以查看、导出及移除。词包不会合并到 `user.dict`，也不修改学习历史。停用或移除领域词包不会删除个人学习数据。

## 文件格式

支持 UTF-8（可带 BOM）文本，每份最多 8 MiB、50000 条。TXT 每行是 `词语 拼音 score`，空行和以 `#` 开头的注释行可以保留。例如：

```text
后量子密码 hou'liang'zi'mi'ma 0
注意力机制 zhu'yi'li'ji'zhi 0
```

CSV、TSV 首行需要 `word,pinyin` 两列，可增加 `score`、`english`，列顺序不限。TSV 用制表符分隔。

```csv
word,pinyin,english
后量子密码,hou'liang'zi'mi'ma,post-quantum cryptography
注意力机制,zhu'yi'li'ji'zhi,attention mechanism
```

拼音使用无声调全拼，用英文单引号分隔，`ü` 规范化为 `v`；不接受模糊拼音、声调数字、双拼键序或未分隔的拼音串。本版要求词语每个字符对应一个音节，英文缩写放入 `english` 释义列。底层输入方案如何消费词包，仍需对应环境验收；导入成功不代表已验证双拼支持。

`score` 可省略，默认为 0；填写时须为 0–1000000000 的整数。这是原样传入 LibIME 的词条数值，不保证某一候选排名。词语最多 128 字符，英文释义最多 1024 字符。不接受未知列、列数错误、空词包、控制字符、错误音节以及重复的“词语 + 拼音”组合；一条错误会拒绝整份文件，不静默丢弃。

`english` 会保存并可再次导出，**目前尚未接入翻译模型**，添加释义不会自动约束译文。

## 管理与恢复

`DictionaryManager(data_home)` 中 `data_home` 是 Fcitx 数据目录，例如测试账户的 `~/.local/share/fcitx5`。生成文件放入 `pinyin/dictionaries/transime-<随机 ID>.dict`，源 TSV 与包含哈希的元数据放入 `transime/dictionaries/<ID>/`。用户显示名称不作为路径，也不交给 shell 执行。

导入需要目标环境中的 `libime_pinyindict`。编译后反向解码，检查全部词语、拼音与分值是否完整一致，再原子发布单个二进制词典。工具缺失、报错或回读不符时不会发布该词包。源文件和二进制位于不同目录，因此整个操作不是跨文件的单次原子事务；进程意外中断后，损坏或不完整包会在列表中显示错误。

删除只允许本工具记录的 ID，先核对文件和源文件哈希，拒绝符号链接、改变过的内容和额外未知文件。它不会递归清理目录。导出为 TSV，拒绝覆盖已有目标文件。

安装或移除后需要让 Fcitx 重新加载词典；管理 API 只报告 `reload_required`，不自动重启正在输入的会话。桌面设置层应提示用户在合适时间应用。编译回读通过只证明词典结构有效；真实候选、不同系统及输入方案的行为须在虚拟机中单独验收。

注意：普通 `fcitx5-remote -r` 不会重新加载 Pinyin 的附加词典。独立设置的“加载词包”调用 `fcitx://config/addon/pinyin/dictmanager` 子配置刷新；若会话不可用，用户需结束草稿后退出并重新打开输入法。这里不会自动重启或向应用输入测试文字。

## 开发接口与验证

`settings/dictionaries.py` 提供 `DictionaryManager` 与 `DictionaryError(ValueError)`。`preview_file(path)` 返回可 JSON 序列化的 `count, entries, sha256, translator_integration, notice`；`import_file(path, name, expected_sha256=None)` 返回 ID、名称、数量、路径及 `reload_required`。设置界面传入预览的 `sha256`，文件改变时拒绝导入，要求重新预览。摘要与解析结果来自同一次读取的字节；后续编译使用已验证的内存条目。`list_packs()` 返回包含 `status` 的列表；`remove_pack(id)` 返回移除结果；`export_pack(id, path)` 返回导出路径及数量。调用方应展示 `DictionaryError`，并处理文件系统错误。

在专用虚拟机中的项目目录运行：

```sh
python3 -m unittest discover -s tests -p test_dictionaries.py -v
```

测试仅创建临时合成数据。事务单元测试使用模拟编译器；另有实际 LibIME 编译回读测试，缺少工具时会明确跳过。不得将跳过或模拟通过写成真实词库兼容性通过。遵循本次用户边界，不在宿主机运行测试或安装词包。
