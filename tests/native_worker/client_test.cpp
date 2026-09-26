#include <functional>
#include <fstream>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include <cerrno>
#include <sys/wait.h>
#include <unistd.h>

#include <fcitx-utils/event.h>
#include "worker_client.h"

using transime::WorkerError;
using transime::WorkerResult;

class Tests {
public:
    Tests(std::string python, std::string script, std::string model)
        : client_(loop_), config_{std::move(python), std::move(script), std::move(model), 1, 250, 2000, 80} {
        client_.configure(config_);
    }
    int run() {
        after(0, [this] { basic(); });
        after(15000, [this] { throw std::runtime_error("suite deadline exceeded"); });
        loop_.exec();
        return result_;
    }
private:
    void require(bool value, const char *message) {
        if (!value) throw std::runtime_error(message);
    }
    void pass(const char *name) { ++passed_; std::cout << "PASS: " << name << '\n'; }
    void guarded(const std::function<void()> &function) {
        try { function(); }
        catch (const std::exception &error) {
            std::cerr << "FAIL: " << error.what() << '\n'; result_ = 1; loop_.exit();
        }
    }
    void after(std::uint64_t delay, std::function<void()> function) {
        // Zero selects the event backend's default coalescing tolerance, which
        // can exceed the synthetic 80 ms response delay on another distro.
        auto timer = loop_.addTimeEvent(CLOCK_MONOTONIC, fcitx::now(CLOCK_MONOTONIC) + delay * 1000, 1000,
            [this, function = std::move(function)](fcitx::EventSourceTime *source, std::uint64_t) {
                source->setEnabled(false); guarded(function); return true;
            });
        timer->setOneShot(); timers_.push_back(std::move(timer));
    }
    transime::RequestSnapshot request(std::string text) {
        transime::RequestSnapshot result; result.source = std::move(text); return result;
    }
    std::uint64_t submit(const std::string &text, std::function<void(WorkerResult)> function) {
        return client_.submit(request(text), false,
            [this, function = std::move(function)](WorkerResult result) {
                guarded([&] { function(std::move(result)); });
            });
    }
    void basic() {
        require(!client_.running(), "worker started before a request");
        submit("fragmented", [this](WorkerResult result) {
            require(result.error == WorkerError::None && result.translation == "translated fragmented",
                    "fragmented UTF-8 JSON response failed");
            pass("lazy spawn and fragmented framing");
            auto req = request("history"); req.history.push_back({std::nullopt, "prior", "und"});
            client_.submit(req, true, [this](WorkerResult response) {
                guarded([&] {
                    require(response.error == WorkerError::None, "history protocol failed");
                    pass("typed history and context protocol"); queue();
                });
            });
        });
    }
    void queue() {
        callbacks_ = 0;
        submit("first", [this](WorkerResult response) {
            require(response.error == WorkerError::None, "first queue request failed"); ++callbacks_;
        });
        after(10, [this] {
            submit("middle", [](WorkerResult) { throw std::runtime_error("replaced pending request delivered"); });
            submit("last", [this](WorkerResult response) {
                require(response.error == WorkerError::None && response.translation == "translated last" && callbacks_ == 1,
                        "latest-only queue failed");
                pass("one in-flight plus latest pending request"); cancel();
            });
            require(client_.retained_transactions() == 2, "transaction queue not bounded");
        });
    }
    void cancel() {
        const auto id = submit("cancel", [](WorkerResult) { throw std::runtime_error("cancelled response delivered"); });
        after(10, [this, id] {
            client_.cancel(id);
            submit("after_cancel", [this](WorkerResult response) {
                require(response.error == WorkerError::None && response.translation == "translated after_cancel",
                        "cancellation damaged next transaction");
                pass("cancelled response is discarded without corrupting stream"); responsiveness();
            });
        });
    }
    void responsiveness() {
        heartbeat_ = false;
        submit("slow", [this](WorkerResult response) {
            require(heartbeat_ && response.error == WorkerError::None, "event loop blocked by child inference");
            pass("event-loop heartbeat during delayed child request"); failures(0);
        });
        after(15, [this] { heartbeat_ = true; });
    }
    void failures(std::size_t index) {
        static const std::vector<std::pair<std::string, WorkerError>> cases{
            {"wrong_id", WorkerError::Protocol}, {"oversized", WorkerError::Protocol},
            {"bad_json", WorkerError::Protocol}, {"bad_utf8", WorkerError::Protocol},
            {"wrong_type", WorkerError::Protocol}, {"large_translation", WorkerError::Protocol},
            {"backend_error", WorkerError::Backend}, {"truncated", WorkerError::Exited},
            {"exit", WorkerError::Exited}, {"timeout", WorkerError::Timeout}};
        if (index == cases.size()) { clearQueued(); return; }
        submit(cases[index].first, [this, index](WorkerResult response) {
            require(response.error == cases[index].second && response.translation.empty(),
                    ("wrong failure result for " + cases[index].first).c_str());
            pass(("bounded failure: " + cases[index].first).c_str());
            // Verify restart/reuse after every fault, not just the error code.
            submit("recovered", [this, index](WorkerResult next) {
                require(next.error == WorkerError::None && next.translation == "translated recovered",
                        "worker failed to recover after a fault");
                failures(index + 1);
            });
        });
    }
    void clearQueued() {
        submit("timeout", [](WorkerResult) { throw std::runtime_error("cleared private active callback delivered"); });
        after(10, [this] {
            auto privateRequest = request("SYNTHETIC_PRIVATE_PENDING");
            privateRequest.history.push_back({std::nullopt, "SYNTHETIC_PRIVATE_HISTORY", "en"});
            client_.submit(privateRequest, true, [](WorkerResult) {
                throw std::runtime_error("cleared private pending callback delivered");
            });
            require(client_.retained_transactions() == 2, "clear queue setup did not retain two transactions");
            client_.clear();
            require(client_.retained_transactions() == 0, "clear retained queued history");
            after(50, [this] {
                submit("after_private_clear", [this](WorkerResult response) {
                    require(response.error == WorkerError::None && response.translation == "translated after_private_clear",
                            "new session was contaminated by a cleared queued response");
                    pass("clear drops active and pending private payloads before clean restart");
                    clearActive();
                });
            });
        });
    }
    void clearActive() {
        submit("timeout", [](WorkerResult) { throw std::runtime_error("cleared active callback delivered"); });
        after(15, [this] {
            client_.clear();
            require(client_.retained_transactions() == 0, "clear retained request/history payload");
            after(50, [this] {
                require(!client_.running(), "cleared child not killed/reaped");
                pass("clear drops callbacks and reaps active child without waiting"); idle();
            });
        });
    }
    void idle() {
        submit("idle", [this](WorkerResult response) {
            require(response.error == WorkerError::None, "idle setup failed");
            // Production timers may coalesce; test eventual release without
            // asserting a hard realtime deadline the client does not promise.
            after(600, [this] {
                require(!client_.running(), "idle child retained model memory");
                pass("idle timeout kills and reaps worker"); configuration();
            });
        });
    }
    void configuration() {
        auto config = config_; config.python = "python;echo unsafe";
        client_.configure(config);
        submit("configuration", [this](WorkerResult response) {
            require(response.error == WorkerError::Configuration && !client_.running(),
                    "relative/shell-like executable accepted");
            pass("invalid executable configuration fails without spawning a shell");
            client_.configure(config_);
            auto oversized = request(std::string(4097, 'x'));
            client_.submit(oversized, false, [this](WorkerResult result) {
                guarded([&] {
                    require(result.error == WorkerError::RequestTooLarge, "oversized request retained");
                    pass("request size rejected before serialization/spawn");
                    auto history = request("x");
                    history.history.push_back({std::nullopt, std::string(1024, 'a'), "und"});
                    history.history.push_back({std::nullopt, std::string(1024, 'b'), "und"});
                    client_.submit(history, true, [this](WorkerResult oversizedHistory) {
                        guarded([&] {
                            require(oversizedHistory.error == WorkerError::RequestTooLarge,
                                    "aggregate history exceeded 2048 bytes");
                            pass("aggregate history byte cap applies independently of source length");
                            client_.clear();
                            destruction();
                        });
                    });
                });
            });
        });
    }

