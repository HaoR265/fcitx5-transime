#include <algorithm>
#include <cstdint>
#include <deque>
#include <exception>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include <fcitx-config/configuration.h>
#include <fcitx-config/iniparser.h>
#include <fcitx-config/option.h>
#include <fcitx-utils/capabilityflags.h>
#include <fcitx-utils/event.h>
#include <fcitx-utils/eventdispatcher.h>
#include <fcitx-utils/key.h>
#include <fcitx-utils/misc.h>
#include <fcitx/addonfactory.h>
#include <fcitx/addoninstance.h>
#include <fcitx/addonmanager.h>
#include <fcitx/candidatelist.h>
#include <fcitx/event.h>
#include <fcitx/inputcontext.h>
#include <fcitx/inputcontextmanager.h>
#include <fcitx/inputcontextproperty.h>
#include <fcitx/inputpanel.h>
#include <fcitx/instance.h>
#include <fcitx/text.h>

#include "pinyin_public.h"
#include "session.h"
#include "translation_cache.h"
#include "transime_test_public.h"
#include "worker_client.h"

namespace fcitx {
namespace {
constexpr std::uint64_t historyLifetimeUsec = 120000000;
constexpr std::uint64_t fixtureDelayUsec = 20000;
constexpr const char *unavailableMessage = "TransIME：暂时无法翻译，请继续输入中文";
constexpr std::size_t maxDeferredKeys = 128;

#if TRANSIME_ENABLE_TEST_FIXTURE
#define TRANSIME_FIXTURE_OPTION \
    Option<bool> testFixture{this, "UseTestFixture", "Use synthetic translations for tests only", true}; \
    Option<int, IntConstrain> testHistoryLifetime{this, "FixtureHistoryLifetimeMilliseconds", "History expiry interval for tests only", 120000, IntConstrain(10, 120000)};
#else
#define TRANSIME_FIXTURE_OPTION
#endif

FCITX_CONFIGURATION(
    TransimeConfig,
    Option<bool> enabled{this, "Enabled", "Enable experimental translation", false};
    Option<bool> history{this, "EnableHistory", "Use recent IME commits (experimental)", false};
    Option<bool> contextAware{this, "ContextAware", "Use recent terminology", false};
    Option<bool> candidateTranslations{this, "CandidateTranslations", "Show translations beside candidates", true};
    Option<bool> continuous{this, "ContinuousComposition", "Keep punctuation and chosen words in the paragraph", true};
    Option<bool> showModeHints{this, "ShowModeHints", "Show paragraph language hints", false};
    Option<bool> fastContext{this, "FastContext", "Use low-latency terminology ranking", true};
    Option<bool> autoCommitOnPunctuation{this, "AutoCommitOnPunctuation", "Legacy punctuation submission outside continuous mode", false};
    Option<std::string> python{this, "PythonExecutable", "Absolute path to local Python executable", ""};
    Option<std::string> workerScript{this, "WorkerScript", "Absolute path to local translation worker", ""};
    Option<std::string> modelDirectory{this, "ModelDirectory", "Absolute path to local translation model", ""};
    Option<int, IntConstrain> threads{this, "WorkerThreads", "CPU translation threads", 2, IntConstrain(1, 2)};
    Option<int, IntConstrain> debounce{this, "DebounceMilliseconds", "Delay after candidate change (ms)", 60, IntConstrain(0, 1000)};
    Option<int, IntConstrain> timeout{this, "RequestTimeoutMilliseconds", "Translation request timeout (ms)", 2000, IntConstrain(100, 30000)};
    Option<int, IntConstrain> coldTimeout{this, "ColdStartTimeoutMilliseconds", "First request timeout (ms)", 10000, IntConstrain(1000, 120000)};
    Option<int, IntConstrain> idle{this, "WorkerIdleSeconds", "Unload worker after idle time (seconds)", 120, IntConstrain(10, 600)};
    TRANSIME_FIXTURE_OPTION
    KeyListOption translateKey{this, "TranslateKey", "Submit translation", {Key("Control+Return")}, KeyListConstrain(KeyConstrainFlag::AllowModifierLess)};
    KeyListOption clearKey{this, "ClearHistoryKey", "Clear translation history", {Key("Control+Alt+BackSpace")}, KeyListConstrain(KeyConstrainFlag::AllowModifierLess)};
    KeyListOption toggleKey{this, "ToggleKey", "Enable or disable translation", {}, KeyListConstrain(KeyConstrainFlag::AllowModifierLess)};
    KeyListOption chineseKey{this, "ChineseCommitKey", "Submit paragraph as Chinese", {Key("space")}, KeyListConstrain(KeyConstrainFlag::AllowModifierLess)};
    KeyListOption rawKey{this, "RawCommitKey", "Submit paragraph as typed", {Key("Return"), Key("KP_Enter")}, KeyListConstrain(KeyConstrainFlag::AllowModifierLess)};
    KeyListOption literalSpaceKey{this, "LiteralSpaceKey", "Insert a space in the paragraph", {Key("Shift+space")}, KeyListConstrain(KeyConstrainFlag::AllowModifierLess)};
);
#undef TRANSIME_FIXTURE_OPTION

bool validControls(const TransimeConfig &config) {
    return !config.chineseKey.value().empty() && !config.rawKey.value().empty() &&
        !config.literalSpaceKey.value().empty() && transimeValidControlKeys({
            config.translateKey.value(), config.clearKey.value(), config.toggleKey.value(),
            config.chineseKey.value(), config.rawKey.value(), config.literalSpaceKey.value()});
}

void restoreDefaultControls(TransimeConfig &config) {
    config.translateKey.setValue(KeyList{Key("Control+Return")});
    config.clearKey.setValue(KeyList{Key("Control+Alt+BackSpace")});
    config.toggleKey.setValue(KeyList{});
    config.chineseKey.setValue(KeyList{Key("space")});
    config.rawKey.setValue(KeyList{Key("Return"), Key("KP_Enter")});
    config.literalSpaceKey.setValue(KeyList{Key("Shift+space")});
}

void normalizeControls(TransimeConfig &config) {
    config.translateKey.setValue(transimeNormalizeControlKeys(config.translateKey.value()));
    config.clearKey.setValue(transimeNormalizeControlKeys(config.clearKey.value()));
    config.toggleKey.setValue(transimeNormalizeControlKeys(config.toggleKey.value()));
    config.chineseKey.setValue(transimeNormalizeControlKeys(config.chineseKey.value()));
    config.rawKey.setValue(transimeNormalizeControlKeys(config.rawKey.value()));
    config.literalSpaceKey.setValue(transimeNormalizeControlKeys(config.literalSpaceKey.value()));
}

struct TranslationTarget : PinyinTransimeSnapshotV1 {
    std::optional<PinyinTransimeParagraphSnapshotV2> paragraph;
    TranslationTarget() = default;
    TranslationTarget(PinyinTransimeSnapshotV1 value) : PinyinTransimeSnapshotV1(std::move(value)) {}
    explicit TranslationTarget(PinyinTransimeParagraphSnapshotV2 value)
        : PinyinTransimeSnapshotV1(value.activeCandidate), paragraph(std::move(value)) {
        ready = paragraph->ready;
        source = paragraph->source;
        revision = paragraph->revision;
    }
};

struct CandidateTranslationRow {
    int pageIndex = -1;
    TranslationTarget target;
    std::string translation;
};

struct DeferredKey {
    Key key, raw, original;
    bool release = false;
    int time = 0;
    bool forward = false;
    bool bypassAuto = false;
    bool triggerPair = false;
};

struct PunctuationCommit {
    TranslationTarget target;
    DeferredKey trigger;
    std::string punctuation;
    std::string sourcePunctuation;
    std::uint64_t boundary = 0;
};

struct TransimeState : InputContextProperty {
    ::transime::Session session;
    ::transime::TranslationCache cache;
    std::unique_ptr<EventSourceTime> cacheTimer;
    std::unique_ptr<EventSourceTime> rowTimer;
    bool active = false;
    bool refreshQueued = false;
    bool panelUpdateQueued = false;
    bool ownUI = false;
    std::optional<TranslationTarget> target;
    std::optional<::transime::RequestIdentity> pendingIdentity;
    std::unique_ptr<EventSourceTime> timer;
    std::unique_ptr<EventSourceTime> historyTimer;
    std::uint64_t workerRequest = 0;
    std::uint64_t rowWorkerRequest = 0;
    std::uint64_t rowGeneration = 0;
    bool rowMode = false;
    std::size_t nextRow = 0;
    std::vector<CandidateTranslationRow> rows;
    std::weak_ptr<CommonCandidateList> rowList;
    CandidateLayoutHint originalLayout = CandidateLayoutHint::NotSet;
    std::string displayed;
    std::optional<std::string> committingSource;
    std::string committingText;
    std::uint64_t commitSerial = 0;
    KeySym swallowedKey = FcitxKey_None;
    std::uint64_t lastCommit = 0;
    std::uint64_t inputBoundary = 0;
    std::optional<PunctuationCommit> punctuationCommit;
    std::deque<DeferredKey> deferredKeys;
    std::optional<DeferredKey> nativeTrigger;
    std::optional<Key> consumedPunctuation;
    bool replaying = false;
    bool replayScheduled = false;
    bool skipNextCommitReset = false;
    const KeyEvent *replayEvent = nullptr;
    const DeferredKey *replayKey = nullptr;
};

DeferredKey saveKey(const KeyEvent &event) {
    return {event.key(), event.rawKey(), event.origKey(), event.isRelease(),
            event.time(), event.forward(), false, false};
}

bool samePhysicalKey(const Key &a, const Key &b) {
    return a.code() && b.code() ? a.code() == b.code() : a.sym() == b.sym();
}

std::optional<std::string> punctuationFor(const KeyEvent &event) {
    const auto modifiers = event.rawKey().states() | event.origKey().states();
    if (modifiers.testAny(KeyStates(KeyState::Ctrl) | KeyState::Alt | KeyState::Super |
                          KeyState::Super2 | KeyState::Meta | KeyState::Hyper |
                          KeyState::Hyper2)) return std::nullopt;
    auto ch = Key::keySymToUnicode(event.key().sym());
    if (ch >= 0xff01 && ch <= 0xff5e) ch -= 0xfee0;
    if ((ch >= 0x21 && ch <= 0x2f) || (ch >= 0x3a && ch <= 0x40) ||
        (ch >= 0x5b && ch <= 0x60) || (ch >= 0x7b && ch <= 0x7e)) {
        return std::string(1, static_cast<char>(ch));
    }
    switch (ch) {
    case 0x3002: return ".";
    case 0x3001: return ",";
    case 0x2018: case 0x2019: return "'";
    case 0x201c: case 0x201d: case 0x300c: case 0x300d:
    case 0x300e: case 0x300f: return "\"";
    case 0x3008: return "<";
    case 0x3009: return ">";
    case 0x300a: return "<<";
    case 0x300b: return ">>";
    case 0x3010: case 0x3014: return "[";
    case 0x3011: case 0x3015: return "]";
    case 0x2010: case 0x2013: return "-";
    case 0x2014: return "--";
    case 0x2026: return "...";
    case 0x00b7: return ".";
    default: return std::nullopt;
    }
}

std::string withPunctuation(std::string translation, const std::string &punctuation) {
    while (!translation.empty() &&
           std::string(" \t\r\n.,!?;:").find(translation.back()) != std::string::npos) {
        translation.pop_back();
    }
    if (translation.empty()) return {};
    return translation + punctuation;
}

// A deliberately tiny synthetic backend; no inference runs in this module.
// Compiled out of builds that do not explicitly request test fixtures.
std::optional<std::string> fixtureTranslation(const std::string &source) {
#if TRANSIME_ENABLE_TEST_FIXTURE
    if (source == "你好") return "Hello.";
    if (source == "世界") return "World.";
    if (source == "中国") return "China.";
    if (source == "我们") return "We.";
    if (source == "你好世界") return "Hello, world.";
    if (source == "汉字") return "Chinese characters";
    if (source == "汗渍") return "Sweat stains";
    if (source == "汉子") return "Man";
#else
    (void)source;
#endif
    return std::nullopt;
}

bool sameTarget(const TranslationTarget &a,
                const TranslationTarget &b) {
    if (a.paragraph.has_value() != b.paragraph.has_value()) return false;
    if (a.paragraph) {
        const auto &x = *a.paragraph, &y = *b.paragraph;
        if (x.apiVersion != 2 || y.apiVersion != 2 || !x.hold || !y.hold ||
            !x.ready || !y.ready || x.draftId != y.draftId || x.revision != y.revision ||
            x.activeSegment != y.activeSegment || x.cursorBytes != y.cursorBytes ||
            x.pageIndex != y.pageIndex || x.source != y.source || x.rawInput != y.rawInput ||
            x.fullySelected != y.fullySelected) return false;
    }
    return a.apiVersion == 1 && b.apiVersion == 1 && a.ready && b.ready &&
           a.revision == b.revision && a.source == b.source &&
           a.selectedText == b.selectedText && a.candidateText == b.candidateText &&
           a.inputBytes == b.inputBytes && a.selectedInputBytes == b.selectedInputBytes &&
           a.candidateInputEnd == b.candidateInputEnd && a.cursorBytes == b.cursorBytes &&
           a.candidateIndex == b.candidateIndex;
}
} // namespace

class Transime final : public AddonInstance {
public:
    explicit Transime(Instance *instance) : instance_(instance), worker_(instance->eventLoop()) {
        readAsIni(config_, "conf/transime.conf");
        if (!validControls(config_)) restoreDefaultControls(config_);
        normalizeControls(config_);
        configureWorker();
        instance_->inputContextManager().registerProperty("transimeSessionV1", &factory_);
        watch(EventType::InputContextKeyEvent, EventWatcherPhase::PreInputMethod,
              [this](Event &event) { keyEvent(static_cast<KeyEvent &>(event)); });
        watch(EventType::InputContextUpdateUI, EventWatcherPhase::PostInputMethod,
              [this](Event &event) {
                  auto &update = static_cast<InputContextUpdateUIEvent &>(event);
                  auto *ic = update.inputContext();
                  if (update.component() == UserInterfaceComponent::InputPanel &&
                      !state(ic).ownUI) {
                      // A mouse or another engine action can change the
                      // highlighted candidate without passing our key watcher.
                      // Invalidate synchronously, even if queued refreshes are
                      // coalesced across A -> B -> A in one loop iteration.
                      clearCandidate(ic);
                      queueRefresh(ic);
                  }
              });
        for (auto type : {EventType::InputContextFocusIn,
                          EventType::InputContextFocusOut,
                          EventType::InputContextInputMethodActivated,
                          EventType::InputContextInputMethodDeactivated,
                          EventType::InputContextSwitchInputMethod,
                          EventType::InputContextCapabilityChanged}) {
            watch(type, EventWatcherPhase::PreInputMethod, [this](Event &event) {
                auto *ic = static_cast<InputContextEvent &>(event).inputContext();
                if (ic->capabilityFlags() & CapabilityFlag::PasswordOrSensitive) worker_.clear();
                resetSession(ic);
                queueRefresh(ic);
            });
        }
        watch(EventType::InputContextDestroyed, EventWatcherPhase::PreInputMethod,
              [this](Event &event) {
                  auto *ic = static_cast<InputContextEvent &>(event).inputContext();
                  auto &s = state(ic);
                  cancelAutomation(ic);
                  s.timer.reset();
                  worker_.cancel(s.workerRequest);
                  s.workerRequest = 0;
                  clearRows(ic, false);
                  s.historyTimer.reset();
                  clearCache(ic);
                  s.session.end_session();
                  s.active = false;
              });
        watch(EventType::InputContextReset, EventWatcherPhase::PreInputMethod,
              [this](Event &event) {
                  auto *ic = static_cast<InputContextEvent &>(event).inputContext();
                  auto &s = state(ic);
                  if (s.skipNextCommitReset) s.skipNextCommitReset = false;
                  else cancelAutomation(ic);
                  clearCache(ic);
                  clearCandidate(ic);
              });
        watch(EventType::InputContextCommitString, EventWatcherPhase::PostInputMethod,
              [this](Event &event) {
                  auto &commit = static_cast<CommitStringEvent &>(event);
                  recordCommit(commit.inputContext(), commit.text());
              });
        watch(EventType::InputContextCommitStringWithCursor, EventWatcherPhase::PostInputMethod,
              [this](Event &event) {
                  auto &commit = static_cast<CommitStringWithCursorEvent &>(event);
                  recordCommit(commit.inputContext(), commit.text());
              });
    }

