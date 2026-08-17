#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
correct_titles.py —— 用 LLM 校正试卷拆分后文件名里的 OCR 噪声。

流程：
  1. 读取 LLM 配置（来自 StudyNoteBook 的 data/app.sqlite settings 表）。
  2. 读取 _manifest.json 里的标题（即实际文件名）作为待校正项。
  3. 把全部标题 + OCR 纠错规则发给 DeepSeek（chat/completions）。
  4. 解析返回的 orig->fixed 映射。
  5. 默认只打印映射并写出 _title_corrections.json（不改动文件）；
     加 --apply 才真正：重命名 PDF + 更新 _manifest.json + 更新 _work/exam_sets.json。

用法：
  python correct_titles.py --manifest "英语拆分版/_manifest.json" --pdfdir "英语拆分版"
  python correct_titles.py --manifest "英语拆分版/_manifest.json" --pdfdir "英语拆分版" --apply
"""
import sys, os, re, json, argparse, sqlite3, urllib.request, urllib.error

DEFAULT_DB = r"C:\Github\StudyNoteBook\data\app.sqlite"

SYSTEM_PROMPT = """你是一名中文 OCR 纠错专家，专门校正「名校初一新生入学/分班考试真卷」的标题。
这些标题来自成都/四川的知名中学（分班考、入学考英语卷）。标题结构通常为：
  {学校}初{YYYY}届新初一入学真卷英语
  {学校}初{YYYY}届新初一分班(奖学金)真卷英语
  {学校}({校区})初{YYYY}届新初一入学真卷英语
其中学校名里的「某」是原文故意用的匿名占位符，必须保留。

OCR 常见错误与纠正规则：
1. 科目词固定为「真卷英语」。把以下 OCR 变体统一改回「真卷英语」：
   - 卷名：趴卷 / 上卷 / 贞卷 / 奖卷 / 关卷 / 某卷 -> 真卷
   - 科目：奖语 / 闫语 / 关语 / 关 / 某语 -> 英语
2. 学校名里若出现与列表中其他条目相同的学校但被 OCR 读错，请依据列表里出现的规范写法改正（利用交叉一致性）。例如「茶盐祥」应改为「某嘉祥」（对应列表中的「成都某嘉祥外国语学校」）；「四川菜师大一中」「色四川某师大一中」「人知四川某师大一中」应改为「四川某师大一中」；「四成都某西川中学」去掉前缀「四」改为「成都某西川中学」；「成都其七中育才学校」改为「成都某七中育才学校」。
3. 校区/括号里的 OCR 纠错：龙录师一 -> 龙泉师一；奖池金 -> 奖学金。
4. 删除明显是版式装饰被误识别的杂符号：单独的「47)」「74)」「4)」「_—-«4)」「BERG」「©」；以及「IeA)—」应还原为「届新初一入学真」；「(二_)a」改为「(二)」。
5. 输出标题不要包含多余空格；「初」与年份之间不要空格。
6. 只改动明显的 OCR 错误，不要臆造学校名；若某标题已基本正确则原样返回。

