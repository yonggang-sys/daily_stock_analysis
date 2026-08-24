# -*- coding: utf-8 -*-
"""
ci_emit.py — CI 端取数 + 产出（替代本地 westock 管线）。

背景：
  本地 gen_report.py step1 依赖 westock-data node 包（已从沙箱消失），且沙箱屏蔽东财
  clist/push2his，本地无法复现「选股/热点/资金流」。GitHub Actions Runner 出网无屏蔽，
  可访问 qt.gtimg.cn + 东财全量接口，故把取数/产出整体迁到 CI。

产出（与本地 schema 完全一致，本地消费脚本字节级不变）：
  - 中间文件（供本地 build_reco.py / gen_stock_detail.py 原样解析）：
      _candA.md.._candU.md  _hot_board.md  _hot_rank.md  _hot_sectors.json
  - JSON（直接被仪表盘/报告消费）：
      reco.json  hotspot_leaders.json  holding_fundflow.json  dsa_decisions.json
  - latest.meta.json（发布时间戳，供本地拉取器判定新鲜度）

运行：
  CI：python ci_emit.py            # 全量：fetch + build + 写本地工作区
  本地测试：python ci_emit.py --offline --selftest   # 用内置样例数据跑 build_*，校验 schema

评分/筛选/产出逻辑均为本地 build_reco.py / holding_fundflow.py / build_report3.py /
gen_stock_detail.py 的忠实副本，保证「逻辑保持一致」。
"""
import os, sys, json, re, subprocess, time, io, argparse

# ============================ 工具 ============================
def http_get(url, timeout=15, retries=3, headers=None, use_curl=False):
    """返回 (text, ok)。优先 urllib；push2his 等东财接口在部分环境 urllib 被拦，可切 curl。"""
    hd = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.qq.com/"}
    if headers:
        hd.update(headers)
    last = None
    for i in range(retries):
        try:
            if use_curl:
                r = subprocess.run(["curl", "-s", "-m", str(timeout), "-A", hd["User-Agent"], url],
                                   capture_output=True, text=True, timeout=timeout + 5, shell=False)
                if r.returncode == 0 and r.stdout:
                    return r.stdout, True
                last = f"curl rc={r.returncode}"
                continue
            else:
                import urllib.request, ssl
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                req = urllib.request.Request(url, headers=hd)
                with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                    raw = resp.read()
                # 东财 push2/push2his 接口实际返回 UTF-8；个别老接口为 GBK。
                # 优先 UTF-8，失败再回退 GBK（避免把 UTF-8 字节当 GBK 解码产生乱码，如 鑸绌烘満鍦→机器人）。
                for _enc in ("utf-8", "gbk"):
                    try:
                        return raw.decode(_enc), True
                    except Exception:
                        continue
                return raw.decode("gbk", "ignore"), True
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            time.sleep(1.5 * (i + 1))
    return "", False


def num(v):
    if v in ('-', '', None):
        return None
    try:
        return float(v)
    except Exception:
        return None


def sh_prefix(code):
    return ("sh" if str(code).startswith(("6", "9")) else "sz") + str(code)


def em_secid(code):
    return ("1." if str(code).startswith(("6", "9")) else "0.") + str(code)


# ============================ 取数（仅 CI 出网环境有效）============================
QT_BULK = "https://qt.gtimg.cn/q="  # codes 逗号分隔，如 sh601318,sz000001
KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,320,qfq"
EM_SINGLE = "https://push2.eastmoney.com/api/qt/stock/get?secid={secid}&fields=f43,f57,f58,f116,f117,f162"
EM_CLIST = "https://push2.eastmoney.com/api/qt/clist/get?fs=m:90+t:2&fields=f12,f14,f3,f62,f104,f105,f128,f136,f207&pn=1&pz=80&po=1&fid=f3&ut=b2884a393a59ad640360834c4157f792"
EM_HOT = "https://push2.eastmoney.com/api/qt/clist/get?fs=m:90+t:3&fields=f12,f14,f3,f62,f104,f105,f128,f136,f207&pn=1&pz=80&po=1&fid=f3&ut=b2884a393a59ad640360834c4157f792"
EM_BOARD_MEMBERS = "https://push2.eastmoney.com/api/qt/clist/get?fs=b:{bk}+f:!50&fields=f12&pn=1&pz=1000&po=1&ut=b2884a393a59ad640360834c4157f792"
EM_FLOW = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get?lmt=30&klt=101&secid={secid}&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63"


def fetch_qt_bulk(codes):
    """codes: list['sh601318', ...] -> {code: {price,chg,mv,name,pe,pb}}  qt.gtimg 批量报价。"""
    out = {}
    for i in range(0, len(codes), 80):
        batch = codes[i:i + 80]
        url = QT_BULK + ",".join(batch)
        txt, ok = http_get(url, timeout=20)
        if not ok:
            print(f"[warn] qt bulk failed batch {i}: {txt[:80]}")
            continue
        for line in txt.split(";"):
            line = line.strip()
            if not line.startswith("v_"):
                continue
            m = re.match(r"v_(\w+)=\"([^\"]*)\"", line)
            if not m:
                continue
            c = m.group(1)
            f = m.group(2).split("~")
            # 字段索引参考 qt.gtimg 实测：1=名称 3=当前价 31=涨跌额 32=涨跌幅(%)
            # 38=换手率 39=市盈率(TTM) 43=振幅 44=流通市值(亿) 45=总市值(亿) 46=市净率 49=量比
            try:
                price = num(f[3]); chg = num(f[32]); mv = num(f[45])
                circ_mv = num(f[44]) if len(f) > 44 else None
                name = f[1] if len(f) > 1 else ""
                pe = num(f[39]) if len(f) > 39 else None
                pb = num(f[46]) if len(f) > 46 else None
            except (IndexError, ValueError):
                continue
            out[c] = {"price": price, "chg": chg, "mv": mv, "circ_mv": circ_mv,
                      "name": name, "pe": pe, "pb": pb}
    return out


