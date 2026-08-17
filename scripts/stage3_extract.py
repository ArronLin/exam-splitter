# -*- coding: utf-8 -*-
"""Stage 3 — detect exam-set boundaries and extract full titles from OCR.

Strategy (robust to the two most common scan-header problems):
  1. Circled item numbers (⑫, ⑱ …) at the very beginning of the title line
     are mis-OCR'd into garbage Chinese chars (其/龟/和/人/入…). We strip
     them before matching.
  2. Headers are often two physical lines: line 1 = school(+campus), line 2 =
     "初20xx级新初一分班（奖学金）真卷数学". We merge consecutive lines when
     the first line contains a school but no year and the second line carries
     the year/subtitle.
  3. Body text ("六年级共有学", "目平均每个同学"…) can accidentally match a
     loose "学" suffix. We require a real school suffix and reject lines that
     contain grade/body-only words.

Output: <workdir>/exam_sets.json
  list of {start,end,school,branch,year,title}
  where `title` is the cleaned full header string used for the output filename.
"""
import os
import json
import argparse
import re
from common import load_layout, load_cuts

# --- Tunables (region-independent) ---
# 学校后缀：越长越优先，去掉单独的 "学"（会把正文误当学校）
SUFFIX = r'(?:初中学校|学校|中学|一中|附中|学院|学术|学木)'
SCHOOL_RE = re.compile(
    r'([\u4e00-\u9fff]{2,16}?' + SUFFIX + r')'
    r'\s*([（(]\s*([\u4e00-\u9fff]{1,10})\s*[)）])?')
YEAR_RE = re.compile(r'初\s*(\d{4})\s*(?:级|届)?')
YEAR_RE2 = re.compile(r'(\d{4})\s*届')
# 届字被 OCR 破坏时（如 "2025 fe ... 一分班"），只要 20xx 与分班/入学/新初一/真卷
# 出现在同一句/相邻行，仍可判定为年份。正文极少同时出现 20xx + 这些词。
YEAR_RE3 = re.compile(r'(\d{4})(?:\s*[a-zA-Z]*)?\s*(?:届|级|分班|入学|新初一|真卷)')
YEAR_CAND = re.compile(r'(\d{4})')

# 已知 OCR 把带圈数字读成的开头杂字，以及把"某"误读的字
LEADING_NOISE_WORDS = ["龟", "和", "入", "人的", "国", "图", "罗", "钨", "的", "僵", "例", "因", "人知"]
GARBAGE_SINGLE = "龟和入国图罗钨的僵例因色站"

# 常见 OCR 错字修正（作用于整行/标题）
FIX = [
    ("学术", "学校"), ("学木", "学校"),
    ("彭祥", "嘉祥"), ("其嘉祥", "某嘉祥"),
    ("基西川", "某西川"), ("基绵实", "某绵实"),
    ("邮都区", "郫都区"), ("郭都区", "郫都区"),
    ("其夹验", "某实验"),
    ("北第二外国语学院", "北京第二外国语学院附属中学"),
    ("人学", "入学"),
    ("盐祥", "嘉祥"),
    ("菜师大", "某师大"),
    ("龙录师一", "龙泉师一"),
    ("奖池金", "奖学金"),
    # 石室联合中学 OCR 成 "成都某联中"：联中 -> 联合中学。安全：
    # "联合中学"=联/合/中/学，不含相邻"联中"子串，不会对已正确的校名二次触发。
    ("联中", "联合中学"),
    # 实验外国语学校 OCR 漏掉"语"成 "验外国学校"：该错误串并不出现在正确
    # 的 "实验外国语学校" 中（正确串在外-国之间还有"语"），故替换安全。
    ("验外国学校", "实验外国语学校"),
    # 嘉祥北城 OCR 首字误为"亮"，改正。
    ("亮祥北城", "嘉祥北城"),
    # 郫都区博瑞实验学校 OCR 严重破坏：校名主体"成都某郫都区博瑞"被吞，
    # 仅剩"前实验学校"（"前"为残字）。直接还原为完整校名。
    ("前实验学校", "成都某郫都区博瑞实验学校"),
    # NOTE: "外国语学" -> "外国语学校" is applied via a regex in _apply_fix
    # (negative lookahead) so it only fires when the 校 is genuinely missing.
    # A plain string replace would double-fire on already-correct "外国语学校"
    # (output re-contains the input prefix) and produce "外国语学校校", which
    # then fails valid_school — silently dropping every 外国语-containing school.
]

# 科目/卷类 OCR 错字修正（作用于标题）
SUBJECT_FIX = [
    ("闫语", "英语"), ("关语", "英语"), ("奖语", "英语"), ("趴语", "英语"),
    ("贞卷", "真卷"), ("趴卷", "真卷"), ("上卷", "真卷"),
]

# 匿名符“某”的常见误读（单字）→ 还原为“某”
# 注意：绝不能把“四”映射成“某”——“四”是“四川”的合法字，否则“四川”
# 会被改成“某川”，随后被 CITY_STARTS 规则当作冗余匿名符剥掉，变成“川某师大一中”。
# “四”作为带圈序号④的误读只在最前面出现，统一当作前导噪声剥离（见 LEAD_NOISE/GARBAGE_SINGLE）。
ANON_REPLACE = {
    "茶": "某", "基": "某", "菜": "某", "其": "某", "色": "某",
    "前": "某", "吕": "某",
}
# 标题/校名最前面可能出现的纯噪声字（来自带圈数字或版式杂讯），直接剥离
LEAD_NOISE = set("人知龟和入国图罗钨的僵例因色站")
# 城市/地域开头字：若校名以“某”开头且紧跟这些字，则“某”是冗余匿名符，去掉
CITY_STARTS = set("成四川浙北上海广深重天武西南长哈沈济青苏南福贵昆兰西")


