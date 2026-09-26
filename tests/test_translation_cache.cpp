#include "translation_cache.h"

#include <cstdint>
#include <functional>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace {
using transime::RequestSnapshot;
using transime::Session;
using transime::TranslationCache;

// Remains active in Release builds; no assert/NDEBUG-dependent checks.
#define CHECK(expression) do { if (!(expression)) { \
    throw std::runtime_error(std::string("line ") + std::to_string(__LINE__) + ": " + #expression); \
} } while (false)

RequestSnapshot request(Session& session, const std::string& source) {
    CHECK(session.set_candidate(source));
    auto result = session.request_snapshot();
    CHECK(result);
    return *result;
}

RequestSnapshot key(const std::string& source) {
    return {source, {}, {1, 1, 2, 3, 1}};
}

void fresh_identity_still_required() {
    Session session;
    session.begin_session();
    TranslationCache cache;
    auto old_request = request(session, "早上好");
    CHECK(session.accept_response(old_request, "Good morning."));
    CHECK(cache.put(old_request, "Good morning.", 100));
    request(session, "晚上好");
    auto current = request(session, "早上好");
    CHECK(current.identity.candidate_version != old_request.identity.candidate_version);
    CHECK(current.identity.request_version != old_request.identity.request_version);
    auto hit = cache.get(current, 101);
    CHECK(hit == "Good morning.");
    CHECK(!session.ready_translation());
    CHECK(!session.accept_response(old_request, *hit));
    CHECK(session.accept_response(current, *hit));
    CHECK(session.history().empty());
    (*hit)[0] = 'X';
    CHECK(cache.get(current, 102) == "Good morning.");
    CHECK(session.consume_translation() == "Good morning.");
}

void version_and_source_isolation() {
    TranslationCache cache;
    const auto original = key("收到");
    CHECK(cache.put(original, "Received.", 0));
    auto changed = original;
    ++changed.identity.session_epoch;
    CHECK(!cache.get(changed, 1));
    changed = original; ++changed.identity.history_version;
    CHECK(!cache.get(changed, 2));
    changed = original; ++changed.identity.configuration_version;
    CHECK(!cache.get(changed, 3));
    changed = original; changed.source = "收到了";
    CHECK(!cache.get(changed, 4));
    CHECK(cache.get(original, 5) == "Received.");
}

void separate_context_instances() {
    Session first, second;
    first.begin_session(); second.begin_session();
    auto a = request(first, "收到");
    auto b = request(second, "收到");
    CHECK(a.identity == b.identity); // Epochs are per Session, not globally unique.
    TranslationCache first_cache, second_cache;
    CHECK(first_cache.put(a, "Received.", 0));
    CHECK(!second_cache.get(b, 0));
    CHECK(second_cache.put(b, "Acknowledged.", 0));
    first_cache.clear();
    CHECK(!first_cache.get(a, 1));
    CHECK(second_cache.get(b, 1) == "Acknowledged.");
    CHECK(first.history().empty() && second.history().empty());
}

void real_history_and_configuration_boundaries() {
    Session session;
    session.begin_session();
    TranslationCache cache;
    auto original = request(session, "收到");
    CHECK(cache.put(original, "Received.", 0));
    CHECK(session.record_commit({std::nullopt, "已经发送了文件", "und"}));
    auto after_history = request(session, "收到");
    CHECK(!cache.get(after_history, 1));
    CHECK(cache.put(after_history, "Acknowledged.", 1));
    session.configuration_changed();
    auto after_config = request(session, "收到");
    CHECK(!cache.get(after_config, 2));
    CHECK(cache.put(after_config, "Understood.", 2));
    session.end_session(); session.begin_session();
    CHECK(!cache.get(request(session, "收到"), 3));
}

void entry_lru_and_miss_behavior() {
    TranslationCache cache({2, 100, 100});
    const auto a = key("甲"), b = key("乙"), c = key("丙");
    CHECK(cache.put(a, "A", 0));
    CHECK(cache.put(b, "B", 1));
    CHECK(cache.get(a, 2) == "A");
    CHECK(!cache.get(key("missing"), 3));
    CHECK(cache.put(c, "C", 4));
    CHECK(!cache.get(b, 5));
    CHECK(cache.get(a, 5) == "A");
    CHECK(cache.get(c, 5) == "C");
    CHECK(cache.size() == 2 && cache.bytes() == 8);
}

void utf8_byte_budget_and_replacement() {
    TranslationCache cache({10, 10, 100});
    auto a = key("中"), b = key("文");
    CHECK(cache.put(a, "Ab", 0));
    CHECK(cache.put(b, "Cd", 0));
    CHECK(cache.bytes() == 10); // 2 * (3 UTF-8 source bytes + 2 output bytes).
    CHECK(!cache.put(a, "012345678", 1));
    CHECK(cache.bytes() == 10 && cache.size() == 2);
    CHECK(cache.get(a, 1) == "Ab");
    CHECK(cache.put(a, "Large", 2));
    CHECK(cache.bytes() == 8 && cache.size() == 1);
    CHECK(!cache.get(b, 2));
    CHECK(cache.get(a, 2) == "Large");
    CHECK(cache.put(a, "X", 3));
    CHECK(cache.bytes() == 4 && cache.size() == 1);
    cache.clear();
    CHECK(cache.size() == 0 && cache.bytes() == 0 && !cache.next_expiry());
}

