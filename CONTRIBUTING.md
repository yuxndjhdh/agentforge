# Contributing

1. Create a focused branch from `master`.
2. Install `requirements.lock` and run `pytest -q`, `ruff check agentforge tests`, and `python -m compileall -q agentforge`.
3. Add a regression test for behavior changes, especially evaluator and sandbox changes.
4. Keep API keys, `.agentforge`, `runs`, and generated benchmark output out of commits.
5. Describe security and reproducibility implications in the pull request.
