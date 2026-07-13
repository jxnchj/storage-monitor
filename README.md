# storage-monitor · 存储板块监控与决策辅助 Dashboard

两步走存储研究的活体化监控（PRD v0.3 架构）：**行业态为主体、个股为派生下游**；自动数据层程序化更新，判断层由 Claude 会话经作者确认后更新。

> **硬边界**：本仓全部内容为描述性研究状态标签与条件触发——无目标价、无仓位、无买入/卖出/持有/做空建议。词汇黑名单由 pipeline（`blacklist_check`）与前端双重审查。

## 架构

```
GitHub Actions（每日两次：UTC 09:30 / 21:30 ≈ 台北 17:30 / 05:30）
  └─ pipeline/fetch_data.py     ← tushare(token=Secrets) + yfinance 直连
       ├─ 行情：11 标的（美/韩=yfinance，A股=tushare）+ ^SOX
       ├─ 引擎：pipeline/engine.py（V_C/V_M/V_S/V_stress、implied N*/g、位置带）
       ├─ 温度：申万半导体 801081.SI PE/PB + 2020 以来分位（唯一 A 股口径）+ basket
       ├─ 第三腿：A股 PE(TTM)/PB（daily_basic）
       ├─ 条件触发旗标：E0 保质期、带位迁移、P<V_M 下沿参考（非买入线）
       └─ 写 data/auto/latest.json（+ history/YYYY-MM-DD.json 留痕）
Claude 会话（演练/事件加照 → 作者确认 → 四闸门）
  └─ 手工/半自动更新 data/judgment.json（行业态·灯号·事件台账·周期日志·待确认）
index.html（纯静态，GitHub Pages 托管）
  └─ 读取上述两个 JSON 渲染；fetch 失败时回退到内嵌种子
```

## 部署（约 10 分钟）

1. GitHub 新建**公开**仓 `storage-monitor`，把本目录全部内容 push 上去（含 `.github/`）。
2. 仓库 **Settings → Secrets and variables → Actions → New repository secret**：`TUSHARE_TOKEN` = 你的 tushare token（加密存储、日志掩码，不会出现在仓库/页面/数据中）。
3. **Settings → Pages**：Source = Deploy from a branch，Branch = `main` / `/ (root)`。
4. **Actions** 页启用 workflows，手动 Run 一次 `update-data` 验证（绿色 ✓ 且 data/ 有新 commit）。
5. 访问 `https://<你的用户名>.github.io/storage-monitor/`。手机浏览器可直接打开（自适应 + 深色模式）。

### 安全模型
- token 只存在于 Actions Secrets 与运行器内存；仓库、数据 JSON、前端均无任何凭据。
- fork 的 PR 默认拿不到 Secrets；最坏情形=配额被耗，tushare 后台可随时重置。
- 若日后想遮蔽研究内容：转私有仓 + Cloudflare Pages/Access，前端零改动。

## 判断层更新流程（人在环四闸门）

行业态迁移 / 灯号定级 / 判断型证据定级 / 参数台账变更，只能经 Claude 会话产出 → **作者确认** → 修改 `data/judgment.json`（及 `pipeline/params.json`）→ commit。每次变化同步追加 `cycle_log` 条目（与项目档案《周期日志.md》一致）。取证协议：A 官方行为 > A- 官方表态 > B 专业库 > C 传闻/卖方，原文+URL+日期留痕。

## 本地开发

```bash
pip install -r pipeline/requirements.txt
SEED_FILE=pipeline/seed_20260709.json python3 pipeline/fetch_data.py   # 离线种子模式
TUSHARE_TOKEN=xxx python3 pipeline/fetch_data.py                       # 实取模式
python3 -m http.server 8000   # 打开 http://localhost:8000
```

改动 `index_template.html` 后重新注入种子生成 `index.html`：
```bash
python3 - <<'PY'
tpl=open('index_template.html',encoding='utf-8').read()
a=open('data/auto/latest.json',encoding='utf-8').read(); j=open('data/judgment.json',encoding='utf-8').read()
open('index.html','w',encoding='utf-8').write(tpl.replace('__SEED_AUTO__',a.replace('</','<\\/')).replace('__SEED_JUDGMENT__',j.replace('</','<\\/')))
PY
```

## 文件地图

| 路径 | 作用 |
|---|---|
| `index.html` / `index_template.html` | 静态 dashboard（成品 / 种子注入模板） |
| `data/judgment.json` | LLM 判断层（仅经作者确认更新） |
| `data/auto/latest.json` + `history/` | 自动数据层最新值 + 每日留痕 |
| `pipeline/params.json` | 机器可读参数台账（value/prior/source/grade/as_of） |
| `pipeline/engine.py` | 估值引擎（two_step v2 移植，零方法改动） |
| `pipeline/fetch_data.py` | 取数+计算+旗标+黑名单自检 |
| `pipeline/sw_pe_history.csv` | 申万半导体 PE 历史（分位基期 2020 起，每日追加） |
| `.github/workflows/update-data.yml` | 定时任务（每日两次 + 手动触发） |

## 方法与上游档案

方法：《研究纲领 v2.3》《完整方法论 审计版 v1.2》（两步走 · 双向状态机 · 状态依赖下轨 · 三角验证 · 监控运行规则 §20–26）。研究成稿与周期日志存于本地项目 `Storage-research/`（按规划 v2.0 后决定是否合仓）。