def fetch_kline_52(code):
    """返回 (high52, low52, chg20, chg60) 或 None。qt kline（前复权日线）。"""
    url = KLINE.format(code=code)
    txt, ok = http_get(url, timeout=20)
    if not ok:
        return None
    try:
        j = json.loads(txt)
        # 注意：data 键为完整符号（如 'sh601808'），裸码仅作兜底（此前用裸码 KeyError 全挂）
        node = j["data"].get(code) or j["data"].get(code.lstrip("shsz")) or {}
        days = node.get("qfqday") or node.get("day")
        if not days or len(days) < 60:
            return None
        closes = [float(d[2]) for d in days if len(d) > 2]
        highs = [float(d[3]) for d in days if len(d) > 3]
        lows = [float(d[4]) for d in days if len(d) > 4]
        high52 = max(highs); low52 = min(lows)
        last = closes[-1]
        c20 = (last / closes[-21] - 1) * 100 if len(closes) > 21 else None
        c60 = (last / closes[-61] - 1) * 100 if len(closes) > 61 else None
        return {"high52": high52, "low52": low52, "c20": c20, "c60": c60}
    except Exception as e:
        print(f"[warn] kline {code}: {e}")
        return None


def fetch_em_valuation(codes):
    """codes: list['601318', ...] -> {code: {name,pe,pb,div,mv}}  东财单股（补充股息率）。"""
    out = {}
    for code in codes:
        sec = em_secid(code)
        url = EM_SINGLE.format(secid=sec)
        txt, ok = http_get(url, timeout=12)
        if not ok:
            continue
        try:
            d = json.loads(txt).get("data") or {}
            name = d.get("f58") or ""
            # 注意：该接口不返回 f9(PE)；f116=总市值(元)、f117 部分环境也回市值。
            # PE/PB 由 qt.gtimg(f39/f40) 主供，此处仅取股息率与市值。
            div_raw = num(d.get("f162"))
            div = (div_raw / 100.0) if (div_raw is not None and div_raw > 50) else div_raw  # f162 常为百分号×100
            mv = num(d.get("f116"))
            out[code] = {"name": name, "pe": None, "pb": None, "div": div, "mv": (mv / 1e8) if mv else None}
        except Exception:
            continue
    return out


def _valid_chg(v, limit=15.0):
    """板块涨跌幅合法性校验：A股板块涨跌幅上限 ±10%（放宽到 ±15 容差）。"""
    return v if (v is not None and abs(v) <= limit) else None


def fetch_em_sectors():
    """东财行业板块榜 -> [ {code,name,chg,lead_name,lead_pct,lead_code} ... ]。
    字段：f12=板块代码 f14=板块名 f3=涨跌幅 f104/f105=上涨/下跌家数（仅日志核对）
    f128=领涨股名 f136=领涨股涨跌幅 f207=领涨股代码。"""
    boards = []
    try:
        txt, ok = http_get(EM_CLIST, timeout=20)
        if ok:
            j = json.loads(txt).get("data") or {}
            items = (j.get("diff") or {}) if isinstance(j.get("diff"), dict) else {}
            first = True
            for k, v in items.items():
                if first:
                    # 原始首条打日志，便于核验字段语义（此前 f3 曾返回家数差类数据）
                    print(f"[dbg] clist first raw: {json.dumps(v, ensure_ascii=False)[:300]}")
                    first = False
                chg = _valid_chg(num(v.get("f3")))
                if chg is None:
                    # f3 语义错位时降级：上涨家数-下跌家数仅作排序参考，涨跌幅置 0
                    print(f"[warn] clist f3 越界，按 0 处理: {v.get('f14')} f3={v.get('f3')} f104={v.get('f104')} f105={v.get('f105')}")
                    chg = 0.0
                boards.append({"code": str(v.get("f12") or ""), "name": v.get("f14") or "",
                               "chg": chg, "lead_name": v.get("f128") or "",
                               "lead_pct": _valid_chg(num(v.get("f136")), limit=21.0),
                               "lead_code": str(v.get("f207") or "")})
    except Exception as e:
        print(f"[warn] clist: {e}")
    print(f"[info] 行业板块榜: {len(boards)} 条")
    return boards


def fetch_em_hotspot():
    """东财概念板块榜 -> 领涨股列表 [(concept_name, lead_name, lead_pct, lead_code)]。"""
    leads = []
    try:
        txt, ok = http_get(EM_HOT, timeout=20)
        if ok:
            j = json.loads(txt).get("data") or {}
            items = j.get("diff") or {}
            first = True
            for k, v in items.items():
                if first:
                    print(f"[dbg] hot first raw: {json.dumps(v, ensure_ascii=False)[:300]}")
                    first = False
                concept = v.get("f14") or ""      # 概念/板块名
                lead_name = v.get("f128") or ""   # 领涨股名
                lead_code = str(v.get("f207") or "")  # 领涨股代码
                pct = _valid_chg(num(v.get("f136")), limit=21.0)  # 领涨股涨跌幅（20cm 上限）
                if concept and lead_name:
                    leads.append((concept, lead_name, pct if pct is not None else 0.0, lead_code))
    except Exception as e:
        print(f"[warn] hot: {e}")
    print(f"[info] 概念热点领涨: {len(leads)} 条")
    return leads


def fetch_board_members(bk_code):
    """东财板块成分股 -> {bare_code,...}（供热点行业映射 universe）。"""
    out = set()
    if not bk_code or not str(bk_code).startswith("BK"):
        return out
    url = EM_BOARD_MEMBERS.format(bk=bk_code)
    txt, ok = http_get(url, timeout=15)
    if ok:
        try:
            j = json.loads(txt).get("data") or {}
            items = j.get("diff") or {}
            for k, v in items.items():
                c = v.get("f12")
                if c:
                    out.add(str(c))
        except Exception as e:
            print(f"[warn] members {bk_code}: {e}")
    return out


