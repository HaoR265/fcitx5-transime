#include "session.h"

#include <utility>

namespace transime {
namespace {

// Unicode 17.0 White_Space; see the exact data links in core/README.md.
bool unicode_whitespace(std::uint32_t value) {
    return (value >= 0x09 && value <= 0x0d) || value == 0x20 ||
           value == 0x85 || value == 0xa0 || value == 0x1680 ||
           (value >= 0x2000 && value <= 0x200a) || value == 0x2028 ||
           value == 0x2029 || value == 0x202f || value == 0x205f || value == 0x3000;
}

// Unicode 17.0 Cf. Intentionally reject bidi/zero-width format characters in
// translation output; do not silently strip characters and change the result.
bool unicode_format(std::uint32_t value) {
    return value == 0xad || (value >= 0x600 && value <= 0x605) ||
           value == 0x61c || value == 0x6dd || value == 0x70f ||
           (value >= 0x890 && value <= 0x891) || value == 0x8e2 || value == 0x180e ||
           (value >= 0x200b && value <= 0x200f) ||
           (value >= 0x202a && value <= 0x202e) ||
           (value >= 0x2060 && value <= 0x2064) ||
           (value >= 0x2066 && value <= 0x206f) || value == 0xfeff ||
           (value >= 0xfff9 && value <= 0xfffb) || value == 0x110bd ||
           value == 0x110cd || (value >= 0x13430 && value <= 0x1343f) ||
           (value >= 0x1bca0 && value <= 0x1bca3) ||
           (value >= 0x1d173 && value <= 0x1d17a) || value == 0xe0001 ||
           (value >= 0xe0020 && value <= 0xe007f);
}

// Strict UTF-8 scalar decoding: rejects overlongs, surrogates, and > U+10FFFF.
bool valid_utf8(const std::string& text, bool translation_policy = false) {
    bool has_non_whitespace = false;
    for (std::size_t i = 0; i < text.size();) {
        const auto first = static_cast<unsigned char>(text[i++]);
        std::uint32_t scalar = 0;
        std::uint32_t minimum = 0;
        std::size_t remaining = 0;
        if (first <= 0x7f) {
            scalar = first;
        } else if (first >= 0xc2 && first <= 0xdf) {
            scalar = first & 0x1f;
            minimum = 0x80;
            remaining = 1;
        } else if (first >= 0xe0 && first <= 0xef) {
            scalar = first & 0x0f;
            minimum = 0x800;
            remaining = 2;
        } else if (first >= 0xf0 && first <= 0xf4) {
            scalar = first & 0x07;
            minimum = 0x10000;
            remaining = 3;
        } else {
            return false;
        }
        if (remaining > text.size() - i) {
            return false;
        }
        for (std::size_t j = 0; j < remaining; ++j) {
            const auto next = static_cast<unsigned char>(text[i++]);
            if ((next & 0xc0) != 0x80) {
                return false;
            }
            scalar = (scalar << 6) | (next & 0x3f);
        }
        if (scalar < minimum || scalar > 0x10ffff ||
            (scalar >= 0xd800 && scalar <= 0xdfff)) {
            return false;
        }
        if (translation_policy && (scalar <= 0x1f ||
                                   (scalar >= 0x7f && scalar <= 0x9f) ||
                                   unicode_format(scalar))) {
            return false;
        }
        has_non_whitespace = has_non_whitespace || !unicode_whitespace(scalar);
    }
    return !translation_policy || has_non_whitespace;
}

bool valid_record(const CommitRecord& record) {
    return !record.committed.empty() && valid_utf8(record.committed) &&
           valid_utf8(record.language) &&
           (!record.source || valid_utf8(*record.source));
}

// Bounded addition avoids overflow even if limits are set to SIZE_MAX.
std::optional<std::size_t> bounded_record_bytes(const CommitRecord& record,
                                               std::size_t limit) {
    std::size_t total = 0;
    const auto add = [&total, limit](std::size_t bytes) {
        if (bytes > limit - total) {
            return false;
        }
        total += bytes;
        return true;
    };
    if (!add(record.committed.size()) || !add(record.language.size()) ||
        (record.source && !add(record.source->size()))) {
        return std::nullopt;
    }
    return total;
}

}  // namespace

bool CommitRecord::operator==(const CommitRecord& other) const {
    return source == other.source && committed == other.committed &&
           language == other.language;
}

bool RequestIdentity::operator==(const RequestIdentity& other) const {
    return session_epoch == other.session_epoch &&
           candidate_version == other.candidate_version &&
           history_version == other.history_version &&
           configuration_version == other.configuration_version &&
           request_version == other.request_version;
}

bool RequestSnapshot::operator==(const RequestSnapshot& other) const {
    return source == other.source && history == other.history &&
           identity == other.identity;
}

Session::Session(HistoryLimits limits) : limits_(limits) {}

bool Session::active() const { return focused_ && enabled_ && !sensitive_; }

void Session::invalidate_work() {
    pending_.reset();
    ready_.reset();
}

void Session::invalidate_session() {
    ++identity_.session_epoch;
    ++identity_.candidate_version;
    ++identity_.history_version;
    source_.clear();
    history_.clear();
    history_bytes_ = 0;
    invalidate_work();
}

void Session::begin_session() {
    focused_ = true;
    invalidate_session();
}

void Session::end_session() {
    focused_ = false;
    invalidate_session();
}

void Session::set_sensitive(bool sensitive) {
    if (sensitive_ != sensitive) {
        sensitive_ = sensitive;
        invalidate_session();
    }
}

void Session::set_enabled(bool enabled) {
    if (enabled_ != enabled) {
        enabled_ = enabled;
        invalidate_session();
    }
}

void Session::configuration_changed() {
    ++identity_.configuration_version;
    invalidate_work();
}

bool Session::set_candidate(std::string source) {
    ++identity_.candidate_version;
    invalidate_work();
    if (!active() || !valid_utf8(source)) {
        source_.clear();
        return false;
    }
    source_ = std::move(source);
    return true;
}

bool Session::record_commit(CommitRecord record) {
    if (!active() || !valid_record(record)) {
        return false;
    }
    ++identity_.history_version;
    invalidate_work();
    const auto bytes = bounded_record_bytes(record, limits_.max_bytes);
    if (!bytes || limits_.max_entries == 0) {
        return false;
    }
    while (!history_.empty() &&
           (history_.size() >= limits_.max_entries ||
            history_bytes_ > limits_.max_bytes - *bytes)) {
        history_bytes_ -= *bounded_record_bytes(history_.front(), limits_.max_bytes);
        history_.erase(history_.begin());
    }
    history_bytes_ += *bytes;
    history_.push_back(std::move(record));
    return true;
}

void Session::clear_history() {
    ++identity_.history_version;
    history_.clear();
    history_bytes_ = 0;
    invalidate_work();
}

std::vector<CommitRecord> Session::history() const { return history_; }

std::size_t Session::history_bytes() const { return history_bytes_; }

std::optional<RequestSnapshot> Session::request_snapshot() {
    if (!active() || source_.empty()) {
        return std::nullopt;
    }
    invalidate_work();
    ++identity_.request_version;
    pending_ = RequestSnapshot{source_, history_, identity_};
    return pending_;
}

bool Session::matches_current(const RequestSnapshot& snapshot) const {
    return active() && snapshot.identity == identity_ &&
           snapshot.source == source_ && snapshot.history == history_;
}

bool Session::accept_response(const RequestSnapshot& snapshot,
                              std::string translation) {
    if (!pending_ || !(snapshot == *pending_) || !matches_current(snapshot) ||
        !valid_utf8(translation, true)) {
        return false;
    }
    ready_ = ReadyTranslation{snapshot, std::move(translation)};
    pending_.reset();
    return true;
}

std::optional<std::string> Session::ready_translation() const {
    if (!ready_ || !matches_current(ready_->snapshot)) {
        return std::nullopt;
    }
    return ready_->text;
}

std::optional<std::string> Session::consume_translation() {
    auto result = ready_translation();
    ready_.reset();
    return result;
}

}  // namespace transime
