#include <fcitx-config/rawconfig.h>
#include <fcitx-utils/capabilityflags.h>
#include <fcitx-utils/event.h>
#include <fcitx-utils/eventdispatcher.h>
#include <fcitx-utils/eventloopinterface.h>
#include <fcitx-utils/key.h>
#include <fcitx-utils/log.h>
#include <fcitx-utils/standardpaths.h>
#include <fcitx-utils/testing.h>
#include <fcitx/addoninstance.h>
#include <fcitx/addonloader.h>
#include <fcitx/addonmanager.h>
#include <fcitx/candidatelist.h>
#include <fcitx/event.h>
#include <fcitx/inputcontext.h>
#include <fcitx/inputmethodgroup.h>
#include <fcitx/inputmethodmanager.h>
#include <fcitx/inputpanel.h>
#include <fcitx/instance.h>
#include <fcitx/text.h>
#include <fcitx/userinterface.h>

#include "pinyin_public.h"
#include "transime_test_public.h"

#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iomanip>
#include <iostream>
#include <iterator>
#include <memory>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>
#include <sys/wait.h>
#include <unistd.h>

FCITX_DEFINE_STATIC_ADDON_REGISTRY(transimeTestStaticAddons);
FCITX_IMPORT_ADDON_FACTORY(transimeTestStaticAddons, keyboard);

namespace {

// All input in this program is synthetic. It creates no desktop connection.
void require(bool value, const std::string &message) {
    if (!value) {
        throw std::runtime_error(message);
    }
}

class TestInputContext final : public fcitx::InputContextV2 {
public:
    explicit TestInputContext(fcitx::InputContextManager &manager)
        : InputContextV2(manager, "transime-headless-synthetic-test") {
        setCapabilityFlags(fcitx::CapabilityFlags(fcitx::CapabilityFlag::Preedit) |
                           fcitx::CapabilityFlag::FormattedPreedit |
                           fcitx::CapabilityFlag::CommitStringWithCursor);
        created();
    }
    ~TestInputContext() override { destroy(); }
    const char *frontend() const override { return "transime-testfrontend"; }

    bool key(const std::string &description, bool release = false, bool repeat = false) {
        fcitx::InputContextEventBlocker blocker(this);
        auto key = fcitx::Key(description);
        if (repeat) key = fcitx::Key(key.sym(), key.states() | fcitx::KeyState::Repeat, key.code());
        fcitx::KeyEvent event(this, key, release);
        const bool accepted = keyEvent(event);
        if (!accepted && !release && !repeat) unhandled_presses.push_back(description);
        return accepted;
    }
    bool unicode_key(std::uint32_t codepoint, bool release = false) {
        fcitx::InputContextEventBlocker blocker(this);
        fcitx::KeyEvent event(this, fcitx::Key(fcitx::Key::keySymFromUnicode(codepoint)), release);
        return keyEvent(event);
    }
    void type(const std::string &ascii) {
        for (char character : ascii) {
            const std::string description(1, character);
            require(key(description), "pinyin did not accept a synthetic input key");
            key(description, true);
        }
    }

    std::vector<std::string> commits;
    std::size_t cursor_commits = 0;
    std::size_t preedit_updates = 0;
    std::size_t forwarded_keys = 0;
    std::vector<std::string> forwarded_presses;
    std::vector<std::string> unhandled_presses;
    std::string last_preedit;

private:
    void commitStringImpl(const std::string &text) override { commits.push_back(text); }
    void commitStringWithCursorImpl(const std::string &text, std::size_t) override {
        ++cursor_commits;
        commits.push_back(text);
    }
    void deleteSurroundingTextImpl(int, unsigned int) override {
        throw std::runtime_error("unexpected surrounding-text deletion");
    }
    void forwardKeyImpl(const fcitx::ForwardKeyEvent &event) override {
        ++forwarded_keys;
        if (!event.isRelease()) forwarded_presses.push_back(event.key().toString());
    }
    void updatePreeditImpl() override {
        ++preedit_updates;
        last_preedit = inputPanel().clientPreedit().toString();
    }
};

struct Options {
    std::string root;
    std::vector<std::string> addon_dirs;
    std::vector<std::string> data_dirs;
    bool with_transime = false;
    bool without_bridge = false;
    bool candidate_rows = false;
    bool punctuation_diagnostic = false;
    bool punctuation_commit = false;
    bool punctuation_history_expiry = false;
    bool paragraph = false;
    bool mixed = false;
    bool dictionary_pack = false;
    std::string native_python, native_script, native_model;
};

Options parse(int argc, char **argv) {
    Options result;
    for (int i = 1; i < argc; ++i) {
        const std::string arg(argv[i]);
        const auto next = [&]() {
            require(i + 1 < argc, "missing value after " + arg);
            return std::string(argv[++i]);
        };
        if (arg == "--root") result.root = next();
        else if (arg == "--addon-dir") result.addon_dirs.push_back(next());
        else if (arg == "--data-dir") result.data_dirs.push_back(next());
        else if (arg == "--with-transime") result.with_transime = true;
        else if (arg == "--candidate-rows") { result.candidate_rows = true; result.with_transime = true; }
        else if (arg == "--mixed") { result.mixed = true; result.with_transime = true; }
        else if (arg == "--dictionary-pack") result.dictionary_pack = true;
        else if (arg == "--paragraph") { result.paragraph = true; result.with_transime = true; }
        else if (arg == "--punctuation-diagnostic") {
            result.punctuation_diagnostic = true; result.with_transime = true;
        }
        else if (arg == "--punctuation-commit") {
            result.punctuation_commit = true; result.with_transime = true;
        }
        else if (arg == "--punctuation-history-expiry") {
            result.punctuation_history_expiry = true; result.with_transime = true;
        }
        else if (arg == "--native-python") { result.native_python = next(); result.with_transime = true; }
        else if (arg == "--native-script") result.native_script = next();
        else if (arg == "--native-model") result.native_model = next();
        else if (arg == "--without-bridge") {
            result.with_transime = true;
            result.without_bridge = true;
        }
        else throw std::runtime_error("unknown test option: " + arg);
    }
    require(!result.root.empty() && std::filesystem::path(result.root).is_absolute(),
            "an explicit absolute isolated test root is required");
    require(std::filesystem::exists(std::filesystem::path(result.root) /
                                    ".transime-headless-test-root"),
            "run through run_headless.py to prepare an isolated test root");
    require(!result.addon_dirs.empty() && !result.data_dirs.empty(),
            "explicit addon and data directories are required");
    if (!result.native_python.empty() || !result.native_script.empty() || !result.native_model.empty()) {
        require(!result.without_bridge, "native worker path requires the pinyin bridge");
        for (const auto &path : {result.native_python, result.native_script, result.native_model})
            require(!path.empty() && std::filesystem::path(path).is_absolute(),
                    "all three absolute native worker paths are required");
    }
    require(static_cast<int>(result.candidate_rows) + static_cast<int>(result.punctuation_diagnostic) +
                static_cast<int>(result.punctuation_commit) + static_cast<int>(result.punctuation_history_expiry) +
                static_cast<int>(result.paragraph) + static_cast<int>(result.mixed) +
                static_cast<int>(result.dictionary_pack) <= 1,
            "test modes must be separate");
    require(!(result.candidate_rows || result.punctuation_diagnostic || result.punctuation_commit ||
              result.punctuation_history_expiry || result.paragraph || result.mixed) ||
                (!result.native_python.empty() && !result.without_bridge),
            "row/paragraph/diagnostic modes require the patched bridge and native worker paths");
    for (const char *name : {"DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS"}) {
        require(std::getenv(name) == nullptr, "desktop environment variable must be unset");
    }
    return result;
}

class Suite {
public:
    Suite(fcitx::Instance &instance, const Options &options)
        : instance_(instance), with_transime_(options.with_transime), without_bridge_(options.without_bridge),
          candidate_rows_(options.candidate_rows),
          punctuation_diagnostic_(options.punctuation_diagnostic),
          punctuation_commit_(options.punctuation_commit),
          punctuation_history_expiry_(options.punctuation_history_expiry),
          paragraph_(options.paragraph), mixed_(options.mixed), dictionary_pack_(options.dictionary_pack), root_(options.root),
          native_python_(options.native_python), native_script_(options.native_script), native_model_(options.native_model) {}

    void start() {
        guarded([this] {
            require(instance_.addonManager().addon("keyboard", true),
                    "real statically linked keyboard addon failed to load");
            auto *pinyin = instance_.addonManager().addon("pinyin", true);
            require(pinyin, "real pinyin addon failed to load");
            require(!instance_.addonManager().addon("dbus"), "dbus must stay unloaded");
            require(!instance_.addonManager().addon("x11"), "x11 must stay unloaded");
            require(!instance_.addonManager().addon("wayland"), "wayland must stay unloaded");

            fcitx::RawConfig config;
            config["CloudPinyinEnabled"].setValue("False");
            config["Prediction"].setValue("False");
            if (candidate_rows_ || paragraph_ || mixed_) {
                // Exercise the intended desktop bindings without touching its config.
                config["PrevCandidate/0"].setValue("Up");
                config["NextCandidate/0"].setValue("Down");
                config["PrevPage/0"].setValue("Page_Up");
                config["NextPage/0"].setValue("Page_Down");
                if (paragraph_) config["BackSpaceToUnselect"].setValue("True");
            }
            pinyin->setConfig(config);
            auto group = instance_.inputMethodManager().currentGroup();
            group.inputMethodList().clear();
            group.inputMethodList().emplace_back("keyboard-us");
            group.inputMethodList().emplace_back("pinyin");
            group.setDefaultInputMethod("pinyin");
            instance_.inputMethodManager().setGroup(std::move(group));

            flush_watcher_ = instance_.watchEvent(
                fcitx::EventType::InputContextFlushUI,
                fcitx::EventWatcherPhase::PostInputMethod,
                [this](fcitx::Event &event) {
                    const auto &update = static_cast<fcitx::InputContextEvent &>(event);
                    if (ic_ && update.inputContext() == ic_.get()) ++ui_flushes_;
                });
            new_context();
            baseline();
            async_aux();
        });
    }

    int result() const { return failed_ ? 1 : 0; }

private:
    using Action = std::function<void()>;
    void guarded(Action action) {
        if (finished_) return;
        try { action(); }
        catch (const std::exception &error) {
            failed_ = true;
            std::cerr << "FAIL: " << error.what() << '\n';
            finish();
        }
    }

    void after(std::uint64_t delay_us, Action action) {
        timers_.push_back(instance_.eventLoop().addTimeEvent(
            CLOCK_MONOTONIC, fcitx::now(CLOCK_MONOTONIC) + delay_us, 0,
            [this, action = std::move(action)](fcitx::EventSourceTime *timer,
                                              std::uint64_t) {
                timer->setEnabled(false);
                guarded(action);
                return true;
            }));
    }

    void await_fixture(const std::string &expected, Action next) {
        const auto deadline = fcitx::now(CLOCK_MONOTONIC) + 5000000;
        poll_fixture(expected, deadline, std::move(next));
    }

    void poll_fixture(std::string expected, std::uint64_t deadline, Action next) {
        const auto aux = ic_->inputPanel().auxDown().toString();
        if (aux.find("fixture") != std::string::npos &&
            aux.find(expected) != std::string::npos) {
            next();
            return;
        }
        require(fcitx::now(CLOCK_MONOTONIC) < deadline,
                "fixture translation did not become ready without another input key");
        after(10000, [this, expected = std::move(expected), deadline,
                      next = std::move(next)]() mutable {
            poll_fixture(std::move(expected), deadline, std::move(next));
        });
    }

    void new_context() {
        if (ic_) ic_->focusOut();
        ic_ = std::make_unique<TestInputContext>(instance_.inputContextManager());
        ic_->focusIn();
        instance_.setCurrentInputMethod(ic_.get(), "pinyin", true);
        require(instance_.inputMethod(ic_.get()) == "pinyin", "pinyin is not active in test context");
    }

    int candidate(const std::string &word) const {
        const auto list = ic_->inputPanel().candidateList();
        require(list && !list->empty(), "real pinyin candidate list is empty");
        if (const auto *bulk = list->toBulk()) {
            for (int i = 0; i < bulk->totalSize(); ++i)
                if (bulk->candidateFromAll(i).text().toString() == word) return i;
        } else {
            for (int i = 0; i < list->size(); ++i)
                if (list->candidate(i).text().toString() == word) return i;
        }
        throw std::runtime_error("required synthetic candidate is absent from real dictionary");
    }

    void select(const std::string &word) {
        const int index = candidate(word);
        const auto list = ic_->inputPanel().candidateList();
        if (const auto *bulk = list->toBulk()) bulk->candidateFromAll(index).select(ic_.get());
        else list->candidate(index).select(ic_.get());
    }

    void baseline() {
        ic_->type("nihao");
        candidate("你好");
        require(ic_->commits.empty(), "candidate browsing unexpectedly committed text");
        select("你");
        require(ic_->commits.empty(), "partial candidate unexpectedly committed the whole composition");
        const auto preedit = ic_->inputPanel().preedit().toString() +
                             ic_->inputPanel().clientPreedit().toString();
        require(preedit.find("你") != std::string::npos, "selected prefix missing from preedit");
        select("好");
        require(ic_->commits == std::vector<std::string>{"你好"},
                "real partial pinyin selection did not commit one complete Chinese result");
        pass("real pinyin partial selection and one Chinese commit");

        ic_->reset();
        ic_->commits.clear();
        ic_->type("zhongguo");
        candidate("中国");
        ic_->reset();
        require(ic_->commits.empty(), "reset emitted an unwanted Chinese commit");
        require(ic_->inputPanel().empty(), "reset left a nonempty input panel");
        require(ic_->last_preedit.empty(), "reset did not clear frontend preedit");
        ic_->type("nihao");
        candidate("你好");
        require(ic_->key("space"), "pinyin space selection was not accepted");
        ic_->key("space", true);
        require(ic_->commits == std::vector<std::string>{"你好"},
                "ordinary Chinese space selection failed after reset");
        pass("reset without commit and subsequent normal Chinese input");

        ic_->commitStringWithCursor("fixture cursor", 7);
        require(ic_->cursor_commits == 1 && ic_->commits.back() == "fixture cursor",
                "InputContextV2 cursor commit was not delivered");
        ic_->commits.clear();
        ic_->reset();
        pass("real CommitStringWithCursor frontend path");
    }

    void async_aux() {
        const auto before = ui_flushes_;
        ic_->inputPanel().setAuxDown(fcitx::Text("synthetic asynchronous aux"));
        ic_->updateUserInterface(fcitx::UserInterfaceComponent::InputPanel);
        after(20000, [this, before] {
            require(ui_flushes_ > before, "aux update did not reach deferred UI flush");
            require(ic_->inputPanel().auxDown().toString() == "synthetic asynchronous aux",
                    "asynchronous auxiliary text was not retained");
            pass("asynchronous auxiliary flush without another input key");
            ic_->reset();
            if (dictionary_pack_) dictionary_pack_start();
            else if (with_transime_) start_module(); else finish();
        });
    }

    bool synthetic_pack_candidate() const {
        const auto list = ic_->inputPanel().candidateList();
        if (!list || list->empty()) return false;
        if (const auto *bulk = list->toBulk()) {
            for (int i = 0; i < bulk->totalSize(); ++i)
                if (bulk->candidateFromAll(i).text().toString() == "麒麟密钥探针") return true;
        } else {
            for (int i = 0; i < list->size(); ++i)
                if (list->candidate(i).text().toString() == "麒麟密钥探针") return true;
        }
        return false;
    }

    void dictionary_fixture(const char *action) {
        const auto script = root_ + "/dictionary-fixture.py";
        const auto pid = fork();
        require(pid >= 0, "could not start synthetic dictionary fixture");
        if (!pid) {
            execl("/usr/bin/python3", "python3", script.c_str(), action, static_cast<char *>(nullptr));
            _exit(127);
        }
        int status = 0;
        require(waitpid(pid, &status, 0) == pid && WIFEXITED(status) && WEXITSTATUS(status) == 0,
                "production DictionaryManager failed in isolated synthetic fixture");
    }