def fetch_em_fundflow(code):
    """东财个股主力资金流（日级）-> {1d,3d,5d,10d,20d} 主力净流入(元)。push2his。"""
    sec = em_secid(code)
    url = EM_FLOW.format(secid=sec)
    txt, ok = http_get(url, timeout=20, use_curl=True)
    if not ok:
        # 回退 urllib
        txt, ok = http_get(url, timeout=20, use_curl=False)
    if not ok:
        return None
    try:
        klines = (json.loads(txt).get("data") or {}).get("klines") or []
        # 每行: 日期,主力净流入(f52),主力净占比,...
        mains = []
        for k in klines:
            parts = k.split(",")
            if len(parts) > 1:
                v = num(parts[1])
                if v is not None:
                    mains.append(v)
        if not mains:
            return None
        def tail(n):
            s = mains[-n:] if len(mains) >= n else mains
            return float(sum(s))
        return {"1d": tail(1), "3d": tail(3), "5d": tail(5), "10d": tail(10), "20d": tail(20)}
    except Exception as e:
        print(f"[warn] fundflow {code}: {e}")
        return None


# ============================ 中间文件产出（与 westock 同格式）============================
CAND_COLS = ["code", "name", "price", "change_percent", "pe_ratio", "pb_ratio",
             "dividend_ratio_ttm", "chg_20d", "chg_60d", "high_52week", "low_52week", "total_market_cap"]


def emit_cand_markdown(path, rows):
    """rows: {sh/sz+code: {name,price,chg,pe,pb,div,mv,high52,low52,c20,c60}} -> markdown 表。"""
    lines = ["| " + " | ".join(CAND_COLS) + " |",
             "|" + "---|" * len(CAND_COLS)]
    for code, r in rows.items():
        cells = [code, r.get("name", ""),
                 f'{r.get("price") or 0:.2f}', f'{r.get("chg") or 0:.2f}',
                 f'{r.get("pe") or 0:.2f}', f'{r.get("pb") or 0:.2f}',
                 f'{r.get("div") or 0:.2f}', f'{r.get("c20") or 0:.2f}',
                 f'{r.get("c60") or 0:.2f}', f'{r.get("high52") or 0:.2f}',
                 f'{r.get("low52") or 0:.2f}', f'{r.get("mv") or 0:.2f}']
        lines.append("| " + " | ".join(cells) + " |")
    open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")


def emit_hot_board(path, boards):
    """boards: [{code,name,chg,...}] -> | index | name | rank | zdf | 表。"""
    lines = ["| index | name | rank | zdf |", "|---|---|---|---|"]
    for i, b in enumerate(boards):
        lines.append(f"| {i} | {b['name']} | {i + 1} | {b['chg']:.2f} |")
    open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")


def emit_hot_rank(path, leads):
    """leads: [(concept, name, pct, code)] -> | concept | code | name | pct | 表（供 parse_hotspot_leaders）。"""
    lines = ["| concept | code | name | pct |", "|---|---|---|---|"]
    for concept, name, pct, code in leads:
        lines.append(f"| {concept} | {code or ''} | {name} | {pct:.2f} |")
    open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")


def emit_hot_sectors(path, mapping):
    """mapping: {sh/sz+code: sector_name} -> JSON。"""
    json.dump(mapping, open(path, "w", encoding="utf-8"), ensure_ascii=False)


