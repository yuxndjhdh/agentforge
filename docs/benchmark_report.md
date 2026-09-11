# Code Agent Benchmark Report

This file is generated from the four variant `report.json` files. Do not edit metrics manually.

- Model: `deepseek-v4-flash-vision-exp`
- AgentForge commit: `e256238d158e36708019a7754876670744141e67`
- Workspace dirty: `False`
- Tasks: `23`
- Task order: `fix-add, fix-subtract, fix-uppercase, fix-clamp, fix-average, fix-json-key, fix-safe-divide, fix-cache, fix-retry, add-unit-test, add-api-field, api-status-code, cli-argument, config-default, config-env, refactor-helper, security-path, security-secret, docs-command, type-contract, fix-date-format, fix-filter, fix-immutability`

## Variant Configuration

| Variant | Compression | Verify retry | Max attempts | Trials | Episodes |
| --- | --- | --- | ---: | ---: | ---: |
| A | False | False | 1 | 5 | 115 |
| B | True | False | 1 | 5 | 115 |
| C | False | True | 3 | 5 | 115 |
| D | True | True | 3 | 5 | 115 |

## Metrics

| Variant | pass@1 | pass@3 | pass@5 | First success | Final success | p50/p95 latency | Avg steps | Avg input tokens | Avg output tokens | Cost |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| A | 0.835 | 0.891 | 0.913 | 0.835 | 0.835 | 10.644/24.811 | 6.261 | 17808.983 | 884.930 | unavailable |
| B | 0.800 | 0.826 | 0.826 | 0.800 | 0.800 | 11.054/21.274 | 6.104 | 17141.009 | 918.991 | unavailable |
| C | 1.000 | 1.000 | 1.000 | 0.800 | 1.000 | 10.396/38.380 | 7.165 | 30484.904 | 1738.557 | unavailable |
| D | 1.000 | 1.000 | 1.000 | 0.800 | 1.000 | 10.522/37.931 | 7.243 | 22229.078 | 1345.417 | unavailable |

## Ablation Deltas From A

| Comparison | Final success delta | Avg input token delta | Estimated cost delta | Compression trigger rate | Verify incremental cost |
| --- | ---: | ---: | ---: | ---: | ---: |
| A -> B | -0.035 | -667.974 | unavailable | 0.174 | unavailable |
| A -> C | 0.165 | 12675.922 | unavailable | 0.000 | unavailable |
| A -> D | 0.165 | 4420.096 | unavailable | 0.252 | unavailable |

## Limitations

The comparison is descriptive for five trials per task; it is not a significance test. A compression or Verify conclusion requires the corresponding trigger/attempt data to be non-zero. Every metric above is sourced from the validated episode records in the four input reports.

The four reports do not contain positive input and output cost rates, so cost fields are `unavailable`; token totals remain available, but no billing conclusion is valid.
The Docker performance runs do not constitute complete container-security evidence when disk quota enforcement is disabled or unverified.
