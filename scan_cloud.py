# -*- coding: utf-8 -*-
"""
云端版扫描器（自包含、零第三方依赖、纯标准库）
---------------------------------------------
  python scan_cloud.py            # 扫描全市场，生成起爆信号
  python scan_cloud.py --auction  # 采集集合竞价（9:15~9:40）
输出：docs/data.json / docs/data.js / docs/auction.json
"""
import os, sys, io, json, time, re, urllib.request, urllib.parse, datetime
from collections import deque, defaultdict
from concurrent.futures import ThreadPoolExecutor

BASE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(BASE, "docs")
os.makedirs(DOCS, exist_ok=True)
HDR = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
       "Connection": "close", "Referer": "https://finance.sina.com.cn/"}
BARS = 200
WORKERS = 16


def get(url, enc="utf-8", tries=2):
    for i in range(tries):
        try:
            return urllib.request.urlopen(
                urllib.request.Request(url, headers=HDR), timeout=25).read().decode(enc, "ignore")
        except Exception:
            time.sleep(0.5 * (i + 1))
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


def _secid(sym):
    """sh600000 -> 1.600000 ; sz000001 -> 0.000001 ; bj830799 -> 0.830799"""
    s = (sym or "").lower()
    code = s[2:] if len(s) > 2 else s
    return ("1." if s.startswith("sh") else "0.") + code


EM_HDR = dict(HDR)
EM_HDR["Referer"] = "https://quote.eastmoney.com/"


def _klines_em(sym, tries=2):
    """东财日线：返回腾讯同构 [[date,o,c,h,l,vol], ...]，vol 单位=手"""
    u = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s"
         "&fields1=f1,f2,f3,f4,f5&fields2=f51,f52,f53,f54,f55,f56,f57"
         "&klt=101&fqt=1&end=20500101&lmt=%d" % (_secid(sym), BARS))
    for i in range(tries):
        try:
            t = urllib.request.urlopen(
                urllib.request.Request(u, headers=EM_HDR), timeout=20).read().decode("utf-8", "ignore")
            ks = ((json.loads(t) or {}).get("data") or {}).get("klines") or []
            out = []
            for line in ks:
                p = line.split(",")
                if len(p) >= 6:
                    out.append([p[0], float(p[1]), float(p[2]), float(p[3]),
                                float(p[4]), float(p[5])])
            if len(out) >= 20:
                return out
        except Exception:
            time.sleep(0.25 * (i + 1))
    return None


def _klines_tx(sym):
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


def klines(sym):
    """双源交错：代码偶数优先东财、奇数优先腾讯，失败互备。
    两个源各承担一半压力，单源限流时不至于全灭。"""
    if sum(ord(ch) for ch in sym) & 1:
        return _klines_tx(sym) or _klines_em(sym, tries=1)
    return _klines_em(sym, tries=1) or _klines_tx(sym)


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


# ---------------- 同花顺涨停池 / 异动解读 ----------------
THS_HDR = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Referer": "https://data.10jqka.com.cn/datacenterph/limitup/limtupInfo.html",
    "Accept": "application/json, text/plain, */*",
    "Connection": "close",
}
THS_FIELD = "199112,10,9001,330323,9002,330329,133971,133970,1968584,3475914,9004,9005"


def ths_get(path, date_str, extra=""):
    u = "https://data.10jqka.com.cn/dataapi/limit_up/%s?filter=HS,GEM2STAR&date=%s%s" % (
        path, date_str, extra)
    for i in range(2):
        try:
            t = urllib.request.urlopen(
                urllib.request.Request(u, headers=THS_HDR), timeout=25).read().decode("utf-8", "ignore")
            j = json.loads(t)
            if j.get("status_code") == 0:
                return j.get("data")
        except Exception:
            time.sleep(0.4 * (i + 1))
    return None


BLK_BAD_RE = re.compile(r"(预增|预减|扭亏|中报|年报|季报|高送转|送转|摘帽|破净|次新|低价|"
                        r"高股息|红利|融资融券|转债标的|股权激励|员工持股|增持|回购|"
                        r"机构重仓|基金重仓|QFII|社保|险资|参股银行|参股券商|参股保险)")



# ============================================================
# 龙虎榜：净买额 / 机构 / 游资 三判
# 回测 lhb_backtest.txt（21255 条 / 253 个交易日）：
#   全样本上榜       D1 -0.17% 胜率 45.8%   D20 -4.76%
#   净买占比 >15%    D1 +3.30% 胜率 65.3%   D20 +1.40%  ← 唯一 D20 还正
#   净买占比 8-15%   D1 +1.64% 胜率 57.4%
#   净卖占比 <0%     D1 -1.48% 胜率 38.1%
#   0家机构 -0.22% / 1家 -0.29% / 2家+ +0.00%  → 机构不加分
#   净买>15% 且 无机构  D1 +3.93% 胜率 67.9%  D20 +2.66%  ← 最强
#   净买>8%  且 有机构  D1 +1.57% 胜率 58.6%（弱于无机构的 +2.73%/61.6%）
#   主力做T          D1 -1.30% 胜率 39.7%  D20 -6.24%  ← 明确回避
#   买一主买          D1 +1.27% 胜率 53.3%
#   换手 >20%        D1 -0.71% 胜率 42.4%   D20 -6.57%
# ============================================================
LHB_REPORT = "RPT_DAILYBILLBOARD_DETAILSNEW"