    ~Transime() override {
        alive_.reset();
        handlers_.clear();
        // WorkerClient's destructor closes watchers and terminates the child
        // without rearming timers after Instance's event loop has stopped.
        factory_.unregister();
    }

    const Configuration *getConfig() const override { return &config_; }
    void setConfig(const RawConfig &config) override {
        RawConfig previous;
        config_.save(previous);
        config_.load(config, true);
        if (!validControls(config_)) {
            config_.load(previous, true);
            throw std::invalid_argument("TransIME shortcuts conflict or replace ordinary typing keys");
        }
        normalizeControls(config_);
        resetAll();
        configureWorker();
        safeSaveAsIni(config_, "conf/transime.conf");
    }
    void reloadConfig() override {
        readAsIni(config_, "conf/transime.conf");
        if (!validControls(config_)) restoreDefaultControls(config_);
        normalizeControls(config_);
        resetAll();
        configureWorker();
    }

#if TRANSIME_ENABLE_TEST_FIXTURE
    std::vector<::transime::CommitRecord> fixtureHistory(InputContext *ic) {
        return state(ic).session.history();
    }
#endif

private:
#if TRANSIME_ENABLE_TEST_FIXTURE
    FCITX_ADDON_EXPORT_FUNCTION(Transime, fixtureHistory);
#endif
    TransimeState &state(InputContext *ic) { return *ic->propertyFor(&factory_); }

