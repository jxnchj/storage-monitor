#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动层回归测试（不联网）。运行：python3 -m unittest discover -s pipeline"""
import datetime as dt
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine
import fetch_data as fd

UTC = dt.timezone.utc
NY = ZoneInfo("America/New_York")
SEOUL = ZoneInfo("Asia/Seoul")
D = dt.date


def old_pv(N, E0, term, r=0.10):
    """修复前的写法：年金整年、折现小数年。"""
    return sum(E0 / (1 + r) ** t for t in range(1, int(N) + 1)) + term / (1 + r) ** N


class ImpliedNstar(unittest.TestCase):
    E0, Em = 281000, 26000  # SK Hynix，M9-8 的锯齿样本

    def test_old_formula_was_sawtooth(self):
        term = self.Em * 1.01 / 0.09
        self.assertGreater(old_pv(8.0, self.E0, term), old_pv(8.9, self.E0, term))
        self.assertGreater(old_pv(9.0, self.E0, term), old_pv(8.9, self.E0, term))

    def test_monotonic_in_price(self):
        cap = self.E0 / engine.R_MID
        prev = -1.0
        for i in range(1, 400):
            n, _ = engine.implied_Nstar(cap * i / 400, self.E0, self.Em)
            self.assertGreaterEqual(n, prev)
            prev = n

    def test_m9_8_price_up_nstar_not_down(self):
        lo, _ = engine.implied_Nstar(1593000, self.E0, self.Em)
        hi, _ = engine.implied_Nstar(1645000, self.E0, self.Em)
        self.assertGreater(hi, lo)

    def test_integer_years_match_old_formula(self):
        term = self.Em * 1.01 / 0.09
        for N in range(0, 21):
            new = (self.E0 / 0.10) * (1 - 1.1 ** -N) + term / 1.1 ** N
            self.assertAlmostEqual(new, old_pv(N, self.E0, term), places=6)

    def test_guards_unchanged(self):
        self.assertEqual(engine.implied_Nstar(100, 10, 9)[1], "N/A(ill-conditioned)")
        self.assertIsNone(engine.implied_Nstar(999999, 100, 10)[0])


