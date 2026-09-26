# Privacy and security

TransIME handles text before submission. Use this Alpha only in an isolated VM
with synthetic input. Do not use it for secrets or depend on it to recognize
every password field, application context, or sensitive document.

The default translator runs locally; model preparation may download only when
explicitly requested. No personal dictionary, real input history, credentials,
desktop snapshots, or private configuration is included in this source package.
Context assistance may retain a bounded recent session in memory; this is not a
claim that all processing is stateless. Independent user-imported word packs
persist until the user removes them. Native Fcitx/Pinyin learning and third-party
components have their own storage behavior.

Diagnostics and test runners can record synthetic input. Never run them against
a daily desktop or attach unredacted logs from real use. Before posting an issue,
remove text samples, paths, account names, tokens, environment variables and
dictionary contents that are not intended to be public.

Do not post exploitable security findings or real secrets in public issues.
Use GitHub private vulnerability reporting if enabled on the repository. If it
is unavailable, ask the maintainer for a private contact without disclosing the
vulnerability details. No response-time commitment is currently offered.
