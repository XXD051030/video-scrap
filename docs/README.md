# Project Records

Historical working records for Video Scraper, kept unchanged for reference. User-facing documentation stays in the repository root: [README](../README.md) and the [Windows build guide (Chinese)](../WINDOWS_BUILD.md).

These records were written in the repository root and moved here on 2026-11-06 without editing their contents. Their links and paths, such as `/Users/xxd/Playground/video-scrap/diary.md`, still name the original root locations and only resolve on the author's machine. Files under `build/` were never committed.

## Audits and fix reports (`audits/`)

| File | Contents |
| --- | --- |
| [code_audit_2026-10-17.md](audits/code_audit_2026-10-17.md) | Code review of version 0.3.1 |
| [code_audit_followup_2026-10-17.md](audits/code_audit_followup_2026-10-17.md) | Follow-up review and fix record |
| [code_audit_2026-10-18.md](audits/code_audit_2026-10-18.md) | Full review of the code at the time |
| [code_audit_recheck_2026-10-18.md](audits/code_audit_recheck_2026-10-18.md) | Second independent review |
| [audit_fixes_2026-10-18.md](audits/audit_fixes_2026-10-18.md) | Fixes and verification for the 2026-10-18 review |
| [code_audit_current_2026-10-19.md](audits/code_audit_current_2026-10-19.md) | Read-only review of version 0.3.4 |
| [code_audit_status_2026-10-19.md](audits/code_audit_status_2026-10-19.md) | Remaining issues and review status |
| [audit_fixes_2026-10-19.md](audits/audit_fixes_2026-10-19.md) | Fixes for the second-round review |
| [remaining_fixes_2026-10-19.md](audits/remaining_fixes_2026-10-19.md) | Fixes for remaining cache and process-ownership issues |
| [code_audit_recheck_2026-10-20.md](audits/code_audit_recheck_2026-10-20.md) | Further review on 2026-10-20 |
| [audit_fixes_2026-10-22.md](audits/audit_fixes_2026-10-22.md) | Fixes R01–R09 and final verification |
| [ci_failure_39744314319.md](audits/ci_failure_39744314319.md) | Investigation of GitHub Actions run 39744314319 |

## Renamed test files

On 2026-11-22 the test files named after audit rounds were renamed after what they test, without changing their contents. The records above still use the old names.

| Old name | New name |
| --- | --- |
| `tests/test_audit_core_regressions.py` | `tests/test_settings_scraping_cleanup_edges.py` |
| `tests/test_connection_lease_recheck.py` | `tests/test_http_connection_leases.py` |
| `tests/test_download_audit_oct18.py` | `tests/test_download_integrity.py` |
| `tests/test_download_audit_regressions.py` | `tests/test_download_quality_and_sessions.py` |
| `tests/test_download_recheck_oct18.py` | `tests/test_download_redirects_and_hls_playlists.py` |
| `tests/test_gui_audit_regressions.py` | `tests/test_gui_input_and_task_state.py` |
| `tests/test_gui_lifecycle_regressions.py` | `tests/test_browser_and_settings_lifecycle.py` |
| `tests/test_http_lifecycle_recheck.py` | `tests/test_http_response_deadlines.py` |
| `tests/test_proxy_audit_boundaries.py` | `tests/test_proxy_cache_and_transport.py` |
| `tests/test_proxy_protocol_regressions.py` | `tests/test_proxy_ranges_and_cookie_scope.py` |
| `tests/test_proxy_recheck_oct18.py` | `tests/test_proxy_cache_bounds_and_refresh.py` |

## Progress diaries (`diary/`)

| File | Contents |
| --- | --- |
| [diary.md](diary/diary.md) | Project progress record (to 2026-09-28) |
| [diary_2026-10-04_0356.md](diary/diary_2026-10-04_0356.md) | Project progress record, 2026-10-04 |
| [diary_2026-10-16_1032.md](diary/diary_2026-10-16_1032.md) | Project progress record, 2026-10-16 |