void exact_ttl_and_lru_independent_expiry() {
    TranslationCache cache({10, 100, 10});
    auto a = key("甲"), b = key("乙");
    CHECK(cache.put(a, "A", 100));
    CHECK(cache.put(b, "B", 105));
    CHECK(cache.get(a, 109) == "A"); // Hit cannot extend the absolute deadline.
    CHECK(cache.next_expiry() == 110);
    CHECK(cache.prune(110));
    CHECK(!cache.get(a, 110));
    CHECK(cache.get(b, 110) == "B");
    CHECK(cache.next_expiry() == 115);
    CHECK(cache.prune(115));
    CHECK(cache.size() == 0 && cache.bytes() == 0 && !cache.next_expiry());
}

void replacement_ttl() {
    TranslationCache cache({10, 100, 10});
    const auto a = key("甲");
    CHECK(cache.put(a, "A", 0));
    CHECK(cache.put(a, "Updated", 5));
    CHECK(cache.size() == 1 && cache.next_expiry() == 15);
    CHECK(cache.get(a, 10) == "Updated");
    CHECK(!cache.get(a, 15));
}

void backwards_clock_fails_closed() {
    TranslationCache cache;
    const auto a = key("甲");
    CHECK(cache.put(a, "A", 100));
    CHECK(!cache.get(a, 99));
    CHECK(cache.size() == 0 && cache.bytes() == 0 && !cache.next_expiry());
    CHECK(!cache.put(a, "A", 99));
    CHECK(cache.put(a, "A", 100));
    CHECK(!cache.prune(98));
    CHECK(cache.size() == 0);
    cache.clear();
    CHECK(!cache.put(a, "A", 99)); // clear does not reset the time domain.
    CHECK(cache.put(a, "A", 101));
}

void invalid_text_does_not_replace_success() {
    TranslationCache cache;
    const auto a = key("收到");
    CHECK(cache.put(a, "Received.", 0));
    for (const std::string& value : {std::string(), std::string("\xc2\xa0"),
                                   std::string("broken\ntext"), std::string("\xed\xa0\x80")}) {
        CHECK(!cache.put(a, value, 1));
    }
    CHECK(!cache.put(key(""), "Empty source", 1));
    CHECK(!cache.put(key(std::string("\xff")), "Broken UTF-8", 1));
    CHECK(cache.size() == 1 && cache.get(a, 1) == "Received.");
}

void defaults_and_disabled_limits() {
    TranslationCache cache;
    for (unsigned index = 0; index < 33; ++index) {
        CHECK(cache.put(key("source" + std::to_string(index)), "translation", 0));
    }
    CHECK(cache.size() == 32 && cache.next_expiry() == 120000);
    CHECK(!cache.get(key("source0"), 0));
    TranslationCache exact_default;
    CHECK(exact_default.put(key("中"), std::string(256 * 1024 - 3, 'A'), 0));
    CHECK(exact_default.bytes() == 256 * 1024);
    CHECK(!exact_default.put(key("中"), std::string(256 * 1024 - 2, 'A'), 0));
    CHECK(exact_default.bytes() == 256 * 1024);
    for (auto limits : {transime::TranslationCacheLimits{0, 100, 100},
                        transime::TranslationCacheLimits{10, 0, 100},
                        transime::TranslationCacheLimits{10, 100, 0}}) {
        TranslationCache disabled(limits);
        CHECK(!disabled.put(key("中"), "A", 0));
        CHECK(!disabled.get(key("中"), 0));
        CHECK(!disabled.next_expiry());
    }
}

void deadline_overflow_is_rejected() {
    TranslationCache cache({10, 100, 10});
    constexpr auto maximum = std::numeric_limits<std::uint64_t>::max();
    CHECK(!cache.put(key("中"), "A", maximum - 9));
    CHECK(cache.size() == 0 && !cache.next_expiry());
    TranslationCache boundary({10, 100, 10});
    CHECK(boundary.put(key("中"), "A", maximum - 10));
    CHECK(boundary.next_expiry() == maximum);
    CHECK(boundary.get(key("中"), maximum - 1) == "A");
    CHECK(!boundary.get(key("中"), maximum));
}
} // namespace

int main() {
    const std::vector<std::pair<std::string, std::function<void()>>> tests = {
        {"new request identity and copied hits", fresh_identity_still_required},
        {"source/session/history/configuration isolation", version_and_source_isolation},
        {"independent input context caches", separate_context_instances},
        {"real Session history/configuration/focus boundaries", real_history_and_configuration_boundaries},
        {"LRU capacity and miss behavior", entry_lru_and_miss_behavior},
        {"UTF-8 byte accounting and atomic rejection", utf8_byte_budget_and_replacement},
        {"absolute expiry independent of LRU", exact_ttl_and_lru_independent_expiry},
        {"replacement gets a fresh TTL", replacement_ttl},
        {"backwards clocks reject and clear", backwards_clock_fails_closed},
        {"invalid text never replaces success", invalid_text_does_not_replace_success},
        {"default limits and disabled caches", defaults_and_disabled_limits},
        {"deadline arithmetic overflow", deadline_overflow_is_rejected},
    };
    try {
        for (const auto& [name, run] : tests) { run(); std::cout << "PASS: " << name << '\n'; }
        std::cout << tests.size() << " translation cache checks passed\n";
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