    void dictionary_pack_start() {
        new_context(); ic_->type("kakakakakaka");
        require(!synthetic_pack_candidate(), "synthetic mapping unexpectedly exists before import");
        dictionary_fixture("import");
        new_context(); ic_->type("kakakakakaka");
        require(!synthetic_pack_candidate(), "extra dictionary was loaded before explicit reload");
        ic_->reset();
        instance_.addonManager().addon("pinyin")->setSubConfig("dictmanager", fcitx::RawConfig{});
        poll_dictionary_pack(fcitx::now(CLOCK_MONOTONIC) + 5000000);
    }

    void poll_dictionary_pack(std::uint64_t deadline) {
        after(50000, [this, deadline] {
            new_context(); ic_->type("kakakakakaka");
            if (!synthetic_pack_candidate()) {
                require(fcitx::now(CLOCK_MONOTONIC) < deadline,
                        "imported pack did not appear in real pinyin after dictmanager reload");
                poll_dictionary_pack(deadline);
                return;
            }
            require(ic_->commits.empty(), "candidate lookup unexpectedly learned or committed a word");
            pass("production DictionaryManager import appears in real Pinyin only after dictionary reload");
            ic_->reset();
            dictionary_fixture("remove");
            instance_.addonManager().addon("pinyin")->setSubConfig("dictmanager", fcitx::RawConfig{});
            after(100000, [this] {
                new_context(); ic_->type("kakakakakaka");
                require(!synthetic_pack_candidate(), "removed pack remains in candidates after reload");
                pass("production DictionaryManager removal and reload remove synthetic candidate without learning");
                finish();
            });
        });
    }

    void start_module() {
        auto *module = instance_.addonManager().addon("transime", true);
        require(module, "transime module failed to load");
        if (without_bridge_) { module_without_bridge(); return; }
        if (mixed_) { mixed_start(); return; }
        if (paragraph_) { paragraph_start(); return; }
        if (punctuation_diagnostic_) { punctuation_case(0); return; }
        if (punctuation_commit_) { punctuation_commit_start(); return; }
        if (punctuation_history_expiry_) { punctuation_expiry(); return; }
        if (candidate_rows_) { bridge_rows(); module_rows(); return; }
        bridge_snapshot();
        if (!native_python_.empty()) { module_native(); return; }
        fcitx::RawConfig config;
        config["Enabled"].setValue("True");
        config["CandidateTranslations"].setValue("False");
        config["ContinuousComposition"].setValue("False");
        config["TranslateKey/0"].setValue("Control+Alt+Return");
        config["AutoCommitOnPunctuation"].setValue("False");
        config["EnableHistory"].setValue("True");
        module->setConfig(config);
        new_context();
        ic_->type("nihao");
        await_fixture("Hello.", [this] {
            require(ic_->commits.empty(), "fixture completion committed before a shortcut");
            ic_->key("Control_L");
            ic_->key("Control+Alt_L");
            require(ic_->key("Control+Alt+Return"), "translation shortcut was not accepted");
            require(ic_->key("Control+Alt+Return"), "held translation shortcut escaped before release");
            require(ic_->key("Control+Alt+Return", true), "translation key release was not swallowed");
            ic_->key("Control+Alt+Alt_L", true);
            ic_->key("Control+Control_L", true);
            require(ic_->commits == std::vector<std::string>{"Hello."},
                    "fixture shortcut did not commit exactly one English translation");
            require(ic_->last_preedit.empty(), "English commit left Chinese preedit");
            require(!ic_->key("Control+Alt+Return"),
                    "translation shortcut with no preedit did not pass through");
            ic_->key("Control+Alt+Return", true);
            require(ic_->commits.size() == 1, "repeated shortcut duplicated a consumed result");
            const auto history = fixture_history();
            require(history.size() == 1 && history[0].source == "你好" &&
                    history[0].committed == "Hello." && history[0].language == "en",
                    "deferred outgoing English commit lost or duplicated its Chinese association");
            pass("real module fixture display, reset, English commit, and duplicate suppression");
            module_normal_chinese();
        });
    }

    void module_without_bridge() {
        auto *pinyin = instance_.addonManager().addon("pinyin");
        bool absent = false;
        try { pinyin->call<fcitx::IPinyin::transimeSnapshotV1>(ic_.get()); }
        catch (const std::exception &) { absent = true; }
        require(absent, "--without-bridge requires a real unpatched pinyin addon");
        auto *module = instance_.addonManager().addon("transime");
        fcitx::RawConfig config;
        config["Enabled"].setValue("True");
        config["CandidateTranslations"].setValue("False");
        config["ContinuousComposition"].setValue("False");
        config["TranslateKey/0"].setValue("Control+Alt+Return");
        config["AutoCommitOnPunctuation"].setValue("False");
        config["EnableHistory"].setValue("True");
        module->setConfig(config);
        new_context();
        ic_->type("nihao");
        after(60000, [this] {
            require(ic_->inputPanel().auxDown().toString().find("fixture") == std::string::npos,
                    "unpatched pinyin incorrectly produced a fixture translation");
            require(ic_->key("Control+Alt+Return"), "missing bridge shortcut escaped active composition");
            require(ic_->key("Control+Alt+Return", true), "missing bridge key release escaped");
            require(ic_->commits.empty(), "missing bridge path committed unintended text");
            candidate("你好");
            pass("missing pinyin bridge fails closed without translation or application submission");
            require(ic_->key("space"), "missing bridge path blocked normal Chinese selection");
            ic_->key("space", true);
            require(ic_->commits == std::vector<std::string>{"你好"},
                    "unpatched pinyin Chinese commit failed with enabled module");
            pass("unpatched pinyin still commits ordinary Chinese with enabled module");
            finish();
        });
    }

    fcitx::RawConfig native_config(bool enabled = true) const {
        fcitx::RawConfig config;
        config["Enabled"].setValue(enabled ? "True" : "False");
        config["CandidateTranslations"].setValue("False");
        config["ContinuousComposition"].setValue("False");
        config["TranslateKey/0"].setValue("Control+Alt+Return");
        config["AutoCommitOnPunctuation"].setValue("False");
        config["EnableHistory"].setValue("True");
        config["ContextAware"].setValue("False");
        config["UseTestFixture"].setValue("False");
        config["PythonExecutable"].setValue(native_python_);
        config["WorkerScript"].setValue(native_script_);
        config["ModelDirectory"].setValue(native_model_);
        config["DebounceMilliseconds"].setValue("150");
        config["FastContext"].setValue("False");
        return config;
    }

    std::string native_translation() const {
        const std::string prefix = "TransIME: ";
        const auto aux = ic_->inputPanel().auxDown().toString();
        return aux.starts_with(prefix) ? aux.substr(prefix.size()) : "";
    }

    fcitx::PinyinTransimeSnapshotV1 snapshot() {
        return instance_.addonManager().addon("pinyin")->
            call<fcitx::IPinyin::transimeSnapshotV1>(ic_.get());
    }

    void await_native(Action next, std::uint64_t deadline = 0) {
        if (!native_translation().empty()) { next(); return; }
        if (!deadline) deadline = fcitx::now(CLOCK_MONOTONIC) + 12000000;
        require(fcitx::now(CLOCK_MONOTONIC) < deadline,
                "native backend did not display a result within cold-start deadline");
        after(10000, [this, next = std::move(next), deadline]() mutable {
            await_native(std::move(next), deadline);
        });
    }

    void module_native() {
        instance_.addonManager().addon("transime")->setConfig(native_config());
        new_context();
        ic_->type("nihao");
        after(40000, [this] {
            require(native_translation().empty(), "native backend bypassed configured debounce");
            pass("native backend debounce keeps event loop responsive");
            await_native([this] {
                const auto translation = native_translation();
                require(translation != "你好" && ic_->commits.empty(),
                        "native result was source echo or an unsolicited commit");
                first_native_translation_ = translation;
                ic_->key("Control_L"); ic_->key("Control+Alt_L");
                require(ic_->key("Control+Alt+Return"), "native translation shortcut was not accepted");
                ic_->key("Control+Alt+Return");
                ic_->key("Control+Alt+Return", true);
                ic_->key("Control+Alt+Alt_L", true); ic_->key("Control+Control_L", true);
                require(ic_->commits == std::vector<std::string>{translation} && ic_->last_preedit.empty(),
                        "native translation was duplicated or left Chinese preedit");
                pass("native process result displays and commits once through real pinyin path");
                native_terminology();
            });
        });
    }

    void native_terminology() {
        auto config = native_config();
        config["ContextAware"].setValue("True");
        instance_.addonManager().addon("transime")->setConfig(config);
        new_context();
        ic_->type("yuhangyuanderenwushiweixiuweixing");
        // Select only prefixes of the real composition; leave the final word
        // pending so the Module translates the complete chosen Chinese source.
        for (const auto *word : {"宇航员", "的", "任务", "是", "维修"}) select(word);
        require(snapshot().source == "宇航员的任务是维修卫星" && ic_->commits.empty(),
                "terminology history phrase was not composed by real pinyin");
        await_native([this] {
            const auto history = native_translation();
            require(history.find("mission") != std::string::npos,
                    "synthetic astronaut history did not establish the expected terminology");
            ic_->key("Control+Alt+Return"); ic_->key("Control+Alt+Return", true);
            require(ic_->commits == std::vector<std::string>{history},
                    "terminology history translation did not traverse the real commit path");
            ic_->type("zhegerenwuyijingwanchengle");
            for (const auto *word : {"这个", "任务", "已经", "完成"}) select(word);
            require(snapshot().source == "这个任务已经完成了",
                    "terminology target was not composed by real pinyin");
            await_native([this, history] {
                const auto translation = native_translation();
                require(translation.find("mission") != std::string::npos &&
                        translation.find("astronaut") == std::string::npos,
                        "recent committed terminology was not applied to the current sentence alone");
                ic_->key("Control+Alt+Return"); ic_->key("Control+Alt+Return", true);
                require(ic_->commits == std::vector<std::string>{history, translation},
                        "contextual translation did not commit once after the real prior translation");
                pass("native terminology follows a real earlier pinyin translation commit");
                native_unavailable();
            });
        });
    }

    void native_unavailable() {
        auto config = native_config();
        config["WorkerScript"].setValue("/nonexistent/transime-synthetic-unavailable-worker.py");
        instance_.addonManager().addon("transime")->setConfig(config);
        new_context(); ic_->type("nihao");
        require(ic_->key("Control+Alt+Return"), "unavailable native hotkey escaped composition");
        ic_->key("Control+Alt+Return", true);
        after(350000, [this] {
            require(native_translation().empty() && ic_->commits.empty(),
                    "failed worker displayed or committed a stale translation");
            require(ic_->inputPanel().auxDown().toString() ==
                        "TransIME：暂时无法翻译，请继续输入中文",
                    "current worker failure did not display the fixed status message");
            require(ic_->key("Control+Alt+Return"), "failure status shortcut escaped composition");
            ic_->key("Control+Alt+Return", true);
            require(ic_->commits.empty(), "worker failure status was submitted as a translation");
            candidate("你好"); ic_->key("space"); ic_->key("space", true);
            require(ic_->commits == std::vector<std::string>{"你好"},
                    "worker failure interrupted normal Chinese input");
            require(ic_->inputPanel().auxDown().empty(), "worker failure status survived Chinese commit");
            pass("unavailable native worker displays non-submittable status and preserves Chinese commit");
            instance_.addonManager().addon("transime")->setConfig(native_config());
            new_context(); ic_->type("nihao");
            after(180000, [this] {
                new_context(); ic_->type("zhongguo");
                await_native([this] {
                    const auto translation = native_translation();
                    require(translation != first_native_translation_ && ic_->commits.empty(),
                            "old native context result appeared in a new context");
                    ic_->key("Control+Alt+Return"); ic_->key("Control+Alt+Return", true);
                    require(ic_->commits == std::vector<std::string>{translation},
                            "latest native target was not committed exactly once");
                    pass("destroyed context cannot receive the earlier native worker result");
                    native_sensitive();
                });
            });
        });
    }

    void native_sensitive() {
        new_context(); ic_->type("nihao");
        after(180000, [this] {
            ic_->setCapabilityFlags(fcitx::CapabilityFlags(fcitx::CapabilityFlag::Preedit) |
                                   fcitx::CapabilityFlag::Password);
            // Fcitx changes IM for Password and stock pinyin can synchronously
            // commit its preedit on deactivation. Preserve that engine behavior
            // while requiring no later result/commit from the native backend.
            const auto transition_commits = ic_->commits;
            for (const auto &text : transition_commits)
                require(text != first_native_translation_, "capability transition committed a ready English result");
            after(300000, [this, transition_commits] {
                require(native_translation().empty() && ic_->commits == transition_commits,
                        "sensitive state received an in-flight native result (aux bytes=" +
                        std::to_string(native_translation().size()) + ", commits=" +
                        std::to_string(ic_->commits.size()) + ")");
                pass("sensitive capability cancels in-flight native translation");
                instance_.addonManager().addon("transime")->setConfig(native_config(false));
                new_context(); ic_->type("nihao");
                after(300000, [this] {
                    require(native_translation().empty(), "disabled native module displayed a result");
                    ic_->key("space"); ic_->key("space", true);
                    require(ic_->commits == std::vector<std::string>{"你好"},
                            "disabled native module prevented Chinese commit");
                    pass("disabled native module preserves ordinary Chinese input");
                    finish();
                });
            });
        });
    }

    std::vector<transime::CommitRecord> fixture_history() {
        auto *module = instance_.addonManager().addon("transime");
        require(module, "test module is absent");
        return module->call<fcitx::ITransime::fixtureHistory>(ic_.get());
    }

    void bridge_snapshot() {
        auto *pinyin = instance_.addonManager().addon("pinyin");
        const auto read = [&] { return pinyin->call<fcitx::IPinyin::transimeSnapshotV1>(ic_.get()); };
        ic_->type("nihao");
        const auto first = read();
        const auto repeat = read();
        require(first.ready && first.source == "你好" && first.selectedText.empty(),
                "bridge did not expose the complete ordinary candidate");
        require(repeat.revision == first.revision && repeat.source == first.source,
                "reading the bridge changed input state");
        select("你");
        const auto partial = read();
        require(partial.ready && partial.source == "你好" && partial.selectedText == "你" &&
                partial.candidateText == "好" && partial.revision != first.revision,
                "bridge lost the already-selected Chinese prefix");
        ic_->key("Left");
        ic_->key("Left", true);
        require(!read().ready, "bridge accepted an incomplete target with a middle cursor");
        ic_->reset();
        require(!read().ready, "bridge returned a ready target after reset");
        pass("explicit pinyin bridge full target, partial selection, and cursor guard");
    }

    using RowSnapshot = fcitx::PinyinTransimeSnapshotV1;

    RowSnapshot row_snapshot(int row) {
        return instance_.addonManager().addon("pinyin")->
            call<fcitx::IPinyin::transimeSnapshotAtV1>(ic_.get(), row);
    }

    bool set_row_comment(int row, RowSnapshot expected, std::string comment) {
        return instance_.addonManager().addon("pinyin")->
            call<fcitx::IPinyin::transimeSetCommentV1>(ic_.get(), row,
                                                     std::move(expected), std::move(comment));
    }

    struct PageBeforeTranslation {
        std::shared_ptr<fcitx::CandidateList> list;
        fcitx::CandidateLayoutHint layout;
        int first_bulk_index;
        std::vector<std::pair<std::string, std::string>> text_comments;
    };

