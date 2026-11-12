# -*- coding: utf-8 -*-
"""공모전 모음 — 수집기.

여섯 사이트의 '목록' 화면을 읽어 제목·주최·기간·원문 주소를 모으고,
공모전마다 상세 화면을 한 번 열어 주최 측 주소와 'AI 사용 가능' 문장 한 줄을 찾은 뒤,
틀.html 에 자료를 끼워 넣어 ../공모전모음.html 한 파일로 만든다.
표준 라이브러리만 쓰므로 기기마다 따로 설치할 것이 없다.

쓰는 법:  python collect.py            전부 모아 HTML 을 다시 만든다
          python collect.py --only 위비티 --dry   한 곳만 시험(파일을 쓰지 않음)
          python collect.py --render   사이트에 가지 않고 지난 자료로 HTML 만 다시 만든다
"""
import concurrent.futures as cf
import datetime as dt
import difflib
import hashlib
import html
import http.cookiejar
import json
import os
import re
import shutil
import ssl
import sys
import tempfile
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "공모전모음.html")
TEMPLATE = os.path.join(HERE, "틀.html")
OUT_M = os.path.join(ROOT, "공모전모음_모바일.html")          # 휴대폰용 화면(웹에서는 /m/ 주소)
TEMPLATE_M = os.path.join(HERE, "틀_모바일.html")
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


AGGREGATOR = re.compile(r"wevity\.com|linkareer\.com|contestkorea\.com|thinkcontest\.com", re.I)


def host_url(u):
    """주최 측 사이트 주소를 다듬는다. 주소 꼴이 아니거나 모음 사이트 자신을 가리키면 빈 글."""
    u = html.unescape((u or "").strip())
    if not u or len(u) > 600 or re.search(r"\s", u):
        return ""
    if not re.match(r"https?://", u, re.I):
        if not re.match(r"[\w-]+(\.[\w-]+)+(/|$)", u):
            return ""
        u = "http://" + u
    try:
        if AGGREGATOR.search(urllib.parse.urlsplit(u).netloc):
            return ""
    except ValueError:
        return ""
    return u


FORM_LINK = re.compile(r"forms\.gle|docs\.google\.com/forms|form\.naver\.com|naver\.me|bit\.ly|url\.kr|me2\.do|"
                       r"tally\.so|typeform\.com|google\.com/forms|surveymonkey", re.I)
LIST_WORD = re.compile(r"notice|board|bbs|news|list|community|gongji|press|공지|게시", re.I)


NOT_ID_KEY = re.compile(r"page|menu|manage|top|sub|cate|size|limit|offset|sort|row|cp$|tab|lang|year|utm_", re.I)
ID_KEY = re.compile(r"id|idx|no|num|seq|sn|pk|article|ntt|data|post|doc", re.I)
FILE_LINK = re.compile(r"download|attach|filedown|file_?id|/files?/|\.(?:pdf|hwpx?|zip|docx?|xlsx?|pptx?|jpe?g|png)(?:$|\?)", re.I)


def spec(u):
    """주소가 얼마나 구체적인가. 3=공고 글·전용 화면(글 번호가 있음), 2=하위 화면, 1=게시판 목록, 0=대문."""
    try:
        p = urllib.parse.urlsplit(u)
        pairs = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
                 if not k.lower().startswith("utm_")]
    except ValueError:
        return 0
    path = urllib.parse.unquote(p.path).rstrip("/")
    if FORM_LINK.search(u):
        return 3
    if not path and not pairs:
        return 0
    if re.search(r"\d{3,}|[0-9a-f]{8}-[0-9a-f]{4}|/[A-Za-z_-]*\d+$", path):
        return 3
    for k, v in pairs:
        if NOT_ID_KEY.search(k):
            continue
        if re.fullmatch(r"\d{3,}", v) or (re.fullmatch(r"\d+", v) and ID_KEY.search(k)) \
                or re.match(r"(?:view|read|detail)", v, re.I):
            return 3
    if LIST_WORD.search(path + "?" + "&".join(k + "=" + v for k, v in pairs)):
        return 1
    if re.fullmatch(r"/(?:index|main|home|default)(?:\.\w+)?", path, re.I) and not pairs:
        return 0
    return 2


def item(src, title, url, host="", cats="", target="", start=None, end=None,
         posted=None, note="", horg="", num=None, home=""):
    """한 건. target=응모 대상(사이트 표기 그대로), note=자격 덧말, horg=주최 분류, posted=사이트에 올라온 날,
    num=사이트 안 일련번호(올라온 날을 모를 때 이웃 번호로 어림하는 데 쓴다), home=주최 측 사이트 주소."""
    d = {"src": src, "title": clean(title), "url": url, "host": clean(host),
         "cats": clean(cats), "target": clean(target), "start": iso(start), "end": iso(end)}
    home = host_url(home)
    if home:
        d["home"] = home
    if posted:
        d["posted"] = iso(posted)
    if note:
        d["note"] = clean(note)[:160]
    if horg:
        d["horg"] = clean(horg)
    if num:
        d["num"] = int(num)
    return d


# ───────────────────────── 사이트별 수집 ─────────────────────────

