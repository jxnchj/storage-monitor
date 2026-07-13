#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动数据层 pipeline：行情/引擎/位置带/第三腿/温度 → data/auto/latest.json
- 纯程序、不经 LLM；判断层内容一律来自 data/judgment.json（本脚本只读不写）。
- 降级不静默：任一数据源失败→沿用上期值并打 stale/degraded 标记。
- SEED_FILE 环境变量：离线模式（本地验证/首期种子），跳过网络取数。
- 硬边界：输出均为描述性研究状态；词汇黑名单自检见 blacklist_check()。
"""
import json, os, sys, csv, datetime, shutil, copy

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
def fetch_live(params, degraded):
    quotes, third, asof = {}, {}, {}
    yf_tickers = [t["src"]["ticker"] for t in params["targets"] if t["src"]["quote"] == "yf"] + ["^SOX"]
    try:
        import yfinance as yf
        data = yf.download(yf_tickers, period="10d", interval="1d", progress=False, group_by="ticker", threads=False)
        for tk in yf_tickers:
            ser = data[tk]["Close"].dropna()
            quotes[tk] = float(ser.iloc[-1]); asof[tk] = str(ser.index[-1].date())
    except Exception as e:
        degraded.append({"src": "yfinance", "error": str(e)[:200]})
    sox = {"value": quotes.pop("^SOX", None), "asof": asof.get("^SOX")}
    sw = None
    try:
        import tushare as ts
        pro = ts.pro_api(os.environ["TUSHARE_TOKEN"])
        cn = [t["src"]["ticker"] for t in params["targets"] if t["src"]["quote"] == "ts"]
        df = pro.daily(ts_code=",".join(cn))
        for code in cn:
            sub = df[df.ts_code == code].sort_values("trade_date")
            quotes[code] = float(sub.iloc[-1].close); asof[code] = str(sub.iloc[-1].trade_date)
        db = pro.daily_basic(ts_code=",".join(cn)) if hasattr(pro, "daily_basic") else None
        for code in cn:
            try:
                sub = db[db.ts_code == code].sort_values("trade_date").iloc[-1]
                third[code] = {"pe_ttm": round(float(sub.pe_ttm), 2), "pb": round(float(sub.pb), 2)}
            except Exception: pass
        swdf = pro.sw_daily(ts_code="801081.SI", fields="trade_date,pe,pb").sort_values("trade_date")
        last = swdf.iloc[-1]
        sw = {"pe": float(last.pe), "pb": float(last.pb), "asof": str(last.trade_date)}
        sw_append_history(str(last.trade_date), float(last.pe))
    except Exception as e:
        degraded.append({"src": "tushare", "error": str(e)[:200]})
    return quotes, third, sox, sw, asof

def fetch_seed(seed):
    return (dict(seed["quotes"]), dict(seed["third_leg"]),
            {"value": seed["sox"]["value"], "asof": seed["sox"]["asof"]},
            dict(seed["sw"]), {})

# ---------- 计算 ----------
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
            out["views"].append(cyc_view("保守口径", t["E0_cons"]["value"], t["E0_cons"].get("note", "")))
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
        quotes, third, sox, sw, asof = fetch_seed(load_json(seed_path))
    else:
        quotes, third, sox, sw, asof = fetch_live(params, degraded)
    # 降级回填
    prev_q = {x["ticker"]: x["P"] for x in prev.get("targets", [])}
    targets = []
    for t in params["targets"]:
        tk = t["src"]["ticker"]
        P, stale = quotes.get(tk), False
        if P is None:
            P, stale = prev_q.get(tk), True
            if P is None: continue
        row = compute_target(t, P, industry_state)
        row["stale"] = stale
        row["asof"] = asof.get(tk, "")
        if tk in third: row["third_leg"] = third[tk]
        prev_t = next((x for x in prev.get("targets", []) if x["id"] == t["id"]), None)
        if prev_t and prev_t["views"] and row["views"] and prev_t["views"][0]["band"] != row["views"][0]["band"]:
            row["flags"].append(f"带位迁移: {prev_t['views'][0]['band_label']}→{row['views'][0]['band_label']}")
        if row["views"][0].get("p_below_vm") and t["archetype"] in ("A", "B", "E"):
            row["flags"].append("P<V_M下沿参考·复核结构事实(非买入线)")
        targets.append(row)
    base = params["_meta"]["baseline_prices_20260601"]
    cn5 = ["301308.SZ", "688008.SH", "002409.SZ", "603986.SH", "688123.SH"]
    basket = round(sum(quotes.get(c, prev_q.get(c, base[c])) / base[c] - 1 for c in cn5) / 5 * 100, 1)
    SOX_BASE_0602 = 13726.27
    if sox and sox.get("value"):
        sox["chg_vs_0602"] = round(sox["value"] / SOX_BASE_0602 - 1, 4)
    sw_pct = sw_percentile(sw["pe"]) if sw else None
    out = {
        "generated_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "degraded": degraded,
        "temperature": {
            "sw_pe": sw and round(sw["pe"], 2), "sw_pb": (round(sw["pb"], 2) if sw and sw.get("pb") else None),
            "sw_asof": sw and sw.get("asof"), "sw_pe_percentile_since2020": sw_pct,
            "sw_spark": sw_spark(), "sw_anchor": {"peak2021": 106.5, "median": 82.1, "base_0601": 110.45},
            "sox": sox, "basket_vs_0601_pct": basket,
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
    print(f"ok: {len(targets)} targets, degraded={len(degraded)}, sw_pctile={sw_pct}")

if __name__ == "__main__":
    main()

