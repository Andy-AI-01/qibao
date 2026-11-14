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
            try:
                _cap = float(d.get("mktcap") or 0)
            except Exception:
                _cap = 0.0
            out.append({"code": c, "market": 1 if sym.startswith("sh") else 0,
                        "name": nm, "cap": _cap})
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


def bjnow():
    """北京时间（GitHub 服务器是 UTC）"""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) + \
        datetime.timedelta(hours=8)


def indexes():
    """三大指数"""
    t = get("https://qt.gtimg.cn/q=sh000001,sz399001,sz399006", enc="gbk")
    out = []
    if not t:
        return out
    for line in t.split(";"):
        if "~" not in line:
            continue
        f = line.split("~")
        if len(f) > 32 and f[2]:
            try:
                out.append(dict(code=f[2], name=f[1], px=float(f[3]), chg=float(f[32] or 0)))
            except Exception:
                pass
    return out


def em_get(url, timeout=15):
    try:
        return urllib.request.urlopen(
            urllib.request.Request(url, headers=HDR), timeout=timeout).read().decode("utf-8", "ignore")
    except Exception:
        return None


ZT_UT = "7eea3edcaed734bea9cbfc24409ed989"


def _pool(path, date_str, sort="fbt%3Aasc"):
    u = ("https://push2ex.eastmoney.com/%s?ut=%s&dpt=wz.ztzt&Pageindex=0&pagesize=500"
         "&sort=%s&date=%s" % (path, ZT_UT, sort, date_str))
    t = em_get(u)
    if not t:
        return []
    try:
        return (json.loads(t).get("data") or {}).get("pool") or []
    except Exception:
        return []


def zt_pool(d):
    return _pool("getTopicZTPool", d, "fbt%3Aasc")


def zb_pool(d):
    return _pool("getTopicZBPool", d, "fbt%3Aasc")


def dt_pool(d):
    return _pool("getTopicDTPool", d, "fund%3Aasc")


def industry_map(uni):
    """代码->行业。优先读本地 industry.json（一次抓取永久复用），没有再联网抓"""
    f = os.path.join(DOCS, "industry.json")
    if os.path.exists(f):
        try:
            d = json.load(open(f, encoding="utf-8"))
            if len(d) >= len(uni) * 0.6:
                return d
        except Exception:
            pass

    out = {}
    pairs = [("1." if s["market"] == 1 else "0.") + s["code"] for s in uni]
    base = ("https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&invt=2"
            "&fields=f12,f100&secids=")
    chunks = [pairs[i:i + 100] for i in range(0, len(pairs), 100)]
    for i, ch in enumerate(chunks):
        t = em_get(base + ",".join(ch), timeout=12)
        if not t:
            time.sleep(0.4)
            continue
        try:
            diff = (json.loads(t).get("data") or {}).get("diff") or []
            if isinstance(diff, dict):
                diff = [diff]
            for it in diff:
                c, ind = it.get("f12"), it.get("f100")
                if c and ind and ind != "-":
                    out[str(c)] = ind
        except Exception:
            pass
        time.sleep(0.12)

    if len(out) >= len(uni) * 0.6:
        try:
            json.dump(out, open(f, "w", encoding="utf-8"), ensure_ascii=False)
            print("  行业表已缓存 %d 条 -> docs/industry.json" % len(out), flush=True)
        except Exception:
            pass
    return out


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
    # 复权异常过滤：单日跳变超过 23% 说明前复权数据有问题
    for i in range(1, n):
        if c[i - 1] <= 0:
            return None
        if abs(c[i] / c[i - 1] - 1) > 0.23:
            return None

    m5, m10, m20 = roll_mean(c, 5), roll_mean(c, 10), roll_mean(c, 20)
    m60, lm = roll_mean(c, 60), roll_mean(c, 120)
    vma20, vma60, vma5 = roll_mean(v, 20), roll_mean(v, 60), roll_mean(v, 5)
    hh20, ll20 = roll_ext(c, 20, True), roll_ext(l, 20, False)
    ll30, ll60 = roll_ext(l, 30, False), roll_ext(l, 60, False)
    hh60 = roll_ext(h, 60, True)
    hh120, ll120 = roll_ext(h, 120, True), roll_ext(l, 120, False)

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

    # 长期横盘后的爆量突破箱体
    plat_w = 0.0
    if hh60[t - 1] and ll60[t - 1] and ll60[t - 1] > 0:
        plat_w = (hh60[t - 1] - ll60[t - 1]) / ll60[t - 1]
    BOX = (0 < plat_w <= 0.30 and c[t] > Z(hh60, t - 1)
           and v[t] >= Z(vma60, t - 1) * 2.5 and chg >= 0.04)

    tags = []
    if BOOM: tags.append("爆")
    if (A2 or B2) and hb: tags.append("二爆")
    if C2: tags.append("二板")
    if ADDX: tags.append("加仓")
    if LIFE2: tags.append("生命线")
    if BOX: tags.append("横盘突破")
    if XL: tags.append("新低")
    if CUT: tags.append("清仓")
    if not tags:
        return None
    score = ("爆" in tags) + ("二爆" in tags) * 3 + ("二板" in tags) * 2 + \
        ("加仓" in tags) * 2 + ("生命线" in tags) * 3 + ("横盘突破" in tags) * 3 - ("清仓" in tags) * 2
    return dict(date=d[t], close=round(c[t], 2), chg=round(chg * 100, 2), tags=tags,
                score=score, vol_ratio=round(v[t] / Z(vma20, t), 2),
                ma20_gap=round((c[t] / Z(m20, t) - 1) * 100, 2),
                life_gap=round((c[t] / Z(lm, t) - 1) * 100, 2) if Z(lm, t) else 0.0,
                amt=round(v[t] * 100 * c[t] / 1e8, 2),
                pos=round((c[t] - Z(ll120, t)) / (Z(hh120, t) - Z(ll120, t)) * 100, 1)
                if Z(hh120, t) > Z(ll120, t) else 50.0)


