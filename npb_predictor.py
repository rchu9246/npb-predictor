#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NPB(日本職棒)每日勝率預測系統
================================
模型與 MLB / CPBL 版相同:Log5(賽季勝率)+ 主場優勢 + 先發投手戰力 + 傷兵嚴重度。

資料輸入方式(NPB 官網不允許自動化存取,所以這一版不爬網站):
  1. data/matchups.txt   把 NPB 官方公布的「予告先發」文字貼進去
                         例:6/11(木)の予告先発 （F-DB）孫易磊×東克樹 （E-G）瀧中瞭太×田中将大
                         (代碼前面是主場球隊;× 前面是主場先發)
  2. data/actual_results/日期.txt   比賽打完後貼上官方公布的比分
                         例:F 4-2 DB E 0-7 G L 5-4 C     (也可寫 F 中止 DB 表示雨天中止)
  3. data/standings.json  順位表的「基準日」戰績;基準日之後你貼的比分會自動累加,不用另外更新

用法:
    python npb_predictor.py                 # 讀 matchups.txt,預測並產生網站(site/ 資料夾)
    python npb_predictor.py --site docs     # 輸出到 docs/(給 GitHub Pages 用)
    python npb_predictor.py --add-results 2026-09-28 "F 4-2 DB E 0-7 G"
