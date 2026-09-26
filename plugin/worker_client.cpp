#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include "worker_client.h"

#include <algorithm>
#include <array>
#include <cerrno>
#include <climits>
#include <cstring>
#include <limits>
#include <optional>
#include <utility>
#include <vector>

#include <fcntl.h>
#include <json-c/json.h>
#include <signal.h>
#include <spawn.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;

namespace transime {
namespace {
constexpr std::size_t maxFrame = 65536;
constexpr std::size_t maxTranslation = 4096;
constexpr std::uint64_t maxId = INT64_MAX;
using Json = std::unique_ptr<json_object, decltype(&json_object_put)>;
Json json(json_object *value) { return Json(value, &json_object_put); }
std::uint64_t clockNow() { return fcitx::now(CLOCK_MONOTONIC); }
void closeFd(int &fd) { if (fd >= 0) { ::close(fd); fd = -1; } }

bool absolutePath(const std::string &path) {
    return !path.empty() && path.front() == '/' && path.size() <= 4096 &&
           path.find('\0') == std::string::npos;
}
bool validConfig(const WorkerConfig &config) {
    struct stat py{}, script{}, model{};
    return absolutePath(config.python) && absolutePath(config.script) &&
           absolutePath(config.model) && config.threads >= 1 && config.threads <= 2 &&
           config.timeout_ms >= 1 && config.timeout_ms <= 120000 &&
           config.cold_timeout_ms >= 1 && config.cold_timeout_ms <= 120000 &&
           config.idle_ms >= 1 && config.idle_ms <= 3600000 &&
           ::stat(config.python.c_str(), &py) == 0 && S_ISREG(py.st_mode) &&
           ::access(config.python.c_str(), X_OK) == 0 &&
           ::stat(config.script.c_str(), &script) == 0 && S_ISREG(script.st_mode) &&
           ::access(config.script.c_str(), R_OK) == 0 &&
           ::stat(config.model.c_str(), &model) == 0 && S_ISDIR(model.st_mode);
}

std::optional<std::string> encode(std::uint64_t id,
                                  const RequestSnapshot &request, bool context) {
    // Count before constructing JSON; callers cannot cause an unbounded copy.
    if (request.source.size() > 4096 || request.history.size() > 5) return std::nullopt;
    std::size_t historyBytes = 0;
    for (const auto &entry : request.history) {
        if (entry.committed.size() > 2048 || entry.language.size() > 2048 ||
            (entry.source && entry.source->size() > 2048)) return std::nullopt;
        const auto extra = entry.committed.size() + entry.language.size() +
                           (entry.source ? entry.source->size() : 0);
        if (extra > 2048 || historyBytes > 2048 - extra) return std::nullopt;
        historyBytes += extra;
    }
    auto object = json(json_object_new_object());
    json_object_object_add(object.get(), "id", json_object_new_uint64(id));
    json_object_object_add(object.get(), "source",
                           json_object_new_string_len(request.source.data(), request.source.size()));
    auto *history = json_object_new_array_ext(static_cast<int>(request.history.size()));
    json_object_object_add(object.get(), "history", history);
    for (const auto &entry : request.history) {
        auto *item = json_object_new_object();
        json_object_object_add(item, "source", entry.source
            ? json_object_new_string_len(entry.source->data(), entry.source->size()) : nullptr);
        json_object_object_add(item, "committed",
                               json_object_new_string_len(entry.committed.data(), entry.committed.size()));
        json_object_object_add(item, "language",
                               json_object_new_string_len(entry.language.data(), entry.language.size()));
        json_object_array_add(history, item);
    }
    json_object_object_add(object.get(), "context", json_object_new_boolean(context));
    const char *text = json_object_to_json_string_ext(object.get(), JSON_C_TO_STRING_PLAIN);
    if (!text) return std::nullopt;
    const auto length = std::strlen(text);
    if (!length || length > maxFrame) return std::nullopt;
    std::string frame(4, '\0');
    for (unsigned i = 0; i < 4; ++i) frame[i] = static_cast<char>(length >> (24 - 8 * i));
    frame.append(text, length);
    return frame;
}

std::optional<WorkerResult> decode(const std::string &frame, std::uint64_t expected) {
    std::unique_ptr<json_tokener, decltype(&json_tokener_free)> parser(
        json_tokener_new_ex(16), &json_tokener_free);
    if (!parser) return std::nullopt;
    json_tokener_set_flags(parser.get(), JSON_TOKENER_STRICT | JSON_TOKENER_VALIDATE_UTF8);
    auto root = json(json_tokener_parse_ex(parser.get(), frame.data(), frame.size()));
    if (!root || json_tokener_get_error(parser.get()) != json_tokener_success ||
        json_tokener_get_parse_end(parser.get()) != frame.size() ||
        !json_object_is_type(root.get(), json_type_object)) return std::nullopt;
    json_object *id = nullptr, *translation = nullptr, *error = nullptr;
    if (!json_object_object_get_ex(root.get(), "id", &id) ||
        !json_object_is_type(id, json_type_int) || json_object_get_int64(id) <= 0 ||
        json_object_get_uint64(id) != expected ||
        !json_object_object_get_ex(root.get(), "translation", &translation) ||
        !json_object_is_type(translation, json_type_string) ||
        json_object_get_string_len(translation) > static_cast<int>(maxTranslation) ||
        !json_object_object_get_ex(root.get(), "error", &error) ||
        (error && !json_object_is_type(error, json_type_string)) ||
        (error && json_object_get_string_len(error) > 256)) return std::nullopt;
    if (error) return WorkerResult{{}, WorkerError::Backend};
    return WorkerResult{
        std::string(json_object_get_string(translation), json_object_get_string_len(translation)),
        WorkerError::None};
}
} // namespace

class WorkerClient::Impl {
public:
    explicit Impl(fcitx::EventLoop &loop) : loop_(loop) {
        pump_ = loop_.addDeferEvent([this](fcitx::EventSource *event) {
            event->setEnabled(false);
            pump();
            return true;
        });
        pump_->setEnabled(false);
        timeout_ = timer([this] { fail(WorkerError::Timeout); });
        idle_ = timer([this] { stop(); });
        reap_ = timer([this] { reap(); });
    }
    ~Impl() {
        pending_.reset();
        current_.reset();
        pump_.reset(); timeout_.reset(); idle_.reset(); reap_.reset();
        io_.reset(); childEvent_.reset();
        closeFd(socket_);
        // Never block addon destruction. pidfd addresses this exact child, even
        // if Fcitx's own SIGCHLD reaper already collected its numeric PID.
        signalChild();
        if (pid_ > 0) ::waitpid(pid_, nullptr, WNOHANG);
        closeFd(pidfd_);
        // Fcitx Instance's SIGCHLD/WNOHANG zombieReaper covers a child that exits
        // after this destructor. No callback into the unloaded module survives.
    }

