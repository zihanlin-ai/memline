"""One-shot migrations that already ran, kept runnable.

Each module here moved the store through a transition — importing legacy
Markdown ledgers, backfilling normalized metadata — and earned retirement by
finishing. They stay installable under their original script names because a
store restored from an old backup needs the same door it came through, but
nothing in the living package imports them: an arrow from memline proper
into this directory means a migration quietly became a dependency.
"""