    void configureWorker() {
        worker_.configure({config_.python.value(), config_.workerScript.value(),
                           config_.modelDirectory.value(), config_.threads.value(),
                           static_cast<std::uint64_t>(config_.timeout.value()),
                           static_cast<std::uint64_t>(config_.coldTimeout.value()),
                           static_cast<std::uint64_t>(config_.idle.value()) * 1000,
                           config_.fastContext.value()});
    }

    bool useFixture() const {
#if TRANSIME_ENABLE_TEST_FIXTURE
        return config_.testFixture.value();
#else
        return false;
#endif
    }

    std::uint64_t historyLifetime() const {
#if TRANSIME_ENABLE_TEST_FIXTURE
        return static_cast<std::uint64_t>(config_.testHistoryLifetime.value()) * 1000;
#else
        return historyLifetimeUsec;
#endif
    }

    void clearCache(InputContext *ic) {
        auto &s = state(ic);
        s.cacheTimer.reset();
        s.cache.clear();
    }

    void armCacheExpiry(InputContext *ic) {
        auto &s = state(ic);
        auto expiry = s.cache.next_expiry();
        if (!expiry) { s.cacheTimer.reset(); return; }
        auto ref = ic->watch();
        std::weak_ptr<int> alive = alive_;
        s.cacheTimer = instance_->eventLoop().addTimeEvent(
            CLOCK_MONOTONIC, *expiry * 1000, 0,
            [this, alive, ref](EventSourceTime *timer, std::uint64_t stamp) {
                timer->setEnabled(false);
                if (!alive.expired() && ref.isValid()) {
                    state(ref.get()).cache.prune(stamp / 1000);
                    // Do not delete the current timer inside its own callback.
                    instance_->eventDispatcher().schedule([this, alive, ref] {
                        if (!alive.expired() && ref.isValid()) armCacheExpiry(ref.get());
                    });
                }
                return true;
            });
        s.cacheTimer->setOneShot();
    }

    void cacheResponse(InputContext *ic, const ::transime::RequestSnapshot &request,
                       const std::string &translation) {
        if (state(ic).cache.put(request, translation, now(CLOCK_MONOTONIC) / 1000))
            armCacheExpiry(ic);
    }

    void expireHistory(InputContext *ic) {
        auto &s = state(ic);
        // Expiry invalidates the translation's memory, but it is not a user
        // cancellation. Preserve already captured input through native replay
        // before clearing the request and its history on this same context.
        if (s.punctuationCommit && eligible(ic)) fallbackPunctuation(ic);
        ++s.commitSerial;
        s.session.clear_history();
        clearCache(ic);
        s.lastCommit = 0;
        worker_.clear();
        clearCandidate(ic);
        queueRefresh(ic);
    }

    void armHistoryExpiry(InputContext *ic) {
        auto &s = state(ic);
        auto ref = ic->watch();
        std::weak_ptr<int> alive = alive_;
        s.historyTimer = instance_->eventLoop().addTimeEvent(
            CLOCK_MONOTONIC, s.lastCommit + historyLifetime(), 0,
            [this, alive, ref](EventSourceTime *timer, std::uint64_t) {
                timer->setEnabled(false);
                if (!alive.expired() && ref.isValid()) expireHistory(ref.get());
                return true;
            });
        s.historyTimer->setOneShot();
    }