class LastClosed(unittest.TestCase):
    BARS_US = [(D(2026, 11, 4), 795.23), (D(2026, 11, 5), 656.39), (D(2026, 11, 6), 682.14)]

    def test_us_in_session_drops_live_bar(self):
        now = dt.datetime(2026, 11, 6, 18, 11, tzinfo=UTC)  # 13:11 ET
        trade = (682.78, dt.datetime(2026, 11, 6, 13, 11, tzinfo=NY))
        self.assertEqual(fd.last_closed(self.BARS_US, trade, "US", now), (656.39, D(2026, 11, 5), "bar"))

    def test_us_before_settle_drops_bar(self):
        now = dt.datetime(2026, 11, 6, 21, 10, tzinfo=UTC)  # 16:10 ET，收盘后未过结算时刻
        self.assertEqual(fd.last_closed(self.BARS_US, None, "US", now)[1], D(2026, 11, 5))

    def test_us_after_settle_keeps_bar(self):
        now = dt.datetime(2026, 11, 7, 1, 40, tzinfo=UTC)  # 20:40 ET
        self.assertEqual(fd.last_closed(self.BARS_US, None, "US", now), (682.14, D(2026, 11, 6), "bar"))

    def test_us_post_close_nan_bar_uses_last_trade(self):
        # 11-06 01:40 UTC 的实况：11-05 收盘已过约 4.7 小时，K 线空，旧代码读成 11-04
        bars = [(D(2026, 11, 4), 795.23), (D(2026, 11, 5), None)]
        now = dt.datetime(2026, 11, 6, 1, 40, tzinfo=UTC)
        trade = (656.39, dt.datetime(2026, 11, 5, 16, 0, 2, tzinfo=NY))
        self.assertEqual(fd.last_closed(bars, trade, "US", now), (656.39, D(2026, 11, 5), "last_trade"))

    def test_kr_post_close_nan_bar_uses_last_trade(self):
        # 18:11 UTC 实测：KRX 已收盘 11.7 小时，当日 K 线仍空
        bars = [(D(2026, 11, 5), 1264000.0), (D(2026, 11, 6), None)]
        now = dt.datetime(2026, 11, 6, 18, 11, tzinfo=UTC)
        trade = (1132000.0, dt.datetime(2026, 11, 6, 15, 30, 5, tzinfo=SEOUL))
        self.assertEqual(fd.last_closed(bars, trade, "KR", now), (1132000.0, D(2026, 11, 6), "last_trade"))

    def test_kr_in_session_ignores_bar_and_last_trade(self):
        bars = [(D(2026, 11, 5), 1264000.0), (D(2026, 11, 6), 1138000.0)]
        now = dt.datetime(2026, 11, 6, 1, 40, tzinfo=UTC)  # 10:40 KST
        trade = (1138000.0, dt.datetime(2026, 11, 6, 10, 40, tzinfo=SEOUL))
        self.assertEqual(fd.last_closed(bars, trade, "KR", now), (1264000.0, D(2026, 11, 5), "bar"))

    def test_kr_late_close_day_not_treated_as_closed(self):
        # 高考日收盘延后到 16:30：16:00 KST 的成交晚于 15:30，但结算时刻未到
        bars = [(D(2026, 11, 11), 1000.0), (D(2026, 11, 12), 1010.0)]
        now = dt.datetime(2026, 11, 12, 7, 0, tzinfo=UTC)  # 16:00 KST
        trade = (1010.0, dt.datetime(2026, 11, 12, 16, 0, tzinfo=SEOUL))
        self.assertEqual(fd.last_closed(bars, trade, "KR", now), (1000.0, D(2026, 11, 11), "bar"))

    def test_last_trade_before_close_is_rejected(self):
        # 美股半日市 13:00 收盘：成交时间早于 16:00，不补，as-of 如实滞后
        bars = [(D(2026, 11, 25), 100.0), (D(2026, 11, 27), None)]
        now = dt.datetime(2026, 11, 28, 1, 0, tzinfo=UTC)
        trade = (101.0, dt.datetime(2026, 11, 27, 13, 0, tzinfo=NY))
        self.assertEqual(fd.last_closed(bars, trade, "US", now), (100.0, D(2026, 11, 25), "bar"))

    def test_weekend_uses_friday(self):
        bars = [(D(2026, 11, 5), 1264000.0), (D(2026, 11, 6), None)]
        now = dt.datetime(2026, 11, 7, 3, 0, tzinfo=UTC)  # 周六
        trade = (1132000.0, dt.datetime(2026, 11, 6, 15, 30, 5, tzinfo=SEOUL))
        self.assertEqual(fd.last_closed(bars, trade, "KR", now)[1], D(2026, 11, 6))

    def test_no_data(self):
        now = dt.datetime(2026, 11, 7, 3, 0, tzinfo=UTC)
        self.assertIsNone(fd.last_closed([], None, "US", now)[0])


class FakePro:
    """daily、daily_basic 都只认单代码：逗号多代码返回 0 行（复现线上：第三腿从未写入、11-06 的 daily 全部失败）。"""
    ROWS = {"301308.SZ": (6.5672, 3.1666), "688008.SH": (47.704, 8.2709), "002409.SZ": (39.2773, 4.795),
            "603986.SH": (14.4959, 2.1944), "688123.SH": (float("nan"), 4.8752)}

    def __init__(self, fail=()):
        self.fail = set(fail)
        self.calls = []
        self.daily_calls = []

    def daily(self, ts_code=None, **kw):
        self.daily_calls.append((ts_code, kw))
        if "daily" in self.fail or f"daily:{ts_code}" in self.fail:
            raise RuntimeError("daily down")
        if "," in ts_code or ts_code not in self.ROWS:
            return pd.DataFrame(columns=["ts_code", "trade_date", "close"])
        close = 100.0 + list(self.ROWS).index(ts_code)
        return pd.DataFrame([{"ts_code": ts_code, "trade_date": "20261106", "close": close},
                             {"ts_code": ts_code, "trade_date": "20261105", "close": 1.0}])

    def daily_basic(self, ts_code=None, **kw):
        self.calls.append((ts_code, kw))
        if "daily_basic" in self.fail:
            raise RuntimeError("daily_basic down")
        if "," in ts_code or ts_code not in self.ROWS:
            return pd.DataFrame()
        pe, pb = self.ROWS[ts_code]
        return pd.DataFrame([{"ts_code": ts_code, "trade_date": "20261105", "pe_ttm": 1.0, "pb": 1.0},
                             {"ts_code": ts_code, "trade_date": "20261106", "pe_ttm": pe, "pb": pb}])

    def sw_daily(self, **kw):
        if "sw_daily" in self.fail:
            raise RuntimeError("sw down")
        return pd.DataFrame([{"trade_date": "20261106", "pe": 80.0, "pb": 5.0}])