    PageBeforeTranslation remember_page() {
        auto list = ic_->inputPanel().candidateList();
        require(list && list->toBulk() && list->toBulkCursor(),
                "row tests require real pinyin's public bulk candidate interfaces");
        PageBeforeTranslation saved{list, list->layoutHint(),
            list->toBulkCursor()->globalCursorIndex() - list->cursorIndex(), {}};
        require(saved.first_bulk_index >= 0, "page has no selected candidate");
        for (int row = 0; row < list->size(); ++row) {
            const auto &word = list->candidate(row);
            saved.text_comments.emplace_back(word.text().toString(), word.comment().toString());
        }
        return saved;
    }

    void require_restored(const PageBeforeTranslation &saved, const std::string &boundary) {
        require(saved.list->layoutHint() == saved.layout,
                boundary + " did not restore the original candidate layout");
        // Keep the original list alive and use public global indices: paging
        // may reuse that same list while displaying a different page.
        for (std::size_t row = 0; row < saved.text_comments.size(); ++row) {
            const auto &word = saved.list->toBulk()->candidateFromAll(
                saved.first_bulk_index + static_cast<int>(row));
            require(word.text().toString() == saved.text_comments[row].first &&
                    word.comment().toString() == saved.text_comments[row].second,
                    boundary + " left translated comments or changed an original Chinese candidate");
        }
    }

    void bridge_rows() {
        instance_.addonManager().addon("transime")->setConfig(native_config(false));
        new_context(); ic_->type("hanzi");
        const auto before = remember_page();
        const auto cursor = before.list->cursorIndex();
        const auto highlighted = snapshot();
        require(before.list->size() >= 3, "hanzi did not expose at least three candidates");
        for (int row = 0; row < 3; ++row) {
            const auto target = row_snapshot(row);
            const auto repeat = row_snapshot(row);
            require(target.ready && target.selectedText.empty() &&
                    target.candidateText == before.text_comments[row].first &&
                    target.source == target.selectedText + target.candidateText &&
                    target.candidateInputEnd == target.inputBytes,
                    "hanzi row snapshot does not contain the complete original Chinese source");
            require(repeat.revision == target.revision && repeat.source == target.source &&
                    before.list->cursorIndex() == cursor && snapshot().revision == highlighted.revision,
                    "row snapshot read moved the cursor or advanced revision");
            require(set_row_comment(row, target, "synthetic English row"),
                    "bridge refused a matching complete candidate comment");
            require(before.list->candidate(row).comment().toString() == "synthetic English row" &&
                    before.list->candidate(row).text().toString() == target.candidateText &&
                    row_snapshot(row).revision == target.revision &&
                    before.list->cursorIndex() == cursor,
                    "adding a comment changed candidate text, cursor, or revision");
            auto wrong = target;
            wrong.source += "错误";
            require(!set_row_comment(row, wrong, "wrong target") &&
                    !set_row_comment(row, wrong, ""),
                    "bridge accepted an incorrect source for write or restore");
            wrong = target; ++wrong.revision;
            require(!set_row_comment(row, wrong, "wrong revision"),
                    "bridge accepted a stale revision");
            wrong = target; ++wrong.candidateIndex;
            require(!set_row_comment(row, wrong, "wrong index"),
                    "bridge accepted a mismatched candidate index");
            wrong = target; ++wrong.candidateInputEnd;
            require(!set_row_comment(row, wrong, "wrong offset"),
                    "bridge accepted a mismatched input offset");
            for (const std::string &invalid : {std::string("line\nbreak"),
                    std::string("English\0comment", 15), std::string(4097, 'x'),
                    std::string("\xC0\xAF"), std::string("English\xE2\x80\xAE")}) {
                require(!set_row_comment(row, target, invalid),
                        "bridge accepted control characters or malformed UTF-8 in a comment");
            }
            require(set_row_comment(row, target, ""), "bridge could not restore its original comment");
        }
        require_restored(before, "bridge restore");
        require(!row_snapshot(-1).ready && !row_snapshot(before.list->size()).ready,
                "bridge accepted an out-of-page row");
        pass("three real hanzi rows: complete source, read-only snapshots, exact guarded comment restore");

        const auto stale = row_snapshot(0);
        ic_->key("Down"); ic_->key("Down", true);
        require(snapshot().source != highlighted.source && before.list->cursorIndex() == cursor + 1,
                "Down did not move one candidate under the isolated row-test bindings");
        ic_->key("Up"); ic_->key("Up", true);
        require(snapshot().source == highlighted.source && snapshot().revision != stale.revision &&
                !set_row_comment(0, stale, "late A response"),
                "A to B to A accepted an old row comment");
        const auto current = row_snapshot(0);
        ic_->key("Left"); ic_->key("Left", true);
        require(!row_snapshot(0).ready && !set_row_comment(0, current, "middle cursor"),
                "row bridge accepted a middle-cursor composition");
        pass("row bridge rejects candidate A-B-A responses and middle-cursor targets");

        ic_->reset(); // Do not learn a half-edited source via focus-out commit.
        new_context(); ic_->type("hanzi"); select("汉");
        require(ic_->commits.empty(), "selecting a Chinese prefix committed prematurely");
        for (int row = 0; row < 3; ++row) {
            const auto target = row_snapshot(row);
            require(target.ready && target.selectedText == "汉" &&
                    target.candidateText == ic_->inputPanel().candidateList()->candidate(row).text().toString() &&
                    target.source == "汉" + target.candidateText,
                    "row source lost the selected Chinese prefix");
        }
        int prefix_row = -1;
        for (int row = 0; row < ic_->inputPanel().candidateList()->size(); ++row)
            if (row_snapshot(row).candidateText == "字") { prefix_row = row; break; }
        require(prefix_row >= 0, "selected-prefix test needs the ordinary Chinese word 汉字");
        const auto target = row_snapshot(prefix_row);
        require(set_row_comment(prefix_row, target, "synthetic selected-prefix English"),
                "could not annotate a selected-prefix candidate");
        ic_->inputPanel().candidateList()->candidate(prefix_row).select(ic_.get());
        require(ic_->commits == std::vector<std::string>{target.source},
                "comment changed direct candidate selection or appended English to Chinese");
        pass("selected-prefix row source and direct Chinese selection remain unchanged by comments");
        ic_->reset();
    }

    fcitx::RawConfig rows_config(bool enabled = true) const {
        auto config = native_config(enabled);
        config["CandidateTranslations"].setValue("True");
        config["EnableHistory"].setValue("False");
        return config;
    }

    static bool has_english_letter(const std::string &text) {
        for (const unsigned char value : text)
            if ((value >= 'a' && value <= 'z') || (value >= 'A' && value <= 'Z')) return true;
        return false;
    }

    void await_rows(Action next, std::uint64_t deadline = 0) {
        auto list = ic_->inputPanel().candidateList();
        bool ready = list && list->size() >= 3;
        if (ready) {
            for (int row = 0; row < 3; ++row) {
                const auto target = row_snapshot(row);
                const auto comment = list->candidate(row).comment().toString();
                if (!target.ready || !has_english_letter(comment)) ready = false;
            }
        }
        if (ready) {
            require(list->layoutHint() == fcitx::CandidateLayoutHint::Vertical,
                    "translated candidate page was not vertical");
            require(ic_->inputPanel().auxDown().empty(),
                    "candidate comments also emitted a duplicate auxiliary translation");
            require(ic_->commits.empty(), "candidate translation committed without user selection");
            next(); return;
        }
        if (!deadline) deadline = fcitx::now(CLOCK_MONOTONIC) + 15000000;
        require(fcitx::now(CLOCK_MONOTONIC) < deadline,
                "first three complete hanzi rows did not receive English comments within 15 seconds");
        after(10000, [this, next = std::move(next), deadline]() mutable {
            await_rows(std::move(next), deadline);
        });
    }

    void module_rows() {
        new_context(); ic_->type("hanzi");
        const auto original = remember_page();
        instance_.addonManager().addon("transime")->setConfig(rows_config());
        await_rows([this, original] {
            const auto list = ic_->inputPanel().candidateList();
            for (int row = 0; row < 3; ++row) {
                const auto target = row_snapshot(row);
                require(list->candidate(row).text().toString() == original.text_comments[row].first,
                        "module replaced the original candidate text");
                std::cout << "SYNTHETIC_ROW " << row << " source=" << std::quoted(target.source)
                          << " text=" << std::quoted(list->candidate(row).text().toString())
                          << " comment=" << std::quoted(list->candidate(row).comment().toString()) << '\n';
            }
            pass("native worker translates three original hanzi candidates into vertical comments without aux duplication");
            const auto before = snapshot();
            ic_->key("Down"); ic_->key("Down", true);
            require(snapshot().source != before.source && list->cursorIndex() == 1,
                    "Down changed page instead of highlighting the next original Chinese candidate");
            require(ic_->key("Control+Alt+Return"), "candidate-movement hotkey escaped the composition");
            ic_->key("Control+Alt+Return", true);
            require(ic_->commits.empty(), "candidate movement allowed an old translation to commit");
            require_restored(original, "candidate movement");
            await_rows([this] {
                const auto list = ic_->inputPanel().candidateList();
                require(list->cursorIndex() == 1, "background row translations moved the highlighted candidate");
                const auto translation = list->candidate(1).comment().toString();
                ic_->key("Control_L"); ic_->key("Control+Alt_L");
                require(ic_->key("Control+Alt+Return"), "current row translation shortcut was not accepted");
                ic_->key("Control+Alt+Return", true);
                ic_->key("Control+Alt+Alt_L", true); ic_->key("Control+Control_L", true);
                require(ic_->commits == std::vector<std::string>{translation} && ic_->last_preedit.empty(),
                        "shortcut did not submit exactly the highlighted row's English comment");
                pass("Up/Down candidate bindings invalidate old English; shortcut submits only the newly highlighted row");
                rows_chinese_selection(0);
            });
        });
    }

    void rows_chinese_selection(int mode) {
        new_context(); ic_->type("hanzi");
        await_rows([this, mode] {
            const auto target = row_snapshot(mode);
            if (mode == 0) { ic_->key("space"); ic_->key("space", true); }
            else if (mode == 1) { ic_->key("2"); ic_->key("2", true); }
            else ic_->inputPanel().candidateList()->candidate(mode).select(ic_.get());
            require(ic_->commits == std::vector<std::string>{target.source},
                    "space/number/direct selection submitted English or changed Chinese selection");
            pass(std::string("translated comments preserve Chinese selection by ") +
                 (mode == 0 ? "space" : mode == 1 ? "number" : "candidate.select"));
            if (mode < 2) rows_chinese_selection(mode + 1);
            else rows_boundary(0);
        });
    }

    void rows_boundary(int mode) {
        instance_.addonManager().addon("transime")->setConfig(rows_config());
        new_context(); ic_->type("hanzi");
        const auto original = remember_page();
        await_rows([this, original, mode] {
            const auto source = row_snapshot(0).source;
            if (mode == 0) {
                auto *page = original.list->toPageable();
                require(page && page->hasNext(), "hanzi has no second page for cancellation test");
                const auto before = page->currentPage();
                ic_->key("Page_Down"); ic_->key("Page_Down", true);
                require(page->currentPage() == before + 1, "PageDown did not change candidate page");
                require_restored(original, "page away");
                ic_->key("Page_Up"); ic_->key("Page_Up", true);
                require(row_snapshot(0).source == source, "PageUp did not return to the original page");
                require_restored(original, "page return before new requests");
            } else if (mode == 1) {
                ic_->focusOut();
                require_restored(original, "focus out");
            } else if (mode == 2) {
                ic_->type("hao");
                require(row_snapshot(0).source != source, "synthetic input did not change the translation target");
                require_restored(original, "composition change");
            } else if (mode == 3) {
                auto *page = original.list->toPageable();
                require(page && page->hasNext(), "hanzi has no second page for direct UI paging");
                const auto before = page->currentPage();
                // Public UI actions can change the page BEFORE the Module sees
                // updateUI; no key event has a chance to clear the old page.
                page->next();
                ic_->updateUserInterface(fcitx::UserInterfaceComponent::InputPanel);
                require(page->currentPage() == before + 1, "direct UI next did not change page");
                require_restored(original, "direct UI page away");
                page->prev();
                ic_->updateUserInterface(fcitx::UserInterfaceComponent::InputPanel);
                require(page->currentPage() == before && row_snapshot(0).source == source,
                        "direct UI previous did not return to the original Chinese page");
                require_restored(original, "direct UI page return");
                ic_->key("Control+Alt+Return"); ic_->key("Control+Alt+Return", true);
                require(ic_->commits.empty(), "direct page away/back allowed stale English submission");
            } else {
                instance_.addonManager().addon("transime")->setConfig(rows_config(false));
                require_restored(original, "disable");
                ic_->key("space"); ic_->key("space", true);
                require(ic_->commits == std::vector<std::string>{source},
                        "disabling row translations blocked ordinary Chinese space selection");
            }
            // Stop replacement requests after checking the immediate boundary.
            // A worker response still in flight must not redecorate the old page.
            instance_.addonManager().addon("transime")->setConfig(rows_config(false));
            const auto boundary_commits = ic_->commits;
            after(350000, [this, original, mode, boundary_commits] {
                require_restored(original, "late response after row cancellation");
                require(ic_->commits == boundary_commits && ic_->inputPanel().auxDown().empty(),
                        "cancelled row work committed text or added auxiliary English later");
                pass(std::string("row comments/layout restore and late responses stay cancelled on ") +
                     (mode == 0 ? "page away and back" : mode == 1 ? "focus out" :
                      mode == 2 ? "input change" : mode == 3 ? "direct UI page away and back" : "disable"));
                if (mode < 4) rows_boundary(mode + 1);
                else finish();
            });
        });
    }

    std::string punctuation_observation() {
        const auto target = snapshot();
        const auto list = ic_->inputPanel().candidateList();
        std::ostringstream output;
        output << "commits=[";
        for (std::size_t index = 0; index < ic_->commits.size(); ++index) {
            if (index) output << ',';
            output << std::quoted(ic_->commits[index]);
        }
        output << "] preedit=" << std::quoted(ic_->inputPanel().preedit().toString())
               << " client_preedit=" << std::quoted(ic_->inputPanel().clientPreedit().toString())
               << " candidates=" << (list ? list->size() : 0)
               << " snapshot_ready=" << target.ready << " snapshot_reason=" << std::quoted(target.reason)
               << " snapshot_source=" << std::quoted(target.source)
               << " candidate_texts=[";
        if (list) for (int row = 0; row < list->size(); ++row) {
            if (row) output << ',';
            output << std::quoted(list->candidate(row).text().toString());
        }
        output << ']';
        return output.str();
    }

    void await_highlight_comment(Action next, std::uint64_t deadline = 0) {
        const auto list = ic_->inputPanel().candidateList();
        if (list && list->cursorIndex() >= 0 &&
            has_english_letter(list->candidate(list->cursorIndex()).comment().toString())) {
            next(); return;
        }
        if (!deadline) deadline = fcitx::now(CLOCK_MONOTONIC) + 15000000;
        require(fcitx::now(CLOCK_MONOTONIC) < deadline,
                "punctuation diagnostic could not establish a ready native translation first");
        after(10000, [this, next = std::move(next), deadline]() mutable {
            await_highlight_comment(std::move(next), deadline);
        });
    }

    void punctuation_case(int index) {
        const bool enabled = index % 2 == 1;
        const std::string punctuation = std::vector<std::string>{",", ".", ";"}[index / 2];
        instance_.addonManager().addon("transime")->setConfig(rows_config(enabled));
        new_context(); ic_->type("nihao");
        auto observe = [this, index, enabled, punctuation] {
            require(snapshot().ready && snapshot().source == "你好",
                    "punctuation diagnostic did not start with the same Chinese source");
            std::vector<std::string> observations;
            ic_->key(punctuation); ic_->key(punctuation, true);
            observations.push_back(punctuation_observation());
            for (const char letter : std::string("shijie")) {
                const std::string key(1, letter);
                // Diagnostics record native forwarding/quickphrase behavior;
                // punctuation may legitimately leave the normal pinyin path.
                ic_->key(key); ic_->key(key, true);
            }
            observations.push_back(punctuation_observation());
            for (std::size_t stage = 0; stage < observations.size(); ++stage)
                std::cout << "PUNCTUATION key=" << std::quoted(punctuation) << " enabled=" << enabled
                          << " stage=" << (stage == 0 ? "after-punctuation" : "after-shijie")
                          << ' ' << observations[stage] << '\n';
            if (!enabled) punctuation_without_plugin_ = observations;
            else {
                require(observations == punctuation_without_plugin_,
                        "plugin enabled/disabled changed synthetic native punctuation input behavior");
                pass("native punctuation behavior matches plugin off/on for " + punctuation);
            }
            if (index < 5) after(20000, [this, index] { punctuation_case(index + 1); });
            else finish();
        };
        if (enabled) await_highlight_comment(std::move(observe));
        else after(20000, std::move(observe));
    }

