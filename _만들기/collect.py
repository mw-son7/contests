# -*- coding: utf-8 -*-
"""공모전 모음 — 수집기.

여섯 사이트의 '목록' 화면만 읽어 제목·주최·기간·원문 주소를 모으고,
틀.html 에 자료를 끼워 넣어 ../공모전모음.html 한 파일로 만든다.
표준 라이브러리만 쓰므로 기기마다 따로 설치할 것이 없다.

쓰는 법:  python collect.py            전부 모아 HTML 을 다시 만든다
          python collect.py --only 위비티 --dry   한 곳만 시험(파일을 쓰지 않음)
          python collect.py --render   사이트에 가지 않고 지난 자료로 HTML 만 다시 만든다
"""
import concurrent.futures as cf
import datetime as dt
import hashlib
import html
import http.cookiejar
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "공모전모음.html")
TEMPLATE = os.path.join(HERE, "틀.html")
STATE = os.path.join(HERE, "_기록.json")
LOG = os.path.join(HERE, "_로그.txt")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
DELAY = 0.5            # 한 사이트에 요청을 잇달아 보낼 때 쉬는 시간(초). 상대 서버에 부담을 주지 않으려는 값이다.
TODAY = dt.date.today()
KST = dt.timezone(dt.timedelta(hours=9))


# ───────────────────────── 공통 도구 ─────────────────────────

class Session:
    """사이트마다 하나씩. 쿠키를 기억하고, 요청 사이에 잠깐 쉰다."""

    def __init__(self):
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def raw(self, url, data=None, headers=None):
        h = {"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"}
        h.update(headers or {})
        last = None
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, data=data, headers=h)
                with self.opener.open(req, timeout=40) as r:
                    body = r.read()
                time.sleep(DELAY)
                return body
            except Exception as e:      # 일시적인 끊김은 두 번 더 해 본다
                last = e
                time.sleep(2 + attempt * 3)
        raise last

    def text(self, url, data=None, headers=None):
        return self.raw(url, data, headers).decode("utf-8", "replace")

    def json(self, url, data=None, headers=None):
        return json.loads(self.raw(url, data, headers))


def clean(s):
    """태그를 떼고 &amp; 같은 표기를 풀고 빈칸을 하나로 줄인다."""
    if s is None:
        return ""
    s = re.sub(r"<[^>]+>", " ", str(s))
    s = html.unescape(html.unescape(s)).replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def iso(d):
    return d.isoformat() if d else None


def parse_date(s):
    """'2026-11-30 17:00:00.0'·'2026.11.13' → date. 못 읽으면 None."""
    m = re.search(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})", s or "")
    if not m:
        return None
    try:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def end_from_dday(text):
    """'D-19' → 오늘+19일. 'D-day'·'오늘' → 오늘. 못 읽으면 None."""
    m = re.search(r"D\s*-\s*(\d+)", text or "")
    if m:
        return TODAY + dt.timedelta(days=int(m.group(1)))
    if re.search(r"D\s*-\s*day|오늘", text or "", re.I):
        return TODAY
    return None


def item(src, title, url, host="", cats="", target="", start=None, end=None):
    return {"src": src, "title": clean(title), "url": url, "host": clean(host),
            "cats": clean(cats), "target": clean(target), "start": iso(start), "end": iso(end)}


# ───────────────────────── 사이트별 수집 ─────────────────────────

def wevity():
    """위비티 — 전체 공모전 목록. 접수 중인 것이 앞쪽에, 마감된 것이 뒤쪽에 온다."""
    s, out, seen = Session(), [], set()
    for gp in range(1, 81):
        t = s.text(f"https://www.wevity.com/?c=find&s=1&gub=1&gp={gp}")
        got = open_cnt = 0
        for b in t.split('<div class="tit">')[1:]:
            m = re.search(r'<a href="[^"]*?ix=(\d+)"[^>]*>(.*?)</a>', b, re.S)
            st = re.search(r'class="dday \w+">([^<]+)<', b)
            if not m or not st:
                continue
            got += 1
            if "마감" == clean(st.group(1)):
                continue
            open_cnt += 1
            ix = m.group(1)
            if ix in seen:
                continue
            seen.add(ix)
            day = re.search(r'<div class="day">(.*?)</div>', b, re.S)
            cats = re.search(r'분야 : (.*?)</div>', b, re.S)
            organ = re.search(r'<div class="organ">(.*?)</div>', b, re.S)
            out.append(item("위비티", re.sub(r"<span.*?</span>", "", m.group(2), flags=re.S),
                            f"https://www.wevity.com/?c=find&s=1&gub=1&gbn=view&ix={ix}",
                            host=organ.group(1) if organ else "",
                            cats=cats.group(1) if cats else "",
                            end=end_from_dday(clean(day.group(1)) if day else "")))
        if got == 0 or open_cnt == 0:
            break
    return out


