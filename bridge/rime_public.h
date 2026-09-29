/*
 * SPDX-FileCopyrightText: 2026 TransIME contributors
 * SPDX-License-Identifier: LGPL-2.1-or-later
 */
#ifndef _RIME_TRANSIME_PUBLIC_H_
#define _RIME_TRANSIME_PUBLIC_H_

#include <cstddef>
#include <cstdint>
#include <fcitx/addoninstance.h>
#include <string>

namespace fcitx {
class InputContext;

// Experimental fcitx5-rime 5.1.14 extension, not an upstream Rime API.
// Read-only, per-input-context, and valid only on Fcitx's event thread.
struct RimeTransimeSnapshotV1 {
    std::uint32_t apiVersion = 1;
    bool ready = false;
    std::uint64_t revision = 0;
    std::string source;
    std::string rawInput;
    std::size_t inputBytes = 0;
    std::size_t cursorBytes = 0;
    int candidateIndex = -1;
};
} // namespace fcitx

FCITX_ADDON_DECLARE_FUNCTION(Rime, transimeSnapshotV1,
                            fcitx::RimeTransimeSnapshotV1(fcitx::InputContext *));

#endif // _RIME_TRANSIME_PUBLIC_H_