def _fl(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _org_n(explain):
    m = re.search(r"(\d+)\s*家机构", explain or "")
    return int(m.group(1)) if m else 0


def _seat_kind(explain):
    e = explain or ""
    if "机构" in e:
        return "机构"
    if "主力做T" in e:
        return "主力做T"
    if "买一主买" in e:
        return "买一主买"
    if "普通席位" in e:
        return "普通席位"
    return "其他"


def _short_why(x):
    x = x or ""
    if "换手率" in x:
        return "高换手"
    if "跌幅偏离" in x:
        return "跌幅上榜"
    if "涨幅偏离" in x:
        return "涨幅上榜"
    if "振幅" in x:
        return "振幅上榜"
    if "累计达到20%" in x or "累计达到 20%" in x:
        return "三日累涨20%"
    return x[:8]



# ============================================================
# 游资席位识别
# 营业部 -> 游资代号；部分代号有公开可查的本人
# conf: open=公开可查 / folk=圈内流传
# ============================================================
HOTMONEY = [
    # ---- 具体席位必须放在笼统的"证券股份有限公司××"之前，否则会被吃掉 ----
    ("紫阳东路", "超短隔日", ["武汉紫阳东路"], "武汉帮代表席位（在武汉，不在杭州）"),
    ("知春路", "超短隔日", ["北京知春路"], "北京游资席位，偏超短"),
    ("溧阳路", "超短隔日", ["上海溧阳路"], "老牌超短席位"),
    ("金田路", "打板客", ["深圳金田路"], "深圳游资，打板为主"),
    ("上塘路", "超短隔日", ["杭州上塘路"], "杭州游资，一日游为主"),
    ("太平南路", "超短隔日", ["南京太平南路"], "南京游资席位"),
    ("北一环", "三板组", ["成都北一环"], "成都三板接力"),
    ("清扬路", "超短隔日", ["无锡清扬路"], "无锡游资"),
    ("五四路", "低吸派", ["福州五四路"], "福州游资，偏低吸"),
    ("花园石桥路", "外资券商", ["花园石桥路"], "瑞银老席位"),
    # 字段：(代号, 风格, 营业部关键词[], 备注)
    # 风格：超短隔日 / 三板组 / 撬板王 / 格局王 / 打板客 / 低吸派 / 首板挖掘
    #       量化通道 / 机构席位 / 北向资金 / 外资券商 / 散户型 / 波段派
    ("拉萨", "散户型",
     ["东方财富证券股份有限公司拉萨", "东方财富证券股份有限公司山南",
      "东方财富证券股份有限公司昌都", "东方财富证券股份有限公司林芝",
      "东方财富证券股份有限公司日喀则", "东方财富证券股份有限公司那曲"],
     "东财西藏各营业部，散户与量化混合"),
    ("江苏路", "超短隔日", ["长宁区江苏路", "江苏路证券营业部"], "上海长宁区江苏路席位，不是赵老哥"),
    ("浙江互联网", "量化通道", ["浙江互联网分公司"], "国信浙江互联网分公司"),
    ("机构通道", "机构席位",
     ["证券股份有限公司总部", "证券股份有限公司上海分公司",
      "证券股份有限公司北京分公司", "证券股份有限公司深圳分公司"],
     "券商总部/分公司席位，机构与量化混合"),
    ("绍兴路", "格局王",
     ["银河证券股份有限公司绍兴", "华鑫证券有限责任公司上海分公司",
      "华鑫证券有限责任公司上海徐汇区瑞平路"], "浙江绍兴系，偏大市值中线"),
    ("茅台路", "格局王",
     ["华鑫证券有限责任公司上海茅台路", "上海茅台路"], "上海茅台路，中线锁仓"),
    ("玉兰路", "超短隔日", ["南京玉兰路"], "华泰南京玉兰路"),
    ("红岭路", "超短隔日", ["深圳红岭中路", "红岭路"], "国泰海通深圳红岭中路"),
    ("成都", "三板组",
     ["成都科华北路", "国金证券股份有限公司成都", "成都天府大道"], "成都系接力"),
    ("佛山", "打板客",
     ["佛山绿景路", "佛山普澜二路", "证券股份有限公司佛山"], "佛山无影脚，打板即走"),
    ("温州", "撬板王",
     ["证券股份有限公司温州", "温州大南路", "温州车站大道"], "温州系，撬板与对倒"),
    ("宁波桑田路", "打板客", ["宁波桑田路"], "华鑫宁波桑田路"),
    ("湖里大道", "超短隔日", ["厦门湖滨南路", "湖里大道"], "华福厦门湖滨南路"),
    ("思明南路", "低吸派",
     ["厦门思明南路", "思明南路"], "厦门思明南路一带，回调低吸"),
    ("欢乐海岸", "格局王", ["益田路荣超商务中心"], "华泰深圳益田路荣超商务中心"),
    ("句容", "超短隔日", ["镇江句容", "句容华阳"], "华泰镇江句容"),
    ("宁波华鑫", "打板客", ["华鑫证券有限责任公司宁波分公司"], "华鑫宁波分公司"),
    ("望京", "撬板王", ["北京望京"], "银河北京望京，跌停撬板"),
    ("世纪大道", "格局王",
     ["中信证券股份有限公司上海世纪大道", "上海世纪大道"], "上海世纪大道，大资金中线"),
    ("宛平南路", "超短隔日", ["宛平南路"], "华鑫上海宛平南路"),
    ("苏州", "三板组",
     ["证券股份有限公司苏州", "苏州工业园区"], "苏州系，高位接力"),
    ("杭州", "超短隔日",
     ["证券股份有限公司杭州", "杭州庆春路"], "杭州系"),
    ("广州", "低吸派",
     ["证券股份有限公司广州", "广州天河北路"], "广州系，回调低吸"),
    ("低位挖掘", "首板挖掘", [], "低位首板型席位，无固定营业部"),
    ("宁波解放南路", "超短隔日", ["宁波解放南路"], "银河宁波解放南路，老牌超短"),
    ("深圳华强北", "打板客", ["深圳华强北路", "华强北路"], "华强北系打板"),
    ("北向", "北向资金", ["深股通专用", "沪股通专用"], "香港中央结算"),
    ("外资", "外资券商",
     ["高盛", "瑞银证券", "摩根大通", "摩根士丹利", "野村", "汇丰"], "外资券商席位"),
    ("机构专用", "机构席位", ["机构专用"], "公募/私募/保险专用席位"),
]

STYLE_CLS = {
    "超短隔日": "s1", "三板组": "s2", "撬板王": "s3", "格局王": "s4",
    "打板客": "s5", "低吸派": "s6", "首板挖掘": "s7", "量化通道": "s8",
    "机构席位": "s9", "北向资金": "s10", "外资券商": "s11", "散户型": "s12",
    "波段派": "s13",
}


def _hm_load():
    """内置表打底 + docs/hotmoney.json 覆盖/追加
    （旧文件不会再把内置新增席位"回退"掉）"""
    out = [(a, b, list(c), d) for (a, b, c, d) in HOTMONEY]
    p = os.path.join(DOCS, "hotmoney.json")
    try:
        arr = json.load(io.open(p, encoding="utf-8"))
    except Exception:
        return out
    idx = {t[0]: i for i, t in enumerate(out)}
    for it in (arr or []):
        tag = (it.get("tag") or "").strip()
        keys = it.get("keys") or []
        if not tag or not keys:
            continue
        rec = (tag, it.get("style") or "", keys, it.get("note") or "")
        if tag in idx:
            out[idx[tag]] = rec
        else:
            out.append(rec)
            idx[tag] = len(out) - 1
    return out


_HM = None


def hm_match(dept):
    """营业部全名 -> (tag, style, note) 或 None"""
    global _HM
    if _HM is None:
        _HM = _hm_load()
    d = dept or ""
    if not d:
        return None
    if "机构专用" in d:
        return ("机构专用", "机构席位", "公募/私募/保险专用席位")
    for tag, style, keys, note in _HM:
        for k in keys:
            if k and k in d:
                return (tag, style, note)
    return None


def _dept_short(d):
    """营业部简称：去掉「证券股份有限公司」「证券营业部」等"""
    x = d or ""
    for w in ("证券股份有限公司", "证券有限责任公司", "股份有限公司",
              "证券营业部", "证券分公司", "有限公司", "证券", "营业部", "分公司"):
        x = x.replace(w, "")
    return x.strip()


def lhb_seats(date_str, want=("BUY", "SELL")):
    """返回 {code: dict(buy=[...], sell=[...])}，每条含 dept/short/amt/tag/real/conf"""
    out = {}
    for side in want:
        rep = ("RPT_BILLBOARD_DAILYDETAILSBUY" if side == "BUY"
               else "RPT_BILLBOARD_DAILYDETAILSSELL")
        for pg in (1, 2, 3, 4):
            got = _em_page(rep, "(TRADE_DATE='%s')" % date_str, pg, 500)
            if not got:
                break
            for r in got:
                code = str(r.get("SECURITY_CODE") or "")
                if not code or code.startswith(("900", "200")):
                    continue
                dept = (r.get("OPERATEDEPT_NAME") or "").strip()
                amt = _fl(r.get("BUY") if side == "BUY" else r.get("SELL")) / 1e4
                net = _fl(r.get("NET")) / 1e4
                m = hm_match(dept)
                item = dict(dept=dept, short=_dept_short(dept),
                            amt=round(amt, 0), net=round(net, 0),
                            tag=m[0] if m else "", style=m[1] if m else "",
                            note=m[2] if m else "")
                key = "buy" if side == "BUY" else "sell"
                d = out.setdefault(code, {"buy": [], "sell": []})
                d[key].append(item)
            if len(got) < 500:
                break
    return out


def _em_page(rep, flt, page, size):
    q = {"reportName": rep, "columns": "ALL", "pageNumber": str(page),
         "pageSize": str(size), "source": "WEB", "client": "WEB", "filter": flt}
    txt = em_get("https://datacenter-web.eastmoney.com/api/data/v1/get?" +
                 urllib.parse.urlencode(q))
    if not txt:
        return []
    try:
        j = json.loads(txt)
    except Exception:
        return []
    return (j.get("result") or {}).get("data") or []


def lhb_dept_rank(seats, name_map, top=20):
    """按营业部聚合当日买卖 -> 净买排行（复刻打板客网那张图）"""
    agg = {}
    for code, sn in seats.items():
        nm = name_map.get(code, code)
        for key in ("buy", "sell"):
            for it in sn.get(key, []):
                k = it.get("dept") or ""
                if not k:
                    continue
                a = agg.get(k)
                if a is None:
                    a = agg[k] = dict(dept=k, short=it.get("short") or k,
                                      tag=it.get("tag") or "",
                                      style=it.get("style") or "",
                                      buy=0.0, sell=0.0, net=0.0, items=[], _cells={})
                amt = float(it.get("amt") or 0)
                if key == "buy":
                    a["buy"] += amt
                else:
                    a["sell"] += amt
                kk = (code, key)
                cell = a.setdefault("_cells", {}).get(kk)
                if cell is None:
                    a["_cells"][kk] = dict(c=code, n=nm, amt=amt,
                                           s="买" if key == "buy" else "卖")
    for a in agg.values():
        a["net"] = round(a["buy"] - a["sell"])
        a["buy"] = round(a["buy"])
        a["sell"] = round(a["sell"])
        cells = list((a.pop("_cells", None) or {}).values())
        for c in cells:
            c["amt"] = round(c["amt"])
        a["items"] = sorted(cells, key=lambda x: -x["amt"])[:6]
        a["n"] = len(cells)
        # 未映射到游资的席位：按交易特征自动归类（每天自动判定，无需维护名单）
        if not a.get("style"):
            bc = [c for c in cells if c["s"] == "买"]
            nb = len(bc)
            top1 = max((c["amt"] for c in bc), default=0)
            if a["net"] >= 1e4 and nb >= 3:   # net 单位＝万元，1e4 即 1 亿
                a["style"] = "疑似分仓"
            elif a["net"] >= 1e4 and nb <= 2 and a["buy"] > 0 and top1 / a["buy"] >= 0.7:
                a["style"] = "单票重仓"
            elif a["net"] <= -1e4 and nb == 0:
                a["style"] = "单边砸盘"
    arr = [x for x in agg.values() if x["net"] != 0]
    arr.sort(key=lambda x: -abs(x["net"]))

    # 只留「频繁 + 大资金」的席位，但**已知游资席位一律保留**（哪怕当天只上榜 1 只）
    #   已知席位：命中 HOTMONEY 的（紫阳东路 / 知春路 / 江苏路 ...）
    #   未识别营业部：要求 上榜>=8只 且 买入>=1.5亿，或 净额>=3亿
    def _active(a, nc=8, na=15000.0, nn=30000.0):
        return (a["n"] >= nc and a["buy"] >= na) or (abs(a["net"]) >= nn and a["n"] >= 2)

    act = []
    for x in arr:
        if x.get("tag"):                                  # 已知游资席位：保留
            if x["n"] >= 2 or abs(x["net"]) >= 1000.0:    # 但滤掉几乎没量的挂单
                act.append(x)
        elif _active(x):                                  # 未识别：只留大资金
            act.append(x)
    if len(act) < 8:                                      # 冷清日放宽
        act = [x for x in arr if x.get("tag") or _active(x, 5, 8000.0, 15000.0)]
    # 最后一道闸：只留「有量」的 —— 上榜>=3 只 或 净额>=3000万
    # （这样"只上榜1只但砸了3亿"的席位留得住，蹭边的 1 只小单被滤掉）
    # 聚合型标签（拉萨/杭州/机构通道…）走门槛；精确地名席位（紫阳东路/知春路…）一律保留
    GROUP_TAG = {"拉萨", "杭州", "苏州", "广州", "成都", "佛山", "温州",
                 "浙江互联网", "机构通道", "北向", "外资", "机构专用", ""}
    out = [x for x in act
           if (x.get("tag") not in GROUP_TAG)          # 精确游资席位：留
           or x["n"] >= 3 or abs(x["net"]) >= 3000.0]
    if len(out) < 10:                       # 冷清日放宽
        out = [x for x in act if (x["n"] >= 2 or abs(x["net"]) >= 1000.0)]
    out.sort(key=lambda x: -abs(x["net"]))
    print("龙虎榜营业部：全部 %d -> 活跃 %d -> 输出 %d"
          % (len(arr), len(act), len(out)), flush=True)
    return out[:top]


def lhb_today(date_str):
    """返回 (rows 全部上榜, top 强信号)，失败返回 ([], [])"""
    date_str = _norm_date(date_str)
    q = {"reportName": LHB_REPORT, "columns": "ALL", "pageNumber": "1",
         "pageSize": "500", "source": "WEB", "client": "WEB",
         "filter": "(TRADE_DATE='%s')" % date_str}
    txt = em_get("https://datacenter-web.eastmoney.com/api/data/v1/get?" +
                 urllib.parse.urlencode(q))
    if not txt:
        return [], []
    try:
        j = json.loads(txt)
    except Exception:
        return [], []
    raw = (j.get("result") or {}).get("data") or []
    # 同一只股票可能因多个原因上榜（涨幅榜+换手榜），按 code 去重
    # 规则：保留净买占比绝对值最大的一行，上榜原因合并
    best = {}
    for r in raw:
        cc = str(r.get("SECURITY_CODE") or "")
        if not cc:
            continue
        nr = _fl(r.get("DEAL_NET_RATIO"))
        w = _short_why(r.get("EXPLANATION"))
        if cc in best:
            o = best[cc]
            if abs(nr) > abs(_fl(o.get("DEAL_NET_RATIO"))):
                o["_why2"] = o.pop("_why2", w)
                best[cc] = r
                best[cc]["_why2"] = o.get("_why2")
            elif w not in (best[cc].get("_why2") or ""):
                best[cc]["_why2"] = (best[cc].get("_why2") or "") + "/" + w
        else:
            best[cc] = r
    raw = list(best.values())

    rows = []
    for r in raw:
        code = str(r.get("SECURITY_CODE") or "")
        if not code:
            continue
        if code.startswith(("900", "200")):   # B 股，成交额畸小会虚高净买占比
            continue
        net = _fl(r.get("BILLBOARD_NET_AMT")) / 1e4
        nrat = _fl(r.get("DEAL_NET_RATIO"))
        brat = _fl(r.get("DEAL_AMOUNT_RATIO"))
        hs = _fl(r.get("TURNOVERRATE"))
        org = _org_n(r.get("EXPLAIN"))
        seat = _seat_kind(r.get("EXPLAIN"))
        tags = []
        if nrat >= 15 and org == 0:
            tags.append("游资抢筹")
        elif nrat >= 15:
            tags.append("龙虎强买")
        elif nrat >= 8:
            tags.append("龙虎净买")
        if org >= 1:
            tags.append("机构买")
        if seat == "主力做T":
            tags.append("主力做T")
        elif seat == "买一主买":
            tags.append("买一主买")
        if nrat < 0:
            tags.append("龙虎净卖")
        if hs >= 20:
            tags.append("高换手")
        mrate = re.search(r"成功率([\d.]+)%", r.get("EXPLAIN") or "")
        rows.append(dict(c=code, n=r.get("SECURITY_NAME_ABBR") or "",
                         chg=round(_fl(r.get("CHANGE_RATE")), 2),
                         net=round(net, 0), nrat=round(nrat, 2), brat=round(brat, 2),
                         org=org, seat=seat,
                         why=(_short_why(r.get("EXPLANATION")) + (r.get("_why2") or "")),
                         hs=round(hs, 2),
                         buy=round(_fl(r.get("BILLBOARD_BUY_AMT")) / 1e4, 0),
                         sell=round(_fl(r.get("BILLBOARD_SELL_AMT")) / 1e4, 0),
                         rate=float(mrate.group(1)) if mrate else None,
                         tags=tags))
    # 合并游资席位（一次拉全市场买卖明细，按 code 分配）
    try:
        seats = lhb_seats(date_str)
    except Exception as e:
        print("  席位明细失败 %s" % e, flush=True)
        seats = {}
    for row in rows:
        sn = seats.get(row["c"])
        row["hm"] = []
        row["hm_seat"] = ""
        row["hm_style"] = ""
        row["hm_amt"] = 0
        row["hm_amts"] = {}
        row["hm_tag"] = ""
        if not sn:
            continue
        buys = [x for x in sn.get("buy", []) if x.get("tag")]
        buys.sort(key=lambda x: -x["amt"])
        tags = []
        for x in buys:
            if x["tag"] not in tags:
                tags.append(x["tag"])
        row["hm"] = tags[:4]
        for x in buys:
            row["hm_amts"][x["tag"]] = row["hm_amts"].get(x["tag"], 0) + x["amt"]
        if buys:
            b = buys[0]
            row["hm_tag"] = b["tag"]
            row["hm_seat"] = b["short"]
            row["hm_style"] = b["style"]
            row["hm_amt"] = b["amt"]

    # 营业部净买排行
    nmap = {r["c"]: r["n"] for r in rows}
    try:
        drank = lhb_dept_rank(seats, nmap, top=45)
    except Exception:
        drank = []

    STRONG = ("游资抢筹", "龙虎强买", "龙虎净买")
    top = [x for x in rows if any(t in x["tags"] for t in STRONG)]
    top.sort(key=lambda x: -x["nrat"])
    return rows, top, drank


def _norm_date(d):
    """把 20261117 / 2026-11-17 / 2026/11/17 统一成 2026-11-17"""
    d = (d or "").strip()
    for f in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return datetime.datetime.strptime(d, f).strftime("%Y-%m-%d")
        except Exception:
            continue
    return bjnow().strftime("%Y-%m-%d")


def lhb_latest(dstr=None, back=4):
    """找最近一个有龙虎榜数据的交易日。
    龙虎榜在收盘后 18:00 左右才披露 -> 当天 18 点前自动从上一交易日找起。
    返回 (trade_date, rows, top, dept_rank)"""
    now = bjnow()
    dstr = _norm_date(dstr) if dstr else now.strftime("%Y-%m-%d")
    start = 0
    if dstr == now.strftime("%Y-%m-%d") and now.hour < 18:
        start = 1
    base = datetime.datetime.strptime(dstr, "%Y-%m-%d")
    for i in range(start, start + back + 1):
        d = (base - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
        rows, top, dr = lhb_today(d)
        if rows:
            return d, rows, top, dr
        time.sleep(0.2)
    return "", [], [], []


def do_lhb():
    """单独跑一次龙虎榜，写 docs/lhb.json（每晚 20:00 由 lhb.yml 触发，约 10 秒）"""
    out = {}
    try:
        trade_date, rows, top, dr = lhb_latest()
        b = lhb_brief(rows)
        out = dict(trade_date=trade_date, rows=rows, top=top[:40],
                   brief=b, dept=dr, n=len(rows),
                   ts=bjnow().strftime("%Y-%m-%d %H:%M:%S"))
        print("龙虎榜 %s：上榜 %d 只 / 强信号 %d / 营业部 %d"
              % (trade_date or "无", len(rows), len(top), len(dr)), flush=True)
    except Exception as e:
        print("龙虎榜失败 %s" % e, flush=True)
        out = dict(trade_date="", rows=[], top=[], brief={}, dept=[], n=0,
                   ts=bjnow().strftime("%Y-%m-%d %H:%M:%S"), error=str(e))
    p = os.path.join(DOCS, "lhb.json")
    with open(p, "w", encoding="utf-8") as f:
        f.write(json.dumps(out, ensure_ascii=False))
    print("  写入 %s (%d 字节)" % (p, os.path.getsize(p)), flush=True)


def lhb_brief(rows):
    if not rows:
        return dict(mood="今日无龙虎榜数据", tone="g", line="", youzi=0, jigou=0,
                    strong=0, buy=0, sell=0)
    youzi = sum(1 for x in rows if "游资抢筹" in x["tags"])
    jigou = sum(1 for x in rows if "机构买" in x["tags"])
    strong = sum(1 for x in rows if x["nrat"] >= 15)
    nbuy = sum(1 for x in rows if x["nrat"] > 0)
    nsell = len(rows) - nbuy
    if youzi >= 3 and nbuy > nsell:
        mood, tone = "游资主导 · 资金净流入", "r"
    elif jigou >= len(rows) * 0.4:
        mood, tone = "机构接盘为主 · 游资缺位", "y"
    elif nsell > nbuy:
        mood, tone = "整体净流出", "g"
    else:
        mood, tone = "游资机构分歧", "y"
    line = ("上榜 %d 只，净买 %d / 净卖 %d；净买占比≥15%% 的 %d 只，"
            "其中纯游资抢筹 %d 只、含机构 %d 只" % (len(rows), nbuy, nsell, strong, youzi, jigou))
    # 活跃游资榜（按买入金额）
    act = {}
    for r in rows:
        for t in (r.get("hm") or []):
            if t in ("机构专用", "北向", "外资", "机构通道"):
                continue
            a = act.setdefault(t, dict(tag=t, n=0, amt=0, style=""))
            a["n"] += 1
            a["amt"] += (r.get("hm_amts") or {}).get(t, 0)
            if r.get("hm_tag") == t and r.get("hm_style"):
                a["style"] = r["hm_style"]
    hot = sorted(act.values(), key=lambda x: -x["amt"])[:8]
    return dict(mood=mood, tone=tone, line=line, youzi=youzi, jigou=jigou,
                strong=strong, buy=nbuy, sell=nsell, hot=hot)


def ths_zt_detail(d):
    """涨停池 + 板块异动解读。返回 (zt_map, blocks, detail)"""
    zt_map = {}
    d0 = ths_get("limit_up_pool", d,
                 "&page=1&limit=200&field=" + THS_FIELD + "&order_field=330329&order_type=0")
    for it in ((d0 or {}).get("info") or []):
        zt_map[str(it.get("code"))] = dict(
            name=it.get("name") or "",
            reason=(it.get("reason_type") or "").strip(),
            hd=it.get("high_days") or "",
            seal=round((it.get("order_amount") or 0) / 1e8, 2),
            opn=int(it.get("open_num") or 0),
            hs=round(float(it.get("turnover_rate") or 0), 2),
            cval=float(it.get("currency_value") or 0),
            again=int(it.get("is_again_limit") or 0))
    blocks, detail = [], {}
    for b in (ths_get("block_top", d) or []):
        bd = dict(name=b.get("name") or "", n=int(b.get("limit_up_num") or 0),
                  cont=int(b.get("continuous_plate_num") or 0),
                  high=b.get("high") or "", days=int(b.get("days") or 0),
                  names=[], codes=[], stocks=[])
        for s in (b.get("stock_list") or []):
            c = str(s.get("code"))
            nm = s.get("name") or ""
            if len(bd["names"]) < 6:
                bd["names"].append(nm)
            if len(bd["codes"]) < 6:
                bd["codes"].append(c)
            bd["stocks"].append(dict(c=c, n=nm, lb=int(s.get("continue_num") or 1),
                                     chg=round(float(s.get("change_rate") or 0), 2)))
            ri = (s.get("reason_info") or "").strip()
            if ri and c not in detail:
                ri = ri.split("（免责声明")[0].strip()
                ri = ri.replace("\n", " ")
                detail[c] = ri[:1200]
        if bd["n"] >= 2 and not BLK_BAD_RE.search(bd["name"]):
            blocks.append(bd)
    blocks.sort(key=lambda x: (-x["n"], -x["cont"]))
    return zt_map, blocks, detail


# ---------------- 字辈 / 名字炒作 ----------------
ZODIAC = "鼠牛虎兔龙蛇马羊猴鸡狗猪"
NUM_CHARS = "三四五六七八九一二"
PLAY_CHARS = ("麒麟", "凤凰", "锂", "熊", "鹰")  # 只留罕见字，常规字噪音太大
# 常见谐音/影射梗（可自行扩充 docs/playwords.json）
DEFAULT_KW = [
    dict(kw="川大智胜", group="谐音梗·特朗普", pos="any"),
    dict(kw="哈尔斯", group="谐音梗·哈里斯", pos="any"),
    dict(kw="登云", group="谐音梗·拜登", pos="any"),
    dict(kw="海能达", group="谐音梗·还能打", pos="any"),
    dict(kw="澳柯玛", group="谐音梗·奥巴马", pos="any"),
    dict(kw="三羊马", group="谐音梗·三羊开泰", pos="any"),
    dict(kw="兔宝宝", group="谐音梗·兔年", pos="any"),
]


def load_playwords():
    """docs/playwords.json 可选：[{"kw":"羊","group":"羊字辈","pos":"any"}]"""
    lst = list(DEFAULT_KW)
    fp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "playwords.json")
    try:
        with open(fp, encoding="utf-8") as f:
            extra = json.load(f)
        if isinstance(extra, list):
            lst += [x for x in extra if x.get("kw")]
    except Exception:
        pass
    return lst


def wordplay(uni, pool_map):
    """uni: [{code,name}] ; pool_map: {code:(name,lb)} → 字辈/名字炒作聚合"""
    custom = load_playwords()
    # (关键词, 组名, 位置 any|start, 是否严格: 命中少于2家时需要占比达标)
    rules = []
    for ch in ZODIAC:
        rules.append((ch, ch + "字辈", "any", 0))
    for ch in NUM_CHARS:
        rules.append((ch, ch + "字头", "start", 0))
    for ch in PLAY_CHARS:
        rules.append((ch, "含「%s」" % ch, "any", 1))
    for it in custom:
        rules.append((it["kw"], it.get("group") or ("含「%s」" % it["kw"]),
                      it.get("pos") or "any", int(it.get("strict", 1))))

    seen = {}
    for s in uni:
        nm = s.get("name") or ""
        if len(nm) < 2:
            continue
        for kw, grp, pos, strict in rules:
            hit = nm.startswith(kw) if pos == "start" else (kw in nm)
            if hit:
                mp = seen.setdefault(grp, {"strict": strict, "d": {}})
                mp["d"][s.get("code")] = s

    rows = []
    for g, mp in seen.items():
        lst = list(mp["d"].values())
        tot = len(lst)
        if tot < 2:
            continue
        hit = [(s["code"],) + tuple(pool_map[s["code"]])
               for s in lst if s.get("code") in pool_map]
        if not hit:
            continue
        pct = 100.0 * len(hit) / tot
        if mp["strict"] and len(hit) < 2 and pct < 15:
            continue
        rows.append(dict(name=g, total=tot, n=len(hit),
                         max_lb=max(h[2] for h in hit),
                         pct=round(100.0 * len(hit) / tot, 1),
                         names=[h[1] for h in hit][:8],
                         stocks=[dict(c=h[0], n=h[1], lb=h[2],
                                      chg=round(h[3] if len(h) > 3 else 0, 2))
                                 for h in hit]))
    rows.sort(key=lambda x: (-x["n"], -x["max_lb"], -x["pct"]))
    return rows[:12]


CONCEPT_BLACK = {
    "融资融券", "沪股通", "深股通", "MSCI中国", "富时罗素", "标准普尔",
    "上证180_", "上证50_", "沪深300_", "中证500_", "创业板综", "转债标的",
    "预盈预增", "预亏预减", "股权激励", "员工持股", "高送转", "破净股",
    "AH股", "B股", "GDR", "机构重仓", "基金重仓", "QFII重仓", "社保重仓",
    "券商重仓", "保险重仓", "信托重仓", "参股银行", "参股券商", "参股保险",
    "参股期货", "举牌", "并购重组", "壳资源", "ST股", "次新股", "低价股",
    "高股息", "红利股", "昨日涨停", "昨日连板", "昨日触板",
    "昨日涨停_含一字", "昨日连板_含一字",
    # 东财"工具箱"类统计标签，无炒作含义
    "昨日高振幅", "今日高振幅", "高振幅", "东方财富热股", "最近多板",
    "百日新高", "近期新高", "历史新高", "连续亏损", "亏损股",
    "破发股", "破增发价股", "high市净率", "高市净率", "低市净率",
    "题材股", "高换手", "活跃股", "人气股", "热门股", "超跌股",
    "同花顺热股", "通达信热股", "热度榜", "龙虎榜",
}
import re as _re
_CONCEPT_BAD_RE = _re.compile(
    r"(中报|年报|季报|一季|三季|业绩|预增|预减|扭亏|快报|预告|"
    r"增仓|减仓|新进|QFII|社保|险资|信托|盘股|摘帽|市盈率|盘成长|盘价值)")
_CONCEPT_BAD_PREFIX = _re.compile(r"^(昨日|今日|近期|百日|历史|连续|破)")
_REGION_RE = _re.compile(
    r"(北京|上海|天津|重庆|广东|江苏|浙江|山东|河南|河北|湖南|湖北|四川|福建|"
    r"安徽|江西|陕西|山西|辽宁|吉林|黑龙江|云南|贵州|广西|甘肃|青海|宁夏|"
    r"新疆|西藏|内蒙|海南|深圳|香港|澳门)")


def _concept_ok(nm):
    if not nm or nm in CONCEPT_BLACK:
        return False
    if _CONCEPT_BAD_RE.search(nm):
        return False
    if _CONCEPT_BAD_PREFIX.search(nm):
        return False
    if _re.match(r"^(19|20)\d{2}", nm):
        return False
    if _REGION_RE.search(nm) and ("特区" in nm or "板块" in nm or len(nm) <= 4):
        return False
    return True


def stock_concepts(code):
    """单只股票的东财概念列表（失败返回空）"""
    u = ("https://datacenter-web.eastmoney.com/api/data/v1/get?"
         "reportName=RPT_F10_CORETHEME_BOARDTYPE"
         "&columns=SECUCODE,SECURITY_CODE,BOARD_NAME,BOARD_CODE"
         "&filter=(SECURITY_CODE%3D%22" + code + "%22)"
         "&pageNumber=1&pageSize=200")
    t = em_get(u, timeout=12)
    if not t:
        return []
    try:
        rows = (json.loads(t).get("result") or {}).get("data") or []
        return [r.get("BOARD_NAME") for r in rows if _concept_ok(r.get("BOARD_NAME"))]
    except Exception:
        return []


def concept_top(pool_stocks, limit=14):
    """pool_stocks: [{code,name,lb}] → 概念聚合"""
    agg = {}

    def work(it):
        for nm in stock_concepts(it["code"]):
            a = agg.setdefault(nm, dict(n=0, lb=0, names=[], stocks=[]))
            a["n"] += 1
            a["lb"] = max(a["lb"], it["lb"])
            if len(a["names"]) < 4:
                a["names"].append(it["name"])
            a["stocks"].append(dict(c=it["code"], n=it["name"], lb=it["lb"],
                                    chg=round(it.get("chg") or 0, 2)))

    try:
        with ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(work, pool_stocks[:60]))
    except Exception:
        pass
    rows = [dict(name=k, n=v["n"], max_lb=v["lb"], names=v["names"], stocks=v["stocks"])
            for k, v in agg.items() if v["n"] >= 2]
    rows.sort(key=lambda x: (-x["n"], -x["max_lb"]))
    return rows[:limit]


