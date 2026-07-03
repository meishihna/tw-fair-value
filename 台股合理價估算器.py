#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
台股合理價估算器  ——  歷史倍數回歸法
====================================================
用法：
    python3 台股合理價估算器.py
執行後會自動開啟瀏覽器介面。輸入任何台股代碼（例如 2330、2368、2337），
程式會自動抓取該股近 5 年的本益比 / 股價淨值比，用「回歸歷史典型倍數」
推估合理價 —— 你不需要輸入任何財報數字。

資料來源：FinMind Open Data（https://finmind.github.io/）
本工具為分析輔助，非投資建議。
"""
import json, statistics, webbrowser, threading, datetime, sys
import urllib.request, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FINMIND = "https://api.finmindtrade.com/api/v4/data"


# ---------------------------------------------------------------- 資料抓取
def fetch_finmind(dataset, data_id, start_date, end_date, token=""):
    params = {"dataset": dataset, "data_id": data_id,
              "start_date": start_date, "end_date": end_date}
    if token:
        params["token"] = token
    url = FINMIND + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "tw-fairprice/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------------------------------------------------------------- 估值計算（純函式，方便測試）
def _pct_le(series, value):
    if not series:
        return None
    return round(100.0 * sum(1 for x in series if x <= value) / len(series), 1)

def _percentile(series, q):
    if not series:
        return None
    s = sorted(series)
    k = (len(s) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)

def compute_valuation(per_rows, price_rows, basis="median"):
    """吃 FinMind 原始資料列，回傳估值結果 dict。basis: 'median' 中位 或 'mean' 平均。"""
    typ_fn = statistics.fmean if basis == "mean" else statistics.median
    per_series = [r["PER"] for r in per_rows if isinstance(r.get("PER"), (int, float)) and r["PER"] > 0]
    pbr_series = [r["PBR"] for r in per_rows if isinstance(r.get("PBR"), (int, float)) and r["PBR"] > 0]

    # 目前值：取最後一筆有效資料
    cur_per = next((r["PER"] for r in reversed(per_rows) if isinstance(r.get("PER"), (int, float)) and r["PER"] > 0), None)
    cur_pbr = next((r["PBR"] for r in reversed(per_rows) if isinstance(r.get("PBR"), (int, float)) and r["PBR"] > 0), None)
    yld     = next((r.get("dividend_yield") for r in reversed(per_rows) if r.get("dividend_yield") is not None), None)
    per_date = per_rows[-1]["date"] if per_rows else None

    # 目前股價：最後一筆有收盤價
    close = None; price_date = None
    for r in reversed(price_rows):
        if isinstance(r.get("close"), (int, float)) and r["close"] > 0:
            close = r["close"]; price_date = r["date"]; break

    have_pe = len(per_series) >= 20 and bool(cur_per)
    have_pb = len(pbr_series) >= 20 and bool(cur_pbr)
    if close is None or not (have_pe or have_pb):
        return {"ok": False,
                "error": "資料不足或查無此代碼。請確認是上市／上櫃股票代碼（例如 2330），"
                         "興櫃或剛上市、資料過短的個股可能無法估算。"}

    med_per = statistics.median(per_series) if per_series else None
    med_pbr = statistics.median(pbr_series) if pbr_series else None
    avg_per = round(statistics.fmean(per_series), 2) if per_series else None
    avg_pbr = round(statistics.fmean(pbr_series), 2) if pbr_series else None
    # 「典型倍數」依 basis 取中位或平均；估值與 gauge 中心線都用它
    typ_per = typ_fn(per_series) if per_series else None
    typ_pbr = typ_fn(pbr_series) if pbr_series else None

    eps  = close / cur_per if cur_per else None
    bvps = close / cur_pbr if cur_pbr else None

    fair_pe = typ_per * eps if (have_pe and eps) else None
    fair_pb = typ_pbr * bvps if (have_pb and bvps) else None

    # 第三法：殖利率回歸（存股/高息股觀點）。合理價 = 現價 × 目前殖利率 ÷ 歷史中位殖利率。
    # 殖利率是估值的倒數：目前殖利率高於中位＝相對便宜，故比率用「目前÷中位」（與 P/E、P/B 相反）。
    # 用近 20 日殖利率中位當「目前值」以平滑雜訊；無配息或剛停息者（近 20 日多數為 0）自動不採。
    yld_series = [r["dividend_yield"] for r in per_rows
                  if isinstance(r.get("dividend_yield"), (int, float)) and r["dividend_yield"] > 0]
    recent_ylds = [r["dividend_yield"] for r in per_rows[-20:]
                   if isinstance(r.get("dividend_yield"), (int, float)) and r["dividend_yield"] > 0]
    cur_yld = statistics.median(recent_ylds) if recent_ylds else None
    med_yld = statistics.median(yld_series) if yld_series else None
    typ_yld = typ_fn(yld_series) if yld_series else None
    have_div = len(yld_series) >= 20 and cur_yld is not None and len(recent_ylds) >= 5
    fair_div = close * cur_yld / typ_yld if (have_div and typ_yld) else None

    # 近期虧損判定：FinMind 對虧損股回傳 PER=0。看最近約 40 個交易日，若過半 PER 無效，
    # 視為目前虧損；此時 P/E 法以獲利年代倍數硬套當前價，參考性低 → 綜合價僅採股價淨值比法。
    # （用近期占比而非「最後有效 PER 距今天數」，才不會被虧損期間偶發一天的正 PER 騙過。）
    pe_stale = False
    recent = per_rows[-40:]
    if len(recent) >= 10:
        recent_valid = sum(1 for r in recent
                           if isinstance(r.get("PER"), (int, float)) and r["PER"] > 0)
        pe_stale = recent_valid < len(recent) * 0.5

    use_pe = (fair_pe is not None) and not pe_stale
    fairs = [x for x in ((fair_pe if use_pe else None), fair_pb, fair_div) if x is not None]
    if not fairs and fair_pe is not None:      # 極端：只剩(陳舊)P/E 可用時仍給值，不再標記排除
        fairs, use_pe, pe_stale = [fair_pe], True, False
    estimate = sum(fairs) / len(fairs) if fairs else None
    upside = (estimate / close - 1) if (estimate and close) else None

    # 綜合價由哪幾法平均而來（供前端標示）
    used = (["本益比"] if use_pe else []) \
         + (["股價淨值比"] if fair_pb is not None else []) \
         + (["殖利率"] if fair_div is not None else [])
    _num = {2: "兩", 3: "三"}
    if len(used) >= 2:
        method_label = f"{_num.get(len(used), len(used))}法平均：" + "、".join(used)
    elif len(used) == 1:
        method_label = f"僅{used[0]}法"
    else:
        method_label = "—"

    # 判讀
    verdict, tone = "接近合理", "fair"
    if upside is not None:
        if   upside >=  0.15: verdict, tone = "明顯偏低", "cheap"
        elif upside >=  0.05: verdict, tone = "略偏低",   "cheap"
        elif upside <= -0.15: verdict, tone = "明顯偏貴", "rich"
        elif upside <= -0.05: verdict, tone = "略偏貴",   "rich"

    def band(series, cur, med):
        if not series:
            return None
        return {"lo": round(_percentile(series, 0.10), 2),
                "hi": round(_percentile(series, 0.90), 2),
                "min": round(min(series), 2), "max": round(max(series), 2),
                "cur": round(cur, 2) if cur else None,
                "med": round(med, 2) if med else None,
                "pct": _pct_le(series, cur) if cur else None}

    # 本益比河流圖：對齊每日 PER 與收盤價，得隱含 EPS_t = 收盤_t / PER_t；
    # 各倍數帶(t) = 該百分位PER × EPS_t，價格線觸及某帶即代表當時 PER 等於該百分位。
    chart = None
    if have_pe:
        price_by_date = {r["date"]: r["close"] for r in price_rows
                         if isinstance(r.get("close"), (int, float)) and r["close"] > 0}
        pts = []
        for r in per_rows:
            p, d = r.get("PER"), r.get("date")
            if isinstance(p, (int, float)) and p > 0 and d in price_by_date:
                c = price_by_date[d]
                pts.append((d, round(c, 2), round(c / p, 4)))
        if len(pts) >= 20:
            step = max(1, len(pts) // 180)          # 降採樣至 ~180 點，維持前端流暢
            sampled = pts[::step]
            if sampled[-1] != pts[-1]:
                sampled.append(pts[-1])
            chart = {
                "dates": [x[0] for x in sampled],
                "close": [x[1] for x in sampled],
                "eps":   [x[2] for x in sampled],
                "pcts": {q: round(_percentile(per_series, v), 2)
                         for q, v in (("p10", .10), ("p25", .25), ("p50", .50),
                                      ("p75", .75), ("p90", .90))},
            }

    return {
        "ok": True,
        "chart": chart,
        "close": round(close, 2), "price_date": price_date, "per_date": per_date,
        "cur_per": round(cur_per, 2) if cur_per else None,
        "cur_pbr": round(cur_pbr, 2) if cur_pbr else None,
        "med_per": round(med_per, 2) if med_per else None,
        "med_pbr": round(med_pbr, 2) if med_pbr else None,
        "avg_per": avg_per, "avg_pbr": avg_pbr,
        "typ_per": round(typ_per, 2) if typ_per else None,
        "typ_pbr": round(typ_pbr, 2) if typ_pbr else None,
        "typ_yld": round(typ_yld, 2) if typ_yld else None,
        "basis": basis,
        "yield": round(yld, 2) if isinstance(yld, (int, float)) else None,
        "eps": round(eps, 2) if eps else None,
        "bvps": round(bvps, 2) if bvps else None,
        "fair_pe": round(fair_pe, 1) if fair_pe else None,
        "fair_pb": round(fair_pb, 1) if fair_pb else None,
        "fair_div": round(fair_div, 1) if fair_div else None,
        "med_yld": round(med_yld, 2) if med_yld else None,
        "cur_yld": round(cur_yld, 2) if cur_yld else None,
        "method_label": method_label,
        "estimate": round(estimate, 1) if estimate else None,
        "upside": round(upside, 4) if upside is not None else None,
        "verdict": verdict, "tone": tone,
        "pe_stale": pe_stale, "use_pe": use_pe,
        "years": round(len(per_series) / 250, 1),
        "n": len(per_series),
        "per_band": band(per_series, cur_per, typ_per) if have_pe else None,
        "pbr_band": band(pbr_series, cur_pbr, typ_pbr) if have_pb else None,
        "div_band": band(yld_series, cur_yld, typ_yld) if have_div else None,
    }


def estimate_ticker(ticker, token="", years=5, basis="median"):
    ticker = ticker.strip()
    if not ticker.isdigit():
        return {"ok": False, "error": "請輸入數字代碼（例如 2330）。"}
    years = years if years in (3, 5, 10) else 5
    basis = basis if basis in ("median", "mean") else "median"
    today = datetime.date.today()
    start = (today - datetime.timedelta(days=365 * years + 30)).isoformat()
    end = today.isoformat()
    try:
        per = fetch_finmind("TaiwanStockPER", ticker, start, end, token)
    except Exception as e:
        return {"ok": False, "error": f"連線資料來源失敗：{e}。請確認你目前有連上網路。"}
    if per.get("status") not in (200, None) and not per.get("data"):
        msg = per.get("msg", "")
        if "reach the upper limit" in msg or per.get("status") == 402:
            return {"ok": False, "error": "已達 FinMind 免費流量上限（每小時 300 次）。"
                                          "稍後再試,或在下方填入免費 Token 以提高上限。"}
        return {"ok": False, "error": f"資料來源回應異常：{msg or per.get('status')}"}
    per_rows = per.get("data", [])
    if not per_rows:
        return {"ok": False, "error": "查無此代碼的資料。請確認是上市／上櫃股票代碼。"}
    try:
        # 抓完整區間收盤價：既取得最新價，也供本益比河流圖對齊歷史 PER
        price = fetch_finmind("TaiwanStockPrice", ticker, start, end, token)
    except Exception as e:
        return {"ok": False, "error": f"連線資料來源失敗：{e}"}
    result = compute_valuation(per_rows, price.get("data", []), basis)
    result["ticker"] = ticker
    result["years_req"] = years
    return result


# ---------------------------------------------------------------- 前端頁面
PAGE = r"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>台股合理價估算</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{
    --paper:#e9edec; --card:#fbfcfb; --ink:#16242b; --ink-soft:#5c6d74;
    --line:#d5dbdb; --line-strong:#b9c2c2; --up:#c8362f; --down:#127a4f;
    --brass:#a97c1e; --brass-soft:#f0e6cf;
    --mono:'IBM Plex Mono',ui-monospace,SFMono-Regular,Menlo,monospace;
    --sans:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang TC","Microsoft JhengHei","Noto Sans TC",sans-serif;
  }
  *{box-sizing:border-box}
  html,body{margin:0}
  body{
    background:var(--paper); color:var(--ink); font-family:var(--sans);
    line-height:1.5; -webkit-font-smoothing:antialiased;
    background-image:linear-gradient(var(--line) 1px,transparent 1px),linear-gradient(90deg,var(--line) 1px,transparent 1px);
    background-size:44px 44px; background-position:center;
    min-height:100vh; padding:40px 20px 64px;
  }
  .wrap{max-width:760px;margin:0 auto}
  .brandrow{display:flex;align-items:baseline;gap:12px;margin-bottom:6px}
  .mark{font-family:var(--mono);font-weight:600;font-size:13px;letter-spacing:.14em;
         text-transform:uppercase;color:var(--brass)}
  .mark .dot{color:var(--ink)}
  h1{font-size:clamp(28px,5vw,40px);font-weight:800;letter-spacing:-.02em;margin:.15em 0 .1em;line-height:1.05}
  .lede{color:var(--ink-soft);font-size:15px;max-width:52ch;margin:0}
  .panel{background:var(--card);border:1px solid var(--line-strong);border-radius:2px;
         margin-top:26px;box-shadow:0 1px 0 rgba(22,36,43,.04)}
  .askrow{display:flex;gap:0;padding:18px;border-bottom:1px solid var(--line)}
  .askrow input{flex:1;min-width:0;font-family:var(--mono);font-size:24px;letter-spacing:.06em;
    border:1px solid var(--line-strong);border-right:0;border-radius:2px 0 0 2px;padding:12px 16px;
    background:#fff;color:var(--ink);outline:none;font-weight:500}
  .askrow input::placeholder{color:#aeb8b9}
  .askrow input:focus{border-color:var(--brass);box-shadow:inset 0 0 0 1px var(--brass)}
  .askrow button{border:1px solid var(--ink);background:var(--ink);color:#fff;font-family:var(--sans);
    font-weight:700;font-size:16px;padding:0 26px;border-radius:0 2px 2px 0;cursor:pointer;white-space:nowrap;letter-spacing:.04em}
  .askrow button:hover{background:#0c171c}
  .askrow button:disabled{opacity:.5;cursor:default}
  .hint{padding:0 18px 16px;color:var(--ink-soft);font-size:12.5px;display:flex;flex-wrap:wrap;gap:6px 14px;align-items:center}
  .chip{font-family:var(--mono);font-size:12px;border:1px solid var(--line-strong);border-radius:2px;
        padding:2px 8px;cursor:pointer;color:var(--ink);background:#fff}
  .chip:hover{border-color:var(--brass);color:var(--brass)}
  details.tok{padding:0 18px 16px}
  details.tok summary{color:var(--ink-soft);font-size:12.5px;cursor:pointer;list-style:none}
  details.tok summary::-webkit-details-marker{display:none}
  details.tok input{margin-top:8px;width:100%;font-family:var(--mono);font-size:13px;padding:8px 10px;
    border:1px solid var(--line-strong);border-radius:2px;background:#fff}
  /* control row: 回看年數 / 中位↔平均 */
  .ctrl{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px;padding:0 18px 16px}
  .ctrl .lbl{font-size:12.5px;color:var(--ink-soft)}
  .seg{display:inline-flex;border:1px solid var(--line-strong);border-radius:2px;overflow:hidden}
  .seg button{font-family:var(--mono);font-size:12.5px;padding:5px 12px;border:0;border-left:1px solid var(--line-strong);
    background:#fff;color:var(--ink-soft);cursor:pointer}
  .seg button:first-child{border-left:0}
  .seg button:hover{color:var(--brass)}
  .seg button.on{background:var(--ink);color:#fff}
  /* ---- result ---- */
  #out{margin-top:22px}
  .state{color:var(--ink-soft);font-size:14px;padding:8px 2px}
  .state.err{color:var(--up)}
  .hero{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:16px;
        padding:22px 22px 18px;border-bottom:1px solid var(--line)}
  .hero .name{font-size:13px;color:var(--ink-soft);font-family:var(--mono);letter-spacing:.08em;margin-bottom:4px}
  .hero .name b{color:var(--ink);font-weight:700;font-size:16px}
  .fairbox .k{font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:var(--brass);font-family:var(--mono)}
  .fairbox .v{font-family:var(--mono);font-weight:600;font-size:clamp(40px,9vw,64px);line-height:1;letter-spacing:-.01em}
  .verdict{text-align:right}
  .verdict .tag{display:inline-block;font-weight:800;font-size:20px;padding:6px 14px;border-radius:2px;letter-spacing:.02em}
  .verdict .up-note{font-family:var(--mono);font-size:22px;font-weight:600;margin-top:8px}
  .t-cheap .tag{background:#fbe5e2;color:var(--up)} .t-cheap .up-note{color:var(--up)}
  .t-rich .tag{background:#dcefe6;color:var(--down)} .t-rich .up-note{color:var(--down)}
  .t-fair .tag{background:#eceef0;color:var(--ink)} .t-fair .up-note{color:var(--ink-soft)}
  .cmp{display:flex;gap:22px;padding:14px 22px;border-bottom:1px solid var(--line);font-family:var(--mono);font-size:13px;color:var(--ink-soft);flex-wrap:wrap}
  .cmp b{color:var(--ink);font-weight:600}
  /* band gauge (signature) */
  .band{padding:20px 22px;border-bottom:1px solid var(--line)}
  .band h3{font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--ink-soft);margin:0 0 30px;font-family:var(--mono);font-weight:500}
  .band h3 span{color:var(--ink);text-transform:none;letter-spacing:0}
  .track{position:relative;height:12px;border-radius:6px;
    background:linear-gradient(90deg,var(--down) 0%,#d9c98a 50%,var(--up) 100%);opacity:.9}
  .track.inv{background:linear-gradient(90deg,var(--up) 0%,#d9c98a 50%,var(--down) 100%)}
  .tick{position:absolute;top:-6px;width:2px;height:24px;background:var(--ink);transform:translateX(-1px)}
  .tick.med{background:var(--ink);opacity:.55}
  .cur{position:absolute;top:-13px;transform:translateX(-50%);text-align:center}
  .cur i{display:block;width:0;height:0;border-left:7px solid transparent;border-right:7px solid transparent;
    border-top:9px solid var(--brass);margin:0 auto}
  .cur b{font-family:var(--mono);font-size:12px;color:var(--brass);font-weight:600;background:var(--card);padding:0 3px;position:relative;top:-14px}
  .scale{display:flex;justify-content:space-between;font-family:var(--mono);font-size:11px;color:var(--ink-soft);margin-top:12px}
  .bandnote{font-size:12.5px;color:var(--ink-soft);margin-top:12px}
  .bandnote b{color:var(--ink)}
  /* PER river chart */
  .band svg{width:100%;height:auto;display:block;margin-top:2px}
  .rlegend{display:flex;flex-wrap:wrap;gap:6px 16px;font-family:var(--mono);font-size:11.5px;color:var(--ink-soft);margin-top:10px}
  .rlegend i{display:inline-block;width:12px;height:12px;border-radius:2px;vertical-align:-2px;margin-right:5px;border:1px solid var(--line-strong)}
  .rlegend i.ln{width:16px;height:0;border:0;border-top:2px solid var(--ink);border-radius:0;vertical-align:2px}
  /* breakdown table */
  table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:13.5px}
  td{padding:9px 22px;border-bottom:1px solid var(--line)}
  tr:last-child td{border-bottom:0}
  td.lab{color:var(--ink-soft);font-family:var(--sans);font-size:13px}
  td.num{text-align:right;font-weight:500}
  tr.tot td{font-weight:700;background:#f4f6f5}
  tr.tot td.num{font-size:16px;color:var(--brass)}
  tr.excl td{color:var(--ink-soft)}
  tr.excl td.num{text-decoration:line-through;text-decoration-color:var(--line-strong)}
  tr.excl td .excl-tag{color:var(--up);font-size:11.5px;margin-left:4px}
  .foot{color:var(--ink-soft);font-size:11.5px;line-height:1.7;margin-top:16px;padding:0 2px}
  .foot b{color:var(--ink)}
  .spin{display:inline-block;width:13px;height:13px;border:2px solid var(--line-strong);border-top-color:var(--brass);
    border-radius:50%;animation:sp .7s linear infinite;vertical-align:-2px;margin-right:7px}
  @keyframes sp{to{transform:rotate(360deg)}}
  @media (prefers-reduced-motion:reduce){.spin{animation:none}}
</style>
</head>
<body>
<div class="wrap">
  <div class="brandrow"><span class="mark">FAIR<span class="dot">·</span>VALUE</span></div>
  <h1>台股合理價估算</h1>
  <p class="lede">輸入代碼即可。用該股歷史本益比、股價淨值比與殖利率，回歸典型倍數推估合理價 —— 不需要你填任何財報數字。年數與中位／平均可自行切換。</p>

  <div class="panel">
    <div class="askrow">
      <input id="tk" inputmode="numeric" autocomplete="off" placeholder="輸入股票代碼，例如 2330" aria-label="股票代碼">
      <button id="go">估算</button>
    </div>
    <div class="hint">
      <span>試試：</span>
      <span class="chip" data-t="2330">2330 台積電</span>
      <span class="chip" data-t="2368">2368 金像電</span>
      <span class="chip" data-t="2337">2337 旺宏</span>
      <span class="chip" data-t="2454">2454 聯發科</span>
    </div>
    <div class="ctrl">
      <span class="lbl">回看年數</span>
      <div class="seg" id="segYears">
        <button data-y="3">3 年</button>
        <button data-y="5" class="on">5 年</button>
        <button data-y="10">10 年</button>
      </div>
      <span class="lbl">典型倍數</span>
      <div class="seg" id="segBasis">
        <button data-b="median" class="on">中位</button>
        <button data-b="mean">平均</button>
      </div>
    </div>
    <details class="tok">
      <summary>流量不夠？填入 FinMind 免費 Token（選填）</summary>
      <input id="tok" placeholder="貼上 Token，可將上限從 300/小時 提高到 600/小時">
    </details>
  </div>

  <div id="out"></div>
</div>

<script>
const $=s=>document.querySelector(s);
const out=$('#out'), tk=$('#tk'), tok=$('#tok'), go=$('#go');
const nf=(x,d=1)=>x==null?'—':Number(x).toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d});
const pf=x=>x==null?'—':(x>=0?'+':'')+ (x*100).toFixed(1)+'%';

function pos(band,val){ // 0..100 位置，夾在 lo..hi 視窗內
  const lo=band.lo, hi=band.hi; if(hi<=lo) return 50;
  return Math.max(2,Math.min(98,(val-lo)/(hi-lo)*100));
}
function gauge(title,unit,band,inv,bl){
  if(!band||band.cur==null) return '';
  bl=bl||'中位';
  const curPos=pos(band,band.cur), medPos=band.med!=null?pos(band,band.med):null;
  // inv=true 用於殖利率：數值越高＝越便宜，故左右語意與色帶相反
  const leftLab  = inv?`偏貴 ${nf(band.lo,2)}${unit}`:`便宜 ${nf(band.lo,2)}${unit}`;
  const rightLab = inv?`${nf(band.hi,2)}${unit} 便宜`:`${nf(band.hi,2)}${unit} 偏貴`;
  const note = inv
    ? `目前${title.replace(' 區間','')}落在近 ${band.pct!=null?('<b>'+band.pct+'</b> 百分位'):'—'}（殖利率越高越便宜：0＝史上最低/最貴、100＝史上最高/最便宜）。`
    : `目前 ${title.replace(' 區間','')} 落在近 ${band.pct!=null?('<b>'+band.pct+'</b> 百分位'):'—'}（0＝史上最便宜、100＝史上最貴）。`;
  return `<div class="band">
    <h3>${title}　<span>目前 ${nf(band.cur,2)}${unit}</span></h3>
    <div class="track${inv?' inv':''}">
      ${medPos!=null?`<div class="tick med" style="left:${medPos}%" title="歷史${bl} ${nf(band.med,2)}"></div>`:''}
      <div class="cur" style="left:${curPos}%"><b>${nf(band.cur,2)}</b><i></i></div>
    </div>
    <div class="scale"><span>${leftLab}</span><span>歷史${bl} ${nf(band.med,2)}${unit}</span><span>${rightLab}</span></div>
    <div class="bandnote">${note}</div>
  </div>`;
}

function riverPanel(d){
  const c=d.chart;
  if(!c||!c.dates||c.dates.length<2) return '';
  const W=700,H=300,mL=54,mR=14,mT=12,mB=26;
  const iw=W-mL-mR, ih=H-mT-mB, n=c.dates.length;
  const keys=['p10','p25','p50','p75','p90'];
  const bands=keys.map(k=>c.eps.map(e=>c.pcts[k]*e));   // 每條倍數帶(t)=百分位PER×EPS_t
  const vals=[].concat(...bands, c.close);
  let ymin=Math.min(...vals), ymax=Math.max(...vals);
  const pad=(ymax-ymin)*0.05||1; ymin-=pad; ymax+=pad;
  const X=i=> mL + iw*(i/(n-1));
  const Y=v=> mT + ih*(1-(v-ymin)/(ymax-ymin));
  const path=arr=> arr.map((v,i)=>(i?'L':'M')+X(i).toFixed(1)+' '+Y(v).toFixed(1)).join(' ');
  const area=(lo,hi)=>{
    let up=hi.map((v,i)=>(i?'L':'M')+X(i).toFixed(1)+' '+Y(v).toFixed(1)).join(' ');
    let dn=''; for(let i=n-1;i>=0;i--){ dn+='L'+X(i).toFixed(1)+' '+Y(lo[i]).toFixed(1)+' '; }
    return up+' '+dn+'Z';
  };
  const fills=['#d7ebe0','#eef1ea','#f5ecd7','#f6ddd5'];      // 低倍數(便宜)綠 → 高倍數(貴)紅
  const strk =['#127a4f','#8aa89a','#a97c1e','#c9976a','#c8362f'];
  let g='';
  for(let b=0;b<4;b++) g+=`<path d="${area(bands[b],bands[b+1])}" fill="${fills[b]}"/>`;
  for(let b=0;b<5;b++) g+=`<path d="${path(bands[b])}" fill="none" stroke="${strk[b]}" stroke-width="1" opacity=".65"/>`;
  // y 軸格線與價格刻度
  for(let t=0;t<=4;t++){ const v=ymin+(ymax-ymin)*t/4, y=Y(v);
    g+=`<line x1="${mL}" y1="${y.toFixed(1)}" x2="${W-mR}" y2="${y.toFixed(1)}" stroke="#e2e7e6"/>`;
    g+=`<text x="${mL-6}" y="${(y+3).toFixed(1)}" text-anchor="end" class="ax">${nf(v,0)}</text>`;
  }
  // x 軸年份
  let ly='';
  c.dates.forEach((dt,i)=>{ const yr=dt.slice(0,4);
    if(yr!==ly){ ly=yr; g+=`<text x="${X(i).toFixed(1)}" y="${H-8}" text-anchor="middle" class="ax">${yr}</text>`; }
  });
  g+=`<path d="${path(c.close)}" fill="none" stroke="#16242b" stroke-width="2"/>`;
  g+=`<circle cx="${X(n-1).toFixed(1)}" cy="${Y(c.close[n-1]).toFixed(1)}" r="3.5" fill="#a97c1e" stroke="#fff" stroke-width="1.5"/>`;
  const P=c.pcts;
  // 若本益比資料明顯落後最新股價（近期虧損 → PER=0 被濾掉），提醒圖表未涵蓋近期股價
  let stale='';
  const gapDays=(new Date(d.price_date)-new Date(c.dates[n-1]))/86400000;
  if(gapDays>60) stale=`<div class="bandnote" style="color:var(--up)">⚠ 因近期出現虧損（本益比無意義），本益比序列僅到 <b>${c.dates[n-1]}</b>；其後至 ${d.price_date} 的股價無對應倍數帶、未畫入本圖。此情況下 P/E 法參考性低，請以股價淨值比為主。</div>`;
  return `<div class="band">
    <h3>本益比河流圖　<span>近 ${d.years} 年 · 倍數帶 P10 ${nf(P.p10,1)} / P25 ${nf(P.p25,1)} / 中位 ${nf(P.p50,1)} / P75 ${nf(P.p75,1)} / P90 ${nf(P.p90,1)}</span></h3>
    <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="本益比河流圖">
      <style>.ax{font:10px 'IBM Plex Mono',monospace;fill:#5c6d74}</style>
      ${g}
    </svg>
    <div class="rlegend">
      <span><i class="ln"></i>股價</span>
      <span><i style="background:#d7ebe0"></i>P10–P25 便宜</span>
      <span><i style="background:#eef1ea"></i>P25–中位</span>
      <span><i style="background:#f5ecd7"></i>中位–P75</span>
      <span><i style="background:#f6ddd5"></i>P75–P90 偏貴</span>
    </div>
    ${stale}
    <div class="bandnote">價格線落在哪條帶之間，代表當時本益比約在該百分位區間；穿到最上緣＝逼近近 ${d.years} 年最貴、貼近底部＝相對便宜。倍數帶隨每期 EPS 起伏，故帶寬會擴縮。</div>
  </div>`;
}

function render(d){
  if(!d.ok){ out.innerHTML=`<div class="state err">⚠ ${d.error}</div>`; return; }
  const cls = d.tone==='cheap'?'t-cheap':d.tone==='rich'?'t-rich':'t-fair';
  const bl = d.basis==='mean'?'平均':'中位';
  out.innerHTML = `<div class="panel ${cls}">
    <div class="hero">
      <div class="fairbox">
        <div class="name">代碼 <b>${d.ticker}</b></div>
        <div class="k">綜合合理價</div>
        <div class="v">${nf(d.estimate,1)}</div>
      </div>
      <div class="verdict">
        <span class="tag">${d.verdict}</span>
        <div class="up-note">${pf(d.upside)}<span style="font-size:13px;color:var(--ink-soft)"> vs 現價</span></div>
      </div>
    </div>
    <div class="cmp">
      <span>目前股價 <b>${nf(d.close,2)}</b></span>
      <span>推估 EPS <b>${nf(d.eps,2)}</b></span>
      <span>每股淨值 <b>${nf(d.bvps,2)}</b></span>
      ${d.yield!=null?`<span>殖利率 <b>${nf(d.yield,2)}%</b></span>`:''}
      <span>近 <b>${d.years}</b> 年 · ${d.n} 筆</span>
    </div>
    ${gauge('本益比 區間','x',d.per_band,false,bl)}
    ${gauge('股價淨值比 區間','x',d.pbr_band,false,bl)}
    ${gauge('殖利率 區間','%',d.div_band,true,bl)}
    ${riverPanel(d)}
    <table>
      <tr class="${d.pe_stale?'excl':''}"><td class="lab">本益比法　歷史${bl} ${nf(d.typ_per,2)}x × EPS ${nf(d.eps,2)}${d.pe_stale?'<span class="excl-tag">近期虧損，未納入</span>':''}</td><td class="num">${nf(d.fair_pe,1)}</td></tr>
      <tr><td class="lab">股價淨值比法　歷史${bl} ${nf(d.typ_pbr,2)}x × 淨值 ${nf(d.bvps,2)}</td><td class="num">${nf(d.fair_pb,1)}</td></tr>
      ${d.fair_div!=null?`<tr><td class="lab">殖利率法　現價 × 目前殖利率 ${nf(d.cur_yld,2)}% ÷ ${bl} ${nf(d.typ_yld,2)}%</td><td class="num">${nf(d.fair_div,1)}</td></tr>`:''}
      <tr class="tot"><td class="lab">綜合合理價（${d.method_label}）</td><td class="num">${nf(d.estimate,1)}</td></tr>
    </table>
  </div>
  <div class="foot">
    方法：以個股近 ${d.years} 年<b>本益比 / 股價淨值比 / 殖利率的歷史${bl}</b>為「典型水準」，回推合理價（殖利率法：現價 × 目前殖利率 ÷ ${bl}殖利率）。三者可用者取平均；近期虧損時排除本益比、無配息時排除殖利率。它回答的是「若估值回到自身歷史常態、價格會落在哪」，<b>不是</b>對未來獲利的預測。<br>
    因此對高成長股（例如 AI 概念）容易顯示「偏貴」——市場給的高倍數可能有其道理，這個模型看不到未來成長，請斟酌。<br>
    資料：FinMind（本益比截至 ${d.per_date||'—'}、股價截至 ${d.price_date||'—'}）。本工具為分析輔助，非投資建議。
  </div>`;
}

let curYears=5, curBasis='median', lastTicker='';
async function run(){
  const t=tk.value.trim(); if(!t) return;
  lastTicker=t;
  go.disabled=true; out.innerHTML=`<div class="state"><span class="spin"></span>估算中…（抓取 ${t} 近 ${curYears} 年的歷史估值資料）</div>`;
  try{
    const u='/api/estimate?ticker='+encodeURIComponent(t)+'&years='+curYears+'&basis='+curBasis
      +(tok.value.trim()?'&token='+encodeURIComponent(tok.value.trim()):'');
    const r=await fetch(u); const d=await r.json(); render(d);
  }catch(e){ out.innerHTML=`<div class="state err">⚠ 無法取得結果：${e}</div>`; }
  go.disabled=false;
}
// 分段控制：切換後若已有查詢結果，立即用新參數重算
function seg(id,attr,set){
  document.querySelectorAll('#'+id+' button').forEach(b=>b.onclick=()=>{
    document.querySelectorAll('#'+id+' button').forEach(x=>x.classList.remove('on'));
    b.classList.add('on'); set(b.dataset[attr]);
    if(lastTicker) run();
  });
}
seg('segYears','y',v=>curYears=+v);
seg('segBasis','b',v=>curBasis=v);
go.onclick=run;
tk.addEventListener('keydown',e=>{if(e.key==='Enter')run();});
document.querySelectorAll('.chip').forEach(c=>c.onclick=()=>{tk.value=c.dataset.t;run();});
tk.focus();
</script>
</body>
</html>"""


