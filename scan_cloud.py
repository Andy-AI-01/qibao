# -*- coding: utf-8 -*-
"""
云端版扫描器（自包含、零第三方依赖、纯标准库）
---------------------------------------------
  python scan_cloud.py            # 扫描全市场，生成起爆信号
  python scan_cloud.py --auction  # 采集集合竞价（9:15~9:40）
输出：docs/data.json / docs/data.js / docs/auction.json
"""
import os, sys, json, time, urllib.request, datetime
from collections import deque
from concurrent.futures import ThreadPoolExecutor

BASE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(BASE, "docs")
os.makedirs(DOCS, exist_ok=True)
HDR = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
       "Connection": "close", "Referer": "https://finance.sina.com.cn/"}
BARS = 200
WORKERS = 32


def get(url, enc="utf-8", tries=2):
    for i in range(tries):
        try:
            return urllib.request.urlopen(
                urllib.request.Request(url, headers=HDR), timeout=25).read().decode(enc, "ignore")
        except Exception:
            time.sleep(0.3 * (i + 1))
    return None


# ---------------- 高效滚动计算 ----------------
def roll_mean(x, n):
    ps = [0.0]
    for v in x:
        ps.append(ps[-1] + v)
    return [None if i < n - 1 else (ps[i + 1] - ps[i - n + 1]) / n for i in range(len(x))]


def roll_ext(x, n, want_max=True):
    dq, out = deque(), [None] * len(x)
    for i, v in enumerate(x):
        while dq and ((x[dq[-1]] <= v) if want_max else (x[dq[-1]] >= v)):
            dq.pop()
        dq.append(i)
        while dq[0] <= i - n:
            dq.popleft()
        if i >= n - 1:
            out[i] = x[dq[0]]
    return out


def ema(x, n):
    k = 2.0 / (n + 1)
    out, v = [0.0] * len(x), x[0]
    for i, val in enumerate(x):
        v = val if i == 0 else v + k * (val - v)
        out[i] = v
    return out


# ---------------- 数据源 ----------------
def universe():
    out, seen = [], set()
    for page in range(1, 80):
        u = ("http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
             "Market_Center.getHQNodeData?page=%d&num=100&sort=mktcap&asc=0&node=hs_a" % page)
        t = get(u, enc="gbk")
        if not t or t.strip() in ("null", "[]"):
            break
        try:
            arr = json.loads(t)
        except Exception:
            continue
        if not arr:
            break
        for d in arr:
            c, nm, sym = d.get("code"), d.get("name", ""), d.get("symbol", "")
            if not c or c in seen or c[0] in ("4", "8", "9") or "退" in nm:
                continue
            seen.add(c)
            out.append({"code": c, "market": 1 if sym.startswith("sh") else 0, "name": nm})
        time.sleep(0.05)
    return out


def klines(sym):
    u = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,%d,qfq" % (sym, BARS)
    t = get(u)
    if not t:
        return None
    try:
        j = json.loads(t)
        if j.get("code") != 0:
            return None
        d = j["data"][sym]
        return d.get("qfqday") or d.get("day") or []
    except Exception:
        return None


def snapshot(syms):
    raw = {}

    def work(chunk):
        t = get("https://qt.gtimg.cn/q=" + ",".join(chunk), enc="gbk")
        if not t:
            return
        for line in t.split(";"):
            if "~" not in line:
                continue
            fl = line.split("~")
            if len(fl) < 40:
                continue
            try:
                raw[fl[2]] = dict(px=float(fl[3]), prev=float(fl[4]), op=float(fl[5]),
                                  vol=float(fl[6]), amt=float(fl[37] or 0) * 1e4,
                                  chg=float(fl[32] or 0))
            except Exception:
                continue

    chunks = [syms[i:i + 60] for i in range(0, len(syms), 60)]
    with ThreadPoolExecutor(max_workers=12) as ex:
        list(ex.map(work, chunks))
    return raw