def wevity():
    """위비티 — 전체 목록을 받고, 응모 대상별 목록을 한 번씩 더 돌아 누가 낼 수 있는지 표시한다.
    어느 목록이든 접수 중인 것이 앞쪽에, 마감된 것이 뒤쪽에 온다."""
    s, found = Session(), {}
    lists = [("1", "", "")] + [("2", cidx, who) for cidx, who in
                               (("32", "누구나"), ("4", "일반인"), ("5", "대학생"),
                                ("30", "청소년"), ("31", "어린이"), ("90", "기타"))]
    for gub, cidx, who in lists:
        for gp in range(1, 81):
            t = s.text(f"https://www.wevity.com/?c=find&s=1&gub={gub}"
                       + (f"&cidx={cidx}" if cidx else "") + f"&gp={gp}")
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
                if ix not in found:
                    day = re.search(r'<div class="day">(.*?)</div>', b, re.S)
                    cats = re.search(r'분야 : (.*?)</div>', b, re.S)
                    organ = re.search(r'<div class="organ">(.*?)</div>', b, re.S)
                    found[ix] = [item("위비티", re.sub(r"<span.*?</span>", "", m.group(2), flags=re.S),
                                      f"https://www.wevity.com/?c=find&s=1&gub=1&gbn=view&ix={ix}",
                                      host=organ.group(1) if organ else "",
                                      cats=cats.group(1) if cats else "",
                                      end=end_from_dday(clean(day.group(1)) if day else ""), num=ix), []]
                if who and who not in found[ix][1]:
                    found[ix][1].append(who)
            if got == 0 or open_cnt == 0:
                break
    out = []
    for row, whos in found.values():
        row["target"] = ", ".join(whos)
        out.append(row)
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
                                start=parse_date(x.get("accept_dt")), end=parse_date(x.get("finish_dt")),
                                posted=parse_date(x.get("putup_sdt")), horg=x.get("host_organ_nm"),
                                home=x.get("hompage_url"),
                                note=" / ".join(v.strip() for v in (x.get("enter_qualified_limit"),
                                                                     x.get("enter_qualified_etc"))
                                                if v and v.strip())))
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
                            host=a.get("organizationName"), end=end, num=a["id"]))
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
        link = urllib.parse.urljoin("https://www.korea.kr/", html.unescape(m.group(1)))
        out.append(item("정책브리핑", title[host.end():] if host else title, link,
                        home="" if "korea.kr" in link else link,      # 이 목록의 링크는 주최 측 사이트로 곧바로 간다
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
                            start=start, end=end, posted=parse_date(m.group(2)[:4] + "-" + m.group(2)[4:6]
                                                                    + "-" + m.group(2)[6:8])))
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


# ───────────────────────── 응모 자격 읽기 ─────────────────────────
# 화면의 '내 조건으로 거르기'가 쓰는 재료를 만든다. 판정은 화면(브라우저)이 한다 —
# 보는 사람의 조건(신분·사는 곳·생년월일)은 그 사람 브라우저에만 있고 여기로 오지 않는다.

WHO = [("누구나", r"제한\s*없음|누구나"), ("어린이", r"어린이|유치원|유아"), ("초등", r"초등"),
       ("중등", r"중학"), ("고등", r"고등|고교"), ("청소년", r"청소년"), ("대학원생", r"대학원"),
       ("대학생", r"대학생"), ("일반인", r"일반인"), ("기업", r"기업"), ("외국인", r"외국인"), ("기타", r"기타")]
WHO_RE = [(name, re.compile(pat)) for name, pat in WHO]


def who_tokens(target, title):
    """응모 대상 표기를 공통 낱말로. 사이트가 대상을 안 알려 주면 제목에서 짐작한다."""
    got = [name for name, rx in WHO_RE if rx.search(target or "")]
    if got and got != ["기타"]:
        return got
    if re.search(r"대학\(?원?\)?생", title):
        return ["대학생", "대학원생"]
    if re.search(r"청소년|고교생|고등학생|중학생|중·?고등?학생", title):
        return ["중등", "고등", "청소년"]
    if re.search(r"어린이|초등학생|유아|유치원", title):
        return ["어린이", "초등"]
    return got


SIDO = ["서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "경기", "강원",
        "충북", "충남", "전북", "전남", "경북", "경남", "제주"]
SIDO_LONG = {"서울특별시": "서울", "서울시": "서울", "부산광역시": "부산", "부산시": "부산", "대구광역시": "대구",
             "대구시": "대구", "인천광역시": "인천", "인천시": "인천", "광주광역시": "광주", "대전광역시": "대전",
             "대전시": "대전", "울산광역시": "울산", "울산시": "울산", "세종특별자치시": "세종", "세종시": "세종",
             "경기도": "경기", "강원특별자치도": "강원", "강원도": "강원", "충청북도": "충북", "충청남도": "충남",
             "전북특별자치도": "전북", "전라북도": "전북", "전라남도": "전남", "경상북도": "경북", "경상남도": "경남",
             "제주특별자치도": "제주", "제주도": "제주"}