SCON_PATH = os.path.join(DOCS, "scon.json")


def load_scon():
    """读概念缓存，超过 10 天的丢"""
    try:
        d = json.load(io.open(SCON_PATH, encoding="utf-8"))
    except Exception:
        return {}
    keep, now = {}, bjnow()
    for c, e in (d or {}).items():
        try:
            days = (now - datetime.datetime.strptime(e.get("d") or "2000-01-01",
                                                     "%Y-%m-%d")).days
        except Exception:
            days = 99
        if days <= 10:
            keep[c] = e
    return keep


def save_scon(d):
    try:
        json.dump(d, io.open(SCON_PATH, "w", encoding="utf-8"), ensure_ascii=False)
        print("  scon 已缓存 %d 只 -> docs/scon.json" % len(d), flush=True)
    except Exception as e:
        print("  scon 写入失败 %s" % e, flush=True)


def scon_bulk(pairs, workers=8):
    """pairs: [(code, name)] → {code: {n, cs, d}}"""
    out, today = {}, bjnow().strftime("%Y-%m-%d")

    def work(p):
        c, n = p
        cs = stock_concepts(c)
        if cs:
            out[c] = dict(n=n, cs=cs, d=today)

    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(work, pairs))
    except Exception as e:
        print("  scon_bulk 失败 %s" % e, flush=True)
    return out


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
                                  chg=float(fl[32] or 0),
                                  tov=float(fl[38] or 0),   # idx38 = 换手率(%)
                                  hi=float(fl[33] or 0),    # idx33 = 当日最高
                                  lo=float(fl[34] or 0))    # idx34 = 当日最低
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
    dea = ema(dif, 9)
    bar = dif[t] - dea[t]
    # VBASE 对齐同花顺 VBASE:=REF(MA(VOL,20),1) —— 不含当日，否则放量日分母抬高、量比偏小
    def vb_at(i):
        return Z(vma20, i - 1) if i >= 1 else 0.0
    vbase = vb_at(t)
    hsx = v[t] / vbase if vbase > 0 else 99  # 量比 = 当日量 / 前20日均量

    # ---- 板性反推涨停价（v13.4：ST 已 10%，只按涨幅分三档）----
    cgr = [0.0] * n
    for i in range(1, n):
        cgr[i] = c[i] / c[i - 1] - 1
    mxg = max(cgr[max(1, t - 249):t + 1])
    lim = 1.30 if mxg > 0.25 else (1.20 if mxg > 0.15 else 1.10)
    def zt_at(i):
        return i >= 1 and c[i] >= round(c[i - 1] * lim * 100 + 0.5) / 100 - 0.010
    ztc = [zt_at(i) for i in range(n)]
    fb = [i >= 1 and ztc[i] and not ztc[i - 1] for i in range(n)]

    # ---- 一爆（v13.7 放宽到 5% / 1.5 倍）----
    def boom(i):
        if i < 1:
            return False
        vb = vb_at(i)
        return (c[i] / c[i - 1] - 1) >= 0.05 and vb > 0 and v[i] >= vb * 1.5 and c[i] > Z(m20, i)
    BOOM = boom(t) and hsx < 12
    has30 = any(boom(i) for i in range(max(1, t - 29), t + 1))
    has40 = any(boom(i) for i in range(max(1, t - 39), t + 1))

    # ---- 二爆：深洗 / 强洗 ----
    maf = min(Z(m5, t), Z(m10, t), Z(m20, t), Z(m60, t))
    deep = has30 and Z(ll30, t) < maf
    strong = has30 and Z(ll30, t) > Z(m20, t)
    cross5 = c[t] > Z(m5, t) and c[t - 1] <= Z(m5, t - 1)
    A2 = deep and cross5 and v[t] >= vbase * 1.3
    B2 = strong and c[t] > Z(hh20, t - 1) and v[t] >= vbase * 1.8 and chg >= 0.04
    hb = dif[t] > 0
    B2L = (A2 or B2) and hb and not BOOM

    # ---- 二板 / 加仓 ----
    C2 = chg >= 0.095 and chg_p >= 0.095 and hsx < 8
    DUO = Z(m5, t) > Z(m10, t) > Z(m20, t) > Z(m60, t)
    UPX = Z(m5, t) > Z(m5, t - 1) and Z(m20, t) > Z(m20, t - 1) and Z(m60, t) > Z(m60, t - 1)
    ADDX = C2 and DUO and UPX and hsx < 8
    C2L = C2 and not B2L and not BOOM

    # ---- 生命线起爆 ----
    SHRINK = Z(vma5, t - 1) > 0 and Z(vma60, t) > 0 and Z(vma5, t - 1) < Z(vma60, t) * 0.85
    HOLD = lm[t] is not None and Z(ll20, t) > Z(lm, t) > Z(lm, t - 1)
    LIFE2 = has40 and HOLD and SHRINK and c[t] > Z(hh20, t - 1) and \
        v[t] >= vbase * 1.8 and chg >= 0.04

    # ---- 清仓 / 新低三档 ----
    CUT = c[t] < Z(m20, t) and c[t - 1] < Z(m20, t - 1) and c[t - 2] < Z(m20, t - 2) and \
        c[t - 3] >= Z(m20, t - 3)
    XL = l[t] <= Z(ll60, t)
    nlv = vbase > 0 and v[t] >= vbase * 1.5
    nls = vbase > 0 and v[t] <= vbase * 0.80
    nlflat = Z(m20, t - 5) > 0 and abs(Z(m20, t) / Z(m20, t - 5) - 1) < 0.015
    PIT = XL and nlv and chg <= -0.05
    GRIND = XL and not PIT and nls and bar > (dif[t - 1] - dea[t - 1]) and nlflat
    NLNX = XL and not PIT and not GRIND

    # ---- 横盘突破箱体 ----
    plat_w = 0.0
    if hh60[t - 1] and ll60[t - 1] and ll60[t - 1] > 0:
        plat_w = (hh60[t - 1] - ll60[t - 1]) / ll60[t - 1]
    BOX = (0 < plat_w <= 0.30 and c[t] > Z(hh60, t - 1)
           and v[t] >= Z(vma60, t - 1) * 2.5 and chg >= 0.04)

    # ---- 首板平台突破（v13.2 掩码法）----
    fbs = [i for i in range(max(1, t - 15), t + 1) if fb[i]]
    BRK_STR = BRK_MID = P_WEAK = False
    if fbs:
        f = fbs[-1]
        nbf = t - f
        onefb = len(fbs) == 1
        WIN = 5 <= nbf <= 15 and onefb
        seg_h = h[f + 1:t] if t - 1 >= f + 1 else []
        seg_l = l[f + 1:t] if t - 1 >= f + 1 else []
        if WIN and seg_h:
            plat_h, plat_l = max(seg_h), min(seg_l)
            fbc, fbl = c[f], l[f]
            p_str = plat_l >= fbc
            p_mid = plat_l < fbc and plat_l >= fbl
            P_WEAK = plat_l < fbl and c[t] < fbl
            shrink_p = Z(vma5, t - 1) > 0 and Z(vma5, t - 1) <= vbase * 0.90
            if shrink_p and c[t] > plat_h and hsx < 12:
                if p_str and v[t] >= vbase * 1.5:
                    BRK_STR = True
                elif p_mid and v[t] >= vbase * 1.8:
                    BRK_MID = True

    # ---- 炸板 ----
    ztj_t = round(c[t - 1] * lim * 100 + 0.5) / 100
    ZBREAK = h[t] >= ztj_t - 0.010 and c[t] < ztj_t - 0.010

    # ---- 顶部预警（严格版：涨50% 且 站上120日高点90%）----
    preg = c[t] / Z(ll60, t - 1) if Z(ll60, t - 1) > 0 else 1.0
    TOPH = preg >= 1.50 and c[t] >= Z(hh120, t) * 0.90
    bgv = v[t] >= Z(vma20, t - 1) * 2.0 and v[t] >= max(v[max(0, t - 19):t + 1])
    SHOT = TOPH and bgv and c[t] < o[t] and (o[t] - c[t]) / o[t] >= 0.04 and chg <= -0.03
    STL = (TOPH and c[t] >= max(c[max(0, t - 59):t + 1]) * 0.97 and chg < 0.015
           and v[t] >= Z(vma20, t - 1) * 1.5 and (h[t] - c[t]) / c[t] >= 0.02 and h[t] > o[t])

    def last_cross(up, span=90):
        # 从 t-1 往前找"上一次"交叉；起点若写 t，今天恰好交叉时 t-i==0 会被区间判断排掉
        for i in range(t - 1, max(1, t - span), -1):
            a, b = dif[i] - dea[i], dif[i - 1] - dea[i - 1]
            if up and a > 0 and b <= 0:
                return i
            if (not up) and a < 0 and b >= 0:
                return i
        return -1
    dc = last_cross(False)
    DIVX = (3 < t - dc < 90 and c[t] > c[dc] and dif[t] < dif[dc]
            and dif[t] < dea[t] and dif[t - 1] >= dea[t - 1] and TOPH)
    TOPW = SHOT or STL or DIVX

    # ---- 底部介入（低位 + 底背离）----
    lc120 = roll_ext(c, 120, False)
    lowp = Z(lc120, t) > 0 and c[t] <= Z(lc120, t) * 1.05
    drop = Z(hh60, t - 1) > 0 and c[t] / Z(hh60, t - 1) <= 0.80
    BOTL = lowp and drop
    gc = last_cross(True)
    BDIV = 3 < t - gc < 90 and c[t] < c[gc] and dif[t] > dif[gc] and \
        dif[t] > dea[t] and dif[t - 1] <= dea[t - 1]
    lmres = lm[t] is not None and lm[t - 1] is not None and lm[t - 5] is not None and \
        lm[t] > lm[t - 1] and lm[t] > lm[t - 5]
    BOTS = BDIV and BOTL
    DIV3 = BOTS and lmres
    DIV2 = BOTS and v[t] >= vbase * 1.5 and not DIV3
    DIV1 = BOTS and not DIV3 and not DIV2

    tags = []
    if DIV3: tags.append("底共振")
    if LIFE2: tags.append("生命线")
    if BRK_STR: tags.append("平台强破")
    if BRK_MID: tags.append("平台中破")
    if PIT: tags.append("坑")
    if BOX: tags.append("横盘突破")
    if B2L: tags.append("二爆")
    if C2L: tags.append("二板")
    if ADDX: tags.append("加仓")
    if DIV2: tags.append("底背离")
    if NLNX: tags.append("新低")
    if BOOM: tags.append("爆")
    if P_WEAK: tags.append("弃")
    if GRIND: tags.append("磨")
    if ZBREAK: tags.append("炸板")
    if SHOT: tags.append("清仓")
    elif STL or DIVX: tags.append("减仓")
    if CUT: tags.append("破20")
    if not tags:
        return None

    # 权重按回测 20 日胜率：坑 65.0 > 生命线 60.6 > 二爆 > 平台破 > 底共振
    W = {"底共振": 4, "生命线": 3, "平台强破": 4, "平台中破": 2, "坑": 5, "横盘突破": 3,
         "二爆": 3, "二板": 2, "加仓": 2, "底背离": 2, "爆": 1,
         "弃": -2, "磨": -3, "炸板": -1, "清仓": -4, "减仓": -2, "破20": -2}
    score = sum(W.get(x, 0) for x in tags)
    # 互斥后的最强买点（复刻 v13.7 的 BUY 链）
    lead = ""
    for k in ("底共振", "生命线", "平台强破", "平台中破", "坑", "横盘突破",
              "二爆", "二板", "加仓", "爆"):
        if k in tags:
            lead = k
            break
    return dict(date=d[t], close=round(c[t], 2), chg=round(chg * 100, 2), tags=tags,
                lead=lead, score=score, vol_ratio=round(hsx, 2),
                ma20_gap=round((c[t] / Z(m20, t) - 1) * 100, 2),
                life_gap=round((c[t] / Z(lm, t) - 1) * 100, 2) if Z(lm, t) else 0.0,
                amt=round(v[t] * 100 * c[t] / 1e8, 2),
                pos=round((c[t] - Z(ll120, t)) / (Z(hh120, t) - Z(ll120, t)) * 100, 1)
                if Z(hh120, t) > Z(ll120, t) else 50.0)