def thinkcontest():
    """씽굿 — 목록을 따로 불러오는 주소(subList.do)에 '접수중'·'접수예정'만 청한다."""
    s, out, seen = Session(), [], set()
    index = "https://www.thinkcontest.com/thinkgood/user/contest/index.do"
    s.text(index)
    head = {"Content-Type": "application/json; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest", "Referer": index}
    for proc in ("ING", "YET"):
        for page in range(1, 81):
            body = {"recordsPerPage": 10, "currentPageNo": page, "contest_field": "", "host_organ": "",
                    "enter_qualified": "", "award_size": "", "searchStatus": "Y",
                    "sidx": "", "sord": "", "searchProcess": proc}
            res = s.json("https://www.thinkcontest.com/thinkgood/user/contest/subList.do",
                         json.dumps(body).encode(), head)
            rows = res.get("listJsonData") or []
            for x in rows:
                pk = x.get("contest_pk")
                if pk in seen or x.get("process") == "END":
                    continue
                seen.add(pk)
                kind = x.get("reg_type") or "contest"
                out.append(item("씽굿", x.get("program_nm"),
                                f"https://www.thinkcontest.com/thinkgood/user/{kind}/view.do?contest_pk={pk}",
                                host=x.get("host_company"), cats=x.get("contest_field_nm"),
                                target=x.get("enter_qualified_nm"),
                                start=parse_date(x.get("accept_dt")), end=parse_date(x.get("finish_dt"))))
            if not rows or page * 10 >= int(res.get("totalcnt") or 0):
                break
    return out


def linkareer():
    """링커리어 — 공모전 목록 화면에 함께 실려 오는 자료(__NEXT_DATA__)를 읽는다."""
    s, out, seen = Session(), [], set()
    for page in range(1, 61):
        t = s.text("https://linkareer.com/list/contest?filterBy_activityTypeID=3"
                   f"&orderBy_direction=DESC&orderBy_field=CREATED_AT&page={page}")
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', t, re.S)
        state = json.loads(m.group(1))["props"]["pageProps"]["__APOLLO_STATE__"]
        conn = next((v for k, v in state.get("ROOT_QUERY", {}).items() if k.startswith("activities(")), None)
        nodes = (conn or {}).get("nodes") or []
        for ref in nodes:
            a = state.get(ref.get("__ref"), {})
            if not a.get("id") or a["id"] in seen:
                continue
            seen.add(a["id"])
            close = a.get("recruitCloseAt")
            end = dt.datetime.fromtimestamp(close / 1000, KST).date() if close else None
            out.append(item("링커리어", a.get("title"), f"https://linkareer.com/activity/{a['id']}",
                            host=a.get("organizationName"), end=end))
        if not nodes or page * 20 >= int((conn or {}).get("totalCount") or 0):
            break
    return out