def clean_school_name(s):
    """还原匿名符误读、剥离前缀噪声、折叠冗余‘某’。"""
    s = _apply_fix(s)
    for a, b in ANON_REPLACE.items():
        s = s.replace(a, b)
    s = s.lstrip()
    while s and s[0] in LEAD_NOISE:
        s = s[1:].lstrip()
    if len(s) > 1 and s[0] == "某" and s[1] in CITY_STARTS:
        s = s[1:]
    return s


def clean_title(s):
    """清洗标题：科目/卷类错字修正 + 匿名符还原 + 空白折叠 + 去除尾部 OCR 杂讯。"""
    s = _apply_fix(s)
    for a, b in ANON_REPLACE.items():
        s = s.replace(a, b)
    for a, b in SUBJECT_FIX:
        s = s.replace(a, b)
    s = re.sub(r'\s+', ' ', s).strip()
    # 去除括号里多余的竖线，如 "(二|)" -> "(二)"
    s = re.sub(r'\(([^()]*)\|([^()]*)\)', r'(\1\2)', s)
    # 把漏掉左括号的 "校区)" 补成 "(校区)"
    s = re.sub(r'(?<!\()校区\)', r'(校区)', s)
    # OCR 把"英语"误成"语"：真卷语(一) -> 真卷英语(一)
    s = re.sub(r'真卷语\(', '真卷英语(', s)
    s = re.sub(r'分班语\(', '分班英语(', s)
    # OCR 杂讯页码如 "47) 2026届" / "4) 2025届" -> 去掉页码
    s = re.sub(r'\d+\)\s*(?=\d{4}\s*届|初\s*\d{4})', '', s)
    # 校名与年份之间的 OCR 杂讯（| — - « ~ • · 等）清掉，如 "西川中学 |—-« 2025 届"
    s = re.sub(r'\s*[|—\-«~•·]+\s*(?=\d{4}\s*[届级]|初\s*\d{4})', '', s)
    # "华(校区)" 缺"成" -> "成华(校区)"
    s = re.sub(r'(?<!成)华\s*[\(（]\s*校\s*区\s*[\)）]', '成华（校区）', s)
    # 去除尾部 OCR 杂讯：连续拉丁字母 / © / ® / 竖线 / 间隔号 / 顿号 / 空白
    s = re.sub(r'[\s©®|、．·a-zA-Z]+$', '', s)
    return s

# 正文噪声词：若候选校名里出现这些，直接判为伪标题
REJECT_WORDS = ["年级", "共有", "平均", "同学", "其余", "人数", "每班", "每个学生",
                "男生", "女生", "班级", "学生"]

# 卷头里常见的后续关键词（用于判断两行是否应合并、或一行是否完整）
SUBTITLE_HINTS = ["分班", "真卷", "真题", "新初一", "奖学金", "数学", "语文", "英语"]

# --- 版权/水印/宣传样板文字 ---------------------------------------------------
# 这些字样以斜向水印或页脚的形式印在【每一页】上（"绿色真卷水印为正版图书"、
# "百年树人 品德第一"、"抵制盗版从我做起"…）。OCR 会把它们混进任意一列的文本，
# 于是任何一页正文都凭空带上了 SUBTITLE_HINTS 里的"真卷"二字。
# 后果：正文列被误判成新卷头，一份试卷被劈成两份（用户反馈的"只有两页的 PDF"）。
# 因此在做卷头判定前必须先把这类行整行剔除——它们永远不是标题证据。
# 安全阀：若该行本身带有 初20xx / 20xx届 这种硬年份，说明它其实是真卷头，不剔除。
NOISE_LINE_PAT = re.compile(
    r'水印|正版|盗版|翻印|侵权|版权|必究|印次|印张|出版|书号|定价|'
    r'百年树人|品德第一|我做起|扫码|二维码|微信|公众号|淘宝|客服|'
    r'视频讲解|购买正版|举报')

# 正文句子的形态特征：长、带句末/疑问标点、带设问词。卷头行绝不会长这样。
BODY_PUNCT = re.compile(r'[。？?！!；;]')
BODY_WORDS = ("多少", "几名", "参加", "如果", "已知", "求出", "至少", "分别是",
              "每小题", "共有", "则这", "那么")

# 合理的招生年份区间。OCR 里任何四位数（1200、2580、1250…）都能被旧的宽松
# 年份正则当成"届"，再配上水印里的"真卷"就凭空造出一份新试卷。
YEAR_MIN, YEAR_MAX = 2015, 2039

# 科目：卷名结尾的"真卷X"。旧代码在 _make_title 里把科目写死成"英语"，
# 于是数学合订本里凡是走宽松识别的卷子都被命名成"…真卷英语"。
SUBJECT_WORDS = ["数学", "语文", "英语", "科学", "物理", "化学", "综合"]
_DOC_SUBJECT = None      # 由 run_extract 依据整册 OCR 统计得出


def _plausible_year(y):
    try:
        y = int(y)
    except (TypeError, ValueError):
        return False
    return YEAR_MIN <= y <= YEAR_MAX


def _is_noise_line(line):
    """True for watermark / copyright / promo boilerplate lines."""
    if not NOISE_LINE_PAT.search(line):
        return False
    # 真·卷头绝不会被误杀：带硬年份的行一律保留
    if YEAR_RE.search(line) or YEAR_RE2.search(line):
        return False
    return True