    void watch(EventType type, EventWatcherPhase phase, EventHandler handler) {
        handlers_.push_back(instance_->watchEvent(type, phase, std::move(handler)));
    }

    bool eligible(InputContext *ic) const {
        return config_.enabled.value() && ic->hasFocus() &&
               !(ic->capabilityFlags() & CapabilityFlag::PasswordOrSensitive) &&
               instance_->inputMethod(ic) == "pinyin";
    }

    void cancelAutomation(InputContext *ic) {
        auto &s = state(ic);
        ++s.inputBoundary;
        s.punctuationCommit.reset();
        s.deferredKeys.clear();
        s.nativeTrigger.reset();
        s.consumedPunctuation.reset();
        s.replayScheduled = false;
    }

    bool commitTranslation(InputContext *ic, const std::string &source,
                           const std::string &translation) {
        auto &s = state(ic);
        auto ref = ic->watch();
        const auto serialBefore = s.commitSerial;
        const auto boundary = s.inputBoundary;
        s.skipNextCommitReset = true;
        ic->reset();
        if (!ref.isValid()) return false;
        s.skipNextCommitReset = false;
        // Only our first reset is expected. Nested resets, focus/configuration
        // changes, or commits from another watcher invalidate the intention.
        if (s.commitSerial != serialBefore || s.inputBoundary != boundary ||
            !eligible(ic) || composing(ic) || ic->hasPendingEventsStrictOrder()) return false;
        s.committingSource = source;
        s.committingText = translation;
        const auto serial = ++s.commitSerial;
        ic->commitString(translation);
        if (!ref.isValid()) return false;
        std::weak_ptr<int> alive = alive_;
        instance_->eventDispatcher().schedule([this, alive, ref, serial] {
            if (alive.expired() || !ref.isValid()) return;
            auto &current = state(ref.get());
            if (current.commitSerial == serial) {
                current.committingSource.reset();
                current.committingText.clear();
            }
        });
        clearCandidate(ic);
        return s.inputBoundary == boundary && eligible(ic);
    }

    void scheduleReplay(InputContext *ic) {
        auto &s = state(ic);
        if (s.replayScheduled || (s.deferredKeys.empty() && !s.nativeTrigger) ||
            s.punctuationCommit) return;
        s.replayScheduled = true;
        const auto boundary = s.inputBoundary;
        auto ref = ic->watch();
        std::weak_ptr<int> alive = alive_;
        instance_->eventDispatcher().schedule([this, alive, ref, boundary] {
            if (alive.expired() || !ref.isValid()) return;
            auto &current = state(ref.get());
            if (current.inputBoundary != boundary) return;
            current.replayScheduled = false;
            replayKeys(ref.get(), boundary);
        });
    }

    void replayKeys(InputContext *ic, std::uint64_t boundary, bool makeRoom = false) {
        auto &s = state(ic);
        if ((s.replaying && !makeRoom) || s.inputBoundary != boundary || !eligible(ic)) return;
        auto ref = ic->watch();
        const auto wasReplaying = s.replaying;
        const auto *previousEvent = s.replayEvent;
        const auto *previousKey = s.replayKey;
        s.replaying = true;
        s.replayScheduled = false;
        while (!s.punctuationCommit && (s.nativeTrigger || !s.deferredKeys.empty()) &&
               s.inputBoundary == boundary && eligible(ic)) {
            DeferredKey saved;
            if (s.nativeTrigger) {
                saved = std::move(*s.nativeTrigger);
                s.nativeTrigger.reset();
            } else {
                saved = std::move(s.deferredKeys.front());
                s.deferredKeys.pop_front();
            }
            if (saved.triggerPair) continue;
            // Overflow drains run inside the frontend's outer event blocker.
            // Native commits from earlier keys may not be delivered yet, so
            // keep this bounded drain native instead of creating another
            // translation intent that those delayed commits would invalidate.
            if (makeRoom) saved.bypassAuto = true;
            // Preserve the physical/original key and timestamp for forwarding.
            // The pre-IME watcher restores the captured layout-converted keys
            // after Fcitx's ReservedFirst layout handler has run again.
            KeyEvent replay(ic, saved.original, saved.release, saved.time);
            replay.setRawKey(saved.raw);
            replay.setKey(saved.key);
            replay.setForward(saved.forward);
            s.replayEvent = &replay;
            s.replayKey = &saved;
            const bool handled = ic->keyEvent(replay);
            if (!ref.isValid()) return;
            s.replayEvent = previousEvent;
            s.replayKey = previousKey;
            if (s.inputBoundary != boundary || !eligible(ic)) break;
            if (!handled) ic->forwardKey(saved.original, saved.release, saved.time);
            if (!ref.isValid()) return;
        }
        s.replaying = wasReplaying;
    }

    void fallbackPunctuation(InputContext *ic) {
        auto &s = state(ic);
        if (!s.punctuationCommit) return;
        auto intent = std::move(*s.punctuationCommit);
        s.punctuationCommit.reset();
        if (s.inputBoundary != intent.boundary || !eligible(ic)) {
            cancelAutomation(ic);
            return;
        }
        s.consumedPunctuation.reset();
        // Restore the original press as well as its paired releases/repeats.
        // Only these events bypass automatic translation; later sentences can
        // still start their own bounded wait when replay reaches them.
        for (auto &key : s.deferredKeys) {
            if (key.triggerPair) { key.triggerPair = false; key.bypassAuto = true; }
        }
        intent.trigger.bypassAuto = true;
        // Keep the original trigger separate: the subsequent-key FIFO itself
        // never temporarily exceeds its 128-event bound during fallback.
        s.nativeTrigger = std::move(intent.trigger);
        clearCandidate(ic);
        if (ic->inputPanel().auxDown().empty()) {
            s.displayed = unavailableMessage;
            ic->inputPanel().setAuxDown(Text(s.displayed));
            updatePanel(ic);
        }
        scheduleReplay(ic);
    }

    bool completePunctuation(InputContext *ic) {
        auto &s = state(ic);
        if (!s.punctuationCommit) return false;
        const auto intent = *s.punctuationCommit;
        auto actual = snapshot(ic);
        if (!actual || !sameTarget(*actual, intent.target) ||
            intent.boundary != s.inputBoundary || !eligible(ic)) {
            cancelAutomation(ic);
            return true;
        }
        auto ready = s.session.ready_translation();
        if (!ready) return false;
        auto translation = withPunctuation(*ready, intent.punctuation);
        const auto source = intent.target.source + intent.sourcePunctuation;
        if (!validRowTranslation(source, translation)) {
            fallbackPunctuation(ic);
            return true;
        }
        s.session.consume_translation();
        s.punctuationCommit.reset();
        auto ref = ic->watch();
        if (commitTranslation(ic, source, translation) && ref.isValid()) {
            scheduleReplay(ref.get());
        } else if (ref.isValid()) {
            // A reset-side boundary change cannot safely replay into a field
            // whose composition has just been replaced by another handler.
            cancelAutomation(ref.get());
        }
        return true;
    }

