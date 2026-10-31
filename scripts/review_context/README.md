# Diff-scoped code context for pr-agent's `[artifacts]` slot

A starting point, not a finished tool. `graph_bundle_ts.py` builds a review-context bundle for one PR with tree-sitter, and `instructions.txt` tells the model how to read it. Together they are the setup measured in [The-PR-Agent/pr-agent#2499](https://github.com/The-PR-Agent/pr-agent/issues/2499).

The bundle holds the changed symbols (signature plus the body lines the diff does not show), module constants and callees they use, inbound callers (hub callers ranked, under a cap), sibling implementations and sibling config/prompt/doc files, sites still using a pattern the PR removed, similar code and same-pattern sites, tests that exercise the change, and importers. It is capped at about 12k tokens.

With `--generic` every language is handled by one class driven by tree-sitter tags queries, so it covers the 68 grammars in tree-sitter-language-pack that ship one (Python, JavaScript, TypeScript, Go, Java, C, C++, C#, Rust, Ruby, PHP, Kotlin and more). It has only been scored on Python; Go and TypeScript were smoke-tested. Without the flag, the hand-written Python and JavaScript classes are used and other languages fall back to the generic one.

## Run

```bash
uv run --no-project --with tree-sitter-language-pack==1.22.1 python scripts/review_context/graph_bundle_ts.py \
  --repo "$PWD" --base "$BASE_SHA" --head "$HEAD_SHA" --pr "$PR_NUMBER" \
  --out-dir /tmp/review-context --generic --grammar-cache /tmp/grammars
```

`--repo` must be a checkout of the head commit. The bundle is written to `<out-dir>/pr<N>.G6.md` and a summary JSON goes to stdout. On pr-agent's own PRs a build takes 5 to 30 seconds. tree-sitter-language-pack downloads grammars from GitHub on first use, so prefetch them into `--grammar-cache` for offline or sandboxed runs.

Hand the bundle to pr-agent, here for `/review` only:

```bash
export ARTIFACT_PATH=/tmp/review-context/pr$PR_NUMBER.G6.md
export ARTIFACT_INSTRUCTIONS="$(cat scripts/review_context/instructions.txt)"
export ARTIFACTS__TARGET_TOOLS='["pr_reviewer"]'
```

In the GitHub Action, set the same three on the action step's `env` (or its `artifact_path` input), and write the bundle inside `GITHUB_WORKSPACE`, because paths outside it are rejected.

## Before running it on untrusted PRs

It never imports or executes the target repo's code, but it is not hardened yet:

- It reads source files from the working tree, so a symlink in the PR can pull in a file from outside the repo. Read blobs with `git show <sha>:<path>`, or skip symlinks and non-regular files.
- It has no per-file size cap or wall-clock limit, and it raises the recursion limit for deep trees.
- It does not skip `.env`-style or secret-looking files.
- Under `pull_request_target`, take the builder from the base branch, check the head out by SHA with `persist-credentials: false`, keep secrets out of the build step, and fall back to a diff-only review if the build fails.
- Pin tree-sitter-language-pack and its grammars, and never cache the grammar directory from a PR event.