def turnover_filter(rows):
    """用腾讯快照的真实换手率给 boom 行补 hs 并二次过滤。

    为什么需要它：K 线里只有量比，量比受"该股平时有多活跃"影响极大。
      · 活跃股日均换手 8%，今天换手 15%（已经很热）→ 量比只有 1.9，轻松通过
      · 冷门股日均换手 2%，今天换手 15%（真启动）→ 量比 7.5，会被 HSX<5 误杀
    所以量比只能挡"和自己比突然放量"，能不能接要看绝对换手。
    """
    if not rows:
        return rows, 0, 0
    syms = [_sym(r["code"]) for r in rows]
    snap = {}
    try:
        snap = snapshot(syms)
    except Exception:
        pass
    kept, hot, cold = [], 0, 0
    for r in rows:
        q = snap.get(_sym(r["code"])) or {}
        tov = float(q.get("tov") or 0)
        r["hs"] = round(tov, 2)
        tg = r.get("tags") or []
        # 换手 >30%：天量接力，扣分但保留（妖股也这样，不能一刀切）
        if tov >= 30:
            r["score"] = r.get("score", 0) - 2
            r["hsw"] = "过热"
            hot += 1
        elif tov and tov <= 1.5 and ("爆" in tg or "二爆" in tg):
            # 冷门股放量：量比高但绝对换手低，属于刚被激活，反而优先
            r["score"] = r.get("score", 0) + 1
            r["hsw"] = "初动"
            cold += 1
        kept.append(r)
    kept.sort(key=lambda x: (-x.get("score", 0), -x.get("vol_ratio", 0)))
    return kept, hot, cold


def _lb_num(hd):
    ns = re.findall(r"\d+", hd or "")
    return int(ns[-1]) if ns else 1


def _sym(code):
    c = str(code)
    if c[0] == "6":
        return "sh" + c
    if c[:2] in ("83", "87", "88", "92") or c[0] == "4":
        return "bj" + c
    return "sz" + c


def _au_time_ok(now):
    """竞价数据只在 9:24:30 - 9:31:00 之间可信"""
    return (now.hour == 9 and 24 <= now.minute <= 31)


def _dragon_verdict(pct, ar):
    if pct >= 5 and ar >= 3:
        return "延续强势·资金抢筹", "r", 3
    if pct >= 2 and ar >= 1.5:
        return "延续·温和承接", "y", 2
    if pct >= 0:
        return "分歧待定", "y", 1
    return "转弱·防炸板", "g", 0


def _ladder_verdict(ratio3):
    if ratio3 >= 0.5:
        return "梯队完整·晋级意愿强", "r", 3
    if ratio3 >= 0.33:
        return "分化·部分晋级", "y", 2
    if ratio3 >= 0.17:
        return "偏弱·只有龙头", "y", 1
    return "断层·无人跟", "g", 0


def _light_amt(sym):
    """取昨日成交额（元）：腾讯日K 收*量*100（qfqday 无金额字段，用收盘价估算）"""
    u = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,6,qfq" % sym
    t = get(u)
    if not t:
        return 0.0
    try:
        j = json.loads(t)
        if j.get("code") != 0:
            return 0.0
        d = j["data"][sym]
        bars = d.get("qfqday") or d.get("day") or []
        if not bars:
            return 0.0
        b = bars[-1]
        return float(b[2]) * float(b[5]) * 100.0
    except Exception:
        return 0.0


def _fill_prev_amt(base):
    """补齐缺失的昨日成交额，返回补齐只数"""
    need = [c for c, b in base.items() if b.get("prev_amt", 0) <= 0]
    if not need:
        return 0
    from concurrent.futures import ThreadPoolExecutor

    def w(c):
        return c, _light_amt(_sym(c))

    ok = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        for c, a in ex.map(w, need):
            if a > 0:
                base[c]["prev_amt"] = a
                ok += 1
    return ok


EM_ZT = ("https://push2ex.eastmoney.com/getTopicZTPool"
         "?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt&Pageindex=0"
         "&pagesize=600&sort=fbt:asc&date=")


def em_zt_pool(date_str):
    """东财涨停池（支持历史日期，含成交额 amount）。
    返回 [{c,n,lb,amt,hs,ltsz,fund,zbc,fbt,hybk}]，失败返回 []"""
    d = _norm_date(date_str).replace("-", "")
    txt = get(EM_ZT + d, enc="utf-8")
    if not txt:
        return []
    try:
        j = json.loads(txt)
    except Exception:
        return []
    out = []
    for x in (((j.get("data") or {}).get("pool")) or []):
        c = str(x.get("c") or "")
        if not c:
            continue
        out.append(dict(c=c, n=(x.get("n") or c).strip(),
                        lb=int(x.get("lbc") or 1),
                        amt=float(x.get("amount") or 0),
                        hs=round(float(x.get("hs") or 0), 2),
                        ltsz=float(x.get("ltsz") or 0),
                        fund=float(x.get("fund") or 0),
                        zbc=int(x.get("zbc") or 0),
                        fbt=str(x.get("fbt") or ""),
                        hybk=(x.get("hybk") or "").strip()))
    return out


def em_prev_pool(base=None, back=6):
    """从 base 往前找第一个有涨停数据的交易日 -> (date, pool)"""
    d0 = base or bjnow()
    for i in range(1, back + 1):
        d = d0 - datetime.timedelta(days=i)
        pool = em_zt_pool(d.strftime("%Y%m%d"))
        if pool:
            return d.strftime("%Y-%m-%d"), pool
        time.sleep(0.15)
    return "", []


def do_auction(uni=None):
    """9:25 集合竞价三判：空间龙量价 / 梯队晋级 / 资金是否认可"""
    now = bjnow()
    prev = now - datetime.timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= datetime.timedelta(days=1)
    ps = prev.strftime("%Y%m%d")
    out = dict(date=now.strftime("%Y-%m-%d"), time=now.strftime("%H:%M:%S"),
               prev=prev.strftime("%Y-%m-%d"), valid=False, items=[], dragon=None,
               dragons=[], ladder=[], follow=None, verdict=None, idx=[],
               message="")
    base, src = {}, ""

    # ① 东财涨停池（支持历史日期，早盘也能拿到；自带成交额，不用估）
    try:
        edate, epool = em_prev_pool()
        if epool:
            src = "东财涨停池"
            out["prev"] = edate
            for r in epool:
                base[r["c"]] = dict(code=r["c"], name=r["n"], lb=r["lb"],
                                    hd="%d板" % r["lb"], seal=round(r["fund"] / 1e8, 2),
                                    tov=r["hs"], opn=r["zbc"], ftime=int(r["fbt"] or 0),
                                    reason=r["hybk"], prev_amt=r["amt"])
            print("auction: 东财涨停池 %s，%d 只" % (edate, len(base)), flush=True)
    except Exception as e:
        print("auction: 东财池失败 %s" % e, flush=True)

    # ② 同花顺昨日池（当日有效，历史日期早盘常为空）
    pool = []
    if not base:
        pool = (ths_get("limit_up_pool", ps,
                        "&page=1&limit=300&field=" + THS_FIELD +
                        "&order_field=330329&order_type=0") or {}).get("info") or []
        src = "同花顺昨日池"
    for it in pool:
        c = str(it.get("code") or "")
        if not c:
            continue
        cv = float(it.get("currency_value") or 0)
        tv = float(it.get("turnover_rate") or 0)
        base[c] = dict(code=c, name=(it.get("name") or "").strip(),
                       lb=_lb_num(it.get("high_days")), hd=it.get("high_days") or "",
                       seal=round(float(it.get("order_amount") or 0) / 1e8, 2),
                       tov=round(tv, 2), opn=int(it.get("open_num") or 0),
                       ftime=int(it.get("first_limit_up_time") or 0),
                       reason=(it.get("reason_type") or "").strip(),
                       prev_amt=cv * tv / 100.0)
    if not base:
        # 早盘同花顺对历史日期常返回空 → 回退昨天 15:35 定稿的 data.json
        src = "本地定稿 data.json"
        try:
            dj = json.load(open(os.path.join(DOCS, "data.json"), encoding="utf-8"))
            for it in (dj.get("ths_zt") or []):
                c = str(it.get("code") or "")
                if not c:
                    continue
                cv = float(it.get("cval") or 0)
                tv = float(it.get("tov") or it.get("hs") or 0)
                base[c] = dict(code=c, name=(it.get("name") or "").strip(),
                               lb=_lb_num(it.get("hd")), hd=it.get("hd") or "",
                               seal=float(it.get("seal") or 0), tov=round(tv, 2),
                               opn=int(it.get("opn") or 0), ftime=0,
                               reason=(it.get("reason") or "").strip(),
                               prev_amt=cv * tv / 100.0)
            out["prev_snapshot"] = dj.get("trade_date") or ""
            print("auction: 回退本地定稿 %s，%d 只" % (out["prev_snapshot"], len(base)))
        except Exception as e:
            print("auction: 本地定稿回退失败 %s" % e)
    if not base:
        out["message"] = "未取到昨日涨停池（东财/同花顺/本地定稿均空）"
        json.dump(out, open(os.path.join(DOCS, "auction.json"), "w", encoding="utf-8"),
                  ensure_ascii=False)
        print("auction: 昨日涨停池为空")
        return out
    filled = _fill_prev_amt(base)
    out["src"] = src
    if filled:
        out["filled"] = filled
    q = snapshot([_sym(c) for c in base])
    rows = []
    for c, b in base.items():
        s = q.get(c) or {}
        op = float(s.get("op") or 0)
        pv = float(s.get("prev") or 0)
        if op <= 0 or pv <= 0:
            continue
        amt = float(s.get("amt") or 0)
        pct = round((op - pv) / pv * 100, 2)
        ar = round(amt / b["prev_amt"] * 100, 2) if b["prev_amt"] > 0 else 0.0
        b.update(op=round(op, 2), pct=pct, ar=ar, amt=round(amt / 1e4, 0))
        rows.append(b)
    if not rows:
        out["message"] = "竞价快照为空（非交易时段或接口限流）"
        json.dump(out, open(os.path.join(DOCS, "auction.json"), "w", encoding="utf-8"),
                  ensure_ascii=False)
        print("auction: 快照为空")
        return out

    rows.sort(key=lambda x: (-x["lb"], -x["ar"]))
    out["items"] = rows
    out["valid"] = _au_time_ok(now)
    if not out["valid"]:
        out["message"] = "非竞价时段（%s），金额不可用，仅供竞价涨幅参考" % now.strftime("%H:%M")

    for it in indexes():
        out["idx"].append(dict(name=it["name"], pct=round(float(it.get("chg") or 0), 2)))

    # ---- ① 空间龙 ----
    maxlb = max(x["lb"] for x in rows)
    tops = [x for x in rows if x["lb"] == maxlb]
    tops.sort(key=lambda x: (-x["seal"], -x["ar"]))
    dl = []
    for t in tops[:3]:
        v, cl, sc = _dragon_verdict(t["pct"], t["ar"])
        dl.append(dict(code=t["code"], name=t["name"], lb=t["lb"], hd=t["hd"],
                       pct=t["pct"], ar=t["ar"], amt=t["amt"], seal=t["seal"],
                       reason=t["reason"], verdict=v, cls=cl, score=sc))
    out["dragons"] = dl
    out["dragon"] = dl[0] if dl else None
    d_score = dl[0]["score"] if dl else 0

    # ---- ② 连板梯队 ----
    groups = [(4, "4板+"), (3, "3板"), (2, "2板"), (1, "首板")]
    ladder = []
    for lb, nm in groups:
        g = [x for x in rows if (x["lb"] >= 4 if lb == 4 else x["lb"] == lb)]
        if not g:
            continue
        n = len(g)
        avg_pct = round(sum(x["pct"] for x in g) / n, 2)
        hi = len([x for x in g if x["pct"] >= 3.0])
        r3 = round(hi / float(n), 2)
        avg_ar = round(sum(x["ar"] for x in g) / n, 2)
        v, cl, sc = _ladder_verdict(r3)
        ladder.append(dict(lb=nm, n=n, avg_pct=avg_pct, hi3=hi, ratio3=r3,
                           avg_ar=avg_ar, verdict=v, cls=cl, score=sc,
                           names=[x["name"] for x in g[:6]]))
    out["ladder"] = ladder
    l_score = next((x["score"] for x in ladder if x["lb"] == "2板"), 0)
    if not l_score and ladder:
        l_score = max(x["score"] for x in ladder)

    # ---- ③ 跟风（昨日首板） ----
    fb = [x for x in rows if x["lb"] == 1]
    if fb:
        n = len(fb)
        hi2 = len([x for x in fb if x["pct"] >= 2.0])
        r2 = round(hi2 / float(n), 2)
        avg = round(sum(x["pct"] for x in fb) / n, 2)
        f_score = 2 if r2 >= 0.5 else (1 if r2 >= 0.3 else 0)
        out["follow"] = dict(n=n, avg_pct=avg, hi2=hi2, ratio2=r2,
                             avg_ar=round(sum(x["ar"] for x in fb) / n, 2),
                             verdict=("跟风积极·可接力" if r2 >= 0.5 else
                                      ("跟风一般·只做龙一" if r2 >= 0.3 else "跟风退潮·纯龙头行情")),
                             cls=("r" if r2 >= 0.5 else ("y" if r2 >= 0.3 else "g")),
                             score=f_score)
    f_score = (out["follow"] or {}).get("score", 0)

    # ---- ④ 资金认可总判 ----
    total = d_score + l_score + f_score
    if total >= 6:
        lvl, cl = "资金认可", "r"
    elif total >= 4:
        lvl, cl = "中性分歧", "y"
    else:
        lvl, cl = "资金不认可", "g"
    reasons = []
    if dl:
        reasons.append("空间龙 %s %d板 高开%+.2f%%、竞价额占昨额 %.2f%% → %s"
                       % (dl[0]["name"], dl[0]["lb"], dl[0]["pct"], dl[0]["ar"], dl[0]["verdict"]))
    l2 = next((x for x in ladder if x["lb"] == "2板"), None)
    if l2:
        reasons.append("2板梯队 %d 只，高开≥3%% 占 %d%%（%d只），平均高开 %+.2f%%"
                       % (l2["n"], round(l2["ratio3"] * 100), l2["hi3"], l2["avg_pct"]))
    if out["follow"]:
        fo = out["follow"]
        reasons.append("昨日首板 %d 只，高开≥2%% 占 %d%%（%d只）→ %s"
                       % (fo["n"], round(fo["ratio2"] * 100), fo["hi2"], fo["verdict"]))
    if out["idx"]:
        reasons.append("指数竞价：" + "、".join("%s%+.2f%%" % (i["name"], i["pct"]) for i in out["idx"]))
    out["verdict"] = dict(level=lvl, cls=cl, score=total, max=8, reasons=reasons,
                          parts=dict(dragon=d_score, ladder=l_score, follow=f_score))
    json.dump(out, open(os.path.join(DOCS, "auction.json"), "w", encoding="utf-8"),
              ensure_ascii=False)
    print("auction: 昨日涨停%d 采集%d 龙%s 总判%d/%d"
          % (len(base), len(rows), (dl[0]["name"] if dl else "-"), total, 8))
    return out


