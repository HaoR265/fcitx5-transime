#pragma once

#include <cstdint>
#include <functional>
#include <memory>
#include <string>

#include <fcitx-utils/event.h>

#include "session.h"

namespace transime {

struct WorkerConfig {
    std::string python;
    std::string script;
    std::string model;
    int threads = 2;
    std::uint64_t timeout_ms = 2000;
    std::uint64_t cold_timeout_ms = 10000;
    std::uint64_t idle_ms = 120000;
    bool fast_context = false;
    bool operator==(const WorkerConfig &) const = default;
};

// Stable diagnostics only; child text/errors must not be logged or exposed here.
enum class WorkerError { None, Configuration, RequestTooLarge, Spawn,
                         Transport, Protocol, Timeout, Exited, Backend };
struct WorkerResult {
    std::string translation;
    WorkerError error = WorkerError::None;
};

// All methods and callbacks run on the caller's Fcitx event thread. Never runs
// inference, waits for a child, or performs blocking stream I/O on that thread.
// One running transaction and one replaceable pending transaction are retained.
class WorkerClient {
public:
    using Completion = std::function<void(WorkerResult)>;
    explicit WorkerClient(fcitx::EventLoop &loop);
    ~WorkerClient();
    WorkerClient(const WorkerClient &) = delete;
    WorkerClient &operator=(const WorkerClient &) = delete;

    void configure(WorkerConfig config);
    std::uint64_t submit(const RequestSnapshot &request, bool context,
                         Completion completion);
    // Erases callbacks and pending payload for this request. An already sent
    // transaction may finish, but cannot deliver a result to the caller.
    void cancel(std::uint64_t id);
    // Also stops the process, discarding its in-flight text and model memory.
    void clear();
    bool running() const;
    std::size_t retained_transactions() const;

private:
    class Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace transime
