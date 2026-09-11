# External Benchmark Protocol

This protocol defines the P1 external-repository evaluation boundary. It does
not turn the existing internal seed-task benchmark into external evidence.

## Dataset

Each task is declared in JSON under a frozen `dataset_version`. The task pins a
repository URL, license, source issue/PR URL, 40-character base commit, task
prompt, allowed/protected path patterns, environment limits, verifier bundle
hash, and reference patch hash. `ExternalDataset.load()` rejects malformed
records, duplicate task IDs, overlapping allowed/protected paths, and a
repository appearing in both validation and holdout.

`file://` repositories are supported only to test the pipeline. The audit marks
them as `pipeline_smoke` and sets `external_validity_evidence` to false.

## Verifier isolation

Verifier bundles are resolved below the dataset root and hashed by normalized
relative path plus file bytes. A verifier is rejected if it is inside the
agent workspace or if its hash differs from the manifest. Commands are argv
lists and run with `shell=False`; `{verifier_root}`, `{workdir}`, and
`{task_id}` placeholders are expanded by the harness.

The reference patch and hidden checks are not copied into the agent workspace.
The current loader records the required hash and path metadata; a formal data
audit must additionally run base-fail/reference-pass checks before freezing the
dataset.

## Execution and statistics

`RepositoryMaterializer` clones a pinned base commit into a fresh workspace.
`run_external_episode()` records repository, commit, verifier hash, trace,
changed paths, path-policy violations, and verifier output. The report keeps
validation and holdout separate.

`summarize_external_benchmark.py` reports both episode-level numerator/denominator
and task-level bootstrap intervals. It is descriptive when the task count is
small. A formal result requires the planned 30 tasks, two models, E0/E1/E2,
five trials per task/configuration, frozen holdout, and manual audit records.