def _strip_noise(lines):
    return [l for l in lines if not _is_noise_line(l)]


def _is_body_line(line, max_len=45):
    """True if the line reads like an exercise sentence rather than a title."""
    s = re.sub(r'\s+', '', line)
    if BODY_PUNCT.search(s):
        return True
    if any(w in s for w in BODY_WORDS):
        return True
    return len(s) > max_len

# 汇编册分卷标记：真卷精编（一）（二）… 用于「按学校分卷」识别落空时的兜底。
# 末尾括号设为可选：OCR 常把卷号后的「）」漏掉（如 "真卷精编(十四"），
# 若强求闭合括号会漏识别整个卷头。
COMPILE_VOL_RE = re.compile(
    r'真卷精编\s*[（(]\s*([一二三四五六七八九十百0-9]+)\s*[)）]?')


def _apply_fix(s):
    for a, b in FIX:
        s = s.replace(a, b)
    # OCR 常把"外国语学校"末尾的"校"漏掉，写成"外国语学"，需补回。
    # 但源文本若已写"外国语学校"，绝不能再加一个"校"（否则变"外国语学校校"）。
    # 负向先行断言 (?!校) 仅在"外国语学"后不是"校"时才补，避免重复触发。
    s = re.sub(r'外国语学(?!校)', '外国语学校', s)
    # OCR 常把"西川中学"的后缀"中学"吞掉，只留下"西川"（如 成都某西川 -> 成都吕西川）。
    # 仅当"西川"后接的不是 中/外国/学 时才补回"中学"，避免误改"西川外国语"等。
    s = re.sub(r'西川(?!中|外国|学)', '西川中学', s)
    return s


def _strip_circled_and_leading(s):
    """Remove circled digits and their common OCR misreads from the start."""
    # Unicode circled digits ①-⑳, ⑴-⒇
    s = re.sub(r'^[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳⑴⑵⑶⑷⑸⑹⑺⑻⑼⑽⑾⑿⒀⒁⒂⒃⒄⒅⒆⒇]+', '', s)
    s = re.sub(r'^[\(\（]\s*\d+\s*[\)\）]\s*', '', s)   # (12) / （12）
    s = re.sub(r'^\d+[\.、\)\）]\s*', '', s)             # 12. / 12、
    # 带圈序号④常被误读成"四"。但"四"也是"四川"的合法首字，绝不能把
    # 地理名里的"四"剥掉。只在"四"后面不是"川"时才当作前导噪声剥离。
    s = re.sub(r'^四(?!川)', '', s)
    # A circled item number mis-OCR'd as a quote + a few latin letters
    # (e.g. '"aa' right before 初20xx届). Harmless to strip globally in
    # Chinese exam headers, which never carry a legitimate quote+letters.
    s = re.sub(r"['\"\u2018\u2019\u201c\u201d]\s*[a-zA-Z]{0,3}\s*", '', s)
    s = s.lstrip()
    # strip known leading-noise words / chars
    changed = True
    while changed:
        changed = False
        for w in LEADING_NOISE_WORDS:
            if s.startswith(w):
                s = s[len(w):].lstrip()
                changed = True
        while s and s[0] in GARBAGE_SINGLE:
            s = s[1:].lstrip()
            changed = True
    return s


def normalize(s):
    s = _apply_fix(s)
    s = _strip_circled_and_leading(s)
    # a stray leading char right before the anonymizer "某" is almost always noise
    while len(s) > 1 and s[0] != '某' and s[1] == '某':
        s = s[1:]
        s = _strip_circled_and_leading(s)
    return s.strip()


def normalize_title(s):
    """Like normalize, but keeps the full subtitle and collapses whitespace."""
    s = _apply_fix(s)
    s = _strip_circled_and_leading(s)
    # collapse internal whitespace/newlines to a single space
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def valid_school(s):
    """Accept a school token if it looks like a real school name, not body text."""
    if not s:
        return False
    if any(w in s for w in REJECT_WORDS):
        return False
    # must end with a proper suffix (standalone "学" is too permissive)
    if not re.search(r'(学校|中学|一中|附中|学院|初中学校)$', s):
        return False
    # Reject quantifier/pronoun false positives such as "一名一中", "这个中学".
    if any(w in s for w in ["一名", "一个", "这个", "那个", "的", "是"]):
        return False
    # 指示代词开头的一定是正文里"这样学校/该中学/每个学校"之类的说法，
    # 没有任何真实校名以它们开头。（正是它把 "…,这样学校先后派出150名学生"
    # 变成了一份凭空多出来的试卷。）
    if s[0] in "这那该此每各":
        return False
    # 以"某"开头的匿名校名通常可信
    if s.startswith('某'):
        return True
    return len(s) >= 4


def _has_year(line):
    return _year_from_line(line) is not None


def _year_from_line(line):
    """First *plausible* enrolment year in the line (see YEAR_MIN/YEAR_MAX).

    We scan every match of every pattern instead of only the first one: a body
    line like "第 1250 棵树 … 2025 届" would otherwise yield 1250 and, worse,
    the loose YEAR_RE3 would happily read "1250 真卷" (the watermark!) as a
    year and fabricate a new exam.
    """
    for r in (YEAR_RE, YEAR_RE2, YEAR_RE3):
        for m in r.finditer(line):
            if _plausible_year(m.group(1)):
                return m.group(1)
    return None


def _has_year_relaxed(line, next_line=""):
    """Relaxed year detection: standard patterns, or a 4-digit year that is
    immediately followed / accompanied by a paper-subtitle hint.
    This catches OCR variants such as '2025 fe ... 一分班' (届 missing)."""
    return _year_from_relaxed(line, next_line) is not None