# ---------------- 资讯分级 ----------------
NEWS_KIND = [
    ("政策", re.compile(r"(国务院|政治局|国常会|证监会|央行|发改委|工信部|财政部|商务部|"
                      r"能源局|科技部|药监局|市场监管|部委|印发|出台|试点|批复|"
                      r"减税|降准|降息|补贴|专项债|行动计划|标准体系|征求意见稿)")),
    ("国际", re.compile(r"(美联储|美股|纳斯达克|道琼斯|标普|英伟达|特斯拉|苹果|微软|谷歌|"
                      r"亚马逊|Meta|OpenAI|ChatGPT|GPT|Claude|Gemini|Anthropic|台积电|"
                      r"三星|阿斯麦|欧盟|日本|美国|关税|出口管制|制裁|OPEC|原油)")),
    ("公司", re.compile(r"(公告|中标|签订|订单|合同|投产|扩产|量产|获批|回购|增持|定增|"
                      r"并购|重组|收购|业绩|净利|营收|毛利率|涨价|提价)")),
    ("行情", re.compile(r"(涨停|跌停|封板|炸板|龙虎榜|成交额|换手|盘中|午后|早盘|收盘|异动)")),
]
NEWS_IMP = {"政策": 3, "国际": 2, "公司": 2, "行情": 0, "": 1}


def news_index(news, chain):
    """把每条快讯标上：类别（政策/国际/公司/行情）、命中的产业链题材、重要性
    只保留「国家 / 企业 / 国际」级别的催化信息，纯行情播报降权到 0。"""
    trig = []
    for ch in CHAIN:
        trig.append((ch["key"], ch["trig"]))
    chain_keys = set(c.get("key") for c in (chain or []))

    out = []
    for i, nw in enumerate(news or []):
        t = (nw.get("t") or "").strip()
        if not t:
            continue
        body = t + " " + (nw.get("s") or "")
        hit = [k for k, ws in trig if any(w in body for w in ws)]
        kind = ""
        for kd, rgx in NEWS_KIND:      # 类别只看标题，摘要里"发布/规划"之类的词太多，会全判成政策
            if rgx.search(t):
                kind = kd
                break
        imp = NEWS_IMP.get(kind, 1)
        real_hit = [k for k in hit if k in chain_keys]
        if real_hit:
            imp += 2                      # 能落到产业链上，价值最高
        if kind == "行情" and not real_hit:
            imp = 0                       # 纯播报且没链条 → 丢掉
        if imp <= 0:
            continue
        out.append({
            "i": i,
            "t": t[:90],
            "s": (nw.get("s") or "")[:70],
            "tm": nw.get("tm") or "",
            "url": nw.get("url") or "",
            "ts": nw.get("ts") or 0,
            "kind": kind or "其他",
            "hit": real_hit,
            "imp": imp,
        })
    # 有链条的排前面，再按重要性、再按时间
    out.sort(key=lambda a: (-(1 if a["hit"] else 0), -a["imp"], -(a["ts"] or 0)))
    return out


# ---------------- 产业链树 ----------------
# trig: 新闻触发词；links: 环节（stage/part/概念名关键词）
CHAIN = [
    {
        "key": "固态电池",
        "trig": ["固态电池", "半固态", "凝聚态电池", "硫化物电解质", "干法电极", "锂金属负极"],
        "links": [
            {"stage": "上游", "part": "锂资源", "core": 1,
             "kw": ["小金属", "能源金属", "工业金属", "金属新材料", "锂矿", "盐湖提锂"]},
            {"stage": "上游", "part": "电解质", "core": 1,
             "kw": ["化学制品", "化学原料", "电子化学品", "氟化工", "电解液", "固态电池"]},
            {"stage": "上游", "part": "正负极材料",
             "kw": ["非金属材料", "金属新材料", "石墨烯"]},
            {"stage": "中游", "part": "电芯 PACK", "core": 1,
             "kw": ["电池", "锂电池", "钠离子电池"]},
            {"stage": "下游", "part": "整车储能",
             "kw": ["汽车零部件", "汽车整车", "新能源汽车", "储能"]},
        ],
    },
    {
        "key": "AI算力",
        "trig": ["算力", "大模型", "推理芯片", "HBM", "CPO", "液冷", "光模块", "智算中心", "英伟达"],
        "links": [
            {"stage": "上游", "part": "芯片设计", "core": 1,
             "kw": ["半导体", "元件", "芯片概念", "国产芯片", "存储芯片", "GPU"]},
            {"stage": "上游", "part": "光模块/CPO", "core": 1,
             "kw": ["通信设备", "光学光电子", "CPO", "光模块", "光通信"]},
            {"stage": "中游", "part": "服务器/液冷", "core": 1,
             "kw": ["计算机设备", "IT服务", "其他电子", "算力概念", "数据中心", "液冷"]},
            {"stage": "下游", "part": "IDC/应用",
             "kw": ["软件开发", "通信服务", "数字媒体", "人工智能", "AI应用", "云计算"]},
        ],
    },
    {
        "key": "人形机器人",
        "trig": ["人形机器人", "丝杠", "谐波减速器", "灵巧手", "六维力传感器", "Optimus"],
        "links": [
            {"stage": "上游", "part": "丝杠/减速器", "core": 1,
             "kw": ["通用设备", "专用设备", "工程机械", "减速器", "轴承"]},
            {"stage": "上游", "part": "电机/传感", "core": 1,
             "kw": ["电机", "元件", "自动化设备", "传感器", "伺服"]},
            {"stage": "中游", "part": "本体集成", "core": 1,
             "kw": ["自动化设备", "机器人概念", "人形机器人"]},
            {"stage": "下游", "part": "场景应用",
             "kw": ["软件开发", "IT服务", "人工智能"]},
        ],
    },
    {
        "key": "低空经济",
        "trig": ["低空经济", "eVTOL", "飞行汽车", "无人机", "空域管理", "通航"],
        "links": [
            {"stage": "上游", "part": "材料/动力",
             "kw": ["化学制品", "电池", "航空装备", "碳纤维"]},
            {"stage": "上游", "part": "飞控/导航", "core": 1,
             "kw": ["军工电子", "通信设备", "航天装备", "卫星导航", "北斗"]},
            {"stage": "中游", "part": "整机", "core": 1,
             "kw": ["航空装备", "航天装备", "汽车整车", "低空经济", "无人机"]},
            {"stage": "下游", "part": "运营空管",
             "kw": ["航空机场", "物流", "铁路公路", "专业服务"]},
        ],
    },
    {
        "key": "可控核聚变",
        "trig": ["可控核聚变", "托卡马克", "高温超导", "聚变", "氘氚", "第一壁"],
        "links": [
            {"stage": "上游", "part": "超导材料", "core": 1,
             "kw": ["金属新材料", "小金属", "特钢", "超导"]},
            {"stage": "上游", "part": "磁体/部件", "core": 1,
             "kw": ["电网设备", "电机", "通用设备", "特高压"]},
            {"stage": "中游", "part": "装置工程",
             "kw": ["专用设备", "专业工程", "电力", "核电"]},
            {"stage": "下游", "part": "运营",
             "kw": ["电力", "绿色电力"]},
        ],
    },
    {
        "key": "半导体设备",
        "trig": ["光刻机", "光刻胶", "半导体设备", "国产替代", "晶圆厂", "刻蚀", "先进制程"],
        "links": [
            {"stage": "上游", "part": "材料", "core": 1,
             "kw": ["电子化学品", "非金属材料", "光刻胶", "半导体材料", "靶材"]},
            {"stage": "上游", "part": "设备", "core": 1,
             "kw": ["专用设备", "通用设备", "自动化设备", "半导体设备", "工业母机"]},
            {"stage": "中游", "part": "制造封测", "core": 1,
             "kw": ["半导体", "元件", "光学光电子", "先进封装", "存储芯片"]},
            {"stage": "下游", "part": "应用",
             "kw": ["消费电子", "汽车零部件", "军工电子", "汽车电子"]},
        ],
    },
    {
        "key": "创新药",
        "trig": ["创新药", "减肥药", "GLP-1", "ADC", "License", "出海授权", "CRO", "临床试验"],
        "links": [
            {"stage": "上游", "part": "原料 CXO", "core": 1,
             "kw": ["化学制药", "化学原料", "医疗服务", "CRO", "原料药"]},
            {"stage": "中游", "part": "创新药", "core": 1,
             "kw": ["化学制药", "生物制品", "中药", "创新药", "减肥药"]},
            {"stage": "下游", "part": "流通器械",
             "kw": ["医药商业", "医疗器械", "医疗服务"]},
        ],
    },
    {
        "key": "稀土永磁",
        "trig": ["稀土", "永磁", "氧化镨钕", "出口管制", "磁材"],
        "links": [
            {"stage": "上游", "part": "稀土矿", "core": 1,
             "kw": ["小金属", "工业金属", "稀土永磁", "稀缺资源"]},
            {"stage": "中游", "part": "磁材", "core": 1,
             "kw": ["金属新材料", "非金属材料", "磁性材料"]},
            {"stage": "下游", "part": "电机/新能源",
             "kw": ["电机", "汽车零部件", "风电设备", "光伏设备", "机器人概念"]},
        ],
    },
]

NEWS_MACRO_RE = re.compile(
    r"(国务院|政治局|中央经济工作会议|工信部|发改委|能源局|科技部|财政部|央行|证监会|"
    r"国家标准|征求意见稿|补贴|税收优惠|专项债|规划|行动方案|试点|批复)")
NEWS_NUM_RE = re.compile(r"\d+(\.\d+)?\s*(亿|万|台|座|吨|GW|GWh|MW|%|亿元|万元|万辆)")


def _org_level(t):
    """政策层级：3 中央级 / 2 部委 / 1 地方 / 0 无"""
    if re.search(r"(国务院|政治局|中央经济工作会议|中央|国家主席|国常会)", t):
        return 3
    if NEWS_MACRO_RE.search(t):
        return 2
    if re.search(r"(省|市|自治区|地方)政府|地方", t):
        return 1
    return 0


def _news_num(t):
    """数字密度：具体量化的给 2"""
    return 2 if len(NEWS_NUM_RE.findall(t)) >= 1 else 0


def chain_scan(news, ctop, scon=None, cand=None, indm=None, names=None, chgm=None,
               today=None, all_mode=False):
    """news: [{t,s,tag,tm}]  ctop: concept_top[{name,n,max_lb,stocks[]}]"""
    news = news or []
    ctop = ctop or []

    # 股票 -> {code,name,lb,chg,cs[]}
    # ① 先铺概念缓存（覆盖面最广，来自 docs/scon.json）
    smap = {}
    for code, e in (scon or {}).items():
        smap[code] = {"code": code, "name": e.get("n") or code,
                      "lb": 0, "chg": 0, "cs": list(e.get("cs") or [])}
    # ② 再用 concept_top 补（缓存没覆盖到的当日涨停股）
    for c in ctop:
        cn = c.get("name") or ""
        for s in (c.get("stocks") or []):
            code = s.get("c")
            if not code:
                continue
            e = smap.setdefault(code, {"code": code, "name": s.get("n") or code,
                                       "lb": s.get("lb") or 0, "chg": s.get("chg") or 0,
                                       "cs": []})
            if cn and cn not in e["cs"]:
                e["cs"].append(cn)
    # ③ 行业名当"概念"参与匹配（industry.json 覆盖 4100 只，稳定性远好于东财概念接口）
    nm = names or {}
    cm = chgm or {}
    for code, ind in (indm or {}).items():
        if not ind:
            continue
        e = smap.get(code)
        if e is None:
            e = smap[code] = {"code": code, "name": nm.get(code) or code,
                              "lb": 0, "chg": 0, "cs": []}
        else:
            if e["name"] == code and nm.get(code):
                e["name"] = nm[code]
        if ind not in e["cs"]:
            e["cs"].append(ind)
        if code in cm:
            e["chg"] = cm[code]
    # ④ 用当日候选池补名称/连板/涨幅
    for x in (cand or []):
        e = smap.get(x.get("c"))
        if not e:
            continue
        e["name"] = x.get("n") or e["name"]
        e["lb"] = x.get("lb") or 0
        e["chg"] = x.get("chg") or 0

    # 新闻 -> 题材
    hits = {}
    for nw in news:
        txt = ((nw.get("t") or "") + " " + (nw.get("s") or "") + " " + (nw.get("tag") or ""))
        for ch in CHAIN:
            if any(k in txt for k in ch["trig"]):
                hits.setdefault(ch["key"], []).append({"nw": nw, "txt": txt})

    out = []
    for ch in CHAIN:
        hs = hits.get(ch["key"])
        if not hs and not all_mode:
            continue
        hs = hs or []

        used = set()
        links = []
        for lk in ch["links"]:
            mem = []
            for code, e in smap.items():
                if code in used:
                    continue
                if any(k in cn for cn in e["cs"] for k in lk["kw"]):
                    mem.append(e)
                    used.add(code)
            if not mem:
                continue
            mem.sort(key=lambda x: (-(x["lb"] or 0), -(x["chg"] or 0)))
            zt = sum(1 for x in mem if (x["lb"] or 0) >= 1)
            # 只展示"今天在动"的票（涨停 或 涨幅≥5%），避免把整个行业 200 只全铺上去
            act = [x for x in mem if (x["lb"] or 0) >= 1 or (x["chg"] or 0) >= 5]
            links.append({
                "stage": lk["stage"], "part": lk["part"],
                "core": lk.get("core", 0),
                "n": len(mem), "act": len(act), "zt": zt,
                "stocks": [{"c": x["code"], "n": x["name"], "lb": x["lb"], "chg": x["chg"],
                            "cs": x["cs"][:3]} for x in act[:10]],
            })

        allmem = [x for x in smap.values() if x["code"] in used]
        zt_all = sum(1 for x in allmem if (x["lb"] or 0) >= 1)

        # 六维硬度
        joined = " ".join(h["txt"] for h in hs)
        pol = max([_org_level(h["txt"]) for h in hs] or [0])
        num = max([_news_num(h["txt"]) for h in hs] or [0])
        ncnt = len(hs)
        reso = 3 if ncnt >= 4 else (2 if ncnt == 3 else (1 if ncnt == 2 else 0))
        chainlen = 2 if len(links) >= 4 else (1 if len(links) == 3 else 0)
        mkt = 2 if zt_all >= 4 else (1 if zt_all >= 2 else 0)
        fresh = 1  # 简化：默认 1，接历史新闻后改成真值
        hard = pol + num + reso + chainlen + mkt + fresh

        # 个股催化等级：涨停/涨幅 + 是否核心环节 + 板块热度
        for lk in links:
            core = 1 if lk.get("core") else 0
            for s in lk["stocks"]:
                sc = 0
                if (s["lb"] or 0) >= 1:
                    sc += 2
                elif (s["chg"] or 0) >= 5:
                    sc += 1
                if core:
                    sc += 1
                if lk["zt"] >= 3:
                    sc += 1
                s["cat"] = "强催化" if sc >= 4 else ("中催化" if sc >= 2 else "弱催化")

        out.append({
            "key": ch["key"],
            "hot": 1 if hs else 0,
            "score": hard,
            "grade": "硬逻辑" if hard >= 9 else ("中等" if hard >= 6 else "弱"),
            "dim": {"政策": pol, "数字": num, "共振": reso, "链长": chainlen, "市场": mkt, "新鲜": fresh},
            "news": [{"t": h["nw"].get("t"), "tm": h["nw"].get("tm"), "url": h["nw"].get("url")}
                     for h in hs[:3]],
            "links": links,
            "trig": [w for w in ch["trig"]][:14],
            "zt": zt_all, "mem": len(allmem),
        })

    out.sort(key=lambda x: (-x["hot"], -x["score"], -x["zt"], -x["mem"]))
    return out