# 시·군·구 이름(뒤에 시·군·구가 붙었을 때만 지역으로 본다 — '예산', '고령', '연수' 같은 보통 낱말과 겹치기 때문)
SIGUNGU = {
    "서울": "종로 용산 성동 광진 동대문 중랑 성북 강북 도봉 노원 은평 서대문 마포 양천 구로 금천 영등포 동작 관악 서초 강남 송파 강동",
    "부산": "해운대 기장 사하 사상 금정 연제 영도 동래 부산진 수영",
    "대구": "달서 수성 달성 군위",
    "인천": "강화 옹진 부평 계양 미추홀 연수 남동",
    "광주": "광산", "대전": "유성 대덕", "울산": "울주",
    "경기": "수원 성남 의정부 안양 부천 광명 평택 동두천 안산 고양 과천 구리 남양주 오산 시흥 군포 의왕 하남 용인 "
            "파주 이천 안성 김포 화성 양주 포천 여주 연천 가평 양평",
    "강원": "춘천 원주 강릉 동해 태백 속초 삼척 홍천 횡성 영월 평창 정선 철원 화천 양구 인제 양양",
    "충북": "청주 충주 제천 보은 옥천 영동 증평 진천 괴산 음성 단양",
    "충남": "천안 공주 보령 아산 서산 논산 계룡 당진 금산 부여 서천 청양 홍성 예산 태안",
    "전북": "전주 군산 익산 정읍 남원 김제 완주 진안 무주 장수 임실 순창 고창 부안",
    "전남": "목포 여수 순천 나주 광양 담양 곡성 구례 고흥 보성 화순 장흥 강진 해남 영암 무안 함평 영광 장성 완도 진도 신안",
    "경북": "포항 경주 김천 안동 구미 영주 영천 상주 문경 경산 의성 청송 영양 영덕 청도 고령 성주 칠곡 예천 봉화 울진 울릉",
    "경남": "창원 진주 통영 사천 김해 밀양 거제 양산 의령 함안 창녕 남해 하동 산청 함양 거창 합천",
    "제주": "서귀포",
}
SGG = {name: sido for sido, names in SIGUNGU.items() for name in names.split()}
SGG_RE = re.compile("(" + "|".join(sorted(SGG, key=len, reverse=True)) + ")(?=시|군|구)")
SIDO_RE = re.compile("|".join(sorted(SIDO_LONG, key=len, reverse=True))
                     + "|" + "|".join(s for s in SIDO if s != "경기"))     # '경기'만으로는 운동 경기와 못 가린다
LOCAL_CUE = re.compile(r"시민|도민|군민|구민|관내|거주|소재|주민|지역\s*제한")
NOT_LOCAL = re.compile(r"^\s*(국내|전국|온라인|대한민국|해당\s*없음|없음)")


def regions_in(text):
    out = []
    for m in SIDO_RE.finditer(text):
        out.append(SIDO_LONG.get(m.group(0), m.group(0)))
    for m in SGG_RE.finditer(text):
        out.append(SGG[m.group(1)])
    return list(dict.fromkeys(out))


def region_rule(target, note, title, host):
    """지역 제한이 있으면 (시·도 목록, 지역을 못 알아냈는가)를 돌려준다. 제한이 없으면 ([], False)."""
    flagged = "지역 제한" in (target or "")
    cue = LOCAL_CUE.search(note or "")
    if not flagged and not cue:
        # 제목의 '시민·도민'만으로는 단정하지 않는다('민주시민'·'세계시민'도 있다) — 확인 필요로만 둔다
        return [], bool(re.search(r"시민|도민|군민|구민", title)) and not re.search(r"민주시민|세계시민|시민의식", title)
    if not flagged and NOT_LOCAL.match(note or ""):
        return [], False
    for text in (note or "", title, host or ""):       # 자격 덧말 → 제목 → 주최 순으로 지역 이름을 찾는다
        found = regions_in(text)
        if found:
            return found, False
    return [], True


def age_rule(text):
    """'만 19~39세'·'만 14세 이상'·'34세 이하' → [가장 어린 나이, 가장 많은 나이]. 없으면 None."""
    text = re.sub(r"[^.。/]*?(법정대리인|보호자|동의)[^.。/]*", " ", text)     # '만 14세 미만은 보호자 동의'는 제한이 아니다
    lo, hi = None, None
    m = re.search(r"(\d{1,2})\s*세?\s*[~\-–]\s*만?\s*(\d{1,2})\s*세(?!대)", text)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
    else:
        m = re.search(r"(\d{1,2})\s*세\s*(이상|부터)", text)
        if m:
            lo = int(m.group(1))
        m = re.search(r"(\d{1,2})\s*세\s*(이하|까지|미만)", text)
        if m:
            hi = int(m.group(1)) - (1 if m.group(2) == "미만" else 0)
    if lo is None and hi is None:
        if re.search(r"시니어|어르신|실버|노인|신중년", text):
            return [50, 150]
        return None
    return [lo if lo is not None else 0, hi if hi is not None else 150]


NOTE_STRICT = re.compile(r"회원|재학|휴학|임직원|교원|교사|종사자|소속|이용자|작가|전공|졸업|수료|경력|보유|등록|"
                         r"사업자|단체|장애|여성|학부모|가족|유학생|장병|군인|동문|신인|보호자와")
NOTE_OPEN = re.compile(r"누구나|국민|제한\s*없|관심\s*(이\s*)?있는|요강|홈페이지|참조|참고|상세|전국|모든|대한민국")
TITLE_STRICT = re.compile(r"재학생|임직원|교원|교사|장병|유학생|여성|학부모|동문")
PUBLIC_ONLY = re.compile(r"공무원|공직자|공공기관\s*(임|재)?직(원|자)|공공부문\s*종사자")