# ---------------------------------------------------------------- 伺服器
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        b = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
            return
        if parsed.path == "/api/estimate":
            q = urllib.parse.parse_qs(parsed.query)
            ticker = (q.get("ticker", [""])[0])
            token = (q.get("token", [""])[0])
            try:
                years = int(q.get("years", ["5"])[0])
            except ValueError:
                years = 5
            basis = q.get("basis", ["median"])[0]
            try:
                res = estimate_ticker(ticker, token, years, basis)
            except Exception as e:
                res = {"ok": False, "error": f"程式錯誤：{e}"}
            self._send(200, json.dumps(res, ensure_ascii=False))
            return
        self._send(404, json.dumps({"ok": False, "error": "not found"}, ensure_ascii=False))

    def log_message(self, *args):
        pass  # 靜音


def main():
    port = 8787
    httpd = None
    for p in range(port, port + 12):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            port = p
            break
        except OSError:
            continue
    if httpd is None:
        print("找不到可用的連接埠，請關掉其他程式後再試。")
        sys.exit(1)
    url = f"http://127.0.0.1:{port}/"
    print("=" * 52)
    print("  台股合理價估算器已啟動")
    print(f"  請在瀏覽器開啟： {url}")
    print("  （視窗應會自動彈出；沒有的話手動貼上上面網址）")
    print("  按 Ctrl+C 可結束程式。")
    print("=" * 52)
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已結束。")


if __name__ == "__main__":
    main()