    void beginPunctuation(InputContext *ic, KeyEvent &event,
                          const TranslationTarget &target,
                          const std::string &punctuation) {
        auto &s = state(ic);
        if (!s.target || !sameTarget(*s.target, target)) refresh(ic);
        if (!s.target || !sameTarget(*s.target, target) || !s.pendingIdentity) return;
        auto sourcePunctuation = Key::keySymToUTF8(event.key().sym());
        s.punctuationCommit = PunctuationCommit{target, saveKey(event), punctuation,
                                                sourcePunctuation, s.inputBoundary};
        s.consumedPunctuation = event.origKey();
        event.filterAndAccept();
        // Background comments are disposable. The one highlighted request may
        // finish, or its debounce is advanced to the next event-loop turn.
        worker_.cancel(s.rowWorkerRequest);
        s.rowWorkerRequest = 0;
        ++s.rowGeneration;
        if (completePunctuation(ic)) return;
        if (s.workerRequest) return;
        if (s.timer) {
            s.timer->setTime(now(CLOCK_MONOTONIC));
            s.timer->setOneShot();
        } else {
            fallbackPunctuation(ic);
        }
    }

    void updatePanel(InputContext *ic) {
        auto &s = state(ic);
        if (s.panelUpdateQueued) return;
        s.panelUpdateQueued = true;
        auto ref = ic->watch();
        std::weak_ptr<int> alive = alive_;
        // UI events are synchronous in Fcitx and third-party watchers may
        // destroy the IC. Dispatch only after our state operation is complete;
        // never retain a state reference across the external event callback.
        instance_->eventDispatcher().schedule([this, alive, ref] {
            if (alive.expired() || !ref.isValid()) return;
            auto *live = ref.get();
            state(live).panelUpdateQueued = false;
            state(live).ownUI = true;
            live->updateUserInterface(UserInterfaceComponent::InputPanel);
            if (alive.expired() || !ref.isValid()) return;
            live = ref.get();
            state(live).ownUI = false;
            const auto target = state(live).target;
            if (target) {
                const auto actual = snapshot(live);
                if (!eligible(live) || !actual || !sameTarget(*target, *actual)) {
                    clearCandidate(live);
                    queueRefresh(live);
                }
            }
        });
    }

    void clearDisplay(InputContext *ic) {
        auto &s = state(ic);
        if (!s.displayed.empty() && ic->inputPanel().auxDown().toString() == s.displayed) {
            ic->inputPanel().setAuxDown(Text());
            s.displayed.clear();
            updatePanel(ic);
        } else {
            s.displayed.clear();
        }
    }

    void clearCandidate(InputContext *ic) {
        auto &s = state(ic);
        if (s.punctuationCommit) cancelAutomation(ic);
        s.timer.reset();
        worker_.cancel(s.workerRequest);
        s.workerRequest = 0;
        clearRows(ic);
        s.target.reset();
        s.pendingIdentity.reset();
        s.session.set_candidate("");
        clearDisplay(ic);
    }

    void resetSession(InputContext *ic) {
        auto &s = state(ic);
        cancelAutomation(ic);
        clearCandidate(ic);
        s.session.end_session();
        clearCache(ic);
        s.historyTimer.reset();
        s.active = false;
        s.lastCommit = 0;
        s.committingSource.reset();
        s.committingText.clear();
        ++s.commitSerial;
        s.swallowedKey = FcitxKey_None;
    }

    void resetAll() {
        worker_.clear();
        instance_->inputContextManager().foreach([this](InputContext *ic) {
            auto ref = ic->watch();
            resetSession(ic);
            syncHold(ic);
            if (ref.isValid()) queueRefresh(ref.get());
            return true;
        });
    }

    void syncHold(InputContext *ic) {
        auto *addon = instance_->addonManager().addon("pinyin", false);
        if (!addon) return;
        auto ref = ic->watch();
        try {
            PinyinTransimeControlsV1 controls;
            controls.showModeHints = config_.showModeHints.value();
            controls.chineseCommit = config_.chineseKey.value();
            controls.rawCommit = config_.rawKey.value();
            controls.literalSpace = config_.literalSpaceKey.value();
            addon->call<IPinyin::transimeSetControlsV1>(ic, std::move(controls));
        } catch (const std::exception &) {
            // Old bridges retain their historical keys. Capability is optional;
            // the settings application must flag custom controls as unsupported.
        }
        if (!ref.isValid()) return;
        try {
            // Focus loss only suspends a paragraph; it must never disable hold.
            addon->call<IPinyin::transimeSetHoldV1>(ic,
                config_.enabled.value() && config_.continuous.value());
        } catch (const std::exception &) { }
    }

    bool holding(InputContext *ic) {
        auto *addon = instance_->addonManager().addon("pinyin", false);
        if (!addon) return false;
        try { return addon->call<IPinyin::transimeParagraphSnapshotV2>(ic, -1).hold; }
        catch (const std::exception &) { return false; }
    }

    bool ensureActive(InputContext *ic) {
        auto ref = ic->watch();
        syncHold(ic);
        if (!ref.isValid()) return false;
        auto &s = state(ic);
        if (!eligible(ic)) {
            if (s.active || s.target || !s.displayed.empty()) resetSession(ic);
            return false;
        }
        if (!s.active) {
            s.session.begin_session();
            s.active = true;
        }
        if (s.lastCommit && now(CLOCK_MONOTONIC) - s.lastCommit > historyLifetime()) {
            s.historyTimer.reset();
            expireHistory(ic);
        }
        return true;
    }

    std::optional<TranslationTarget> snapshot(InputContext *ic) {
        return snapshotAt(ic, -1);
    }

    std::optional<TranslationTarget> snapshotAt(InputContext *ic, int pageIndex) {
        auto *addon = instance_->addonManager().addon("pinyin", false);
        if (!addon) return std::nullopt;
        try {
            auto paragraph = addon->call<IPinyin::transimeParagraphSnapshotV2>(ic, pageIndex);
            if (paragraph.hold) {
                if (paragraph.apiVersion != 2 || !paragraph.ready ||
                    paragraph.source.empty() || paragraph.source.size() > 4096) return std::nullopt;
                return TranslationTarget(std::move(paragraph));
            }
        } catch (const std::exception &) { }
        try {
            auto target = pageIndex < 0 ? addon->call<IPinyin::transimeSnapshotV1>(ic) :
                addon->call<IPinyin::transimeSnapshotAtV1>(ic, pageIndex);
            if (target.apiVersion != 1 || !target.ready || target.source.empty() ||
                target.source.size() > 4096 || target.cursorBytes != target.inputBytes ||
                target.candidateInputEnd != target.inputBytes) return std::nullopt;
            return TranslationTarget(std::move(target));
        } catch (const std::exception &) { return std::nullopt; }
    }

