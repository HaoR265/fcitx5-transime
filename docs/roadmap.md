# Known limitations and roadmap

This source snapshot is an Alpha; the inherited CMake component version 0.1.4
does not mean the new settings and Qt patch were part of a stable 0.1.4 release.

1. Create a portable, version-checked full installer and validate a clean-machine
   install/upgrade/uninstall cycle independently of the developer SDK.
2. Test additional Linux distributions, Wayland, Qt/GTK/browser/office apps and
   sensitive-field/focus lifecycles. The current tested Qt fix is opt-in.
3. Implement a genuine Windows text-service adapter before claiming Windows
   support. A portable settings window is not a Windows input method.
4. Improve translation terminology, ambiguity, negation and numeric/identifier
   fidelity using larger independent evaluation sets. A 1.7B candidate in a
   small CPU diagnostic was slower and still mistranslated terms; it was not
   promoted to default. No fine-tuning or universal quality gain is claimed.
5. Add CI for component tests and VM regression recipes, then signed/reproducible
   distribution artifacts. No CI status is advertised before it actually runs.

Public corpus files formerly named heldout are now development examples and
must not be treated as an unseen benchmark.
