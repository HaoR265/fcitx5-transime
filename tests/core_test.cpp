#include "session.h"

#include <functional>
#include <iostream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace {

using transime::CommitRecord;
using transime::HistoryLimits;
using transime::RequestSnapshot;
using transime::Session;

// Deliberately not assert(): these checks also execute in Release / NDEBUG.
#define CHECK(expression)                                                    \
    do {                                                                     \
        if (!(expression)) {                                                 \
            throw std::runtime_error(std::string("line ") +                 \
                                     std::to_string(__LINE__) + ": " +       \
                                     #expression);                           \
        }                                                                    \
    } while (false)

RequestSnapshot request(Session& session, const std::string& source) {
    CHECK(session.set_candidate(source));
    auto snapshot = session.request_snapshot();
    CHECK(snapshot.has_value());
    return *snapshot;
}

void candidate_late_response() {
    Session session;
    session.begin_session();
    const auto a = request(session, "收到");
    const auto b = request(session, "明白了");
    CHECK(!session.accept_response(a, "Received."));
    CHECK(!session.ready_translation());
    CHECK(session.accept_response(b, "Understood."));
    CHECK(!session.accept_response(a, "Received."));
    CHECK(session.ready_translation() == "Understood.");
    CHECK(session.history().empty());  // Browsing never becomes commit history.
}

void candidate_a_b_a() {
    Session session;
    session.begin_session();
    const auto old_a = request(session, "收到");
    request(session, "明白了");
    const auto new_a = request(session, "收到");
    CHECK(old_a.source == new_a.source);
    CHECK(old_a.identity.candidate_version != new_a.identity.candidate_version);
    CHECK(!session.accept_response(old_a, "Old reply."));
    CHECK(session.accept_response(new_a, "Got it."));
}

void history_change_with_same_source() {
    Session session;
    session.begin_session();
    CHECK(session.record_commit({"麻烦检查附件", "Please check the attachment.", "en"}));
    const auto before = request(session, "收到");
    CHECK(session.record_commit({std::nullopt, "文件已经送到", "zh"}));
    const auto after = session.request_snapshot();
    CHECK(after.has_value());
    CHECK(before.source == after->source);
    CHECK(before.identity.candidate_version == after->identity.candidate_version);
    CHECK(before.identity.history_version != after->identity.history_version);
    CHECK(before.history.size() == 1);  // Snapshot owns its historical value.
    CHECK(after->history.size() == 2);
    CHECK(!session.accept_response(before, "Acknowledged."));
    CHECK(session.accept_response(*after, "Received."));
    session.clear_history();
    CHECK(!session.ready_translation());
    CHECK(session.history().empty());
}

void focus_and_session_switch() {
    Session session;
    CHECK(!session.request_snapshot());
    CHECK(!session.set_candidate("未聚焦"));
    session.begin_session();
    CHECK(session.record_commit({std::nullopt, "previous text", "en"}));
    const auto old = request(session, "收到");
    CHECK(session.accept_response(old, "Got it."));
    session.end_session();
    CHECK(session.history().empty());
    CHECK(!session.ready_translation());
    CHECK(!session.request_snapshot());
    CHECK(!session.record_commit({std::nullopt, "unfocused text", "en"}));
    session.begin_session();
    CHECK(!session.request_snapshot());  // Old candidate was cleared too.
    const auto next = request(session, "收到");
    CHECK(old.identity.session_epoch != next.identity.session_epoch);
    CHECK(!session.accept_response(old, "Old window."));
    // begin_session also handles another field/conversation without focus-out.
    CHECK(session.record_commit({std::nullopt, "new context", "en"}));
    session.begin_session();
    CHECK(session.history().empty());
    CHECK(!session.accept_response(next, "Old conversation."));
}

void sensitive_field() {
    Session session;
    session.begin_session();
    CHECK(session.record_commit({std::nullopt, "context", "en"}));
    const auto old = request(session, "收到");
    CHECK(session.accept_response(old, "Got it."));
    session.set_sensitive(true);
    CHECK(session.history().empty());
    CHECK(!session.ready_translation());
    CHECK(!session.set_candidate("敏感输入"));
    CHECK(!session.request_snapshot());
    CHECK(!session.record_commit({std::nullopt, "secret", "en"}));
    session.begin_session();  // A focus event cannot override sensitivity.
    CHECK(!session.set_candidate("仍然敏感"));
    session.set_sensitive(false);
    CHECK(!session.request_snapshot());
    const auto next = request(session, "收到");
    CHECK(!session.accept_response(old, "Old field."));
    CHECK(session.accept_response(next, "Got it."));
}

void disabled_feature() {
    Session session;
    session.begin_session();
    CHECK(session.record_commit({std::nullopt, "context", "en"}));
    const auto old = request(session, "收到");
    session.set_enabled(false);
    CHECK(session.history().empty());
    CHECK(!session.request_snapshot());
    CHECK(!session.accept_response(old, "Late reply."));
    CHECK(!session.set_candidate("禁用时输入"));
    CHECK(!session.record_commit({std::nullopt, "disabled text", "en"}));
    session.set_enabled(true);
    CHECK(!session.request_snapshot());
    const auto current = request(session, "收到");
    CHECK(session.accept_response(current, "Got it."));
    session.set_enabled(false);
    CHECK(!session.ready_translation());
}

void configuration_changes() {
    Session session;
    session.begin_session();
    const auto old = request(session, "辛苦了");
    session.configuration_changed();
    const auto current = session.request_snapshot();
    CHECK(current.has_value());
    CHECK(old.source == current->source);
    CHECK(old.identity.configuration_version != current->identity.configuration_version);
    CHECK(!session.accept_response(old, "Thanks."));
    CHECK(session.accept_response(*current, "Thank you for your help."));
    session.configuration_changed();
    CHECK(!session.ready_translation());
}

void history_entry_limit_and_actual_text() {
    Session session;
    session.begin_session();
    CHECK(session.record_commit({"辛苦了", "Thank you for your help.", "en"}));
    CHECK(session.history().front().source == "辛苦了");
    CHECK(session.history().front().committed == "Thank you for your help.");
    for (int i = 0; i < 6; ++i) {
        CHECK(session.record_commit({std::nullopt, "commit-" + std::to_string(i), "en"}));
    }
    CHECK(session.history().size() == 5);
    CHECK(session.history().front().committed == "commit-1");
    CHECK(session.history().back().committed == "commit-5");
    CHECK(session.history_bytes() <= 2048);
}

void history_byte_limit_and_oversize() {
    Session session;
    session.begin_session();
    CHECK(session.record_commit({std::nullopt, std::string(1000, 'a'), "en"}));
    CHECK(session.record_commit({std::nullopt, std::string(1000, 'b'), "en"}));
    CHECK(session.history_bytes() == 2004);
    CHECK(session.record_commit({std::nullopt, std::string(1000, 'c'), "en"}));
    CHECK(session.history().size() == 2);
    CHECK(session.history().front().committed == std::string(1000, 'b'));
    const auto old = request(session, "收到");
    CHECK(!session.record_commit({std::nullopt, std::string(2049, 'x'), "en"}));
    CHECK(session.history_bytes() == 2004);
    CHECK(!session.accept_response(old, "Stale after a real oversized commit."));

    Session exact(HistoryLimits{5, 7});
    exact.begin_session();
    CHECK(exact.record_commit({"中", "ok", "en"}));  // 3 + 2 + 2 bytes.
    CHECK(exact.history_bytes() == 7);
    CHECK(!exact.record_commit({"中", "okay", "en"}));
    CHECK(exact.record_commit({std::nullopt, "中文", ""}));
    CHECK(exact.history().size() == 1);
    CHECK(exact.history().front().committed == "中文");
    CHECK(exact.history_bytes() == 6);
    CHECK(!exact.record_commit({std::nullopt, "中文字", ""}));
    CHECK(exact.history().front().committed == "中文");  // No split UTF-8.

    Session no_history(HistoryLimits{0, 2048});
    no_history.begin_session();
    CHECK(!no_history.record_commit({std::nullopt, "text", "en"}));
    CHECK(no_history.history().empty());
    Session no_bytes(HistoryLimits{5, 0});
    no_bytes.begin_session();
    CHECK(!no_bytes.record_commit({std::nullopt, "text", "en"}));
}

void repeated_requests_and_consumption() {
    Session session;
    session.begin_session();
    const auto first = request(session, "收到");
    const auto second = session.request_snapshot();
    CHECK(second.has_value());
    CHECK(first.identity.request_version != second->identity.request_version);
    CHECK(!session.accept_response(first, "Old duplicate job."));
    CHECK(session.accept_response(*second, "Got it."));
    CHECK(!session.accept_response(*second, "Duplicate response."));
    CHECK(session.ready_translation() == "Got it.");
    CHECK(session.ready_translation() == "Got it.");
    CHECK(session.consume_translation() == "Got it.");
    CHECK(!session.consume_translation());
    CHECK(!session.ready_translation());
    CHECK(!session.accept_response(*second, "Replay after consumption."));
    CHECK(session.history().empty());  // Consuming does not claim a real commit.
    const auto next = session.request_snapshot();
    CHECK(next.has_value());
    CHECK(next->source == "收到");  // Consuming does not reset the input engine.
}

void snapshot_integrity() {
    Session session;
    session.begin_session();
    CHECK(session.record_commit({std::nullopt, "context", "en"}));
    const auto original = request(session, "收到");
    auto changed = original;
    changed.source = "其他内容";
    CHECK(!session.accept_response(changed, "Wrong source."));
    changed = original;
    changed.history[0].committed = "changed context";
    CHECK(!session.accept_response(changed, "Wrong context."));
    changed = original;
    ++changed.identity.configuration_version;
    CHECK(!session.accept_response(changed, "Wrong identity."));
    CHECK(session.accept_response(original, "Got it."));
    auto history_copy = session.history();
    history_copy[0].committed = "externally changed";
    CHECK(session.history()[0].committed == "context");
}

void controls_and_empty_responses() {
    Session session;
    session.begin_session();
    const auto snapshot = request(session, "你好");
    CHECK(!session.accept_response(snapshot, ""));
    CHECK(!session.accept_response(snapshot, "   "));
    for (unsigned int value = 0; value <= 0x1f; ++value) {
        std::string text = "before";
        text.push_back(static_cast<char>(value));
        text += "after";
        CHECK(!session.accept_response(snapshot, text));
    }
    CHECK(!session.accept_response(snapshot, std::string("\x7f", 1)));
    for (unsigned int value = 0x80; value <= 0x9f; ++value) {
        std::string text(1, static_cast<char>(0xc2));
        text.push_back(static_cast<char>(value));
        CHECK(!session.accept_response(snapshot, text));
    }
    CHECK(!session.ready_translation());
    CHECK(session.accept_response(snapshot, "Hello — café 😀."));
}

void unicode_whitespace_and_format() {
    Session session;
    session.begin_session();
    const auto snapshot = request(session, "你好");
    const std::vector<std::string> whitespace = {
        u8"\u00a0", u8"\u1680", u8"\u2000", u8"\u2001", u8"\u2002", u8"\u2003",
        u8"\u2004", u8"\u2005", u8"\u2006", u8"\u2007", u8"\u2008", u8"\u2009",
        u8"\u200a", u8"\u2028", u8"\u2029", u8"\u202f", u8"\u205f", u8"\u3000",
    };
    for (const auto& text : whitespace) {
        CHECK(!session.accept_response(snapshot, text));
        CHECK(!session.accept_response(snapshot, " " + text + u8"\u3000"));
    }
    const std::vector<std::string> format_characters = {
        u8"\u00ad", u8"\u061c", u8"\u200b", u8"\u200c", u8"\u200d", u8"\u200e",
        u8"\u202a", u8"\u202e", u8"\u2060", u8"\u2066", u8"\u2069", u8"\ufeff",
        u8"\U00013430", u8"\U000e007f",
    };
    for (const auto& text : format_characters) {
        CHECK(!session.accept_response(snapshot, text));
        CHECK(!session.accept_response(snapshot, "Hello" + text + "world."));
    }
    CHECK(!session.ready_translation());
    // A visible phrase containing an NBSP is not an all-whitespace result.
    CHECK(session.accept_response(snapshot, u8"Hello\u00a0world."));
}

void strict_utf8() {
    const std::vector<std::string> malformed = {
        std::string("\x80", 1),              // Lone continuation.
        std::string("\xc0\xaf", 2),        // Overlong ASCII.
        std::string("\xc1\xbf", 2),        // Illegal lead.
        std::string("\xe0\x80\xaf", 3),  // Three-byte overlong.
        std::string("\xf0\x80\x80\xaf", 4),
        std::string("\xed\xa0\x80", 3),  // Surrogate.
        std::string("\xf4\x90\x80\x80", 4),  // > U+10FFFF.
        std::string("\xf5\x80\x80\x80", 4),
        std::string("\xff", 1),
        std::string("\xe4\xb8", 2),       // Truncated Chinese character.
        std::string("\xe4\x41\xad", 3),  // Invalid continuation.
    };
    Session session;
    session.begin_session();
    auto snapshot = request(session, "你好");
    for (const auto& text : malformed) {
        CHECK(!session.accept_response(snapshot, text));
        CHECK(!session.record_commit({std::nullopt, text, "en"}));
        CHECK(!session.record_commit({text, "valid", "en"}));
        CHECK(!session.record_commit({std::nullopt, "valid", text}));
    }
    CHECK(session.accept_response(snapshot, "Hello."));
    CHECK(!session.set_candidate(malformed.front()));
    CHECK(!session.ready_translation());
    CHECK(!session.request_snapshot());
    CHECK(session.set_candidate(""));
    CHECK(!session.request_snapshot());
    snapshot = request(session, "有效中文 😀");
    CHECK(session.accept_response(snapshot, "Valid text 😀."));
    CHECK(session.history().empty());
}

}  // namespace