# ============================ 评分/产出（与本地逻辑一致）============================
def build_reco_json(ws):
    """忠实复制 build_reco.py 的 parse_file/enrich/score/pullback/reason/group 逻辑。"""
    def parse_file(path):
        text = open(path, encoding="utf-8").read()
        lines = [l for l in text.splitlines() if l.strip().startswith("|")]
        hdr = next((l for l in lines if l.strip().startswith("| code")), None)
        if not hdr:
            return {}, []
        cols = [c.strip() for c in hdr.strip().strip("|").split("|")]
        ci = {n: i for i, n in enumerate(cols)}
        out = {}
        for l in lines:
            if l.strip().startswith("| code") or "---" in l:
                continue
            cells = [c.strip() for c in l.strip().strip("|").split("|")]
            code = cells[0]
            if code.startswith(("sz", "sh")) and len(cells) > ci.get("pe_ratio", 20):
                out[code] = {
                    "code": code[2:], "name": cells[ci["name"]],
                    "price": num(cells[ci["price"]]), "chg": num(cells[ci["change_percent"]]),
                    "pe": num(cells[ci["pe_ratio"]]), "pb": num(cells[ci["pb_ratio"]]),
                    "div": num(cells[ci["dividend_ratio_ttm"]]) or 0.0,
                    "c20": num(cells[ci["chg_20d"]]), "c60": num(cells[ci["chg_60d"]]),
                    "high": num(cells[ci["high_52week"]]), "low": num(cells[ci["low_52week"]]),
                    "mv": num(cells[ci["total_market_cap"]]),
                }
        return out, cols

    def parse_hot_board(path):
        text = open(path, encoding="utf-8").read()
        lines = [l for l in text.splitlines() if l.strip().startswith("|")]
        hdr = next((l for l in lines if l.strip().startswith("| index")), None)
        hot = {}
        if hdr:
            cols = [c.strip() for c in hdr.strip().strip("|").split("|")]
            ci = {n: i for i, n in enumerate(cols)}
            for l in lines:
                if l.strip().startswith("| index") or "---" in l:
                    continue
                cells = [c.strip() for c in l.strip().strip("|").split("|")]
                try:
                    hot[cells[ci["name"]]] = {"rank": int(cells[ci["rank"]]), "chg": float(cells[ci["zdf"]])}
                except (ValueError, IndexError, KeyError):
                    pass
        return hot

    hot = parse_hot_board(f"{ws}/_hot_board.md")
    HOT_RANK_CUTOFF = 10

    def pullback(r):
        c20, c60 = r.get("c20"), r.get("c60")
        if c20 is None or c60 is None:
            return False, 0.0, ""
        if c60 < 6:
            return False, 0.0, ""
        rel = c60 - c20
        if rel < 6:
            return False, 0.0, ""
        if c20 > 5:
            return False, 0.0, ""
        if c20 < -35:
            return False, 0.0, ""
        depth = -c20
        if 5 <= depth <= 25:
            q = 15
        elif depth < 5:
            q = 9
        else:
            q = 6
        return True, q, f"回踩{depth:.0f}%"

    def score(r):
        pe, roe, pos, div = r["pe"], r["roe"], r["pos52"], r["div"] or 0
        if not pe or pe <= 0:
            return 0
        if pe <= 12: vs = 40
        elif pe <= 20: vs = 32
        elif pe <= 30: vs = 24
        elif pe <= 45: vs = 16
        elif pe <= 60: vs = 8
        else: vs = 0
        if roe is None: qs = 4
        elif roe >= 15: qs = 30
        elif roe >= 12: qs = 24
        elif roe >= 8: qs = 18
        elif roe >= 5: qs = 10
        else: qs = 4
        if pos is None: ps = 9
        elif pos <= 30: ps = 20
        elif pos <= 50: ps = 15
        elif pos <= 70: ps = 9
        else: ps = 3
        if div >= 3: rs = 10
        elif div >= 2: rs = 7
        elif div >= 1: rs = 4
        else: rs = 1
        return vs + qs + ps + rs

    def reason(r):
        pe = r["pe"]; roe = r["roe"]; pos = r["pos52"]; div = r["div"] or 0
        bits = []
        if pe and pe > 0: bits.append(f"PE{pe:.0f}倍")
        if roe is not None: bits.append(f"ROE{roe:.0f}%")
        if div >= 1: bits.append(f"股息{div:.1f}%")
        if pos is not None: bits.append(f"52周{pos:.0f}%分位")
        if r.get("pb_label"): bits.append(r["pb_label"])
        return "·".join(bits)

    def safe_parse(p):
        if not os.path.exists(p):
            return {}, []
        return parse_file(p)

    A, _ = safe_parse(f"{ws}/_candA.md")
    B, _ = safe_parse(f"{ws}/_candB.md")
    C, _ = safe_parse(f"{ws}/_candC.md")
    D, _ = safe_parse(f"{ws}/_candD.md")
    U, _ = safe_parse(f"{ws}/_candU.md")
    allc = {**A, **B, **C, **D, **U}

    _hot_sec = {}
    try:
        _hot_sec = json.load(open(f"{ws}/_hot_sectors.json", encoding="utf-8"))
    except Exception:
        pass

    def enrich(r):
        pe, pb, high, low, price = r["pe"], r["pb"], r["high"], r["low"], r["price"]
        r["roe"] = round(pb / pe * 100, 1) if (pe and pe > 0 and pb) else None
        r["pos52"] = round((price - low) / (high - low) * 100, 1) if (high and low and high > low) else None
        r["dd_high"] = round((price - high) / high * 100, 1) if (high and price) else None
        if r["mv"]:
            r["mv_yi"] = round(r["mv"], 1)  # mv 已是亿元（qt f45 / 东财 f116/1e8）
        r["pullback"], r["pb_score"], r["pb_label"] = pullback(r)
        return r

    cands = []
    for _key, _r in allc.items():
        rr = enrich(dict(_r))
        rr["score"] = score(rr)
        rr["reason"] = reason(rr)
        _bn = _hot_sec.get(_key)
        _h = hot.get(_bn, {}) if _bn else {}
        rr["sector"] = _bn
        rr["heat_rank"] = _h.get("rank")
        rr["sector_chg"] = _h.get("chg")
        rr["hot"] = _bn is not None
        rr["cat"] = ("hot_highpe" if (rr["pe"] and rr["pe"] > 50) else "hot_lowval") if _bn else "not_hot"
        rr["fund_ok"] = (rr["roe"] is not None and rr["roe"] >= 8)
        rr["val_ok"] = bool(rr["pe"] and rr["pe"] > 0 and rr["pe"] < 50 and rr["pullback"])
        rr["quad_ok"] = rr["fund_ok"] and rr["val_ok"] and rr["hot"] and rr["pullback"]
        cands.append(rr)

    def triple_ok(r):
        return bool(r["pe"] and r["pe"] > 0 and r["pe"] < 50 and r["hot"] and r["pullback"])

    def combined(r):
        return r["score"] + (12 if r["hot"] else 0) + r.get("pb_score", 0)

    quad = sorted([r for r in cands if r["quad_ok"]], key=combined, reverse=True)
    triple = sorted([r for r in cands if triple_ok(r)], key=combined, reverse=True)
    low_hot = sorted([r for r in cands if r["pe"] and r["pe"] > 0 and r["pe"] < 50 and r["hot"] and not r["pullback"]], key=lambda x: x["score"], reverse=True)
    low_pb = sorted([r for r in cands if r["pe"] and r["pe"] > 0 and r["pe"] < 50 and r["pullback"] and not r["hot"]], key=lambda x: x["score"], reverse=True)
    hot_hi = sorted([r for r in cands if r["hot"] and not (r["pe"] and r["pe"] > 0 and r["pe"] < 50)], key=lambda x: x["score"], reverse=True)

    groups = []
    if quad:
        groups.append({"group": "✅ 四重符合（基本面良好×低估值×阶段热点×回踩调整）", "board_name": None, "cat": "quad",
                       "hot": True, "heat_rank": None, "sector_chg": None, "tag": "四条件全中·最优选区（优先关注）", "items": quad[:8]})
    else:
        groups.append({"group": "✅ 四重符合（基本面良好×低估值×阶段热点×回踩调整）", "board_name": None, "cat": "quad",
                       "hot": True, "heat_rank": None, "sector_chg": None, "tag": "当前市场无四条件全中标的（见下方分层/行业细分）", "items": []})
    if triple:
        groups.append({"group": "✅ 三重符合（低估值×阶段热点×回踩调整）", "board_name": None, "cat": "triple",
                       "hot": True, "heat_rank": None, "sector_chg": None, "tag": "三重符合·回踩买点区（优先关注）", "items": triple[:6]})
    else:
        groups.append({"group": "✅ 三重符合（低估值×阶段热点×回踩调整）", "board_name": None, "cat": "triple",
                       "hot": True, "heat_rank": None, "sector_chg": None, "tag": "当前市场无完全符合三重条件的标的（见下方分层）", "items": []})
    if low_hot:
        groups.append({"group": "⚠️ 估值偏低×热点（未回踩·需回踩方能入池）", "board_name": None, "cat": "lowhot",
                       "hot": True, "heat_rank": None, "sector_chg": None, "tag": "估值偏低(PE<50)+热点，但未回踩，不满足入池条件，等回踩确认后再介入", "items": low_hot[:4]})
    if low_pb:
        groups.append({"group": "低估值×回踩（非当前热点）", "board_name": None, "cat": "lowpb",
                       "hot": False, "heat_rank": None, "sector_chg": None, "tag": "低估值+回踩，但不在热点行业，弹性与资金关注较弱", "items": low_pb[:4]})
    if hot_hi:
        groups.append({"group": "仅热点（估值偏高·仅参考）", "board_name": None, "cat": "hothi",
                       "hot": True, "heat_rank": None, "sector_chg": None, "tag": "热点但PE≥50，非低估值优选(低估值要求PE<50且回踩)，仅作参考", "items": hot_hi[:4]})

    _seen_sec = {}
    for r in cands:
        s = r.get("sector")
        if s:
            _seen_sec.setdefault(s, []).append(r)
    for s, items in _seen_sec.items():
        items.sort(key=lambda x: (x.get("pb_score", 0), x["score"]), reverse=True)
        rank = (hot.get(s) or {}).get("rank")
        hot_now = rank is not None and rank <= HOT_RANK_CUTOFF
        hot_tag = f"阶段热点(#第{rank}名)" if hot_now else "非当前热点"
        has_triple = any(triple_ok(x) for x in items)
        if hot_now:
            tag2 = f"{hot_tag}·低估值优选" + ("·回踩调整" if has_triple else "·当前未回踩(暂观望)")
        else:
            tag2 = "非当前热点行业·长期观察"
        cat2 = "hot_lowval" if hot_now else "not_hot"
        groups.append({"group": s, "board_name": s, "cat": cat2, "hot": hot_now, "heat_rank": rank,
                       "sector_chg": (hot.get(s) or {}).get("chg"), "tag": tag2, "items": items[:3]})

    out = {"hot": hot, "groups": groups}
    json.dump(out, open(f"{ws}/reco.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return out


def build_hotspot_leaders(ws, name_to_code=None):
    """解析 _hot_rank.md（emit_hot_rank 4 列格式：| concept | code | name | pct |）-> hotspot_leaders.json。
    兼容旧 2 列格式（| concept | name(pct) |）。"""
    path = f"{ws}/_hot_rank.md"
    if not os.path.exists(path):
        return []
    rows = []
    in_table = False
    with open(path, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if s.startswith("|") and "concept" in s:
                in_table = True
                continue
            if s.startswith("|") and in_table:
                if set(s) <= set("|- "):
                    continue
                parts = [p.strip() for p in s.strip("|").split("|")]
                if len(parts) >= 4 and parts[0] and parts[2]:
                    # 新 4 列：concept | code | name | pct
                    concept, code, name, pct = parts[0], parts[1], parts[2], parts[3]
                    try:
                        pct = float(pct)
                    except ValueError:
                        continue
                    if not code:
                        code = (name_to_code or {}).get(name)
                    rows.append({"name": name, "pct": pct, "concept": concept, "code": code or None})
                    continue
                if len(parts) < 2:
                    continue
                # 旧 2 列回退：concept | name(pct)
                concept = parts[0]
                lead = parts[-1]
                m = re.match(r"^(.+?)\((\-?\d+\.?\d*)\)$", lead)
                if not m:
                    continue
                name = m.group(1).strip()
                pct = float(m.group(2))
                code = (name_to_code or {}).get(name)
                rows.append({"name": name, "pct": pct, "concept": concept, "code": code})
    if rows:
        json.dump(rows, open(f"{ws}/hotspot_leaders.json", "w", encoding="utf-8"), ensure_ascii=False)
    return rows


def build_holding_fundflow(ws, codes, names=None):
    """codes: 裸码列表（如 ['601318','000001']，= 候选宇宙含 ci_universe_extra 的持仓/观测股）。
    用东财 push2his 主力净流入 + qt 报价，复刻 holding_fundflow.json schema（key=裸码）。
    与本地 holding_fundflow.py 输出字段完全一致；仪表盘按持仓裸码查表即可。"""
    names = names or {}
    out = {}
    sym_map = [(c, sh_prefix(c)) for c in codes]
    qt = fetch_qt_bulk([s for _, s in sym_map])
    for bare, sym in sym_map:
        fl = fetch_em_fundflow(bare)
        q = qt.get(sym, {})
        k = fetch_kline_52(sym)
        price = q.get("price") or 0
        high52 = (k or {}).get("high52") or 0
        low52 = (k or {}).get("low52") or 0
        circ_mv_raw = q.get("circ_mv") or q.get("mv")  # 亿元（qt f44 流通市值 / f45 总市值，已是亿元）
        main1 = (fl or {}).get("1d", 0.0) or 0.0
        main3 = (fl or {}).get("3d", 0.0) or 0.0
        main5 = (fl or {}).get("5d", 0.0) or 0.0
        main10 = (fl or {}).get("10d", 0.0) or 0.0
        main20 = (fl or {}).get("20d", 0.0) or 0.0
        circ_mv_yi = circ_mv_raw if circ_mv_raw else 0
        drawdown_high = (price - high52) / high52 * 100 if high52 else 0
        rise_from_low = (price - low52) / low52 * 100 if low52 else 0
        distribution_ratio = main20 / (circ_mv_yi * 1e8) if circ_mv_yi else 0
        out[bare] = {
            "name": names.get(bare, ""), "code": bare, "price": price,
            "main_netflow_1d": round(main1, 0), "main_netflow_3d": round(main3, 0),
            "main_netflow_5d": round(main5, 0), "main_netflow_10d": round(main10, 0),
            "main_netflow_20d": round(main20, 0),
            "high_52w": high52, "low_52w": low52,
            "drawdown_from_high": round(drawdown_high, 1), "rise_from_low": round(rise_from_low, 1),
            "distribution_ratio": round(distribution_ratio, 4),
        }
        print(f"  {names.get(bare, bare)}({bare}): 20日主力净流出={main20/1e8:.1f}亿 较年内高回撤={drawdown_high:.1f}% 派发强度={distribution_ratio:.3f}")
    json.dump(out, open(f"{ws}/holding_fundflow.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return out


def build_dsa_decisions(ws):
    """忠实复制 build_report3.py 的 decision_engine，运行于 CI 的 dsa_data.json -> dsa_decisions.json。"""
    dsa = f"{ws}/dsa_data.json"
    if not os.path.exists(dsa):
        print("[skip] dsa_data.json 不存在，跳过 dsa_decisions.json")
        return None
    DATA = json.load(open(dsa, encoding="utf-8"))
    SECTOR = {}  # CI 端可不提供板块估算；用 dsa 自带 sector 或 '其他'
    ST_SET = set()

    def g(d, *keys, default=None):
        for k in keys:
            if isinstance(d, dict) and k in d and d[k] is not None:
                return d[k]
        return default

    def decision_engine(rec):
        code = rec["code"]
        name = rec.get("name") or code
        rt = rec.get("realtime") or {}
        dly = rec.get("daily") or {}
        st = code in ST_SET
        price = g(rt, "price") or g(dly, "close")
        chg = g(rt, "change_pct") or g(dly, "last_pct_chg")
        pe = g(rt, "pe_ratio")
        pb = g(rt, "pb_ratio")
        mv = g(rt, "total_mv")
        vol_ratio = g(rt, "volume_ratio")
        turnover = g(rt, "turnover_rate")
        close = g(dly, "close")
        ma5, ma10, ma20, ma60 = g(dly, "ma5"), g(dly, "ma10"), g(dly, "ma20"), g(dly, "ma60")
        macd_hist = g(dly, "macd_hist"); macd_hist_prev = g(dly, "macd_hist_prev")
        kdj_k, kdj_d, kdj_j = g(dly, "kdj_k"), g(dly, "kdj_d"), g(dly, "kdj_j")
        kdj_kp, kdj_dp = g(dly, "kdj_k_prev"), g(dly, "kdj_d_prev")
        boll_u, boll_m, boll_l = g(dly, "boll_upper"), g(dly, "boll_mid"), g(dly, "boll_lower")
        pct_from_high = g(dly, "pct_from_high_52w")
        ret_20d = g(dly, "ret_20d"); ret_60d = g(dly, "ret_60d")
        high_52w = g(dly, "high_52w"); low_52w = g(dly, "low_52w")
        trend = "震荡"; bull = 0
        if close and ma5 and ma10 and ma20:
            if close > ma5 > ma10 > ma20:
                trend = "多头排列"; bull = 2
            elif close > ma20:
                trend = "中期偏强"; bull = 1
            elif close < ma5 < ma10 < ma20:
                trend = "空头排列"; bull = -2
            elif close < ma20:
                trend = "中期偏弱"; bull = -1
        macd_sig = 0
        if macd_hist is not None:
            if macd_hist > 0 and (macd_hist_prev is None or macd_hist >= macd_hist_prev):
                macd_sig = 1
            elif macd_hist > 0:
                macd_sig = 0.5
            elif macd_hist < 0 and (macd_hist_prev is None or macd_hist <= macd_hist_prev):
                macd_sig = -1
            elif macd_hist < 0:
                macd_sig = -0.5
        kdj_sig = 0
        if kdj_k is not None and kdj_d is not None:
            if kdj_k > kdj_d and (kdj_kp is None or kdj_k >= kdj_kp):
                kdj_sig = 1
            elif kdj_k > kdj_d:
                kdj_sig = 0.5
            elif kdj_k < kdj_d:
                kdj_sig = -1
        trend_score = bull * 1.0 + macd_sig * 1.0 + kdj_sig * 1.0
        val_score = 0; val_note = []
        if pe is not None:
            if pe <= 0:
                val_note.append("TTM亏损(PE为负)"); val_score -= 1.5
            elif pe < 15:
                val_score += 1.5; val_note.append(f"估值偏低 PE {pe:.1f}")
            elif pe < 35:
                val_score += 0.5; val_note.append(f"估值中性 PE {pe:.1f}")
            elif pe > 80:
                val_score -= 1; val_note.append(f"估值偏高 PE {pe:.1f}")
            else:
                val_note.append(f"PE {pe:.1f}")
        if pb is not None:
            val_note.append(f"PB {pb:.2f}")
        pos_score = 0; risk = []
        if pct_from_high is not None:
            if pct_from_high <= -50:
                risk.append(f"距52周高点深套 {pct_from_high:.0f}%"); pos_score -= 1.5
            elif pct_from_high <= -30:
                risk.append(f"较52周高点回撤 {pct_from_high:.0f}%"); pos_score -= 0.5
            elif pct_from_high >= -10:
                pos_score += 0.5
        if st:
            risk.append("ST/*ST 股，退市/波动风险高"); pos_score -= 2.5; val_score -= 1
        if macd_sig == -1:
            risk.append("MACD绿柱放大，动能走弱")
        if kdj_k is not None and kdj_k > 80:
            risk.append(f"KDJ超买(K={kdj_k:.0f})")
        if kdj_k is not None and kdj_k < 20:
            pos_score += 0.5
        if ret_20d is not None and ret_20d <= -20:
            risk.append(f"20日跌幅 {ret_20d:.0f}%")
        act_note = []
        if vol_ratio is not None:
            if vol_ratio >= 1.5:
                act_note.append(f"放量(量比{vol_ratio:.2f})")
            elif vol_ratio <= 0.6:
                act_note.append(f"缩量(量比{vol_ratio:.2f})")
        if turnover is not None:
            act_note.append(f"换手{turnover:.2f}%")
        raw = trend_score * 6 + val_score * 8 + pos_score * 9 + (1 if (vol_ratio and vol_ratio >= 1.2) else 0) * 2
        score = max(0, min(100, round(50 + raw)))
        verdict = "观望"
        if score >= 62 and trend in ("多头排列", "中期偏强") and not st:
            verdict = "关注/买入"
        elif score <= 38 or st or (pct_from_high is not None and pct_from_high <= -50):
            verdict = "谨慎/回避"
        sup, res = [], []
        if boll_l: sup.append(("BOLL下轨", round(boll_l, 2)))
        if ma20: sup.append(("MA20", round(ma20, 2))); res.append(("MA20", round(ma20, 2)))
        if ma60: res.append(("MA60", round(ma60, 2)))
        if boll_u: res.append(("BOLL上轨", round(boll_u, 2)))
        if low_52w: sup.append(("52周低", round(low_52w, 2)))
        if high_52w: res.append(("52周高", round(high_52w, 2)))
        rationale = [trend]
        if val_note: rationale.append(val_note[0])
        if pct_from_high is not None: rationale.append(f"距高点{pct_from_high:.0f}%")
        if act_note: rationale.append(act_note[0])
        if st: rationale.append("ST高风险")
        return {
            "code": code, "name": name, "price": price, "chg": chg, "pe": pe, "pb": pb,
            "mv": mv, "vol_ratio": vol_ratio, "turnover": turnover,
            "trend": trend, "score": score, "verdict": verdict,
            "macd_hist": macd_hist, "kdj_k": kdj_k, "kdj_d": kdj_d,
            "ma5": ma5, "ma20": ma20, "ma60": ma60,
            "boll_u": boll_u, "boll_m": boll_m, "boll_l": boll_l,
            "pct_from_high": pct_from_high, "ret_20d": ret_20d, "ret_60d": ret_60d,
            "high_52w": high_52w, "low_52w": low_52w, "sector": SECTOR.get(code, "其他"),
            "risk": risk, "val_note": val_note, "act_note": act_note,
            "sup": sup, "res": res, "rationale": "；".join(rationale),
            "has_chip": "chip" in rec and bool(rec.get("chip")),
            "has_fund": "fundamental" in rec and bool(rec.get("fundamental") and rec["fundamental"].get("status") == "partial"),
        }

    decisions = [decision_engine(rec) for rec in (DATA[c] for c in DATA)]
    import statistics
    scores = [d["score"] for d in decisions]
    gainers = [d for d in decisions if (d["chg"] or 0) > 0]
    losers = [d for d in decisions if (d["chg"] or 0) < 0]
    avg_score = round(statistics.mean(scores), 1) if scores else 0
    avg_draw = round(statistics.mean([d["pct_from_high"] for d in decisions if d["pct_from_high"] is not None]), 1) if any(d["pct_from_high"] is not None for d in decisions) else 0
    sector_cnt = {}
    for d in decisions:
        sector_cnt[d["sector"]] = sector_cnt.get(d["sector"], 0) + 1
    tmt = sum(1 for d in decisions if d["sector"].startswith("TMT"))
    buy = [d for d in decisions if d["verdict"] == "关注/买入"]
    avoid = [d for d in decisions if d["verdict"] == "谨慎/回避"]
    summary = {"n": len(decisions), "avg_score": avg_score, "avg_draw": avg_draw,
               "gainers": len(gainers), "losers": len(losers), "sector_cnt": sector_cnt,
               "tmt": tmt, "buy": len(buy), "avoid": len(avoid)}
    out = {"summary": summary, "decisions": decisions}
    json.dump(out, open(f"{ws}/dsa_decisions.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)
    print(f"DECISIONS_OK n={summary['n']} avg_score={avg_score} tmt={tmt} buy={len(buy)} avoid={len(avoid)}")
    return out


# ============================ 主流程 ============================
def _build_cand_codes(ws):
    """A-D 骨架 + 可选 ci_universe_extra.json（裸码列表，用户提交到仓库以纳入持仓/观测股）。"""
    base = {
        "A": "sh601808,sh603619,sh600583,sh600871,sz300164,sh603727,sz002828,sz300191,sh601881,sh601688,sh600030,sh601211,sh600999,sh600958,sh601377,sz000776",
        "B": "sh601607,sh603229,sh688222,sh600276,sz000963,sz002422,sh600196,sz002294,sz300347,sh600085",
        "C": "sz300661,sz300782,sh603501,sh603986,sz002049,sh688008,sz300316",
        "D": "sz000063,sh600522,sh600487,sh600498,sz002396,sz002138,sz300408,sz000636,sz002463,sz002415,sz002236,sz000977,sh603019",
    }
    # 扩大扫描宇宙（热点行业成分股）：此处用骨架 + 扩展占位；CI 端可由 fetch_em_sectors 增补
    extra = []
    ext = f"{ws}/ci_universe_extra.json"
    if os.path.exists(ext):
        try:
            extra = json.load(open(ext, encoding="utf-8")).get("codes", [])
            print(f"[info] 载入 ci_universe_extra.json: {len(extra)} 只")
        except Exception as e:
            print(f"[warn] 读 ci_universe_extra.json 失败: {e}")
    if extra:
        base["D"] = base["D"] + "," + ",".join(("sh" if c.startswith(("6", "9")) else "sz") + c for c in extra)
    return base


def run_full(ws):
    t0 = time.time()
    cand = _build_cand_codes(ws)
    # 1) 取候选行情 + 技术面（qt.gtimg 批量 + kline + 东财单股估值）
    all_codes = []
    for codes in cand.values():
        all_codes += [c for c in codes.split(",") if c]
    bare = [c[2:] for c in all_codes]
    qt = fetch_qt_bulk(all_codes)
    kls = {}
    for c in all_codes:
        kls[c] = fetch_kline_52(c)
    emv = fetch_em_valuation(bare)
    rows = {}
    for c in all_codes:
        bare_c = c[2:]
        k = kls.get(c) or {}
        e = emv.get(bare_c) or {}
        q = qt.get(c) or {}
        # 名称/PE/PB 由 qt.gtimg 主供（f1/f39/f40 实测可靠），东财单股补股息率/市值
        rows[c] = {
            "name": q.get("name") or e.get("name") or "",
            "price": q.get("price") or 0, "chg": q.get("chg") or 0,
            "pe": q.get("pe") or e.get("pe") or 0, "pb": q.get("pb") or e.get("pb") or 0,
            "div": e.get("div") or 0,
            "mv": e.get("mv") or q.get("mv") or 0,
            "high52": k.get("high52") or 0, "low52": k.get("low52") or 0,
            "c20": k.get("c20") or 0, "c60": k.get("c60") or 0,
        }
    # 写 _candA~U.md（名称已由 qt 主供写入 rows）
    keys = list(cand.keys())  # A,B,C,D, U 由 build_universe 扩；此处骨架到 D，U 同 D 占位
    for k in ["A", "B", "C", "D"]:
        emit_cand_markdown(f"{ws}/_cand{k}.md", {c: rows[c] for c in cand[k].split(",") if c})
    emit_cand_markdown(f"{ws}/_candU.md", rows)  # 全量宇宙（含扩展）
    # 2) 热点行业榜 + 板块成分映射 + 热点领涨
    boards = fetch_em_sectors()
    emit_hot_board(f"{ws}/_hot_board.md", boards)
    # 板块成分映射：对 top10 热点行业逐个拉成分股（东财板块成分接口），
    # 将 universe 中命中成分的股票映射到板块名（旧版用「板块名 in 股票名」子串匹配，恒为空）
    hot_sec = {}
    for b in boards[:10]:
        members = fetch_board_members(b.get("code"))
        if not members:
            continue
        for c in all_codes:
            if c[2:] in members:
                hot_sec[c] = b["name"]
        print(f"[info] 热点行业 {b['name']}: 成分 {len(members)} 只，universe 命中 {sum(1 for c in all_codes if c[2:] in members)} 只")
    emit_hot_sectors(f"{ws}/_hot_sectors.json", hot_sec)
    leads = fetch_em_hotspot()
    emit_hot_rank(f"{ws}/_hot_rank.md", leads)
    # 3) 评分/产出（逻辑与本地一致）
    #    注：dsa_decisions.json（诊股 verdict）由本地 build_report3.py 产出（0 token 规则引擎），
    #    其数据层 dsa_data.json 来自本地 dsa_fetch（efinance/akshare，亦 0 token），不迁 CI，
    #    避免持仓 universe 在 CI 端缺失导致 verdict 对持仓失效。
    build_reco_json(ws)
    build_hotspot_leaders(ws)
    names = {c[2:]: rows[c].get("name", "") for c in all_codes}
    build_holding_fundflow(ws, bare, names=names)
    # 4) meta
    meta = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "source": "ci-emit",
            "universe_size": len(all_codes)}
    json.dump(meta, open(f"{ws}/latest.meta.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"== CI EMIT DONE in {int(time.time()-t0)}s == reco/hotspot/holding_fundflow/dsa_decisions 已产出")


def selftest(ws):
    """用内置样例数据验证 build_* 输出 schema 与本地一致（离线、无需网络）。"""
    import tempfile
    d = tempfile.mkdtemp(prefix="ci_emit_test_")
    # 构造样例 _candA.md
    sample = {
        "sh601318": {"name": "中国平安", "price": 52.6, "chg": 0.5, "pe": 9.2, "pb": 1.1, "div": 4.5,
                      "mv": 9500.0, "high52": 60.0, "low52": 40.0, "c20": -8.0, "c60": 12.0},
        "sz000001": {"name": "平安银行", "price": 11.2, "chg": -1.0, "pe": 4.8, "pb": 0.6, "div": 3.0,
                      "mv": 2200.0, "high52": 14.0, "low52": 10.0, "c20": 2.0, "c60": 8.0},
    }
    emit_cand_markdown(f"{d}/_candA.md", sample)
    emit_hot_board(f"{d}/_hot_board.md", [{"code": "BK0474", "name": "保险Ⅱ", "chg": 2.3},
                                          {"code": "BK0475", "name": "银行", "chg": 1.1}])
    emit_hot_sectors(f"{d}/_hot_sectors.json", {"sh601318": "保险Ⅱ", "sz000001": "银行"})
    emit_hot_rank(f"{d}/_hot_rank.md", [("AI", "科大讯飞", 5.2, "002230"), ("机器人", "拓斯达", 3.1, "300607")])
    reco = build_reco_json(d)
    leads = build_hotspot_leaders(d, name_to_code={"科大讯飞": "002230", "拓斯达": "300607"})
    # 断言关键 schema
    assert "groups" in reco and "hot" in reco, "reco.json schema 缺失"
    assert any(g["cat"] == "quad" for g in reco["groups"]), "缺少 quad 分组"
    assert leads and leads[0]["code"] == "002230", "hotspot_leaders 解析/解析码失败"
    print(f"[selftest] reco groups={len(reco['groups'])} hotspot={len(leads)} OK")
    print("[selftest] 样例 reco 四重符合:", [it['code'] for g in reco['groups'] if g['cat']=='quad' for it in g['items']])
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="仅跑 build_*（不取数）")
    ap.add_argument("--selftest", action="store_true", help="用样例数据校验 schema")
    ap.add_argument("--ws", default=os.path.dirname(os.path.abspath(__file__)), help="工作区根")
    args = ap.parse_args()
    if args.selftest:
        selftest(args.ws)
    elif args.offline:
        build_reco_json(args.ws)
        build_hotspot_leaders(args.ws)
        build_holding_fundflow(args.ws)
        build_dsa_decisions(args.ws)
    else:
        run_full(args.ws)
