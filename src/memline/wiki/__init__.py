"""The wiki pipeline: compile evidence into pages a human has approved.

One package because it is one product area with one boundary: everything in
here reaches the memory store only through an injected ``execute`` callable
and reaches the outside world only through ``bundle`` and ``relay``. Modules
follow the pipeline — batch/profile/suggest/state compile, draft/verify/
review/review_report/threads produce and gate, check/nav/index/related keep
the published corpus honest — and ``page`` holds the format facts they all
agree on.
"""