def modu():
    """소통24 모두의 광장 — 공모전 공고. '진행 중'(1003)과 '예정'(1002)만 청한다."""
    s, out, seen = Session(), [], set()
    base = "https://modu.go.kr/front/epilogue/"
    page_url = base + "epilogueNewListPage.do?pagetype=bbs&menu_id=518"
    first = s.text(page_url)
    tok = re.search(r"csrfToken = '([^']+)'", first)
    head = {"X-Requested-With": "XMLHttpRequest", "Referer": page_url}
    if tok:
        head["X-CSRF-TOKEN"] = tok.group(1)
    for code in ("1003", "1002"):
        for page in range(1, 41):
            form = {"miv_pageNo": str(page), "miv_pageSize": "", "orderBy": "", "bbs_id": "", "pagetype": "bbs",
                    "search_title_contents": "", "search_insttNm": "", "search_wrk_sj": "",
                    "date_range": "all", "epilogue_bgnde": "", "epilogue_endde": "", "search_result": code,
                    "date_range_cnddt": "all", "epilogue_bgnde_cnddt": "", "epilogue_endde_cnddt": "",
                    "search_result_cnddt": "", "search_code": "title_contents", "search_txt": ""}
            t = s.text(base + "epilogueNewList.do", urllib.parse.urlencode(form).encode(), head)
            cards = t.split('<li class="thm_card')[1:]
            for c in cards:
                bid = re.search(r"bbs_id=([0-9a-f]+)", c)
                title = re.search(r'<p class="ac_title">(.*?)</p>', c, re.S)
                if not bid or not title or bid.group(1) in seen:
                    continue
                seen.add(bid.group(1))
                per = re.search(r"(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})", c)
                instt = re.search(r'<p class="ac_instt">(.*?)</p>', c, re.S)
                out.append(item("소통24", re.sub(r"^\s*【[^】]*】", "", clean(title.group(1))),
                                f"{base}epilogueNewViewPage.do?menu_id=527&bbs_id={bid.group(1)}&pagetype=bbs",
                                host=instt.group(1) if instt else "",
                                start=parse_date(per.group(1)) if per else None,
                                end=parse_date(per.group(2)) if per else None))
            last = re.search(r"btn-paging-last[^>]*miv_pageNo=(\d+)", t)
            nums = [int(n) for n in re.findall(r"miv_pageNo=(\d+)", t)]
            last_page = int(last.group(1)) if last else (max(nums) if nums else page)
            if not cards or page >= last_page:
                break
    return out


CONTEST_WORD = re.compile(r"공모|경진|콘테스트|챌린지|어워드|대회|수기|백일장")


def korea():
    """대한민국 정책브리핑 — 진행 중인 이벤트 가운데 제목에 공모·경진대회 등이 들어간 것만."""
    s, out = Session(), []
    t = s.text("https://www.korea.kr/etc/byulbyulList.do")
    area = t.split('class="list_type event"', 1)[-1]
    for m in re.finditer(r'<a href="([^"]+)"[^>]*>\s*<span class="thumb">.*?<strong>(.*?)</strong>'
                         r'.*?기간\s*:\s*([\d.]+)\s*~\s*([\d.]+)', area, re.S):
        title = clean(m.group(2))
        if not CONTEST_WORD.search(title):
            continue
        host = re.match(r"\s*\[([^\]]+)\]\s*", title)
        out.append(item("정책브리핑", title[host.end():] if host else title,
                        urllib.parse.urljoin("https://www.korea.kr/", html.unescape(m.group(1))),
                        host=host.group(1) if host else "",
                        start=parse_date(m.group(3)), end=parse_date(m.group(4))))
    return out


