# Licensing and third-party notices

Copyright (C) 2026 TransIME contributors.

Except where a file or the exceptions below state otherwise, the original code,
documentation and authored example data in this source snapshot are distributed
under the GNU Lesser General Public License version 2.1 or, at your option, any
later version (SPDX: LGPL-2.1-or-later). See LICENSE. No warranty is provided.
This declaration does not replace any upstream copyright or license notice.

## Source patches included

- `bridge/pinyin-5.1.12-transime-snapshot-v1.patch` modifies
  [fcitx5-chinese-addons 5.1.12](https://github.com/fcitx/fcitx5-chinese-addons/tree/5.1.12),
  notably its LGPL-2.1-or-later Pinyin implementation. The modified pinyin.cpp/.h
  identify Copyright 2017 CSSlayer <wengxt@gmail.com>; pinyincandidate.cpp/.h
  identify Copyright 2024 CSSlayer <wengxt@gmail.com>. Retain upstream notices when
  applying the patch. `bridge/pinyin_public.h` declares LGPL-2.1-or-later.
- `bridge/qt6-preserve-panel-draft.patch` modifies the shared Qt input-context
  sources of [fcitx5-qt 5.1.14](https://github.com/fcitx/fcitx5-qt/tree/5.1.14).
  Those source files declare BSD-3-Clause: the .cpp identifies Copyright
  2011–2017 CSSlayer and the .h identifies Copyright 2012–2017 CSSlayer
  <wengxt@gmail.com>.
  TransIME modifications in this patch are also offered under BSD-3-Clause,
  Copyright 2026 TransIME contributors. The complete terms are in
  `LICENSES/BSD-3-Clause.txt`. This exception applies to the Qt patch, not to all
  components of fcitx5-qt. Retain all original notices in patched source.

TransIME changes include paragraph/mixed-input holding, candidate snapshots,
configurable controls, and opt-in Qt draft lifecycle handling. This source
snapshot records those modifications as of 2026-09-26. These are not upstream
releases and do not imply upstream endorsement.

## External dependencies, not bundled

Fcitx5, LibIME, Qt/PySide6/PyQt6, CTranslate2, SentencePiece, NumPy, Sacremoses,
and their transitive dependencies have separate upstream licenses. Installing
or redistributing them requires preserving the applicable notices. Build scripts
may retrieve additional development dependencies; this file is not a license
grant for those projects and is not a complete binary-distribution manifest.

The optional translation model is
[Helsinki-NLP/opus-mt-zh-en](https://huggingface.co/Helsinki-NLP/opus-mt-zh-en/tree/cf109095479db38d6df799875e34039d4938aaa6),
pinned to revision `cf109095479db38d6df799875e34039d4938aaa6`.
Its upstream model card states CC-BY-4.0. No weights are included here.
`scripts/prepare_model.py` performs INT8 conversion, not fine-tuning, and creates
source attribution and a manifest. Preserve these when distributing a converted
model, and comply with the model's own terms rather than this repository's LGPL.

All example dictionary entries and evaluation sentences included here are
authored synthetic examples, not a user's learned dictionary. Public evaluation
sets, including files historically named `heldout`, must not be described as
unseen benchmark data after publication.