    void configure(WorkerConfig config) {
        if (config_ == config) return;
        clear();
        config_ = std::move(config);
    }
    std::uint64_t submit(const RequestSnapshot &request, bool context, Completion done) {
        if (++nextId_ > maxId) nextId_ = 1;
        auto frame = encode(nextId_, request, context);
        pending_ = Job{nextId_, frame.value_or(""), std::move(done),
                       frame ? WorkerError::None : WorkerError::RequestTooLarge};
        idle_->setEnabled(false);
        schedule();
        return nextId_;
    }
    void cancel(std::uint64_t id) {
        if (!id) return;
        if (pending_ && pending_->id == id) pending_.reset();
        if (current_ && current_->id == id) current_->done = {};
        if (!pending_ && !current_) armIdle();
    }
    void clear() {
        pending_.reset();
        current_.reset();
        stop();
    }
    bool running() const { return pid_ > 0; }
    std::size_t retained() const { return bool(current_) + bool(pending_); }

private:
    struct Job {
        std::uint64_t id;
        std::string frame;
        Completion done;
        WorkerError error;
    };

    std::unique_ptr<fcitx::EventSourceTime> timer(std::function<void()> callback) {
        auto event = loop_.addTimeEvent(CLOCK_MONOTONIC, clockNow(), 0,
            [callback = std::move(callback)](fcitx::EventSourceTime *source, std::uint64_t) {
                source->setEnabled(false);
                callback();
                return true;
            });
        event->setEnabled(false);
        return event;
    }
    static void arm(fcitx::EventSourceTime *timer, std::uint64_t delayMs) {
        timer->setTime(clockNow() + delayMs * 1000);
        timer->setOneShot();
    }
    void schedule() { pump_->setOneShot(); }
    void armIdle() {
        if (pid_ > 0 && !stopping_ && !current_ && !pending_) arm(idle_.get(), config_.idle_ms);
    }