def fetch_news_raw(limit=320, hours=48):
    """全量快讯（不做政策/题材筛选），专供产业链引擎与资讯搜索用"""
    out, seen = [], set()
    for pg in range(1, 9):
        u = ("https://news.10jqka.com.cn/tapp/news/push/stock/"
             "?page=%d&tag=&track=website&pagesize=40" % pg)
        try:
            d = json.loads(get(u))
            lst = (d.get("data") or {}).get("list") or []
        except Exception:
            break
        if not lst:
            break
        now = time.time()
        for it in lst:
            try:
                ts = float(it.get("ctime") or 0)
            except Exception:
                ts = 0
            if ts and now - ts > hours * 3600:
                continue
            t = (it.get("title") or "").strip()
            if not t or t in seen or NEWS_BAD.search(t):
                continue
            seen.add(t)
            s2 = (it.get("digest") or it.get("short") or "").strip()
            tags = [x.get("name", "") for x in (it.get("tags") or [])]
            try:
                tm = time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else ""
            except Exception:
                tm = ""
            out.append(dict(t=t, s=s2[:150], tag=tags[0] if tags else "", tm=tm,
                            url=it.get("url") or "", ts=ts))
        time.sleep(0.3)
    out.sort(key=lambda x: -(x.get("ts") or 0))
    return out[:limit]


NEWS_URL = ("https://news.10jqka.com.cn/tapp/news/push/stock/"
            "?page=1&tag=&track=website&pagesize=40")
NEWS_MACRO = re.compile(r"(证监会|国务院|国常会|政治局|央行|发改委|财政部|工信部|商务部|交易所|金融监管|降准|降息|"
                        r"LPR|MLF|逆回购|关税|中美|美联储|美股|纳指|纳斯达克|道指|道琼斯|标普|A50|离岸人民币|"
                        r"外资|北向|IPO|注册制|退市新规|印花税|资本市场|政策|监管新规)")
NEWS_THEME = re.compile(r"(芯片|半导体|光刻|存储|算力|数据中心|液冷|AI|人工智能|大模型|机器人|人形|固态电池|"
                        r"锂电|光伏|储能|核电|核聚变|量子|6G|卫星|商业航天|低空经济|军工|创新药|减肥药|CRO|"
                        r"稀土|永磁|黄金|铜|铝|煤炭|石油|天然气|电力|电网|特高压|并购重组|重组|借壳|回购|增持|"
                        r"中标|订单|量产|涨价|提价|获批|投产|扩产|突破|国产替代|数据要素|信创|鸿蒙|智驾|无人驾驶)")
NEWS_BAD = re.compile(r"(足球|篮球|NBA|世界杯|奥运|明星|演员|歌手|演唱会|离婚|结婚|车祸|火灾|地震|台风|"
                      r"暴雨|判刑|受贿|落马|纪委|双开|彩票|网红|直播带货|电影票房|"
                      r"董事长|当选|聘任|辞职|离任|代行|抵达|会见|致辞|座谈|来访|签署合作备忘录)")
NEWS_TAGS = ("A股", "宏观", "政策", "要闻")


def fetch_news(limit=10, hours=18):
    """隔夜要闻：同花顺 7x24 快讯，按关键词过滤出与炒作相关的"""
    try:
        d = json.loads(get(NEWS_URL))
        lst = (d.get("data") or {}).get("list") or []
    except Exception:
        return []
    now = time.time()
    cand = []
    for it in lst:
        try:
            ts = float(it.get("ctime") or 0)
        except Exception:
            ts = 0
        if ts and now - ts > hours * 3600:
            continue
        t = (it.get("title") or "").strip()
        s = (it.get("digest") or it.get("short") or "").strip()
        tags = [x.get("name", "") for x in (it.get("tags") or [])]
        if not t or NEWS_BAD.search(t):
            continue
        is_macro = bool(NEWS_MACRO.search(t) or NEWS_MACRO.search(s[:120]))
        is_theme = bool(NEWS_THEME.search(t))
        if not (is_macro or is_theme):
            continue
        sc = (3 if is_macro else 0) + (2 if is_theme else 0) + (1 if "A股" in tags else 0)
        cand.append((sc, ts, it, t, s, tags))
    cand.sort(key=lambda x: (-x[0], -x[1]))
    out, seen, us = [], {}, 0
    for sc, ts, it, t, s, tags in cand:
        us_flag = ("盘前" in t) or ("美股" in t and "A股" not in tags)
        if us_flag and us >= 2:
            continue
        subj = t.split("：")[0][:6] if "：" in t else t[:6]
        if seen.get(subj, 0) >= 2:
            continue
        seen[subj] = seen.get(subj, 0) + 1
        if us_flag:
            us += 1
        out.append(dict(t=t, s=s[:110], tag=tags[0] if tags else "",
                        url=it.get("url") or "",
                        tm=time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else ""))
        if len(out) >= limit:
            break
    out.sort(key=lambda x: x["tm"], reverse=True)
    return out


def fetch_global():
    """隔夜外盘：美股三大 + 恒指 + A50 期货（名称用固定映射，接口是 GBK 编码）"""
    NAME = {"usDJI": "道琼斯", "usIXIC": "纳斯达克", "usINX": "标普500",
            "hkHSI": "恒生指数", "CHA50CFD": "A50期货"}
    out = []
    try:
        raw = get("https://qt.gtimg.cn/q=usDJI,usIXIC,usINX,hkHSI,CHA50CFD", enc="gbk")
        for line in raw.split(";"):
            line = line.strip()
            if "=" not in line or not line.startswith("v_"):
                continue
            key = line.split("=", 1)[0][2:].strip()
            body = line.split('="', 1)[-1].rstrip('"')
            f = body.split("~")
            if len(f) < 33:
                continue
            try:
                out.append(dict(name=NAME.get(key, key), price=float(f[3]),
                                pct=round(float(f[32]), 2)))
            except Exception:
                pass
    except Exception:
        pass
    return out


def premarket():
    """盘前解读：隔夜外盘 + 要闻 + 昨日复盘 + 今日关注"""
    now = bjnow()
    prev = now - datetime.timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= datetime.timedelta(days=1)
    ps = prev.strftime("%Y%m%d")
    zt, blk, det = ths_zt_detail(ps)
    gq = fetch_global()
    news = fetch_news()
    zt_n = len(zt)
    lbs = []
    for v in zt.values():
        hd = v.get("hd") or ""
        nums = re.findall(r"\d+", hd)
        lbs.append(int(nums[-1]) if nums else 1)
    max_lb = max(lbs) if lbs else 0
    n2 = sum(1 for x in lbs if x >= 2)
    n3 = sum(1 for x in lbs if x >= 3)
    review = ["昨日（%s）涨停 %d 家，其中 %d 只连板、%d 只 3 板以上，空间高度 %d 板。"
              % (prev.strftime("%m-%d"), zt_n, n2, n3, max_lb)]
    if blk:
        t = blk[0]
        review.append("昨日主线「%s」%d 家涨停、%d 只连板、最高 %s、持续 %d 天。"
                      % (t["name"], t["n"], t.get("cont") or 0, t.get("high") or "-", t.get("days") or 0))
        if len(blk) > 2:
            review.append("跟风：「%s」%d 家、「%s」%d 家。"
                          % (blk[1]["name"], blk[1]["n"], blk[2]["name"], blk[2]["n"]))
    focus = []
    for b in blk[:3]:
        why = []
        for s in (b.get("stocks") or [])[:6]:
            r = (det.get(s["c"]) or "").strip()
            if not r:
                r = (zt.get(s["c"]) or {}).get("reason") or ""
            if r:
                why.append(dict(n=s["n"], c=s["c"], lb=s.get("lb") or 1, t=r))
            if len(why) >= 2:
                break
        focus.append(dict(blk=b["name"], n=b["n"], high=b.get("high") or "",
                          why=why or [dict(n="", c="", lb=0,
                                           t="%d 家涨停、最高 %s" % (b["n"], b.get("high") or "-"))],
                          stocks=(b.get("stocks") or [])[:8]))
    adv = []
    if max_lb >= 5:
        adv.append("昨日空间 %d 板，接力情绪还在，今天重点看高标能否继续上台阶。" % max_lb)
    elif max_lb <= 3:
        adv.append("昨日最高只有 %d 板，空间被压，今天先看低位补涨而不是高位接力。" % max_lb)
    else:
        adv.append("昨日空间 %d 板，处于中性区间，先看主线是否延续。" % max_lb)
    if zt_n >= 60:
        adv.append("涨停家数在 60 家以上，赚钱效应不错，可正常仓位参与主线。")
    elif zt_n >= 35:
        adv.append("涨停家数中等，半仓以内，只做最强主线。")
    else:
        adv.append("涨停家数偏少，情绪弱，轻仓或空仓，等确认再动手。")
    adv.append("9:25 集合竞价重点看昨日主线个股：高开 3% 以上、竞价量占昨日成交量 10% 以上，才算资金真认可。")
    return dict(active=now.hour < 9 or (now.hour == 9 and now.minute < 30),
                prev=prev.strftime("%Y-%m-%d"), global_idx=gq, news=news,
                review=review, focus=focus, advice=" ".join(adv),
                zt=zt_n, max_lb=max_lb, n2=n2,
                ts=now.strftime("%H:%M"))




# ---------------- 核心：逆势吸金的总龙头 ----------------
def _cum(seq, k):
    """近 k 日累计涨幅（seq 是收盘价序列，最后一个是今天）"""
    if len(seq) < k + 1:
        return 0.0
    return seq[-1] / seq[-1 - k] - 1.0


def core_scan(cands, snap=None, idx_sym="sh000001", topn=3):
    """core 的本质：市场跌的时候它涨（逆势）、成交额自己放大（吸走存量资金）、
    连续多天都在做这件事（持续）。不是振幅大就行 —— 天地板只是它的表现形式。"""
    cands = cands or []
    snap = snap or {}
    if not cands:
        return []
    MIN_AMT3 = 0.30e8      # 近3日均额门槛：太小说明是微型票，容不下资金，当不了总龙头

    ib = klines(idx_sym) or []
    if len(ib) < 12:
        print("  核心：指数日线不足，跳过", flush=True)
        return []
    ic = [float(b[2]) for b in ib]
    i3, i5 = _cum(ic, 3), _cum(ic, 5)

    def _one(c):
        try:
            return c, klines(_sym(c))
        except Exception:
            return c, None

    with ThreadPoolExecutor(max_workers=10) as ex:
        syms = list(ex.map(_one, cands[:200]))

    out = []
    for code, bars in syms:
        if not bars or len(bars) < 13:
            continue
        try:
            c = [float(b[2]) for b in bars]
        except Exception:
            continue
        if c[-1] <= 0 or min(c[-13:]) <= 0:
            continue

        r3, r5 = _cum(c, 3), _cum(c, 5)
        rs3 = (r3 - i3) * 100.0
        rs5 = (r5 - i5) * 100.0

        # 吸金：近 3 日成交额均值 / 前 10 日均值（自身放大＝有场内存量资金调过来）
        try:
            amt = [float(b[2]) * float(b[5]) for b in bars[-13:]]
        except Exception:
            continue
        a3 = sum(amt[-3:]) / 3.0
        a10 = sum(amt[:-3]) / max(1, len(amt) - 3)
        amt_r = (a3 / a10) if a10 > 0 else 1.0

        # 逆势：大盘跌的那几天，它是不是在涨
        k = min(len(c), len(ic), 6)
        dn, win = 0, 0
        for i in range(1, k):
            icg = ic[-i] / ic[-i - 1] - 1.0
            ccg = c[-i] / c[-i - 1] - 1.0
            if icg < 0:
                dn += 1
                if ccg > 0:
                    win += 1

        if a3 < MIN_AMT3:
            continue

        # 注意力：当日振幅 + 换手（天地板/地天板就是高换手的极端形态）
        sp = snap.get(code) or {}
        px, prev = sp.get("px") or 0, sp.get("prev") or 0
        hi, lo = sp.get("hi") or 0, sp.get("lo") or 0
        tov = sp.get("tov") or 0
        amp = ((hi - lo) / prev) if (hi and lo and prev) else 0.0

        sc = 0.0
        # ① 逆势（0-5）：5日超额为主，3日辅助
        if rs5 >= 20:
            sc += 4
        elif rs5 >= 12:
            sc += 3
        elif rs5 >= 6:
            sc += 2
        elif rs5 >= 2:
            sc += 1
        if rs3 >= 10:
            sc += 1
        # ② 吸金（0-3）：成交额自己放大
        if amt_r >= 2.0:
            sc += 3
        elif amt_r >= 1.5:
            sc += 2
        elif amt_r >= 1.2:
            sc += 1
        # ③ 注意力（0-5）：振幅 + 换手，全市场都盯着它
        if amp >= 0.15:
            sc += 3
        elif amp >= 0.10:
            sc += 2
        elif amp >= 0.07:
            sc += 1
        if tov >= 20:
            sc += 2
        elif tov >= 12:
            sc += 1
        # ④ 持续（0-2）：大盘跌的每一天它都扛住
        if dn >= 2 and win == dn:
            sc += 2
        elif dn >= 2 and win >= dn * 0.6:
            sc += 1

        if sc < 9:
            continue

        why = []
        if rs5 >= 2:
            why.append("5日跑赢大盘 %.1f%%" % rs5)
        if amt_r >= 1.2:
            why.append("成交额放大 %.1f 倍" % amt_r)
        if amp >= 0.07:
            why.append("日内振幅 %.0f%%" % (amp * 100))
        if tov >= 12:
            why.append("换手 %.0f%%" % tov)
        if dn >= 1:
            why.append("大盘跌 %d 天它涨 %d 天" % (dn, win))

        out.append({
            "c": code, "rs3": round(rs3, 1), "rs5": round(rs5, 1),
            "amt_r": round(amt_r, 2), "amt3": round(a3 / 1e8, 2),
            "amp": round(amp * 100, 1), "tov": round(tov, 1),
            "dn": dn, "win": win,
            "r5": round(r5 * 100, 1), "sc": round(sc, 1), "why": why,
            "uni": 0,
        })

    out.sort(key=lambda a: (-a["sc"], -a["rs3"], -a["amt_r"]))
    out = out[:topn]
    if out:
        # 唯一性：领先第二名 ≥1 分才算「唯一核心」
        if len(out) == 1 or out[0]["sc"] - out[1]["sc"] >= 1:
            out[0]["uni"] = 1
    return out