def _year_from_relaxed(line, next_line=""):
    y = _year_from_line(line)
    if y:
        return y
    ctx = line + "\n" + next_line
    if any(h in ctx for h in ("分班", "入学", "新初一", "真卷")):
        for m in YEAR_CAND.finditer(line):
            if _plausible_year(m.group(1)):
                return m.group(1)
    return None


def _line_has_subtitle(line):
    return any(h in line for h in SUBTITLE_HINTS)


def _is_short_subtitle_line(line, max_len=70):
    """True if line looks like a header continuation, not a body paragraph."""
    return _line_has_subtitle(line) and len(line) <= max_len


def _extract_from_line(line):
    """Try to extract one header candidate from a single (possibly merged) line.

    Returns dict {school, branch, year, title} or None.
    """
    line = normalize_title(line)
    if len(line) < 6:
        return None
    # Must contain a year expression (初20xx or 20xx届) and subtitle hint
    if not _has_year(line):
        return None
    if not _line_has_subtitle(line):
        return None

    # Find the first school match. Because we normalized and the line now starts
    # (after circled-number stripping) with the real school, the first match is
    # usually the longest valid school name.
    m = SCHOOL_RE.search(line)
    if not m:
        return None
    school = clean_school_name(normalize(m.group(1)))
    branch = m.group(3)
    if not valid_school(school):
        return None

    year = _year_from_line(line)
    if not year:
        return None

    # Title = from school start to end of line, cleaned
    title = normalize_title(line[m.start():])
    # remove trailing punctuation / garbage
    title = re.sub(r'[\s。，；:]+$', '', title)
    title = clean_title(title)
    return {"school": school, "branch": branch, "year": year, "title": title}


def _extract_school_only(line):
    """Return a school match from a line that has a school but (maybe) no year."""
    line = normalize_title(line)
    m = SCHOOL_RE.search(line)
    if not m:
        return None
    school = clean_school_name(normalize(m.group(1)))
    if not valid_school(school):
        return None
    return {"school": school, "branch": m.group(3), "match": m, "line": line}


def _cn_num(s):
    """Convert Chinese numerals (一二三…十/十一/二十八) to int. Returns 0 if unclear."""
    d = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7,
         '八': 8, '九': 9, '十': 10, '百': 100, '零': 0}
    s = s.strip()
    if s.isdigit():
        return int(s)
    if not s:
        return 0
    # 十一 -> 11, 二十 -> 20, 二十八 -> 28, 一百零五 -> 105 (best-effort)
    total, cur, has = 0, 0, False
    for ch in s:
        if ch in '一二三四五六七八九':
            cur = d[ch]; has = True
        elif ch == '十':
            total += (cur * 10 if cur else 10); cur = 0; has = True
        elif ch == '百':
            total += (cur * 100 if cur else 100); cur = 0; has = True
        elif ch.isdigit():
            cur = int(ch); has = True
    return total + cur if has else 0


def _fuzzy_school_line(ln):
    """Try to recover a school name from a line polluted by OCR Latin/digit noise.

    Example: "成 都 其 rs} tn 联 L 中 RNY" -> collapse spaces / drop Latin ->
    "成都其联中" which SCHOOL_RE can match.
    """
    # Strip circled-number / leading noise first
    s = _strip_circled_and_leading(ln)
    # Drop Latin letters, digits, common OCR punctuation/braces, then collapse whitespace
    s = re.sub(r"[a-zA-Z0-9\{\}\[\]\(\)\|\\/\-_]+", "", s)
    s = re.sub(r"\s+", "", s)
    if not s:
        return None
    return _extract_school_only(s)


def _school_from_full(full_text):
    """Recover a school name from the FULL-page OCR when a single *column*'s
    OCR lost it entirely.

    Example: column 0 reads '2) Mab JI PS) 2027 届新初一分班真卷闫语' (school
    gone, replaced by Latin garbage), but the full-page OCR still holds
    '成都 吕 西川 ... 2027 ... 分班真卷碳语'.  Dropping Latin/digits and
    collapsing whitespace yields '成都吕西川...', and after ANON(吕->某) +
    西川->西川中学 it resolves to '成都某西川中学'.

    Safety: only the TOP header band (first 10 lines) is scanned, and the match
    must be a *clean* school name (no subtitle/body keywords).  This prevents a
    school name buried in body text from being mis-attributed to a new exam
    when a continuation column happens to carry a stray year/meta signal.
    """
    if not full_text:
        return None
    lines = [l.strip() for l in full_text.splitlines() if l.strip()]
    for ln in lines[:10]:
        s = re.sub(r"[a-zA-Z0-9\{\}\[\]\(\)\|\\/\-_]+", "", ln)
        s = re.sub(r"\s+", "", s)
        if not s:
            continue
        so = _extract_school_only(s)
        if so and valid_school(so["school"]) and \
           not any(h in so["school"] for h in SUBTITLE_HINTS):
            return so
    return None


def _subject_from(blob):
    """Subject of the paper, read from a '真卷X' / '分班X' pattern near the header."""
    b = blob
    for a, c in SUBJECT_FIX:
        b = b.replace(a, c)
    for pre in ("真卷", "分班", "试卷", "考试"):
        for s in SUBJECT_WORDS:
            if pre + s in b:
                return s
    return None


