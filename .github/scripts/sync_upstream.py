"""Fast-forward the fork's upstream branch and open one review PR; never merge main."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.parse import urlencode


REPOSITORY = "coledeb/sidepulse"
SOURCE = "inteliwear/sidepulse"
STATUS_CONTEXT = "Upstream merge check"
BODY = """## Upstream SidePulse updates

This PR tracks the original repository's `main` branch. New upstream commits
are added to this same PR by the daily **Sync upstream** workflow.

Before merging:
- Review changes alongside our customizations (music visualizer, LED lease,
  menu-bar visualizer controls). Merging is optional: be selective.
- Check the Upstream merge check status; it reports merge conflicts with main.
- Approve CI workflows if requested and review their results before merging.
- Resolve any merge conflicts and test the resulting app.
- Use **Create a merge commit** to preserve shared upstream history; avoid squash
  or rebase merging these sync PRs.

This automation does not merge the PR, reinstall SidePulse, or update your Mac.
"""


def api(method, endpoint, payload=None):
    command = ["gh", "api", "--method", method, endpoint]
    if payload is not None:
        command += ["--input", "-"]
    result = subprocess.run(
        command, input=json.dumps(payload) if payload is not None else None,
        text=True, capture_output=True, check=True,
    )
    return json.loads(result.stdout)


def sync(request=api, dry_run=False):
    source_sha = request("GET", f"repos/{SOURCE}/git/ref/heads/main")["object"]["sha"]
    current_sha = request("GET", f"repos/{REPOSITORY}/git/ref/heads/upstream")["object"]["sha"]
    if source_sha != current_sha:
        relation = request("GET", f"repos/{REPOSITORY}/compare/{current_sha}...{source_sha}")
        if relation["status"] != "ahead":
            raise RuntimeError("upstream has diverged or is ahead of the source; refusing to overwrite it.")
        if not dry_run:
            # GitHub rejects non-fast-forward changes even if the branch moves
            # between our comparison and update. No force push is permitted.
            request("PATCH", f"repos/{REPOSITORY}/git/refs/heads/upstream",
                    {"sha": source_sha, "force": False})
        print(f"{'Would sync' if dry_run else 'Synced'} upstream to {source_sha}.")

    comparison = request("GET", f"repos/{REPOSITORY}/compare/main...{source_sha}")
    if comparison["ahead_by"] == 0:
        return "Your customized main already contains all upstream commits. No PR needed.", None, None

    query = urlencode({"state": "open", "base": "main", "head": "coledeb:upstream"})
    existing = request("GET", f"repos/{REPOSITORY}/pulls?{query}")
    if existing:
        return (f"Update PR is tracking upstream: {existing[0]['html_url']}",
                None if dry_run else existing[0]["number"], None if dry_run else source_sha)
    if dry_run:
        return f"Would open an update PR with {comparison['ahead_by']} upstream commits.", None, None
    pull = request("POST", f"repos/{REPOSITORY}/pulls", {
        "title": "Sync upstream SidePulse updates", "base": "main",
        "head": "upstream", "body": BODY,
    })
    return f"Created update PR: {pull['html_url']}", pull["number"], source_sha


def merge_commit(request, number, head_sha, attempts=24, wait=5):
    """Return GitHub's test merge of the PR into main, or None if it conflicts."""
    for _ in range(attempts):
        pull = request("GET", f"repos/{REPOSITORY}/pulls/{number}")
        if pull["head"]["sha"] == head_sha and pull["mergeable"] is not None:
            return pull["merge_commit_sha"] if pull["mergeable"] else None
        time.sleep(wait)
    raise RuntimeError("GitHub did not compute the PR's test merge in time.")


def report_conflicts(request, number, head_sha):
    """Post whether the PR merges cleanly into main, only when that changes."""
    merge_sha = merge_commit(request, number, head_sha)
    state, description = (("success", "Merges cleanly into main") if merge_sha
                          else ("failure", "Merge conflicts with main; resolve them before merging"))
    statuses = request("GET", f"repos/{REPOSITORY}/commits/{head_sha}/status")["statuses"]
    current = next((status for status in statuses if status["context"] == STATUS_CONTEXT), None)
    if not current or current["state"] != state:
        request("POST", f"repos/{REPOSITORY}/statuses/{head_sha}", {
            "state": state, "context": STATUS_CONTEXT, "description": description,
        })
    return description


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Inspect GitHub without changing anything")
    args = parser.parse_args()
    result, number, head_sha = sync(dry_run=args.dry_run)
    if number and not args.dry_run:
        result += f" {report_conflicts(api, number, head_sha)}."
    print(result)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
            summary.write(result + "\n")
