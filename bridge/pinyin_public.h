/*
 * SPDX-FileCopyrightText: 2026 TransIME contributors
 * SPDX-License-Identifier: LGPL-2.1-or-later
 */
#ifndef _PINYIN_TRANSIME_PUBLIC_H_
#define _PINYIN_TRANSIME_PUBLIC_H_

#include <cstddef>
#include <cstdint>
#include <fcitx/addoninstance.h>
#include <fcitx-utils/key.h>
#include <string>

namespace fcitx {
class InputContext;

// Experimental extension to chinese-addons 5.1.12, NOT an upstream API.
// All strings are owned copies. Call only on Fcitx's event thread with a live IC.
struct PinyinTransimeSnapshotV1 {
    std::uint32_t apiVersion = 1;
    bool ready = false;
    std::string reason = "inactive";
    // Per-IC conservative input/UI generation, never advanced by this read.
    // This is not an IC identity; callers must also track IC lifetime/focus.
    std::uint64_t revision = 0;
    std::string selectedText;
    std::string candidateText;
    std::string source;
    // Offsets are bytes in ASCII pinyin input, not offsets into Chinese text.
    std::size_t inputBytes = 0;
    std::size_t selectedInputBytes = 0;
    std::size_t candidateInputEnd = 0;
    std::size_t cursorBytes = 0;
    // The libime candidate index; not the on-screen page-relative position.
    int candidateIndex = -1;
};

// Paragraph extension. V1 readers deliberately refuse an IC in hold mode.
// All offsets below refer to UTF-8 bytes in the rendered paragraph preedit;
// activeCandidate keeps its separate, segment-local ASCII input offsets.
struct PinyinTransimeParagraphSnapshotV2 {
    std::uint32_t apiVersion = 2;
    bool hold = false;
    bool hasDraft = false;
    bool ready = false; // Entire source is resolved Chinese/literal text.
    bool fullySelected = false;
    std::string reason = "inactive";
    std::uint64_t draftId = 0;
    std::uint64_t revision = 0;
    std::size_t activeSegment = 0;
    std::size_t cursorBytes = 0;
    // -1 means there is no ordinary pending candidate (e.g. fully selected).
    int pageIndex = -1;
    std::string source;
    std::string rawInput; // Original pinyin of all segments plus literal marks.
    PinyinTransimeSnapshotV1 activeCandidate;
};

enum class PinyinTransimeCommitModeV2 { Chinese = 0, Raw = 1, Translation = 2 };

// Optional extension: old V1/V2 consumers retain the historical shortcuts.
struct PinyinTransimeControlsV1 {
    bool showModeHints = false;
    KeyList chineseCommit{Key("space")};
    KeyList rawCommit{Key("Return"), Key("KP_Enter")};
    KeyList literalSpace{Key("Shift+space")};
};

// Shared validation for settings supplied over the addon boundary. Bare text,
// editing/navigation keys and modifier taps belong to composition. Ctrl+Space
// remains the paragraph's language switch. Detect normalized duplicates too.
inline bool transimeValidControlKeys(const std::vector<KeyList> &groups) {
    KeyList seen;
    for (const auto &group : groups) {
        for (const auto &original : group) {
            if (original.states() & ~KeyStates{KeyState::Ctrl, KeyState::Alt,
                    KeyState::Shift, KeyState::Super, KeyState::Super2}) return false;
            const auto key = original.normalize();
            if (!key.isValid() || key.isModifier() || key.code() ||
                key.check(FcitxKey_space, KeyState::Ctrl)) return false;
            const bool command = key.states().testAny(KeyStates{
                KeyState::Ctrl, KeyState::Alt, KeyState::Super});
            const auto sym = key.sym();
            if (!command && sym != FcitxKey_space && sym != FcitxKey_Return &&
                sym != FcitxKey_KP_Enter && !(sym >= FcitxKey_F1 && sym <= FcitxKey_F35)) return false;
            if (key.checkKeyList(seen)) return false;
            seen.push_back(key);
        }
    }
    return true;
}

inline KeyList transimeNormalizeControlKeys(KeyList keys) {
    for (auto &key : keys) key = key.normalize();
    return keys;
}
} // namespace fcitx