def do_auction(uni):
    """云端版不采集竞价（定时任务延迟不可控，采到的不是真实竞价量）"""
    now = bjnow()
    payload = dict(date=now.strftime("%Y-%m-%d"), time=now.strftime("%H:%M"),
                   valid=False, message="云端版不采集集合竞价，请在本地版查看", items=[])
    json.dump(payload, open(os.path.join(DOCS, "auction.json"), "w", encoding="utf-8"),
              ensure_ascii=False)
    print("auction skipped (cloud version)")


def main():
    uni = universe()
    print("universe %d" % len(uni), flush=True)
    ind_map = industry_map(uni)      # 先取行业，避免后续请求把它限流
    print("行业映射 %d" % len(ind_map), flush=True)
    rows = []

    def work(s):
        sym = ("sh" if s["market"] == 1 else "sz") + s["code"]
        return s, klines(sym)

    done = 0
    ok_fetch = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for s, bars in ex.map(work, uni):
            done += 1
            if done % 800 == 0:
                print("  %d/%d" % (done, len(uni)), flush=True)
            if not bars or bars[-1][0] < "2020":
                continue
            ok_fetch += 1
            r = analyse(bars)
            if r and r["date"] == bars[-1][0]:
                r.update(code=s["code"], name=s["name"])
                rows.append(r)

    ratio = ok_fetch / max(1, len(uni))
    err = ""
    if ratio < 0.5:
        err = "行情数据抓取失败（成功 %d/%d），本次结果不可信，请稍后重跑" % (ok_fetch, len(uni))
        print("!! " + err, flush=True)

    snap = snapshot([("sh" if s["market"] == 1 else "sz") + s["code"] for s in uni])
    up = sum(1 for q in snap.values() if q["chg"] > 0)
    dn = sum(1 for q in snap.values() if q["chg"] < 0)
    capmap = {u["code"]: u.get("cap", 0) for u in uni}
    tracks = {"T": 0, "Y": 0, "N": 0}
    for r in rows:
        capy = capmap.get(r["code"], 0) / 1e4          # 亿元
        r["cap"] = round(capy, 1)
        bull = any(t in r["tags"] for t in ("爆", "二爆", "二板", "加仓", "生命线", "横盘突破"))
        if not bull:
            r["track"] = "N"
        elif capy >= 104 and r.get("pos", 0) >= 70:
            r["track"] = "T"
        elif 0 < capy <= 50 and r["close"] <= 10 and r.get("pos", 100) <= 30:
            r["track"] = "Y"
        else:
            r["track"] = "N"
        tracks[r["track"]] += 1

    rows.sort(key=lambda x: (-x["score"], -x["vol_ratio"]))
    trade_date = max((r["date"] for r in rows), default=bjnow().strftime("%Y-%m-%d"))

    # ---------------- 涨停池 / 连板梯队 / 催化 / 情绪 ----------------
    dstr = bjnow().strftime("%Y%m%d")
    zt = zt_pool(dstr)
    zb = zb_pool(dstr)
    dtp = dt_pool(dstr)
    print("涨停 %d 炸板 %d 跌停 %d 行业映射 %d" % (len(zt), len(zb), len(dtp), len(ind_map)), flush=True)

    ind_stat = {}
    for x in zt:
        h = x.get("hybk") or ind_map.get(str(x.get("c"))) or "其他"
        e = ind_stat.setdefault(h, {"n": 0, "max_lb": 0, "names": []})
        e["n"] += 1
        lb = int(x.get("lbc") or 1)
        e["max_lb"] = max(e["max_lb"], lb)
        if len(e["names"]) < 3:
            e["names"].append(x.get("n"))
    for h, e in ind_stat.items():
        e["cat"] = "强催化" if e["n"] >= 4 else ("中催化" if e["n"] >= 2 else "弱催化")

    ztmap = {str(x.get("c")): x for x in zt}
    for r in rows:
        z = ztmap.get(r["code"])
        r["lbc"] = int(z.get("lbc") or 0) if z else 0
        r["ind"] = (z.get("hybk") if z else None) or ind_map.get(r["code"], "") or ""
        r["cat"] = ind_stat.get(r["ind"], {}).get("cat", "") if r["ind"] else ""
        tj = (z or {}).get("zttj") or {}
        r["ztj"] = ("%d天%d板" % (tj.get("days", 0), tj.get("ct", 0))) if tj else ""

    def grade(x):
        lb = int(x.get("lbc") or 1)
        zbc = int(x.get("zbc") or 0)
        fbt = int(x.get("fbt") or 0)
        ltsz = float(x.get("ltsz") or 0)
        fund = float(x.get("fund") or 0)
        hs = float(x.get("hs") or 0)
        ind = x.get("hybk") or ""
        cat = ind_stat.get(ind, {}).get("cat", "")
        ratio = (fund / ltsz) if ltsz else 0
        dims = [("连板≥3", lb >= 3), ("0炸板", zbc == 0), ("10点前封", 0 < fbt <= 100000),
                ("封单≥1%", ratio >= 0.01), ("强催化", cat == "强催化"),
                ("换手3-15%", 3 <= hs <= 15)]
        sc = sum(1 for _, ok in dims if ok)
        miss = [n for n, ok in dims if not ok]
        if hs < 3:
            bt = "缩量板"
        elif hs <= 10:
            bt = "温和换手"
        elif hs <= 20:
            bt = "高换手"
        else:
            bt = "巨量板"
        return sc, miss, ratio, bt

    ladder = {"1": [], "2": [], "3": [], "4+": []}
    for x in zt:
        lb = int(x.get("lbc") or 1)
        k = "4+" if lb >= 4 else str(lb)
        ind = x.get("hybk") or ""
        ladder[k].append(dict(
            code=str(x.get("c")), name=x.get("n"), ind=ind, lb=lb,
            fbt=int(x.get("fbt") or 0),
            fund=round(float(x.get("fund") or 0) / 1e8, 2),
            zbc=int(x.get("zbc") or 0),
            hs=round(float(x.get("hs") or 0), 2),
            ltsz=float(x.get("ltsz") or 0),
            cat=ind_stat.get(ind, {}).get("cat", "")))
        _g = grade(x)
        ladder[k][-1].update(bt=_g[3], score=_g[0])
    for k in ladder:
        ladder[k].sort(key=lambda z: (z["fbt"] if z["fbt"] else 999999))

    # ---------- 六维打分：找"没有短板"的龙头 ----------
    graded = []
    for x in zt:
        sc, miss, ratio, bt = grade(x)
        graded.append(dict(code=str(x.get("c")), name=x.get("n"),
                           ind=x.get("hybk") or "", lb=int(x.get("lbc") or 1),
                           fbt=int(x.get("fbt") or 0),
                           fund=round(float(x.get("fund") or 0) / 1e8, 2),
                           zbc=int(x.get("zbc") or 0),
                           hs=round(float(x.get("hs") or 0), 2),
                           seal=round(ratio * 100, 2), score=sc, miss=miss, bt=bt))
    graded.sort(key=lambda z: (-z["score"], -z["lb"], z["fbt"] if z["fbt"] else 999999))
    leaders = [z for z in graded if z["score"] >= 4][:3]

    ind_top = sorted(
        [dict(name=h, n=e["n"], max_lb=e["max_lb"], cat=e["cat"], names=e["names"])
         for h, e in ind_stat.items()],
        key=lambda x: (-x["n"], -x["max_lb"]))[:12]

    zb_rate = len(zb) / max(1, len(zt) + len(zb))
    max_lb = max([int(x.get("lbc") or 1) for x in zt], default=0)
    strong = (len(zt) >= 45 and zb_rate <= 0.35 and max_lb >= 4)
    sent = dict(level="强情绪" if strong else "弱情绪", zt=len(zt), zb=len(zb),
                dt=len(dtp), zb_rate=round(zb_rate * 100, 1), max_lb=max_lb)

    payload = dict(updated=bjnow().strftime("%Y-%m-%d %H:%M:%S"),
                   trade_date=trade_date, universe=len(uni), params={},
                   market=dict(up=up, down=dn, total=len(snap),
                               temp=round(100.0 * up / max(1, up + dn), 1),
                               indexes=indexes()),
                   buy=[], sell=[], boom=rows, history=[], recent=[],
                   ladder=ladder, leaders=leaders, tracks=tracks, ind_top=ind_top, sentiment=sent,
                   fetch_ok=ok_fetch, fetch_total=len(uni), error=err)
    js = json.dumps(payload, ensure_ascii=False)
    open(os.path.join(DOCS, "data.json"), "w", encoding="utf-8").write(js)
    open(os.path.join(DOCS, "data.js"), "w", encoding="utf-8").write("window.__DATA__=" + js + ";")
    print("signals %d  temp %.1f%%" % (len(rows), payload["market"]["temp"]), flush=True)


if __name__ == "__main__":
    if "--auction" in sys.argv:
        do_auction(universe())
    else:
        main()