def _make_title(school_line, lines, i, school, branch, year):
    """Build a clean canonical title from the recovered pieces.

    Rather than pasting the noisy raw OCR line, we reconstruct a predictable
    filename:  {school}{branch}初{year}届新初一{分班/入学}真卷{科目}.
    This protects filenames from Latin/OCR body junk that often follows a
    garbled school line (e.g. p68 石室联合中学).

    The subject used to be hard-coded as 英语, so every relaxed-detected paper
    in a 数学 book came out named "…真卷英语". We now read it from the header
    context and fall back to the subject of the book as a whole.
    """
    if not school:
        school = "某校"
    blob = "\n".join(lines[max(0, i - 1):i + 5])
    subject = _subject_from(blob) or _DOC_SUBJECT or "英语"
    if "奖学金" in blob:
        sub = "分班（奖学金）"
    elif "分班" in blob:
        sub = "分班"
    elif "入学" in blob:
        sub = "入学"
    elif "真卷" in blob:
        sub = "分班"
    else:
        sub = "分班"
    title = school
    if branch:
        title += f"（{branch}）"
    if year:
        title += f"初{year}届新初一{sub}真卷{subject}"
    else:
        title += f"新初一{sub}真卷{subject}"
    return clean_title(title)


# A paper header always sits at the very top of its column (each column *is*
# one physical exam page). Allow a little slack for OCR junk lines above it.
TOP_BAND = 10


def _detect_relaxed_header(txt, recent_school=None, recent_year=None, page=None,
                            full_text=None):
    """Catch malformed paper-start headers that `extract_text` misses.

    `extract_text` requires a full (school + year + subtitle) header in ONE
    line.  In the wild a new exam page is often OCR'd with only a partial
    header, so we fall back to signals that a *body* column does not carry:

      * a plausible year expression (初20xx级 / 20xx届) in the column's TOP
        BAND, on a title-like line                       -> STRONG (new paper)
      * a school name on a title-like line in the top band whose immediate
        neighbourhood carries a subtitle or 满分-时间 meta -> MEDIUM

    Three guards make this safe — every one of them exists because its absence
    produced a real false split (one exam sliced into two PDFs):

      1. Watermark / copyright boilerplate is stripped first.  "绿色真卷水印为
         正版图书" is printed on *every* page and put the word 真卷 into every
         column, which used to satisfy the subtitle test all by itself.
      2. The corroborating signal must sit NEXT TO the school line, not merely
         somewhere in the column — a page-footer watermark is ~8 lines below
         the last body line and must not vouch for it.
      3. The school must come from a title-like line.  A school name inside a
         running sentence ("…,这样学校先后派出 150 名学生参加劳动,…") is body
         text, never a header.
    """
    lines = _strip_noise([l.strip() for l in txt.splitlines() if l.strip()])
    if not lines:
        return None

    def _school_on(i):
        """School from line i, but only if that line looks like a title."""
        ln = lines[i]
        if _is_body_line(ln):
            return None
        return _fuzzy_school_line(ln) or _extract_school_only(ln)

    # STRONG: a plausible year expression in the header band.
    year_any = None
    year_line_idx = -1
    for i, ln in enumerate(lines[:TOP_BAND]):
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        y = _year_from_relaxed(ln, nxt)
        # A year inside a word problem ("2025 届毕业生共有 480 人…") is body,
        # not a header; the line shape tells them apart.
        if y and not _is_body_line(ln, max_len=60):
            year_any = y
            year_line_idx = i
            break
    if year_any:
        # school if readable in the top region, else 某校 (force a new paper)
        so, so_line_idx = None, -1
        for i in range(min(TOP_BAND, len(lines))):
            so = _school_on(i)
            if so:
                so_line_idx = i
                break
        # Fallback: a single column's OCR sometimes loses the school name
        # entirely (Latin garbage), while the full-page OCR still keeps it.
        if so is None and full_text:
            so = _school_from_full(full_text)
            if so:
                so_line_idx = 0
        school = so["school"] if so else "某校"
        branch = so["branch"] if so else None
        anchor = max(so_line_idx, year_line_idx) if so_line_idx >= 0 else year_line_idx
        title = _make_title(lines[anchor] if 0 <= anchor < len(lines) else lines[0],
                            lines, anchor, school, branch, year_any)
        return {"school": school, "branch": branch, "year": year_any, "title": title}

    # MEDIUM: a school name on a title-like line whose NEIGHBOURHOOD carries a
    # subtitle (分班/真卷/新初一…) or a 满分-时间 meta line.
    for i in range(min(TOP_BAND, len(lines))):
        so = _school_on(i)
        if not so:
            continue
        near = lines[max(0, i - 1):i + 5]
        blob = "\n".join(near)
        has_sub = any(_line_has_subtitle(x) for x in near)
        has_meta = bool(re.search(r'满分\s*[：:]\s*\d+', blob) and
                        re.search(r'时[间加]\s*[：:]\s*\d+|\d+\s*分钟', blob))
        if not (has_sub or has_meta):
            continue
        school, branch = so["school"], so["branch"]
        # try to recover a year from nearby lines for the title/key
        yr = None
        for j in range(i, min(i + 5, len(lines))):
            nxt = lines[j + 1] if j + 1 < len(lines) else ""
            y = _year_from_relaxed(lines[j], nxt)
            if y:
                yr = y
                break
        if yr is None:
            for ln2 in lines[:TOP_BAND]:
                y = _year_from_line(ln2)
                if y:
                    yr = y
                    break
        title = _make_title(lines[i], lines, i, school, branch, yr)
        return {"school": school, "branch": branch,
                "year": yr or recent_year, "title": title}

    return None