    bool spawn() {
        if (!validConfig(config_)) return false;
        int pair[2] = {-1, -1};
        if (::socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0, pair) != 0) return false;
        posix_spawn_file_actions_t actions;
        posix_spawnattr_t attributes;
        if (posix_spawn_file_actions_init(&actions) != 0) {
            ::close(pair[0]); ::close(pair[1]); return false;
        }
        if (posix_spawnattr_init(&attributes) != 0) {
            posix_spawn_file_actions_destroy(&actions);
            ::close(pair[0]); ::close(pair[1]); return false;
        }
        // The child side remains blocking; only the event-thread side becomes
        // nonblocking. Use dup2 and closefrom so unrelated host FDs do not leak.
        int error = posix_spawn_file_actions_adddup2(&actions, pair[1], STDIN_FILENO);
        error = error ? error : posix_spawn_file_actions_adddup2(&actions, pair[1], STDOUT_FILENO);
        error = error ? error : posix_spawn_file_actions_addopen(&actions, STDERR_FILENO, "/dev/null", O_WRONLY, 0);
        error = error ? error : posix_spawn_file_actions_addclosefrom_np(&actions, 3);
        sigset_t mask, defaults;
        ::sigemptyset(&mask); ::sigemptyset(&defaults);
        ::sigaddset(&defaults, SIGPIPE); ::sigaddset(&defaults, SIGTERM);
        error = error ? error : posix_spawnattr_setsigmask(&attributes, &mask);
        error = error ? error : posix_spawnattr_setsigdefault(&attributes, &defaults);
        error = error ? error : posix_spawnattr_setflags(&attributes, POSIX_SPAWN_SETSIGMASK | POSIX_SPAWN_SETSIGDEF);
        std::string threads = std::to_string(config_.threads);
        std::vector<std::string> arguments{config_.python, "-I", "-B", "-u", config_.script,
                                           "--model-dir", config_.model, "--threads", threads};
        if (config_.fast_context) {
            arguments.emplace_back("--context-policy");
            arguments.emplace_back("lexical");
        }
        std::vector<char *> argv;
        for (auto &argument : arguments) argv.push_back(argument.data());
        argv.push_back(nullptr);
        pid_t child = -1;
        if (!error) error = ::posix_spawn(&child, config_.python.c_str(), &actions,
                                         &attributes, argv.data(), environ);
        posix_spawn_file_actions_destroy(&actions);
        posix_spawnattr_destroy(&attributes);
        ::close(pair[1]);
        if (error) { ::close(pair[0]); return false; }
        pid_ = child;
        pidfd_ = static_cast<int>(::syscall(SYS_pidfd_open, child, 0));
        if (pidfd_ < 0) {
            // This backend requires Linux pidfd support for race-free lifetime
            // control. No event-loop turn/reaper occurs between spawn and here.
            if (::waitpid(child, nullptr, WNOHANG) == 0) ::kill(child, SIGKILL);
            ::close(pair[0]);
            stopping_ = true;
            arm(reap_.get(), 10);
            return false;
        }
        socket_ = pair[0];
        const int flags = ::fcntl(socket_, F_GETFL, 0);
        if (flags < 0 || ::fcntl(socket_, F_SETFL, flags | O_NONBLOCK) < 0) {
            stop(); return false;
        }
        stopping_ = false;
        warm_ = false;
        io_ = loop_.addIOEvent(socket_, fcitx::IOEventFlag::In,
            [this](fcitx::EventSourceIO *, int, fcitx::IOEventFlags flags) {
                if (flags & fcitx::IOEventFlag::Out) write();
                if (socket_ >= 0 && (flags & (fcitx::IOEventFlags(fcitx::IOEventFlag::In) |
                        fcitx::IOEventFlag::Err | fcitx::IOEventFlag::Hup))) read();
                return true;
            });
        childEvent_ = loop_.addIOEvent(pidfd_, fcitx::IOEventFlag::In,
            [this](fcitx::EventSourceIO *, int, fcitx::IOEventFlags) {
                // Drain a final complete response before reporting worker exit.
                if (socket_ >= 0) read();
                reap();
                return true;
            });
        return true;
    }

    void pump() {
        if (current_ || stopping_ || !pending_) { armIdle(); return; }
        if (pending_->error != WorkerError::None) {
            auto job = std::move(*pending_); pending_.reset();
            if (job.done) job.done({{}, job.error});
            return;
        }
        if (!validConfig(config_)) {
            auto job = std::move(*pending_); pending_.reset();
            if (job.done) job.done({{}, WorkerError::Configuration});
            return;
        }
        if (pid_ <= 0 && !spawn()) {
            auto job = std::move(*pending_); pending_.reset();
            if (job.done) job.done({{}, WorkerError::Spawn});
            return;
        }
        current_ = std::move(pending_); pending_.reset();
        written_ = 0;
        received_.clear();
        expected_ = 0;
        idle_->setEnabled(false);
        arm(timeout_.get(), warm_ ? config_.timeout_ms : config_.cold_timeout_ms);
        io_->setEvents(fcitx::IOEventFlags(fcitx::IOEventFlag::In) | fcitx::IOEventFlag::Out);
        write();
    }

