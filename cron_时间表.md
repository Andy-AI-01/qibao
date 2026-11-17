# 起爆雷达 · 定时任务时间表

> 换算铁律：**北京时间 − 8 小时 = UTC**。中国不实行夏令时，全年恒定。
> GitHub Actions 的 `cron` 只认 UTC；cron-job.org 可以指定 `Asia/Shanghai`。

---

## 一、真实执行路径（谁在跑）

| 来源 | 文件 | 时区 | 角色 |
|---|---|---|---|
| cron-job.org「起爆雷达」 | → `update.yml` dispatch | Asia/Shanghai | **主力**（每 15 分钟） |
| cron-job.org「起爆雷达-竞价」 | → `auction.yml` dispatch | Asia/Shanghai | **主力**（9:25 一次） |
| GitHub `update.yml` | `.github/workflows/update.yml` | UTC | 备胎 |
| GitHub `auction.yml` | `.github/workflows/auction.yml` | UTC | 备胎 |

GitHub 自带的 cron **延迟 3–30 分钟很常见**，卡时间窗的任务（如 9:25 竞价）靠它必失败，所以主力是 cron-job.org。

---

## 二、`update.yml` 的 6 条 cron

```yaml
on:
  schedule:
    - cron: '40 0 * * 1-5'      # ① 盘前解读
    - cron: '25 1 * * 1-5'      # ② 集合竞价
    - cron: '30 1 * * 1-5'      # ③ 竞价兜底
    - cron: '*/15 1-6 * * 1-5'  # ④ 盘中滚动
    - cron: '0 7 * * 1-5'       # ⑤ 收盘瞬间
    - cron: '35 7 * * 1-5'      # ⑥ 收盘定稿
  workflow_dispatch:
```

| # | 北京时间 | UTC | cron | 干什么 |
|---|---|---|---|---|
| ① | **08:40** | 00:40 | `40 0` | 盘前解读：外盘 5 指数 + 隔夜要闻 + 昨日复盘 + 今日关注 |
| ② | **09:25** | 01:25 | `25 1` | 集合竞价三判（窗口 9:23–9:31） |
| ③ | **09:30** | 01:30 | `30 1` | 竞价兜底（仍在窗口内） |
| ④ | **09:00 → 14:45** | 01:00–06:45 | `*/15 1-6` | 盘中滚动，每 15 分钟共 24 个点 |
| ⑤ | **15:00** | 07:00 | `0 7` | 收盘瞬间（拿到当日收盘价） |
| ⑥ | **15:35** | 07:35 | `35 7` | 收盘定稿（涨停池/板块/异动解读已齐） |

### ④ 展开的 24 个时间点（北京时间）

```
 9:00  9:15  9:30  9:45
10:00 10:15 10:30 10:45
11:00 11:15 11:30 11:45
12:00 12:15 12:30 12:45   ← 午休，跑了也没新数据
13:00 13:15 13:30 13:45
14:00 14:15 14:30 14:45   ← 最后一个点
```

---

## 三、`auction.yml` 的 1 条 cron

```yaml
on:
  schedule:
    - cron: '25 1 * * 1-5'      # 北京 09:25
  workflow_dispatch:
```

和 `update.yml` 的 ② 同一时刻，**故意重复**。

原因：`update.yml` 有 `concurrency: group: radar`，若 9:15 那班扫描超时，9:25 会排队，等到 9:32 就被窗口守卫跳掉。
`auction.yml` 用 `group: auction`，**独立队列不受阻塞**。两条路任意一条通了就够。

---

## 四、cron-job.org 两个任务

| 任务名 | cron（北京时间） | URL |
|---|---|---|
| 起爆雷达 | `*/15 9-14 * * 1-5` | `https://api.github.com/repos/Andy-AI-01/qibao/actions/workflows/update.yml/dispatches` |
| 起爆雷达-竞价 | `25 9 * * 1-5` | `https://api.github.com/repos/Andy-AI-01/qibao/actions/workflows/auction.yml/dispatches` |

两个都要：
- Method: **POST**
- Body: `{"ref":"main"}`
- Header: `Authorization: Bearer <GitHub PAT>`（`repo` + `workflow` 权限）
- Header: `Accept: application/vnd.github+json`
- 时区：`Asia/Shanghai`

cron-job.org 填的是北京时间，**不用减 8 小时**。

---

## 五、改时间怎么算

```
北京 09:25 → UTC 01:25   →  '25 1'
北京 15:00 → UTC 07:00   →  '0 7'
北京 15:35 → UTC 07:35   →  '35 7'
北京 20:00 → UTC 12:00   →  '0 12'
```

### 跨日陷阱

```
北京 周二 00:30  →  UTC 周一 16:30  →  '30 16 * * 1-5'
                                        ↑ 星期要减一天！
```

北京 00:00–07:59 之间的任何时刻，UTC 都落在**前一天**。

---

## 六、三条禁令

1. **不要写 `*/5` 以下**。GitHub 最短 5 分钟一次，繁忙时直接丢弃；`*/5` 一天 78 次纯烧额度。
2. **不要给 `concurrency` 加 `cancel-in-progress: true`**。会打断正在 `git push` 的班次，留下半个 data.json。
3. **不要在 `--auction` 前面省掉时间窗守卫**：

   ```bash
   H=$((10#$(TZ=Asia/Shanghai date +%H%M)))
   if [ "$H" -ge 923 ] && [ "$H" -le 931 ]; then
     python scan_cloud.py --auction
   fi
   ```

   `do_auction()` 无条件写 `auction.json`，`valid = _au_time_ok(now)`。
   没有守卫的话，9:45 那班会把你 9:25 采到的 `valid:true` 刷成 `false`，
   下午打开页面会看到「非竞价时段，金额不可用」。

---

## 七、任务耗时（决定会不会撞车）

| 任务 | 典型耗时 | 说明 |
|---|---|---|
| `--auction` | 约 1 分钟 | 快照 60 只/批 + 同花顺 3 接口 |
| 全量扫描 | 3–4 分钟 | 5223 只 / 16 并发 |
| 盘前解读 | 约 2 分钟 | 含新闻 + 外盘 |

`*/15` 间隔 15 分钟、单班 3–4 分钟，正常不会积压。但如果腾讯限流导致单班跑满 `timeout-minutes: 25`，后续班次会排队——这是 `concurrency` 的预期行为，数据不会坏，只是晚。