    bool setRowComment(InputContext *ic, const CandidateTranslationRow &row,
                       const std::string &translation) {
        auto *addon = instance_->addonManager().addon("pinyin", false);
        if (!addon) return false;
        try {
            if (row.target.paragraph) return addon->call<IPinyin::transimeSetParagraphCommentV2>(
                ic, row.pageIndex, *row.target.paragraph, translation);
            return addon->call<IPinyin::transimeSetCommentV1>(
                ic, row.pageIndex, row.target, translation);
        } catch (const std::exception &) {
            // Older bridges keep the original auxiliary-line presentation.
            return false;
        }
    }

    void clearRows(InputContext *ic, bool notify = true) {
        auto &s = state(ic);
        ++s.rowGeneration;
        s.rowTimer.reset();
        worker_.cancel(s.rowWorkerRequest);
        s.rowWorkerRequest = 0;
        bool changed = false;
        for (const auto &row : s.rows) {
            if (!row.translation.empty()) changed = setRowComment(ic, row, "") || changed;
        }
        if (auto list = s.rowList.lock()) {
            // Restore only our own forced layout, preserving subsequent changes
            // by the engine or another addon.
            if (list->layoutHint() == CandidateLayoutHint::Vertical &&
                list->layoutHint() != s.originalLayout) {
                list->setLayoutHint(s.originalLayout);
                changed = changed || list == ic->inputPanel().candidateList();
            }
        }
        s.rows.clear();
        s.rowList.reset();
        s.rowMode = false;
        s.nextRow = 0;
        if (changed && notify) updatePanel(ic);
    }

    void prepareRows(InputContext *ic, const TranslationTarget &target) {
        if (!config_.candidateTranslations.value()) return;
        auto list = std::dynamic_pointer_cast<CommonCandidateList>(ic->inputPanel().candidateList());
        if (!list) return;
        std::vector<CandidateTranslationRow> rows;
        bool foundMain = false;
        const auto count = std::min(list->size(), 10);
        for (int pageIndex = 0; pageIndex < count; ++pageIndex) {
            auto rowTarget = snapshotAt(ic, pageIndex);
            if (!rowTarget) continue; // Partial or non-pinyin rows remain Chinese.
            foundMain = foundMain || sameTarget(*rowTarget, target);
            rows.push_back({pageIndex, std::move(*rowTarget), {}});
        }
        if (!foundMain) return;
        const auto main = std::find_if(rows.begin(), rows.end(), [&target](const auto &row) {
            return sameTarget(row.target, target);
        });
        // Probe both versioned functions before changing presentation. An empty
        // comment restores the original value and never alters the candidate.
        if (!setRowComment(ic, *main, "")) return;
        auto &s = state(ic);
        s.rows = std::move(rows);
        s.rowMode = true;
        s.rowList = list;
        s.originalLayout = list->layoutHint();
        list->setLayoutHint(CandidateLayoutHint::Vertical);
        updatePanel(ic);
    }

    bool rowIsCurrent(InputContext *ic, std::uint64_t generation,
                      const CandidateTranslationRow &row) {
        auto &s = state(ic);
        if (!eligible(ic) || !s.rowMode || s.rowGeneration != generation ||
            s.rowList.lock() != ic->inputPanel().candidateList()) return false;
        auto actual = snapshotAt(ic, row.pageIndex);
        return actual && sameTarget(row.target, *actual);
    }

    static bool validRowTranslation(const std::string &source,
                                    const std::string &translation) {
        // Background rows must not mutate the highlighted candidate's Session.
        // A disposable Session applies the same UTF-8/control/size validation.
        ::transime::Session validator;
        validator.begin_session();
        if (!validator.set_candidate(source)) return false;
        auto request = validator.request_snapshot();
        return request && validator.accept_response(*request, translation);
    }

    void scheduleRows(InputContext *ic, const ::transime::RequestSnapshot &request,
                      const TranslationTarget &target, std::uint64_t generation) {
        auto &s = state(ic);
        auto ref = ic->watch();
        std::weak_ptr<int> alive = alive_;
        s.rowTimer = instance_->eventLoop().addTimeEvent(
            CLOCK_MONOTONIC, now(CLOCK_MONOTONIC) + (useFixture() ? 0 : 300000), 0,
            [this, alive, ref, request, target, generation](EventSourceTime *timer, std::uint64_t) {
                timer->setEnabled(false);
                if (!alive.expired() && ref.isValid()) pumpRows(ref.get(), request, target, generation);
                return true;
            });
        s.rowTimer->setOneShot();
    }

    void pumpRows(InputContext *ic, const ::transime::RequestSnapshot &request,
                  const TranslationTarget &target, std::uint64_t generation) {
        auto &s = state(ic);
        if (!s.rowMode || s.rowGeneration != generation ||
            !responseIsCurrent(ic, request, target)) return;
        while (s.nextRow < s.rows.size()) {
            const auto index = s.nextRow++;
            const auto row = s.rows[index];
            if (sameTarget(row.target, target)) continue;
            if (!rowIsCurrent(ic, generation, row)) return;
            auto wireRequest = request;
            wireRequest.source = row.target.source;
            const bool context = config_.history.value() && config_.contextAware.value();
            if (!context) wireRequest.history.clear();
            if (auto cached = s.cache.get(wireRequest, now(CLOCK_MONOTONIC) / 1000)) {
                if (setRowComment(ic, row, *cached)) {
                    s.rows[index].translation = *cached;
                    updatePanel(ic);
                }
                continue;
            }
            auto ref = ic->watch();
            std::weak_ptr<int> alive = alive_;
            auto complete = [this, alive, ref, request, target, generation, index, row, wireRequest]
                            (::transime::WorkerResult result) {
                if (alive.expired() || !ref.isValid()) return;
                auto *currentIC = ref.get();
                if (!rowIsCurrent(currentIC, generation, row) ||
                    !responseIsCurrent(currentIC, request, target)) return;
                auto &current = state(currentIC);
                current.rowWorkerRequest = 0;
                if (result.error == ::transime::WorkerError::None &&
                    validRowTranslation(row.target.source, result.translation) &&
                    setRowComment(currentIC, row, result.translation)) {
                    cacheResponse(currentIC, wireRequest, result.translation);
                    current.rows[index].translation = std::move(result.translation);
                    updatePanel(currentIC);
                }
                // Failed rows preserve their original Chinese candidate and
                // comment. Only this live generation can schedule another row.
                if (!alive.expired() && ref.isValid()) {
                    scheduleRows(ref.get(), request, target, generation);
                }
            };
            if (useFixture()) {
                auto translation = fixtureTranslation(row.target.source);
                instance_->eventDispatcher().schedule(
                    [complete = std::move(complete), translation]() mutable {
                        complete({translation.value_or(""), translation ?
                            ::transime::WorkerError::None : ::transime::WorkerError::Backend});
                    });
            } else {
                s.rowWorkerRequest = worker_.submit(wireRequest, context, std::move(complete));
            }
            return;
        }
    }