    fcitx::RawConfig punctuation_config(const std::string &synthetic = "") const {
        auto config = rows_config();
        config["AutoCommitOnPunctuation"].setValue("True");
        if (!synthetic.empty()) {
            const auto script = std::filesystem::path(root_) / ("synthetic-" + synthetic + "-worker.py");
            require(std::filesystem::is_regular_file(script), "isolated synthetic punctuation worker is absent");
            config["WorkerScript"].setValue(script.string());
        }
        return config;
    }

    static std::string english_with_punctuation(std::string translation, const std::string &punctuation) {
        while (!translation.empty() && std::string(".,!?;:").find(translation.back()) != std::string::npos)
            translation.pop_back();
        return translation + punctuation;
    }

    void await_commits(std::size_t count, Action next, std::uint64_t deadline = 0) {
        if (ic_->commits.size() >= count) { next(); return; }
        if (!deadline) deadline = fcitx::now(CLOCK_MONOTONIC) + 15000000;
        require(fcitx::now(CLOCK_MONOTONIC) < deadline,
                "punctuation output did not complete before its deadline (commits=" +
                std::to_string(ic_->commits.size()) + ")");
        after(10000, [this, count, next = std::move(next), deadline]() mutable {
            await_commits(count, std::move(next), deadline);
        });
    }

    void require_only_commits(const std::vector<std::string> &expected, const std::string &stage) {
        if (ic_->commits != expected) {
            std::cerr << "SYNTHETIC_UNEXPECTED_COMMITS stage=" << std::quoted(stage) << " actual=";
            for (const auto &value : ic_->commits) std::cerr << std::quoted(value) << ' ';
            std::cerr << '\n';
        }
        require(ic_->commits == expected, stage + " changed, duplicated, or reordered committed text");
        require(ic_->forwarded_presses.empty(), stage + " leaked queued printable keys to the frontend");
    }

    void punctuation_commit_start() {
        instance_.addonManager().addon("transime")->setConfig(punctuation_config());
        punctuation_ready_case(0);
    }

    void punctuation_ready_case(std::size_t index) {
        // Every ASCII punctuation/symbol key, including apostrophe, brackets,
        // slash and semicolon that otherwise have special pinyin behavior.
        const std::string punctuation = "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~";
        require(punctuation.size() == 32, "test does not cover all 32 ASCII punctuation keys");
        if (index == punctuation.size()) {
            pass("all 32 ASCII punctuation keys commit ready native English once and preserve vertical candidate display");
            punctuation_shifted(); return;
        }
        new_context(); ic_->type("nihao");
        await_highlight_comment([this, index, punctuation] {
            const auto list = ic_->inputPanel().candidateList();
            require(list && list->layoutHint() == fcitx::CandidateLayoutHint::Vertical,
                    "automatic punctuation disabled the requested vertical candidate layout");
            const auto translation = list->candidate(list->cursorIndex()).comment().toString();
            if (index == 0) first_native_translation_ = translation;
            const std::string key(1, punctuation[index]);
            const auto expected = english_with_punctuation(translation, key);
            require(ic_->key(key), "ready punctuation press escaped the input method");
            require(ic_->key(key, false, true), "punctuation autorepeat escaped the one-press guard");
            ic_->key(key, true);
            await_commits(1, [this, index, key, expected] {
                require_only_commits({expected}, "ready punctuation " + key);
                require(ic_->last_preedit.empty() && !snapshot().ready,
                        "ready punctuation left pending Chinese or raw pinyin");
                std::cout << "SYNTHETIC_PUNCTUATION_COMMIT key=" << std::quoted(key)
                          << " committed=" << std::quoted(expected) << '\n';
                after(10000, [this, index, expected] {
                    require_only_commits({expected}, "punctuation release/repeat completion");
                    punctuation_ready_case(index + 1);
                });
            });
        });
    }

    void punctuation_shifted() {
        new_context(); ic_->type("nihao");
        await_highlight_comment([this] {
            const auto list = ic_->inputPanel().candidateList();
            const auto expected = english_with_punctuation(list->candidate(list->cursorIndex()).comment().toString(), "!");
            ic_->key("Shift_L");
            require(ic_->key("Shift+exclam"), "real Shift-modified punctuation was not handled");
            ic_->key("Shift+exclam", true); ic_->key("Shift+Shift_L", true);
            await_commits(1, [this, expected] {
                require_only_commits({expected}, "Shift-modified punctuation");
                pass("Shift-modified exclamation commits English without requiring the translation shortcut");
                punctuation_unicode(0);
            });
        });
    }

    void punctuation_unicode(int index) {
        const std::vector<std::pair<std::uint32_t, std::string>> cases{{0x3002, "."}, {0xff1f, "?"}};
        new_context(); ic_->type("nihao");
        await_highlight_comment([this, index, cases] {
            const auto list = ic_->inputPanel().candidateList();
            const auto expected = english_with_punctuation(list->candidate(list->cursorIndex()).comment().toString(),
                                                            cases[index].second);
            require(ic_->unicode_key(cases[index].first), "full-width punctuation was not accepted");
            ic_->unicode_key(cases[index].first, true);
            await_commits(1, [this, index, expected] {
                require_only_commits({expected}, "full-width punctuation normalization");
                if (index == 0) punctuation_unicode(1);
                else {
                    pass("full-width Chinese period and question mark normalize to English punctuation");
                    punctuation_separate_press();
                }
            });
        });
    }

    void punctuation_separate_press() {
        new_context(); ic_->type("nihao");
        await_highlight_comment([this] {
            const auto list = ic_->inputPanel().candidateList();
            const auto expected = english_with_punctuation(list->candidate(list->cursorIndex()).comment().toString(), ",");
            ic_->key(","); ic_->key(",", true);
            await_commits(1, [this, expected] {
                // An independent second press has no composition: native pinyin
                // punctuation remains responsible for it, rather than repeat filtering.
                ic_->key(","); ic_->key(",", true);
                require_only_commits({expected, "，"}, "independent second punctuation press");
                pass("second independent punctuation press is preserved after the first release");
                punctuation_native_queue();
            });
        });
    }

    void punctuation_native_queue() {
        new_context(); ic_->type("shijie");
        await_highlight_comment([this] {
            require(snapshot().source == "世界", "native queue needs the same synthetic world source");
            const auto list = ic_->inputPanel().candidateList();
            const auto world = list->candidate(list->cursorIndex()).comment().toString();
            ic_->reset();
            new_context(); ic_->type("nihao");
            require(ic_->inputPanel().candidateList()->candidate(0).comment().empty(),
                    "native immediate-punctuation test unexpectedly started with a ready comment");
            ic_->key(","); ic_->key(",", true);
            ic_->type("shijie"); ic_->key("."); ic_->key(".", true);
            require(ic_->commits.empty(), "native immediate punctuation fell through to Chinese before translation");
            const std::vector<std::string> expected{
                english_with_punctuation(first_native_translation_, ","), english_with_punctuation(world, ".")};
            await_commits(2, [this, expected] {
                require_only_commits(expected, "native two-sentence queued input");
                require(ic_->last_preedit.empty(), "native queued input left raw pinyin pending");
                pass("real worker translates immediate punctuation and queued next sentence in input order");
                punctuation_delayed_queue();
            });
        });
    }

    void punctuation_delayed_queue() {
        instance_.addonManager().addon("transime")->setConfig(punctuation_config("delay"));
        new_context(); ic_->type("nihao");
        ic_->key(","); ic_->key(",", false, true); ic_->key(",", true);
        ic_->type("shijie"); ic_->key("."); ic_->key(".", false, true); ic_->key(".", true);
        require(ic_->commits.empty(), "synthetic delayed punctuation committed before its response");
        after(100000, [this] {
            require(ic_->commits.empty(), "synthetic 400ms worker was not actually delaying output");
            await_commits(2, [this] {
                require_only_commits({"Hello,", "World."}, "synthetic delayed FIFO");
                require(ic_->last_preedit.empty(), "synthetic delayed FIFO left unconsumed pinyin");
                pass("synthetic delayed worker preserves two queued sentences and swallows releases/autorepeats");
                punctuation_overflow();
            });
        });
    }

    void punctuation_overflow() {
        instance_.addonManager().addon("transime")->setConfig(punctuation_config("delay"));
        new_context(); ic_->type("nihao"); ic_->key(","); ic_->key(",", true);
        constexpr std::size_t spaces = 90;
        // 180 following events exceed the documented 128-event FIFO. Spaces
        // reach the synthetic frontend either through replay's forwardKey or
        // as unhandled original keys after fallback; count both paths.
        for (std::size_t index = 0; index < spaces; ++index) {
            ic_->key("space"); ic_->key("space", true);
        }
        ic_->type("shijie"); ic_->key("space"); ic_->key("space", true);
        // The tail is replayed asynchronously. Check at the observable third
        // commit, after the preceding spaces must have reached the frontend.
        await_commits(3, [this] {
            std::size_t delivered_spaces = 0;
            for (const auto &key : ic_->forwarded_presses) if (key == "space") ++delivered_spaces;
            for (const auto &key : ic_->unhandled_presses) if (key == "space") ++delivered_spaces;
            for (const auto &commit : ic_->commits)
                if (commit.find_first_not_of(' ') == std::string::npos) delivered_spaces += commit.size();
            if (delivered_spaces != spaces) {
                std::cerr << "SYNTHETIC_FIFO delivered_spaces=" << delivered_spaces << " expected=" << spaces
                          << " forwarded=";
                for (const auto &key : ic_->forwarded_presses) std::cerr << std::quoted(key) << ' ';
                std::cerr << " unhandled=";
                for (const auto &key : ic_->unhandled_presses) std::cerr << std::quoted(key) << ' ';
                std::cerr << " commits=";
                for (const auto &value : ic_->commits) std::cerr << std::quoted(value) << ' ';
                std::cerr << '\n';
            }
            require(delivered_spaces == spaces, "FIFO overflow lost or duplicated subsequent space key presses");
            require(ic_->commits == std::vector<std::string>{"你好", "，", "世界"},
                    "FIFO overflow failed to restore native Chinese and subsequent composition");
            after(700000, [this] {
                require(ic_->commits == std::vector<std::string>{"你好", "，", "世界"},
                        "overflow's cancelled translation committed after native recovery");
                pass("128-event FIFO overflow preserves all 90 subsequent spaces and the next Chinese composition");
                punctuation_repeat_overflow();
            });
        });
    }

    void punctuation_repeat_overflow() {
        instance_.addonManager().addon("transime")->setConfig(punctuation_config("delay"));
        new_context(); ic_->type("nihao"); ic_->key(",");
        // Put the original punctuation's own repeat/release at the capacity
        // boundary. Native fallback may repeat punctuation; it must not leave
        // the following normal Chinese input behind a stale held-key guard.
        for (int index = 0; index < 129; ++index) ic_->key(",", false, true);
        ic_->key(",", true);
        ic_->type("shijie"); ic_->key("space"); ic_->key("space", true);
        await_repeat_overflow();
    }

    void await_repeat_overflow(std::uint64_t deadline = 0) {
        if (!ic_->commits.empty() && ic_->commits.back() == "世界") {
            require(ic_->commits.front() == "你好" && ic_->last_preedit.empty(),
                    "repeat-boundary fallback changed the original or subsequent Chinese composition");
            for (std::size_t index = 1; index + 1 < ic_->commits.size(); ++index)
                require(ic_->commits[index] == "，" || ic_->commits[index] == ",",
                        "repeat-boundary fallback inserted an unexpected English or raw-pinyin commit");
            const auto completed = ic_->commits;
            after(700000, [this, completed] {
                require(ic_->commits == completed,
                        "repeat/release overflow left an automatic English transaction alive");
                pass("punctuation repeat/release at FIFO capacity recovers native input without a stuck key or late English");
                punctuation_nested_overflow();
            });
            return;
        }
        if (!deadline) deadline = fcitx::now(CLOCK_MONOTONIC) + 15000000;
        require(fcitx::now(CLOCK_MONOTONIC) < deadline,
                "repeat/release overflow prevented subsequent Chinese input from completing");
        after(10000, [this, deadline] { await_repeat_overflow(deadline); });
    }

    std::size_t observed_spaces() const {
        std::size_t count = 0;
        for (const auto &key : ic_->forwarded_presses) if (key == "space") ++count;
        for (const auto &key : ic_->unhandled_presses) if (key == "space") ++count;
        for (const auto &commit : ic_->commits)
            if (commit.find_first_not_of(' ') == std::string::npos) count += commit.size();
        return count;
    }

    void punctuation_nested_overflow() {
        instance_.addonManager().addon("transime")->setConfig(punctuation_config("delay"));
        new_context(); ic_->type("nihao"); ic_->key(","); ic_->key(",", true);
        ic_->type("shijie"); ic_->key("."); ic_->key(".", true);
        // The queued second sentence is BEFORE the event which exhausts the
        // FIFO. Synchronous fallback runs inside that event's commit blocker.
        for (int index = 0; index < 90; ++index) { ic_->key("space"); ic_->key("space", true); }
        await_nested_overflow();
    }

    void await_nested_overflow(std::uint64_t deadline = 0) {
        if (observed_spaces() >= 90) {
            require(observed_spaces() == 90 &&
                    ic_->commits == std::vector<std::string>{"你好", "，", "世界", "。"},
                    "nested overflow reordered/dropped punctuation, a sentence, or subsequent spaces");
            after(700000, [this] {
                require(observed_spaces() == 90 &&
                        ic_->commits == std::vector<std::string>{"你好", "，", "世界", "。"},
                        "blocked earlier native commits damaged a later replayed sentence");
                pass("overflow with a second punctuated sentence already queued preserves both native sentences and all 90 spaces");
                punctuation_cancel(0);
            });
            return;
        }
        if (!deadline) deadline = fcitx::now(CLOCK_MONOTONIC) + 2000000;
        require(fcitx::now(CLOCK_MONOTONIC) < deadline,
                "nested overflow did not deliver every queued space (delivered=" +
                std::to_string(observed_spaces()) + ")");
        after(10000, [this, deadline] { await_nested_overflow(deadline); });
    }

    void punctuation_expiry() {
        auto config = punctuation_config("delay");
        config["EnableHistory"].setValue("True");
        config["FixtureHistoryLifetimeMilliseconds"].setValue("80");
        instance_.addonManager().addon("transime")->setConfig(config);
        new_context();
        ic_->commitString("synthetic prior history");
        require(fixture_history().size() == 1,
                "short TTL test requires a fixture-enabled addon with native worker mode selected");
        ic_->commits.clear();
        ic_->type("nihao"); ic_->key(","); ic_->key(",", true); ic_->type("shijie");
        const auto deadline = fcitx::now(CLOCK_MONOTONIC) + 1000000;
        // Timer callbacks can be coalesced into the same event-loop iteration.
        // Expiry queues native replay; wait for that replay instead of assuming
        // it must already have run when this observation timer is dispatched.
        after(200000, [this, deadline] {
            await_commits(2, [this] {
                std::cout << "SYNTHETIC_TTL history_size=" << fixture_history().size()
                          << ' ' << punctuation_observation() << '\n';
                require_only_commits({"你好", "，"}, "history expiry during automatic punctuation");
                require(snapshot().ready && snapshot().source == "世界",
                        "automatic history expiry discarded queued subsequent pinyin");
                for (const auto &entry : fixture_history())
                    require(entry.committed != "synthetic prior history", "expired history was retained");
                ic_->key("space"); ic_->key("space", true);
                require_only_commits({"你好", "，", "世界"}, "Chinese continuation after automatic expiry");
                after(600000, [this] {
                    require_only_commits({"你好", "，", "世界"}, "late worker response after automatic history expiry");
                    pass("80ms history expiry during 400ms worker wait replays native punctuation and preserves queued Chinese input");
                    finish();
                });
            }, deadline);
        });
    }