def note_rule(note, title):
    """자격 덧말에 따로 확인할 조건이 있으면 보여 줄 글을, 없으면 빈 글을 돌려준다."""
    note = re.sub(r"[^/]*(법정대리인|보호자)\s*(의\s*)?동의[^/]*", "", note or "").strip(" /")
    words = [w.strip() for w in re.split(r"[,/]", note) if w.strip()]
    plain = {"누구나", "유치원", "어린이", "초등학생", "중학생", "고등학생", "청소년", "대학생", "대학원생",
             "일반인", "외국인", "기업", "기타", "제한 없음", "제한없음"}
    if words and all(w in plain for w in words):
        note = ""                                   # 대상 낱말만 늘어놓은 덧말은 버린다
    # 나이·지역처럼 따로 읽어 낸 조건만 적힌 덧말도 버린다('부산 거주', '만 34세 이하', '국내 거주')
    rest = re.sub(r"만?\s*\d{1,2}\s*세?\s*[~\-–]\s*만?\s*\d{1,2}\s*세|만?\s*\d{1,2}\s*세\s*(이상|이하|미만|부터|까지)?", " ", note)
    rest = SGG_RE.sub(" ", SIDO_RE.sub(" ", rest))
    rest = re.sub(r"거주자?|국내|전국|온라인|누구나|시민|도민|군민|구민|[\s,./·()의및]", "", rest)
    if len(rest) < 2:
        note = ""
    if note and (NOTE_STRICT.search(note) or not NOTE_OPEN.search(note)):
        return note[:70]
    m = TITLE_STRICT.search(title)
    return f"제목에 '{m.group(0)}'" if m else ""


# ───────────────────────── 공공·민간 가르기 ─────────────────────────
# 공공 = 중앙부처·지방자치단체·교육청·공공기관·공기업이 여는 것. 그 밖(기업·언론·협회·학교·민간 재단)은 민간.

PUB_HORG = re.compile(r"중앙정부|지방자치단체|공공기관|공기업")
MINISTRY = ("교육부|과학기술정보통신부|외교부|통일부|법무부|국방부|행정안전부|국가보훈부|문화체육관광부|농림축산식품부|"
            "산업통상자원부|산업통상부|보건복지부|환경부|기후에너지환경부|고용노동부|여성가족부|성평등가족부|국토교통부|"
            "해양수산부|중소벤처기업부|기획재정부|재정경제부")
PUB_TOKEN = re.compile(
    r"^(" + MINISTRY + r")$"
    r"|^[가-힣]{2,}(처|청)$"                                             # 법제처·산림청·시청·구청·교육청
    r"|^(서울|부산|대구|인천|광주|대전|울산|세종)(특별|광역|특별자치)?시$"
    r"|^(경기|강원|충청[남북]|전라[남북]|경상[남북]|제주|전북|강원특별자치|전북특별자치|제주특별자치)도$"
    r"|^[가-힣]{1,4}(?<![도택표전게즉증])시$|^[가-힣]{1,4}(?<![장대해육공])군$"
    r"|^[가-힣]{1,4}(?<![연가농야축배친기도지탁당])구$")
PUB_WORD = re.compile(
    r"공사$|공사[\s,/·(]|공단|진흥원|개발원|정보원|관리원|평가원|인재원|국립|시립|도립|군립|구립|경찰|소방|보건소|의회|"
    r"우체국|우정사업|한국은행|교육청|교육지원청|한국전력|한전|코레일|건강보험|국민연금|근로복지|"
    r"(시|군|구)(립)?(도서관|박물관|미술관|문화원)|"
    r"(?<!조직)(?<!추진)(?<!집행)(?<!준비)위원회|"
    r"\b(KOTRA|LH|LX|aT|KOICA|KISA|KISTI|KEIT|KSPO|KOBACO|KIAT|NIA|NIPA|KOCCA|KERIS|KTL)\b|"
    r"(서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|전북|전남|경북|경남|제주|[가-힣]{2,3}(시|군|구))\S*재단")


def is_public(srcs, horgs, hosts):
    if "소통24" in srcs or "정책브리핑" in srcs:
        return True
    if any(PUB_HORG.search(h) for h in horgs):
        return True
    for host in hosts:
        if PUB_WORD.search(host):
            return True
        if any(PUB_TOKEN.match(tok) for tok in re.split(r"[\s,/·()\[\]&]+", host) if tok):
            return True
    return False


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


# ───────────────────────── 주최 측 사이트 주소 ─────────────────────────
# 씽굿은 목록에 주소가 실려 오고, 정책브리핑은 링크가 곧 주최 측 사이트다.
# 위비티·링커리어·콘테스트코리아는 공모전 상세 화면의 '홈페이지' 칸에 있다. 한 번 열어 본 것은
# _기록.json 에 적어 두고 다시 열지 않으므로, 날마다 새로 올라온 것만 열어 보게 된다.

DETAIL_ORDER = ("위비티", "링커리어", "콘테스트코리아")
DETAIL_CAP = 500        # 한 번 실행에 사이트마다 열어 볼 상세 화면 수의 한도(처음 채울 때 한꺼번에 몰리지 않게)


def home_from_page(src, t):
    if src == "위비티":
        m = re.search(r'<span class="tit">홈페이지</span>\s*<a href="([^"]+)"', t)
        return host_url(m.group(1)) if m else ""
    if src == "링커리어":
        m = re.search(r'"homepageURL":("(?:[^"\\]|\\.)*")', t)
        return host_url(json.loads(m.group(1))) if m else ""
    m = re.search(r'<a href="([^"]+)"[^>]*class="homepage_go"', t)
    return host_url(m.group(1)) if m else ""


# ───────────────────────── AI 사용 가능 표기 ─────────────────────────
# 모음 사이트 상세 화면의 공고 본문(글자로 적힌 부분)에서 'AI 활용 가능'처럼 생성형 AI 로 만든 작품을
# 받는다고 적은 문장을 찾는다. 찾은 문장 한 줄만 _기록.json 의 ai 에 적어 두고(없으면 빈 글) 다시 열지 않는다.
# 본문이 그림(포스터)뿐인 공고와 정책브리핑(주최 측 사이트로 바로 감)은 읽지 못한다.