def contestkorea():
    """콘테스트코리아 — 대회·공모전 목록."""
    s, out, seen = Session(), [], set()
    # 한 쪽에 100건씩. 최신 등록순이라 뒤로 갈수록 마감된 것뿐이다 — 접수 중인 것이 하나도 없는 쪽에서 멈춘다.
    q = ("https://www.contestkorea.com/sub/list.php?displayrow=100&int_gbn=1&Txt_sGn=1&Txt_key=all&Txt_word="
         "&Txt_bcode=&Txt_code1=&Txt_aarea=&Txt_area=&Txt_sortkey=a.int_sort&Txt_sortword=desc&Txt_ahost="
         "&Txt_host=&Txt_award=&Txt_award2=&Txt_code3=&Txt_tipyn=&Txt_comment=&Txt_resultyn=&Txt_actcode=&page=")
    for page in range(1, 16):
        t = s.text(q + str(page))
        got = open_cnt = 0
        for b in t.split('<div class="title">')[1:]:
            m = re.search(r'view\.php\?int_gbn=1&(?:amp;)?Txt_bcode=(\d+)&(?:amp;)?str_no=(\d+)', b)
            name = re.search(r'<span class="txt">(.*?)</span>', b, re.S)
            cond = re.search(r'<span class="condition">([^<]*)</span>', b)
            if not m or not name or not cond:
                continue
            got += 1
            if not re.search(r"접수중|접수예정|마감임박", cond.group(1)):
                continue
            open_cnt += 1
            if m.group(2) in seen:
                continue
            seen.add(m.group(2))
            host = re.search(r"<strong>주최</strong>\s*\.(.*?)</li>", b, re.S)
            target = re.search(r"<strong>대상</strong>\s*\.(.*?)</li>", b, re.S)
            day = re.search(r'<span class="day">([^<]*)</span>', b)
            end = end_from_dday(day.group(1) if day else "")
            per = re.search(r"<em>접수</em>\s*(\d{1,2})\.(\d{1,2})\s*~\s*(\d{1,2})\.(\d{1,2})", b)
            start = None
            if per:
                sm, sd, em, ed = (int(x) for x in per.groups())
                if end is None:          # 남은 날이 안 적혀 있으면 접수 끝 날짜에서 해를 짐작한다
                    for y in (TODAY.year, TODAY.year + 1):
                        try:
                            cand = dt.date(y, em, ed)
                        except ValueError:
                            continue
                        if cand >= TODAY:
                            end = cand
                            break
                if end:
                    for y in (end.year, end.year - 1):
                        try:
                            cand = dt.date(y, sm, sd)
                        except ValueError:
                            continue
                        if cand <= end:
                            start = cand
                            break
            out.append(item("콘테스트코리아", name.group(1),
                            f"https://www.contestkorea.com/sub/view.php?int_gbn=1&Txt_bcode={m.group(1)}&str_no={m.group(2)}",
                            host=host.group(1) if host else "",
                            cats=", ".join(clean(c) for c in re.findall(r'<span class="category">(.*?)</span>', b, re.S)),
                            target=re.sub(r"\s*,\s*", ", ", clean(target.group(1))) if target else "",
                            start=start, end=end))
        if got == 0 or open_cnt == 0:
            break
    return out


# 이름, 함수, 대문 주소. 앞에 있을수록 같은 공모전이 겹칠 때 그 사이트의 날짜·주최를 먼저 믿는다.
SOURCES = [
    ("소통24", modu, "https://modu.go.kr/front/epilogue/epilogueNewListPage.do?pagetype=bbs&menu_id=518"),
    ("정책브리핑", korea, "https://www.korea.kr/etc/byulbyulList.do"),
    ("씽굿", thinkcontest, "https://www.thinkcontest.com/thinkgood/user/contest/index.do"),
    ("콘테스트코리아", contestkorea, "https://www.contestkorea.com/sub/list.php?int_gbn=1"),
    ("위비티", wevity, "https://www.wevity.com/?c=find&s=1&gub=1"),
    ("링커리어", linkareer, "https://linkareer.com/list/contest"),
]


# ───────────────────────── 분야 맞추기 ─────────────────────────
# 사이트마다 분야 이름이 달라, 낱말을 보고 공통 분야 열두 가지로 옮긴다.

FIELDS = [
    ("기획·아이디어", r"아이디어|기획|제안|정책"),
    ("네이밍·슬로건", r"네이밍|슬로건|명칭|표어|캐치프레이즈|이름\s*짓"),
    ("광고·마케팅", r"광고|마케팅|홍보|브랜드"),
    ("디자인·캐릭터", r"디자인|캐릭터|웹툰|일러스트|만화|이모티콘|포스터|로고|굿즈"),
    ("영상·사진", r"영상|UCC|사진|숏폼|쇼츠|영화|유튜브|릴스|다큐"),
    ("문학·수기", r"문학|문예|시나리오|수기|소설|에세이|독후감|글짓기|백일장|신춘문예|스토리|동화|수필|편지|시\s*공모|동시"),
    ("건축·인테리어", r"건축|건설|인테리어|실내|조경|정원|한옥|설계\s*공모|도시재생"),
    ("과학·IT", r"과학|공학|IT|소프트웨어|SW|AI|인공지능|데이터|해커톤|게임|모바일|코딩|로봇|프로그래밍|웹(?!툰|소설)|앱\b"),
    ("예체능", r"미술|음악|체육|무용|공예|작곡|가요|연주|서예|국악|댄스|예체능|그림|스포츠"),
    ("창업·취업", r"창업|스타트업|취업|사업화|비즈니스"),
    ("논문·학술", r"논문|학술|리포트|연구"),
]
FIELD_RE = [(name, re.compile(pat, re.I)) for name, pat in FIELDS]