def _extract_compile(col_texts, full_text):
    """Fallback for compilation-style headers: 真卷精编（一）（二）…

    Triggered only when no school-based header is found. Returns a candidate
    dict (with a unique _key so repeated 序号 like (一) never merge across a
    reset) or None.

    The header on these scans is split across columns: the compilation name
    (e.g. "公立名校初一新生入学(") usually sits as the FIRST line of one column
    while "分班)考试真卷精编(一)" sits in another. We take the volume from the
    真卷精编 line and the name from any column's first line that looks like a
    title (contains 名校/入学/考试/新生 …), never from arbitrary body text.
    """
    cols = list(col_texts)
    if full_text:
        cols = cols + [full_text]
    # Answer / solution pages also carry a 真卷精编（N） line; we must NOT
    # treat them as a new exam start. Use the explicit "参考答案" header only —
    # bare "答案" also appears inside exam questions ("写出答案") and would
    # cause false positives.
    ANSWER_PAT = re.compile(r'参考\s*答案')
    full_blob = "\n".join(cols)
    if ANSWER_PAT.search(full_blob):
        return None
    # locate the 真卷精编 marker (first column that has one)
    target_col = None
    target_vol = None
    for col in cols:
        m = COMPILE_VOL_RE.search(col)
        if m:
            target_col = col
            target_vol = m.group(1)
            break
    if not target_col:
        return None
    # name prefix = any top line of ANY column/full-page that looks like a
    # compilation title. The full-page top-band OCR often carries the complete
    # title, which can be >24 chars; individual columns may only hold fragments.
    # Note: "考试" alone matches the volume line (e.g. "分班)考试真卷精编(N)"),
    # so it is deliberately excluded; the real prefix always carries 名校/入学/新生/初一.
    NAME_PAT = re.compile(r'(名校|公立|私立|入学|人学|新生|初一)')
    Q_PAT = re.compile(r'^[一二三四五六七八九十]、|^第\s*\d+\s*题|答案|解析|选择题|填空题')
    PREFIX_MAX_LEN = 48

    def _pick_prefix(col):
        """Return the best compilation-title prefix line(s) from one text blob."""
        lines = [l.strip() for l in col.splitlines() if l.strip()]
        # The title lives at the very top; only consider the first few lines.
        # Try merging two consecutive top lines first (OCR sometimes splits the
        # header across physical lines), then fall back to a single line.
        for a, b in zip(lines[:3], lines[1:4]):
            merged = a + b
            if (NAME_PAT.search(a) and NAME_PAT.search(b)
                    and not Q_PAT.search(a) and not Q_PAT.search(b)
                    and len(merged) <= PREFIX_MAX_LEN):
                merged = re.sub(r'[（(][^）)]*$', '', merged)
                merged = re.sub(r'[^\u4e00-\u9fff]+$', '', merged)
                return _apply_fix(merged)
        for ln in lines[:3]:
            if NAME_PAT.search(ln) and not Q_PAT.search(ln) and len(ln) <= PREFIX_MAX_LEN:
                ln = re.sub(r'[（(][^）)]*$', '', ln)
                ln = re.sub(r'[^\u4e00-\u9fff]+$', '', ln)
                return _apply_fix(ln)
        return ""

    prefix = ""
    for col in cols:
        prefix = _pick_prefix(col)
        if prefix:
            break
    # Build the title. Guard against two duplication cases:
    #  - a column's first line may already BE the full header (name+volume);
    #    if so, use it directly and don't append main.
    #  - when there is no name prefix, the title is just 真卷精编（N）; do NOT
    #    prepend another 真卷精编 (main already contains it).
    if prefix and '真卷精编' in prefix:
        title = re.sub(r'\s+', '', prefix).replace('（', '(').replace('）', ')')
    else:
        main = "(分班)考试真卷精编(" + target_vol + ")"
        title = (prefix + main) if prefix else ("真卷精编(" + target_vol + ")")
        title = re.sub(r'\s+', '', title).replace('（', '(').replace('）', ')')
    # cosmetic: collapse accidental double opening parens and ensure a trailing
    # closing paren after the volume number (OCR sometimes drops it)
    title = title.replace('((', '(')
    if re.search(r'真卷精编\([一二三四五六七八九十百0-9]+$', title):
        title += ')'
    return {"school": prefix or "真卷精编汇编", "branch": None, "year": target_vol,
            "title": title, "_compile": True}


def extract_text(txt):
    """Return a list of unique header candidates from a column/full-page OCR text."""
    raw_lines = [l.strip() for l in txt.splitlines()]
    lines = [l for l in raw_lines if len(l) >= 4]
    cands = []
    i = 0
    while i < len(lines):
        line = lines[i]
        c = _extract_from_line(line)
        if c:
            # If the next line(s) look like a continuation of the subtitle
            # (no school, has subtitle hints), merge them into the title.
            # Use a running merge so a school+year line can still absorb a
            # trailing subject line such as "真卷英语".
            j = i + 1
            merged = line
            while j < len(lines):
                nxt = lines[j]
                if _extract_school_only(nxt):
                    break
                if _is_short_subtitle_line(nxt):
                    merged = merged + nxt
                    mc = _extract_from_line(merged)
                    if mc:
                        c = mc
                        i = j
                        j += 1
                        continue
                break
            cands.append(c)
            i += 1
            continue

        # Two-line header: first line school, second line year+subtitle
        so = _extract_school_only(line)
        if so and i + 1 < len(lines) and _is_short_subtitle_line(lines[i + 1]):
            merged = line + lines[i + 1]
            c = _extract_from_line(merged)
            if c:
                # Absorb any following subtitle-only lines (e.g. a trailing
                # "真卷英语") just like the single-line branch above.
                j = i + 2
                running = merged
                while j < len(lines):
                    nxt = lines[j]
                    if _extract_school_only(nxt):
                        break
                    if _is_short_subtitle_line(nxt):
                        running = running + nxt
                        mc = _extract_from_line(running)
                        if mc:
                            c = mc
                            i = j
                            j += 1
                            continue
                    break
                cands.append(c)
                i = i + 2 if j == i + 2 else i + 1
                continue
        i += 1

    # Deduplicate while preserving order
    seen, uniq = set(), []
    for c in cands:
        key = (c["school"], c["branch"], c["year"], c["title"])
        if key not in seen:
            seen.add(key)
            uniq.append(c)
    return uniq