int main() {
    const std::vector<std::pair<std::string, std::function<void()>>> cases = {
        {"candidate late response", candidate_late_response},
        {"candidate A-B-A", candidate_a_b_a},
        {"history changes with same source", history_change_with_same_source},
        {"focus and session switch", focus_and_session_switch},
        {"sensitive field", sensitive_field},
        {"disabled feature", disabled_feature},
        {"configuration changes", configuration_changes},
        {"history entry limit and actual text", history_entry_limit_and_actual_text},
        {"history UTF-8 byte limit and oversize", history_byte_limit_and_oversize},
        {"repeated requests and consumption", repeated_requests_and_consumption},
        {"snapshot integrity", snapshot_integrity},
        {"control characters and empty responses", controls_and_empty_responses},
        {"Unicode whitespace and format characters", unicode_whitespace_and_format},
        {"strict UTF-8", strict_utf8},
    };
    std::size_t failures = 0;
    for (const auto& test : cases) {
        try {
            test.second();
            std::cout << "PASS: " << test.first << '\n';
        } catch (const std::exception& error) {
            ++failures;
            std::cerr << "FAIL: " << test.first << ": " << error.what() << '\n';
        } catch (...) {
            ++failures;
            std::cerr << "FAIL: " << test.first << ": unknown exception\n";
        }
    }
    std::cout << (cases.size() - failures) << '/' << cases.size()
              << " experimental core cases passed\n";
    return failures == 0 ? 0 : 1;
}
