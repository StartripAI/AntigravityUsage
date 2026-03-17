# Star Tokens Usage Tracking — SOP

## 核心原则

> **所有 usage 价格必须使用 OpenAI/Google 零售 API 定价，绝对不能用 Team/Pro 订阅本身的价值来计算。**

---

## Codex 价格计算

### 数据来源
- `tu daily --json` — 读取 `~/.codex/sessions/` 下的本地 JSONL session 文件
- Token 数量由 OpenAI API 返回，准确无误

### 定价（GPT-5.4 零售价）
| Token 类型 | 单价 |
|-----------|------|
| Input (非 cached) | $2.50/M |
| Cached Input | $0.625/M |
| Output | $10.00/M |
| Reasoning Output | $10.00/M |

### Worktree 去重
Codex worktree 会为同一个对话创建并行副本。去重规则：
1. 读 `~/.codex/state_5.sqlite` 的 `threads` 表
2. 按 **title + 5秒时间窗** 分组
3. 每组取 **max(tokens_used)**，不是 sum
4. 计算 `dedup_ratio = deduped_total / raw_total`
5. 用此 ratio 缩放 `tu` 的所有字段（tokens + cost）

### 禁止事项
- ❌ 不得用 Team 月费 ($25/seat) 反推 daily cost
- ❌ 不得设置任何 daily cap
- ❌ 不得用订阅价值替代零售价格

---

## Antigravity 价格计算

### 数据来源
- nettop 网络流量监控 → 估算 token 数量
- `tu antigravity --json` → 实时 quota 配额数据

### 定价
- 按当前模型的零售 API 价格（如 Claude Opus 4.6: input $5/M, output $25/M）
- nettop 流量 → 按 API ratio 换算 tokens → 乘以零售价

### Quota 配额
- 每 20% 配额档位 = **$50**（可配置）
- 100% = $250
- 用作显示参考，**不作为 cost cap**

### 配额追踪
- `poll_quota()` 定期从 `tu antigravity --json` 获取配额快照
- 记录到 `~/.config/anti-tracker/quota_snapshots.jsonl`

---

## 配置文件

| 文件 | 用途 |
|------|------|
| `~/.config/anti-tracker/quota_price.json` | Anti 每 20% 档位价格 `{"cost_per_20pct": 50}` |
| `~/.config/anti-tracker/current_model.json` | Anti 当前模型 |
| `~/.config/anti-tracker/nettop_usage.jsonl` | Anti 网络流量日志 |
| `~/.config/anti-tracker/quota_snapshots.jsonl` | Anti 配额快照 |
| `~/.codex/state_5.sqlite` | Codex session DB (worktree dedup) |
| `~/.codex/sessions/` | Codex session JSONL (tu 数据源) |

---

## Dashboard 显示

| 卡片 | 数据源 | 算法 |
|------|--------|------|
| Codex | `tu daily --json` | 零售价 × dedup ratio |
| Antigravity · Quota | nettop 估算 | 零售价, cap $250/day |
| Input/Cached/Output Tokens | `tu` + nettop | 准确 token 数 |
| Quota Bars | `tu antigravity --json` | 实时配额 % |