def _build_subpages(workdir, correct):
    """Expand every source page into its columns (left -> right) and decide,
    for each *column*, which exam it belongs to.

    Why column-level (not page-level): a 2-up spread packs two exam pages side
    by side. The old page-level logic assigned the WHOLE source page to each
    exam that touched it, so a cover shared by two schools bled one exam into
    the other's PDF. Here each column is its own subpage; a subpage's `key` is
    the exam header detected *in that column* (first candidate only, so a title
    line mis-OCR'd twice stays one exam), or None to continue the current exam.

    A page whose columns collectively yield >3 distinct headers is a 目录/TOC
    page — skipped entirely so it doesn't spawn bogus 1-page "exams".
    """
    strips = json.load(open(os.path.join(workdir, "strips.json"), encoding="utf-8"))
    layout = load_layout(os.path.join(workdir, "layout_class.json"))
    pages = sorted(int(k) for k in strips)

    CORRECT = {}
    for k, v in (correct or {}).items():
        p = int(k)
        school, branch = v[0], (v[1] if len(v) > 1 else None)
        year = v[2] if len(v) > 2 else None
        title = v[3] if len(v) > 3 else None
        if title is None:
            title = school + (f"({branch})" if branch else "") + (f"初{year}级新初一分班真卷数学" if year else "")
        CORRECT[p] = {"school": school, "branch": branch, "year": year,
                      "title": title, "key": (school, branch, year, title)}

    subpages = []
    for p in pages:
        rec = strips[str(p)]
        cols = rec.get("col", []) if isinstance(rec, dict) else [rec]
        full = rec.get("full", "") if isinstance(rec, dict) else ""
        lay = layout.get(p, "2-up")
        ncols = 1 if lay == "1-up" else (3 if lay == "3-up" else 2)

        col_keys, col_titles, col_meta = [], [], []
        all_keys = set()
        page_keys_all = set()   # ALL candidate keys on the page (for TOC detection)
        page_has_answer = False
        # Most recent reliably-detected school/year for relaxed fallback.
        # Updated whenever a column yields a non-None key with a real school.
        recent_school, recent_year = None, None
        for c in range(ncols):
            txt = cols[c] if c < len(cols) else ""
            hs = extract_text(txt)
            for h in hs:
                page_keys_all.add((h["school"], h["branch"], h["year"], h["title"]))
            key, meta = None, None
            if hs:
                h = hs[0]                      # first candidate → ignore duplicate mis-OCR
                key = (h["school"], h["branch"], h["year"], h["title"])
                meta = h
            else:
                # Malformed partial-header fallback (school-only, year+subtitle,
                # or 满分/时间 meta).  This catches new-exam boundaries that
                # OCR destroyed but still leave enough title signals.
                rh = _detect_relaxed_header(txt, recent_school=recent_school,
                                            recent_year=recent_year, page=p,
                                            full_text=full)
                if rh:
                    key = (rh["school"], rh["branch"], rh["year"], rh["title"])
                    meta = rh
                    page_keys_all.add(key)
                else:
                    # 真卷精编 兜底：仅用本列文本，避免误用邻列/整页标题
                    cc = _extract_compile([txt] if txt else [], txt)
                    if cc:
                        key = cc.get("_key") or ("__compile__", cc["title"])
                        meta = cc
                        page_keys_all.add(key)
            col_keys.append(key)
            col_titles.append(meta["title"] if meta else None)
            col_meta.append(meta)
            if key:
                all_keys.add(key)
                if meta and meta.get("school") and meta["school"] != "某校":
                    recent_school = meta["school"]
                if meta and meta.get("year"):
                    recent_year = meta["year"]
            if re.search(r'参考\s*答案', (txt or "") + "\n" + full):
                page_has_answer = True

        # A 目录/TOC page lists many schools on one full-width (1-up) page.
        # Skip it so it doesn't spawn bogus 1-page "exams". 2-up/3-up pages
        # can legitimately show up to ncols covers, so don't treat them as TOC.
        is_toc = (lay == "1-up") and len(page_keys_all) > 3
        if p in CORRECT:
            ov = CORRECT[p]
            for c in range(ncols):
                col_keys[c] = ov["key"]
                col_titles[c] = ov["title"]
                col_meta[c] = {"school": ov["school"], "branch": ov["branch"],
                               "year": ov["year"], "title": ov["title"]}
            is_toc = False
            page_has_answer = False

        for c in range(ncols):
            sp = {"page": p, "col": c, "key": col_keys[c], "title": col_titles[c],
                  "is_answer": page_has_answer, "is_toc": is_toc}
            m = col_meta[c]
            sp["school"] = m["school"] if m else None
            sp["branch"] = m["branch"] if m else None
            sp["year"] = m["year"] if m else None
            subpages.append(sp)
    return subpages