class ThirdLeg(unittest.TestCase):
    CN = list(FakePro.ROWS)
    NOW = dt.datetime(2026, 11, 6, 18, 0, tzinfo=UTC)

    def test_old_call_shape_finds_nothing(self):
        db = FakePro().daily_basic(ts_code=",".join(self.CN))
        self.assertTrue(db.empty)

    def test_per_code_with_dates(self):
        pro, degraded = FakePro(), []
        third = fd.fetch_third_leg(pro, self.CN, self.NOW, degraded)
        self.assertEqual(sorted(third), sorted(self.CN))
        self.assertEqual(third["301308.SZ"], {"pe_ttm": 6.57, "pb": 3.17, "asof": "20261106"})
        self.assertIsNone(third["688123.SH"]["pe_ttm"])  # 亏损/空值不写 NaN
        self.assertEqual(degraded, [])
        for code, kw in pro.calls:
            self.assertNotIn(",", code)
            self.assertEqual(kw["end_date"], "20261107")  # 北京时间已到 11-07
        json.dumps(third, allow_nan=False)

    def test_failure_reaches_degraded(self):
        degraded = []
        third = fd.fetch_third_leg(FakePro(fail=["daily_basic"]), self.CN, self.NOW, degraded)
        self.assertEqual(third, {})
        self.assertEqual({d["src"] for d in degraded}, {"tushare.daily_basic"})
        self.assertEqual([d["ticker"] for d in degraded], self.CN)

    def test_empty_reaches_degraded(self):
        degraded = []
        fd.fetch_third_leg(FakePro(), ["000000.SZ"], self.NOW, degraded)
        self.assertEqual(degraded[0]["ticker"], "000000.SZ")


class CnQuotes(unittest.TestCase):
    CN = list(FakePro.ROWS)
    NOW = dt.datetime(2026, 11, 6, 23, 38, tzinfo=dt.timezone.utc)

    def test_comma_multi_code_returns_no_rows(self):
        df = FakePro().daily(ts_code=",".join(self.CN))
        self.assertTrue(df.empty)
        self.assertIn("ts_code", df.columns)

    def test_each_code_fetched_alone_with_beijing_window(self):
        pro, degraded = FakePro(), []
        fd.fetch_cn_quotes(pro, self.CN, self.NOW, degraded)
        self.assertEqual([c for c, _ in pro.daily_calls], self.CN)
        for _, kw in pro.daily_calls:
            self.assertEqual((kw["start_date"], kw["end_date"]), ("20261024", "20261107"))
        self.assertEqual(degraded, [])

    def test_takes_latest_trade_date_from_descending_rows(self):
        quotes, asof, src = fd.fetch_cn_quotes(FakePro(), self.CN, self.NOW, [])
        self.assertEqual(quotes["301308.SZ"], 100.0)
        self.assertEqual(quotes["688123.SH"], 104.0)
        self.assertEqual(asof["301308.SZ"], "20261106")
        self.assertEqual(src["301308.SZ"], "bar")
        self.assertEqual(set(quotes), set(self.CN))

    def test_one_code_failure_keeps_the_rest(self):
        degraded = []
        quotes, *_ = fd.fetch_cn_quotes(FakePro(fail=["daily:688008.SH"]), self.CN, self.NOW, degraded)
        self.assertNotIn("688008.SH", quotes)
        self.assertEqual(len(quotes), 4)
        self.assertEqual([(d["src"], d["ticker"]) for d in degraded], [("tushare.daily", "688008.SH")])

    def test_no_rows_is_degraded(self):
        degraded = []
        quotes, *_ = fd.fetch_cn_quotes(FakePro(), ["000000.SZ"], self.NOW, degraded)
        self.assertEqual(quotes, {})
        self.assertEqual(degraded[0]["ticker"], "000000.SZ")
        self.assertIn("无数据", degraded[0]["error"])

    def test_nan_close_is_degraded(self):
        pro = FakePro()
        pro.daily = lambda **kw: pd.DataFrame([{"ts_code": "301308.SZ", "trade_date": "20261106", "close": float("nan")}])
        degraded = []
        quotes, *_ = fd.fetch_cn_quotes(pro, ["301308.SZ"], self.NOW, degraded)
        self.assertEqual(quotes, {})
        self.assertIn("close", degraded[0]["error"])