# ---------------- 指标判定 ----------------
def analyse(bars):
    if len(bars) < 150:
        return None
    d = [b[0] for b in bars]
    o = [float(b[1]) for b in bars]
    c = [float(b[2]) for b in bars]
    h = [float(b[3]) for b in bars]
    l = [float(b[4]) for b in bars]
    v = [float(b[5]) for b in bars]
    n = len(c)
    if min(c[-5:]) <= 0:
        return None

    m5, m10, m20 = roll_mean(c, 5), roll_mean(c, 10), roll_mean(c, 20)
    m60, lm = roll_mean(c, 60), roll_mean(c, 120)
    vma20, vma60, vma5 = roll_mean(v, 20), roll_mean(v, 60), roll_mean(v, 5)
    hh20, ll20 = roll_ext(c, 20, True), roll_ext(l, 20, False)
    ll30, ll60 = roll_ext(l, 30, False), roll_ext(l, 60, False)

    t = n - 1
    for arr in (m5, m10, m20, m60, vma20, ll20, ll30, ll60):
        if arr[t] is None:
            return None
    Z = lambda x, i: x[i] if x[i] is not None else 0.0

    chg = c[t] / c[t - 1] - 1
    chg_p = c[t - 1] / c[t - 2] - 1
    dif = [a - b for a, b in zip(ema(c, 12), ema(c, 26))]

    def boom(i):
        return (c[i] / c[i - 1] - 1) >= 0.06 and v[i] >= Z(vma20, i) * 1.8 and c[i] > Z(m20, i)

    BOOM = boom(t)
    has30 = any(boom(i) for i in range(max(1, t - 29), t + 1))
    has40 = any(boom(i) for i in range(max(1, t - 39), t + 1))

    maf = min(Z(m5, t), Z(m10, t), Z(m20, t), Z(m60, t))
    deep = has30 and Z(ll30, t) < maf
    strong = has30 and Z(ll30, t) > Z(m20, t)
    cross5 = c[t] > Z(m5, t) and c[t - 1] <= Z(m5, t - 1)
    A2 = deep and cross5 and v[t] >= Z(vma20, t) * 1.3
    B2 = strong and c[t] > Z(hh20, t - 1) and v[t] >= Z(vma20, t) * 1.8 and chg >= 0.04
    hb = dif[t] > 0
    C2 = chg >= 0.095 and chg_p >= 0.095
    DUO = Z(m5, t) > Z(m10, t) > Z(m20, t) > Z(m60, t)
    UPX = Z(m5, t) > Z(m5, t - 1) and Z(m20, t) > Z(m20, t - 1) and Z(m60, t) > Z(m60, t - 1)
    ADDX = C2 and DUO and UPX
    SHRINK = Z(vma5, t - 1) > 0 and Z(vma60, t) > 0 and Z(vma5, t - 1) < Z(vma60, t) * 0.85
    HOLD = lm[t] is not None and Z(ll20, t) > Z(lm, t) > Z(lm, t - 1)
    LIFE2 = has40 and HOLD and SHRINK and c[t] > Z(hh20, t - 1) and \
        v[t] >= Z(vma20, t) * 1.8 and chg >= 0.04
    CUT = c[t] < Z(m20, t) and c[t - 1] < Z(m20, t - 1) and c[t - 2] < Z(m20, t - 2) and \
        c[t - 3] >= Z(m20, t - 3)
    XL = l[t] <= Z(ll60, t)

    tags = []
    if BOOM: tags.append("爆")
    if (A2 or B2) and hb: tags.append("二爆")
    if C2: tags.append("二板")
    if ADDX: tags.append("加仓")
    if LIFE2: tags.append("生命线")
    if XL: tags.append("新低")
    if CUT: tags.append("清仓")
    if not tags:
        return None
    score = ("爆" in tags) + ("二爆" in tags) * 3 + ("二板" in tags) * 2 + \
        ("加仓" in tags) * 2 + ("生命线" in tags) * 3 - ("清仓" in tags) * 2
    return dict(date=d[t], close=round(c[t], 2), chg=round(chg * 100, 2), tags=tags,
                score=score, vol_ratio=round(v[t] / Z(vma20, t), 2),
                ma20_gap=round((c[t] / Z(m20, t) - 1) * 100, 2),
                life_gap=round((c[t] / Z(lm, t) - 1) * 100, 2) if Z(lm, t) else 0.0,
                amt=round(v[t] * 100 * c[t] / 1e8, 2))


def do_auction(uni):
    syms = [("sh" if s["market"] == 1 else "sz") + s["code"] for s in uni]
    snap = snapshot(syms)
    now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    hm = now.hour * 100 + now.minute
    valid = 915 <= hm <= 940
    nm = {s["code"]: s["name"] for s in uni}
    items = []
    for code, q in snap.items():
        if q["vol"] <= 0 or q["prev"] <= 0:
            continue
        items.append(dict(code=code, name=nm.get(code, code), op=round(q["op"], 2),
                          gap=round((q["op"] / q["prev"] - 1) * 100, 2),
                          vol=round(q["vol"] / 1e4, 2), amt=round(q["amt"] / 1e4, 1),
                          ratio=0.0, ratio5=0.0, lv="", in_buy=False, in_boom=False))
    payload = dict(date=now.strftime("%Y-%m-%d"), time=now.strftime("%H:%M"),
                   valid=valid, message="" if valid else "非竞价时段", items=items[:1])
    json.dump(payload, open(os.path.join(DOCS, "auction.json"), "w", encoding="utf-8"),
              ensure_ascii=False)
    print("auction done, valid=%s" % valid)


def main():
    uni = universe()
    print("universe %d" % len(uni), flush=True)
    rows = []

    def work(s):
        sym = ("sh" if s["market"] == 1 else "sz") + s["code"]
        return s, klines(sym)

    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for s, bars in ex.map(work, uni):
            done += 1
            if done % 800 == 0:
                print("  %d/%d" % (done, len(uni)), flush=True)
            if not bars or bars[-1][0] < "2020":
                continue
            r = analyse(bars)
            if r and r["date"] == bars[-1][0]:
                r.update(code=s["code"], name=s["name"])
                rows.append(r)

    snap = snapshot([("sh" if s["market"] == 1 else "sz") + s["code"] for s in uni])
    up = sum(1 for q in snap.values() if q["chg"] > 0)
    dn = sum(1 for q in snap.values() if q["chg"] < 0)
    rows.sort(key=lambda x: (-x["score"], -x["vol_ratio"]))
    trade_date = max((r["date"] for r in rows), default=datetime.date.today().isoformat())

    payload = dict(updated=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   trade_date=trade_date, universe=len(uni), params={},
                   market=dict(up=up, down=dn, total=len(snap),
                               temp=round(100.0 * up / max(1, up + dn), 1), indexes=[]),
                   buy=[], sell=[], boom=rows, history=[], recent=[])
    js = json.dumps(payload, ensure_ascii=False)
    open(os.path.join(DOCS, "data.json"), "w", encoding="utf-8").write(js)
    open(os.path.join(DOCS, "data.js"), "w", encoding="utf-8").write("window.__DATA__=" + js + ";")
    print("signals %d  temp %.1f%%" % (len(rows), payload["market"]["temp"]), flush=True)


if __name__ == "__main__":
    if "--auction" in sys.argv:
        do_auction(universe())
    else:
        main()