def _verify_long_exams(exams, workdir):
    """Transparency report for the user's rule: 'a PDF > 4 pages very likely
    contains > 1 exam'.

    For any exam whose *source-page* span exceeds MAX_PAGE_SPAN we re-scan its
    INTERIOR columns for a paper-start header.  Because `run_extract` already
    splits exams exactly at detected column-level headers, a long span means
    the interior columns carried no header -> a genuine single (long) exam.
    This function documents that, so a >4-page PDF is shown to be verified-clean
    rather than a silent merge.  It is non-mutating (the first pass already did
    the splitting); it only reports.
    """
    MAX_PAGE_SPAN = 4
    try:
        strips = json.load(open(os.path.join(workdir, "strips.json"), encoding="utf-8"))
    except Exception:
        return
    print(f"\n--- long-exam verification (output PDF pages > {MAX_PAGE_SPAN}) ---")
    any_long = False
    for e in exams:
        segs = e["segments"]
        # The user's rule is about the OUTPUT PDF page count, which equals the
        # number of source columns (each 2-up page contributes 2 output pages).
        npages = len(segs)
        if npages <= MAX_PAGE_SPAN:
            continue
        any_long = True
        interior = segs[1:-1]
        hidden = []
        for (p, c) in interior:
            rec = strips.get(str(p))
            if not rec:
                continue
            cols = rec.get("col", []) if isinstance(rec, dict) else [rec]
            txt = cols[c] if c < len(cols) else ""
            if not txt:
                continue
            rh = _detect_relaxed_header(txt)
            if rh:
                hidden.append((p, c, rh["title"]))
        if hidden:
            print(f"  ⚠ {e['title']} (src p{segs[0][0]}-{segs[-1][0]}, {npages} out-pages): "
                  f"HIDDEN interior header(s) -> {hidden}")
        else:
            print(f"  ✓ {e['title']} (src p{segs[0][0]}-{segs[-1][0]}, {npages} out-pages): "
                  f"no interior header -> verified single exam")
    if not any_long:
        print("  (none — every exam is <= 4 output pages)")


def _infer_doc_subject(workdir):
    """Best-guess the subject of the whole book from the OCR corpus.

    Stage-3 relaxed headers often lose the subject word (a header may read only
    '成都某外国语学校 初2025级 新初一分班（奖学金）' with no '数学'), so the
    per-title subject probe returns None.  Falling back to a hard-coded '英语'
    mis-named every paper in a 数学 book.  Instead we fall back to the subject
    that appears most often across the entire book — which, for these exam
    collections, is always a single subject per volume.
    """
    try:
        strips = json.load(open(os.path.join(workdir, "strips.json"), encoding="utf-8"))
    except Exception:
        return None
    cnt = {}
    for k, rec in strips.items():
        texts = []
        if isinstance(rec, dict):
            texts = list(rec.get("col", [])) + [rec.get("full", "")]
        for t in texts:
            if not t:
                continue
            for s in SUBJECT_WORDS:
                if s in t:
                    cnt[s] = cnt.get(s, 0) + 1
    if cnt:
        return max(cnt, key=cnt.get)
    return None


def run_extract(workdir, correct=None):
    global _DOC_SUBJECT
    _DOC_SUBJECT = _infer_doc_subject(workdir)
    if _DOC_SUBJECT:
        print(f"[extract] 整册科目推断 = {_DOC_SUBJECT}")
    subpages = _build_subpages(workdir, correct)

    # NOTE: we deliberately do NOT smooth here.  The previous smoothing step
    # merged short new-exam starts (only one column wide) into their neighbours,
    # causing multiple real papers to land in one PDF.  Body pages never carry
    # school names / year expressions / 满分-时间 lines, so a relaxed header
    # hit in the middle of a run is almost always a genuine new paper.

    exams = []
    cur = None
    cur_key = None
    for sp in subpages:
        if sp["is_toc"] or sp["is_answer"]:
            if cur:
                exams.append(cur)
                cur, cur_key = None, None
            continue
        if sp["key"] is None:
            if cur:
                cur["segments"].append((sp["page"], sp["col"]))
            continue
        if cur is None or cur_key != sp["key"]:
            if cur:
                exams.append(cur)
            cur = {"school": sp["school"], "branch": sp["branch"], "year": sp["year"],
                   "title": sp["title"], "segments": [(sp["page"], sp["col"])]}
            cur_key = sp["key"]
        else:
            cur["segments"].append((sp["page"], sp["col"]))
            if len(sp["title"] or "") > len(cur["title"] or ""):
                cur["title"] = sp["title"]
    if cur:
        exams.append(cur)

    _verify_long_exams(exams, workdir)

    print(f"\n=== TOTAL EXAMS: {len(exams)} ===")
    print(f"{'#':>2}  start end  npg title")
    for i, e in enumerate(exams, 1):
        segs = e["segments"]
        print(f"{i:>2}  {segs[0][0]:>3} {segs[-1][0]:>3}  {len(segs):>2}  {e['title']}")

    out = os.path.join(workdir, "exam_sets.json")
    json.dump(
        [{"start": e["segments"][0][0], "end": e["segments"][-1][0],
          "school": e["school"], "branch": e["branch"], "year": e["year"],
          "title": e["title"], "segments": e["segments"]} for e in exams],
        open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\nsaved {out}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--correct", default=None,
                    help="JSON file: {page: [school, branch, year, title?]} manual "
                         "override for irrecoverably garbled title pages")
    a = ap.parse_args()
    correct = json.load(open(a.correct, encoding="utf-8")) if a.correct else None
    run_extract(a.workdir, correct=correct)


if __name__ == "__main__":
    main()