class FetchLive(unittest.TestCase):
    PARAMS = {"targets": [{"src": {"quote": "yf", "ticker": "MU"}},
                          {"src": {"quote": "yf", "ticker": "000660.KS"}},
                          {"src": {"quote": "ts", "ticker": "301308.SZ"}},
                          {"src": {"quote": "ts", "ticker": "688008.SH"}}]}

    def fake_yf(self, broken=()):
        def Ticker(tk):
            if tk in broken:
                raise RuntimeError(f"{tk} boom")
            obj = types.SimpleNamespace()
            obj.history = lambda **kw: pd.DataFrame(
                {"Close": [10.0, 11.0]},
                index=pd.DatetimeIndex(["2020-01-02", "2020-01-03"]).tz_localize("America/New_York"))
            obj.history_metadata = {}
            return obj
        return types.SimpleNamespace(Ticker=Ticker)

    def run_live(self, yf=None, pro=None, token="x"):
        ts_mod = types.SimpleNamespace(pro_api=lambda t: pro or FakePro())
        degraded = []
        env = {"TUSHARE_TOKEN": token} if token else {}
        with mock.patch.dict(sys.modules, {"yfinance": yf or self.fake_yf(), "tushare": ts_mod}), \
                mock.patch.dict(os.environ, env, clear=not token), \
                mock.patch.object(fd, "sw_append_history"):
            out = fd.fetch_live(self.PARAMS, degraded)
        return out, degraded

    def test_happy_path(self):
        (quotes, third, sox, sw, asof, src), degraded = self.run_live()
        self.assertEqual(degraded, [])
        self.assertEqual(quotes["MU"], 11.0)
        self.assertEqual(asof["MU"], "2020-01-03")
        self.assertEqual(src["MU"], "bar")
        self.assertEqual(sox["value"], 11.0)
        self.assertIn("301308.SZ", third)
        self.assertEqual(sw["pe"], 80.0)
        self.assertEqual((quotes["301308.SZ"], quotes["688008.SH"]), (100.0, 101.0))
        self.assertEqual((asof["301308.SZ"], src["688008.SH"]), ("20261106", "bar"))

    def test_one_ticker_failure_keeps_the_rest(self):
        (quotes, *_), degraded = self.run_live(yf=self.fake_yf(broken=["MU"]))
        self.assertNotIn("MU", quotes)
        self.assertIn("000660.KS", quotes)
        self.assertEqual([(d["src"], d["ticker"]) for d in degraded], [("yfinance", "MU")])

    def test_tushare_sources_fail_independently(self):
        (quotes, third, _, sw, *_), degraded = self.run_live(pro=FakePro(fail=["daily"]))
        self.assertNotIn("301308.SZ", quotes)
        self.assertIn("301308.SZ", third)
        self.assertEqual(sw["pe"], 80.0)
        self.assertEqual([(d["src"], d["ticker"]) for d in degraded],
                         [("tushare.daily", "301308.SZ"), ("tushare.daily", "688008.SH")])

    def test_missing_token_is_degraded(self):
        (quotes, third, _, sw, *_), degraded = self.run_live(token=None)
        self.assertIsNone(sw)
        self.assertEqual(third, {})
        self.assertEqual([d["src"] for d in degraded], ["tushare"])


