#pragma once

#include "session.h"

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <iterator>
#include <limits>
#include <list>
#include <optional>
#include <string>
#include <utility>

namespace transime {

struct TranslationCacheLimits {
    std::size_t max_entries = 32;
    // Counts source and translation UTF-8 bytes; never stores history strings.
    std::size_t max_bytes = 256 * 1024;
    std::uint64_t ttl_ms = 120000;
};

// One cache per Session / input context, on its single event thread. Epochs
// belong to a Session and are not globally unique: never share one instance
// across independent Sessions. No persistence, inference, commits, or timers.
//
// Cache keys contain exact source + session/history/configuration versions.
// Candidate/request versions deliberately do not participate. A hit is only a
// string copy: the adapter MUST use its NEW RequestSnapshot with
// Session::accept_response and validate the current pinyin snapshot before UI
// display or submission. The adapter must advance configuration_version for
// context/model/runtime changes and clear at focus/sensitive/reset boundaries.
class TranslationCache {
public:
    explicit TranslationCache(TranslationCacheLimits limits = {}) : limits_(limits) {}

    std::optional<std::string> get(const RequestSnapshot& request, std::uint64_t now_ms) {
        if (!prune(now_ms)) return std::nullopt;
        auto found = find(request);
        if (found == entries_.end()) return std::nullopt;
        auto result = found->translation;
        entries_.splice(entries_.begin(), entries_, found);
        return result;
    }

    // Call only for successful validated translations. The local text gate
    // reuses Session's UTF-8/output policy, but cannot establish semantic quality
    // or whether this caller's asynchronous request is still current.
    bool put(const RequestSnapshot& request, std::string translation, std::uint64_t now_ms) {
        if (!prune(now_ms) || !limits_.max_entries || !limits_.max_bytes || !limits_.ttl_ms ||
            request.source.size() > limits_.max_bytes ||
            translation.size() > limits_.max_bytes - request.source.size() ||
            now_ms > std::numeric_limits<std::uint64_t>::max() - limits_.ttl_ms) return false;
        Session validator;
        validator.begin_session();
        if (!validator.set_candidate(request.source)) return false;
        const auto validation_request = validator.request_snapshot();
        if (!validation_request || !validator.accept_response(*validation_request, translation)) return false;
        const auto bytes = request.source.size() + translation.size();
        auto found = find(request);
        if (found != entries_.end()) erase(found);
        while (entries_.size() >= limits_.max_entries || bytes_ > limits_.max_bytes - bytes) {
            erase(std::prev(entries_.end()));
        }
        entries_.push_front({request.source, std::move(translation),
                             request.identity.session_epoch, request.identity.history_version,
                             request.identity.configuration_version, now_ms + limits_.ttl_ms, bytes});
        bytes_ += bytes;
        return true;
    }

    // True means the supplied monotonic time was accepted. Time reversal clears
    // retained text and rejects this operation; the high-water mark remains so
    // further reversed calls cannot revive entries. Clear does not reset this
    // clock domain. Construct a new object to intentionally use a new clock.
    bool prune(std::uint64_t now_ms) {
        if (last_now_ms_ && now_ms < *last_now_ms_) {
            clear();
            return false;
        }
        last_now_ms_ = now_ms;
        for (auto entry = entries_.begin(); entry != entries_.end();) {
            if (entry->expires_at_ms <= now_ms) entry = erase(entry);
            else ++entry;
        }
        return true;
    }

    void clear() noexcept {
        entries_.clear();
        bytes_ = 0;
    }

    // Earliest absolute deadline, independent of LRU order. No hidden timer:
    // adapters can arm an event-loop timer then call prune at this deadline.
    // Hits do not extend TTL; replacement puts receive a new absolute TTL.
    std::optional<std::uint64_t> next_expiry() const noexcept {
        std::optional<std::uint64_t> result;
        for (const auto& entry : entries_) {
            if (!result || entry.expires_at_ms < *result) result = entry.expires_at_ms;
        }
        return result;
    }

    std::size_t size() const noexcept { return entries_.size(); }
    std::size_t bytes() const noexcept { return bytes_; }

private:
    struct Entry {
        std::string source;
        std::string translation;
        std::uint64_t session_epoch;
        std::uint64_t history_version;
        std::uint64_t configuration_version;
        std::uint64_t expires_at_ms;
        std::size_t bytes;
    };

    std::list<Entry>::iterator find(const RequestSnapshot& request) {
        return std::find_if(entries_.begin(), entries_.end(), [&request](const Entry& entry) {
            return entry.source == request.source &&
                   entry.session_epoch == request.identity.session_epoch &&
                   entry.history_version == request.identity.history_version &&
                   entry.configuration_version == request.identity.configuration_version;
        });
    }

    std::list<Entry>::iterator erase(std::list<Entry>::iterator entry) {
        bytes_ -= entry->bytes;
        return entries_.erase(entry);
    }

    TranslationCacheLimits limits_;
    std::list<Entry> entries_; // Front is most recently used; at most 32 by default.
    std::size_t bytes_ = 0;
    std::optional<std::uint64_t> last_now_ms_;
};

} // namespace transime