    bool rowTranslationVisible(InputContext *ic) {
        const auto &s = state(ic);
        if (!s.rowMode || !s.target) return false;
        auto list = s.rowList.lock();
        if (!list || list != ic->inputPanel().candidateList()) return false;
        for (const auto &row : s.rows) {
            if (sameTarget(row.target, *s.target) && !row.translation.empty() &&
                row.pageIndex >= 0 && row.pageIndex < list->size() &&
                rowIsCurrent(ic, s.rowGeneration, row)) {
                return list->candidate(row.pageIndex).comment().toString() == row.translation;
            }
        }
        return false;
    }

    void queueRefresh(InputContext *ic) {
        auto &s = state(ic);
        if (s.refreshQueued) return;
        s.refreshQueued = true;
        auto ref = ic->watch();
        std::weak_ptr<int> alive = alive_;
        instance_->eventDispatcher().schedule([this, alive, ref] {
            if (alive.expired() || !ref.isValid()) return;
            state(ref.get()).refreshQueued = false;
            refresh(ref.get());
        });
    }

    bool canDisplay(InputContext *ic) {
        if (state(ic).rowMode) {
            return state(ic).rowList.lock() == ic->inputPanel().candidateList();
        }
        auto text = ic->inputPanel().auxDown().toString();
        return text.empty() || text == state(ic).displayed;
    }

    void refresh(InputContext *ic) {
        if (!ensureActive(ic)) return;
        auto &s = state(ic);
        auto target = snapshot(ic);
        if (!target) {
            clearCandidate(ic);
            return;
        }
        if (s.target && sameTarget(*s.target, *target)) return;
        clearCandidate(ic);
        s.target = *target;
        prepareRows(ic, *target);
        if (!canDisplay(ic)) {
            clearCandidate(ic);
            return;
        }
        s.session.set_candidate(target->source);
        auto request = s.session.request_snapshot();
        if (!request) return;
        s.pendingIdentity = request->identity;
        const auto fixture = useFixture();
        if (auto cached = s.cache.get(*request, now(CLOCK_MONOTONIC) / 1000)) {
            displayResponse(ic, *request, *target, *cached, fixture, true);
            return;
        }
        auto translation = fixture ? fixtureTranslation(target->source) : std::nullopt;
        if (fixture && !translation) return;
        auto ref = ic->watch();
        std::weak_ptr<int> alive = alive_;
        s.timer = instance_->eventLoop().addTimeEvent(
            CLOCK_MONOTONIC, now(CLOCK_MONOTONIC) + (fixture ? fixtureDelayUsec :
                static_cast<std::uint64_t>(config_.debounce.value()) * 1000), 0,
            [this, alive, ref, request = *request, target = *target, translation, fixture]
            (EventSourceTime *timer, std::uint64_t) {
                timer->setEnabled(false);
                if (alive.expired() || !ref.isValid()) return true;
                auto *currentIC = ref.get();
                if (!eligible(currentIC)) return true;
                auto &current = state(currentIC);
                auto actual = snapshot(currentIC);
                if (!actual || !current.target || !sameTarget(target, *actual) ||
                    !sameTarget(target, *current.target) || !canDisplay(currentIC)) return true;
                if (fixture) {
                    displayResponse(currentIC, request, target, *translation, true);
                    return true;
                }
                auto wireRequest = request;
                const bool context = config_.history.value() && config_.contextAware.value();
                // Do not send retained history to a sentence-only backend when
                // contextual use has not been explicitly enabled.
                if (!context) wireRequest.history.clear();
                current.workerRequest = worker_.submit(wireRequest, context,
                    [this, alive, ref, request, target](::transime::WorkerResult result) {
                        if (alive.expired() || !ref.isValid()) return;
                        if (result.error != ::transime::WorkerError::None) {
                            displayFailure(ref.get(), request, target);
                            return;
                        }
                        displayResponse(ref.get(), request, target, std::move(result.translation), false);
                    });
                return true;
            });
        s.timer->setOneShot();
    }

    bool responseIsCurrent(InputContext *ic, const ::transime::RequestSnapshot &request,
                           const TranslationTarget &target) {
        if (!eligible(ic)) return false;
        auto &current = state(ic);
        auto actual = snapshot(ic);
        return actual && current.target && current.pendingIdentity &&
               *current.pendingIdentity == request.identity &&
               sameTarget(target, *actual) && sameTarget(target, *current.target) && canDisplay(ic);
    }

    void displayFailure(InputContext *ic, const ::transime::RequestSnapshot &request,
                        const TranslationTarget &target) {
        if (!responseIsCurrent(ic, request, target)) return;
        auto &current = state(ic);
        current.workerRequest = 0;
        if (current.punctuationCommit) {
            fallbackPunctuation(ic);
            return;
        }
        // A status line is never passed to Session::accept_response, so the
        // translation shortcut cannot submit it. No child error text is shown.
        const auto existing = ic->inputPanel().auxDown().toString();
        if (!existing.empty() && existing != current.displayed) return;
        current.displayed = unavailableMessage;
        ic->inputPanel().setAuxDown(Text(current.displayed));
        updatePanel(ic);
    }

    void displayResponse(InputContext *ic, const ::transime::RequestSnapshot &request,
                         const TranslationTarget &target,
                         std::string translation, bool fixture, bool cached = false) {
        if (!responseIsCurrent(ic, request, target)) return;
        auto &current = state(ic);
        if (!current.session.accept_response(request, translation)) {
            if (!fixture) displayFailure(ic, request, target);
            return;
        }
        current.workerRequest = 0;
        if (!cached) cacheResponse(ic, request, translation);
        if (completePunctuation(ic)) return;
        if (current.rowMode) {
            auto row = std::find_if(current.rows.begin(), current.rows.end(),
                [&target](const auto &item) { return sameTarget(item.target, target); });
            if (row == current.rows.end() || !setRowComment(ic, *row, translation)) {
                displayFailure(ic, request, target);
                return;
            }
            row->translation = std::move(translation);
            auto ref = ic->watch();
            const auto generation = current.rowGeneration;
            clearDisplay(ic);
            if (!ref.isValid()) return;
            updatePanel(ic);
            // The highlighted result is ready before any background work starts.
            // Every other row has its own request id and cannot consume it.
            if (ref.isValid()) scheduleRows(ref.get(), request, target, generation);
            return;
        }
        current.displayed = (fixture ? "TransIME [fixture]: " : "TransIME: ") + translation;
        ic->inputPanel().setAuxDown(Text(current.displayed));
        updatePanel(ic);
    }

    void recordCommit(InputContext *ic, const std::string &text) {
        if (!ensureActive(ic)) return;
        auto &s = state(ic);
        // Only the observable outgoing IME event is recorded. This is not
        // confirmation that an application received or retained the text.
        ++s.commitSerial;
        clearCache(ic);
        if (config_.history.value()) {
            const auto source = text == s.committingText ? s.committingSource : std::nullopt;
            if (s.session.record_commit({source, text, source ? "en" : "und"})) {
                s.lastCommit = now(CLOCK_MONOTONIC);
                armHistoryExpiry(ic);
            }
        }
        s.committingSource.reset();
        s.committingText.clear();
        clearCandidate(ic);
    }

    bool composing(InputContext *ic) const {
        const auto &panel = ic->inputPanel();
        return !panel.preedit().empty() || !panel.clientPreedit().empty() ||
               (panel.candidateList() && panel.candidateList()->size() > 0);
    }

