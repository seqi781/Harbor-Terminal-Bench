# 全量跑批结果分析：问题汇总

- **作业目录**：`jobs/2026-06-13__12-40-55/`
- **配置**：`run_all.json`，agent = `main:MiMoAgent`，84 道任务，`-n 6` 并发
- **任务范围**：nop_verify 的 78 道 + rerun 里 oracle 通过的 6 道；已排除 5 道 oracle 本身 0 分的题
  （largest-eigenval / make-doom-for-mips / protein-assembly / build-pmars / rstan-to-pystan）

## 总览

| 结果 | 数量 | 占比 |
|---|---|---|
| ✅ 通过 (reward=1) | 29 | 34.5% |
| ❌ 失败 (reward=0，跑完但没过验证) | 40 | 47.6% |
| 💥 报错中断 (trial errored) | 15 | 17.9% |
| **合计** | **84** | |

- 绝对通过率 **29/84 = 34.5%**；若把 15 道基础设施/超时崩溃排除，按真正评分的 69 道算 **29/69 ≈ 42%**。
- 评分均值（harbor 统计，errored 除外）：**0.345**。

问题分三大类，按"是不是 agent 能改善"排序如下。

---

## 类别一 · 执行超时崩溃（15 道，💥 最该先修）

**现象**：trial 直接 errored，没有 reward。

**根因（同一个）**：`mimo_agent/agent.py:271` 的
`result = await environment.exec(command, timeout_sec=120)`——
当一条命令超过 **120 秒**（`apt-get install`、`pip install torch`、大文件下载、长编译等），
harbor 抛 `RuntimeError: Command timed out after 120 seconds`，而**我们的 agent 没有捕获**，
异常直接冒泡，整个 trial 崩溃。一个未处理的超时，报废了 14 道"装环境很重"的题。

| 任务 | 崩在第几轮 | 异常 |
|---|---|---|
| adaptive-rejection-sampler | 2 | exec 超时（装 r-base） |
| caffe-cifar-10 | 6 | exec 超时 |
| crack-7z-hash | 13 | exec 超时 |
| extract-moves-from-video | 5 | exec 超时 |
| feal-linear-cryptanalysis | 14 | exec 超时 |
| mcmc-sampling-stan | 4 | exec 超时（装 stan） |
| mteb-leaderboard | 20 | exec 超时 |
| pytorch-model-recovery | 6 | exec 超时 |
| qemu-alpine-ssh | 14 | exec 超时 |
| query-optimize | 3 | exec 超时 |
| sam-cell-seg | 3 | exec 超时 |
| torch-pipeline-parallelism | 7 | exec 超时（装 torch） |
| torch-tensor-parallelism | 10 | exec 超时（装 torch） |
| train-fasttext | 14 | exec 超时 |
| write-compressor | 16 | **AgentTimeoutError**：整个 agent 阶段超 900s |

**问题归纳**
- exec 单命令 120s 上限对"装依赖/编译"类任务太短，且**超时未被捕获** → 直接 errored，
  连补救机会都没有。
- write-compressor 是另一种超时：单条命令没崩，但 agent 整体耗时超过 harbor 的 900s 上限。

---

## 类别二 · 跑满 20 轮仍未收敛（33 道，❌）

**现象**：agent 一直跑到 `MAX_TURNS=20` 上限，停下时验证不通过。属于"难题没做出来"，
不是假通过——它知道还没好，只是轮数用光了。

按失败测试看，主要集中在几类：

- **重编译 / 构建类**（编译没成功或没跑完）：
  `compile-compcert`(3)、`build-cython-ext`(9)、`path-tracing`(5)、`path-tracing-reverse`(3)、
  `sqlite-with-gcov`(3)、`custom-memory-heap-crash`(1)、`make-mips-interpreter`(3)
- **算法 / 精确产物类**（结果值不对）：
  `chess-best-move`、`circuit-fibsqrt`、`dna-assembly`、`dna-insert`、`gpt2-codegolf`、
  `schemelike-metacircular-eval`、`regex-chess`(4)、`raman-fitting`(3)、`count-dataset-tokens`