# ---------------- 弱市抱团 · 分歧找核心 ----------------
def build_group(zt, zb, snap, stage=None, temp=0, max_lb=0, cores=None):
    """抱团 = 各板块最高标；核心 = 全市场唯一逆势标的（天地板/地天板反复博弈那种）"""
    zt = zt or []
    zb = zb or []
    snap = snap or {}

    # --- 每个行业（板块）取最高标 ---
    by_ind = {}
    for x in list(zt) + list(zb):
        ind = (x.get("hybk") or "未知").strip() or "未知"
        by_ind.setdefault(ind, []).append(x)

    tops = []
    for ind, arr in by_ind.items():
        arr.sort(key=lambda a: (-(int(a.get("lbc") or 1)), -(float(a.get("fund") or 0))))
        t = arr[0]
        tops.append({
            "ind": ind, "n": len(arr),
            "c": str(t.get("c") or "").zfill(6), "nm": t.get("n") or "",
            "lb": int(t.get("lbc") or 1),
            "fund": round(float(t.get("fund") or 0) / 1e8, 2),
            "zbc": int(t.get("zbc") or 0),
        })
    tops.sort(key=lambda a: (-a["lb"], -a["n"], -a["fund"]))
    top_lb = tops[0]["lb"] if tops else 0

    # 抱团度：最高板那几只票所在板块的涨停数 / 全市场涨停数
    tot = max(1, len(zt))
    lead = {}
    for t in tops:
        if t["lb"] == top_lb:
            lead[t["ind"]] = lead.get(t["ind"], 0) + t["n"]
    conc = round(max(lead.values()) / tot, 3) if lead else 0.0
    if zb:
        zb_rate = round(len(zb) / max(1, len(zt) + len(zb)), 3)
    else:
        # 炸板池拿不到时，用涨停池里「炸过板」的只数反推
        zb_rate = round(sum(1 for x in zt if int(x.get("zbc") or 0) > 0) / max(1, len(zt)), 3)

    if top_lb >= 4 and conc >= 0.20:
        mood, cls = "强抱团 · 空间在打开", "r"
    elif top_lb >= 3 and conc >= 0.15:
        mood, cls = "弱抱团 · 资金向最高标集中", "y"
    elif top_lb >= 3:
        mood, cls = "有高标 · 无板块效应", "y"
    elif top_lb <= 2:
        mood, cls = "无高度 · 混沌期", "g"
    else:
        mood, cls = "观察", ""

    tips = []
    tips.append("全市场最高 %d 板：%s" % (
        top_lb, "、".join("%s[%s]" % (t["nm"], t["ind"]) for t in tops[:3] if t["lb"] == top_lb) or "—"))
    tips.append("最高标所在板块涨停 %d / 全市场 %d，抱团度 %.0f%%%s" % (
        max(lead.values()) if lead else 0, len(zt), conc * 100,
        "（资金集中，弱市特征）" if conc >= 0.20 else "（发散，各打各的）"))
    tips.append("炸板率 %.0f%% %s" % (
        zb_rate * 100, "（分歧大，只做最强）" if zb_rate >= 0.35 else "（承接尚可）"))

    # --- 核心：全市场唯一逆势标的（九阳股份型：天地板/地天板反复博弈） ---
    cands = []
    seen = set()
    for x in list(zt) + list(zb):
        c = str(x.get("c") or "").zfill(6)
        if c in seen:
            continue
        seen.add(c)
        sp = snap.get(c) or {}
        px, prev = sp.get("px") or 0, sp.get("prev") or 0
        hi, lo = sp.get("hi") or 0, sp.get("lo") or 0
        if not (px and prev and hi and lo):
            continue
        amp = (hi - lo) / prev
        tov = sp.get("tov") or 0
        chg = sp.get("chg") or 0
        lbc = int(x.get("lbc") or 1)
        zbc = int(x.get("zbc") or 0)

        sc = 0
        if amp >= 0.15:
            sc += 4
        elif amp >= 0.11:
            sc += 3
        elif amp >= 0.08:
            sc += 1
        if tov >= 20:
            sc += 3
        elif tov >= 12:
            sc += 2
        elif tov >= 7:
            sc += 1
        if zbc >= 2:
            sc += 2
        elif zbc == 1:
            sc += 1
        if lbc >= 3:
            sc += 2
        elif lbc == 2:
            sc += 1
        if chg > 0:
            sc += 1

        if sc < 8:
            continue
        why = []
        if amp >= 0.15:
            why.append("日内振幅 %.0f%%（天地板级）" % (amp * 100))
        elif amp >= 0.11:
            why.append("日内振幅 %.0f%%" % (amp * 100))
        if tov >= 20:
            why.append("换手 %.0f%%（巨量换手）" % tov)
        elif tov >= 12:
            why.append("换手 %.0f%%" % tov)
        if zbc >= 1:
            why.append("炸板 %d 次（反复撬板）" % zbc)
        if lbc >= 2:
            why.append("%d 连板" % lbc)
        cands.append({"c": c, "n": x.get("n") or "", "ind": (x.get("hybk") or "").strip(),
                      "lb": lbc, "chg": round(chg, 2), "amp": round(amp * 100, 1),
                      "tov": round(tov, 1), "zbc": zbc, "sc": sc, "why": why})

    if cores:
        pass                     # 用 core_scan 算出来的真·核心
    else:
        cands.sort(key=lambda a: (-a["sc"], -a["amp"], -a["tov"]))
        cores = cands[:3]
        for i, cc in enumerate(cores):
            cc["uni"] = 1 if i == 0 else 0

    if not cores:
        tips.append("今天没有出现「天地板/地天板反复博弈」的单核标的 —— 分歧还没集中到一只票上")
    elif len(cores) == 1:
        tips.append("唯一核心：%s（%s）—— 它不死，老周期就不算结束" % (cores[0]["n"], " · ".join(cores[0]["why"][:2])))
    else:
        tips.append("核心候选 %d 只，最硬的是 %s（%s）" % (
            len(cores), cores[0]["n"], " · ".join(cores[0]["why"][:2])))

    return {
        "mood": mood, "cls": cls, "tips": tips,
        "top_lb": top_lb, "tops": tops[:12],
        "cores": cores, "conc": conc, "zb_rate": zb_rate,
        "zt": len(zt), "zb": len(zb), "temp": temp, "stage": stage or "",
    }


def build_brief(zt, zb, dtp, tblk, tdet, ind_top, stage, rotor, sent, max_lb):
    """早盘解读：把当日盘面压成几句人话 + 推荐炒作逻辑 + 相关个股"""
    zt_n, zb_n, dt_n = len(zt), len(zb), len(dtp)
    rate = 100.0 * zb_n / max(1, zt_n + zb_n)
    tone = {"高潮": "r", "发酵": "r", "启动": "y", "冰点": "g", "退潮": "g"}.get(stage, "y")
    mood = {"高潮": "情绪高潮", "发酵": "发酵中", "启动": "启动期",
            "冰点": "情绪冰点", "退潮": "退潮期"}.get(stage, "盘面")
    L = []
    L.append("涨停 %d 家 / 炸板 %d 家（炸板率 %.0f%%）/ 跌停 %d 家，最高 %d 板。"
             % (zt_n, zb_n, rate, dt_n, max_lb))
    if rate >= 50:
        L.append("炸板率过半，说明接力盘不认，追二板就是送钱；今天只做首板低吸或干脆空仓。")
    elif rate >= 35:
        L.append("炸板率偏高，封板不牢，打板要挑 10 点前封死的，其余只看不做。")
    else:
        L.append("封板质量尚可，接力有一定成功率，可轻仓参与最强主线。")
    if dt_n >= 40:
        L.append("跌停 %d 家、杀跌未止，这类日子任何反弹都是逃命窗口，不要抄底。" % dt_n)
    elif dt_n >= 15:
        L.append("跌停 %d 家，局部流动性仍在收缩，控制仓位、别碰低位补跌股。" % dt_n)
    if tblk:
        t = tblk[0]
        L.append("最强主线「%s」：%d 家涨停、%d 只连板、最高 %s、持续 %d 天。"
                 % (t["name"], t["n"], t.get("cont") or 0, t.get("high") or "-", t.get("days") or 0))
        if len(tblk) > 1:
            L.append("次主线：「%s」%d 家、「%s」%d 家。"
                     % (tblk[1]["name"], tblk[1]["n"],
                        tblk[2]["name"] if len(tblk) > 2 else "-",
                        tblk[2]["n"] if len(tblk) > 2 else 0))
    logic = []
    for b in tblk[:3]:
        why = []
        for s in (b.get("stocks") or [])[:8]:
            r = (tdet.get(s["c"]) or "").strip()
            if not r:
                r = (zt.get(s["c"]) or {}).get("reason") or ""
            if r:
                why.append(dict(n=s["n"], c=s["c"], lb=s.get("lb") or 1, t=r))
            if len(why) >= 2:
                break
        logic.append(dict(blk=b["name"], n=b["n"], cont=b.get("cont") or 0,
                          high=b.get("high") or "", days=b.get("days") or 0,
                          why=why or [dict(n="", c="", lb=0,
                                           t="%d 家涨停、最高 %s" % (b["n"], b.get("high") or "-"))],
                          stocks=(b.get("stocks") or [])[:8]))
    if not logic:
        for it in (ind_top or [])[:3]:
            logic.append(dict(blk=it["name"], n=it["n"], cont=0,
                              high="%d板" % it["max_lb"], days=0,
                              why=[dict(n="", c="", lb=0,
                                        t="%d 家涨停、%d 板高度" % (it["n"], it["max_lb"]))],
                              stocks=(it.get("stocks") or [])[:8]))
    adv = {"冰点": "冰点期只做首板低吸，不接飞刀；等涨停家数回到 60 家以上再动手。",
           "启动": "启动期可试仓，优先最强主线的龙一龙二，仓位半仓以内。",
           "发酵": "发酵期是主升段，龙头二板可打，回踩不破 5 日线就拿着。",
           "高潮": "高潮期只卖不买，次日冲高兑现，别在最热的时候接最后一棒。",
           "退潮": "退潮期只做最强、次日必走；破 20 日线的一律清。"}.get(stage, "轻仓试错，等主线确立。")
    if rotor in ("电风扇", "多点轮动"):
        adv += " 轮动%s，只做低吸不追涨。" % rotor
    elif rotor == "混沌无主线":
        adv += " 今日无主线，空仓等信号。"
    return dict(mood=mood, tone=tone, lines=L, logic=logic, advice=adv,
                zt=zt_n, zb=zb_n, dt=dt_n, rate=round(rate, 1), max_lb=max_lb,
                stage=stage, rotor=rotor)


