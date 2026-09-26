#pragma once

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <vector>

namespace transime {

// Experimental core only: no Fcitx integration, inference, I/O, or real commits.
struct CommitRecord {
    std::optional<std::string> source;
    std::string committed;
    std::string language;

    bool operator==(const CommitRecord& other) const;
};

struct RequestIdentity {
    std::uint64_t session_epoch = 0;
    std::uint64_t candidate_version = 0;
    std::uint64_t history_version = 0;
    std::uint64_t configuration_version = 0;
    std::uint64_t request_version = 0;

    bool operator==(const RequestIdentity& other) const;
};

// An owned value copy; callers may keep it while an asynchronous job runs.
struct RequestSnapshot {
    std::string source;
    std::vector<CommitRecord> history;
    RequestIdentity identity;

    bool operator==(const RequestSnapshot& other) const;
};

struct HistoryLimits {
    std::size_t max_entries = 5;
    // UTF-8 bytes, not characters or sentences. Counts all three string fields.
    std::size_t max_bytes = 2048;
};

// Adapter-owned state. Every call must run on the adapter's one event thread;
// this class is not thread-safe. Worker results must be posted to that thread.
class Session {
public:
    explicit Session(HistoryLimits limits = {});

    // Call on focus-in AND any input-field/conversation switch, even in one app.
    // The core cannot detect these boundaries itself. Starts with no history.
    void begin_session();
    void end_session();
    void set_sensitive(bool sensitive);
    void set_enabled(bool enabled);
    void configuration_changed();

    // Supply the complete pending source chosen by the future pinyin adapter.
    // Every call advances the version, including A -> B -> A or equal text.
    // Invalid UTF-8 clears the source and returns false; empty text clears it.
    bool set_candidate(std::string source);

    // Call only when the adapter observes the real IME commit path, never on
    // candidate browse. This does not confirm the application's final contents.
    // False means not retained (inactive/invalid/empty/oversized/zero limits).
    // Oversized valid commits still advance history_version and invalidate work.
    // Retained records are whole entries; oversized entries are never byte-cut.
    bool record_commit(CommitRecord record);
    void clear_history();
    std::vector<CommitRecord> history() const;
    std::size_t history_bytes() const;

    // Starting a new request invalidates any earlier request/ready translation.
    std::optional<RequestSnapshot> request_snapshot();
    // Rejects malformed UTF-8, Unicode Cc/Cf/Cs, and all-White_Space output.
    bool accept_response(const RequestSnapshot& snapshot, std::string translation);
    std::optional<std::string> ready_translation() const;
    // Only consumes a value. It does not submit text, reset Fcitx, or learn words.
    std::optional<std::string> consume_translation();

private:
    struct ReadyTranslation {
        RequestSnapshot snapshot;
        std::string text;
    };

    bool active() const;
    bool matches_current(const RequestSnapshot& snapshot) const;
    void invalidate_work();
    void invalidate_session();

    HistoryLimits limits_;
    bool focused_ = false;
    bool enabled_ = true;
    bool sensitive_ = false;
    std::string source_;
    std::vector<CommitRecord> history_;
    std::size_t history_bytes_ = 0;
    RequestIdentity identity_;
    std::optional<RequestSnapshot> pending_;
    std::optional<ReadyTranslation> ready_;
};

}  // namespace transime