// Returns false without changing controls if required keys are empty/invalid.
// This changes no draft, input mode or persisted user configuration.
FCITX_ADDON_DECLARE_FUNCTION(Pinyin, transimeSetControlsV1,
                            bool(fcitx::InputContext *, fcitx::PinyinTransimeControlsV1));

FCITX_ADDON_DECLARE_FUNCTION(Pinyin, transimeSnapshotV1,
                            fcitx::PinyinTransimeSnapshotV1(fcitx::InputContext *));

// Read an ordinary complete candidate at this zero-based position on the
// CURRENT page. This does not move the cursor or materialize other pages.
// snapshot.candidateIndex remains the libime index, not this page position.
FCITX_ADDON_DECLARE_FUNCTION(Pinyin, transimeSnapshotAtV1,
                            fcitx::PinyinTransimeSnapshotV1(fcitx::InputContext *, int));

// A nonempty comment is accepted only when expected exactly matches the live
// eligible candidate snapshot. UTF-8, <=4096 bytes, no C0/C1 controls, line/para
// separators or bidi/zero-width controls. Callers still validate translation
// policy/semantics before displaying it. Only comment changes: no learning,
// text/select/forget/cursor/layout/revision changes and no UI event is emitted.
// Empty text restores the annotated candidate object's saved original Text,
// including formatting, only if expected and the original page position match
// its last annotation. Restore may follow focus/sensitivity or page changes; it
// never writes new text or changes the current page/cursor. The engine keeps at
// most 10 weak annotation handles; it never scans/materializes other pages.
FCITX_ADDON_DECLARE_FUNCTION(Pinyin, transimeSetCommentV1,
                            bool(fcitx::InputContext *, int,
                                 fcitx::PinyinTransimeSnapshotV1, std::string));

// 0=disabled, 1=enabled, 2=disable deferred until this draft is explicitly
// committed/cancelled, 3=rejected. No implicit commit, reset or user-config write.
FCITX_ADDON_DECLARE_FUNCTION(Pinyin, transimeSetHoldV1,
                            int(fcitx::InputContext *, bool));

// pageIndex=-1 reads the real highlighted row, or the fully selected paragraph.
// A nonnegative index reads that ordinary row on the current page. Reads never
// select, learn, move the cursor, or increment either generation.
FCITX_ADDON_DECLARE_FUNCTION(Pinyin, transimeParagraphSnapshotV2,
                            fcitx::PinyinTransimeParagraphSnapshotV2(fcitx::InputContext *, int));

// Explicit confirmation only. Revalidates expected against this IC and draft.
// Raw may commit unresolved pinyin; Translation requires ready and safe UTF-8.
// Chinese learns native pinyin segments; Translation does not learn English.
// False retains the draft unless a separate explicit reset/destroy cancelled it.
FCITX_ADDON_DECLARE_FUNCTION(Pinyin, transimeCommitParagraphV2,
                            bool(fcitx::InputContext *,
                                 fcitx::PinyinTransimeParagraphSnapshotV2,
                                 fcitx::PinyinTransimeCommitModeV2, std::string));

// Only current ordinary candidate annotations. Empty text restores the saved
// object (also after paging/focus); never changes input, revision or emits UI.
FCITX_ADDON_DECLARE_FUNCTION(Pinyin, transimeSetParagraphCommentV2,
                            bool(fcitx::InputContext *, int,
                                 fcitx::PinyinTransimeParagraphSnapshotV2, std::string));

// Explicit cancellation bound to a draft. Focus loss must not call this API.
FCITX_ADDON_DECLARE_FUNCTION(Pinyin, transimeCancelParagraphV1,
                            bool(fcitx::InputContext *, std::uint64_t));

#endif // _PINYIN_TRANSIME_PUBLIC_H_
