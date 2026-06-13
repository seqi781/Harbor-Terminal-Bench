# Failed Terminal-Bench Tasks

Recorded from `jobs/rerun-all-failed` after rerunning 11 previously failed tasks.

## Summary

Passed after rerun:

- `reshard-c4-data`
- `caffe-cifar-10`
- `mteb-retrieve`
- `hf-model-inference`
- `mteb-leaderboard`
- `pytorch-model-recovery`

Still failing:

- `largest-eigenval__7ZHSbnw`
- `make-doom-for-mips__nJjXGPs`
- `protein-assembly__sDTJ5F7`
- `build-pmars__HmTDv3Q`
- `rstan-to-pystan__qBUhbyf`

## Failure Details

### `largest-eigenval`

- Job: `jobs/rerun-all-failed/largest-eigenval__7ZHSbnw`
- Result: `0.0`
- Cause: correctness tests passed, but one speed test failed on the `10x10` matrix case.
- Key verifier error: candidate median runtime was marginally slower than reference, roughly `0.000050s > 0.000050s`.
- Classification: oracle solution/performance flakiness, not network.
- Related upstream issue: https://github.com/harbor-framework/terminal-bench/issues/808

### `make-doom-for-mips`

- Job: `jobs/rerun-all-failed/make-doom-for-mips__nJjXGPs`
- Result: `0.0`
- Cause: apt package downloads failed with multiple `404 Not Found` responses through `172.31.0.1:10808`; `clang` was not installed, so the build failed.
- Key agent errors:
  - `E: Failed to fetch ... 404 Not Found [IP: 172.31.0.1 10808]`
  - `make: clang: No such file or directory`
- Key verifier errors:
  - timeout waiting for `/tmp/frame.bmp`
  - `/tmp/frame.bmp` does not exist
- Classification: apt source/proxy/package-index issue, plus task resource constraints may contribute.
- Related upstream issues:
  - https://github.com/harbor-framework/terminal-bench-2/issues/44
  - https://github.com/harbor-framework/terminal-bench-2/issues/67

### `protein-assembly`

- Job: `jobs/rerun-all-failed/protein-assembly__sDTJ5F7`
- Result: `0.0`
- Cause: `dnachisel` failed to resolve translation constraints, so `/app/gblock.txt` was never created.
- Key agent error:
  - `dnachisel.DnaOptimizationProblem.NoSolutionError.NoSolutionError`
  - `FAIL EnforceTranslation[0-2694]`
- Key verifier error:
  - `File /app/gblock.txt does not exist.`
- Classification: upstream task/oracle issue, not network.
- Related upstream PR: https://github.com/harbor-framework/terminal-bench-2/pull/57

### `build-pmars`

- Job: `jobs/rerun-all-failed/build-pmars__HmTDv3Q`
- Result: `0.0`
- Cause: oracle pins `dpkg-dev=1.22.21`, but that version is unavailable from the current Debian source.
- Key agent error:
  - `E: Version '1.22.21' for 'dpkg-dev' was not found`
- Key verifier errors:
  - `/usr/local/bin/pmars` does not exist
  - no `/app/pmars-*` source directory found
- Classification: upstream task/oracle apt version pin issue.
- Related upstream issue: https://github.com/harbor-framework/terminal-bench-2/issues/59

### `rstan-to-pystan`

- Job: `jobs/rerun-all-failed/rstan-to-pystan__qBUhbyf`
- Result: `0.0`
- Cause: oracle pins `curl=8.5.0-2ubuntu10.6`, but that version is unavailable from the current Ubuntu source. Because the install failed, `add-apt-repository` was also unavailable and no output CSV files were generated.
- Key agent errors:
  - `E: Version '8.5.0-2ubuntu10.6' for 'curl' was not found`
  - `sudo: add-apt-repository: command not found`
- Key verifier errors:
  - `/app/alpha_est.csv` not found
  - `/app/sigma_est.csv` not found
  - `/app/rho_est.csv` not found
  - `/app/beta_est.csv` not found
- Classification: upstream task/oracle apt version pin issue.
- Related upstream issue: none found during search.

## Network Notes

- A previous global Docker proxy issue injected `ALL_PROXY=socks5h://172.31.0.1:10808` into containers and caused Hugging Face Hub failures unless `socksio` was installed.
- The Docker client config was changed to remove `allProxy` while preserving `httpProxy`, `httpsProxy`, and `noProxy`.
- After that change, Hugging Face related tasks passed on rerun.