请对下面每一条都给出纠正结果，输出严格的 JSON 数组，每项：
{"orig":"<原始标题>","fixed":"<纠正后标题>","conf":"high|medium|low"}
不要输出任何解释文字，只输出 JSON 数组。"""


def get_llm_config(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT key, value FROM settings WHERE key IN "
        "('llm_enabled','llm_provider','llm_base_url','llm_api_key','llm_model')"
    ).fetchall()
    conn.close()
    raw = {r["key"]: r["value"] for r in rows}
    return {
        "base_url": raw.get("llm_base_url") or "https://api.deepseek.com/v1",
        "api_key": raw.get("llm_api_key", ""),
        "model": raw.get("llm_model", "deepseek-chat"),
        "enabled": raw.get("llm_enabled", "false") == "true",
    }


def _extract_json(text):
    """从文本里抽取第一个 JSON 数组。支持 ``` 围栏与零散前后文。"""
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[-1] if "\n" in s else s[3:]
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
    # 直接解析
    try:
        obj = json.loads(s)
        if isinstance(obj, list):
            return obj
    except Exception:
        pass
    # 找第一个 [ ... ] 完整子串
    start = s.find("[")
    if start >= 0:
        depth = 0
        for i in range(start, len(s)):
            if s[i] == "[":
                depth += 1
            elif s[i] == "]":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(s[start:i + 1])
                    except Exception:
                        break
    return None


def call_llm_batch(cfg, titles):
    """对一批标题调用 LLM，返回解析后的映射列表（每项含 orig/fixed/conf）。
    兼容推理模型：content 为空时回退到 reasoning_content 抽取 JSON。"""
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(titles))
    payload = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": numbered},
        ],
        "temperature": 0.1,
        "max_tokens": 8000,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg['api_key']}",
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    msg = body["choices"][0]["message"]
    content = (msg.get("content") or "").strip()
    rc = msg.get("reasoning_content") or ""
    parsed = _extract_json(content) or _extract_json(rc)
    if not parsed:
        raise RuntimeError(
            f"LLM 未返回可解析的 JSON。content 长度={len(content)}，"
            f"reasoning 长度={len(rc)}"
        )
    return parsed


def call_llm(cfg, titles, batch=6):
    """全量标题分批评识，合并结果。"""
    out = []
    for i in range(0, len(titles), batch):
        chunk = titles[i:i + batch]
        print(f"  LLM 批次 {i // batch + 1}: 处理 {len(chunk)} 条 "
              f"({chunk[0][:18]}…)")
        out.extend(call_llm_batch(cfg, chunk))
    return out


ILLEGAL = re.compile(r'[<>:"/\\|?*]')

def normalize_title(fixed):
    """轻度归一化：保证「初」在年份前（OCR 常把「初」误并入噪声块一起丢掉）。"""
    fixed = re.sub(r'(?<!初)(\d{4})届', r'初\1届', fixed)
    return fixed

def parse_school(fixed):
    """从纠正后的标题里尽量解析 school/branch/year，供回写 JSON。"""
    school, branch, year = fixed, None, None
    m = re.match(r"^(.*?)(?:[(（](.*?)[)）])?\s*初\s*(\d{4})\s*届", fixed)
    if m:
        school = m.group(1).strip()
        branch = m.group(2).strip() if m.group(2) else None
        year = m.group(3)
    return school, branch, year


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True, help="_manifest.json 路径")
    ap.add_argument("--pdfdir", required=True, help="PDF 所在目录（与 manifest 同级）")
    ap.add_argument("--db", default=DEFAULT_DB, help="含 LLM 配置的 sqlite 路径")
    ap.add_argument("--model", default=None, help="覆盖 LLM 模型（默认用 DB 配置；推理模型可改 deepseek-chat）")
    ap.add_argument("--apply", action="store_true", help="真正重命名并写回 JSON")
    args = ap.parse_args()

    manifest_path = args.manifest
    workdir = os.path.dirname(manifest_path)
    examsets_path = os.path.join(workdir, "exam_sets.json") if os.path.basename(workdir) == "_work" \
        else os.path.join(workdir, "_work", "exam_sets.json")

    manifest = json.load(open(manifest_path, encoding="utf-8"))
    titles = [e["name"] for e in manifest]

    cfg = get_llm_config(args.db)
    if args.model:
        cfg["model"] = args.model
    if not cfg["api_key"]:
        print("ERROR: 未从数据库读到 llm_api_key，无法调用 LLM。", file=sys.stderr)
        sys.exit(1)
    print(f"调用 LLM: model={cfg['model']} base_url={cfg['base_url']}")
    mapping = call_llm(cfg, titles)

    # 建索引：orig -> fixed/conf
    by_orig = {m["orig"]: m for m in mapping}
    corrections = []
    for t in titles:
        if t in by_orig:
            raw_fixed = by_orig[t].get("fixed", t)
            corrections.append((t, normalize_title(raw_fixed), by_orig[t].get("conf", "?")))
        else:
            corrections.append((t, normalize_title(t), "unchanged(missing)"))

    # 写出映射文件（始终写）
    corr_path = os.path.join(args.pdfdir, "_title_corrections.json")
    json.dump(
        [{"orig": o, "fixed": f, "conf": c} for o, f, c in corrections],
        open(corr_path, "w", encoding="utf-8"),
        ensure_ascii=False, indent=2,
    )

    print("\n=== 校正映射（orig -> fixed [conf]）===")
    for o, f, c in corrections:
        flag = "" if o == f else "  <-- 改"
        print(f"  [{c}] {o}\n        -> {f}{flag}")

    if not args.apply:
        print(f"\n（预览模式）映射已写入 {corr_path}，未改动任何文件。加 --apply 执行重命名。")
        return

    # ---- 实际执行 ----
    # 1) 重命名 PDF + 更新 manifest
    used = {}
    for e in manifest:
        orig = e["name"]
        fixed = dict((o, f) for o, f, c in corrections).get(orig, orig)
        fixed = ILLEGAL.sub("_", fixed).strip()
        if not fixed:
            fixed = orig
        # 同名去重
        if fixed in used:
            used[fixed] += 1
            fixed = f"{fixed}({used[fixed]})"
        else:
            used[fixed] = 1
        old_path = os.path.join(args.pdfdir, e["file"])
        new_path = os.path.join(args.pdfdir, fixed + ".pdf")
        if old_path != new_path and os.path.exists(old_path):
            os.rename(old_path, new_path)
        e["name"] = fixed
        e["file"] = fixed + ".pdf"
        school, branch, year = parse_school(fixed)
        e["school"] = school
        e["branch"] = branch
        e["year"] = year

    json.dump(manifest, open(manifest_path, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    # 2) 更新 exam_sets.json（title + school/branch/year）
    # 优先按索引对齐（exam_sets 与 manifest 同序同数，最稳）；
    # 条数不一致时回退到 title 匹配（兼容旧格式）。
    if os.path.exists(examsets_path):
        es = json.load(open(examsets_path, encoding="utf-8"))
        if len(es) == len(manifest):
            for m, item in zip(manifest, es):
                item["title"] = m["name"]
                item["school"] = m["school"]
                item["branch"] = m.get("branch")
                item["year"] = m.get("year")
        else:
            fix_by_title = {o: f for o, f, c in corrections}
            for i, item in enumerate(es):
                t = item.get("title", "")
                norm = re.sub(r"\s+", "", t)
                fixed = ILLEGAL.sub("_", fix_by_title.get(norm, norm)).strip()
                item["title"] = fixed
                school, branch, year = parse_school(fixed)
                item["school"] = school
                item["branch"] = branch
                item["year"] = year
        json.dump(es, open(examsets_path, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)

    print(f"\n已应用：重命名 PDF + 更新 {manifest_path} 与 {examsets_path}")


if __name__ == "__main__":
    main()