def fields_of(cats, title):
    found = [name for name, rx in FIELD_RE if rx.search(cats)] if cats else []
    if not found:
        found = [name for name, rx in FIELD_RE if rx.search(title)]
    return found or ["기타"]


def short_target(text):
    """응모 대상을 짧게. '제한 없음'·'누구나'가 들어 있으면 그 한마디로 줄인다."""
    if not text:
        return ""
    if re.search(r"제한\s*없음|누구나", text):
        return "누구나"
    parts = [p.strip() for p in text.split(",") if p.strip() and p.strip() != "기타"]
    return ", ".join(dict.fromkeys(parts))[:60]


def norm(title):
    """같은 공모전인지 견줄 때 쓰는 이름. 기호·빈칸을 떼고, 맨 앞 '[주최기관]' 꼬리표와 '제N회'는 뺀다."""
    plain = lambda s: re.sub(r"[^0-9a-z가-힣]", "", s.lower())
    t = title
    for _ in range(2):
        m = re.match(r"\s*[\[【(〔<][^\]】)〕>]{1,40}[\]】)〕>]\s*", t)
        if not m or len(plain(t[m.end():])) < 8:
            break
        t = t[m.end():]
    return re.sub(r"제\d+회", "", plain(t)) or plain(title)


# ───────────────────────── 모으기·합치기·쓰기 ─────────────────────────

def log(msg):
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line, flush=True)
    try:
        old = []
        if os.path.exists(LOG):
            with open(LOG, encoding="utf-8") as f:
                old = f.read().splitlines()[-400:]
        with open(LOG, "w", encoding="utf-8") as f:
            f.write("\n".join(old + [line]) + "\n")
    except OSError:
        pass


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def run_source(entry):
    name, fn, _ = entry
    t0 = time.time()
    try:
        rows = fn()
        if not rows:
            raise RuntimeError("목록이 비어 있음(화면 구조가 바뀌었을 수 있음)")
        return name, rows, None, time.time() - t0
    except Exception as e:      # 한 곳이 막혀도 나머지는 계속한다
        return name, None, f"{type(e).__name__}: {e}", time.time() - t0


def merge(rows_by_src, first_seen, first_run):
    merged, order = {}, []
    for name, _, _ in SOURCES:
        for r in rows_by_src.get(name, []):
            end = r.get("end")
            if end and end < iso(TODAY):
                continue                      # 이미 마감
            key = norm(r["title"])
            if len(key) < 4:
                continue
            m = merged.get(key)
            if m is None:
                m = {"t": r["title"], "h": r["host"], "c": r["cats"], "g": r["target"],
                     "s": r["start"], "e": end, "l": []}
                merged[key] = m
                order.append(key)
            else:                             # 겹치는 공모전 — 비어 있는 칸만 채운다
                m["h"] = m["h"] or r["host"]
                m["c"] = (m["c"] + ", " + r["cats"]).strip(", ") if r["cats"] else m["c"]
                m["g"] = m["g"] or r["target"]
                m["s"] = m["s"] or r["start"]
                m["e"] = m["e"] or end
            if not any(src == name for src, _ in m["l"]):
                m["l"].append([name, r["url"]])
    items, seen_now = [], {}
    for key in order:
        m = merged[key]
        seen = first_seen.get(key)
        if seen is None:
            seen = "" if first_run else iso(TODAY)     # 첫 실행 때는 전부를 '새 글'로 치지 않는다
        seen_now[key] = seen
        row = {"t": m["t"], "h": m["h"], "f": fields_of(m["c"], m["t"]), "s": m["s"], "e": m["e"], "l": m["l"]}
        who = short_target(m["g"])
        if who:
            row["g"] = who
        if seen:
            row["n"] = seen
        items.append(row)
    return items, seen_now