def main():
    uni = universe()
    print("universe %d" % len(uni), flush=True)
    ind_map = industry_map(uni)      # 先取行业，避免后续请求把它限流
    print("行业映射 %d" % len(ind_map), flush=True)
    rows = []
    CYC = {k: defaultdict(int) for k in ("zt", "dt", "up", "tot", "lb")}

    IND_AMT = defaultdict(lambda: defaultdict(float))   # 行业 -> 日期 -> 成交额
    IND_ZT = defaultdict(lambda: defaultdict(int))      # 日期 -> 行业 -> 涨停家数

    def accum(bars, code):
        n = len(bars)
        if n < 20:
            return
        c = [float(b[2]) for b in bars]
        v = [float(b[5]) for b in bars]
        dd = [b[0] for b in bars]
        lim = 0.195 if code.startswith(("30", "68")) else 0.095
        ind = ind_map.get(code, "") or ""
        streak = 0
        for i in range(max(1, n - 45), n):
            chg = c[i] / c[i - 1] - 1
            k = dd[i]
            CYC["tot"][k] += 1
            if ind:
                IND_AMT[ind][k] += v[i] * 100 * c[i]
            if chg >= lim:
                CYC["zt"][k] += 1
                if ind:
                    IND_ZT[k][ind] += 1
                streak += 1
                if streak > CYC["lb"][k]:
                    CYC["lb"][k] = streak
            else:
                if chg <= -lim:
                    CYC["dt"][k] += 1
                streak = 0
            if chg > 0:
                CYC["up"][k] += 1

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
            accum(bars, s["code"])
            r = analyse(bars)
            if r and r["date"] == bars[-1][0]:
                r.update(code=s["code"], name=s["name"])
                rows.append(r)

    ratio = ok_fetch / max(1, len(uni))
    err = ""
    if ratio < 0.5:
        err = "行情数据抓取失败（成功 %d/%d），本次结果不可信，请稍后重跑" % (ok_fetch, len(uni))
        print("!! " + err, flush=True)

    name_of = {x["code"]: x.get("name") or x["code"] for x in uni}
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

    # ---------- 真实换手率二次判定（补 hs，过热扣分 / 初动加分）----------
    try:
        rows, n_hot, n_cold = turnover_filter(rows)
        print("换手率校准 过热%d 初动%d" % (n_hot, n_cold), flush=True)
    except Exception as e:
        print("换手率校准失败(忽略): %r" % (e,), flush=True)
        rows.sort(key=lambda x: (-x["score"], -x["vol_ratio"]))

    # ---------- 负面标签不进"起爆信号表" ----------
    # 磨/弃/破20/减仓/清仓/炸板/新低 这些不是买点，混进买入表会误导
    BUY_TAG = ("底共振", "生命线", "平台强破", "平台中破", "坑", "横盘突破",
               "二爆", "二板", "加仓", "底背离", "爆")
    risk = [r for r in rows if not any(t in BUY_TAG for t in r["tags"])]
    rows = [r for r in rows if any(t in BUY_TAG for t in r["tags"])]
    for r in risk:
        r["track"] = "R"
    print("买入标签 %d / 风险标签 %d" % (len(rows), len(risk)), flush=True)

    rows.sort(key=lambda x: (-x["score"], -x["vol_ratio"]))
    trade_date = max((r["date"] for r in rows), default=bjnow().strftime("%Y-%m-%d"))

    # ---------------- 涨停池 / 连板梯队 / 催化 / 情绪 ----------------
    dstr = bjnow().strftime("%Y%m%d")
    zt = zt_pool(dstr)
    zb = zb_pool(dstr)
    dtp = dt_pool(dstr)
    print("涨停 %d 炸板 %d 跌停 %d 行业映射 %d" % (len(zt), len(zb), len(dtp), len(ind_map)), flush=True)

    # ---------- 同花顺异动解读（板块 + 一句话原因 + 全文解读） ----------
    tzt, tblk, tdet = {}, [], {}
    try:
        tzt, tblk, tdet = ths_zt_detail(dstr)
        print("  同花顺 涨停%d 板块%d 解读%d" % (len(tzt), len(tblk), len(tdet)), flush=True)
    except Exception as e:
        print("  同花顺失败: %s" % type(e).__name__, flush=True)

    ind_stat = {}
    for x in zt:
        h = x.get("hybk") or ind_map.get(str(x.get("c"))) or "其他"
        e = ind_stat.setdefault(h, {"n": 0, "max_lb": 0, "names": [], "stocks": []})
        e["n"] += 1
        lb = int(x.get("lbc") or 1)
        e["max_lb"] = max(e["max_lb"], lb)
        if len(e["names"]) < 3:
            e["names"].append(x.get("n"))
        e["stocks"].append(dict(c=str(x.get("c")), n=x.get("n"), lb=lb,
                                chg=round(float(x.get("zdp") or 0), 2)))
    for h, e in ind_stat.items():
        e["cat"] = "强催化" if e["n"] >= 4 else ("中催化" if e["n"] >= 2 else "弱催化")

    ztmap = {str(x.get("c")): x for x in zt}
    zbmap = {str(x.get("c")): x for x in zb}
    for r in rows:
        z = ztmap.get(r["code"])
        zz = zbmap.get(r["code"])          # 炸板池（触板未封住）
        src = z or zz
        r["lbc"] = int(z.get("lbc") or 0) if z else 0
        r["ind"] = (src.get("hybk") if src else None) or ind_map.get(r["code"], "") or ""
        r["cat"] = ind_stat.get(r["ind"], {}).get("cat", "") if r["ind"] else ""
        tj = (src or {}).get("zttj") or {}
        r["ztj"] = ("%d天%d板" % (tj.get("days", 0), tj.get("ct", 0))) if tj else ""
        r["zbc"] = int((src or {}).get("zbc") or 0)
        r["fbt"] = int((src or {}).get("fbt") or 0)
        t = tzt.get(r["code"]) or {}
        r["reason"] = t.get("reason", "")
        r["desc"] = tdet.get(r["code"], "")
        if t.get("hd") and not r.get("ztj"):
            r["ztj"] = t["hd"]
        if t.get("seal"):
            r["seal"] = t["seal"]
        if zz is not None and z is None:
            r["tags"].append("炸板")
            r["score"] = r.get("score", 0) - 2

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

    # ---------- 情绪周期序列 ----------
    cdates = sorted(CYC["tot"])[-30:]
    cycle = []
    for k in cdates:
        z_, d_ = CYC["zt"][k], CYC["dt"][k]
        t_ = CYC["tot"][k]
        r_ = round(100.0 * CYC["up"][k] / max(1, t_), 1)
        l_ = CYC["lb"][k]
        if d_ >= 20 or r_ <= 20:
            st = "冰点"
        elif z_ >= 90 or r_ >= 80:
            st = "高潮"
        elif z_ >= 60:
            st = "发酵"
        elif d_ <= 10 and z_ >= 50:
            st = "启动"
        else:
            st = "退潮"
        cycle.append(dict(d=k, zt=z_, dt=d_, lb=l_, temp=r_, st=st))
    stage = cycle[-1]["st"] if cycle else ""

    # ---------- 资金流向 ----------
    fdates = sorted(CYC["tot"])[-25:]
    tot_amt = {d: 0.0 for d in fdates}
    for ind, m in IND_AMT.items():
        for d in fdates:
            tot_amt[d] += m.get(d, 0.0)
    td = fdates[-1] if fdates else ""
    prev = fdates[-21:-1] if len(fdates) >= 21 else fdates[:-1]
    flow_rows = []
    for ind, m in IND_AMT.items():
        a_today = m.get(td, 0.0)
        if a_today <= 0 or tot_amt.get(td, 0) <= 0:
            continue
        share = a_today / tot_amt[td]
        if share < 0.003:      # 占比<0.3%的迷你行业噪声太大，剔除
            continue
        base = [m.get(d, 0.0) / tot_amt[d] for d in prev if tot_amt.get(d, 0)]
        if not base:
            continue
        s20 = sum(base) / len(base)
        if s20 <= 0:
            continue
        chg_pct = share / s20 - 1
        # 连续上升天数
        shares = []
        for d in fdates:
            if tot_amt.get(d, 0):
                shares.append(m.get(d, 0.0) / tot_amt[d])
            else:
                shares.append(0.0)
        st = 0
        for i in range(len(shares) - 1, 0, -1):
            if shares[i] > shares[i - 1]:
                st += 1
            else:
                break
        flow_rows.append(dict(name=ind, share=round(share * 100, 2),
                              share20=round(s20 * 100, 2),
                              chg=round(chg_pct * 100, 1), streak=st,
                              zt=IND_ZT.get(td, {}).get(ind, 0),
                              amt=round(a_today / 1e8, 1)))
    flow_rows.sort(key=lambda x: -x["chg"])
    inflow = [x for x in flow_rows if x["chg"] > 20][:8]
    outflow = sorted(flow_rows, key=lambda x: x["chg"])[:8]

    # 存量 / 增量
    amt_today = tot_amt.get(td, 0.0) / 1e8
    amt_avg = (sum(tot_amt[d] for d in prev) / len(prev) / 1e8) if prev else amt_today
    ratio = (amt_today / amt_avg) if amt_avg else 1.0
    trend = "增量" if ratio >= 1.2 else ("存量" if ratio <= 1.05 else "平衡")

    # 轮动速度：最近6日"涨停第一"的行业换了几个
    tops = []
    prev_top = ""
    for d in fdates[-6:]:
        m = IND_ZT.get(d) or {}
        if not m:
            tops.append(dict(d=d, ind=prev_top, n=0))
            continue
        mx = max(m.values())
        cands = [k for k, v in m.items() if v == mx]
        # 平局时优先沿用前一日的行业，避免噪声式"换主线"
        pick = prev_top if prev_top in cands else sorted(cands)[0]
        tops.append(dict(d=d, ind=pick, n=int(mx)))
        prev_top = pick
    names = [t["ind"] for t in tops]
    changes = sum(1 for i in range(1, len(names))
                  if names[i] and names[i - 1] and names[i] != names[i - 1])
    top_n = tops[-1]["n"] if tops else 0
    # 当前连续天数：末尾同一个第一已经站了几天
    cur = 1
    for i in range(len(names) - 1, 0, -1):
        if names[i] and names[i] == names[i - 1]:
            cur += 1
        else:
            break
    # 七档：强度 → 连续性 → 变动频率
    if top_n <= 3:
        rotor = "混沌无主线"
    elif cur >= 3 and top_n >= 6:
        rotor = "主线确立"
    elif cur >= 2 and top_n >= 4:
        rotor = "主线形成"
    elif top_n >= 12 and cur == 1:
        rotor = "单日爆发"
    elif changes <= 1:
        rotor = "独苗主线"
    elif changes == 2:
        rotor = "双线跷跷板"
    elif changes <= 4:
        rotor = "多点轮动"
    else:
        rotor = "电风扇"

    # ---------- 字辈炒作 ----------
    try:
        pmap = {}
        for x in list(zt) + list(zb):
            pmap[str(x.get("c"))] = (x.get("n"), int(x.get("lbc") or 1),
                                     round(float(x.get("zdp") or 0), 2))
        wplay = wordplay(uni, pmap)
        print("  字辈 %d 组" % len(wplay), flush=True)
    except Exception as e:
        wplay = []
        print("  字辈失败: %s" % type(e).__name__, flush=True)

    # ---------- 概念催化 ----------
    try:
        cpool = [dict(code=str(x.get("c")), name=x.get("n"), lb=int(x.get("lbc") or 1),
                      chg=round(float(x.get("zdp") or 0), 2))
                 for x in list(zt) + list(zb)]
        ctop = concept_top(cpool)
        try:
            # 候选池：涨停 + 炸板 + 涨幅≥5%（不再只看涨停，否则链长算不出来）
            cparts = {}
            for x in list(zt) + list(zb):
                c = str(x.get("c") or "")
                if not c:
                    continue
                cparts[c] = dict(c=c, n=x.get("n") or name_of.get(c, c),
                                 lb=int(x.get("lbc") or 0),
                                 chg=round(float(x.get("zdp") or 0), 2))
            for c, v in (snap or {}).items():
                ch = v.get("chg") or 0
                if ch >= 5 and c not in cparts:
                    cparts[c] = dict(c=c, n=name_of.get(c, c), lb=0, chg=round(ch, 2))
            scon = load_scon()
            need = [(c, v["n"]) for c, v in cparts.items() if c not in scon]
            if need:
                print("  scon 需补 %d 只" % len(need), flush=True)
                scon.update(scon_bulk(need[:500]))
                save_scon(scon)
            news_raw = fetch_news_raw(320)
            chgm = {c: round(float(v.get("chg") or 0), 2) for c, v in (snap or {}).items()}
            chain = chain_scan(news_raw, ctop, scon, list(cparts.values()), ind_map,
                               name_of, chgm, all_mode=True)
            nindex = news_index(news_raw, chain)
            print("产业链 %d 个题材 / 快讯 %d 条 / 候选 %d 只 / 概念缓存 %d 只"
                  % (len(chain), len(news_raw), len(cparts), len(scon)), flush=True)
            print("资讯索引 %d 条（能落到产业链 %d 条）"
                  % (len(nindex), sum(1 for x in nindex if x["hit"])), flush=True)
        except Exception as e:
            print("产业链失败 %s" % e, flush=True)
            chain = []
            nindex = []
        print("  概念 %d 个" % len(ctop), flush=True)
    except Exception as e:
        ctop = []
        print("  概念失败: %s" % type(e).__name__, flush=True)

    # ---------- 板块推荐 ----------
    fmap = {x["name"]: x for x in flow_rows}
    picks = []
    for it in ind_top:
        f = fmap.get(it["name"], {})
        chg = f.get("chg", 0) or 0
        streak = f.get("streak", 0) or 0
        n = it["n"]
        mlb = it["max_lb"]
        sc = 0
        rs = []
        if chg >= 30:
            sc += 3; rs.append("资金大幅流入")
        elif chg >= 10:
            sc += 2; rs.append("资金流入")
        elif chg >= 0:
            sc += 1; rs.append("资金持平")
        else:
            rs.append("资金流出")
        if n >= 6:
            sc += 3; rs.append("%d家涨停" % n)
        elif n >= 3:
            sc += 2; rs.append("%d家涨停" % n)
        elif n >= 2:
            sc += 1; rs.append("%d家涨停" % n)
        else:
            rs.append("仅%d家涨停" % n)
        if mlb >= 4:
            sc += 2; rs.append("%d板龙头" % mlb)
        elif mlb >= 3:
            sc += 1; rs.append("%d板" % mlb)
        if streak >= 3:
            sc += 2; rs.append("占比连升%d天" % streak)
        elif streak >= 2:
            sc += 1; rs.append("占比连升%d天" % streak)
        if sc >= 8:
            lv = "强烈推荐"
        elif sc >= 6:
            lv = "推荐"
        elif sc >= 4:
            lv = "观察"
        else:
            lv = "回避"
        # 冰点/退潮期不给推荐级，板块强度只作参考
        if stage in ("冰点", "退潮") and lv in ("强烈推荐", "推荐"):
            lv = "观察"
            rs.append("情绪%s期，降级" % stage)
        picks.append(dict(name=it["name"], score=sc, level=lv, reasons=rs,
                          zt=n, max_lb=mlb, cat=it["cat"],
                          money=round(chg, 1), streak=streak,
                          names=it.get("names", [])[:4],
                          stocks=(it.get("stocks") or [])[:6]))
    picks.sort(key=lambda x: (-x["score"], -x["zt"]))
    picks = picks[:6]

    flow = dict(inflow=inflow, outflow=outflow,
                market=dict(amt=round(amt_today, 0), amt20=round(amt_avg, 0),
                            ratio=round(ratio, 2), trend=trend),
                rotation=dict(tops=tops[-6:], changes=changes, rotor=rotor,
                            top_n=top_n, cur=cur))

    zb_rate = len(zb) / max(1, len(zt) + len(zb))
    max_lb = max([int(x.get("lbc") or 1) for x in zt], default=0)
    strong = (len(zt) >= 45 and zb_rate <= 0.35 and max_lb >= 4)
    sent = dict(level="强情绪" if strong else "弱情绪", zt=len(zt), zb=len(zb),
                dt=len(dtp), zb_rate=round(zb_rate * 100, 1), max_lb=max_lb)

    brief = {}
    try:
        brief = build_brief(zt, zb, dtp, tblk, tdet, ind_top, stage, rotor, sent, max_lb)
    except Exception as e:
        print("  早盘解读失败 %s" % e, flush=True)

    lhb_date, lhb_rows, lhb_top, lhb_drank = "", [], [], []
    lhb_b = {}
    try:
        lhb_date, lhb_rows, lhb_top, lhb_drank = lhb_latest(dstr)
        lhb_b = lhb_brief(lhb_rows)
        print("  龙虎榜 %d 只（强信号 %d / 游资 %d / 机构 %d）"
              % (len(lhb_rows), len(lhb_top), lhb_b.get("youzi", 0), lhb_b.get("jigou", 0)), flush=True)
    except Exception as e:
        print("  龙虎榜失败 %s" % e, flush=True)

    pre = {}
    try:
        pre = premarket()
        print("  盘前解读 外盘%d 要闻%d 昨日涨停%d" % (len(pre["global_idx"]), len(pre["news"]), pre["zt"]), flush=True)
    except Exception as e:
        print("  盘前解读失败 %s" % e, flush=True)

    # 弱市抱团 · 分歧找核心
    grp = {}
    try:
        # 核心 = 逆势吸金总龙头（只用涨停/炸板/涨幅前排当候选）
        ccand = []
        for x in list(zt) + list(zb):
            cc = str(x.get("c") or "").zfill(6)
            if cc and cc not in ccand:
                ccand.append(cc)
        for c2, v2 in (snap or {}).items():
            if (v2.get("chg") or 0) >= 5 and c2 not in ccand:
                ccand.append(c2)
        cores = core_scan(ccand, snap)
        for x in cores:
            for xx in list(zt) + list(zb):
                if str(xx.get("c") or "").zfill(6) == x["c"]:
                    x["n"] = xx.get("n") or ""
                    x["ind"] = (xx.get("hybk") or "").strip()
                    break
            x.setdefault("n", "")
            x.setdefault("ind", "")
        print("  核心扫描 候选%d 命中%d" % (len(ccand), len(cores)), flush=True)

        grp = build_group(zt, zb, snap, stage,
                          round(100.0 * up / max(1, up + dn), 1), max_lb, cores)
        print("  抱团·核心  最高%d板 抱团度%.0f%% 核心%d只"
              % (grp["top_lb"], grp["conc"] * 100, len(grp["cores"])), flush=True)
    except Exception as e:
        print("  抱团·核心失败 %s" % e, flush=True)

    payload = dict(updated=bjnow().strftime("%Y-%m-%d %H:%M:%S"),
                   trade_date=trade_date, universe=len(uni),
                   brief=brief,
        group=grp, pre=pre,
                   market=dict(up=up, down=dn, total=len(snap),
                               temp=round(100.0 * up / max(1, up + dn), 1),
                               indexes=indexes()),
                   boom=rows, risk=risk[:300], history=[],
                   ladder=ladder, leaders=leaders, cycle=cycle, stage=stage, flow=flow, picks=picks, concept_top=ctop, wordplay=wplay, chain=chain, news=nindex, ind_top=ind_top, sentiment=sent,
                   ths_blocks=tblk,
                   ths_zt=[dict(code=k, name=v.get("name", ""), reason=v.get("reason", ""),
                                hd=v.get("hd", ""), seal=v.get("seal", 0),
                                hs=v.get("hs", 0), cval=v.get("cval", 0),
                                opn=v.get("opn", 0))
                           for k, v in tzt.items()],
                   ths_desc=tdet,
                   core=[r for r in rows if any(t in r["tags"] for t in
                         ("底共振", "生命线", "平台强破", "平台中破"))
                         and "ST" not in (r.get("name") or "").upper()][:60],
                   pits=[r for r in rows if "坑" in r["tags"]
                         and "ST" not in (r.get("name") or "").upper()][:80],
                   lhb=lhb_rows, lhb_top=lhb_top[:40], lhb_brief=lhb_b,
                   lhb_dept=lhb_drank, lhb_date=lhb_date,
                   fetch_ok=ok_fetch, fetch_total=len(uni), error=err)
    js = json.dumps(payload, ensure_ascii=False)
    open(os.path.join(DOCS, "data.json"), "w", encoding="utf-8").write(js)
    open(os.path.join(DOCS, "data.js"), "w", encoding="utf-8").write("window.__DATA__=" + js + ";")
    print("signals %d  temp %.1f%%" % (len(rows), payload["market"]["temp"]), flush=True)


if __name__ == "__main__":
    if "--auction" in sys.argv:
        do_auction(None)
    elif "--lhb" in sys.argv:
        do_lhb()
    else:
        try:
            main()
        except Exception:
            import traceback
            print("!!!!! scan_cloud 主流程崩溃，完整 traceback 如下 !!!!!", flush=True)
            traceback.print_exc()
            # 崩了也要留一份 data.json，否则整个 workflow 红叉 + 页面空白
            try:
                now = bjnow()
                err = dict(
                    trade_date=now.strftime("%Y-%m-%d"),
                    updated=now.strftime("%Y-%m-%d %H:%M:%S"),
                    universe=0, boom=[], auction=[], mode="", error="扫描崩溃：" +
                    (traceback.format_exc().strip().splitlines() or ["未知"])[-1][:200])
                js = json.dumps(err, ensure_ascii=False)
                io.open(os.path.join(DOCS, "data.json"), "w", encoding="utf-8").write(js)
                io.open(os.path.join(DOCS, "data.js"), "w", encoding="utf-8").write(
                    "window.__DATA__=" + js + ";")
                print("已写入占位 data.json（带 error 字段）", flush=True)
            except Exception:
                pass
            sys.exit(1)