    void punctuation_cancel(int mode) {
        instance_.addonManager().addon("transime")->setConfig(punctuation_config("delay"));
        new_context(); ic_->type("nihao"); ic_->key(","); ic_->key(",", true);
        ic_->type("shijie");
        if (mode == 0) {
            ic_->focusOut();
            // Native pinyin may commit Chinese on focus-out; cancellation must
            // never replay the saved input or English into a replacement IC.
            new_context();
        } else if (mode == 1) {
            ic_->setCapabilityFlags(fcitx::CapabilityFlags(fcitx::CapabilityFlag::Preedit) |
                                   fcitx::CapabilityFlag::Password);
        } else if (mode == 2) {
            ic_->key("Escape"); ic_->key("Escape", true);
            require(ic_->commits.empty() && ic_->last_preedit.empty(),
                    "Escape failed to cancel the original composition and queued input");
        } else if (mode == 3) {
            ic_->reset();
            require(ic_->commits.empty(), "explicit reset committed an automatic English intent");
        } else {
            instance_.addonManager().addon("transime")->setConfig(rows_config(false));
            ic_->key("space"); ic_->key("space", true);
            require_only_commits({"你好"}, "disable during punctuation wait preserves Chinese composition");
        }
        const auto boundary_commits = ic_->commits;
        after(700000, [this, mode, boundary_commits] {
            require(ic_->commits == boundary_commits,
                    "cancelled punctuation replayed or committed after a boundary");
            require(ic_->inputPanel().auxDown().empty(), "cancelled punctuation left a stale translation status");
            pass(std::string("pending punctuation/FIFO cancels on ") +
                 (mode == 0 ? "focus replacement" : mode == 1 ? "sensitive capability" :
                  mode == 2 ? "Escape" : mode == 3 ? "explicit reset" : "disable"));
            if (mode < 4) punctuation_cancel(mode + 1);
            else punctuation_failure();
        });
    }

    void punctuation_failure() {
        instance_.addonManager().addon("transime")->setConfig(punctuation_config("fail"));
        new_context(); ic_->type("nihao"); ic_->key(","); ic_->key(",", true);
        ic_->type("shijie"); ic_->key("."); ic_->key(".", true);
        await_commits(3, [this] {
            require_only_commits({"你好", "，", "World."}, "failed first translation native fallback then next sentence");
            require(ic_->last_preedit.empty(), "failure fallback lost or stranded queued pinyin");
            after(100000, [this] {
                require_only_commits({"你好", "，", "World."}, "late failure/queue completion");
                pass("synthetic backend failure replays native Chinese punctuation and preserves the next queued sentence");
                punctuation_timeout();
            });
        });
    }

    void punctuation_timeout() {
        auto config = punctuation_config("timeout");
        config["RequestTimeoutMilliseconds"].setValue("100");
        config["ColdStartTimeoutMilliseconds"].setValue("1000");
        instance_.addonManager().addon("transime")->setConfig(config);
        new_context(); ic_->type("nihao"); ic_->key(","); ic_->key(",", true);
        ic_->type("shijie"); ic_->key("."); ic_->key(".", true);
        after(200000, [this] {
            require(ic_->commits.empty(), "timeout test fell back before its configured cold deadline");
            await_commits(3, [this] {
                require_only_commits({"你好", "，", "World."}, "worker timeout then queued next sentence");
                after(900000, [this] {
                    require_only_commits({"你好", "，", "World."}, "late response beyond timed-out request");
                    require(ic_->last_preedit.empty(), "timeout recovery left queued raw pinyin");
                    pass("actual request deadline falls back natively, preserves next sentence and rejects the late first result");
                    // Leave a running worker for real addon-destruction coverage.
                    finish();
                });
            });
        });
    }

    void module_normal_chinese() {
        ic_->commits.clear();
        ic_->type("zhongguo");
        candidate("中国");
        ic_->key("space");
        ic_->key("space", true);
        require(ic_->commits == std::vector<std::string>{"中国"},
                "enabled translation module interfered with ordinary Chinese commit");
        auto history = fixture_history();
        require(history.size() == 2 && !history.back().source &&
                history.back().committed == "中国",
                "ordinary Chinese commit history was missing or mispaired");
        ic_->commitStringWithCursor("synthetic cursor context", 10);
        history = fixture_history();
        require(history.size() == 3 && history.back().committed == "synthetic cursor context" &&
                !history.back().source, "cursor commit event did not enter history exactly once");
        require(ic_->key("Control+Alt+BackSpace"), "clear-history shortcut was not accepted");
        ic_->key("Control+Alt+BackSpace", true);
        require(fixture_history().empty(), "clear-history shortcut left retained records");
        pass("normal Chinese input while translation module is enabled");
        pass("ordinary/cursor commit history and explicit history clearing");
        module_aux_conflict();
    }

    void module_aux_conflict() {
        ic_->reset();
        ic_->commits.clear();
        ic_->type("nihao");
        ic_->inputPanel().setAuxDown(fcitx::Text("other addon auxiliary text"));
        ic_->updateUserInterface(fcitx::UserInterfaceComponent::InputPanel);
        after(60000, [this] {
            require(ic_->inputPanel().auxDown().toString() == "other addon auxiliary text",
                    "module overwrote another addon's auxiliary text");
            require(ic_->key("Control+Alt+Return"), "unavailable translation shortcut escaped composition");
            ic_->key("Control+Alt+Return", true);
            require(ic_->commits.empty(), "unavailable translation committed text");
            candidate("你好");
            pass("auxiliary conflict preserves composition and blocks unavailable translation commit");
            ic_->reset();
            module_cursor_cycle();
        });
    }

    void module_cursor_cycle() {
        ic_->type("nihao");
        await_fixture("Hello.", [this] {
            const auto list = ic_->inputPanel().candidateList();
            auto *cursor = list->toBulkCursor();
            require(cursor, "pinyin list lacks the bulk cursor used by UI selection");
            const auto original = cursor->globalCursorIndex();
            const auto alternative = candidate("你");
            require(original != alternative, "cursor-cycle candidate is not distinct");
            cursor->setGlobalCursorIndex(alternative);
            ic_->updateUserInterface(fcitx::UserInterfaceComponent::InputPanel);
            cursor->setGlobalCursorIndex(original);
            ic_->updateUserInterface(fcitx::UserInterfaceComponent::InputPanel);
            require(ic_->key("Control+Alt+Return"), "A-B-A pending shortcut escaped composition");
            ic_->key("Control+Alt+Return", true);
            require(ic_->commits.empty(), "external cursor A-B-A reused a stale ready translation");
            candidate("你好");
            await_fixture("Hello.", [this] {
                pass("external UI cursor A-B-A invalidates a ready translation before reuse");
                module_stale_response();
            });
        });
    }

    void module_stale_response() {
        ic_->reset();
        ic_->commits.clear();
        ic_->type("nihao");
        after(5000, [this] {
        ic_->reset();
        ic_->type("zhongguo");
        await_fixture("China.", [this] {
            require(ic_->inputPanel().auxDown().toString().find("Hello.") == std::string::npos,
                    "old fixture result replaced the current candidate result");
            ic_->key("Control+Alt+Return");
            ic_->key("Control+Alt+Alt_L", true);
            ic_->key("Control+Control_L", true);
            require(ic_->key("Return", true), "modifier-first Return release was not swallowed");
            require(ic_->commits == std::vector<std::string>{"China."},
                    "shortcut committed a stale candidate translation");
            pass("candidate reset rejects an earlier fixture response");
            module_destroyed_context();
        });
        });
    }

    void module_destroyed_context() {
        ic_->type("nihao");
        after(5000, [this] {
        new_context(); // Destroys the previous IC while translation may be pending.
        ic_->type("zhongguo");
        await_fixture("China.", [this] {
            require(ic_->commits.empty(), "old context result committed into the new input context");
            pass("destroyed input context does not receive a late fixture result");
            ic_->reset();
            ic_->setCapabilityFlags(fcitx::CapabilityFlags(fcitx::CapabilityFlag::Preedit) |
                                   fcitx::CapabilityFlag::Password);
            for (char key : std::string("nihao")) {
                ic_->key(std::string(1, key));
                ic_->key(std::string(1, key), true);
            }
            after(300000, [this] {
                require(ic_->inputPanel().auxDown().toString().find("fixture") == std::string::npos,
                        "fixture translation appeared in a password-marked context");
                require(ic_->commits.empty(), "sensitive context received an unsolicited commit");
                pass("password capability disables fixture translation");
                module_reset_reentry(false);
            });
        });
        });
    }

    void module_reset_reentry(bool emit_commit) {
        new_context();
        ic_->type("nihao");
        await_fixture("Hello.", [this, emit_commit] {
            bool triggered = false;
            auto reset_watcher = instance_.watchEvent(
                fcitx::EventType::InputContextReset,
                fcitx::EventWatcherPhase::PostInputMethod,
                [this, emit_commit, &triggered](fcitx::Event &event) {
                    auto *context = static_cast<fcitx::InputContextEvent &>(event).inputContext();
                    if (triggered || context != ic_.get()) return;
                    triggered = true;
                    if (emit_commit) context->commitString("synthetic reset commit");
                    else { context->focusOut(); context->focusIn(); }
                });
            require(ic_->key("Control+Alt+Return"), "reset-reentry translation key escaped composition");
            ic_->key("Control+Alt+Return", true);
            reset_watcher.reset();
            require(triggered, "synthetic post-reset reentry did not execute");
            if (emit_commit) {
                require(ic_->commits == std::vector<std::string>{"synthetic reset commit"},
                        "reset-side commit was followed by the stale English transaction");
                pass("outgoing commit during reset cancels the previous English transaction");
                module_configuration();
            } else {
                require(ic_->commits.empty(), "focus-out/in during reset still committed stale English");
                pass("focus-out/in during reset cancels the previous English transaction");
                module_reset_reentry(true);
            }
        });
    }

    void module_configuration() {
        new_context();
        ic_->commitString("synthetic prior context");
        require(fixture_history().size() == 1, "synthetic context was not retained before config change");
        ic_->commits.clear();
        ic_->type("nihao");
        await_fixture("Hello.", [this] {
            auto *module = instance_.addonManager().addon("transime");
            fcitx::RawConfig config;
            config["Enabled"].setValue("True");
            config["CandidateTranslations"].setValue("False");
            config["ContinuousComposition"].setValue("False");
            config["TranslateKey/0"].setValue("Control+Alt+Return");
            config["AutoCommitOnPunctuation"].setValue("False");
            config["EnableHistory"].setValue("False");
            module->setConfig(config);
            require(fixture_history().empty(), "history option change retained old context");
            require(ic_->inputPanel().auxDown().toString().find("fixture") == std::string::npos,
                    "configuration change left a ready translation visible");
            candidate("你好");
            await_fixture("Hello.", [this] {
                auto *module = instance_.addonManager().addon("transime");
                fcitx::RawConfig disabled;
                disabled["Enabled"].setValue("False");
                disabled["CandidateTranslations"].setValue("False");
                disabled["ContinuousComposition"].setValue("False");
                disabled["TranslateKey/0"].setValue("Control+Alt+Return");
                disabled["AutoCommitOnPunctuation"].setValue("False");
                disabled["EnableHistory"].setValue("False");
                module->setConfig(disabled);
                require(fixture_history().empty(), "disabled module retained history");
                require(ic_->inputPanel().auxDown().toString().find("fixture") == std::string::npos,
                        "disabled module retained a ready translation");
                candidate("你好");
                require(ic_->key("space"), "disabled module blocked ordinary Chinese selection");
                ic_->key("space", true);
                require(ic_->commits == std::vector<std::string>{"你好"},
                        "disabling translation damaged pending Chinese composition");
                require(fixture_history().empty(), "disabled module kept collecting commits");
                pass("configuration and disable clear ready/history while preserving ordinary Chinese input");
                module_history_expiry();
            });
        });
    }

    void module_history_expiry() {
        auto *module = instance_.addonManager().addon("transime");
        fcitx::RawConfig config;
        config["Enabled"].setValue("True");
        config["CandidateTranslations"].setValue("False");
        config["ContinuousComposition"].setValue("False");
        config["TranslateKey/0"].setValue("Control+Alt+Return");
        config["AutoCommitOnPunctuation"].setValue("False");
        config["EnableHistory"].setValue("True");
        config["FixtureHistoryLifetimeMilliseconds"].setValue("80");
        module->setConfig(config);
        new_context();
        ic_->commitString("synthetic expiring history");
        require(fixture_history().size() == 1, "expiry setup failed to retain a synthetic commit");
        after(150000, [this] {
            require(fixture_history().empty(), "history retained without input after its timer deadline");
            pass("history expires while focused and idle without another key event");
            finish();
        });
    }

    using Paragraph = fcitx::PinyinTransimeParagraphSnapshotV2;

    Paragraph paragraph_snapshot(int row = -1, TestInputContext *context = nullptr) {
        return instance_.addonManager().addon("pinyin")->
            call<fcitx::IPinyin::transimeParagraphSnapshotV2>(context ? context : ic_.get(), row);
    }

    fcitx::RawConfig paragraph_config(bool enabled = true, bool synthetic = false) const {
        auto config = native_config(enabled);
        config["ContinuousComposition"].setValue("True");
        config["FastContext"].setValue("True");
        config["DebounceMilliseconds"].setValue("60");
        config["TranslateKey/0"].setValue("Control+Return");
        config["CandidateTranslations"].setValue("True");
        config["EnableHistory"].setValue("False");
        // Continuous mode must override this legacy behavior completely.
        config["AutoCommitOnPunctuation"].setValue("True");
        if (synthetic) {
            const auto path = std::filesystem::path(root_) / "synthetic-paragraph-worker.py";
            require(std::filesystem::is_regular_file(path), "synthetic paragraph worker is absent");
            config["WorkerScript"].setValue(path.string());
        }
        return config;
    }

    void paragraph_no_commit(const std::string &stage) const {
        require(ic_->commits.empty(), stage + " committed before explicit paragraph confirmation");
        require(ic_->forwarded_presses.empty(), stage + " forwarded a draft input to the application");
    }

    Paragraph paragraph_expect(const std::string &source, const std::string &raw,
                               const std::string &stage) {
        const auto value = paragraph_snapshot();
        if (!value.hold || !value.hasDraft || !value.ready || value.source != source || value.rawInput != raw) {
            std::cerr << "SYNTHETIC_PARAGRAPH stage=" << std::quoted(stage)
                      << " hold=" << value.hold << " draft=" << value.hasDraft
                      << " ready=" << value.ready << " fully_selected=" << value.fullySelected
                      << " reason=" << std::quoted(value.reason) << " source=" << std::quoted(value.source)
                      << " raw=" << std::quoted(value.rawInput) << " segment=" << value.activeSegment
                      << " cursor=" << value.cursorBytes << '\n';
        }
        require(value.hold && value.hasDraft && value.ready && value.source == source && value.rawInput == raw,
                stage + " did not retain the expected complete paragraph and original pinyin");
        paragraph_no_commit(stage);
        return value;
    }

    void paragraph_type_two() {
        ic_->type("nihao");
        require(ic_->key(","), "continuous comma was not handled"); ic_->key(",", true);
        ic_->type("shijie"); select("世界");
        require(ic_->key("."), "continuous period was not handled"); ic_->key(".", true);
        paragraph_expect("你好,世界.", "nihao,shijie.", "two punctuated segments");
    }

