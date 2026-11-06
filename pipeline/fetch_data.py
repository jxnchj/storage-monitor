#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动数据层 pipeline：行情/引擎/位置带/第三腿/温度 → data/auto/latest.json
- 纯程序、不经 LLM；判断层内容一律来自 data/judgment.json（本脚本只读不写）。
- 降级不静默：取数失败写入 degraded；个股沿用上期值并打 stale；依赖失败源的聚合字段（basket、
  申万、SOX）置 null，不写旧值。工作流末步 --check-degraded 在有降级时让运行显示失败。
- 只记收盘：美/韩股的 P 与 SOX 取最近一个已收盘交易日，as-of 即该交易日，不取盘中价（见 last_closed）。
- SEED_FILE 环境变量：离线模式（本地验证/首期种子），跳过网络取数。
- 硬边界：输出均为描述性研究状态；词汇黑名单自检见 blacklist_check()。
"""
import json, os, sys, csv, datetime, math, shutil, copy, zoneinfo

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data", "auto")
sys.path.insert(0, HERE)
import engine

BLACKLIST = ["目标价", "建仓", "加仓", "减仓", "买入", "卖出", "持有建议", "做空", "止损", "仓位"]

COMPLIANCE_PHRASES = ["非买入线", "无买入", "无卖出", "无目标价", "无仓位",
                      "不给买入", "不构成买入", "买入/卖出/持有/做空建议", "无买卖建议", "非交易指令"]

def blacklist_check(obj):
    s = json.dumps(obj, ensure_ascii=False)
    for ph in COMPLIANCE_PHRASES:  # 合规否定句先剔除，再扫描裸词
        s = s.replace(ph, "")
    hits = [w for w in BLACKLIST if w in s]
    if hits:
        raise SystemExit(f"词汇黑名单违规: {hits}")

def load_json(p, default=None):
    try:
        with open(p, encoding="utf-8") as f: return json.load(f)
    except Exception: return default

def sw_percentile(cur_pe):
    vals = []
    with open(os.path.join(HERE, "sw_pe_history.csv")) as f:
        for row in csv.DictReader(f): vals.append(float(row["pe"]))
    vals.sort()
    import bisect
    return round(bisect.bisect_left(vals, cur_pe) / len(vals) * 100, 1)

def sw_append_history(trade_date, pe):
    p = os.path.join(HERE, "sw_pe_history.csv")
    with open(p) as f: last = f.read().strip().split("\n")[-1].split(",")[0]
    if trade_date > last:
        with open(p, "a") as f: f.write(f"{trade_date},{pe}\n")

def sw_spark(n=260):
    rows = []
    with open(os.path.join(HERE, "sw_pe_history.csv")) as f:
        for row in csv.DictReader(f): rows.append((row["trade_date"], float(row["pe"])))
    rows = rows[-n:]
    step = max(1, len(rows)//52)
    return [{"d": d, "pe": round(v,1)} for d, v in rows[::step]] + [{"d": rows[-1][0], "pe": round(rows[-1][1],1)}]

# ---------- 取数 ----------
# (时区, 法定收盘, 结算时刻)。结算时刻前的当日 K 线一律视为盘中：
# 美股留 30 分钟给收盘竞价入账；韩股放到 16:45，覆盖高考日延后到 16:30 的收盘。
SESSIONS = {"US": ("America/New_York", datetime.time(16, 0), datetime.time(16, 30)),
            "KR": ("Asia/Seoul", datetime.time(15, 30), datetime.time(16, 45))}

def market_of(ticker):
    return "KR" if ticker.endswith(".KS") else "US"

def last_closed(bars, last_trade, market, now_utc):
    """返回最近一个已收盘交易日的 (价格, 日期, 来源)，取不到时价格为 None。
    bars：[(交易所本地日期, 收盘价|None)] 升序；last_trade：(价格, 带时区的成交时间) 或 None。
    Yahoo 当日 K 线盘中就有值，收盘后却常变空、数小时后才回填，所以结算前丢弃当日 K 线；
    结算后若 K 线仍空，用法定收盘时刻之后的最后成交价补上当日。"""
    tz_name, close_t, settled_t = SESSIONS[market]
    tz = zoneinfo.ZoneInfo(tz_name)
    now = now_utc.astimezone(tz)
    def is_closed(day):
        return day < now.date() or (day == now.date() and now.time() >= settled_t)
    closed = [(d, c) for d, c in bars if c is not None and is_closed(d)]
    price, day, src = (closed[-1][1], closed[-1][0], "bar") if closed else (None, None, None)
    if last_trade and last_trade[0]:
        local = last_trade[1].astimezone(tz)
        if is_closed(local.date()) and local.time() >= close_t and (day is None or local.date() > day):
            price, day, src = last_trade[0], local.date(), "last_trade"
    return price, day, src

def yf_last_closed(yf, ticker, now_utc):
    tk = yf.Ticker(ticker)
    hist = tk.history(period="10d", interval="1d", auto_adjust=False)  # 原始收盘价，不做分红复权
    bars = [(i.date(), None if c != c else float(c)) for i, c in hist["Close"].items()]
    meta = tk.history_metadata or {}
    t, p = meta.get("regularMarketTime"), meta.get("regularMarketPrice")
    last_trade = (float(p), t.to_pydatetime()) if p and hasattr(t, "to_pydatetime") else None
    return last_closed(bars, last_trade, market_of(ticker), now_utc)

def note_failure(degraded, src, err, ticker=None):
    item = {"src": src, "error": str(err)[:200]}
    if ticker: item["ticker"] = ticker
    degraded.append(item)

def num(x, nd=2):
    """NaN/None → None。亏损公司 pe_ttm 为空属正常，不能写成 NaN（非法 JSON，前端解析会失败）。"""
    try: v = float(x)
    except (TypeError, ValueError): return None
    return None if math.isnan(v) else round(v, nd)

def fetch_third_leg(pro, cn, now_utc, degraded):
    """daily_basic 逐只带日期窗口取：接口文档只写 ts_code/trade_date 二选一，未写支持逗号多代码；
    旧写法一次传 5 只从未取到数据，且异常被吞。缺值现在进 degraded，不再静默。"""
    today_cn = now_utc + datetime.timedelta(hours=8)
    start, end = (today_cn - datetime.timedelta(days=14)).strftime("%Y%m%d"), today_cn.strftime("%Y%m%d")
    third = {}
    for code in cn:
        try:
            db = pro.daily_basic(ts_code=code, start_date=start, end_date=end, fields="ts_code,trade_date,pe_ttm,pb")
            if db is None or db.empty: raise ValueError("daily_basic 无数据")
            row = db.sort_values("trade_date").iloc[-1]
            pe, pb = num(row.pe_ttm), num(row.pb)
            if pe is None and pb is None: raise ValueError("pe_ttm 与 pb 均为空")
            third[code] = {"pe_ttm": pe, "pb": pb, "asof": str(row.trade_date)}
        except Exception as e:
            note_failure(degraded, "tushare.daily_basic", e, code)
    return third

def fetch_live(params, degraded):
    quotes, third, asof, quote_src = {}, {}, {}, {}
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    yf_tickers = [t["src"]["ticker"] for t in params["targets"] if t["src"]["quote"] == "yf"] + ["^SOX"]
    try:
        import yfinance as yf
        for tk in yf_tickers:
            try:
                P, day, src = yf_last_closed(yf, tk, now_utc)
                if P is None: raise ValueError("无已收盘 K 线")
                quotes[tk], asof[tk], quote_src[tk] = P, str(day), src
            except Exception as e:
                note_failure(degraded, "yfinance", e, tk)
    except Exception as e:
        note_failure(degraded, "yfinance", e)
    sox = {"value": quotes.pop("^SOX", None), "asof": asof.get("^SOX"), "quote_src": quote_src.get("^SOX")}
    sw = None
    try:
        import tushare as ts
        pro = ts.pro_api(os.environ["TUSHARE_TOKEN"])
    except Exception as e:
        note_failure(degraded, "tushare", e)
        return quotes, third, sox, sw, asof, quote_src
    cn = [t["src"]["ticker"] for t in params["targets"] if t["src"]["quote"] == "ts"]
    try:
        df = pro.daily(ts_code=",".join(cn))
        for code in cn:
            try:
                sub = df[df.ts_code == code].sort_values("trade_date")
                quotes[code] = float(sub.iloc[-1].close); asof[code] = str(sub.iloc[-1].trade_date); quote_src[code] = "bar"
            except Exception as e:
                note_failure(degraded, "tushare.daily", e, code)
    except Exception as e:
        note_failure(degraded, "tushare.daily", e)
    third = fetch_third_leg(pro, cn, now_utc, degraded)
    try:
        swdf = pro.sw_daily(ts_code="801081.SI", fields="trade_date,pe,pb").sort_values("trade_date")
        last = swdf.iloc[-1]
        pe = num(last.pe, 6)
        if pe is None: raise ValueError("sw_daily pe 为空")
        sw = {"pe": pe, "pb": num(last.pb, 6), "asof": str(last.trade_date)}
        sw_append_history(str(last.trade_date), pe)
    except Exception as e:
        sw = None
        note_failure(degraded, "tushare.sw_daily", e)
    return quotes, third, sox, sw, asof, quote_src

def fetch_seed(seed, params):
    day = seed.get("as_of", {})
    asof = {}
    for t in params["targets"]:
        tk = t["src"]["ticker"]
        asof[tk] = day.get("cn" if t["src"]["quote"] == "ts" else market_of(tk).lower(), "")
    third = {k: {**v, "asof": day.get("cn", "")} for k, v in seed["third_leg"].items()}
    return (dict(seed["quotes"]), third,
            {"value": seed["sox"]["value"], "asof": seed["sox"]["asof"], "quote_src": "seed"},
            dict(seed["sw"]), asof, {tk: "seed" for tk in seed["quotes"]})

# ---------- 计算 ----------
def cons_view_label(E0, E0_cons):
    """第二口径的名字跟数值走：只有确实低于主口径才叫"保守"（江波龙的报告口径高于剥离口径，属并列）。"""
    return "保守口径" if E0_cons < E0 else "并列口径"

def compute_target(t, P, industry_state):
    out = {"id": t["id"], "name": t["name"], "archetype": t["archetype"], "currency": t["currency"],
           "ticker": t["src"]["ticker"], "P": P, "model": t["model"],
           "e0_asof": t["E0"]["as_of"], "e0_note": t["E0"].get("refresh_note", ""), "views": []}
    def cyc_view(label, E0, extra=""):
        V = engine.cyclical_values(E0, t["Em"], t["N_S"], gterm_S=t.get("gterm_S"))
        n, ntxt = engine.implied_Nstar(P, E0, t["Em"])
        b = engine.band(P, V["C"], V["S"])
        return {"view": label, "kind": "cyclical", "E0": E0, "note": extra,
                "V_C": round(V["C"], 2), "V_M": round(V["M"], 2), "V_S": round(V["S"], 2), "V_stress": round(V["STRESS"], 2),
                "p_vc": round(P / V["C"], 2), "p_vm": round(P / V["M"], 2), "p_vs": round(P / V["S"], 2),
                "implied": ntxt, "band": b, "band_label": engine.BAND_LABELS[b],
                "a2": engine.a2_label(industry_state, P, V["C"], V["S"]),
                "p_below_vm": P < V["M"]}
    def gro_view(label, E0, g, extra=""):
        V = engine.growth_values(E0, g["gC"], g["gM"], g["gS"])
        ig = engine.implied_g1(P, E0)
        b = engine.band(P, V["C"], V["S"])
        return {"view": label, "kind": "growth", "E0": E0, "note": extra or g.get("consensus_note", ""),
                "V_C": round(V["C"], 2), "V_M": round(V["M"], 2), "V_S": round(V["S"], 2),
                "p_vc": round(P / V["C"], 2), "p_vm": round(P / V["M"], 2), "p_vs": round(P / V["S"], 2),
                "implied": f"g≈{ig*100:.1f}%", "band": b, "band_label": engine.BAND_LABELS[b],
                "a2": engine.a2_label(industry_state, P, V["C"], V["S"]),
                "p_below_vm": P < V["M"]}
    if t["model"] == "cyclical":
        out["views"].append(cyc_view("主口径", t["E0"]["value"]))
        if t.get("E0_cons", {}).get("value"):
            out["views"].append(cyc_view(cons_view_label(t["E0"]["value"], t["E0_cons"]["value"]), t["E0_cons"]["value"], t["E0_cons"].get("note", "")))
    elif t["model"] == "growth":
        out["views"].append(gro_view("成长档", t["E0"]["value"], t["growth"]))
    elif t["model"] == "dual":
        out["views"].append(cyc_view("周期视角", t["E0"]["value"]))
        g = t["growth"]
        out["views"].append(gro_view("成长/复苏视角", g.get("E0_growth", t["E0"]["value"]), g))
    # E0 保质期（v1.2 §20）
    days = (datetime.date.today() - datetime.date.fromisoformat(t["E0"]["as_of"])).days
    out["e0_age_days"] = days
    out["flags"] = (["E0超一季未复核·置信度降档"] if days > 92 else []) + ([t["E0"]["refresh_note"]] if t["E0"].get("refresh_note") else [])
    return out

def main():
    params = load_json(os.path.join(HERE, "params.json"))
    judgment = load_json(os.path.join(ROOT, "data", "judgment.json"), {})
    industry_state = judgment.get("industry_state", {}).get("label", "顶部钝化")
    prev = load_json(os.path.join(DATA, "latest.json"), {})
    degraded = []
    seed_path = os.environ.get("SEED_FILE")
    if seed_path:
        quotes, third, sox, sw, asof, quote_src = fetch_seed(load_json(seed_path), params)
    else:
        quotes, third, sox, sw, asof, quote_src = fetch_live(params, degraded)
    # 降级回填：个股沿用上期值，保留上期 as-of 并打标
    prev_rows = {x["ticker"]: x for x in prev.get("targets", [])}
    targets, stale_tickers = [], []
    for t in params["targets"]:
        tk = t["src"]["ticker"]
        P, stale = quotes.get(tk), False
        if P is None:
            stale_tickers.append(tk)
            P, stale = prev_rows.get(tk, {}).get("P"), True
            if P is None: continue
        row = compute_target(t, P, industry_state)
        row["stale"] = stale
        row["asof"] = prev_rows[tk].get("asof", "") if stale else asof.get(tk, "")
        row["quote_src"] = "" if stale else quote_src.get(tk, "")
        if stale: row["flags"].insert(0, f"取数失败·沿用上期值(as-of {row['asof'] or '未知'})")
        if tk in third: row["third_leg"] = third[tk]
        prev_t = next((x for x in prev.get("targets", []) if x["id"] == t["id"]), None)
        if prev_t and prev_t["views"] and row["views"] and prev_t["views"][0]["band"] != row["views"][0]["band"]:
            row["flags"].append(f"带位迁移: {prev_t['views'][0]['band_label']}→{row['views'][0]['band_label']}")
        if row["views"][0].get("p_below_vm") and t["archetype"] in ("A", "B", "E"):
            row["flags"].append("P<V_M下沿参考·复核结构事实(非买入线)")
        targets.append(row)
    base = params["_meta"]["baseline_prices_20260601"]
    cn5 = ["301308.SZ", "688008.SH", "002409.SZ", "603986.SH", "688123.SH"]
    basket_missing = [c for c in cn5 if c not in quotes]  # 缺一只就不算：旧值掺进等权均值看不出来
    basket = None if basket_missing else round(sum(quotes[c] / base[c] - 1 for c in cn5) / 5 * 100, 1)
    SOX_BASE_0602 = 13726.27
    if sox and sox.get("value"):
        sox["chg_vs_0602"] = round(sox["value"] / SOX_BASE_0602 - 1, 4)
    sw_pct = sw_percentile(sw["pe"]) if sw else None
    out = {
        "generated_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "degraded": degraded,
        "stale_tickers": stale_tickers,
        "temperature": {
            "sw_pe": sw and round(sw["pe"], 2), "sw_pb": (round(sw["pb"], 2) if sw and sw.get("pb") else None),
            "sw_asof": sw and sw.get("asof"), "sw_pe_percentile_since2020": sw_pct,
            "sw_spark": sw_spark(), "sw_anchor": {"peak2021": 106.5, "median": 82.1, "base_0601": 110.45},
            "sox": sox, "basket_vs_0601_pct": basket, "basket_missing": basket_missing,
            "caliber_note": "A股温度唯一口径=申万半导体801081.SI(sw_daily)；分位基期2020-01起；跨口径禁比"
        },
        "targets": targets
    }
    blacklist_check(out)
    os.makedirs(DATA, exist_ok=True)
    with open(os.path.join(DATA, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    day = datetime.date.today().isoformat()
    shutil.copy(os.path.join(DATA, "latest.json"), os.path.join(DATA, "history", f"{day}.json"))
    print(f"ok: {len(targets)} targets, degraded={len(degraded)}, stale={len(stale_tickers)}, sw_pctile={sw_pct}")

def check_degraded(path=None):
    """工作流末步：数据已提交后，有降级就让运行显示失败。返回退出码。"""
    d = load_json(path or os.path.join(DATA, "latest.json"), None)
    if d is None:
        print("::error::latest.json 缺失或无法解析")
        return 1
    problems = [f"{x['src']}{':' + x['ticker'] if x.get('ticker') else ''} {x.get('error', '')}" for x in d.get("degraded", [])]
    if d.get("stale_tickers"):
        problems.append("沿用上期值: " + ",".join(d["stale_tickers"]))
    for x in problems:
        print(f"::error::自动层降级 {x}")
    return 1 if problems else 0

if __name__ == "__main__":
    if "--check-degraded" in sys.argv[1:]:
        sys.exit(check_degraded())
    main()

