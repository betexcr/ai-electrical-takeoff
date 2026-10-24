# Claude Code Project Instructions — AI Electrical Takeoff

Before doing project work, read `PROJECT_TRACKING.md` and the Stage 3 assessment in `docs/`.

## Required project discipline

- `PROJECT_TRACKING.md` is the source of truth for implementation phases and the assessment's design, first-release, release, support, and trade-off requirements.
- Keep the tracker current as work progresses. Update statuses, evidence, blocked reasons, decisions, and the next action in the same work session that changes them.
- Never mark work `Done` without real evidence such as a test result, evaluation report, verified artifact, or demonstrated behavior.
- The 1–2 page design document, evaluation evidence, release/versioning approach, support plan, demo materials, and README are required deliverables alongside the code. Do not let them disappear behind implementation work.
- Work in small, testable vertical slices. State acceptance criteria before implementation, implement, run relevant tests, inspect actual outputs, and record the evidence.
- Never invent counts, ground-truth labels, evaluation results, confidence calibration, or successful test runs. Record uncertainty and failures honestly.
- Every displayed count must be derived from saved detection records and traceable to visible markers on the drawing.
- Changes to model, prompt, schema, or detection rules must be versioned and regression-tested against a fixed evaluation set.
- Never silently overwrite completed takeoffs or user corrections when running a new analysis. Preserve history and make re-analysis explicit.
- Keep the core required workflow ahead of the optional bill of materials; defer optional scope if mandatory requirements or release gates are not reliable.
- Inspect repository files and existing conventions before modifying them. Do not make unrelated changes.

## Completion report

At the end of each meaningful work session, report: what changed, checks actually run and their results, remaining issues, tracker entries updated, and the next concrete action.