    std::string paragraph_translation() {
        const auto target = paragraph_snapshot();
        if (!target.ready) return {};
        const auto list = ic_->inputPanel().candidateList();
        if (!target.fullySelected && list && target.pageIndex >= 0 && target.pageIndex < list->size()) {
            const auto text = list->candidate(target.pageIndex).comment().toString();
            if (has_english_letter(text)) return text;
        }
        const auto text = native_translation();
        return has_english_letter(text) ? text : std::string{};
    }

    void await_paragraph_translation(Action next, std::uint64_t deadline = 0) {
        if (!paragraph_translation().empty()) { next(); return; }
        if (!deadline) deadline = fcitx::now(CLOCK_MONOTONIC) + 15000000;
        require(fcitx::now(CLOCK_MONOTONIC) < deadline,
                "whole paragraph English did not become ready within 15 seconds");
        after(10000, [this, deadline, next = std::move(next)]() mutable {
            await_paragraph_translation(std::move(next), deadline);
        });
    }

    void paragraph_start() {
        instance_.addonManager().addon("transime")->setConfig(paragraph_config());
        new_context();
        ic_->type("nihao");
        const auto first = paragraph_expect("你好", "nihao", "first segment enters hold immediately");
        const auto repeated = paragraph_snapshot();
        require(first.apiVersion == 2 && repeated.draftId == first.draftId && repeated.revision == first.revision,
                "paragraph snapshot read advanced identity or revision");
        require(!snapshot().ready, "legacy V1 snapshot must refuse paragraph hold mode");
        const auto chosen = paragraph_snapshot(0);
        require(chosen.ready, "first paragraph numeric candidate is not complete");
        require(ic_->key("1"), "first segment numeric selection was not handled"); ic_->key("1", true);
        const auto selected = paragraph_expect(chosen.source, "nihao", "first segment numeric selection");
        require(selected.fullySelected, "numeric selection did not resolve the first segment");
        pass("continuous composition starts on the first segment; numeric selection never commits");

        new_context(); ic_->type("nihao"); select("你");
        paragraph_no_commit("partial mouse selection");
        select("好");
        require(paragraph_expect("你好", "nihao", "complete mouse selection").fullySelected,
                "complete direct candidate selection was not retained");
        pass("partial and complete direct candidate selection retain Chinese in the paragraph");

        paragraph_literals();
        paragraph_capacity();
        paragraph_native_confirm(0);
    }

    void paragraph_literals() {
        const std::string punctuation = "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~";
        for (const auto value : punctuation) {
            new_context(); ic_->type("nihao");
            const std::string key(1, value);
            require(ic_->key(key), "continuous ASCII punctuation escaped to the application");
            ic_->key(key, true);
            const auto draft = paragraph_expect("你好" + key, "nihao" + key, "ASCII punctuation " + key);
            require(draft.fullySelected, "trailing literal did not leave a resolved paragraph");
        }
        for (const auto &[codepoint, literal] : std::vector<std::pair<std::uint32_t, std::string>>{
                 {0x3002, "。"}, {0xff0c, "，"}, {0x2019, "’"}}) {
            new_context(); ic_->type("nihao");
            require(ic_->unicode_key(codepoint), "Unicode literal escaped to the application");
            ic_->unicode_key(codepoint, true);
            paragraph_expect("你好" + literal, "nihao" + literal, "Unicode punctuation literal");
        }
        pass("all 32 ASCII punctuation symbols and Unicode marks remain literal draft content without commits");
    }

    void paragraph_capacity() {
        new_context();
        for (int index = 0; index < 32; ++index) {
            ic_->type("ni");
            require(ic_->key(","), "segment separator was not retained"); ic_->key(",", true);
        }
        const auto limit = paragraph_snapshot();
        require(limit.hasDraft && limit.activeSegment == 31 && limit.rawInput.size() == 96,
                "32 native paragraph segments were not retained");
        ic_->type("hao");
        const auto rejected = paragraph_snapshot();
        require(rejected.rawInput == limit.rawInput && rejected.source == limit.source &&
                rejected.activeSegment == limit.activeSegment,
                "input beyond 32 segments altered or lost the existing paragraph");
        paragraph_no_commit("paragraph segment capacity");
        require(ic_->inputPanel().auxUp().toString().find("4096") != std::string::npos ||
                !ic_->inputPanel().auxUp().toString().empty() ||
                !ic_->inputPanel().auxDown().toString().empty(),
                "paragraph segment capacity rejected input without visible feedback");
        new_context();
        for (int index = 0; index < 4096; ++index) {
            require(ic_->key("."), "literal capacity input leaked to application"); ic_->key(".", true);
        }
        const auto bytes = paragraph_snapshot();
        require(bytes.rawInput.size() == 4096 && bytes.source.size() == 4096,
                "literal paragraph did not retain its full 4096 UTF-8 byte allowance");
        ic_->unicode_key(0x3002); ic_->unicode_key(0x3002, true);
        const auto full = paragraph_snapshot();
        require(full.rawInput == bytes.rawInput && full.source == bytes.source,
                "over-capacity Unicode input truncated or enlarged the draft");
        paragraph_no_commit("paragraph UTF-8 byte capacity");
        pass("32-segment and 4096-byte limits preserve the existing draft without partial Unicode insertion");
    }

    void paragraph_native_confirm(int mode) {
        new_context(); paragraph_type_two();
        const std::string key = mode == 0 ? "space" : "Return";
        const std::string expected = mode == 0 ? "你好,世界." : "nihao,shijie.";
        require(ic_->key(key), "whole paragraph confirmation was not handled"); ic_->key(key, true);
        require_only_commits({expected}, "whole paragraph " + key + " confirmation");
        ic_->key(key, false, true); ic_->key(key, true);
        require_only_commits({expected}, "whole paragraph confirmation repeat/release");
        require(!paragraph_snapshot().hasDraft && ic_->last_preedit.empty(),
                "confirmed paragraph remained in preedit");
        pass(mode == 0 ? "Space commits both punctuated Chinese segments together exactly once" :
                         "Return commits the exact original pinyin and punctuation together exactly once");
        if (mode == 0) paragraph_native_confirm(1);
        else paragraph_native_english();
    }

    void paragraph_native_english() {
        new_context(); paragraph_type_two();
        const auto target = paragraph_snapshot();
        require(target.fullySelected && target.pageIndex == -1,
                "trailing punctuation should be a fully selected whole paragraph target");
        require(paragraph_translation().empty(), "fresh paragraph unexpectedly reused prior ready English");
        require(ic_->key("Control+Return"), "unready paragraph shortcut leaked to application");
        require(ic_->key("Control+Return", true), "unready paragraph shortcut release leaked");
        paragraph_expect("你好,世界.", "nihao,shijie.", "unready English shortcut");
        await_paragraph_translation([this] {
            const auto expected = paragraph_translation();
            require(!expected.empty() && expected != "你好,世界." &&
                    expected.find("nihao") == std::string::npos && expected.find("shijie") == std::string::npos,
                    "real worker did not provide whole-paragraph English");
            require(native_translation() == expected,
                    "fully selected paragraph English is not displayed in auxDown");
            require(ic_->key("Control+Return"), "ready paragraph translation shortcut not accepted");
            ic_->key("Control+Return", true);
            require_only_commits({expected}, "whole paragraph English confirmation");
            require(!paragraph_snapshot().hasDraft, "English confirmation did not consume the paragraph");
            std::cout << "SYNTHETIC_PARAGRAPH_ENGLISH source=\"你好,世界.\" committed="
                      << std::quoted(expected) << '\n';
            pass("unready Ctrl+Return is swallowed; fully selected paragraph displays and commits real English once");
            paragraph_rows();
        });
    }

    void paragraph_rows() {
        new_context(); ic_->type("nihao"); ic_->key(","); ic_->key(",", true); ic_->type("hanzi");
        const auto first = paragraph_snapshot();
        const auto second = paragraph_snapshot(1);
        require(first.ready && second.ready && first.source != second.source &&
                first.source.starts_with("你好,") && second.source.starts_with("你好,"),
                "paragraph candidate navigation needs two distinct complete hanzi rows");
        require(ic_->key("Down"), "paragraph Down candidate key was not handled"); ic_->key("Down", true);
        require(paragraph_snapshot().source == second.source, "Down did not highlight the next paragraph candidate");
        require(ic_->key("Up"), "paragraph Up candidate key was not handled"); ic_->key("Up", true);
        require(paragraph_snapshot().source == first.source && paragraph_snapshot().revision != first.revision,
                "candidate A-B-A did not invalidate the original paragraph generation");
        require(!instance_.addonManager().addon("pinyin")->call<fcitx::IPinyin::transimeCommitParagraphV2>(
                    ic_.get(), first, fcitx::PinyinTransimeCommitModeV2::Translation, "Stale candidate."),
                "stale paragraph bridge target committed after A-B-A");
        paragraph_no_commit("paragraph A-B-A");
        await_paragraph_translation([this] {
            const auto list = ic_->inputPanel().candidateList();
            require(list && list->layoutHint() == fcitx::CandidateLayoutHint::Vertical,
                    "active paragraph candidates are not vertical");
            const auto expected = paragraph_translation();
            require(ic_->inputPanel().auxDown().toString().empty(),
                    "active candidate paragraph duplicated row translation in auxDown");
            require(ic_->key("Control+Return"), "active candidate paragraph shortcut was not handled");
            ic_->key("Control+Return", true);
            require_only_commits({expected}, "active highlighted paragraph translation");
            pass("Up/Down changes whole-paragraph targets; vertical row English and A-B-A guards remain valid");
            paragraph_editing();
        });
    }

    void paragraph_editing() {
        new_context(); ic_->type("nihao"); select("你"); select("好");
        const auto before = paragraph_expect("你好", "nihao", "explicit original word selections");
        ic_->key(","); ic_->key(",", true);
        require(ic_->key("BackSpace"), "BackSpace across literal was not handled"); ic_->key("BackSpace", true);
        const auto restored = paragraph_expect("你好", "nihao", "BackSpace removes trailing punctuation");
        require(restored.fullySelected && restored.revision != before.revision,
                "removing punctuation did not restore the selected native segment");
        require(ic_->key("BackSpace"), "BackSpace into selected native word was not handled"); ic_->key("BackSpace", true);
        const auto partial = paragraph_snapshot();
        require(partial.hasDraft && partial.rawInput == "nihao" && partial.activeCandidate.selectedText == "你",
                "BackSpace lost the original native partial selection before punctuation");
        select("好");
        paragraph_expect("你好", "nihao", "reselect original final word after BackSpace");
        pass("BackSpace across punctuation restores original native word-selection boundaries");
        paragraph_edit_earlier();
    }

    void paragraph_edit_earlier() {
        new_context(); paragraph_type_two();
        const auto old = paragraph_snapshot();
        {
            fcitx::InputContextEventBlocker blocker(ic_.get());
            // Native InvokeAction cursors count Unicode characters. Position 2
            // is after 你好 and before the comma, not UTF-8 byte offset 2.
            fcitx::InvokeActionEvent click(fcitx::InvokeActionEvent::Action::LeftClick, 2, ic_.get());
            ic_->invokeAction(click);
        }
        require(paragraph_snapshot().activeSegment == 0,
                "preedit click did not reopen the first native segment");
        require(ic_->key("BackSpace"), "first-segment native unselection was not handled");
        ic_->key("BackSpace", true);
        candidate("你号");
        select("你号");
        const auto changed = paragraph_expect("你号,世界.", "nihao,shijie.", "editing first segment preserves following draft");
        require(changed.draftId == old.draftId && changed.revision != old.revision,
                "earlier-segment edit did not retain the draft with a fresh revision");
        require(!instance_.addonManager().addon("pinyin")->call<fcitx::IPinyin::transimeCommitParagraphV2>(
                    ic_.get(), old, fcitx::PinyinTransimeCommitModeV2::Translation, "Stale earlier segment."),
                "old paragraph snapshot committed after editing an earlier segment");
        paragraph_no_commit("reselecting an earlier segment");
        pass("native preedit click and earlier-word reselection preserve all following Chinese and punctuation");
        paragraph_navigation();
    }

    void paragraph_navigation() {
        new_context(); paragraph_type_two();
        const auto end = paragraph_snapshot();
        require(ic_->key("Left"), "paragraph Left key was not accepted"); ic_->key("Left", true);
        const auto left = paragraph_snapshot();
        require(left.rawInput == end.rawInput && left.cursorBytes + 1 == end.cursorBytes,
                "Left did not move exactly across the trailing ASCII literal");
        require(ic_->key("Right"), "paragraph Right key was not accepted"); ic_->key("Right", true);
        require(paragraph_snapshot().cursorBytes == end.cursorBytes,
                "Right did not return to the whole paragraph end");
        ic_->key("Left"); ic_->key("Left", true);
        require(ic_->key("Delete"), "paragraph Delete key was not accepted"); ic_->key("Delete", true);
        paragraph_expect("你好,世界", "nihao,shijie", "Delete a trailing literal at the paragraph cursor");
        require(ic_->key("Home"), "paragraph Home key was not accepted"); ic_->key("Home", true);
        const auto home = paragraph_snapshot();
        require(home.activeSegment == 0 && home.cursorBytes == 0 && home.rawInput == "nihao,shijie",
                "Home lost draft text or failed to address the first segment");
        ic_->key("Right"); ic_->key("Right", true);
        require(paragraph_snapshot().cursorBytes > 0, "Right did not advance from paragraph Home");
        ic_->key("Left"); ic_->key("Left", true);
        require(paragraph_snapshot().cursorBytes == 0, "Left did not return to paragraph Home");
        require(ic_->key("End"), "paragraph End key was not accepted"); ic_->key("End", true);
        const auto returned = paragraph_snapshot();
        require(returned.activeSegment == 1 && returned.rawInput == "nihao,shijie" && returned.cursorBytes > 0,
                "End lost text or failed to address the final native segment");
        paragraph_no_commit("Home/End/Left/Right/Delete editing");
        ic_->key("Return"); ic_->key("Return", true);
        require_only_commits({"nihao,shijie"}, "raw paragraph confirmation after navigation and Delete");
        pass("Home/End/Left/Right and Delete preserve native segments and move the whole-paragraph cursor");
        paragraph_focus();
    }

    void paragraph_focus() {
        new_context(); paragraph_type_two();
        const auto original = paragraph_snapshot();
        ic_->focusOut();
        require(ic_->commits.empty(), "focus-out committed a continuous draft");
        auto first = std::move(ic_);
        new_context();
        require(!paragraph_snapshot().hasDraft && ic_->last_preedit.empty(),
                "another IC inherited the earlier paragraph");
        ic_->type("zhongguo");
        paragraph_expect("中国", "zhongguo", "independent second IC draft");
        ic_->reset(); paragraph_type_two();
        const auto identical = paragraph_snapshot();
        require(identical.source == original.source && identical.rawInput == original.rawInput &&
                identical.draftId != original.draftId,
                "identical paragraphs in separate ICs did not receive distinct draft identities");
        require(!instance_.addonManager().addon("pinyin")->call<fcitx::IPinyin::transimeCommitParagraphV2>(
                    ic_.get(), original, fcitx::PinyinTransimeCommitModeV2::Translation, "Wrong input context."),
                "another IC accepted the first IC's identical-text paragraph snapshot");
        paragraph_no_commit("identical paragraph target in another IC");
        pass("identical Chinese and raw pinyin in two ICs cannot authorize a cross-context paragraph commit");
        ic_->focusOut();
        auto second = std::move(ic_);
        ic_ = std::move(first);
        ic_->focusIn();
        const auto resumed = paragraph_expect("你好,世界.", "nihao,shijie.", "same IC focus return");
        require(resumed.draftId == original.draftId && second->commits.empty(),
                "focus round-trip changed draft identity or submitted the second IC");
        pass("focus-out retains a paragraph for the same IC while another IC keeps a separate invisible draft");
        paragraph_late_result();
    }