    void write() {
        if (!current_ || socket_ < 0 || current_->frame.empty()) return;
        const auto &frame = current_->frame;
        const auto count = ::send(socket_, frame.data() + written_, frame.size() - written_, MSG_NOSIGNAL);
        if (count < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR) return;
            fail(WorkerError::Transport); return;
        }
        written_ += static_cast<std::size_t>(count);
        if (written_ == frame.size()) {
            current_->frame.clear();
            written_ = 0;
            io_->setEvents(fcitx::IOEventFlag::In);
        }
    }

    void read() {
        std::array<char, 8192> buffer{};
        std::size_t budget = maxFrame + 4;
        while (socket_ >= 0 && budget) {
            const auto count = ::recv(socket_, buffer.data(), std::min(buffer.size(), budget), 0);
            if (count < 0) {
                if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR) return;
                fail(WorkerError::Transport); return;
            }
            if (!count) { fail(WorkerError::Exited); return; }
            budget -= static_cast<std::size_t>(count);
            if (!current_ || !current_->frame.empty() ||
                received_.size() + static_cast<std::size_t>(count) > maxFrame + 4) {
                fail(WorkerError::Protocol); return;
            }
            received_.append(buffer.data(), count);
            if (received_.size() >= 4 && !expected_) {
                for (unsigned i = 0; i < 4; ++i)
                    expected_ = (expected_ << 8) | static_cast<unsigned char>(received_[i]);
                if (!expected_ || expected_ > maxFrame) { fail(WorkerError::Protocol); return; }
            }
            if (expected_ && received_.size() >= expected_ + 4) {
                if (received_.size() != expected_ + 4) { fail(WorkerError::Protocol); return; }
                auto result = decode(received_.substr(4), current_->id);
                if (!result) { fail(WorkerError::Protocol); return; }
                auto job = std::move(*current_); current_.reset();
                received_.clear(); expected_ = 0;
                timeout_->setEnabled(false);
                warm_ = result->error == WorkerError::None;
                schedule(); armIdle();
                if (job.done) job.done(std::move(*result));
                // A completion may clear/reconfigure the client. Return before
                // touching state; unsolicited bytes are rejected on next IO.
                return;
            }
        }
    }

    void fail(WorkerError error) {
        auto job = std::move(current_); current_.reset();
        stop();
        if (job && job->done) job->done({{}, error});
    }
    void signalChild() {
        if (pidfd_ >= 0) ::syscall(SYS_pidfd_send_signal, pidfd_, SIGKILL, nullptr, 0);
    }
    void stop() {
        timeout_->setEnabled(false); idle_->setEnabled(false);
        io_.reset(); closeFd(socket_);
        received_.clear(); expected_ = 0; written_ = 0;
        warm_ = false;
        if (pid_ > 0) {
            stopping_ = true;
            signalChild();
            arm(reap_.get(), 10);
        }
    }
    void reap() {
        if (pid_ <= 0) return;
        const auto result = ::waitpid(pid_, nullptr, WNOHANG);
        if (result == 0 || (result < 0 && errno == EINTR)) {
            arm(reap_.get(), 10);
            return;
        }
        // ECHILD is normal if Fcitx's reaper ran first. Never signal a numeric
        // PID after this point; pidfd does not refer to any later reused PID.
        pid_ = -1;
        stopping_ = false;
        childEvent_.reset(); closeFd(pidfd_);
        io_.reset(); closeFd(socket_);
        warm_ = false;
        if (current_) {
            auto job = std::move(*current_); current_.reset();
            timeout_->setEnabled(false);
            received_.clear();
            schedule();
            if (job.done) job.done({{}, WorkerError::Exited});
        } else schedule();
    }

    fcitx::EventLoop &loop_;
    WorkerConfig config_;
    std::uint64_t nextId_ = 0;
    std::optional<Job> current_, pending_;
    pid_t pid_ = -1;
    int pidfd_ = -1, socket_ = -1;
    bool warm_ = false, stopping_ = false;
    std::size_t written_ = 0, expected_ = 0;
    std::string received_;
    std::unique_ptr<fcitx::EventSourceIO> io_, childEvent_;
    std::unique_ptr<fcitx::EventSource> pump_;
    std::unique_ptr<fcitx::EventSourceTime> timeout_, idle_, reap_;
};

WorkerClient::WorkerClient(fcitx::EventLoop &loop) : impl_(std::make_unique<Impl>(loop)) {}
WorkerClient::~WorkerClient() = default;
void WorkerClient::configure(WorkerConfig config) { impl_->configure(std::move(config)); }
std::uint64_t WorkerClient::submit(const RequestSnapshot &request, bool context, Completion done) {
    return impl_->submit(request, context, std::move(done));
}
void WorkerClient::cancel(std::uint64_t id) { impl_->cancel(id); }
void WorkerClient::clear() { impl_->clear(); }
bool WorkerClient::running() const { return impl_->running(); }
std::size_t WorkerClient::retained_transactions() const { return impl_->retained(); }
} // namespace transime