"""

import argparse
import html
import json
import os
import re
import sys
import unicodedata
import webbrowser
from datetime import date, datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# 路徑
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
STANDINGS_FILE = os.path.join(DATA_DIR, "standings.json")
PITCHER_FILE = os.path.join(DATA_DIR, "pitcher_ratings.json")
INJURY_FILE = os.path.join(DATA_DIR, "injury_scores.json")
MATCHUPS_FILE = os.path.join(DATA_DIR, "matchups.txt")
HISTORY_DIR = os.path.join(DATA_DIR, "history")
RESULTS_DIR = os.path.join(DATA_DIR, "actual_results")
DEFAULT_SITE_DIR = os.path.join(BASE_DIR, "site")

# 日本沒有日光節省時間,固定 UTC+9,不需要額外安裝時區套件
JST = timezone(timedelta(hours=9))


def jst_now():
    return datetime.now(JST)


def jst_stamp():
    return jst_now().strftime("%Y-%m-%d %H:%M") + " (日本時間)"


# ---------------------------------------------------------------------------
# 模型權重(都可以自己調)
# ---------------------------------------------------------------------------
HOME_FIELD_ADVANTAGE = 0.53   # 主場優勢基準,是憑印象設定的近似值,沒有回測過
PITCHER_WEIGHT = 0.018        # 每 1 分投手戰力差 → 勝率變動
INJURY_WEIGHT = 0.0009        # 每 1 分傷兵嚴重度差 → 勝率變動
DEFAULT_PITCHER_SCORE = 6.0   # 沒填評分的投手 = 聯盟平均
DEFAULT_INJURY_SCORE = 60
LEAGUE_AVG_ERA = 3.40         # NPB 聯盟平均防禦率的近似值(日職得分環境比 MLB 低)
ERA_SLOPE = 1.8               # 每差 1.00 防禦率 ≈ 差 1.8 分戰力

# 球隊代碼:NPB 官方公布文字裡使用的縮寫
TEAMS = {
    "G": ("巨人", "セ"), "T": ("阪神", "セ"), "DB": ("DeNA", "セ"),
    "C": ("廣島", "セ"), "S": ("養樂多", "セ"), "D": ("中日", "セ"),
    "H": ("軟銀", "パ"), "F": ("日本火腿", "パ"), "M": ("羅德", "パ"),
    "E": ("樂天", "パ"), "L": ("西武", "パ"), "B": ("歐力士", "パ"),
}


def zh(code):
    return TEAMS.get(code, (code, ""))[0]


# ---------------------------------------------------------------------------
# 文字處理
# ---------------------------------------------------------------------------
def norm(s):
    """全形轉半形等(NFKC),讓全形括號、全形英文字母都能被辨識"""
    return unicodedata.normalize("NFKC", s)


def norm_name(s):
    return re.sub(r"\s+", "", norm(s)).casefold()


def read_text(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        return f.read()


def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def find_date(text, today):
    m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text)
    try:
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = re.search(r"(\d{1,2})/(\d{1,2})", text)
        if m:
            return date(today.year, int(m.group(1)), int(m.group(2)))
    except ValueError:
        pass
    return None


GAME_RE = re.compile(r"\(?\s*([A-Z]{1,2})\s*[-−]\s*([A-Z]{1,2})\s*\)?")
UNDECIDED = {"", "未定", "未發表", "未発表", "-", "—", "tbd", "?"}


def _clean_pitcher(name):
    name = re.sub(r"\s+", "", name.strip(" \t\r\n,、。;；:："))
    return "未定" if name.casefold() in UNDECIDED else name


def split_pitchers(seg):
    seg = re.sub(r"\([^)]*\)", " ", seg)          # (球場名)
    seg = re.sub(r"\d{1,2}:\d{2}", " ", seg)      # 18:00
    seg = seg.strip()
    parts = re.split(r"\s*(?:×|✕|\bvs\.?\b|(?<=\s)[xX](?=\s))\s*", seg, flags=re.I)
    if len(parts) == 2:
        return _clean_pitcher(parts[0]), _clean_pitcher(parts[1])
    tokens = seg.split()
    if len(tokens) == 2:
        return _clean_pitcher(tokens[0]), _clean_pitcher(tokens[1])
    if len(tokens) == 4:   # 「東 克樹 工藤 泰己」這種姓名各自有空格
        return _clean_pitcher(tokens[0] + tokens[1]), _clean_pitcher(tokens[2] + tokens[3])
    return "未定", "未定"


def parse_matchups(text, today):
    """回傳 (比賽日期或None, [對戰dict...]),對戰格式與官方公布一致:主場-客場、主場投手×客場投手"""
    lines = [ln for ln in text.splitlines() if not ln.strip().startswith("//")]
    text = norm("\n".join(lines))
    text = re.sub(r"https?://\S+|npb\.jp/\S*|#\S+", " ", text)
    game_date = find_date(text, today)
    matches = [m for m in GAME_RE.finditer(text) if m.group(1) in TEAMS and m.group(2) in TEAMS]
    games = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        hp, ap = split_pitchers(text[m.end():end])
        games.append({"home": m.group(1), "away": m.group(2), "home_pitcher": hp, "away_pitcher": ap})
    return game_date, games


RESULT_RE = re.compile(r"(?<![A-Za-z])([A-Z]{1,2})\s*(\d{1,2})\s*[-−]\s*(\d{1,2})\s*([A-Z]{1,2})(?![A-Za-z])")
OFF_RE = re.compile(r"(?<![A-Za-z])([A-Z]{1,2})\s*(?:雨天中止|中止|延期|ノーゲーム)\s*([A-Z]{1,2})(?![A-Za-z])")


def parse_results(text):
    """解析官方比分文字,例:F 4-2 DB E 0-7 G;回傳 [{home,away,home_score,away_score,off}]"""
    lines = [ln for ln in text.splitlines() if not ln.strip().startswith("//")]
    text = norm("\n".join(lines))
    out = []
    for m in RESULT_RE.finditer(text):
        h, hs, as_, a = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
        if h in TEAMS and a in TEAMS:
            out.append({"home": h, "away": a, "home_score": hs, "away_score": as_, "off": False})
    for m in OFF_RE.finditer(text):
        h, a = m.group(1), m.group(2)
        if h in TEAMS and a in TEAMS:
            out.append({"home": h, "away": a, "home_score": None, "away_score": None, "off": True})
    return out


def load_results(d):
    path = os.path.join(RESULTS_DIR, f"{d}.txt")
    if not os.path.exists(path):
        return None
    return parse_results(read_text(path))


def all_result_dates():
    if not os.path.isdir(RESULTS_DIR):
        return []
    return sorted(f[:-4] for f in os.listdir(RESULTS_DIR) if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.txt", f))


# ---------------------------------------------------------------------------
# 順位表:基準日戰績 + 之後貼上的比分自動累加
# ---------------------------------------------------------------------------
def effective_standings(target_date):
    data = load_json(STANDINGS_FILE, {})
    base = {k: list(v) for k, v in data.get("teams", {}).items()}
    as_of = data.get("as_of", "0000-00-00")
    for d in all_result_dates():
        if not (as_of < d < target_date):
            continue
        for r in load_results(d) or []:
            if r["off"]:
                continue
            for code in (r["home"], r["away"]):
                base.setdefault(code, [0, 0, 0])
            h, a = base[r["home"]], base[r["away"]]
            if r["home_score"] > r["away_score"]:
                h[0] += 1; a[1] += 1
            elif r["home_score"] < r["away_score"]:
                h[1] += 1; a[0] += 1
            else:
                h[2] += 1; a[2] += 1
    return base, as_of


def season_pct(standings, team):
    """NPB 官方勝率的算法:勝 ÷ (勝+敗),和局不計入(和局約佔 2~3%,影響很小)"""
    w, l = standings.get(team, [0, 0, 0])[:2]
    return 0.5 if w + l == 0 else w / (w + l)


# ---------------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------------
def log5(pa, pb):
    d = pa + pb - 2 * pa * pb
    return 0.5 if d == 0 else (pa - pa * pb) / d


def era_to_score(era):
    return max(1.0, min(10.0, 6.0 - (era - LEAGUE_AVG_ERA) * ERA_SLOPE))


def load_pitcher_ratings():
    raw = load_json(PITCHER_FILE, {}) or {}
    return {norm_name(k): v for k, v in raw.items() if not str(k).startswith("_")}


def pitcher_score(name, ratings):
    """回傳 (戰力分數, 來源)。值可以是 1~10 的數字,或 {"era": 2.10} / {"fip": 2.80} / {"score": 8}"""
    key = norm_name(name)
    if key in UNDECIDED:
        return DEFAULT_PITCHER_SCORE, "default"
    v = ratings.get(key)
    if v is None:
        return DEFAULT_PITCHER_SCORE, "default"
    try:
        if isinstance(v, (int, float)):
            return max(1.0, min(10.0, float(v))), "manual"
        if isinstance(v, dict):
            if "score" in v:
                return max(1.0, min(10.0, float(v["score"]))), "manual"
            for k in ("era", "fip"):
                if k in v:
                    return era_to_score(float(v[k])), "manual"
    except (TypeError, ValueError):
        pass
    return DEFAULT_PITCHER_SCORE, "default"


def predict(game, standings, ratings, injuries, target_date):
    home, away = game["home"], game["away"]
    ph, pa = season_pct(standings, home), season_pct(standings, away)
    base = log5(ph, pa)
    odds = base / (1 - base) * (HOME_FIELD_ADVANTAGE / (1 - HOME_FIELD_ADVANTAGE)) if base < 1 else 999
    p_team = odds / (1 + odds)
    hs, hsrc = pitcher_score(game["home_pitcher"], ratings)
    as_, asrc = pitcher_score(game["away_pitcher"], ratings)
    hi = injuries.get(home, DEFAULT_INJURY_SCORE)
    ai = injuries.get(away, DEFAULT_INJURY_SCORE)
    pitcher_adj = (hs - as_) * PITCHER_WEIGHT
    injury_adj = (ai - hi) * INJURY_WEIGHT
    p_final = max(0.05, min(0.95, p_team + pitcher_adj + injury_adj))
    return {
        "date": target_date, "home": home, "away": away,
        "home_pitcher": game["home_pitcher"], "away_pitcher": game["away_pitcher"],
        "home_pitcher_score": hs, "away_pitcher_score": as_,
        "home_pitcher_src": hsrc, "away_pitcher_src": asrc,
        "season_pct_home": ph, "season_pct_away": pa,
        "p_team_strength": p_team, "pitcher_adj": pitcher_adj, "injury_adj": injury_adj,
        "p_final": p_final, "home_injury": hi, "away_injury": ai,
    }


# ---------------------------------------------------------------------------
# 預測記錄與核對
# ---------------------------------------------------------------------------
def history_path(d):
    return os.path.join(HISTORY_DIR, f"{d}.json")


def save_history(preds, d):
    os.makedirs(HISTORY_DIR, exist_ok=True)
    with open(history_path(d), "w", encoding="utf-8") as f:
        json.dump(preds, f, ensure_ascii=False, indent=2)


def load_history(d):
    return load_json(history_path(d))


def history_dates():
    if not os.path.isdir(HISTORY_DIR):
        return []
    return sorted(f[:-5] for f in os.listdir(HISTORY_DIR) if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.json", f))


def compare(preds, results):
    pool = list(results or [])
    rows = []
    for p in preds:
        idx = next((i for i, r in enumerate(pool) if r["home"] == p["home"] and r["away"] == p["away"]), None)
        row = dict(p)
        if idx is None:
            row.update(matched=False, final=False, off=False, tie=False, correct=None)
        else:
            r = pool.pop(idx)
            row.update(matched=True, off=r["off"], home_score=r["home_score"], away_score=r["away_score"])
            if r["off"]:
                row.update(final=False, tie=False, correct=None)
            else:
                tie = r["home_score"] == r["away_score"]
                row.update(final=True, tie=tie,
                           correct=None if tie else ((p["p_final"] >= 0.5) == (r["home_score"] > r["away_score"])))
        rows.append(row)
    fin = [r for r in rows if r["final"] and not r["tie"]]
    ok = [r for r in fin if r["correct"]]
    hi = [r for r in fin if abs(r["p_final"] - 0.5) >= 0.15]
    hi_ok = [r for r in hi if r["correct"]]
    stats = {
        "total": len(rows), "finished": len(fin), "correct": len(ok),
        "accuracy": len(ok) / len(fin) * 100 if fin else None,
        "ties": sum(1 for r in rows if r["final"] and r["tie"]),
        "off": sum(1 for r in rows if r["off"]),
        "pending": sum(1 for r in rows if not r["matched"]),
        "hi_total": len(hi), "hi_correct": len(hi_ok),
    }
    return rows, stats


def build_details():
    details = {}
    for d in history_dates():
        preds = load_history(d)
        if not preds:
            continue
        details[d] = compare(preds, load_results(d))
    return details


def overall_stats(details):
    fin = corr = hi = hi_ok = 0
    for rows, st in details.values():
        fin += st["finished"]; corr += st["correct"]; hi += st["hi_total"]; hi_ok += st["hi_correct"]
    return {"finished": fin, "correct": corr, "accuracy": corr / fin * 100 if fin else None,
            "hi_total": hi, "hi_correct": hi_ok}


# ---------------------------------------------------------------------------
# 網頁
# ---------------------------------------------------------------------------
CSS = """
:root{--bg:#0f1115;--card:#171a21;--line:#2a2e38;--text:#e8e9ec;--sub:#9aa1ac;--accent:#ff9f43;--blue:#4f8cff;--good:#4caf50;--bad:#ff6b6b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font-family:-apple-system,"Microsoft JhengHei","PingFang TC","Yu Gothic",sans-serif;padding:24px 14px 60px}
h1{text-align:center;font-size:22px;margin:0 0 6px}.sub{text-align:center;color:var(--sub);font-size:13px;margin-bottom:6px}
.stamp{text-align:center;color:var(--sub);font-size:12px;opacity:.7;margin-bottom:16px}
table{width:100%;max-width:920px;margin:0 auto;border-collapse:collapse;background:var(--card);border-radius:12px;overflow:hidden}
td,th{padding:11px 8px;border-bottom:1px solid var(--line);font-size:14px;text-align:center}th{color:var(--sub);font-weight:600}
tr:last-child td{border-bottom:none}.small{font-size:11px;color:var(--sub)}.fav{color:var(--accent)}
.prob{font-size:18px;font-weight:700;color:var(--sub);min-width:64px}.prob.fav{color:var(--accent)}
.bar{height:8px;border-radius:4px;background:var(--blue);overflow:hidden;min-width:80px}.bar i{display:block;height:100%;background:var(--accent)}
.ok{color:var(--good);font-weight:700}.ng{color:var(--bad);font-weight:700}.mute{color:var(--sub)}
.box{max-width:920px;margin:0 auto 18px;background:var(--card);border-radius:10px;padding:12px;text-align:center;font-size:15px}
.picker{text-align:center;margin:0 auto 16px;color:var(--sub);font-size:14px}
.picker select{background:var(--card);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:8px 12px;font-size:14px;margin-left:6px;max-width:92%}
.nav{text-align:center;margin-top:22px;font-size:13px}.nav a,a{color:var(--blue)}
.cards{display:flex;flex-direction:column;gap:14px;max-width:420px;margin:30px auto}
a.card{display:block;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;text-decoration:none;color:var(--text)}
a.card b{display:block;font-size:17px;margin-bottom:4px}a.card span{font-size:13px;color:var(--sub)}
@media(max-width:700px){table,tbody,tr,td{display:block;width:100%}tr{margin-bottom:12px;border:1px solid var(--line);border-radius:10px;padding:6px}td{border:none;text-align:left;padding:4px 8px}}
"""


def page(title, body):
    return (f'<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="UTF-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>'
            f'<style>{CSS}</style></head><body>{body}</body></html>')


def write(path, content):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def pitcher_cell(name, score, src, injury):
    tag = "預設" if src == "default" else "評分"
    return (f'{html.escape(name)}<br><span class="small">戰力{score:.1f}({tag}) / 傷病分{injury}</span>')


def report_html(preds, d):
    rows = []
    for p in sorted(preds, key=lambda x: -abs(x["p_final"] - 0.5)):
        hp, ap = p["p_final"] * 100, (1 - p["p_final"]) * 100
        fh = p["p_final"] >= 0.5
        rows.append(f"""<tr>
<td class="{'fav' if fh else ''}"><b>{zh(p['home'])}</b> <span class="small">{p['home']}</span></td>
<td class="mute">{pitcher_cell(p['home_pitcher'], p['home_pitcher_score'], p['home_pitcher_src'], p['home_injury'])}</td>
<td class="prob {'fav' if fh else ''}">{hp:.1f}%</td><td class="small">vs</td>
<td class="prob {'' if fh else 'fav'}">{ap:.1f}%</td>
<td class="mute">{pitcher_cell(p['away_pitcher'], p['away_pitcher_score'], p['away_pitcher_src'], p['away_injury'])}</td>
<td class="{'' if fh else 'fav'}"><b>{zh(p['away'])}</b> <span class="small">{p['away']}</span></td>
<td><div class="bar"><i style="width:{hp:.1f}%"></i></div></td></tr>""")
    body = (f'<h1>⚾ NPB 勝率預測報告</h1><div class="sub">{d} 賽事｜依信心程度排序｜橘色 = 較被看好｜左為主場</div>'
            f'<div class="stamp">最後更新: {jst_stamp()}</div><table><tbody>{"".join(rows)}</tbody></table>'
            '<div class="sub" style="margin-top:16px">「預設」表示該投手沒有填評分,以聯盟平均 6.0 分計算。<br>'
            '簡化量化模型,僅供參考,不構成投注建議。</div>'
            '<div class="nav"><a href="index.html">回首頁</a>　<a href="verify_report.html">最近核對</a>　<a href="stats.html">累積準確率</a></div>')
    return page(f"NPB 勝率預測 {d}", body)


def picker_html(options, current, base):
    if not options:
        return ""
    opts = "".join(f'<option value="{base}{d}.html"{" selected" if d == current else ""}>{html.escape(label)}</option>'
                   for d, label in options)
    return f'<div class="picker"><label>選擇日期:</label><select onchange="if(this.value)location.href=this.value">{opts}</select></div>'


def verify_html(rows, st, d, options, base, nav):
    trs = []
    for r in rows:
        if not r["matched"]:
            score, mark = "尚無比分", '<span class="mute">尚未貼上</span>'
        elif r["off"]:
            score, mark = "中止", '<span class="mute">中止(不計)</span>'
        else:
            score = f"{r['home_score']} - {r['away_score']}"
            mark = ('<span class="mute">和局(不計)</span>' if r["tie"]
                    else '<span class="ok">✔ 猜對</span>' if r["correct"] else '<span class="ng">✘ 猜錯</span>')
        trs.append(f"<tr><td>{zh(r['home'])} <span class='small'>{r['home']}</span></td>"
                   f"<td>{zh(r['away'])} <span class='small'>{r['away']}</span></td>"
                   f"<td>{r['p_final'] * 100:.1f}%</td><td>{score}</td><td>{mark}</td></tr>")
    if st["finished"]:
        line = f"命中率: {st['correct']}/{st['finished']} = {st['accuracy']:.1f}%"
        if st["hi_total"]:
            line += f"　｜　高信心場次: {st['hi_correct']}/{st['hi_total']}"
    else:
        line = "目前沒有已貼上比分的比賽可核對"
    body = (f'<h1>📊 NPB 預測核對報告</h1><div class="sub">{d} 賽事(預測主隊勝率 vs 實際比分)</div>'
            f'<div class="stamp">最後更新: {jst_stamp()}</div>{picker_html(options, d, base)}'
            f'<div class="box">{line}</div>'
            f'<table><thead><tr><th>主場</th><th>客場</th><th>預測主隊勝率</th><th>實際比分</th><th>結果</th></tr></thead>'
            f'<tbody>{"".join(trs)}</tbody></table>'
            f'<div class="nav"><a href="{nav}index.html">回首頁</a>　<a href="{nav}report.html">今日預測</a>　'
            f'<a href="{nav}stats.html">累積準確率</a></div>')
    return page(f"NPB 核對 {d}", body)


def stats_html(details, overall):
    if not details:
        table = '<div class="box mute">還沒有任何預測記錄。貼上先發預告並執行一次後,這裡就會開始累積。</div>'
        summary = ""
    else:
        rows = []
        for d in sorted(details, reverse=True):
            st = details[d][1]
            extra = "".join(f" <span class='small'>({t})</span>" for t in
                            ([f"另有{st['pending']}場未貼比分"] if st["pending"] else []) +
                            ([f"{st['off']}場中止"] if st["off"] else []) +
                            ([f"{st['ties']}場和局"] if st["ties"] else []))
            if st["finished"]:
                rows.append(f"<tr><td><a href='verify/{d}.html'>{d}</a></td><td>{st['finished']}{extra}</td>"
                            f"<td>{st['correct']}</td><td>{st['accuracy']:.1f}%</td></tr>")
            else:
                rows.append(f"<tr><td><a href='verify/{d}.html'>{d}</a></td><td>--{extra}</td><td>--</td>"
                            f"<td class='mute'>尚無已結束比賽</td></tr>")
        table = (f'<table><thead><tr><th>日期</th><th>場次</th><th>猜對</th><th>命中率</th></tr></thead>'
                 f'<tbody>{"".join(rows)}</tbody></table>')
        summary = (f"累積命中率: {overall['correct']}/{overall['finished']} = {overall['accuracy']:.1f}%"
                   if overall["finished"] else "尚無已結束的比賽可統計")
        if overall["hi_total"]:
            summary += f"　｜　高信心場次: {overall['hi_correct']}/{overall['hi_total']}"
        summary = f'<div class="box">{summary}<div class="small">樣本太少時數字沒有參考價值,建議累積 100 場以上再看</div></div>'
    body = (f'<h1>📈 NPB 累積準確率</h1><div class="stamp">最後更新: {jst_stamp()}</div>{summary}{table}'
            '<div class="nav"><a href="index.html">回首頁</a></div>')
    return page("NPB 累積準確率", body)


def index_html():
    body = ('<h1>⚾ NPB 勝率預測系統</h1><div class="sub">貼上官方公布的先發預告與比分,自動預測、核對、統計</div>'
            '<div class="cards">'
            '<a class="card" href="report.html"><b>📅 賽事預測</b><span>最近一次貼上的先發預告,各場勝率</span></a>'
            '<a class="card" href="verify_report.html"><b>📊 最近核對報告</b><span>最近一個已貼上比分的比賽日,逐場對照</span></a>'
            '<a class="card" href="stats.html"><b>📈 累積準確率</b><span>整體命中率,點日期看每天明細</span></a></div>')
    return page("NPB 勝率預測系統", body)


def build_site(site_dir, preds, pred_date):
    write(os.path.join(site_dir, "index.html"), index_html())
    if preds:
        write(os.path.join(site_dir, "report.html"), report_html(preds, pred_date))
    details = build_details()
    options = []
    for d in sorted(details, reverse=True):
        st = details[d][1]
        label = (f"{d} · 猜對 {st['correct']}/{st['finished']} ({st['accuracy']:.0f}%)" if st["finished"]
                 else f"{d} · 尚無已結束比賽")
        options.append((d, label))
    for d, (rows, st) in details.items():
        write(os.path.join(site_dir, "verify", f"{d}.html"), verify_html(rows, st, d, options, "", "../"))
    pick = next((d for d in sorted(details, reverse=True)
                 if details[d][1]["finished"] and details[d][1]["pending"] == 0), None) or \
        next((d for d in sorted(details, reverse=True) if details[d][1]["finished"]), None)
    if pick:
        write(os.path.join(site_dir, "verify_report.html"),
              verify_html(details[pick][0], details[pick][1], pick, options, "verify/", ""))
    elif not os.path.exists(os.path.join(site_dir, "verify_report.html")):
        write(os.path.join(site_dir, "verify_report.html"),
              page("NPB 核對", '<h1>📊 尚未有核對記錄</h1><div class="sub">貼上比賽結果後,這裡會顯示命中率。</div>'
                   '<div class="nav"><a href="index.html">回首頁</a></div>'))
    write(os.path.join(site_dir, "stats.html"), stats_html(details, overall_stats(details)))
    return details


# ---------------------------------------------------------------------------
# 終端機輸出與主程式
# ---------------------------------------------------------------------------
def print_predictions(preds, d):
    print("\n" + "=" * 78)
    print(f"NPB {d} 賽事勝率預測(主場勝率,依信心排序)")
    print("=" * 78)
    for p in sorted(preds, key=lambda x: -abs(x["p_final"] - 0.5)):
        print(f"{zh(p['home']):<6} vs {zh(p['away']):<6} 主隊 {p['p_final'] * 100:5.1f}%   "
              f"{p['home_pitcher']}({p['home_pitcher_score']:.1f}) × {p['away_pitcher']}({p['away_pitcher_score']:.1f})")
    print("=" * 78 + "\n")


def main():
    ap = argparse.ArgumentParser(description="NPB 每日勝率預測系統")
    ap.add_argument("--matchups", default=MATCHUPS_FILE, help="先發預告文字檔(預設 data/matchups.txt)")
    ap.add_argument("--site", default=DEFAULT_SITE_DIR, help="網站輸出資料夾(預設 site;GitHub Pages 用 docs)")
    ap.add_argument("--date", help="強制指定比賽日期 YYYY-MM-DD(預設從文字裡辨識)")
    ap.add_argument("--no-open", action="store_true", help="不要自動用瀏覽器開啟")
    ap.add_argument("--no-save", action="store_true", help="試算用:不存進預測記錄,不影響累積準確率")
    ap.add_argument("--add-results", nargs=2, metavar=("DATE", "TEXT"),
                    help='新增比分,例:--add-results 2026-09-28 "F 4-2 DB E 0-7 G"')
    args = ap.parse_args()
    today = jst_now().date()

    if args.add_results:
        d, text = args.add_results
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
            sys.exit("[錯誤] 日期格式要是 YYYY-MM-DD")
        found = parse_results(text)
        if not found:
            sys.exit("[錯誤] 沒有辨識到任何比分,格式範例: F 4-2 DB E 0-7 G")
        os.makedirs(RESULTS_DIR, exist_ok=True)
        with open(os.path.join(RESULTS_DIR, f"{d}.txt"), "a", encoding="utf-8") as f:
            f.write(text.strip() + "\n")
        print(f"已新增 {len(found)} 場比分到 {d}")

    preds, pred_date = [], None
    if os.path.exists(args.matchups):
        gdate, games = parse_matchups(read_text(args.matchups), today)
        if args.date:
            gdate = date.fromisoformat(args.date)
        if games and gdate is None:
            print("[警告] 文字裡找不到比賽日期(例如 9/29 或 2026-09-29),請在開頭加上日期,或用 --date 指定")
        elif games:
            pred_date = gdate.isoformat()
            standings, as_of = effective_standings(pred_date)
            ratings = load_pitcher_ratings()
            injuries = load_json(INJURY_FILE, {}) or {}
            preds = [predict(g, standings, ratings, injuries, pred_date) for g in games]
            print(f"順位表基準日 {as_of},並已加入其後貼上的比分。")
            if pred_date <= as_of:
                print(f"[注意] 比賽日期 {pred_date} 不晚於順位表基準日 {as_of},用的戰績包含了比賽之後的結果,"
                      "只適合試算(例如範例檔),不要拿來當正式預測。")
            print_predictions(preds, pred_date)
            if args.no_save:
                print("(--no-save:試算模式,未存入預測記錄)")
            elif load_results(pred_date) is not None:
                print(f"[提醒] {pred_date} 已經有比分記錄,為避免用賽後資訊改寫預測,這次不覆蓋該日的預測記錄。")
            else:
                save_history(preds, pred_date)
        else:
            print("matchups.txt 裡沒有辨識到任何對戰。把 NPB 官方公布的予告先發文字貼進去再執行。")
            print("格式範例: 6/11(木)の予告先発 （F-DB）孫易磊×東克樹 （E-G）瀧中瞭太×田中将大")
    else:
        print(f"[提醒] 找不到 {args.matchups}")

    build_site(args.site, preds, pred_date)
    print(f"網站已產生: {os.path.abspath(args.site)}")
    if not args.no_open:
        target = "report.html" if preds else "index.html"
        webbrowser.open("file://" + os.path.abspath(os.path.join(args.site, target)))


if __name__ == "__main__":
    main()