def write_synced(path, text):
    """동기화 폴더에 큰 파일을 곧바로 쓰면 깨질 수 있어, 임시 폴더에 만든 뒤 옮기고 검사한다."""
    data = text.encode("utf-8")
    want = hashlib.sha256(data).hexdigest()
    tmp = os.path.join(tempfile.gettempdir(), f"_contest_{os.getpid()}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
    try:
        for _ in range(3):
            shutil.copyfile(tmp, path)
            with open(path, "rb") as f:
                if hashlib.sha256(f.read()).hexdigest() == want:
                    return
            time.sleep(2)
        raise RuntimeError(f"파일을 옮긴 뒤 검사가 맞지 않음: {path}")
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def main(argv):
    only = argv[argv.index("--only") + 1] if "--only" in argv else None
    dry = "--dry" in argv
    render_only = "--render" in argv          # 사이트에 가지 않고 지난 자료로 HTML 만 다시 만든다(틀을 고쳤을 때)
    targets = [] if render_only else [e for e in SOURCES if not only or e[0] == only]
    state = load_state()
    cache = state.get("sources", {})
    first_run = "first_seen" not in state

    log("지난 자료로 HTML 만 다시 만듦" if render_only else f"수집 시작 — {', '.join(e[0] for e in targets)}")
    rows_by_src, report, results = {}, [], []
    if targets:
        with cf.ThreadPoolExecutor(max_workers=len(targets)) as pool:
            results = list(pool.map(run_source, targets))
    now = f"{dt.datetime.now():%Y-%m-%d %H:%M}"
    if render_only:
        now = state.get("last_run") or now
    ok_count = 0
    for (name, rows, err, sec), (_, _, home) in zip(results, targets):
        if rows is not None:
            ok_count += 1
            rows_by_src[name] = rows
            cache[name] = {"at": now, "items": rows}
            report.append({"name": name, "home": home, "ok": True, "at": now, "count": len(rows)})
            log(f"  {name}: {len(rows)}건 ({sec:.0f}초)")
        else:
            old = cache.get(name, {})
            rows_by_src[name] = old.get("items", [])
            report.append({"name": name, "home": home, "ok": False, "at": old.get("at", ""),
                           "count": len(rows_by_src[name])})
            log(f"  {name}: 실패 — {err} → 지난 자료 {len(rows_by_src[name])}건을 대신 씀")

    if dry:
        for name in rows_by_src:
            for r in rows_by_src[name][:5]:
                print("   ", json.dumps(r, ensure_ascii=False))
        return 0
    if ok_count == 0 and not render_only:
        log("모든 사이트 수집 실패 — 기존 HTML 을 그대로 둠")
        return 1
    done = dict((r["name"], r) for r in report)
    report = []
    for name, _, home in SOURCES:              # 이번에 돌리지 않은 곳은 지난 자료로 채우고, 표는 늘 같은 순서로
        if name not in rows_by_src:
            old = cache.get(name, {})
            rows_by_src[name] = old.get("items", [])
            done[name] = {"name": name, "home": home, "ok": True, "at": old.get("at", ""),
                          "count": len(rows_by_src[name])}
        report.append(done[name])

    items, seen_now = merge(rows_by_src, state.get("first_seen", {}), first_run)
    data = {"generated": now, "sources": report, "fields": [n for n, _ in FIELDS] + ["기타"], "items": items}
    with open(TEMPLATE, encoding="utf-8") as f:
        tpl = f.read()
    if "/*__DATA__*/null" not in tpl:
        raise RuntimeError("틀.html 에 자료를 끼울 자리(/*__DATA__*/null)가 없음")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    write_synced(OUT, tpl.replace("/*__DATA__*/null", payload))
    write_synced(STATE, json.dumps({"last_run": now, "first_seen": seen_now, "sources": cache},
                                   ensure_ascii=False, separators=(",", ":")))
    log(f"완료 — 겹친 것을 합쳐 {len(items)}건, {os.path.basename(OUT)} 갱신")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main(sys.argv[1:]))