AI_SRCS = ("씽굿", "콘테스트코리아", "링커리어", "위비티", "소통24")
# 사이트별 공고 본문 자리(시작 표시, 끝 표시). 옆에 딸린 다른 공모전 목록·광고를 읽지 않으려는 것이다.
BODY_CUT = {"씽굿": ('id="tab01"', 'id="tab02"'),
            "위비티": ('id="viewContents"', 'class="event-notice'),
            "콘테스트코리아": ('view_detail_area', 'class="list_style_2"'),
            "링커리어": ('<article id="DETAIL"', 'ActivityDetailTabReport')}
AIW = r"(?<![A-Za-z])(?:AI|A\.I\.|인공지능|에이아이)(?![A-Za-z])"
AI_VERB = r"(?:활용|사용|이용|제작|생성|창작|작업|보정|도구|툴|프로그램|기술|기능)"
# '지속가능'·'실현 가능성'·'구동 가능한'·'활용할 수 있는 분'·'허용 여부' 같은 말은 허용 표기가 아니다
AI_OK = (r"(?:(?<!지속)가능(?!성|한)|허용(?!\s*여부)|무방|필수|OK|(?:할|하실)\s*수\s*있(?!는|도록|게)"
         r"|(?:하여도|해도)\s*(?:됩|되|좋|무방))")
AI_POS = re.compile(AIW + r".{0,30}?" + AI_VERB + r".{0,20}?" + AI_OK)             # 'AI 활용 가능', 'AI 는 보조 작업에 한하여 사용할 수 있'
AI_POS2 = re.compile(r"생성형\s*" + AIW + r"\s*(?:툴|도구|프로그램)?\s*[를로을]?\s*(?:활용|이용|사용)(?:해|하여|한)\s*"
                     r".{0,25}?(?:제작|표현|작업|작품|영상|이미지|콘텐츠)"
                     r"|생성형\s*" + AIW + r"\s*(?:툴|도구|프로그램)?\s*(?:로|으로)\s*(?:제작|만든|만들|생성)"
                     r"|" + AIW + r"\s*활용\s*부문")
AI_NEG = re.compile(r"금지|불가|불허|제외|취소|감점|불이익|탈락|실격|무효|회수|환수|수\s*없|안\s*됩|않|결격|표절|지양|삼가|박탈|여부|문의|요청|제출")
AI_SKIP = re.compile(r"누구나|관심|파일|확장자|형식|포맷|일러스트|PSD|EPS|PDF|JPG|PNG", re.I)   # 참가 자격 문장, 'AI 파일'(일러스트레이터)
AI_MADE = (r"(?:영상|숏폼|쇼츠|광고|콘텐츠|미디어|웹툰|캐릭터|음악|뮤직|뮤비|아트|이미지|그림|영화|필름|창작|크리에이|패션|"
           r"포스터|사진|동화|웹드라마|애니|작곡|디자인)")
AI_TITLE = re.compile(r"(?<![A-Za-z])AI(?![A-Za-z])\s?(?:활용\s?\S{0,6}\s?|게임\s?)?" + AI_MADE
                      + r"|(?<![A-Za-z])AI\s+(?:Music|Video|Film|Art)\b", re.I)
AI_TITLE_NOTE = "제목으로 보아 AI로 만든 작품을 받는 공모전"


def body_text(src, t):
    """상세 화면에서 공고 본문만 잘라 글자로 바꾼다. 줄이 바뀌는 자리는 ¶ 로 남긴다."""
    if src == "소통24":
        m = re.search(r'property="og:description"\s+content="([^"]*)"', t)
        return clean(m.group(1)) if m else ""
    start, stop = BODY_CUT[src]
    i = t.find(start)
    if i < 0:
        return ""
    j = t.find(stop, i)
    seg = "<x " + t[i:j if j > 0 else i + 80000]
    seg = re.sub(r"<(script|style)\b.*?</\1>", " ", seg, flags=re.S | re.I)
    seg = re.sub(r"<(?:br|/p|/div|/li|/tr|/h\d)\b[^>]*>", " ¶ ", seg, flags=re.I)
    return clean(seg)


BODY_SRCS = ("씽굿", "콘테스트코리아", "링커리어", "위비티", "소통24")
LINK_SKIP = re.compile(r"kakao|facebook\.com|instagram\.com|youtube\.com|youtu\.be|twitter\.com|//x\.com|apple\.com|"
                       r"play\.google|googletagmanager|w3\.org|share\.naver|mois\.go\.kr/html|modu\.go\.kr|korea\.kr|"
                       r"media-cdn|\.(?:jpe?g|png|gif|bmp|svg|pdf|hwpx?|zip|docx?|xlsx?|pptx?|js|css)(?:$|\?)", re.I)


def body_link(src, t):
    """'홈페이지' 칸이 빈 공고 — 공고 본문에 적힌 바깥 주소 가운데 하나를 고른다. 없으면 빈 글.
    주최 측 화면 주소를 신청서(구글 폼 등)보다 앞세우고, 그 안에서는 더 구체적인 주소를 고른다."""
    if src == "소통24":
        m = re.search(r'property="og:description"\s+content="([^"]*)"', t)
        seg = html.unescape(html.unescape(m.group(1))) if m else ""
    else:
        start, stop = BODY_CUT[src]
        i = t.find(start)
        if i < 0:
            return ""
        j = t.find(stop, i)
        seg = html.unescape(t[i:j if j > 0 else i + 80000])
    best, rank = "", None
    for n, raw in enumerate(re.findall(r"https?://[^\s\"'<>()\[\]\\]+", seg)):
        u = host_url(raw.rstrip(".,;:!?·"))
        if not u or LINK_SKIP.search(u):
            continue
        r = (0 if FORM_LINK.search(u) else 1, spec(u), -n)
        if rank is None or r > rank:
            best, rank = u, r
    return best


