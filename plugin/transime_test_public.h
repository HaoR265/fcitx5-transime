#pragma once

#include <vector>
#include <fcitx/addoninstance.h>
#include "session.h"

namespace fcitx { class InputContext; }

// Only exported when TRANSIME_ENABLE_TEST_FIXTURE is compiled in. It exists
// for synthetic headless tests; never enable this build on personal inputs.
FCITX_ADDON_DECLARE_FUNCTION(Transime, fixtureHistory,
    std::vector<transime::CommitRecord>(fcitx::InputContext *));