    void paragraph_late_result() {
        instance_.addonManager().addon("transime")->setConfig(paragraph_config(true, true));
        new_context(); ic_->type("nihao"); ic_->key(","); ic_->key(",", true);
        const auto old = paragraph_expect("你好,", "nihao,", "pending old paragraph");
        after(200000, [this, old] {
            ic_->type("shijie"); select("世界"); ic_->key("."); ic_->key(".", true);
            paragraph_expect("你好,世界.", "nihao,shijie.", "paragraph extended during worker response");
            require(!instance_.addonManager().addon("pinyin")->call<fcitx::IPinyin::transimeCommitParagraphV2>(
                        ic_.get(), old, fcitx::PinyinTransimeCommitModeV2::Translation, "Old paragraph,"),
                    "old whole-paragraph snapshot committed after draft extension");
            require(ic_->key("Control+Return"), "unready edited-paragraph shortcut escaped");
            ic_->key("Control+Return", true);
            paragraph_no_commit("shortcut while extended paragraph waits");
            await_paragraph_translation([this] {
                require(paragraph_translation() == "New paragraph.",
                        "late old paragraph English replaced the current target");
                paragraph_no_commit("late worker response");
                ic_->key("Control+Return"); ic_->key("Control+Return", true);
                require_only_commits({"New paragraph."}, "edited paragraph English confirmation");
                after(600000, [this] {
                    require_only_commits({"New paragraph."}, "late response after paragraph consumption");
                    pass("delayed responses cannot submit or overwrite an older paragraph after editing");
                    paragraph_cache();
                });
            });
        });
    }

    std::size_t paragraph_request_count(const std::string &source) const {
        std::ifstream stream(std::filesystem::path(root_) / "paragraph-requests.tsv");
        std::size_t result = 0;
        std::string line;
        while (std::getline(stream, line)) {
            if (line.starts_with(source + "\t")) ++result;
        }
        return result;
    }

    void paragraph_cache() {
        new_context();
        const auto initial = paragraph_request_count("你好,");
        ic_->type("nihao"); ic_->key(","); ic_->key(",", true);
        const auto old = paragraph_snapshot();
        await_paragraph_translation([this, initial, old] {
            require(paragraph_translation() == "Old paragraph,", "cache setup did not translate target A");
            ic_->key("."); ic_->key(".", true);
            await_paragraph_translation([this, initial, old] {
                require(paragraph_snapshot().source == "你好,.", "cache target B was not current");
                ic_->key("BackSpace"); ic_->key("BackSpace", true);
                paragraph_expect("你好,", "nihao,", "cached A-B-A draft");
                require(!instance_.addonManager().addon("pinyin")->call<fcitx::IPinyin::transimeCommitParagraphV2>(
                    ic_.get(), old, fcitx::PinyinTransimeCommitModeV2::Translation, "Stale cached A."),
                    "cache enabled committing the original stale A snapshot");
                await_paragraph_translation([this, initial] {
                    require(paragraph_translation() == "Old paragraph,", "cache did not recover target A translation");
                    require(paragraph_request_count("你好,") == initial + 1,
                            "A-B-A cache submitted A to the worker more than once");
                    paragraph_no_commit("cached A-B-A");
                    pass("same-context A-B-A reuses cached English without a second worker request while rejecting stale identity");
                    paragraph_provenance();
                });
            });
        });
    }

    void paragraph_provenance() {
        auto config = paragraph_config(true, true);
        config["EnableHistory"].setValue("True");
        config["ContextAware"].setValue("True");
        instance_.addonManager().addon("transime")->setConfig(config);
        new_context(); paragraph_type_two();
        await_paragraph_translation([this] {
            require(paragraph_translation() == "New paragraph.", "paragraph provenance setup English unavailable");
            ic_->key("Control+Return"); ic_->key("Control+Return", true);
            require_only_commits({"New paragraph."}, "paragraph provenance English commit");
            ic_->commits.clear();
            ic_->type("zhongguo");
            await_paragraph_translation([this] {
                require(paragraph_translation() == "History source retained.",
                        "worker history lost whole Chinese paragraph source paired with its English commit");
                paragraph_no_commit("next paragraph after bilingual history");
                pass("English paragraph commit retains its whole Chinese source in the next request history");
                instance_.addonManager().addon("transime")->setConfig(paragraph_config(true, true));
                paragraph_cancel(0);
            });
        });
    }

    void paragraph_cancel(int mode) {
        new_context(); paragraph_type_two();
        after(200000, [this, mode] {
            if (mode == 0) ic_->reset();
            else if (mode == 1) {
                ic_->setCapabilityFlags(fcitx::CapabilityFlags(fcitx::CapabilityFlag::Preedit) |
                                       fcitx::CapabilityFlag::Password);
            } else if (mode == 2) {
                new_context(); // destroys the previous IC after its preserving focus-out
            } else {
                require(ic_->key("Escape"), "paragraph Escape cancellation not handled"); ic_->key("Escape", true);
            }
            require(!paragraph_snapshot().hasDraft && ic_->last_preedit.empty(),
                    "reset/sensitive/destroy/Escape left an old paragraph visible");
            paragraph_no_commit("explicit paragraph cancellation");
            after(700000, [this, mode] {
                paragraph_no_commit("cancelled paragraph delayed response");
                require(!paragraph_snapshot().hasDraft && paragraph_translation().empty(),
                        "cancelled paragraph received a delayed translation");
                const std::vector<std::string> names{"reset", "sensitive capability", "IC destruction", "Escape"};
                pass("whole paragraph cancels without commits or late responses on " + names[mode]);
                if (mode < 3) paragraph_cancel(mode + 1);
                else paragraph_disable();
            });
        });
    }

    void paragraph_disable() {
        new_context(); paragraph_type_two();
        instance_.addonManager().addon("transime")->setConfig(paragraph_config(false, true));
        paragraph_expect("你好,世界.", "nihao,shijie.", "disable with nonempty paragraph");
        after(600000, [this] {
            paragraph_expect("你好,世界.", "nihao,shijie.", "disabled pending paragraph after delayed response");
            require(ic_->key("space"), "disabled pending paragraph could not be confirmed as Chinese");
            ic_->key("space", true);
            require_only_commits({"你好,世界."}, "Chinese confirmation of deferred disabled paragraph");
            require(!paragraph_snapshot().hasDraft && !paragraph_snapshot().hold,
                    "deferred disabling did not finish after explicit Chinese confirmation");
            pass("disabling a nonempty paragraph preserves its full draft until explicit Chinese confirmation");
            paragraph_ui_destroy();
        });
    }

    void paragraph_ui_destroy() {
        instance_.addonManager().addon("transime")->setConfig(paragraph_config(true, true));
        new_context(); paragraph_type_two();
        auto triggered = std::make_shared<bool>(false);
        paragraph_destroy_watcher_ = instance_.watchEvent(
            fcitx::EventType::InputContextUpdateUI,
            fcitx::EventWatcherPhase::PostInputMethod,
            [this, triggered](fcitx::Event &event) {
                auto *context = static_cast<fcitx::InputContextEvent &>(event).inputContext();
                if (*triggered || context != ic_.get() ||
                    context->inputPanel().auxDown().toString().find("New paragraph.") == std::string::npos) return;
                *triggered = true;
                // Registered after the Module's PostInputMethod observer.
                // Fcitx 5.1.21 handles UI updates in ReservedFirst; destruction
                // expires that pending UI target before deferred flush.
                ic_.reset();
            });
        after(1800000, [this, triggered] {
            paragraph_destroy_watcher_.reset();
            require(*triggered && !ic_, "synthetic UI observer did not destroy the translated IC");
            new_context();
            require(!paragraph_snapshot().hasDraft, "destroyed UI target leaked its paragraph to a new IC");
            ic_->type("nihao");
            await_paragraph_translation([this] {
                paragraph_no_commit("new paragraph after synchronous UI target destruction");
                pass("a final synchronous translation-UI observer can destroy its IC without stale access or draft leakage");
                paragraph_learning();
            });
        });
    }

    std::pair<std::string, std::string> paragraph_saved_learning() {
        const auto base = std::filesystem::path(root_) / "data/fcitx5/pinyin";
        instance_.addonManager().addon("pinyin")->save();
        const auto read = [&](const std::string &name) {
            std::ifstream stream(base / name, std::ios::binary);
            require(stream.good(), "private synthetic pinyin learning file was not saved");
            return std::string(std::istreambuf_iterator<char>(stream), std::istreambuf_iterator<char>());
        };
        auto value = std::make_pair(read("user.dict"), read("user.history"));
        require(!value.first.empty() && !value.second.empty(), "private learning outputs are empty");
        return value;
    }

    void paragraph_learning() {
        instance_.addonManager().addon("transime")->setConfig(paragraph_config());
        new_context();
        const auto before = paragraph_saved_learning();
        ic_->type("nihao"); select("你"); select("好"); ic_->key(","); ic_->key(",", true);
        ic_->type("shijie"); select("世界");
        paragraph_expect("你好,世界", "nihao,shijie", "unconfirmed real selected Chinese words");
        require(paragraph_saved_learning() == before,
                "unconfirmed paragraph selection changed native learned data");
        ic_->reset();
        require(paragraph_saved_learning() == before, "cancelled paragraph polluted native learned data");
        paragraph_type_two();
        ic_->key("Return"); ic_->key("Return", true);
        require_only_commits({"nihao,shijie."}, "raw confirmation during learning audit");
        require(paragraph_saved_learning() == before, "raw pinyin confirmation learned uncommitted Chinese selections");
        ic_->commits.clear();
        paragraph_type_two();
        ic_->key("space"); ic_->key("space", true);
        require_only_commits({"你好,世界."}, "Chinese confirmation during learning audit");
        const auto after = paragraph_saved_learning();
        require(after.second != before.second,
                "explicit Chinese paragraph confirmation did not update native learning history");
        std::cout << "SYNTHETIC_PARAGRAPH_LEARNING dictionary_bytes=" << after.first.size()
                  << " history_bytes_before=" << before.second.size()
                  << " history_bytes_after=" << after.second.size() << '\n';
        pass("native selected words learn on Chinese confirmation; cancellation and raw confirmation do not learn");
        finish();
    }

    void mixed_shift_tap(const std::string &side = "Shift_L") {
        ic_->key(side);
        ic_->key("Shift+" + side, true);
    }

    void mixed_shifted(const std::string &symbol, const std::string &side = "Shift_L") {
        ic_->key(side);
        require(ic_->key("Shift+" + symbol), "mixed Shift chord was not handled: " + symbol);
        ic_->key("Shift+" + symbol, true);
        ic_->key("Shift+" + side, true);
    }

    void mixed_ascii(const std::string &text) {
        const std::string shifted = "~!@#$%^&*()_+{}|:\"<>?";
        for (const unsigned char c : text) {
            const std::string symbol(1, c);
            if (c == ' ') mixed_shifted("space");
            else if ((c >= 'A' && c <= 'Z') || shifted.find(c) != std::string::npos)
                mixed_shifted(symbol);
            else { require(ic_->key(symbol), "mixed ASCII input was not handled: " + symbol); ic_->key(symbol, true); }
        }
    }

    void mixed_expect(const std::string &source, const std::string &raw, const std::string &stage) {
        const auto value = paragraph_snapshot();
        std::cout << "SYNTHETIC_MIXED stage=" << std::quoted(stage)
                  << " im=" << std::quoted(instance_.inputMethod(ic_.get()))
                  << " source=" << std::quoted(value.source) << " raw=" << std::quoted(value.rawInput)
                  << " hold=" << value.hold << " ready=" << value.ready
                  << " reason=" << std::quoted(value.reason) << " commits=" << ic_->commits.size() << '\n';
        require(instance_.inputMethod(ic_.get()) == "pinyin", stage + " switched the global input method");
        paragraph_expect(source, raw, stage);
    }

    void mixed_start() {
        // Exercise the real global modifier trigger that the bridge must defer
        // while hold is active; default-only tests can miss this ordering bug.
        fcitx::RawConfig global;
        global["Hotkey/AltTriggerKeys/0"].setValue("Shift_L");
        global["Hotkey/AltTriggerKeys/1"].setValue("Shift_R");
        global["Hotkey/ModifierOnlyKeyTimeout"].setValue("250");
        instance_.globalConfig().load(global, true);
        require(instance_.globalConfig().altTriggerKeys().size() == 2,
                "mixed tests did not enable both real global Shift triggers");
        instance_.addonManager().addon("transime")->setConfig(paragraph_config());
        new_context(); ic_->type("nihao");
        mixed_shift_tap();
        mixed_expect("你好", "nihao", "Shift_L switches internally without flushing the draft");
        mixed_ascii("hello123");
        mixed_shift_tap("Shift_R");
        ic_->type("shijie"); select("世界");
        mixed_expect("你好hello123世界", "nihaohello123shijie", "Chinese English Chinese draft");
        ic_->key("space"); ic_->key("space", true);
        require_only_commits({"你好hello123世界"}, "mixed Chinese confirmation");
        pass("real Shift_L/Shift_R taps change language inside the draft and Space submits the whole mixed text");
        mixed_control_space();
    }

    void mixed_control_space() {
        new_context(); ic_->type("nihao");
        const auto chord = [this](bool control_released_first) {
            ic_->key("Control_L");
            require(ic_->key("Control+space"), "draft Ctrl+Space was not handled");
            if (control_released_first) {
                ic_->key("Control+space", false, true);
                ic_->key("Control+Control_L", true);
                ic_->key("space", true);
            } else {
                ic_->key("Control+space", true);
                ic_->key("Control+Control_L", true);
            }
        };
        chord(true); mixed_ascii("api123"); chord(false);
        ic_->type("shijie"); select("世界");
        mixed_expect("你好api123世界", "nihaoapi123shijie", "Ctrl+Space switches only inside a nonempty draft");
        ic_->key("space"); ic_->key("space", true);
        require_only_commits({"你好api123世界"}, "mixed Ctrl+Space round-trip confirmation");
        new_context(); mixed_ascii("API");
        ic_->key("Control_L"); ic_->key("Control+Shift_L");
        ic_->key("Control+Shift+space"); ic_->key("Control+Shift+space", true);
        ic_->key("Control+Shift+Shift_L", true); ic_->key("Control+Control_L", true);
        mixed_ascii("test");
        mixed_expect("APItest", "APItest", "Ctrl+Shift+Space does not change the literal language mode");
        pass("draft Ctrl+Space preserves order across repeat and reversed modifier release; Ctrl+Shift+Space does not toggle");
        mixed_api();
    }

    void mixed_api() {
        new_context(); ic_->type("zhege"); ic_->key("1"); ic_->key("1", true);
        mixed_ascii("API"); mixed_shift_tap();
        ic_->type("henhaoyong"); ic_->key("1"); ic_->key("1", true);
        ic_->key("."); ic_->key(".", true);
        mixed_expect("这个API很好用.", "zhegeAPIhenhaoyong.", "desktop API top-candidate sequence");
        ic_->key("Return"); ic_->key("Return", true);
        require_only_commits({"zhegeAPIhenhaoyong."}, "raw confirmation preserves English case and original pinyin");
        pass("uppercase API starts literal mode; returning to Chinese preserves top-candidate selection and exact raw commit");
        mixed_literals();
    }

