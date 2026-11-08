"""Exercise the fork's upstream-sync decisions without changing GitHub.

The script lives in .github/scripts; it is tested here so the existing test
workflow covers it without modifying upstream's workflow files.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"
if not (SCRIPTS / "sync_upstream.py").exists():  # e.g. an sdist, which ships tests/ but not .github/
    raise unittest.SkipTest("fork sync script is not present")
sys.path.insert(0, str(SCRIPTS))

from sync_upstream import REPOSITORY, STATUS_CONTEXT, merge_commit, report_conflicts, sync


class SyncTests(unittest.TestCase):
    def test_no_updates_does_not_write(self):
        api = Mock(side_effect=[{"object": {"sha": "a"}}, {"object": {"sha": "a"}}, {"ahead_by": 0}])
        message, number, sha = sync(api)
        self.assertIn("No PR needed", message)
        self.assertEqual((number, sha), (None, None))
        self.assertTrue(all(call.args[0] == "GET" for call in api.call_args_list))

    def test_fast_forward_and_create_pr(self):
        api = Mock(side_effect=[{"object": {"sha": "b"}}, {"object": {"sha": "a"}},
                               {"status": "ahead"}, {}, {"ahead_by": 3}, [], {"html_url": "https://example.test/pr/1", "number": 1}])
        message, number, sha = sync(api)
        self.assertIn("Created update PR", message)
        self.assertEqual((number, sha), (1, "b"))
        writes = [call for call in api.call_args_list if call.args[0] != "GET"]
        self.assertEqual(len(writes), 2)
        self.assertTrue(writes[0].args[1].endswith("/git/refs/heads/upstream"))
        self.assertEqual(writes[0].args[2], {"sha": "b", "force": False})
        self.assertEqual(writes[1].args[2]["base"], "main")

    def test_existing_pr_is_reused(self):
        api = Mock(side_effect=[{"object": {"sha": "a"}}, {"object": {"sha": "a"}},
                               {"ahead_by": 2}, [{"html_url": "https://example.test/pr/1", "number": 1}]])
        message, number, sha = sync(api)
        self.assertIn("tracking upstream", message)
        self.assertEqual((number, sha), (1, "a"))
        self.assertTrue(all(call.args[0] == "GET" for call in api.call_args_list))

    def test_divergence_and_rewound_source_stop_without_writes(self):
        for status in ("diverged", "behind"):
            with self.subTest(status=status):
                api = Mock(side_effect=[{"object": {"sha": "b"}}, {"object": {"sha": "a"}}, {"status": status}])
                with self.assertRaises(RuntimeError):
                    sync(api)
                self.assertTrue(all(call.args[0] == "GET" for call in api.call_args_list))

    def test_dry_run_never_writes(self):
        api = Mock(side_effect=[{"object": {"sha": "b"}}, {"object": {"sha": "a"}},
                               {"status": "ahead"}, {"ahead_by": 1}, []])
        message, number, sha = sync(api, dry_run=True)
        self.assertIn("Would open", message)
        self.assertEqual((number, sha), (None, None))
        self.assertTrue(all(call.args[0] == "GET" for call in api.call_args_list))

    def test_update_failure_stops_before_pr(self):
        api = Mock(side_effect=[{"object": {"sha": "b"}}, {"object": {"sha": "a"}},
                               {"status": "ahead"}, RuntimeError("ref changed")])
        with self.assertRaisesRegex(RuntimeError, "ref changed"):
            sync(api)
        self.assertEqual(api.call_count, 4)


    def test_dry_run_existing_pr_returns_no_status_target(self):
        api = Mock(side_effect=[{"object": {"sha": "a"}}, {"object": {"sha": "a"}},
                               {"ahead_by": 2}, [{"html_url": "https://example.test/pr/1", "number": 1}]])
        message, number, sha = sync(api, dry_run=True)
        self.assertIn("tracking upstream", message)
        self.assertEqual((number, sha), (None, None))
        self.assertTrue(all(call.args[0] == "GET" for call in api.call_args_list))


class MergeCheckTests(unittest.TestCase):
    @patch("sync_upstream.time.sleep")
    def test_waits_for_expected_head_and_computed_merge(self, sleep):
        api = Mock(side_effect=[
            {"head": {"sha": "old"}, "mergeable": True, "merge_commit_sha": "stale"},
            {"head": {"sha": "new"}, "mergeable": None},
            {"head": {"sha": "new"}, "mergeable": True, "merge_commit_sha": "merge"},
        ])
        self.assertEqual(merge_commit(api, 7, "new"), "merge")
        self.assertEqual(sleep.call_count, 2)
        sleep.assert_called_with(5)
        self.assertTrue(all(call.args == ("GET", f"repos/{REPOSITORY}/pulls/7")
                            for call in api.call_args_list))

    def test_conflicting_merge(self):
        api = Mock(return_value={"head": {"sha": "new"}, "mergeable": False})
        self.assertIsNone(merge_commit(api, 7, "new"))
        api.assert_called_once()

    @patch("sync_upstream.time.sleep")
    def test_timeout_for_unknown_merge_or_stale_head(self, sleep):
        for pull in ({"head": {"sha": "new"}, "mergeable": None},
                     {"head": {"sha": "old"}, "mergeable": False}):
            with self.subTest(pull=pull):
                api = Mock(return_value=pull)
                with self.assertRaisesRegex(RuntimeError, "in time"):
                    merge_commit(api, 7, "new", attempts=2, wait=0)
                self.assertEqual(api.call_count, 2)

    def test_status_posts_and_skips_unchanged_context(self):
        for mergeable, state, description in (
            (True, "success", "Merges cleanly into main"),
            (False, "failure", "Merge conflicts with main; resolve them before merging"),
        ):
            for current in (None, "success", "failure", "pending"):
                with self.subTest(mergeable=mergeable, current=current):
                    statuses = [{"context": "CI", "state": state}]
                    if current:
                        statuses += [{"context": STATUS_CONTEXT, "state": current}]
                    api = Mock(side_effect=[
                        {"head": {"sha": "new"}, "mergeable": mergeable,
                         "merge_commit_sha": "merge" if mergeable else None},
                        {"statuses": statuses}, {},
                    ])
                    self.assertEqual(report_conflicts(api, 7, "new"), description)
                    self.assertEqual(api.call_args_list[1].args,
                                     ("GET", f"repos/{REPOSITORY}/commits/new/status"))
                    if current == state:
                        self.assertEqual(api.call_count, 2)
                    else:
                        api.assert_called_with("POST", f"repos/{REPOSITORY}/statuses/new", {
                            "state": state, "context": STATUS_CONTEXT, "description": description,
                        })

    def test_uses_latest_matching_status(self):
        api = Mock(side_effect=[
            {"head": {"sha": "new"}, "mergeable": True, "merge_commit_sha": "merge"},
            {"statuses": [{"context": STATUS_CONTEXT, "state": "success"},
                          {"context": STATUS_CONTEXT, "state": "failure"}]},
        ])
        self.assertEqual(report_conflicts(api, 7, "new"), "Merges cleanly into main")
        self.assertEqual(api.call_count, 2)

    @patch("sync_upstream.time.sleep")
    def test_timeout_does_not_post_status(self, sleep):
        api = Mock(return_value={"head": {"sha": "new"}, "mergeable": None})
        with self.assertRaises(RuntimeError):
            report_conflicts(api, 7, "new")
        self.assertTrue(all(call.args[0] == "GET" for call in api.call_args_list))


if __name__ == "__main__":
    unittest.main()