    void destruction() {
        temporary_ = std::make_unique<transime::WorkerClient>(loop_);
        temporary_->configure(config_);
        temporary_->submit(request("timeout"), false, [](WorkerResult) {
            throw std::runtime_error("destroyed client callback survived");
        });
        after(20, [this] {
            require(temporary_->running(), "destructor test child did not start");
            std::ifstream children("/proc/self/task/" + std::to_string(::getpid()) + "/children");
            pid_t child = -1;
            children >> child;
            require(child > 0, "destructor test could not observe its own child");
            temporary_.reset();
            after(50, [this, child] {
                // A standalone EventLoop lacks the server's SIGCHLD plumbing.
                // Act as Fcitx's final WNOHANG reaper and require an exited child,
                // rather than leaving a callback in a destroyed/unloaded client.
                const auto status = ::waitpid(child, nullptr, WNOHANG);
                require(status == child || (status < 0 && errno == ECHILD),
                        "client destruction left its worker alive");
                pass("client destruction kills child without blocking or leaving callbacks");
                std::cout << passed_ << " native worker checks passed; no translation model used\n";
                result_ = 0; loop_.exit();
            });
        });
    }

    fcitx::EventLoop loop_;
    transime::WorkerClient client_;
    transime::WorkerConfig config_;
    std::unique_ptr<transime::WorkerClient> temporary_;
    std::vector<std::unique_ptr<fcitx::EventSourceTime>> timers_;
    int result_ = 1, passed_ = 0, callbacks_ = 0;
    bool heartbeat_ = false;
};

int main(int argc, char **argv) {
    if (argc != 4) { std::cerr << "usage: client_test ABS_PYTHON ABS_FAKE_WORKER ABS_MODEL_DIR\n"; return 2; }
    Tests tests(argv[1], argv[2], argv[3]);
    return tests.run();
}