- **多文件 / 系统搭建类**：
  `financial-document-processor`(7)、`db-wal-recovery`(7)、`mailman`(2)、`git-multibranch`、
  `install-windows-3.11`、`video-processing`(3)、`tune-mjcf`(3)
- **安全 / 过滤类**：`break-filter-js-from-html`、`sanitize-git-repo`(2)、`password-recovery`(2)
- **性能阈值 / 其它**：`llm-inference-batching-scheduler`（perf 阈值不达标）、
  `winning-avg-corewars`(2)、`overfull-hbox`、`fix-ocaml-gc`、`gcode-to-text`(2)、
  `code-from-image`(2)、`reshard-c4-data`
- 还有 `mteb-retrieve`? → 见类别三（提前收手）

（括号内为失败的测试用例数。）

**问题归纳**
- 20 轮对这些"装环境 + 编译 + 多步实现 + 验证"的重任务不够；很多轮其实消耗在装依赖、
  重编译、反复试错上。
- 部分任务即便不超轮，本身也超出当前 agent 的能力（精确算法、图像相似度、性能阈值）。

---

## 类别三 · 提前收手 / 假通过（7 道，❌ 最值得复盘 agent 行为）

**现象**：agent 在 20 轮以前就主动停手、宣布完成，但验证没过。说明它的**自检没覆盖真正的
判分点**——这正是我们一直在治的"假通过"，在更难/更含糊的题上仍会发生。

| 任务 | 停在第几轮 | 真实失败原因（来自 verifier） |
|---|---|---|
| polyglot-rust-c | 3 | 文件放错位置：测试要 `/app/polyglot/main.rs`，agent 没放到精确路径 |
| qemu-startup | 3 | 产物缺失：`/tmp/data.txt` 不存在（没真正启动 Alpine VM 取数据就收手） |
| model-extraction-relu-logits | 6 | 偷出来的矩阵数值不对（`test_stolen_matrix_matches`） |
| openssl-selfsigned-cert | 14 | 官方验证脚本返回非 0：证书不满足要求 |
| filter-js-from-html | 14 | XSS 向量没被全部拦住（`test_filter_blocks_xss`） |
| mteb-retrieve | 12 | `result.txt` 内容不对（期望精确一行文本，实际值不符） |
| extract-elf | 13 | 输出与参考不一致（编译/执行链路结果不匹配） |

**问题归纳**
- 反复出现的根因是**自检与判分点不对齐**：
  - 没核对**精确路径/精确文件名**（polyglot-rust-c）；
  - 没核对**精确输出内容**就判定完成（mteb-retrieve、qemu-startup 的产物缺失）；
  - 自己的测试**覆盖面不足**（filter 只测了部分 XSS 向量、矩阵只对了部分元素）。
- 验证阶段需要"用任务给定的判定方式/参考产物去比对"，而不是 agent 自拟的、更宽松的检查。

---

## 通过的 29 道（参考）

bn-fit-modify, build-pov-ray, cancel-async-tasks, cobol-modernization, configure-git-webserver,
constraints-scheduling, distribution-search, feal-differential-cryptanalysis, fix-code-vulnerability,
fix-git, git-leak-recovery, headless-terminal, hf-model-inference, kv-store-grpc,
large-scale-text-editing, log-summary-date-ranges, merge-diff-arc-agi-task, modernize-scientific-stack,
multi-source-data-merger, nginx-request-logging, polyglot-c-py, portfolio-optimization, prove-plus-comm,
pypi-server, pytorch-model-cli, regex-log, sparql-university, sqlite-db-truncate, vulnerable-secret

---

## 问题优先级小结（仅分析，未改任何代码）

1. **最高性价比：exec 超时崩溃（15 道）**。一个未捕获的 120s 超时让 ~18% 的任务直接报废，
   且多是"只要环境装好就有戏"的题。属于 agent 健壮性问题，不是任务难度问题。
2. **轮数 / 时间预算（33 道超 20 轮 + 1 道超 900s）**。重任务的轮数与单命令时限明显不足。
3. **自检对齐判分点（7 道假通过）**。验证阶段需要对齐"精确路径 / 精确内容 / 完整覆盖"，
   是 agent 行为层面最该继续打磨的点。
4. 余下若干题超出当前能力上限（精确算法、图像相似度、性能阈值），属于模型/方法天花板。