    void keyEvent(KeyEvent &event) {
        auto *ic = event.inputContext();
        auto &s = state(ic);
        const bool replay = s.replayEvent == &event && s.replayKey;
        const bool bypassAuto = replay && s.replayKey->bypassAuto;
        if (replay) {
            event.setRawKey(s.replayKey->raw);
            event.setKey(s.replayKey->key);
            event.setForward(s.replayKey->forward);
        }
        const bool repeat = event.rawKey().states().test(KeyState::Repeat) ||
                            event.origKey().states().test(KeyState::Repeat);
        bool triggerPair = false;
        if (!bypassAuto && s.consumedPunctuation &&
            samePhysicalKey(*s.consumedPunctuation, event.origKey())) {
            triggerPair = event.isRelease() || repeat;
            if (event.isRelease() || !repeat) s.consumedPunctuation.reset();
            if (triggerPair && !s.punctuationCommit) {
                event.filterAndAccept();
                return;
            }
        }
        if (s.punctuationCommit || (!replay && s.replayScheduled)) {
            if (!event.isRelease() && event.key().check(FcitxKey_Escape)) {
                cancelAutomation(ic);
                clearCandidate(ic);
                // Let pinyin cancel its actual composition now.
            } else {
                auto ref = ic->watch();
                const auto boundary = s.inputBoundary;
                bool nativePair = false;
                while (s.deferredKeys.size() >= maxDeferredKeys) {
                    // Drain native fallback before accepting another event;
                    // never silently drop the event that reaches the bound.
                    if (s.punctuationCommit) {
                        // This event may be the triggering key's release or
                        // repeat. After native fallback it must also reach the
                        // engine, instead of being skipped as a successful
                        // automatic commit's paired event.
                        nativePair = nativePair || triggerPair;
                        triggerPair = false;
                        fallbackPunctuation(ic);
                    }
                    replayKeys(ic, boundary, true);
                    if (!ref.isValid()) { event.filterAndAccept(); return; }
                    if (s.inputBoundary != boundary || !eligible(ic)) {
                        event.filterAndAccept();
                        return;
                    }
                }
                auto saved = saveKey(event);
                saved.triggerPair = triggerPair;
                saved.bypassAuto = bypassAuto || nativePair;
                s.deferredKeys.push_back(std::move(saved));
                event.filterAndAccept();
                if (!s.punctuationCommit) scheduleReplay(ic);
                return;
            }
        }
        // Keep repeats and the matching release consumed even after reset
        // removed the preedit, or modifiers were released before Return.
        if (s.swallowedKey != FcitxKey_None && event.rawKey().sym() == s.swallowedKey) {
            event.filterAndAccept();
            if (event.isRelease()) s.swallowedKey = FcitxKey_None;
            return;
        }
        if (!event.isRelease() && !repeat && ic->hasFocus() &&
            instance_->inputMethod(ic) == "pinyin" &&
            !ic->capabilityFlags().testAny(CapabilityFlag::PasswordOrSensitive) &&
            event.key().checkKeyList(config_.toggleKey.value())) {
            auto ref = ic->watch();
            config_.enabled.setValue(!config_.enabled.value());
            event.filterAndAccept();
            resetAll();
            safeSaveAsIni(config_, "conf/transime.conf");
            if (ref.isValid()) state(ic).swallowedKey = event.rawKey().sym();
            return;
        }
        if (!ensureActive(ic) || event.isRelease()) return;
        // Pressing Ctrl/Alt in preparation for the translation chord must not
        // discard an already ready result. Engine-driven modifier selection
        // still emits its normal UI/reset/commit events and invalidates there.
        if (event.key().isModifier()) return;
        s.committingSource.reset();
        s.committingText.clear();
        if (event.key().checkKeyList(config_.clearKey.value())) {
            s.swallowedKey = event.rawKey().sym();
            event.filterAndAccept();
            ++s.commitSerial;
            worker_.clear();
            s.session.clear_history();
            clearCache(ic);
            s.historyTimer.reset();
            s.lastCommit = 0;
            clearCandidate(ic);
            queueRefresh(ic);
            return;
        }
        if (!bypassAuto && !repeat && !config_.continuous.value() &&
            config_.autoCommitOnPunctuation.value() && !holding(ic)) {
            if (auto punctuation = punctuationFor(event)) {
                if (auto target = snapshot(ic)) {
                    beginPunctuation(ic, event, *target, *punctuation);
                    if (event.filtered()) return;
                }
            }
        }
        if (event.key().checkKeyList(config_.translateKey.value())) {
            if (!composing(ic)) return;
            // Never let an unavailable translation hotkey send a chat while
            // a preedit is active. It leaves the composition unchanged.
            s.swallowedKey = event.rawKey().sym();
            event.filterAndAccept();
            auto actual = snapshot(ic);
            if (!actual || !s.target || !sameTarget(*actual, *s.target) ||
                !(s.rowMode ? rowTranslationVisible(ic) :
                  (!s.displayed.empty() && ic->inputPanel().auxDown().toString() == s.displayed))) return;
            auto translation = s.session.consume_translation();
            if (!translation) return;
            if (actual->paragraph) {
                auto *addon = instance_->addonManager().addon("pinyin", false);
                if (!addon) return;
                s.committingSource = actual->source;
                s.committingText = *translation;
                const auto serial = ++s.commitSerial;
                auto ref = ic->watch();
                try {
                    addon->call<IPinyin::transimeCommitParagraphV2>(ic, *actual->paragraph,
                        PinyinTransimeCommitModeV2::Translation, *translation);
                } catch (const std::exception &) { }
                if (!ref.isValid()) return;
                // A frontend can defer its CommitString event until the outer
                // key handler returns. Preserve bilingual provenance until it
                // is observed, then discard an unused intent on the next turn.
                std::weak_ptr<int> alive = alive_;
                instance_->eventDispatcher().schedule([this, alive, ref, serial] {
                    if (alive.expired() || !ref.isValid()) return;
                    auto &current = state(ref.get());
                    if (current.commitSerial == serial) {
                        current.committingSource.reset();
                        current.committingText.clear();
                    }
                });
                clearCandidate(ic);
                queueRefresh(ic);
            } else {
                commitTranslation(ic, actual->source, *translation);
            }
            return;
        }
        // Invalidate before pinyin handles any composition-changing press;
        // it often filters the event before PostInputMethod can run.
        clearCandidate(ic);
        queueRefresh(ic);
    }

    Instance *instance_;
    ::transime::WorkerClient worker_;
    TransimeConfig config_;
    SimpleInputContextPropertyFactory<TransimeState> factory_;
    std::vector<std::unique_ptr<HandlerTableEntry<EventHandler>>> handlers_;
    std::shared_ptr<int> alive_ = std::make_shared<int>(0);
};

class TransimeFactory final : public AddonFactory {
public:
    AddonInstance *create(AddonManager *manager) override {
        return new Transime(manager->instance());
    }
};
} // namespace fcitx

FCITX_ADDON_FACTORY(fcitx::TransimeFactory)
