# Star Tokens Usage Tracking — SOP

## 核心原则

> **所有 usage 价格必须使用 OpenAI/Google 零售 API 定价，绝对不能用 Team/Pro 订阅本身的价值来计算。**

---

## 覆盖范围

Star Tokens 必须同时覆盖三类本地 usage：

- **Codex**
- **Claude Code**
- **Antigravity**

任何 dashboard、README、GitHub About 文案都要明确说明这三个来源，不能再写成 Antigravity-only。

---

## Codex 价格计算

### 数据来源
- `tu codex --json` — 读取 `~/.codex/sessions/` 和 `~/.codex/archived_sessions/` 下的本地 JSONL session 文件
- Token 数量由 OpenAI API response 中的 `last_token_usage` 返回
- 交叉验证工具：`@ccusage/codex`（npm，用 LiteLLM 实时定价）

### 定价（GPT-5.4 零售价）
| Token 类型 | 单价 |
|-----------|------|
| Input (非 cached) | $2.50/M |
| Cached Input | $0.625/M |
| Output | $10.00/M |
| Reasoning Output | $10.00/M |

### ⚠️ 对话级去重（关键！）

> **`tu` 和 `ccusage` 都有同样的 bug：直接 sum 所有 session，不做对话级去重，导致严重重复计算。**

#### 重复计算的两个来源

1. **Worktree 并行副本**：同一个对话在多个 worktree 同时运行（3个 worktree = 3x）
2. **对话重启**：同一个对话关闭后重新打开，新 session 会**重新报告全部累积 context**
   - Session 1 结束时：250M tokens（累积）
   - 用户重启 → Session 2 从 0 开始，重新积累到 260M
   - `tu` 算法：250M + 260M = 510M ❌
   - **实际使用：260M（最新 session 的总量）** ✅

#### 实测案例
```
"claude告诉我你做错了" 对话：
  18 个 session × ~250M = 4.2B tokens (tu 报的)
  实际 = MAX session = 262M tokens
  → 15.7x 重复计算！
```

#### 正确算法
```text
1. 读 ~/.codex/state_5.sqlite 的 threads 表
2. 按 (日期, title) 分组 ← 日期用 created_at 的 UTC 日期
3. 每组取 MAX(tokens_used)
4. 计算 dedup_ratio = deduped / raw
5. 用此 ratio 缩放 tu 的【所有】字段：
   - input_tokens, cache_read_input_tokens, output_tokens
   - reasoning_output_tokens, total_tokens, cost_usd
   - 必须全部同步缩放，不得只缩放 cost 而保留原始 token 数
```

#### 每日数据隔离（必须遵守）
- **每天的数据必须独立计算**，不得跨天合并
- 同一个对话跨天（03-16 开始，03-17 继续）：每天各自独立去重
- Session 归属日期以 `created_at` UTC 为准
- 去重 ratio 按天独立计算，不得用全局 ratio 应用到所有天

### 禁止事项
- ❌ 不得用 Team 月费 ($25/seat) 反推 daily cost
- ❌ 不得设置任何 daily cap
- ❌ 不得用订阅价值替代零售价格
- ❌ 不得直接 sum 所有 session（必须先去重）

---

## Claude Code 价格计算

### 数据来源
- `tu claude --json` — tokenusage 的 Claude Code 报告
- `~/.claude/projects/**/*.jsonl`
- `~/.config/claude/projects/**/*.jsonl`
- 交叉验证工具：`npx ccusage@latest daily --json`

### 本地 parser 规则
- 只统计 assistant usage entry
- 统计字段：
  - `input_tokens`
  - `cache_creation_input_tokens`
  - `cache_read_input_tokens`
  - `output_tokens`
  - `reasoning_output_tokens`
- 用 `message.id + requestId` 去重
- `total_tokens = input + cache_creation + cache_read + output`

### 数据源选择
- 默认读取 `tu claude --json` 做对照
- 同时运行本地 JSONL parser
- 当本地 parser 与 `tu claude` 的 all-time token 差异超过 2% 时，dashboard 使用本地 dedup parser，并在 Claude Validation 区展示差异
- `ccusage` 仅作为外部诊断，不作为强依赖

### 定价
- 使用 Claude/Anthropic 零售 API 价格口径
- cache creation 按工具基线口径计价：`cache_creation_input_tokens * input_price * 1.25`
- cache read 按 `cache_read_input_tokens * input_price * 0.1`
- 不使用 Claude Pro/Max 订阅价格反推

### 禁止事项
- ❌ 不得把 Claude Code usage 算进 Codex
- ❌ 不得直接 sum 重复的 assistant usage entry
- ❌ 不得在 dashboard 隐藏本地 parser 与 `tu claude` 的显著差异

---

## Antigravity 价格计算

### 数据来源
- nettop 网络流量监控 → 估算 token 数量
- `quota_snapshots.jsonl` → 用于判断当天 primary model 是否 idle
- `tu antigravity --json` → 仅用于 estimator 采集 snapshot，不在 dashboard 单独展示 quota 区

### 定价
- 按当前模型的零售 API 价格（如 Claude Opus 4.6: input $5/M, output $25/M）

### Idle / snapshot 逻辑
- 不删除历史 Anti 日志
- 如果当天有 snapshot 且 primary model used% 为 0，则 dashboard suppress nettop raw estimate，显示 Anti idle / $0
- primary model 选择优先级：
  1. 非-thinking Claude
  2. Gemini Pro Low
  3. Gemini Flash
  4. fallback：remaining 最低
- 避免旧 Claude Thinking 40% 污染今天 Gemini 0%

### 禁止事项
- ❌ 不得默认自动启动 `anti_estimator.py`
- ❌ 不得在 dashboard 单独展示 quota 面板
- ❌ 不得用订阅 quota 价值替代零售 API 价格

---

## 工具清单

| 工具 | 类型 | 用途 | 注意 |
|------|------|------|------|
| `tu` (tokenusage) | Rust CLI v1.5.2 | Codex/Claude/Anti 读取 | 需要额外验证和去重 |
| `@ccusage/codex` | npm CLI | Codex 交叉验证 | LiteLLM 实时价，不做对话去重 |
| `ccusage` | npm CLI | Claude Code 交叉验证 | 外部诊断，不作为强依赖 |
| `state_5.sqlite` | Codex 本地 DB | 去重用 | `threads` 表有 title + tokens |

## 配置文件

| 文件 | 用途 |
|------|------|
| `~/.config/anti-tracker/quota_price.json` | Anti 每 20% 档位价格 |
| `~/.config/anti-tracker/current_model.json` | Anti 当前模型 |
| `~/.config/anti-tracker/nettop_usage.jsonl` | Anti 网络流量日志 |
| `~/.config/anti-tracker/quota_snapshots.jsonl` | Anti idle / adjusted cost 判断 |
| `~/.codex/state_5.sqlite` | Codex session DB (去重) |
| `~/.codex/sessions/` | Codex session JSONL (tu 数据源) |
| `~/.claude/projects/` | Claude Code JSONL (本地 parser 数据源) |
| `~/.config/claude/projects/` | Claude Code JSONL 兼容路径 |