# ───────────────────────── 목록·대문 주소에서 공고 글 찾기 ─────────────────────────
# 주최 측 주소가 공지사항 목록이나 대문이면, 그 화면을 한 번 열어 제목이 같은 글의 링크를 찾는다.
# 찾은 주소(없으면 빈 글)는 _기록.json 의 post 에 적어 두고 다시 열지 않는다. 못 연 곳은 한 번만 더 해 본다.

POST_CAP = 400
SSL_LOOSE = ssl._create_unverified_context()      # 인증서가 낡은 기관 사이트도 읽기만 한다


def find_post(home, title):
    req = urllib.request.Request(home, headers={"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"})
    with urllib.request.urlopen(req, timeout=15, context=SSL_LOOSE) as r:
        raw, base = r.read(1500000), r.geturl()
    try:
        t = raw.decode("utf-8")
    except UnicodeDecodeError:
        t = raw.decode("cp949", "replace")
    key, best, score = norm(title), "", 0.0
    site = re.sub(r"^www\.", "", urllib.parse.urlsplit(base).netloc.lower())      # 같은 사이트 안의 글만 받는다
    for m in re.finditer(r"<a\b([^>]*)>(.*?)</a>", t, re.S | re.I):
        href = re.search(r'href\s*=\s*["\']([^"\']+)["\']', m.group(1), re.I)
        if not href or re.match(r"\s*(?:javascript:|#|mailto:|tel:)", href.group(1), re.I):
            continue
        tip = re.search(r'title\s*=\s*["\']([^"\']+)["\']', m.group(1), re.I)
        for label in (m.group(2), tip.group(1) if tip else ""):
            name = norm(clean(label))
            if len(name) < 6:
                continue
            if key in name or (name in key and len(name) >= max(8, 0.6 * len(key))):
                sc = 1.0
            else:
                sc = difflib.SequenceMatcher(None, key, name).ratio()
            if sc > score and sc >= 0.8:
                u = host_url(urllib.parse.urljoin(base, html.unescape(href.group(1).strip())))
                # 첨부파일·신청서·SNS 가 아니고, 지금 주소보다 구체적인 화면이어야 한다
                if u and not FILE_LINK.search(u) and not FORM_LINK.search(u) and not LINK_SKIP.search(u) \
                        and spec(u) >= 2 and spec(u) > spec(home) \
                        and re.sub(r"^www\.", "", urllib.parse.urlsplit(u).netloc.lower()) == site:
                    best, score = u, sc
    return best


def fetch_posts(want, posts):
    """want = [(주최 측 주소, 제목)…]. 사이트가 제각각이라 여럿을 나란히 연다."""
    jobs = list(dict.fromkeys(want))[:POST_CAP]

    def work(job):
        home, title = job
        try:
            return find_post(home, title)
        except Exception:
            return None

    if jobs:
        with cf.ThreadPoolExecutor(max_workers=12) as pool:
            for (home, title), got in zip(jobs, pool.map(work, jobs)):
                k = home + " " + norm(title)
                if got is None:
                    got = "" if posts.get(k) == "!" else "!"      # 못 열었음 — 한 번만 더 해 본다
                posts[k] = got
    return len(jobs)


def ai_note(text):
    """AI 사용을 허용한다고 적은 문장을 찾아 한 줄(110자까지)로 돌려준다. 없으면 빈 글."""
    for clause in re.split(r"¶|(?<=[.!?。])\s+|\s[-–•⦁●■□○◦※▶▷◆◇*]\s?", text):
        clause = clause.strip(" -–·:,")
        if len(clause) < 6:
            continue
        hit = None
        for m in AI_POS.finditer(clause):
            near = clause[max(0, m.start() - 14):m.end() + 6]
            if not AI_NEG.search(m.group(0)) and not AI_SKIP.search(near):
                hit = m
                break
        if hit is None and not AI_NEG.search(clause) and not AI_SKIP.search(clause):
            hit = AI_POS2.search(clause)
        if hit:
            a = max(0, min(hit.start() - 30, len(clause) - 110))
            return ("…" if a else "") + clause[a:a + 110].strip() + ("…" if len(clause) > a + 110 else "")
    return ""


def fetch_details(todo, homes, ais, bodies):
    """todo = [(사이트, 상세 화면 주소)…]. 사이트마다 차례로(사이트끼리는 나란히) 한 번씩 열어
    주최 측 주소는 homes 에, AI 사용 가능 문장은 ais 에 적는다."""
    by_src = {}
    for src, url in dict.fromkeys(todo):
        by_src.setdefault(src, []).append(url)

    def work(src):
        s, h, a, b, bad = Session(), {}, {}, {}, 0
        for url in by_src[src][:DETAIL_CAP]:
            try:
                t = s.text(url)
                if src in DETAIL_ORDER and url not in homes:
                    h[url] = home_from_page(src, t)
                if src in AI_SRCS and url not in ais:
                    a[url] = ai_note(body_text(src, t))
                if src in BODY_SRCS and url not in bodies:
                    b[url] = body_link(src, t)
                bad = 0
            except Exception:
                bad += 1                       # 못 연 것은 적지 않는다 — 다음 실행 때 다시 해 본다
                if bad >= 5:
                    break                      # 잇달아 막히면 그 사이트는 이번엔 그만둔다
        return h, a, b, len(by_src[src][:DETAIL_CAP])

    done = 0
    if by_src:
        with cf.ThreadPoolExecutor(max_workers=len(by_src)) as pool:
            results = list(pool.map(work, list(by_src)))
        for h, a, b, n in results:
            homes.update(h)
            ais.update(a)
            bodies.update(b)
            done += n
    return done


