# Project instructions

## Judgment and collaboration

Must maintain independent judgment, prioritize identifying problems and risks, and never assume by default that the user's views are correct. if have anything unsure must ask the user, don't simply make decisions that unsure, teach user to solve problem instead of finishing it by your self. if user require you to finish it then you follow user instruction

## Release writing preferences

- Release notes default to English unless the user requests another language.
- Return release notes as raw Markdown inside a fenced `markdown` code block so the user can copy them directly into GitHub Releases.
- Do not substitute a writing card or rendered prose for the requested Markdown code block.
- Apply these preferences to future release-writing requests in this project. The user's latest explicit instruction takes precedence.
- Preference recorded on 2026-10-05 (Asia/Kuala_Lumpur, UTC+08:00).

## Versioning before GitHub pushes

- Before each user-authorized GitHub push containing project changes, update the application version once for that batch of changes.
- Small updates and bug fixes increment the patch version: for example, `0.3.0` → `0.3.1` → `0.3.2`.
- Larger feature updates increment the minor version and reset the patch version: for example, `0.3.1` → `0.4.0`.
- Keep `src/__init__.py`, the macOS bundle version fields in `app_bundle/VideoScraper.spec`, and the current-version references in `README.md` consistent. About and future builds must use the updated version.
- A retry of the same push does not require another version increment. Do not rewrite historical diaries, existing tags/releases, or previous build reports to change their recorded versions; existing binaries retain their version until rebuilt.
- Versioning does not grant permission to push; follow the user's authorization for committing and pushing.
- Preference recorded on 2026-10-12 (Asia/Kuala_Lumpur, UTC+08:00). The download-control update is version `0.3.1`.
