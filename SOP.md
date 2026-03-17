# Star Tokens Usage Tracking — SOP

## 核心原则

> **所有 usage 价格必须使用 OpenAI/Google 零售 API 定价，绝对不能用 Team/Pro 订阅本身的价值来计算。**

---

## Codex 价格计算

### 数据来源
- `tu daily --json` — 读取 `~/.codex/sessions/` 和 `~/.codex/archived_sessions/` 下的本地 JSONL session 文件
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

## Antigravity 价格计算

### 数据来源
- nettop 网络流量监控 → 估算 token 数量
- `tu antigravity --json` → 实时 quota 配额数据

### 定价
- 按当前模型的零售 API 价格（如 Claude Opus 4.6: input $5/M, output $25/M）

### Quota 配额
- 每 20% 配额档位 = **$50**（可配置 `quota_price.json`）
- 100% = $250
- 用作显示参考，**不作为 cost cap**

---

## 工具清单

| 工具 | 类型 | 用途 | 注意 |
|------|------|------|------|
| `tu` (tokenusage) | Rust CLI v1.5.2 | Codex/Anti token 读取 | 硬编码定价，不做对话去重 |
| `@ccusage/codex` | npm CLI | Codex 交叉验证 | LiteLLM 实时价，不做对话去重 |
| `state_5.sqlite` | Codex 本地 DB | 去重用 | `threads` 表有 title + tokens |

## 配置文件

| 文件 | 用途 |
|------|------|
| `~/.config/anti-tracker/quota_price.json` | Anti 每 20% 档位价格 |
| `~/.config/anti-tracker/current_model.json` | Anti 当前模型 |
| `~/.config/anti-tracker/nettop_usage.jsonl` | Anti 网络流量日志 |
| `~/.codex/state_5.sqlite` | Codex session DB (去重) |
| `~/.codex/sessions/` | Codex session JSONL (tu 数据源) |