def guess_posted(nums, table):
    """올라온 날을 알려 주지 않는 사이트(위비티·링커리어)는 일련번호가 가까운 이웃들의 날짜로 어림한다.
    table = {사이트: [(일련번호, 날짜 서수), …]} — 다른 사이트에도 올라와 날짜를 아는 것들."""
    for src, num in nums.items():
        pairs = table.get(src) or []
        if len(pairs) < 8:
            continue
        near = sorted(pairs, key=lambda p: abs(p[0] - num))[:7]
        days = sorted(d for _, d in near)
        return dt.date.fromordinal(days[len(days) // 2])
    return None


def merge(rows_by_src, first_seen, first_run, homes, ais, bodies, posts):
    """homes = {상세 화면 주소: 주최 측 주소 또는 빈 글}, ais = {상세 화면 주소: AI 사용 가능 문장 또는 빈 글}.
    bodies = {상세 화면 주소: 본문 속 바깥 주소}, posts = {주최 측 주소 + 제목: 그 화면에서 찾은 공고 글 주소}.
    돌려주는 todo 는 아직 열어 보지 않은 상세 화면들, want 는 공고 글을 찾아볼 (목록·대문 주소, 제목)들."""
    merged, order, todo, want = {}, [], [], []
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
                m = {"t": r["title"], "s": None, "e": None, "l": [], "hosts": [], "cats": [], "who": [],
                     "notes": [], "horgs": [], "posted": [], "nums": {}, "homes": []}
                merged[key] = m
                order.append(key)
            m["s"] = m["s"] or r.get("start")          # 겹치는 공모전 — 비어 있는 칸만 채운다
            m["e"] = m["e"] or end
            for box, field in (("hosts", "host"), ("cats", "cats"), ("who", "target"), ("notes", "note"),
                               ("horgs", "horg"), ("posted", "posted")):
                if r.get(field) and r[field] not in m[box]:
                    m[box].append(r[field])
            if r.get("num"):
                m["nums"].setdefault(name, r["num"])
            if r.get("home") and r["home"] not in m["homes"]:
                m["homes"].append(r["home"])
            if not any(src == name for src, _ in m["l"]):
                m["l"].append([name, r["url"]])

    table = {}                                 # 올라온 날을 아는 것들로 '일련번호 → 날짜' 눈금을 만든다
    for m in merged.values():
        if m["posted"]:
            day = parse_date(min(m["posted"])).toordinal()
            for src, num in m["nums"].items():
                table.setdefault(src, []).append((num, day))

    items, seen_now = [], {}
    for key in order:
        m = merged[key]
        seen = first_seen.get(key)
        if seen is None:
            seen = "" if first_run else iso(TODAY)     # 첫 실행 때 이미 있던 것은 처음 본 날을 모른다
        seen_now[key] = seen
        title, host = m["t"], (m["hosts"][0] if m["hosts"] else "")
        target, note = ", ".join(m["who"]), " / ".join(m["notes"])
        srcs = [src for src, _ in m["l"]]
        row = {"t": title, "h": host, "f": fields_of(", ".join(m["cats"]), title), "s": m["s"], "e": m["e"],
               "l": m["l"], "p": 1 if is_public(srcs, m["horgs"], m["hosts"]) else 0,
               "w": who_tokens(target, title)}

        # 주최 측 사이트 주소: 여러 사이트가 알려 준 주소(목록에 실려 온 것 + 상세 화면 '홈페이지' 칸)를 모두 모아
        # 가장 구체적인 것(공고 글 > 하위 화면 > 게시판 목록 > 대문)을 고른다. 하나도 없으면 공고 본문 속 주소를 쓴다.
        cands = list(m["homes"])
        for src in DETAIL_ORDER:
            for s2, url in m["l"]:
                if s2 != src:
                    continue
                if url not in homes:
                    todo.append((src, url))
                elif homes[url] and homes[url] not in cands:
                    cands.append(homes[url])
        if not cands:
            for src, url in m["l"]:
                if src not in BODY_SRCS:
                    continue
                if url not in bodies:
                    todo.append((src, url))
                elif bodies[url] and bodies[url] not in cands:
                    cands.append(bodies[url])
        home = max(cands, key=spec) if cands else ""       # 같은 등급이면 앞에 있는 것
        if home and spec(home) <= 1:                        # 목록·대문이면 그 화면에서 이 공모전 글을 찾아본다
            k = home + " " + key
            if posts.get(k, "!") == "!":
                want.append((home, title))
            if posts.get(k) not in (None, "", "!"):
                home = posts[k]
        if home:
            row["u"] = home

        # AI 사용 가능 표기: 공고 본문에서 찾은 문장 → 없으면 제목('AI 영상 공모전' 같은 것)
        ai = ""
        for src, url in m["l"]:
            if src not in AI_SRCS:
                continue
            if url not in ais:
                todo.append((src, url))
            elif ais[url] and not ai:
                ai = ais[url]
        if not ai and AI_TITLE.search(title):
            ai = AI_TITLE_NOTE
        if ai:
            row["ai"] = ai

        # 올라온 날: 사이트가 알려 준 날 → 이 도구가 처음 본 날 → 이웃 번호로 어림 → 접수 시작일
        posted, rough = (parse_date(min(m["posted"])) if m["posted"] else None), False
        if posted is None and seen:
            posted = parse_date(seen)
        if posted is None:
            posted, rough = guess_posted(m["nums"], table), True
        if posted is None and m["s"] and m["s"] <= iso(TODAY):
            posted, rough = parse_date(m["s"]), True
        if posted:
            row["d"] = iso(min(posted, TODAY))
            if rough:
                row["dr"] = 1

        # 응모 자격 재료
        rg, unknown = region_rule(target, note, title, host)
        if rg:
            row["rg"] = rg
        elif unknown:
            row["rx"] = 1
        age = age_rule(title + " / " + note)
        if age:
            row["a"] = age
        elif "청년" in title or "청년" in note:
            row["y"] = 1
        if PUBLIC_ONLY.search(title + " " + note):
            row["pb"] = 1
        q = note_rule(note, title)
        if q:
            row["q"] = q
        items.append(row)
    return items, seen_now, todo, want


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

    homes, ais = state.get("home", {}), state.get("ai", {})
    bodies, posts = state.get("body", {}), state.get("post", {})
    args = (rows_by_src, state.get("first_seen", {}), first_run, homes, ais, bodies, posts)
    online = not render_only and "--no-detail" not in argv
    items, seen_now, todo, want = merge(*args)
    if todo and online:
        t0 = time.time()
        got = fetch_details(todo, homes, ais, bodies)
        items, seen_now, left, want = merge(*args)
        log(f"  상세 화면 {got}건을 열어 봄 ({time.time() - t0:.0f}초) · 아직 안 열어 본 것 {len(set(left))}건")
    if want and online:
        t0 = time.time()
        got = fetch_posts(want, posts)
        items, seen_now, _, left = merge(*args)
        log(f"  목록·대문 주소 {got}곳에서 공고 글을 찾아봄 ({time.time() - t0:.0f}초) · 남은 것 {len(set(left))}건")
    grade = [spec(x["u"]) for x in items if x.get("u")]
    log(f"  제목 링크: 공고 글·전용 화면 {sum(1 for g in grade if g >= 2)}건 · 게시판 목록 {grade.count(1)}건"
        f" · 주최 측 대문 {grade.count(0)}건 · 모음 사이트 {len(items) - len(grade)}건")
    log(f"  주최 측 주소를 아는 것 {sum(1 for x in items if x.get('u'))}건 / 모르는 것 {sum(1 for x in items if not x.get('u'))}건"
        f" / AI 사용 가능 표기 {sum(1 for x in items if x.get('ai'))}건")
    pub = sum(1 for x in items if x["p"])
    log(f"  공공 {pub}건 · 민간 {len(items) - pub}건 / 올라온 날: 확인 {sum(1 for x in items if x.get('d') and not x.get('dr'))}건"
        f" · 어림 {sum(1 for x in items if x.get('dr'))}건 · 모름 {sum(1 for x in items if not x.get('d'))}건"
        f" / 대상 정보 없음 {sum(1 for x in items if not x['w'])}건")
    data = {"generated": now, "sources": report, "fields": [n for n, _ in FIELDS] + ["기타"], "items": items}
    pages = []                                  # (화면 틀, 결과물) — PC 화면과 휴대폰 화면
    for tpl_path, out_path in ((TEMPLATE, OUT), (TEMPLATE_M, OUT_M)):
        if not os.path.exists(tpl_path):
            continue
        with open(tpl_path, encoding="utf-8") as f:
            tpl = f.read()
        if "/*__DATA__*/null" not in tpl:
            raise RuntimeError(f"{os.path.basename(tpl_path)} 에 자료를 끼울 자리(/*__DATA__*/null)가 없음")
        pages.append((tpl, out_path))
    # 판 번호 = 받은 때 + 화면 틀들의 지문. 웹에서는 version.txt 로도 내보내, 브라우저가 옛 화면을 붙들고 있으면
    # 화면이 스스로 알아채고 새로 불러오게 한다.
    mark = hashlib.sha1("".join(tpl for tpl, _ in pages).encode("utf-8")).hexdigest()[:8]
    data["build"] = re.sub(r"\D", "", now) + "-" + mark
    write_synced(os.path.join(ROOT, "version.txt"), data["build"] + "\n")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    for tpl, out_path in pages:
        write_synced(out_path, tpl.replace("/*__DATA__*/null", payload))
    if ok_count == len(SOURCES):          # 여섯 곳을 다 받았을 때만, 목록에서 사라진 공모전의 주소 기록을 지운다
        live = {u for rows in rows_by_src.values() for u in (r["url"] for r in rows)}
        homes = {k: v for k, v in homes.items() if k in live}
        ais = {k: v for k, v in ais.items() if k in live}
        bodies = {k: v for k, v in bodies.items() if k in live}
        known = set(homes.values()) | set(bodies.values()) | {r.get("home") for rows in rows_by_src.values() for r in rows}
        posts = {k: v for k, v in posts.items() if k.rsplit(" ", 1)[0] in known}
    write_synced(STATE, json.dumps({"last_run": now, "first_seen": seen_now, "home": homes, "ai": ais, "body": bodies, "post": posts,
                                        "sources": cache},
                                   ensure_ascii=False, separators=(",", ":")))
    log(f"완료 — 겹친 것을 합쳐 {len(items)}건, {os.path.basename(OUT)} 갱신")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main(sys.argv[1:]))