class MainDegradation(unittest.TestCase):
    """用 SEED 走完 main：缺值时旧值不进聚合字段，且能被 --check-degraded 看到。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = os.path.join(self.tmp.name, "auto")
        os.makedirs(os.path.join(self.data, "history"))
        patcher = mock.patch.object(fd, "DATA", self.data)
        patcher.start()
        self.addCleanup(patcher.stop)
        with open(os.path.join(fd.HERE, "seed_20260709.json"), encoding="utf-8") as f:
            self.seed = json.load(f)

    def run_main(self, seed):
        path = os.path.join(self.tmp.name, "seed.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(seed, f)
        with mock.patch.dict(os.environ, {"SEED_FILE": path}):
            fd.main()
        with open(os.path.join(self.data, "latest.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_clean_run(self):
        out = self.run_main(self.seed)
        self.assertEqual(out["degraded"], [])
        self.assertEqual(out["stale_tickers"], [])
        self.assertIsNotNone(out["temperature"]["basket_vs_0601_pct"])
        rows = {t["ticker"]: t for t in out["targets"]}
        self.assertEqual(rows["MU"]["asof"], "2026-07-08")
        self.assertEqual(rows["000660.KS"]["asof"], "2026-07-09")
        self.assertEqual(rows["301308.SZ"]["third_leg"]["pe_ttm"], 48.24)
        self.assertEqual(fd.check_degraded(), 0)

    def test_failed_source_marks_stale_and_nulls_basket(self):
        self.run_main(self.seed)
        partial = json.loads(json.dumps(self.seed))
        del partial["quotes"]["301308.SZ"]
        partial["third_leg"].pop("301308.SZ")
        out = self.run_main(partial)
        rows = {t["ticker"]: t for t in out["targets"]}
        row = rows["301308.SZ"]
        self.assertTrue(row["stale"])
        self.assertEqual(row["P"], self.seed["quotes"]["301308.SZ"])
        self.assertEqual(row["asof"], "2026-07-09")  # 保留上期 as-of，不再写空
        self.assertTrue(row["flags"][0].startswith("取数失败"))
        self.assertNotIn("third_leg", row)
        self.assertEqual(out["stale_tickers"], ["301308.SZ"])
        self.assertIsNone(out["temperature"]["basket_vs_0601_pct"])
        self.assertEqual(out["temperature"]["basket_missing"], ["301308.SZ"])
        self.assertEqual(fd.check_degraded(), 1)

    def test_check_degraded_flags_source_failure(self):
        path = os.path.join(self.tmp.name, "d.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"degraded": [{"src": "tushare", "error": "x"}], "stale_tickers": []}, f)
        self.assertEqual(fd.check_degraded(path), 1)
        self.assertEqual(fd.check_degraded(os.path.join(self.tmp.name, "missing.json")), 1)


class ConsLabel(unittest.TestCase):
    def test_label_follows_values(self):
        self.assertEqual(fd.cons_view_label(149.64, 73.32), "保守口径")
        self.assertEqual(fd.cons_view_label(24.0, 39.3), "并列口径")   # 江波龙
        self.assertEqual(fd.cons_view_label(20.0, 30.56), "并列口径")  # 线上 params 的江波龙
        self.assertEqual(fd.cons_view_label(10.0, 10.0), "并列口径")

    def test_jbl_view(self):
        t = {"id": "JBL", "name": "江波龙", "archetype": "E", "currency": "CNY", "model": "cyclical",
             "src": {"quote": "ts", "ticker": "301308.SZ"}, "E0": {"value": 24.0, "as_of": "2026-09-08"},
             "E0_cons": {"value": 39.3, "note": "n"}, "Em": 3.5, "N_S": 2}
        views = fd.compute_target(t, 214.77, "顶部钝化")["views"]
        self.assertEqual([v["view"] for v in views], ["主口径", "并列口径"])


class Blacklist(unittest.TestCase):
    def test_not_relaxed(self):
        self.assertEqual(fd.BLACKLIST, ["目标价", "建仓", "加仓", "减仓", "买入", "卖出", "持有建议", "做空", "止损", "仓位"])
        with self.assertRaises(SystemExit):
            fd.blacklist_check({"x": "买入"})
        fd.blacklist_check({"x": "非买入线"})
        fd.blacklist_check({"flags": ["取数失败·沿用上期值(as-of 2026-11-05)"]})


if __name__ == "__main__":
    unittest.main()
