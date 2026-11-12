"""Live check of gated auto-approval (#4855) against a real GitHub PR on the fork.

Runs inside GitHub Actions as github-actions[bot]. Only the model reply is stubbed in the full-run
scenarios; the provider, the GitHub API, the CI status, staleness, authors and the verdict are real.
Prints one PASS/FAIL line per check and exits non-zero on any failure.
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock

from pr_agent.config_loader import get_settings

PR_URL = os.environ["PR_URL"]
MARKER = "<!-- pr-agent:review:auto-approval -->"
results = []

s = get_settings()
s.set("GITHUB.USER_TOKEN", os.environ["GITHUB_TOKEN"])
s.set("GITHUB.DEPLOYMENT_TYPE", "user")
s.set("CONFIG.GIT_PROVIDER", "github")
s.set("CONFIG.ENABLE_AUTO_APPROVAL", True)
s.set("CONFIG.PUBLISH_OUTPUT", True)
s.set("PR_REVIEWER.REQUIRE_MERGE_RECOMMENDATION", True)
s.set("PR_REVIEWER.REQUIRE_RISK_ASSESSMENT", True)
s.set("PR_REVIEWER.PERSISTENT_COMMENT", False)

from pr_agent.commands import pr_reviewer as pr_reviewer_module  # noqa: E402
from pr_agent.commands.pr_reviewer import PRReviewer  # noqa: E402
from pr_agent.git_providers.github_provider import GithubProvider  # noqa: E402
from pr_agent.servers.github_common import revoke_auto_approval_on_push  # noqa: E402


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)


def bot_reviews(provider):
    return [r for r in provider._get_pr().get_reviews() if r.user and r.user.login == "github-actions[bot]"]


def active_marked(provider):
    return [r for r in bot_reviews(provider) if r.state == "APPROVED" and (r.body or "").startswith(MARKER)]


def latest(provider):
    return max(bot_reviews(provider), key=lambda r: r.id)


provider = GithubProvider(PR_URL)
head = provider._get_pr().head.sha
print(f"PR {PR_URL} head {head}", flush=True)
print(f"identity: {provider._agent_login()!r}  ci: {provider.get_head_ci_status()!r}  "
      f"stale: {provider.is_head_stale_against_base()!r}  human: {provider.are_all_authors_human()!r}", flush=True)

# S1: approve the reviewed head, then withdraw it.
ok = provider.auto_approve(body="live test S1")
r = latest(provider)
check("S1 auto_approve returns True", ok is True)
check("S1 review is APPROVED", r.state == "APPROVED", r.state)
check("S1 pinned to the reviewed head", r.commit_id == head, r.commit_id)
check("S1 body carries the marker", (r.body or "").startswith(MARKER))
ok = provider.revoke_auto_approval("live test S1 withdraw")
check("S1 revoke returns True", ok is True)
check("S1 approval is DISMISSED", not active_marked(provider))

# S2: a synchronize event withdraws it through the webhook helper.
provider.auto_approve(body="live test S2")
check("S2 approval in place before push", len(active_marked(provider)) == 1)
revoke_auto_approval_on_push({"action": "synchronize", "pull_request": {"url": provider.pr.url}})
check("S2 synchronize withdraws it", not active_marked(provider))

# S3: an unmarked approval from the same login is left alone.
manual = provider.pr.create_review(event="APPROVE", commit=provider._get_repo().get_commit(head), body="manual")
provider.auto_approve(body="live test S3")
provider.revoke_auto_approval("live test S3 withdraw")
states = {rv.id: rv.state for rv in bot_reviews(provider)}
check("S3 manual approval kept", states.get(manual.id) == "APPROVED", states.get(manual.id))
check("S3 marked approval dismissed", not active_marked(provider))
manual.dismiss("live test cleanup")


# S4/S5: full /review runs, real verdict, stubbed model reply.
def review_yaml(recommendation, risk):
    return (f"review:\n  estimated_effort_to_review_[1-5]: 1\n  relevant_tests: No\n  key_issues_to_review: []\n"
            f"  security_concerns: No\n  risk_level: {risk}\n  merge_recommendation: {recommendation}\n")


async def full_run(prediction):
    reviewer = PRReviewer(PR_URL)

    async def fake_retry(prepare_fn, model_type=None, git_provider=None):
        reviewer.prediction = prediction

    pr_reviewer_module.retry_with_fallback_models = fake_retry
    pr_reviewer_module.extract_and_cache_pr_tickets = AsyncMock()
    await reviewer.run()
    return reviewer


reviewer = asyncio.run(full_run(review_yaml("no_concerns_found", "low")))
verdict = reviewer._merge_recommendation_status
print(f"S4 computed verdict: {verdict!r}", flush=True)
check("S4 clean run computes no_concerns_found", verdict == "no_concerns_found", verdict)
check("S4 clean run leaves exactly one marked approval", len(active_marked(provider)) == 1)

reviewer = asyncio.run(full_run(review_yaml("needs_review", "medium")))
check("S5 model veto computes needs_review", reviewer._merge_recommendation_status == "needs_review",
      reviewer._merge_recommendation_status)
check("S5 vetoed run leaves no approval", not active_marked(provider))

print(f"{sum(results)}/{len(results)} checks passed", flush=True)
sys.exit(0 if all(results) else 1)