    void mixed_literals() {
        new_context(); mixed_shift_tap(); mixed_ascii("hello123");
        mixed_expect("hello123", "hello123", "English first with literal digits");
        require(paragraph_snapshot().fullySelected, "literal English was left as unresolved pinyin");
        pass("English can start an empty draft and digits stay literal instead of selecting Chinese candidates");
        for (const auto &text : std::vector<std::string>{"Python3.12", "https://example.org/a-b?q=2",
                 "dev.test@example.org", "C++", "v1.2.3", "get_user_name"}) {
            new_context();
            if (!(text[0] >= 'A' && text[0] <= 'Z')) mixed_shift_tap();
            mixed_ascii(text);
            mixed_expect(text, text, "literal identifier " + text);
        }
        pass("real shifted symbol events preserve Python, URL, email, C++, versions and underscore identifiers");
        new_context(); mixed_ascii("Python"); mixed_shifted("space"); mixed_ascii("api12");
        mixed_expect("Python api12", "Python api12", "Shift chords and literal space");
        // A Shift+A chord above already entered English; its final Shift
        // release must not be misinterpreted as a bare language-toggle tap.
        mixed_shift_tap(); ic_->type("nihao"); select("你好");
        mixed_expect("Python api12你好", "Python api12nihao", "one explicit tap returns after Shift chords");
        pass("Shift+letter and Shift+Space do not cause a second tap; Shift+Space inserts a real draft space");
        mixed_edit();
    }

    void mixed_edit() {
        new_context(); mixed_ascii("API2");
        ic_->key("Left"); ic_->key("Left", true); ic_->key("Delete"); ic_->key("Delete", true);
        mixed_expect("API", "API", "Delete in literal English");
        ic_->key("BackSpace"); ic_->key("BackSpace", true);
        mixed_expect("AP", "AP", "BackSpace in literal English");
        mixed_ascii("I"); mixed_expect("API", "API", "reinsert shifted letter after literal edit");
        pass("BackSpace and Delete edit the literal English segment without losing earlier characters");
        mixed_native_english();
    }

    void mixed_native_english() {
        new_context(); ic_->type("nihao"); ic_->key(","); ic_->key(",", true); mixed_ascii("Python3.12");
        mixed_expect("你好,Python3.12", "nihao,Python3.12", "whole mixed English target");
        require(ic_->key("Control+Return"), "unready mixed English shortcut leaked");
        ic_->key("Control+Return", true);
        paragraph_no_commit("unready mixed English shortcut");
        await_paragraph_translation([this] {
            const auto translation = paragraph_translation();
            require(translation.find("Python") != std::string::npos && translation.find("3.12") != std::string::npos &&
                    translation.find("nihao") == std::string::npos && translation.find("你好") == std::string::npos,
                    "real mixed English result lost the selected literal identifier or kept Chinese/pinyin");
            ic_->key("Control+Return"); ic_->key("Control+Return", true);
            require_only_commits({translation}, "real mixed English confirmation");
            std::cout << "SYNTHETIC_MIXED_ENGLISH " << std::quoted(translation) << '\n';
            pass("Ctrl+Return waits for a valid whole mixed translation and explicitly submits real English with Python3.12 retained");
            mixed_boundaries();
        });
    }

    void mixed_boundaries() {
        for (int mode = 0; mode < 3; ++mode) {
            new_context(); mixed_ascii("API");
            if (mode == 0) ic_->reset();
            else if (mode == 1) {
                ic_->setCapabilityFlags(fcitx::CapabilityFlags(fcitx::CapabilityFlag::Preedit) |
                                       fcitx::CapabilityFlag::Password);
                require(!paragraph_snapshot().hasDraft, "sensitive capability retained mixed draft");
                ic_->setCapabilityFlags(fcitx::CapabilityFlags(fcitx::CapabilityFlag::Preedit));
            } else new_context();
            require(!paragraph_snapshot().hasDraft, "reset/sensitive/destroy retained mixed draft");
            ic_->type("nihao");
            mixed_expect("你好", "nihao", "new Chinese mode after mixed cancellation");
        }
        pass("reset, sensitive capability and IC destruction clear literal language mode and old mixed draft");
        new_context(); mixed_ascii("API");
        auto first = std::move(ic_); first->focusOut();
        new_context(); ic_->type("nihao"); mixed_expect("你好", "nihao", "second IC starts Chinese independently");
        ic_->focusOut(); auto second = std::move(ic_); ic_ = std::move(first); ic_->focusIn();
        mixed_ascii("test"); mixed_expect("APItest", "APItest", "same IC resumes literal mode");
        require(second->commits.empty(), "cross-IC mixed switch submitted another draft");
        pass("same IC resumes its mixed literal mode while another IC starts independent Chinese composition");
        mixed_selection();
    }

    void mixed_selection() {
        new_context(); ic_->type("hanzi");
        const auto row = paragraph_snapshot(1);
        require(row.ready, "mixed highlight test needs the second complete native candidate");
        ic_->key("Down"); ic_->key("Down", true);
        mixed_shift_tap();
        mixed_expect(row.source, "hanzi", "internal Shift retains the existing highlighted candidate");
        mixed_ascii("1");
        mixed_expect(row.source + "1", "hanzi1", "first English digit confirms the highlighted Chinese candidate only");
        new_context(); mixed_shift_tap(); mixed_shift_tap("Shift_R");
        ic_->type("nihao"); ic_->key("1"); ic_->key("1", true);
        mixed_expect("你好", "nihao", "numeric Chinese choice after language round trip");
        ic_->reset(); ic_->type("nihao"); select("你"); select("好");
        mixed_expect("你好", "nihao", "direct Chinese choices after mixed mode reset");
        pass("Chinese numeric and mouse candidate selection remain noncommitting after internal language switches");
        mixed_disable();
    }

    void mixed_disable() {
        new_context(); mixed_ascii("API");
        instance_.addonManager().addon("transime")->setConfig(paragraph_config(false));
        mixed_expect("API", "API", "disabled module retains literal draft");
        ic_->key("space"); ic_->key("space", true);
        require_only_commits({"API"}, "disabled mixed draft explicit confirmation");
        require(!paragraph_snapshot().hold, "mixed hold did not finish disabling after confirmation");
        pass("disabling the module retains English draft until explicit confirmation and releases hold afterwards");
        ic_->commits.clear();
        mixed_shift_tap();
        require(instance_.inputMethod(ic_.get()) == "keyboard-us", "ordinary global Shift toggle stopped working after hold disabled");
        mixed_shift_tap();
        require(instance_.inputMethod(ic_.get()) == "pinyin", "ordinary global Shift toggle could not return to pinyin");
        require(!paragraph_snapshot().hold, "ordinary global Shift unexpectedly re-enabled mixed hold");
        paragraph_no_commit("ordinary global Shift after disabling hold");
        instance_.addonManager().addon("transime")->setConfig(paragraph_config());
        new_context();
        const auto control_space = [this] {
            ic_->key("Control_L"); ic_->key("Control+space");
            ic_->key("Control+space", true); ic_->key("Control+Control_L", true);
        };
        control_space();
        require(instance_.inputMethod(ic_.get()) == "keyboard-us", "empty-draft Ctrl+Space lost ordinary global switching");
        control_space();
        require(instance_.inputMethod(ic_.get()) == "pinyin" && !paragraph_snapshot().hasDraft,
                "empty-draft Ctrl+Space failed to return without creating a draft");
        paragraph_no_commit("ordinary Ctrl+Space with an empty draft");
        mixed_shift_tap();
        require(!paragraph_snapshot().hasDraft && ic_->inputPanel().auxUp().empty(),
                "quiet default unexpectedly displayed an English mode hint");
        instance_.addonManager().addon("transime")->setConfig(paragraph_config(false));
        require(!paragraph_snapshot().hold && ic_->inputPanel().auxUp().empty(),
                "disabling empty English hold left its mode popup visible");
        mixed_shift_tap();
        require(instance_.inputMethod(ic_.get()) == "keyboard-us", "empty English hold disable did not restore global Shift");
        mixed_shift_tap();
        require(instance_.inputMethod(ic_.get()) == "pinyin", "global Shift return failed after empty English hold disable");
        pass("outside a draft global Shift and Ctrl+Space retain ordinary switching; mode hints are quiet by default");
        configurable_controls();
    }

    void configurable_controls() {
        auto *addon = instance_.addonManager().addon("transime");
        auto config = paragraph_config();
        config["ChineseCommitKey/0"].setValue("F6");
        config["RawCommitKey/0"].setValue("Control+F7");
        config["LiteralSpaceKey/0"].setValue("Alt+F8");
        config["ToggleKey/0"].setValue("Control+F9");
        config["ShowModeHints"].setValue("False");
        addon->setConfig(config);
        new_context(); ic_->type("nihao");
        mixed_shift_tap();
        require(ic_->inputPanel().auxUp().empty(), "quiet mode shows a repeated language label");
        require(ic_->key("Alt+F8"), "configured literal-space shortcut was ignored");
        ic_->key("F8", true);
        mixed_ascii("API");
        mixed_expect("你好 API", "nihao API", "configured literal space preserves mixed draft");
        require(ic_->key("F6"), "configured Chinese submission was ignored");
        ic_->key("F6", false, true);
        ic_->key("F6", true);
        require_only_commits({"你好 API"}, "configured Chinese confirmation commits once");
        new_context(); ic_->type("nihao");
        require(ic_->key("Control+F7"), "configured raw submission was ignored");
        ic_->key("F7", true);
        require_only_commits({"nihao"}, "configured raw confirmation");
        new_context(); ic_->type("nihao");
        require(ic_->key("Return"), "unbound Return leaked to application while composing");
        ic_->key("Return", true);
        require(ic_->key("KP_Enter"), "unbound keypad Enter leaked to application while composing");
        ic_->key("KP_Enter", true);
        require(ic_->key("space"), "remapped bare Space leaked to application while composing");
        ic_->key("space", true);
        mixed_expect("你好 ", "nihao ", "former confirmation keys retain the draft");
        ic_->key("F6"); ic_->key("F6", true);
        require_only_commits({"你好 "}, "only the configured key confirms the paragraph");
        new_context(); ic_->type("nihao");
        auto conflicting = config;
        conflicting["RawCommitKey/0"].setValue("F6");
        bool rejected = false;
        try { addon->setConfig(conflicting); }
        catch (const std::invalid_argument &) { rejected = true; }
        require(rejected, "duplicate action shortcuts were accepted");
        mixed_expect("你好", "nihao", "rejected configuration preserves current draft");
        ic_->key("F6"); ic_->key("F6", true);
        require_only_commits({"你好"}, "rejected configuration preserves previous keys");
        new_context(); ic_->type("nihao");
        ic_->key("Control+F9"); ic_->key("F9", true);
        mixed_expect("你好", "nihao", "toggle off defers disabling until draft confirmation");
        ic_->key("F6"); ic_->key("F6", true);
        require_only_commits({"你好"}, "toggle-off draft confirmation");
        require(!paragraph_snapshot().hold, "toggle-off retained hold after confirmation");
        ic_->key("Control+F9"); ic_->key("F9", true);
        require(paragraph_snapshot().hold, "toggle shortcut cannot re-enable disabled plugin");
        config["ShowModeHints"].setValue("True");
        addon->setConfig(config);
        new_context(); mixed_shift_tap();
        require(!ic_->inputPanel().auxUp().empty(), "optional mode hint cannot be enabled");
        config["ShowModeHints"].setValue("False");
        addon->setConfig(config);
        require(ic_->inputPanel().auxUp().empty(), "disabling mode hints left stale label");
        config["ChineseCommitKey/0"].setValue("Control+a");
        config["RawCommitKey/0"].setValue("Control+Shift+a");
        addon->setConfig(config);
        new_context(); ic_->type("nihao");
        ic_->key("Control+a"); ic_->key("a", true);
        require_only_commits({"你好"}, "lowercase shortcut is normalized to frontend keys");
        new_context(); ic_->type("nihao");
        ic_->key("Control+Shift+a"); ic_->key("a", true);
        require_only_commits({"nihao"}, "Control+Shift letter remains distinct from Control letter");
        require(!fcitx::transimeValidControlKeys({{fcitx::Key("a")}}) &&
                !fcitx::transimeValidControlKeys({{fcitx::Key("Control+space")}}) &&
                !fcitx::transimeValidControlKeys({{fcitx::Key("Shift_L")}}),
                "unsafe typing/global switch shortcut passed validation");
        pass("custom confirmation/literal/toggle shortcuts, quiet hints and atomic conflict rejection");
        finish();
    }

    void pass(const std::string &name) {
        ++passed_;
        std::cout << "PASS: " << name << '\n';
    }

    void finish() {
        if (finished_) return;
        finished_ = true;
        if (ic_) { ic_->reset(); ic_->focusOut(); ic_.reset(); }
        std::cout << passed_ << " headless integration checks passed; "
                  << (native_python_.empty() ? "fixture is not a translation model" :
                      "native model input-path checks do not score translation quality") << '\n';
        instance_.exit();
    }

    fcitx::Instance &instance_;
    bool with_transime_;
    bool without_bridge_;
    bool candidate_rows_;
    bool punctuation_diagnostic_;
    bool punctuation_commit_;
    bool punctuation_history_expiry_;
    bool paragraph_;
    bool mixed_;
    bool dictionary_pack_;
    std::string root_;
    std::string native_python_, native_script_, native_model_, first_native_translation_;
    std::vector<std::string> punctuation_without_plugin_;
    bool failed_ = false;
    bool finished_ = false;
    std::size_t passed_ = 0;
    std::size_t ui_flushes_ = 0;
    std::unique_ptr<TestInputContext> ic_;
    std::unique_ptr<fcitx::HandlerTableEntry<fcitx::EventHandler>> flush_watcher_;
    std::unique_ptr<fcitx::HandlerTableEntry<fcitx::EventHandler>> paragraph_destroy_watcher_;
    std::vector<std::unique_ptr<fcitx::EventSourceTime>> timers_;
};

} // namespace

int main(int argc, char **argv) {
    try {
        // Preserve the last synthetic stage if an addon aborts outside our guard.
        std::cout << std::unitbuf;
        const auto options = parse(argc, argv);
        if (options.paragraph || options.dictionary_pack) {
            // The learning audit intentionally writes synthetic data. Every
            // writable path is explicit and checked before loading any addon;
            // HOME stays unchanged and no personal Fcitx path is consulted.
            const auto join = [](const std::vector<std::string> &paths) {
                std::string value;
                for (const auto &path : paths) { if (!value.empty()) value += ':'; value += path; }
                return value;
            };
            setenv("FCITX_DATA_HOME", (options.root + "/data/fcitx5").c_str(), 1);
            setenv("FCITX_CONFIG_HOME", (options.root + "/config/fcitx5").c_str(), 1);
            setenv("FCITX_CONFIG_DIRS", (options.root + "/config/fcitx5").c_str(), 1);
            setenv("FCITX_DATA_DIRS", join(options.data_dirs).c_str(), 1);
            setenv("FCITX_ADDON_DIRS", join(options.addon_dirs).c_str(), 1);
            setenv("SKIP_FCITX_PATH", "1", 1);
            unsetenv("SKIP_FCITX_USER_PATH");
            const auto &paths = fcitx::StandardPaths::global();
            require(paths.userDirectory(fcitx::StandardPathsType::PkgData) ==
                        std::filesystem::path(options.root) / "data/fcitx5" &&
                    paths.userDirectory(fcitx::StandardPathsType::PkgConfig) ==
                        std::filesystem::path(options.root) / "config/fcitx5",
                    "paragraph learning audit writable paths are not isolated");
        } else {
            fcitx::setupTestingEnvironment(options.root, options.addon_dirs, options.data_dirs);
            require(std::string(std::getenv("SKIP_FCITX_USER_PATH")) == "1",
                    "Fcitx testing environment did not isolate user paths");
        }
        fcitx::Log::setLogRule("default=2");
        std::string enable = "--enable=keyboard,pinyin,punctuation,pinyinhelper,spell,quickphrase";
        if (options.with_transime) enable += ",transime";
        char name[] = "transime-headless-test";
        char disable[] = "--disable=all";
        char *instance_argv[] = {name, disable, enable.data()};
        fcitx::Instance instance(3, instance_argv);
        instance.addonManager().registerDefaultLoader(&transimeTestStaticAddons());
        Suite suite(instance, options);
        instance.eventDispatcher().schedule([&suite] { suite.start(); });
        instance.exec();
        return suite.result();
    } catch (const std::exception &error) {
        std::cerr << "FAIL: " << error.what() << '\n';
        return 1;
    }
}
