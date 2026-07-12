#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""两步走第二步 · 干净估值引擎（dashboard pipeline 版）
源自 two_step_stock_eval_engine.py v2（零方法改动，仅改为返回结构化数据）。
硬边界：仅输出描述性情景内在价值参考与位置带，无目标价/仓位/买卖建议。
"""

PARAMS = {"C": (0.120, 0.01), "M": (0.100, 0.03), "S": (0.085, 0.05), "STRESS": (0.115, 0.04)}
R_MID, FADE, G_S = 0.10, 5, 0.03

def eps_path(E0, Em, N_S, tier, N=10, fade_years=5, g_S=0.03, hc=1.0):
    path = []
    for t in range(1, N + 1):
        if t <= N_S:
            e = E0 * (hc if tier in ("C", "M") else 1.0)
        else:
            k = t - N_S
            if tier == "C":
                e = E0 + (Em - E0) * min(k / fade_years, 1.0)
            elif tier == "M":
                target = (E0 + Em) / 2.0
                e = E0 + (target - E0) * min(k / fade_years, 1.0)
            else:  # S
                e = E0 * ((1 + g_S) ** k)
        path.append(e)
    return path

def dcf(E0, Em, N_S, tier, r, g_term, N=10, fade_years=5, g_S=0.03, hc=1.0):
    path = eps_path(E0, Em, N_S, tier, N, fade_years, g_S, hc)
    pv = sum(e / (1 + r) ** (t + 1) for t, e in enumerate(path))
    pv += (path[-1] * (1 + g_term) / (r - g_term)) / (1 + r) ** N
    return pv

def implied_Nstar(P, E0, Em, r_mid=R_MID, g_low=0.01, Nmax=40):
    """返回 (数值|None, 标注)"""
    if E0 / Em < 1.3:
        return None, "N/A(ill-conditioned)"
    cap = E0 / r_mid
    if P > cap:
        return None, "无界(市场把峰值当近永续)"
    term = Em * (1 + g_low) / (r_mid - g_low)
    best, bestdiff, N = 0.0, 1e18, 0.0
    while N <= Nmax:
        pv = sum(E0 / (1 + r_mid) ** t for t in range(1, int(N) + 1)) + term / (1 + r_mid) ** N
        if abs(pv - P) < bestdiff:
            best, bestdiff = N, abs(pv - P)
        N += 0.1
    return round(best, 1), f"{round(best,1)}y"

def cyclical_values(E0, Em, N_S, g_S=G_S, gterm_S=None, hc=1.0):
    gt = {t: PARAMS[t][1] for t in PARAMS}
    if gterm_S is not None:
        gt["S"] = gterm_S
    return {t: dcf(E0, Em, N_S, (t if t != "STRESS" else "S"), PARAMS[t][0], gt[t],
                   fade_years=FADE, g_S=g_S, hc=(hc if t in ("C", "M") else 1.0)) for t in PARAMS}

def growth_path(E0, g1, N1, g_term, N=10):
    path, e = [], E0
    for t in range(1, N + 1):
        g = g1 if t <= N1 else g1 + (g_term - g1) * ((t - N1) / (N - N1))
        e = e * (1 + g)
        path.append(e)
    return path

def growth_dcf(E0, g1, N1, g_term, r, N=10):
    path = growth_path(E0, g1, N1, g_term, N)
    pv = sum(e / (1 + r) ** (t + 1) for t, e in enumerate(path))
    return pv + (path[-1] * (1 + g_term) / (r - g_term)) / (1 + r) ** N

def implied_g1(P, E0, N1=5, g_term=0.04, r=0.095, N=10):
    lo, hi = -0.30, 0.80
    for _ in range(100):
        mid = (lo + hi) / 2
        if growth_dcf(E0, mid, N1, g_term, r, N) < P:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2, 3)

def growth_values(E0, gC, gM, gS, g_term=0.04, r=0.095, N1=5):
    return {"C": growth_dcf(E0, gC, N1, g_term, r),
            "M": growth_dcf(E0, gM, N1, g_term, r),
            "S": growth_dcf(E0, gS, N1, g_term, r)}

BAND_CODES = ["deep_under", "under", "in_range", "over", "extreme_over"]
BAND_LABELS = {"deep_under": "深度低估区", "under": "低估区", "in_range": "区间内（看持久性）",
               "over": "高估区", "extreme_over": "极端高估区"}

def band(P, V_bear, V_bull):
    if P < 0.5 * V_bear: return "deep_under"
    if P < V_bear: return "under"
    if P <= V_bull: return "in_range"
    if P <= 2 * V_bull: return "over"
    return "extreme_over"

# 行业态 × 价格位置 → 研究状态标签（A2 映射，描述性、非交易指令）
A2_MAP = {
    "上行强化": {"below_vc": "强正向验证", "mid": "正向偏离候选", "above_vs": "风险预警"},
    "顶部钝化": {"below_vc": "正向偏离候选", "mid": "中性观察", "above_vs": "风险预警"},
    "下行确认": {"below_vc": "触底观察（非确认）", "mid": "中性偏risk", "above_vs": "强风险预警"},
    "触底再起": {"below_vc": "强正向验证", "mid": "正向偏离候选", "above_vs": "中性"},
}

def a2_label(industry_state, P, V_C, V_S):
    zone = "below_vc" if P < V_C else ("above_vs" if P >= V_S else "mid")
    return A2_MAP.get(industry_state, A2_MAP["顶部钝化"])[zone]

