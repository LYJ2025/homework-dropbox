#!/usr/bin/env python3
# 作业投件箱：仅上传 / 盲传 / 免账号 / 不可改删
# UI: 液态玻璃 (Liquid Glass) 风格
# 文件夹: uploads/ 根目录 = "未分类"(默认), 子目录 = 自定义文件夹
# 删除文件夹时, 其中所有文件一并删除(前端 confirm 明确提示)
import os, io, re, time, shutil, secrets, zipfile, html, json
from urllib.parse import quote
from flask import Flask, request, send_file, abort, Response, redirect

BASE = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE, "uploads")
TOKEN_FILE = os.path.join(BASE, ".admin_token")
ROSTER_FILE = os.path.join(BASE, ".roster")
ANNOUNCE_FILE = os.path.join(BASE, ".announcement")
CONFIG_FILE = os.path.join(BASE, ".upload_config")
os.makedirs(UPLOAD_DIR, exist_ok=True)

if not os.path.exists(TOKEN_FILE):
    with open(TOKEN_FILE, "w") as f:
        f.write(secrets.token_urlsafe(24))
ADMIN_TOKEN = open(TOKEN_FILE).read().strip()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = None  # 服务端不限制大小

def split_names(txt: str):
    """名单文本按行解析为姓名列表(兼容旧格式, 仅取姓名)。
    每行格式: 「姓名」或「姓名 学号」或「学号 姓名」, 学号自动剔除。"""
    return [name for name, _ in parse_roster(txt)]

def parse_roster(txt: str):
    """名单文本按行解析为 [(姓名, 学号), ...]; 学号可为 ''(未提供)。
    每行: 姓名与学号用空格/制表符/逗号分隔, 顺序不限; 姓名去重保序。"""
    out, seen = [], set()
    for line in (txt or "").splitlines():
        line = line.strip()
        if not line:
            continue
        toks = re.split(r"[\s,，;；\t]+", line)
        digits = [t for t in toks if t.isdigit()]
        name = "".join(t for t in toks if t and not t.isdigit())
        sid = digits[0] if digits else ""
        if name and name not in seen:
            seen.add(name)
            out.append((name, sid))
    return out

def load_roster_pairs():
    """应交名单(姓名, 学号)配对; 学号为空表示该行未提供学号"""
    try:
        txt = open(ROSTER_FILE).read()
    except OSError:
        return []
    return parse_roster(txt)

def load_roster():
    """应交名单: 仅姓名(去重保序)"""
    return [n for n, _ in load_roster_pairs()]

# ---------- 公告栏 ----------
def load_announcement():
    """读取公告 {'text', 'time'}; 无公告返回 None"""
    try:
        data = json.load(open(ANNOUNCE_FILE))
        if isinstance(data, dict) and str(data.get("text", "")).strip():
            return {"text": str(data["text"])[:2000],
                    "time": str(data.get("time", ""))[:20]}
    except (OSError, ValueError):
        pass
    return None

def save_announcement(text: str):
    """发布公告; 内容为空 = 撤下公告"""
    text = (text or "").strip()[:2000]
    if not text:
        try:
            os.remove(ANNOUNCE_FILE)
        except OSError:
            pass
        return
    with open(ANNOUNCE_FILE, "w") as f:
        json.dump({"text": text,
                   "time": time.strftime("%Y-%m-%d %H:%M")}, f,
                  ensure_ascii=False)

# ---------- 上传配置(开关 + 命名模板 + 客户端上限) ----------
def _clamp(v, lo, hi, default):
    """把配置值限制在 [lo, hi]; 非法回退默认"""
    try:
        v = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))

DEFAULT_CONFIG = {"recognize": True, "normalize": True,
                  "template": "学号 姓名",
                  "max_mb": 200, "max_files": 20}
TPL_KEYWORDS = ("姓名", "学号", "文件名称", "日期")

def load_config():
    """读取上传配置; 文件缺失/损坏时回退默认值"""
    cfg = dict(DEFAULT_CONFIG)
    try:
        data = json.load(open(CONFIG_FILE))
        if isinstance(data, dict):
            if "recognize" in data:
                cfg["recognize"] = bool(data["recognize"])
            if "normalize" in data:
                cfg["normalize"] = bool(data["normalize"])
            if isinstance(data.get("template"), str) and data["template"].strip():
                cfg["template"] = data["template"]
            if "max_mb" in data:
                cfg["max_mb"] = _clamp(data["max_mb"], 10, 5000, cfg["max_mb"])
            if "max_files" in data:
                cfg["max_files"] = _clamp(data["max_files"], 1, 999, cfg["max_files"])
    except (OSError, ValueError):
        pass
    return cfg

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump({"recognize": bool(cfg.get("recognize")),
                   "normalize": bool(cfg.get("normalize")),
                   "template": str(cfg.get("template") or DEFAULT_CONFIG["template"]),
                   "max_mb": _clamp(cfg.get("max_mb"), 10, 5000, DEFAULT_CONFIG["max_mb"]),
                   "max_files": _clamp(cfg.get("max_files"), 1, 999, DEFAULT_CONFIG["max_files"])},
                  f, ensure_ascii=False)

def today_str() -> str:
    """当天日期, 格式 年.月.日(不补零)"""
    t = time.localtime()
    return f"{t.tm_year}.{t.tm_mon}.{t.tm_mday}"

def render_template(tpl: str, rname: str, sid: str, stem: str) -> str:
    """把模板里的四个关键字替换为实际值, 其余字符(连接符号)原样保留。
    例: 模板「姓名_学号 文件名称」+ 张三/17/example → 「张三_17 example」"""
    out = tpl
    for k, v in (("姓名", rname), ("学号", sid),
                 ("文件名称", stem), ("日期", today_str())):
        out = out.replace(k, v)
    return out.strip()

def template_valid(tpl: str) -> bool:
    """模板合法性: 至少含一个关键字, 且不含文件系统非法字符"""
    if not any(k in tpl for k in TPL_KEYWORDS):
        return False
    return not re.search(r'[\\/:*?"<>|\r\n\t]', tpl)

def _norm(s: str) -> str:
    """匹配用归一化: 去掉所有空白"""
    return re.sub(r"\s+", "", s or "")

def name_candidate(fn: str) -> str:
    """从文件名提取姓名候选。
    文件命名规范: 学号(纯数字)开头 + 姓名, 分隔符表达不限:
      20261008100134张君凯 / 20261008100134 张君凯 / 20261008100134+张君凯 /
      20261008100134_张君凯 / 20261008100134  张君凯 ...
    兼容 (1) 重命名后缀; 无学号前缀时原样返回。"""
    stem = os.path.splitext(fn or "")[0]
    stem = re.sub(r"\(\s*\d+\s*\)$", "", stem).strip()
    m = re.match(r"^\d+[\s_+＋\-—–－:：.．|｜/\\]*", stem)
    if m:
        stem = stem[m.end():]
    return stem.strip()

def match_roster(roster, files):
    """两遍匹配: 先精确(学号后的姓名部分与名单完全一致), 后子串兜底。
    返回 (submitted: {姓名: [文件名...]}, matched_fns: set, unknown: [(rel,fn)...])"""
    submitted, matched = {}, set()
    # 第一遍: 提取姓名候选, 与名单精确匹配
    for rel, fn, p, mt, sz in files:
        cand = _norm(name_candidate(fn))
        if not cand:
            continue
        for name in roster:
            if _norm(name) == cand:
                submitted.setdefault(name, []).append(fn)
                matched.add(fn)
                break
    # 第二遍: 未匹配的文件用子串兜底(应对"姓名+作业名"等非标准命名)
    for rel, fn, p, mt, sz in files:
        if fn in matched:
            continue
        nf = _norm(fn)
        for name in roster:
            nn = _norm(name)
            if nn and nn in nf:
                submitted.setdefault(name, []).append(fn)
                matched.add(fn)
                break
    unknown = [(rel, fn) for rel, fn, p, mt, sz in files if fn not in matched]
    return submitted, matched, unknown

def recognize_student(fn: str, pairs):
    """从文件名识别同学: 返回 ((姓名, 学号) or None, 原因)。
    与核对功能同源: 先精确(剥离学号前缀后的姓名部分)后子串兜底, 必须唯一命中。"""
    if not any(sid for _, sid in pairs):
        return None, "noid"
    cand = _norm(name_candidate(fn))
    if not cand:
        return None, "unknown"
    hits = [(n, sid) for n, sid in pairs if _norm(n) == cand and sid]
    if not hits:
        nf = _norm(fn)
        hits = [(n, sid) for n, sid in pairs if sid and _norm(n) in nf]
    if len(hits) != 1:
        return None, ("ambiguous" if hits else "unknown")
    return hits[0], "ok"

def student_files_in(target_dir: str, fn: str, pairs):
    """target_dir 内与 fn 属于同一同学的所有文件路径(含 fn 自身);
    识别不出是谁时退化为「精确同名」匹配。"""
    who, _ = recognize_student(fn, pairs)
    out = []
    if not os.path.isdir(target_dir):
        return out
    for f in os.listdir(target_dir):
        fp = os.path.join(target_dir, f)
        if not os.path.isfile(fp):
            continue
        if who:
            w2, _ = recognize_student(f, pairs)
            if w2 == who:
                out.append(fp)
        elif f == fn:
            out.append(fp)
    return out

def normalize_plan(name: str, pairs, cur_path: str):
    """按管理端配置的模板计算规范文件名。
    识别引擎与核对功能同源: 先精确(剥离学号前缀后的姓名部分)后子串兜底,
    且必须唯一命中一个「有学号」的名单成员才动手。
    模板可用关键字: 姓名/学号/文件名称/日期(当天, 年.月.日), 可自行排序和
    添加连接符号; 例「姓名_学号 文件名称」→「张三_17 example」。
    返回 (新文件名 or None, 原因):
      None+'off'      管理端关闭了自动规范化
      None+'canonical' 已是模板渲染出的形式(含 (n) 变体), 不动
      None+'unknown'   没识别出是谁
      None+'ambiguous' 命中多个姓名(如小组作业), 不猜
      None+'noid'      名单里没有学号
      新名+'ok'        建议改名为新名
    注意: 学号一律取名单里的(不信文件名里的), 文件名学号写错也能纠正。"""
    cfg = load_config()
    if not (cfg.get("normalize") and cfg.get("recognize")):
        return None, "off"
    if not any(sid for _, sid in pairs):
        return None, "noid"
    who, why = recognize_student(name, pairs)
    if not who:
        return None, why
    rname, sid = who
    stem, ext = os.path.splitext(name)
    target = render_template(cfg["template"], rname, sid, stem) + ext
    if target == name:
        return None, "canonical"
    # 目标名已存在(且不是自己)时加 (n) 序号, 绝不覆盖
    d = os.path.dirname(cur_path)
    if os.path.exists(os.path.join(d, target)) and \
       os.path.abspath(os.path.join(d, target)) != os.path.abspath(cur_path):
        cstem, cext = os.path.splitext(target)
        n = 1
        while True:
            trial = f"{cstem}({n}){cext}"
            tp = os.path.join(d, trial)
            if not os.path.exists(tp) or os.path.abspath(tp) == os.path.abspath(cur_path):
                target = trial
                break
            n += 1
    if target == name:
        return None, "canonical"
    return target, "ok"

def auto_normalize(target_dir: str, name: str):
    """上传后自动规范化: 按管理端模板把识别出身份的文件改成标准命名。
    管理端关闭 / 认不出是谁 / 名单无学号时原样返回。"""
    pairs = load_roster_pairs()
    new, why = normalize_plan(name, pairs, os.path.join(target_dir, name))
    if new:
        os.rename(os.path.join(target_dir, name), os.path.join(target_dir, new))
        return new
    return name

def is_admin():
    # request.values 同时覆盖 URL 参数和表单字段, 兼容 GET 链接与 POST 表单
    return request.values.get("token", "") == ADMIN_TOKEN

def safe_name(name: str) -> str:
    name = os.path.basename(name or "file")
    name = name.replace("/", "").replace("\\", "")
    return name or "file"

def fmt_sz(sz: int) -> str:
    if sz >= 1024 * 1024:
        return f"{sz / 1048576:.1f} MB"
    return f"{sz / 1024:.0f} KB"

def list_folders():
    """自定义文件夹 = uploads/ 下一级子目录(根目录视为'未分类', 不在此列)"""
    return sorted(d for d in os.listdir(UPLOAD_DIR)
                  if os.path.isdir(os.path.join(UPLOAD_DIR, d))
                  and not d.startswith("."))

def iter_all_files():
    """扫描全部文件 [(rel, fn, path, mtime, size)]; rel='' 表示未分类(根目录)"""
    out = []
    for fn in sorted(os.listdir(UPLOAD_DIR)):
        p = os.path.join(UPLOAD_DIR, fn)
        if os.path.isfile(p):
            mt = os.path.getmtime(p)
            out.append(("", fn, p, mt, os.path.getsize(p)))
    for d in list_folders():
        dd = os.path.join(UPLOAD_DIR, d)
        for fn in sorted(os.listdir(dd)):
            p = os.path.join(dd, fn)
            if os.path.isfile(p):
                mt = os.path.getmtime(p)
                out.append((d, fn, p, mt, os.path.getsize(p)))
    return out

def norm_folder(raw: str) -> str:
    """校验文件夹筛选参数: 'all' / '__root__' / 已存在的文件夹名; 非法回退 'all'"""
    raw = (raw or "all").strip()
    if raw in ("all", "__root__"):
        return raw
    if raw in list_folders():
        return raw
    return "all"

def match_folder(rel: str, folder: str) -> bool:
    if folder == "all":
        return True
    if folder == "__root__":
        return rel == ""
    return rel == folder

def no_store(resp: Response) -> Response:
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp

# ---------- 共享设计系统 ----------
GLASS_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{min-height:100vh;font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
 background:linear-gradient(160deg,#dce9fb 0%,#e9e4fb 45%,#d8f0f7 100%);
 background-attachment:fixed;color:#243049;position:relative;overflow-x:hidden}
.orb{position:fixed;border-radius:50%;filter:blur(70px);opacity:.55;z-index:0;pointer-events:none}
.orb.a{width:340px;height:340px;background:#8ab4ff;top:-90px;left:-90px}
.orb.b{width:300px;height:300px;background:#c39bff;bottom:-80px;right:-70px}
.orb.c{width:230px;height:230px;background:#7fe3ff;top:38%;right:10%}
.glass{background:rgba(255,255,255,.55);
 backdrop-filter:blur(18px) saturate(180%);-webkit-backdrop-filter:blur(18px) saturate(180%);
 border:1px solid rgba(255,255,255,.72);border-radius:20px;
 box-shadow:0 8px 32px rgba(31,45,80,.14), inset 0 1px 0 rgba(255,255,255,.75)}
input[type=date],select{font-family:inherit;font-size:15px;padding:10px 12px;border-radius:12px;
 border:1px solid rgba(255,255,255,.8);background:rgba(255,255,255,.6);color:#243049;outline:none;
 box-shadow:inset 0 2px 6px rgba(31,45,80,.06);transition:.2s}
input[type=date]:focus,select:focus{border-color:rgba(90,124,255,.8);box-shadow:0 0 0 3px rgba(90,124,255,.15)}
.svgico{width:1em;height:1em;display:inline-block;vertical-align:-.16em;flex-shrink:0}
.btn{display:inline-flex;align-items:center;gap:6px;font-size:14px;font-weight:600;padding:9px 16px;
 border-radius:12px;cursor:pointer;text-decoration:none;color:#3a5bcc;font-family:inherit;border:1px solid rgba(255,255,255,.85);
 background:rgba(255,255,255,.55);
 box-shadow:0 3px 10px rgba(31,45,80,.10), inset 0 1px 0 rgba(255,255,255,.8);transition:.2s}
.btn:hover{background:rgba(255,255,255,.85);transform:translateY(-1px);
 box-shadow:0 6px 16px rgba(31,45,80,.16), inset 0 1px 0 #fff}
.btn:active{transform:translateY(1px)}
.btn.danger{color:#c0392b;background:rgba(255,120,100,.10);border-color:rgba(255,120,100,.35);
 box-shadow:0 3px 10px rgba(200,60,40,.10), inset 0 1px 0 rgba(255,255,255,.5)}
.btn.danger:hover{background:rgba(255,120,100,.2);box-shadow:0 6px 16px rgba(200,60,40,.2)}
.btn.mini{font-size:13px;padding:7px 13px}
.cta{display:inline-block;border:none;cursor:pointer;font-family:inherit;font-size:16px;font-weight:700;
 color:#fff;letter-spacing:1px;padding:14px 26px;border-radius:14px;text-decoration:none;
 background:linear-gradient(135deg,#4f7cff,#7a5cff);
 box-shadow:0 10px 24px rgba(79,104,255,.35), inset 0 1px 0 rgba(255,255,255,.45);
 transition:.2s;position:relative;overflow:hidden}
.cta:hover{transform:translateY(-2px);box-shadow:0 14px 30px rgba(79,104,255,.42)}
.cta:active{transform:translateY(1px);box-shadow:0 5px 14px rgba(79,104,255,.35)}
.cta::after{content:"";position:absolute;top:0;left:-60%;width:40%;height:100%;
 background:linear-gradient(100deg,transparent,rgba(255,255,255,.35),transparent);
 animation:shine 3.2s infinite}
@keyframes shine{0%{left:-60%}55%{left:130%}100%{left:130%}}
.pill{font-size:13px;font-weight:600;color:#3a5bcc;padding:6px 14px;border-radius:99px;
 background:rgba(255,255,255,.55);border:1px solid rgba(255,255,255,.8);
 box-shadow:0 3px 10px rgba(31,45,80,.08), inset 0 1px 0 rgba(255,255,255,.7)}
.pill.date{color:#7a4bcc}
.pill.folder{color:#1f6f8b}
@media(max-width:600px){
 /* 安卓 WebView(X5等) 对 backdrop-filter+fixed 渲染有鬼影: 移动端降级为实底卡片 */
 .orb{display:none!important}
 .glass{background:rgba(255,255,255,.93);
  backdrop-filter:none;-webkit-backdrop-filter:none;
  box-shadow:0 4px 16px rgba(31,45,80,.12)}
 input[type=date],select{font-size:15px;padding:11px 12px}
 .btn{font-size:14px;padding:10px 14px}
 .cta{font-size:15px;padding:13px 20px}
}
"""

# ---------- 同学上传页 ----------
INDEX_TPL = ("<!doctype html><html lang=zh><meta charset=utf-8>\n"
"<meta name=viewport content=\"width=device-width,initial-scale=1\">\n"
"<title>作业提交</title><style>" + GLASS_CSS + """
body{display:flex;align-items:center;justify-content:center;padding:24px}
.wrap{position:relative;z-index:1;width:100%;max-width:540px}
h1{font-size:34px;font-weight:800;text-align:center;margin-bottom:8px;
 background:linear-gradient(120deg,#2b3a67,#5b4b9e 55%,#1f6f8b);
 -webkit-background-clip:text;background-clip:text;color:transparent;
 filter:drop-shadow(0 2px 10px rgba(90,110,255,.25))}
.sub{text-align:center;color:#5a6a81;font-size:14px;margin-bottom:18px}
.tool{display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding:13px 16px;margin-bottom:14px}
.tool label{font-size:13px;color:#5a6a81;font-weight:600}
.tool select{flex:1;min-width:110px;padding:9px 10px;font-size:14px}
.drop{padding:38px 20px;text-align:center;cursor:pointer;transition:all .25s ease}
.drop:hover{transform:translateY(-2px)}
.drop.on{background:rgba(255,255,255,.72);transform:scale(1.02);
 border-color:rgba(90,124,255,.9);
 box-shadow:0 0 0 3px rgba(90,124,255,.18),0 14px 44px rgba(80,110,255,.28), inset 0 1px 0 #fff}
.ico{width:70px;height:70px;margin:0 auto 12px;border-radius:22px;
 display:flex;align-items:center;justify-content:center;font-size:32px;
 background:linear-gradient(145deg,rgba(255,255,255,.95),rgba(255,255,255,.45));
 border:1px solid rgba(255,255,255,.85);
 box-shadow:0 8px 20px rgba(31,45,80,.13), inset 0 1px 0 #fff}
.drop b{font-size:16px;color:#243049;display:block;margin-bottom:6px}
.drop small{color:#71809b;font-size:13px}
#pick,#pickimg{display:none}
.flist{display:none;max-height:300px;overflow-y:auto;padding:8px;margin-top:14px}
.flist::-webkit-scrollbar{width:8px}
.flist::-webkit-scrollbar-thumb{background:rgba(120,140,180,.35);border-radius:99px}
.fitem{padding:11px 13px;border-radius:12px;margin:6px;transition:.2s;
 background:rgba(255,255,255,.3);border:1px solid transparent}
.fitem.done{background:rgba(46,125,91,.08)}
.fitem.error{background:rgba(255,120,100,.08)}
.frow{display:flex;align-items:center;gap:8px}
.fnm2{font-weight:600;font-size:13.5px;word-break:break-all;flex:1;min-width:0}
.fsz2{color:#71809b;font-size:12px;flex-shrink:0}
.fst{font-size:12.5px;font-weight:700;flex-shrink:0}
.fst.pending{color:#71809b}.fst.uploading{color:#3a5bcc}
.fst.done{color:#2e7d5b}.fst.error{color:#c0392b}
.fst.canceled{color:#b8860b}.fst.skipped{color:#8a97ab}
.canc{border:none;background:rgba(255,120,100,.15);color:#c0392b;border-radius:8px;
 padding:4px 10px;cursor:pointer;font-size:12px;font-weight:600;flex-shrink:0}
.canc:hover{background:rgba(255,120,100,.28)}
.ferr{font-size:12px;color:#c0392b;margin-top:5px}
.fbar{height:5px;border-radius:99px;background:rgba(120,140,180,.18);margin-top:8px;overflow:hidden}
.fbar>i{display:block;height:100%;width:0;border-radius:99px;
 background:linear-gradient(90deg,#4f7cff,#8a5cff);transition:width .15s}
.total{display:none;margin-top:14px;padding:13px 16px}
.trow{display:flex;align-items:center;gap:10px;margin-bottom:8px}
.trow .lbl{font-size:13px;font-weight:700;color:#31405e}
.trow .cnt{margin-left:auto;font-size:12.5px;color:#5a6a81}
.tbar{height:8px;border-radius:99px;background:rgba(120,140,180,.18);overflow:hidden}
.tbar>i{display:block;height:100%;width:0;border-radius:99px;
 background:linear-gradient(90deg,#4f7cff,#8a5cff);transition:width .2s}
.acts{display:flex;gap:10px;margin-top:14px}
.acts .cta{flex:1;text-align:center}
.acts .btn{display:none;align-items:center;justify-content:center}
.acts .btn.show{display:inline-flex}
.msg{text-align:center;margin-top:14px;font-size:15px;min-height:22px;font-weight:600;color:#2e7d5b}
.msg.err{color:#c0392b}
.announce{padding:14px 18px;margin-bottom:14px;border-color:rgba(255,190,80,.55);
 background:rgba(255,247,235,.80)}
.announce .anrow{display:flex;align-items:center;gap:8px;margin-bottom:5px}
.announce .anico{font-size:18px}
.announce .antext{font-size:13px;font-weight:800;color:#8a6d1a;letter-spacing:1px}
.announce .antime{margin-left:8px;font-size:11.5px;font-weight:400;color:#b59a55}
.announce p{font-size:14px;color:#6b5a2a;line-height:1.8;white-space:pre-wrap;word-break:break-all}
.mask{position:fixed;inset:0;background:rgba(20,30,55,.45);display:none;
 align-items:center;justify-content:center;z-index:10;padding:20px}
.mask.on{display:flex}
.dlg{max-width:440px;width:100%;padding:22px;max-height:85vh;overflow-y:auto}
.dlg h3{font-size:17px;font-weight:800;color:#31405e;margin-bottom:10px}
.dtext{font-size:13.5px;color:#5a6a81;line-height:1.8;margin-bottom:10px}
.duplist{margin:10px 0}
.dupitem{font-size:13px;background:rgba(255,190,80,.10);border:1px solid rgba(255,190,80,.4);
 border-radius:10px;padding:8px 12px;margin:6px 0;word-break:break-all;color:#8a6d1a}
.dupitem small{color:#a08a5a}
.dlgbtns{display:flex;gap:10px;margin-top:14px;flex-wrap:wrap}
.dlgbtns .cta,.dlgbtns .btn{flex:1;justify-content:center;text-align:center;min-width:120px}
.mobrow{display:none;margin-top:10px;text-align:center}
.mobrow.show{display:block}
.mobbtn{display:inline-flex;justify-content:center}
@media(max-width:600px){
 body{padding:14px 10px}
 .wrap{max-width:100%}
 h1{font-size:26px}
 .acts{flex-wrap:wrap}
 .acts .cta{flex:1 1 100%}
 .acts .btn{flex:1}
}
__EMBED_CSS__
</style>
<div class="orb a"></div><div class="orb b"></div><div class="orb c"></div>
<div class=wrap>
<h1>作业提交</h1>
<p class=sub>支持一次选择多个文件 · 文档 / 表格 / 压缩包 / 图片</p>
__ANNOUNCE__
<div class="glass tool">
 <span><label><svg class=svgico viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 7.5a2 2 0 0 1 2-2h3.6l2 2h6.4a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H5.5a2 2 0 0 1-2-2Z"/></svg>文件夹</label><select id=folder>__FOLDERS__</select></span>
</div>
<div class="glass drop" id=drop>
 <div class=ico><svg width="38" height="38" viewBox="0 0 24 24" fill="none" stroke="url(#ig)" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><defs><linearGradient id="ig" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#4f7cff"/><stop offset="1" stop-color="#8a5cff"/></linearGradient></defs><path d="M3.5 7.5a2 2 0 0 1 2-2h3.6l2 2h6.4a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H5.5a2 2 0 0 1-2-2Z"/></svg></div>
 <b>点击选择文件（可多选）</b>
 <small>__LIMIT_HINT__</small>
</div>
<input type=file id=pick accept="*/*" multiple>
<input type=file id=pickimg accept="image/*,video/*" multiple>
<div class=mobrow id=mobrow><button class="btn mobbtn" id=imgbtn type=button>🖼 选图片 / 视频</button></div>
<div class="glass flist" id=flist></div>
<div class="glass total" id=total>
 <div class=trow><span class=lbl>总体进度</span><span class=cnt id=tcnt></span></div>
 <div class=tbar><i id=tbar></i></div>
</div>
<div class=acts>
 <button class=cta id=go style="display:none">开始上传</button>
 <button class="btn danger" id=call>✕ 取消全部</button>
 <button class=btn id=clear>清空列表</button>
</div>
<p class=msg id=msg></p>
</div>
<div class=mask id=dupmask>
 <div class="glass dlg">
  <h3><svg class=svgico style="color:#b8860b" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4.5 2.8 20h18.4Z"/><path d="M12 10v4"/><path d="M12 17.2v.1"/></svg>检测到重复提交</h3>
  <p class=dtext>文件夹「<b id=dupfolder></b>」里已经有这些同学的文件：</p>
  <div class=duplist id=duplist></div>
  <p class=dtext>是否用新上传的文件<b>替换原文件</b>？<br>如果你传错了文件夹，请点「取消」，检查后再传。</p>
  <div class=dlgbtns>
   <button class=cta id=dupReplace>🔄 替换原文件</button>
   <button class="btn danger" id=dupCancel>取消</button>
  </div>
 </div>
</div>
<div class=mask id=smallmask>
 <div class="glass dlg">
  <h3><svg class=svgico style="color:#b8860b" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4.5 2.8 20h18.4Z"/><path d="M12 10v4"/><path d="M12 17.2v.1"/></svg>文件可能有问题</h3>
  <p class=dtext>以下文件小于 1KB，可能没传对或者打不开：</p>
  <div class=duplist id=smalllist></div>
  <p class=dtext>确定仍要上传吗？</p>
  <div class=dlgbtns>
   <button class=cta id=smallOk>确定上传</button>
   <button class="btn danger" id=smallCancel>取消</button>
  </div>
 </div>
</div>
<script>
const drop=document.getElementById('drop'),pick=document.getElementById('pick'),
 pickimg=document.getElementById('pickimg'),imgbtn=document.getElementById('imgbtn'),
 go=document.getElementById('go'),flist=document.getElementById('flist'),
 fself=document.getElementById('folder'),totalBox=document.getElementById('total'),
 tbar=document.getElementById('tbar'),tcnt=document.getElementById('tcnt'),
 callBtn=document.getElementById('call'),clearBtn=document.getElementById('clear'),
 msg=document.getElementById('msg'),
 dupmask=document.getElementById('dupmask'),dupfolder=document.getElementById('dupfolder'),
 duplist=document.getElementById('duplist'),dupReplace=document.getElementById('dupReplace'),
 dupCancel=document.getElementById('dupCancel'),
 smallmask=document.getElementById('smallmask'),smalllist=document.getElementById('smalllist'),
 smallOk=document.getElementById('smallOk'),smallCancel=document.getElementById('smallCancel');
const MAX_MB=__MAX_MB__, MAX_FILES=__MAX_FILES__, MAX_CONC=3, ADMIN_MODE=__ADMIN_MODE__;
const MAX_SIZE=MAX_MB*1024*1024;
const BLOCKED=['exe','bat','cmd','com','scr','msi','apk','jar','vbs','ps1','sh','dll','so'];
let queue=[],seq=0,running=0,canceled=false,started=false;
function fmt(b){return b>=1048576?(b/1048576).toFixed(1)+' MB':(b/1024).toFixed(0)+' KB'}
function esc(s){return s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function extOf(n){const i=n.lastIndexOf('.');return i<0?'':n.slice(i+1).toLowerCase()}
function totalBytes(){return queue.reduce((s,t)=>s+t.file.size,0)}
function sentBytes(){return queue.reduce((s,t)=>s+(t.finished&&t.status!=='error'?t.file.size:(t.status==='uploading'?t.file.size*t.pct/100:0)),0)}
drop.onclick=()=>pick.click();
pick.onchange=()=>{addFiles(pick.files);pick.value='';};
imgbtn.onclick=()=>pickimg.click();
pickimg.onchange=()=>{addFiles(pickimg.files);pickimg.value='';};
// 移动端: 部分浏览器把 accept="*/*" 当成"无限制"后只给相机/相册, 没有「文件」。
// 改成明确的文档类型后系统才会出现「文件」入口; 图片/视频走单独按钮。
(function(){
 if(!/Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent||''))return;
 pick.setAttribute('accept',
  '.doc,.docx,.xls,.xlsx,.ppt,.pptx,.pdf,.txt,.md,.csv,.zip,.rar,.7z,.tar,.gz,.wps,.et,.dps,.odt,.ods,.odp,.c,.cpp,.h,.py,.java,.js,.json,.html,.css,.sql,.ipynb,.tex,.go,.rs,.php,.xml,.yaml,.yml');
 document.getElementById('mobrow').classList.add('show');
})();
drop.ondragover=e=>{e.preventDefault();drop.classList.add('on')};
drop.ondragleave=()=>drop.classList.remove('on');
drop.ondrop=e=>{e.preventDefault();drop.classList.remove('on');addFiles(e.dataTransfer.files)};
function addFiles(list){
 if(started&&queue.some(t=>t.status==='pending'||t.status==='uploading')){
  msg.textContent='还有文件在上传中，请等待完成或取消后再添加';msg.className='msg err';return;}
 let reject=0;
 for(const f of list){
  if(queue.length>=MAX_FILES){msg.textContent='一次最多提交 '+MAX_FILES+' 个文件';msg.className='msg err';break;}
  const e=extOf(f.name);
  if(!ADMIN_MODE&&BLOCKED.includes(e)){queue.push(mk(f,'error','不支持的文件类型：.'+e));reject++;continue;}
  if(!ADMIN_MODE&&f.size>MAX_SIZE){queue.push(mk(f,'error','单个文件超过 '+MAX_MB+'MB 限制'));reject++;continue;}
  if(queue.some(t=>t.file.name===f.name&&t.file.size===f.size&&t.status!=='error')){
   queue.push(mk(f,'error','与列表中已有文件重复'));continue;}
  queue.push(mk(f,'pending','',!ADMIN_MODE&&f.size<=1024));
 }
 msg.textContent='';msg.className='msg';
 render();
}
function mk(file,status,msg2,small){return {id:++seq,file,status:status,msg:msg2,pct:0,retry:0,xhr:null,finished:false,small:!!small}}
function stTxt(t){
 if(t.status==='pending')return t.retry?('等待重试 '+t.retry+'/2'):'等待中';
 if(t.status==='uploading')return t.pct+'%';
 if(t.status==='done')return '已上传';
 if(t.status==='skipped')return '⏭ 已跳过';
 if(t.status==='canceled')return '已取消';
 if(t.status==='error')return '失败';
 return '';}
function render(){
 flist.style.display=queue.length?'block':'none';
 fself.disabled=!!started;  // 上传期间锁定文件夹选择
 go.style.display=(queue.some(t=>t.status==='pending'&&!started)&&!started)?'block':'none';
 callBtn.className='btn danger'+(started&&queue.some(t=>t.status==='uploading'||t.status==='pending')?' show':'');
 clearBtn.className='btn'+(queue.length&&!started?' show':'');
 let html='';
 for(const t of queue){
  html+='<div class="fitem '+t.status+'"><div class=frow>'
   +'<span class=fnm2 title="'+esc(t.file.name)+'"><svg class=svgico viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3.5H7.5a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h9a2 2 0 0 0 2-2V8Z"/><path d="M14 3.5V8h4.5"/></svg> '+esc(t.file.name)+'</span>'
   +'<span class=fsz2>'+fmt(t.file.size)+'</span>'
   +'<span class="fst '+t.status+'">'+stTxt(t)+'</span>'
   +((t.status==='pending'&&!started)||(t.status==='uploading')
     ?'<button class=canc data-id='+t.id+'>✕</button>':'')
   +'</div>'
   +(t.status==='uploading'?'<div class=fbar><i style="width:'+t.pct+'%"></i></div>':'')
   +(t.msg?'<div class=ferr>'+esc(t.msg)+'</div>':'')
   +(t.small&&t.status==='pending'?'<div class=ferr style="color:#b8860b">文件小于 1KB，请检查是否传对、能否正常打开</div>':'')
   +'</div>';
 }
 flist.innerHTML=html;
 flist.querySelectorAll('.canc').forEach(b=>b.onclick=()=>cancelOne(+b.dataset.id));
 updateTotal();
}
function updateTotal(){
 const up=queue.some(t=>t.status==='uploading')||(started&&queue.some(t=>t.status==='pending'));
 totalBox.style.display=up?'block':'none';
 if(!up)return;
 const tot=totalBytes(),sent=sentBytes();
 tbar.style.width=(tot?Math.round(sent/tot*100):0)+'%';
 const ok=queue.filter(t=>t.status==='done'||t.status==='skipped').length;
 const bad=queue.filter(t=>t.status==='error').length;
 tcnt.textContent='已完成 '+ok+'/'+queue.length+' 个'+(bad?(' · 失败 '+bad+' 个'):'');
}
let uploadFolder='';  // 开始上传时锁定, 防止上传途中切换文件夹
let replaceSet=new Set();  // 弹窗里选了「替换原文件」的文件 id
// 上传前查重: 该文件夹内是否已有「这个同学」的文件, 有就让用户选 替换/保留/取消
function precheck(){
 const pend=queue.filter(t=>t.status==='pending');
 if(!pend.length)return Promise.resolve(true);
 return Promise.all(pend.map(t=>{
  const fd=new FormData();
  fd.append('folder',uploadFolder);
  fd.append('name',t.file.name);
  return fetch('/upload/check',{method:'POST',body:fd})
   .then(r=>r.json()).catch(()=>({dup:false}));
 })).then(rs=>{
  const dups=pend.filter((t,i)=>rs[i]&&rs[i].dup);
  if(!dups.length)return true;
  dupfolder.textContent=uploadFolder;
  duplist.innerHTML=dups.map((t,i)=>{
   const r=rs[pend.indexOf(t)];
   const ex=(r.existing||[]).map(x=>'<small>已存在：'+esc(x)+'</small>').join('');
   return '<div class=dupitem><svg class=svgico viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3.5H7.5a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h9a2 2 0 0 0 2-2V8Z"/><path d="M14 3.5V8h4.5"/></svg> '+esc(t.file.name)+'<br>'+ex+'</div>';
  }).join('');
  dupmask.classList.add('on');
  return new Promise(res=>{
   dupReplace.onclick=()=>{dupmask.classList.remove('on');
    dups.forEach(t=>replaceSet.add(t.id));res(true);};
   dupCancel.onclick=()=>{dupmask.classList.remove('on');res(false);};
  });
 });
}
// 小文件确认: ≤1KB 的文件先弹窗问过再传
function askSmall(){
 const smalls=queue.filter(t=>t.status==='pending'&&t.small);
 if(!smalls.length)return Promise.resolve(true);
 smalllist.innerHTML=smalls.map(t=>'<div class=dupitem><svg class=svgico viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3.5H7.5a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h9a2 2 0 0 0 2-2V8Z"/><path d="M14 3.5V8h4.5"/></svg> '+esc(t.file.name)+'（'+fmt(t.file.size)+'）</div>').join('');
 smallmask.classList.add('on');
 return new Promise(res=>{
  smallOk.onclick=()=>{smallmask.classList.remove('on');res(true);};
  smallCancel.onclick=()=>{smallmask.classList.remove('on');res(false);};
 });
}
go.onclick=()=>{
 if(!fself.value){msg.textContent='请先选择要提交到的文件夹';msg.className='msg err';return;}
 uploadFolder=fself.value;
 go.disabled=true;
 if(ADMIN_MODE){go.disabled=false;started=true;canceled=false;render();tick();return;}
 askSmall().then(ok=>{
  if(!ok){go.disabled=false;return;}
  return precheck().then(ok2=>{
   go.disabled=false;
   if(!ok2||canceled)return;
   started=true;canceled=false;render();tick();
  });
 });};
function conc(){return MAX_CONC}  /* 并发数: 内部固定 3, 不再暴露成用户可配的第二个"数量"概念 */
function tick(){
 if(canceled)return;
 while(running<conc()){
  const t=queue.find(x=>x.status==='pending');
  if(!t)break;
  send(t);
 }
 const busy=queue.some(x=>x.status==='pending'||x.status==='uploading');
 if(!busy){ finish(); }
 render();
}
function send(t){
 t.status='uploading';t.pct=0;t.msg='';running++;
 const fd=new FormData();
 fd.append('file',t.file);
 fd.append('folder',uploadFolder);
 if(replaceSet.has(t.id))fd.append('replace','1');
 const xhr=new XMLHttpRequest();t.xhr=xhr;
 xhr.open('POST','/upload');
 xhr.upload.onprogress=e=>{if(e.lengthComputable){t.pct=Math.round(e.loaded/e.total*100);render();}};
 xhr.onload=()=>{ if(t.finished)return; t.finished=true; running--;
  if(xhr.status===200){
   try{const r=JSON.parse(xhr.responseText);
    t.status=(r.status==='skipped')?'skipped':'done';t.msg='';
   }catch(e){t.status='done';}
  }else if(xhr.status===413){t.status='error';t.msg='文件过大，服务器拒绝接收';}
  else if(xhr.status===400){t.status='error';t.msg='服务器拒绝该文件（如文件夹不存在）';}
  else{t.status='error';t.msg='服务器错误 ('+xhr.status+')';}
  render();tick();};
 xhr.onerror=xhr.onabort=()=>{ if(t.finished)return; t.finished=true; running--;
  if(t.canceledByUser){t.status='canceled';t.msg='已取消';}
  else if(t.retry<2){t.retry++;t.msg='网络中断，自动重试 '+t.retry+'/2';t.status='pending';
   setTimeout(()=>{if(!canceled)send(t);},t.retry*900);}
  else{t.status='error';t.msg='网络错误，已自动重试 2 次仍失败';}
  render();tick();};
 xhr.send(fd);
}
function cancelOne(id){
 const t=queue.find(x=>x.id===id);
 if(!t||(t.status!=='pending'&&t.status!=='uploading'))return;
 t.canceledByUser=true;
 if(t.status==='uploading'&&t.xhr&&!t.finished){t.xhr.abort();}
 else{t.finished=true;t.status='canceled';t.msg='已取消';render();tick();}
}
callBtn.onclick=()=>{ canceled=true;
 for(const t of queue){
  if(t.status==='pending'&&!t.finished){t.finished=true;t.status='canceled';t.msg='已取消';}
  if(t.status==='uploading'&&t.xhr&&!t.finished){t.xhr.abort();}
 }
 render();};
clearBtn.onclick=()=>{ if(started&&queue.some(t=>t.status==='uploading'||t.status==='pending'))return;
 queue=[];started=false;canceled=false;replaceSet.clear();msg.textContent='';msg.className='msg';render();};
function finish(){
 if(!started)return;
 const ok=queue.filter(t=>t.status==='done').length;
 const sk=queue.filter(t=>t.status==='skipped').length;
 const bad=queue.filter(t=>t.status==='error').length;
 const cd=queue.filter(t=>t.status==='canceled').length;
 if(bad){msg.textContent='完成：'+ok+' 成功'+(sk?(', '+sk+' 跳过'):'')+'，'+bad+' 失败'+(cd?(', '+cd+' 取消'):'');msg.className='msg err';}
 else if(ok||sk){msg.textContent='🎉 全部完成：'+ok+' 成功'+(sk?(', '+sk+' 跳过'):'')+(cd?(', '+cd+' 取消'):'');msg.className='msg';}
 started=false;
}
render();
</script></body></html>""")

def index_html(embed: bool = False) -> str:
    # 不再提供"未分类(根目录)"选项: 必须选择文件夹才能提交;
    # 默认项为不可选中的提示项, 防止绕过界面直接提交到根目录
    opts = '<option value="" selected disabled>请选择文件夹</option>'
    for d in list_folders():
        opts += f'<option value="{html.escape(d)}">{html.escape(d)}</option>'
    ann = load_announcement()
    if ann:
        announce = ('<div class="glass announce">'
                    '<div class=anrow><span class=anico><svg class=svgico viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 10v4a1 1 0 0 0 1 1h2l5.5 4V5L7 9H5a1 1 0 0 0-1 1Z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M18.5 6a9 9 0 0 1 0 12"/></svg></span>'
                    '<span class=antext>公告'
                    f'<span class=antime>{html.escape(ann["time"])}</span></span></div>'
                    f'<p>{html.escape(ann["text"])}</p></div>')
    else:
        announce = ""
    cfg = load_config()
    if embed:
        # 管理端内嵌: 背景透明融入管理页, 隐藏页标题/上限提示, 且不做任何客户端限制
        limit_hint = ""
        max_mb_v, max_files_v, admin_mode = "Infinity", "Infinity", "true"
        embed_css = ("html,body{background:transparent!important}.orb{display:none!important}"
                     "body{padding:0!important}h1,.sub{display:none!important}"
                     ".wrap{max-width:100%!important}.drop small:empty{display:none}")
    else:
        limit_hint = (f"或拖拽文件到此处 · 单个 ≤ {cfg['max_mb']}MB · "
                      f"单次最多 {cfg['max_files']} 个")
        max_mb_v, max_files_v, admin_mode = str(cfg["max_mb"]), str(cfg["max_files"]), "false"
        embed_css = ""
    return (INDEX_TPL.replace("__FOLDERS__", opts)
            .replace("__ANNOUNCE__", announce)
            .replace("__LIMIT_HINT__", limit_hint)
            .replace("__MAX_MB__", max_mb_v)
            .replace("__MAX_FILES__", max_files_v)
            .replace("__ADMIN_MODE__", admin_mode)
            .replace("__EMBED_CSS__", embed_css))

def upload_pane_parts():
    """把学生上传页拆成 (CSS, HTML, JS), 供管理页原生渲染(不再用 iframe)。
    css : 去掉 body/h1/.sub/.wrap 等会污染管理页的规则
    html: 去掉光斑与标题, 容器换成 #upPane
    js  : 用管理端模式(不限大小/数量), 包进 IIFE 避免与后台脚本变量冲突"""
    page = index_html(embed=True)
    style = page.split("<style>", 1)[1].split("</style>", 1)[0]
    css = style.split(GLASS_CSS, 1)[1] if GLASS_CSS in style else ""
    for pat in (r"body\{[^}]*\}", r"\.wrap\{[^}]*\}", r"h1\{[^}]*\}",
                r"\.sub\{[^}]*\}"):
        css = re.sub(pat, "", css, count=1)
    css = re.sub(r"body\{[^}]*\}\s*\.wrap\{[^}]*\}\s*h1\{[^}]*\}", "", css)
    for pat in (r"html,body\{background:transparent!important\}",
                r"\.orb\{display:none!important\}",
                r"body\{padding:0!important\}",
                r"h1,\.sub\{display:none!important\}",
                r"\.wrap\{max-width:100%!important\}",
                r"\.drop small:empty\{display:none\}"):
        css = re.sub(pat, "", css)
    pre = page.split("<script>", 1)[0]
    body = pre.split("</style>", 1)[1]
    body = re.sub(r'<div class="orb [abc]"></div>', "", body)
    body = re.sub(r"<h1>作业提交</h1>\s*<p class=sub>[^<]*</p>", "", body, count=1)
    body = body.replace("<div class=wrap>", '<div id="upPane">', 1)
    js = page.split("<script>", 1)[1].split("</script>", 1)[0]
    js = "<script>(function(){" + js + "})();</script>"
    return css, body, js

@app.get("/")
def index():
    embed = request.args.get("embed") == "1"
    return no_store(Response(index_html(embed=embed), mimetype="text/html"))

@app.post("/upload")
def upload():
    f = request.files.get("file")
    if not f or not f.filename:
        abort(400, "没有文件")
    # 目标文件夹: 必须是已存在的文件夹, 防穿越; 不允许未分类(根目录)
    folder = (request.form.get("folder") or "").strip()[:32]
    if not folder:
        abort(400, "请先选择文件夹")
    folder = safe_name(folder)
    if folder not in list_folders():
        abort(400, "文件夹不存在")
    target = os.path.join(UPLOAD_DIR, folder) if folder else UPLOAD_DIR
    f.seek(0)
    name = safe_name(f.filename)
    path = os.path.join(target, name)
    # replace=1: 上传前弹窗确认过「替换原文件」——删掉该文件夹内属于同一同学的
    # 所有文件(识别优先, 认不出按精确同名), 让新文件顶替, 不再走 (n) 改名
    replace_mode = (request.form.get("replace") or "") == "1"
    if replace_mode:
        for pf in student_files_in(target, name, load_roster_pairs()):
            try:
                os.remove(pf)
            except OSError:
                pass
    elif os.path.exists(path):
        # 同名一律加 (n) 序号, 绝不覆盖(识别出的同学随后还会被自动规范化学号姓名)
        stem, ext = os.path.splitext(name)
        n = 1
        while os.path.exists(path):
            name = f"{stem}({n}){ext}"
            path = os.path.join(target, name)
            n += 1
    f.save(path)
    # 自动规范化: 识别出是名单里的谁, 就改成「学号 姓名」标准格式
    # (学号取名单里的, 不信文件名; 认不出/多人混淆/名单无学号时保持原样)
    name = auto_normalize(target, name)
    return Response(json.dumps({"status": "uploaded", "name": name,
                                "folder": folder}),
                    mimetype="application/json")

@app.post("/upload/check")
def upload_check():
    """上传前查重(同学端调用, 无需令牌): 该文件夹内是否已有「这个同学」的文件。
    识别得出是谁时, 返回其已有文件列表(仅限对方名义上传的场景, 属已知取舍);
    识别不出时退化为「精确同名」判断。"""
    if not load_config().get("recognize"):
        return Response(json.dumps({"dup": False}),
                        mimetype="application/json")
    folder = (request.form.get("folder") or "").strip()[:32]
    if folder:
        folder = safe_name(folder)
        if folder not in list_folders():
            return Response(json.dumps({"dup": False}),
                            mimetype="application/json")
    target = os.path.join(UPLOAD_DIR, folder) if folder else UPLOAD_DIR
    name = safe_name(request.form.get("name") or "")
    if not name:
        return Response(json.dumps({"dup": False}),
                        mimetype="application/json")
    pairs = load_roster_pairs()
    who, _ = recognize_student(name, pairs)
    existing = []
    if who and os.path.isdir(target):
        # 只对「认得出是谁」的重复弹窗(替换/取消); 认不出是谁的同名文件不弹窗,
        # 由上传时的 (n) 保底——避免不同同学传同名文件时互相覆盖
        for fn in os.listdir(target):
            fp = os.path.join(target, fn)
            if not os.path.isfile(fp):
                continue
            w2, _ = recognize_student(fn, pairs)
            if w2 == who:
                existing.append(fn)
    return Response(json.dumps({"dup": bool(existing),
                                "student": who[0] if who else "",
                                "existing": existing}),
                    mimetype="application/json")

# ---------- 管理页 ----------
ADMIN_TPL = ("<!doctype html><html lang=zh><meta charset=utf-8>\n"
"<meta name=viewport content=\"width=device-width,initial-scale=1\">\n"
"<title>作业管理</title><style>" + GLASS_CSS + """
body{padding:26px 18px 48px}
.wrap{position:relative;z-index:1;max-width:780px;margin:0 auto}
h1{font-size:30px;font-weight:800;margin-bottom:18px;
 background:linear-gradient(120deg,#2b3a67,#5b4b9e 55%,#1f6f8b);
 -webkit-background-clip:text;background-clip:text;color:transparent}
.panel{padding:18px 20px;margin-bottom:18px}
.bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.bar+.bar{margin-top:12px}
.lbl{font-size:14px;color:#5a6a81;font-weight:600}
.tabs{display:flex;gap:10px;margin-bottom:18px;position:relative;z-index:1}
.tab{flex:1;padding:13px 10px;border:none;cursor:pointer;font-family:inherit;font-size:15px;font-weight:700;
 border-radius:14px;color:#5a6a81;background:rgba(255,255,255,.4);border:1px solid rgba(255,255,255,.7);
 transition:.2s;box-shadow:0 3px 10px rgba(31,45,80,.08)}
.tab:hover{background:rgba(255,255,255,.75)}
.tab.on{color:#fff;background:linear-gradient(135deg,#4f7cff,#7a5cff);border-color:transparent;
 box-shadow:0 8px 22px rgba(79,104,255,.35), inset 0 1px 0 rgba(255,255,255,.4)}
.upframe{width:100%;height:calc(100vh - 215px);min-height:620px;border:none;background:transparent;display:block}

/* ===== 顶部: 标题+两个小图标按钮(公告栏/设置) + 双TAB ===== */
.topbar{display:flex;align-items:center;justify-content:space-between;gap:12px;
 margin-bottom:14px;position:relative;z-index:1}
.topbar h1{margin-bottom:0}
.ticbtns{display:flex;gap:8px;flex-shrink:0}
.ticbtn{width:42px;height:42px;border:none;cursor:pointer;border-radius:12px;font-size:19px;
 color:#3a5bcc;background:rgba(255,255,255,.6);border:1px solid rgba(255,255,255,.85);
 box-shadow:0 3px 10px rgba(31,45,80,.10);transition:.2s;
 display:flex;align-items:center;justify-content:center}
.ticbtn:hover{background:rgba(255,255,255,.85);transform:translateY(-1px)}
.ticbtn:active{transform:translateY(0)}
.ticbtn.on{color:#fff;background:linear-gradient(135deg,#4f7cff,#7a5cff);border-color:transparent;
 box-shadow:0 6px 16px rgba(79,104,255,.35), inset 0 1px 0 rgba(255,255,255,.4)}
.ticbtn .svgico{width:22px;height:22px;vertical-align:0}
.tab .svgico{width:18px;height:18px;vertical-align:-.2em;margin-right:5px}
h3 .svgico{width:19px;height:19px;vertical-align:-.2em;color:#4f6cff;margin-right:6px}
.btn .svgico,.cta .svgico{width:17px;height:17px;vertical-align:-.2em}
.lbl .svgico,.ltitle .svgico{width:16px;height:16px;vertical-align:-.2em;margin-right:5px;color:#4f6cff}
.sub2{font-size:13px;color:#8a97ab;line-height:1.8;margin-bottom:14px}
.panel .sw{margin:10px 0}
.panel .sw label{display:flex;align-items:center;gap:9px;cursor:pointer;
 font-size:14px;font-weight:700;color:#31405e;line-height:1.5}
.panel .sw input{width:18px;height:18px;cursor:pointer;accent-color:#4f7cff;flex-shrink:0}
.panel .sw label:has(input:disabled){opacity:.45;cursor:not-allowed}
.panel .fld{margin:16px 0}
.panel .fld .cap{display:block;font-size:13px;font-weight:700;color:#5a6a81;margin-bottom:7px}
.panel input[type=text],.panel input[type=number],.panel textarea{width:100%;font-family:inherit;
 font-size:14px;padding:11px 13px;border-radius:12px;color:#243049;outline:none;
 border:1.5px solid rgba(120,140,180,.5);background:rgba(255,255,255,.9);
 box-shadow:inset 0 1px 3px rgba(31,45,80,.05);transition:.2s;box-sizing:border-box}
.panel input[type=text]:focus,.panel input[type=number]:focus,.panel textarea:focus{
 border-color:#4f7cff;box-shadow:0 0 0 3px rgba(79,124,255,.16)}
.panel textarea{resize:vertical;min-height:130px;line-height:1.8}
.panel .chips{display:flex;gap:8px;flex-wrap:wrap;margin-top:9px}
.panel .numrow{display:flex;align-items:center;gap:9px}
.panel .numrow input{width:100px;flex-shrink:0;text-align:center}
.panel .numrow .unit{font-size:14px;font-weight:700;color:#31405e}
.panel .numrow .rng{font-size:12.5px;color:#8a97ab}
.panel .popbtns{display:flex;gap:10px;margin-top:8px}
.panel .popbtns .cta{flex:1;text-align:center;justify-content:center;font-size:15px;padding:13px 16px}
.panel .popbtns .btn{flex:1;justify-content:center;text-align:center;font-size:14px;padding:12px 14px}
.panel .note{font-size:12.5px;color:#8a97ab;line-height:1.9;margin-top:14px}
.panel .note b{color:#5a6a81}
.panel .note.clamp{max-height:26px;overflow:hidden;position:relative;margin-bottom:8px}
.panel .note.clamp::after{content:"";position:absolute;left:0;right:0;bottom:0;height:22px;
 background:linear-gradient(rgba(255,255,255,0),rgba(255,255,255,1))}
#tplPrev{color:#31405e;font-weight:700}
@media(max-width:600px){
 .panel .numrow input{width:88px}
}
.fmgr{padding-top:18px;border-top:1px solid rgba(130,150,190,.28);margin-top:30px}
.fmgr>.lbl{display:block;margin-bottom:10px}
.fmgr-btns{display:flex;gap:10px;flex-wrap:wrap}
.fmgr-btns .btn{flex:1;min-width:92px;justify-content:center;text-align:center;padding:12px 10px}
.fmgr-btns .btn.armed{background:rgba(255,120,100,.22);color:#c0392b;
 border-color:rgba(255,120,100,.5);animation:pulse 1s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.75}}
.stackbtn{display:flex;flex-direction:column;gap:10px}
.stackbtn .btn{justify-content:center;text-align:center;padding:13px 16px;width:100%;box-sizing:border-box}
#noteBtn{margin-top:16px}
.hintbox{margin-top:12px}
.hintbox-text{font-size:12.5px;color:#8a97ab;line-height:1.8;margin-bottom:8px}
textarea{width:100%;font-family:inherit;font-size:14px;line-height:1.7;padding:12px;
 border-radius:12px;border:1px solid rgba(255,255,255,.8);background:rgba(255,255,255,.6);
 color:#243049;outline:none;resize:vertical;box-shadow:inset 0 2px 6px rgba(31,45,80,.06)}
textarea:focus{border-color:rgba(90,124,255,.8);box-shadow:0 0 0 3px rgba(90,124,255,.15)}
h3{font-size:16px;font-weight:700;color:#31405e;margin-bottom:12px}
.range{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.list{padding:0;overflow:hidden}
.lhead{display:flex;gap:10px;align-items:center;flex-wrap:wrap;
 padding:14px 18px;border-bottom:1px solid rgba(255,255,255,.6)}
.ltitle{font-size:15px;font-weight:700;color:#31405e;margin-right:auto}
.lbody{max-height:420px;overflow-y:auto;padding:8px}
.lbody::-webkit-scrollbar{width:8px}
.lbody::-webkit-scrollbar-thumb{background:rgba(120,140,180,.35);border-radius:99px}
.row{display:flex;align-items:center;justify-content:space-between;gap:12px;
 padding:13px 16px;border-radius:14px;margin:6px;transition:.2s;
 border:1px solid transparent;background:rgba(255,255,255,.28)}
.row:hover{background:rgba(255,255,255,.65);border-color:rgba(255,255,255,.85);
 box-shadow:0 6px 18px rgba(31,45,80,.10);transform:translateY(-1px)}
.fname{font-weight:600;font-size:15px;word-break:break-all}
.ftag{display:inline-block;font-size:11.5px;font-weight:600;color:#1f6f8b;
 padding:2px 9px;border-radius:99px;margin-left:8px;vertical-align:1px;
 background:rgba(127,227,255,.18);border:1px solid rgba(127,227,255,.45)}
.fsub{color:#71809b;font-size:12.5px;margin-top:4px}
.facts{display:flex;gap:8px;flex-shrink:0}
.empty{text-align:center;color:#8a97ab;font-size:14px;padding:34px 0}
.hint{font-weight:400;font-size:12.5px;color:#8a97ab}
@media(max-width:600px){
 body{padding:16px 12px 40px}
 h1{font-size:25px}
 .row{flex-direction:column;align-items:stretch}
 .facts{justify-content:flex-end}
 .facts .btn{flex:1;justify-content:center;padding:12px 0}
 .panel{padding:14px}
 .lbody{max-height:55vh}
 /* 筛选/批量下载表单: 移动端纵向堆叠, 控件全宽对齐 */
 .bar{flex-direction:column;align-items:stretch}
 .bar form{display:flex;flex-direction:column;align-items:stretch;gap:9px}
 .bar form select,.bar form input[type=date]{width:100%}
 .bar form .lbl{margin-bottom:-3px}
 .bar form button{width:100%;justify-content:center}
 .bar>a.btn{justify-content:center;text-align:center;padding:12px 0}
 .range{flex-direction:column;align-items:stretch}
 .range .lbl{margin-bottom:-3px}
 .range input[type=date]{width:100%}
 .range .cta{width:100%;text-align:center}
 .bar>a.cta{width:100%;text-align:center;justify-content:center;padding:12px 0}
}
/* ===== 上传面板(原生渲染, 与学生上传页同源 CSS, 已去掉 body/wrap 等全局规则) ===== */
#upPane{display:flex;flex-direction:column;gap:18px}
#upPane .announce,#upPane .tool,#upPane .flist,#upPane .total,#upPane .acts,#upPane .msg{margin-top:0;margin-bottom:0}
#upPane .tool{padding:18px 20px}
#upPane .drop{padding:34px 20px}
#upPane .announce{padding:16px 20px}
__UP_CSS__
</style>
<div class="orb a"></div><div class="orb b"></div><div class="orb c"></div>
<div class=wrap>
<div class=topbar>
 <h1>作业管理</h1>
 <div class=ticbtns>
__CHECK_ICON__
  <button type=button class=ticbtn id=btnAnn title="公告栏"><svg class=svgico viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 10v4a1 1 0 0 0 1 1h2l5.5 4V5L7 9H5a1 1 0 0 0-1 1Z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M18.5 6a9 9 0 0 1 0 12"/></svg></button>
  <button type=button class=ticbtn id=btnCfg title="设置"><svg class=svgico viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3.2"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1Z"/></svg></button>
 </div>
</div>
<div class=tabs>
 <button type=button class="tab on" id=tabBtnManage><svg class=svgico viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 7.5a2 2 0 0 1 2-2h3.6l2 2h6.4a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H5.5a2 2 0 0 1-2-2Z"/></svg>文件管理</button>
 <button type=button class=tab id=tabBtnUpload><svg class=svgico viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 15.5V4.5"/><path d="m7.5 9 4.5-4.5L16.5 9"/><path d="M4.5 16v3a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2v-3"/></svg>上传文件</button>
</div>

<div id=tabManage>
<section class="glass panel">
 <div class=bar>
  <form id=ffilter method=get style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
   <input type=hidden name=token value="__TOKEN__">
   <span class=lbl><svg class=svgico viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 7.5a2 2 0 0 1 2-2h3.6l2 2h6.4a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H5.5a2 2 0 0 1-2-2Z"/></svg>文件夹</span>
   <select id=ffolder name=folder>
    <option value="all"__SEL_ALL__>全部文件夹</option>
    <option value="__root__"__SEL_ROOT__>未分类</option>
__FOLDER_OPTS__
   </select>
  </form>
 </div>
 <div class=fmgr>
  <span class=lbl><svg class=svgico viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 7.5a2 2 0 0 1 2-2h3.6l2 2h6.4a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H5.5a2 2 0 0 1-2-2Z"/></svg>文件夹管理（对当前筛选的文件夹生效）</span>
  <div class=fmgr-btns>
   <button type=button class="btn mini" onclick="addFolder()"><svg class=svgico viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14"/><path d="M5 12h14"/></svg>新建文件夹</button>
   <button type=button class="btn mini" onclick="renFolder()"><svg class=svgico viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M16.5 4.5a2.1 2.1 0 0 1 3 3L8 19l-4 1 1-4Z"/></svg>重命名</button>
   <button type=button class="btn mini danger" id=delBtn onclick="delFolder()"><svg class=svgico viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4.5 7h15"/><path d="M9.5 7V5.2a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1V7"/><path d="M6.5 7 7.5 20h9L17.5 7"/><path d="M10 11v5"/><path d="M14 11v5"/></svg>删除</button>
  </div>
  <div class=hintbox>
   <p class=hintbox-text id=hbFmgr style="display:none">重命名不影响文件夹内的文件；删除会连同其中所有文件一并删除</p>
   <button type=button class="btn mini" onclick="toggleHb('hbFmgr',this)">展开说明 ▾</button>
  </div>
 </div>
</section>

<section class="glass panel">
 <h3><svg class=svgico viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4.5v11"/><path d="m7.5 10.5 4.5 4.5 4.5-4.5"/><path d="M4.5 16v3a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2v-3"/></svg>批量下载（按文件夹打包 ZIP）</h3>
 <form method=get action=/admin/range class=range id=rform>
  <input type=hidden name=token value="__TOKEN__">
  <span class=lbl><svg class=svgico viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 7.5a2 2 0 0 1 2-2h3.6l2 2h6.4a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H5.5a2 2 0 0 1-2-2Z"/></svg>文件夹</span>
  <select name=folder id=dfolder>
   <option value="all"__SEL_DL_ALL__>全部文件夹</option>
   <option value="__root__"__SEL_DL_ROOT__>未分类</option>
__DL_FOLDER_OPTS__
  </select>
  <button type=submit class=cta id=rbtn>打包下载 ZIP</button>
 </form>
</section>

<section class="glass list">
 <div class=lhead>
  <span class=ltitle><svg class=svgico viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3.5H7.5a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h9a2 2 0 0 0 2-2V8Z"/><path d="M14 3.5V8h4.5"/></svg>文件列表</span>
  <span class=pill>共 __N__ 份</span>
  __FOLDER_PILL__
 </div>
 <div class=lbody>
__ROWS__
 </div>
</section>
</div>
<div id=tabUpload style="display:none">
__UP_BODY__
__UP_JS__
</div>

<div id=tabAnn style="display:none">
 <section class="glass panel">
  <h3><svg class=svgico viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 10v4a1 1 0 0 0 1 1h2l5.5 4V5L7 9H5a1 1 0 0 0-1 1Z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M18.5 6a9 9 0 0 1 0 12"/></svg>公告栏</h3>
  <p class=sub2>发布后立即显示在同学上传页顶部，支持多行；清空内容再发布 ＝ 撤下公告。</p>
  <form method=post action=/admin/announcement>
   <input type=hidden name=token value="__TOKEN__">
   <input type=hidden name=folder value="__CURFOLDER__">
   <div class=fld>
    <span class=cap>公告内容</span>
    <textarea name=text rows=6 maxlength=2000 placeholder="例如：&#10;请于本周五前上传作业，命名格式：学号+姓名&#10;有特殊情况请直接联系学习委员">__ANNOUNCE_TEXT__</textarea>
   </div>
   <div class=popbtns>
    <button type=submit name=action value=publish class=cta><svg class=svgico viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 10v4a1 1 0 0 0 1 1h2l5.5 4V5L7 9H5a1 1 0 0 0-1 1Z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M18.5 6a9 9 0 0 1 0 12"/></svg>发布 / 更新公告</button>
    <button type=submit name=action value=clear class="btn danger"><svg class=svgico viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4.5 7h15"/><path d="M9.5 7V5.2a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1V7"/><path d="M6.5 7 7.5 20h9L17.5 7"/><path d="M10 11v5"/><path d="M14 11v5"/></svg>撤下公告</button>
   </div>
   <p class=note>当前状态：<b>__ANNOUNCE_STATUS__</b></p>
  </form>
 </section>
</div>

<div id=tabCfg style="display:none">
 <section class="glass panel">
  <h3><svg class=svgico viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3.2"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1Z"/></svg>上传设置</h3>
  <p class=sub2>名单识别用于作业核对与重复提交提示；自动规范化用于上传后按模板改名。关掉开关，管理页对应功能入口随之隐藏。</p>
  <form method=post action=/admin/settings id=cfgform>
   <input type=hidden name=token value="__TOKEN__">
   <input type=hidden name=folder value="__CURFOLDER__">
   <div class=sw>
    <label><input type=checkbox name=recognize value=1 id=recOn __RECON__> 启用「名单识别」（作业核对 · 重复提交提示）</label>
   </div>
   <div class=sw>
    <label><input type=checkbox name=normalize value=1 id=normOn __NORMON__> 启用「自动规范化文件名」（按模板改名）</label>
   </div>
   <div class=fld>
    <span class=cap>命名模板</span>
    <input type=text name=template id=tpl value="__TPL__" maxlength=120
     placeholder="输入关键字与连接符号, 如: 姓名_学号 文件名称">
    <div class=chips>
     <button type=button class="btn mini" onclick="insTpl('姓名')">＋姓名</button>
     <button type=button class="btn mini" onclick="insTpl('学号')">＋学号</button>
     <button type=button class="btn mini" onclick="insTpl('文件名称')">＋文件名称</button>
     <button type=button class="btn mini" onclick="insTpl('日期')">＋日期</button>
    </div>
   </div>
   <div class=fld>
    <span class=cap>单个最大传输文件</span>
    <div class=numrow>
     <input type=number name=max_mb min=10 max=5000 value="__MAX_MB_CFG__">
     <span class=unit>MB</span><span class=rng>（10–5000）</span>
    </div>
   </div>
   <div class=fld>
    <span class=cap>单次最多传输文件数量</span>
    <div class=numrow>
     <input type=number name=max_files min=1 max=999 value="__MAX_FILES_CFG__">
     <span class=unit>个</span><span class=rng>（1–999）</span>
    </div>
   </div>
   <button type=submit class=cta style="width:100%;margin-top:6px;text-align:center;justify-content:center">保存设置</button>
   <p class=note id=cfgNote style="display:none">模板里写 <b>姓名 / 学号 / 文件名称 / 日期</b> 四个关键字（可增删、自由排序），中间加任意连接符号；<b>日期为当天</b>（年.月.日）。例：「姓名_学号 文件名称」→「张三_17 example」；「学号-姓名-文件名称」→「17-张三-example」。<br><b>实时预览：</b><span id=tplPrev></span><br>关闭开关后：上传保持原名（同名仍自动加 (1) 防覆盖），对应功能入口从管理页隐藏；「作业核对」只受名单识别开关影响。<br>「单个大小 / 单次数量」只限制<b>同学端</b>（服务端不限制）；<b>管理端上传不受任何限制</b>。</p>
   <button type=button class="btn mini" id=noteBtn onclick="toggleNote()">展开说明 ▾</button>
  </form>
 </section>
 <section class="glass panel">
  <h3><svg class=svgico viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="4.5" width="12" height="16" rx="2.2"/><path d="M9.5 3.2h5a1.2 1.2 0 0 1 1.2 1.2V6H8.3V4.4a1.2 1.2 0 0 1 1.2-1.2Z"/><path d="M9 10h6"/><path d="M9 13.5h6"/><path d="M9 17h4"/></svg>名单与规范设置</h3>
  <p class=sub2>应交名单：__ROSTER_STATUS__</p>
  <div class=stackbtn>
   <a class="btn" href="/admin/roster?token=__TOKEN__&folder=__CURFOLDER__"><svg class=svgico viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M16.5 4.5a2.1 2.1 0 0 1 3 3L8 19l-4 1 1-4Z"/></svg>编辑名单</a>
__NORM_BTN__
  </div>
  <p class=hintbox-text style="margin-top:14px">名单用于核对谁交了、识别重复提交；「规范文件名」按上面的命名模板批量改名历史文件，识别不出身份的文件不会动。</p>
 </section>
</div>

</div>
__WARN__
<script>
const T='__TOKEN__';
const ffolder=document.getElementById('ffolder');
// 文件夹变化即自动筛选
ffolder.onchange=()=>document.getElementById('ffilter').submit();
function goBack(act,name){
 let u='/admin/folder/'+act+'?token='+T
  +'&folder='+encodeURIComponent(ffolder.value);
 if(name)u+='&name='+encodeURIComponent(name);
 location=u;}
function addFolder(){const n=prompt('新文件夹名称：');
 if(n&&n.trim())goBack('add',n.trim());}
function renFolder(){const f=ffolder.value;
 if(f==='all'||f==='__root__'){alert('请先在筛选中选择要重命名的具体文件夹');return;}
 const n=prompt('把文件夹「'+f+'」重命名为：',f);
 if(!n||!n.trim()||n.trim()===f)return;
 location='/admin/folder/rename?token='+T
  +'&name='+encodeURIComponent(f)
  +'&new='+encodeURIComponent(n.trim())
  +'&folder='+encodeURIComponent(f);}
const TRASH_ICO='<svg class=svgico viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4.5 7h15"/><path d="M9.5 7V5.2a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1V7"/><path d="M6.5 7 7.5 20h9L17.5 7"/><path d="M10 11v5"/><path d="M14 11v5"/></svg>';
const WARN_ICO='<svg class=svgico viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4.5 2.8 20h18.4Z"/><path d="M12 10v4"/><path d="M12 17.2v.1"/></svg>';
let delArm=false,delT=null;
function delFolder(){
 const f=ffolder.value;
 if(f==='all'||f==='__root__'){alert('请先在筛选中选择要删除的具体文件夹');return;}
 const b=document.getElementById('delBtn');
 if(!delArm){  // 第一步: 只进入待确认状态, 不删任何东西
  delArm=true;b.innerHTML=WARN_ICO+'再点一次确认';b.classList.add('armed');
  delT=setTimeout(()=>{delArm=false;b.innerHTML=TRASH_ICO+'删除';b.classList.remove('armed');},3000);
  return;}
 clearTimeout(delT);delArm=false;b.innerHTML=TRASH_ICO+'删除';b.classList.remove('armed');
 // 第二步: 再弹窗确认(显示文件夹名, 防删错)
 if(confirm('删除文件夹「'+f+'」会连同其中所有文件一并删除，确定？'))goBack('delete',f);
}
// 提示说明 展开/缩回(通用): 折叠时完全隐藏文字, 只露按钮
function toggleHb(id,btn){
 const t=document.getElementById(id);
 const show=(t.style.display==='none');
 t.style.display=show?'block':'none';
 btn.textContent=show?'缩回说明 ▴':'展开说明 ▾';
}
// 批量下载: 先 fetch 预检, 无文件就在当前页弹窗(不跳转不白屏), 有文件才触发下载
const rform=document.getElementById('rform'),rbtn=document.getElementById('rbtn');
rform.onsubmit=e=>{
 e.preventDefault();
 const q=new URLSearchParams(new FormData(rform)).toString();
 rbtn.disabled=true;
 fetch('/admin/zipcheck?'+q).then(r=>r.json()).then(d=>{
  rbtn.disabled=false;
  if(d.total===0){alert(d.msg||'该文件夹下没有文件，未执行下载。');}
  else if(d.total<0){alert(d.msg||'参数错误');}
  else{location.href='/admin/range?'+q;}
 }).catch(()=>{rbtn.disabled=false;
  alert('网络异常，无法确认文件数量，请重试');});
};
// 页面切换: TAB(文件管理/上传文件) + 标题右侧小按钮(公告栏/设置), 全部切页
const tabManage=document.getElementById('tabManage'),tabUpload=document.getElementById('tabUpload'),
 tabAnn=document.getElementById('tabAnn'),tabCfg=document.getElementById('tabCfg'),
 bM=document.getElementById('tabBtnManage'),bU=document.getElementById('tabBtnUpload'),
 btnAnn=document.getElementById('btnAnn'),btnCfg=document.getElementById('btnCfg');
function showTab(w){
 const pages={manage:tabManage,upload:tabUpload,ann:tabAnn,cfg:tabCfg};
 for(const k in pages){pages[k].style.display=(k===w)?'block':'none';}
 bM.classList.toggle('on',w==='manage');
 bU.classList.toggle('on',w==='upload');
 btnAnn.classList.toggle('on',w==='ann');
 btnCfg.classList.toggle('on',w==='cfg');
 if(w==='manage')location.reload();  // 切回管理页时刷新, 看到最新文件
}
bM.onclick=()=>showTab('manage');
bU.onclick=()=>showTab('upload');
btnAnn.onclick=()=>showTab('ann');
btnCfg.onclick=()=>showTab('cfg');
// 设置页: 名单识别关 → 自动规范化 checkbox 禁用(规范化依赖识别)
const recOn=document.getElementById('recOn'),normOn=document.getElementById('normOn');
if(recOn&&normOn){
 const syncNorm=()=>{normOn.disabled=!recOn.checked;};
 recOn.onchange=syncNorm;syncNorm();
}
// 说明文字 展开/缩回
function toggleNote(){
 const n=document.getElementById('cfgNote'),b=document.getElementById('noteBtn');
 const show=(n.style.display==='none');
 n.style.display=show?'block':'none';
 b.textContent=show?'缩回说明 ▴':'展开说明 ▾';
}


// 命名模板: 元素快捷插入 + 实时预览(与服务端 render_template 逻辑一致)
const tplIn=document.getElementById('tpl'),tplPrev=document.getElementById('tplPrev');
function insTpl(k){tplIn.value+=k;updTpl();tplIn.focus();}
function updTpl(){
 const d=new Date();
 const today=d.getFullYear()+'.'+(d.getMonth()+1)+'.'+d.getDate();
 const s=(tplIn.value||'').replace(/姓名/g,'张三').replace(/学号/g,'20260000000001')
  .replace(/文件名称/g,'example').replace(/日期/g,today);
 tplPrev.textContent=s||'(模板为空)';
}
if(tplIn){tplIn.oninput=updTpl;updTpl();}
</script>
</body></html>""")

# ---------- 应交名单编辑页 ----------
ROSTER_TPL = ("<!doctype html><html lang=zh><meta charset=utf-8>\n"
"<meta name=viewport content=\"width=device-width,initial-scale=1\">\n"
"<title>应交名单</title><style>" + GLASS_CSS + """
body{padding:26px 18px 48px}
.wrap{position:relative;z-index:1;max-width:640px;margin:0 auto}
h1{font-size:30px;font-weight:800;margin-bottom:6px;
 background:linear-gradient(120deg,#2b3a67,#5b4b9e 55%,#1f6f8b);
 -webkit-background-clip:text;background-clip:text;color:transparent}
.sub{color:#5a6a81;font-size:14px;margin-bottom:18px}
.sub b{color:#31405e}
.panel{padding:18px 20px;margin-bottom:16px}
textarea{width:100%;font-family:inherit;font-size:15px;line-height:1.9;padding:14px;
 border-radius:14px;border:1px solid rgba(255,255,255,.8);background:rgba(255,255,255,.6);
 color:#243049;outline:none;resize:vertical;min-height:300px;
 box-shadow:inset 0 2px 6px rgba(31,45,80,.06)}
textarea:focus{border-color:rgba(90,124,255,.8);box-shadow:0 0 0 3px rgba(90,124,255,.15)}
.acts{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.hint{font-size:13px;color:#8a97ab;line-height:1.9;margin-top:14px}
.hint b{color:#5a6a81}
@media(max-width:600px){
 body{padding:16px 12px 40px}
 h1{font-size:25px}
 .acts .cta{flex:1;text-align:center;justify-content:center}
 .acts .btn{flex:1;justify-content:center;text-align:center}
}
</style>
<div class="orb a"></div><div class="orb b"></div><div class="orb c"></div>
<div class=wrap>
<h1>应交名单</h1>
<p class=sub>当前名单共 <b>__CNT__</b> 人，其中 <b>__IDCNT__</b> 人有学号 · 每行一个同学，格式「姓名 学号」或「学号 姓名」</p>
<form method=post action=/admin/roster>
 <input type=hidden name=token value="__TOKEN__">
 <input type=hidden name=folder value="__FOLDER__">
 <div class="glass panel"><textarea name=names placeholder="每行一个同学，例如：&#10;张君凯 20261108100134&#10;20261108100135 张娟">__NAMES__</textarea></div>
 <div class=acts>
  <button type=submit class=cta>💾 保存名单</button>
  <a class=btn href="/admin?token=__TOKEN__&__BACK__">← 返回管理页</a>
 </div>
 <p class=hint><b>为什么需要学号：</b>上传的文件会被自动识别是谁交的（按姓名匹配），然后重命名为「学号 姓名」的标准格式——学号从这份名单里取，所以同学文件名里学号写错都能自动纠正。<b>只有姓名没有学号时，自动重命名不启用</b>（核对功能不受影响）。<br><b>核对规则：</b>点「开始核对」后，系统按当前筛选的文件夹把每个文件名与名单比对。同学们的命名是「学号 + 姓名」，分隔符不限（连写、空格、+、_ 都认）。系统先精确比对姓名部分，比对不上的再用「姓名是否出现在文件名里」兜底；两种都没匹配上的文件会列入「无法识别」，由你人工确认。</p>
</form>
</div>
</body></html>""")

# ---------- 作业核对报告页 ----------
CHECK_TPL = ("<!doctype html><html lang=zh><meta charset=utf-8>\n"
"<meta name=viewport content=\"width=device-width,initial-scale=1\">\n"
"<title>作业核对报告</title><style>" + GLASS_CSS + """
body{padding:26px 18px 48px}
.wrap{position:relative;z-index:1;max-width:780px;margin:0 auto}
h1{font-size:30px;font-weight:800;margin-bottom:6px;
 background:linear-gradient(120deg,#2b3a67,#5b4b9e 55%,#1f6f8b);
 -webkit-background-clip:text;background-clip:text;color:transparent}
.sub{color:#5a6a81;font-size:14px;margin-bottom:18px;line-height:1.7}
.panel{padding:18px 20px;margin-bottom:18px}
h3{font-size:16px;font-weight:700;color:#31405e;margin-bottom:12px}
.stat{display:flex;gap:28px;flex-wrap:wrap}
.stat .num{font-size:32px;font-weight:800;line-height:1.1}
.stat .cap{font-size:13px;color:#71809b;margin-top:3px}
.bar2{height:10px;border-radius:99px;background:rgba(120,140,180,.18);overflow:hidden;margin-top:14px}
.bar2>i{display:block;height:100%;border-radius:99px;
 background:linear-gradient(90deg,#2e7d5b,#6fd39c)}
.miss{display:flex;flex-wrap:wrap;gap:9px}
.mtag{font-weight:700;font-size:14.5px;color:#c0392b;padding:8px 15px;border-radius:12px;
 background:rgba(255,120,100,.12);border:1px solid rgba(255,120,100,.4)}
.okrow{display:flex;justify-content:space-between;align-items:center;gap:12px;
 padding:11px 15px;border-radius:12px;margin:6px;
 background:rgba(46,125,91,.07);border:1px solid rgba(46,125,91,.18)}
.okrow.multi{background:rgba(255,190,80,.10);border-color:rgba(255,190,80,.45)}
.okrow .nm{font-weight:700;font-size:14.5px}
.okrow .cnt{font-size:12px;color:#71809b;font-weight:600;margin-left:6px}
.okrow .fl{font-size:12.5px;color:#71809b;word-break:break-all;text-align:right;max-width:55%}
.unkrow{display:flex;justify-content:space-between;gap:10px;align-items:center;
 padding:10px 15px;border-radius:12px;margin:6px;
 background:rgba(255,255,255,.3);border:1px solid rgba(255,255,255,.6)}
.unkrow .fn{font-size:13.5px;font-weight:600;word-break:break-all}
.ftag{display:inline-block;font-size:11.5px;font-weight:600;color:#1f6f8b;
 padding:2px 9px;border-radius:99px;flex-shrink:0;
 background:rgba(127,227,255,.18);border:1px solid rgba(127,227,255,.45)}
.allok{text-align:center;font-weight:700;font-size:15px;padding:16px 0}
.allok.g{color:#2e7d5b}.allok.b{color:#3a5bcc}
.foot{font-size:12.5px;color:#8a97ab;line-height:1.8;margin-top:6px}
@media(max-width:600px){
 body{padding:16px 12px 40px}
 h1{font-size:25px}
 .okrow{flex-direction:column;align-items:flex-start;gap:4px}
 .okrow .fl{text-align:left;max-width:100%}
 .unkrow{flex-direction:column;align-items:flex-start;gap:4px}
 .stat{gap:20px}
}
</style>
<div class="orb a"></div><div class="orb b"></div><div class="orb c"></div>
<div class=wrap>
<h1>作业核对报告</h1>
<p class=sub>核对范围：__SCOPE____NOTE__</p>

<section class="glass panel">
 <h3><svg class=svgico viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 20h18"/><path d="M6 20v-6"/><path d="M11 20V8"/><path d="M16 20v-9"/></svg>总览</h3>
 <div class=stat>
  <div><div class=num>__N__</div><div class=cap>应交人数</div></div>
  <div><div class=num style="color:#2e7d5b">__OK__</div><div class=cap>已交</div></div>
  <div><div class=num style="color:#c0392b">__MISS__</div><div class=cap>未交</div></div>
 </div>
 <div class=bar2><i style="width:__PCT__%"></i></div>
</section>

<section class="glass panel">
 <h3><svg class=svgico style="color:#c0392b" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8.5"/><path d="m9 9 6 6"/><path d="m15 9-6 6"/></svg>未交名单（__MISS__ 人）</h3>
 __MISSBOX__
</section>

<section class="glass panel">
 <h3><svg class=svgico style="color:#2e7d5b" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8.5"/><path d="m8.5 12 2.4 2.4 4.6-5"/></svg>已交明细（__OK__ 人）</h3>
 __OKBOX__
</section>

<section class="glass panel">
 <h3><svg class=svgico style="color:#b8860b" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4.5 2.8 20h18.4Z"/><path d="M12 10v4"/><path d="M12 17.2v.1"/></svg>无法识别的文件（__UNK__ 个）</h3>
 __UNKBOX__
 <p class=foot>无法识别 = 文件名里匹配不到任何名单姓名，可能是文件名漏写名字、写错名字，或名单外的人传的——需要你人工确认。</p>
</section>

<div class=acts style="display:flex;gap:10px;flex-wrap:wrap">
 <a class=btn href="/admin?token=__TOKEN__&__BACK__">← 返回管理页</a>
</div>
</div>
</body></html>""")

# ---------- 文件名规范化预览页 ----------
NORMALIZE_TPL = ("<!doctype html><html lang=zh><meta charset=utf-8>\n"
"<meta name=viewport content=\"width=device-width,initial-scale=1\">\n"
"<title>规范文件名</title><style>" + GLASS_CSS + """
body{padding:26px 18px 48px}
.wrap{position:relative;z-index:1;max-width:780px;margin:0 auto}
h1{font-size:30px;font-weight:800;margin-bottom:6px;
 background:linear-gradient(120deg,#2b3a67,#5b4b9e 55%,#1f6f8b);
 -webkit-background-clip:text;background-clip:text;color:transparent}
.sub{color:#5a6a81;font-size:14px;margin-bottom:18px;line-height:1.7}
.panel{padding:18px 20px;margin-bottom:18px}
h3{font-size:16px;font-weight:700;color:#31405e;margin-bottom:12px}
.nrow{display:flex;align-items:center;gap:10px;padding:11px 14px;border-radius:12px;margin:6px;
 background:rgba(46,125,91,.06);border:1px solid rgba(46,125,91,.18);flex-wrap:wrap}
.nrow .old{font-size:13px;color:#8a97ab;word-break:break-all;text-decoration:line-through}
.nrow .arrow{color:#2e7d5b;font-weight:800;flex-shrink:0}
.nrow .new{font-size:13.5px;font-weight:700;color:#243049;word-break:break-all}
.srow{padding:9px 14px;border-radius:12px;margin:5px;
 background:rgba(255,255,255,.3);border:1px solid rgba(255,255,255,.6);
 font-size:13px;color:#5a6a81;word-break:break-all}
.srow .rsn{font-size:12px;color:#a5b0c2;margin-left:8px}
.stat{display:flex;gap:22px;flex-wrap:wrap;margin-bottom:12px}
.stat .num{font-size:26px;font-weight:800}
.stat .cap{font-size:12.5px;color:#71809b;margin-top:2px}
.allok{text-align:center;font-weight:700;font-size:15px;padding:16px 0;color:#2e7d5b}
.warnbox{background:rgba(255,190,80,.10);border:1px solid rgba(255,190,80,.45);
 border-radius:14px;padding:14px 16px;font-size:13.5px;color:#8a6d1a;line-height:1.8;margin-bottom:18px}
.acts{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
@media(max-width:600px){
 body{padding:16px 12px 40px}
 h1{font-size:25px}
 .nrow{flex-direction:column;align-items:flex-start;gap:4px}
 .acts .cta{flex:1;text-align:center;justify-content:center}
 .acts .btn{flex:1;justify-content:center;text-align:center}
}
</style>
<div class="orb a"></div><div class="orb b"></div><div class="orb c"></div>
<div class=wrap>
<h1>规范文件名</h1>
<p class=sub>__SCOPE____NOTE__</p>
__WARNBOX__
<section class="glass panel">
 <h3><svg class=svgico viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 20h18"/><path d="M6 20v-6"/><path d="M11 20V8"/><path d="M16 20v-9"/></svg>总览</h3>
 <div class=stat>
  <div><div class=num>__NREN__</div><div class=cap>将重命名</div></div>
  <div><div class=num style="color:#8a97ab">__NSKIP__</div><div class=cap>保持不变</div></div>
 </div>
</section>
<section class="glass panel">
 <h3><svg class=svgico viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M16.5 4.5a2.1 2.1 0 0 1 3 3L8 19l-4 1 1-4Z"/></svg>重命名清单（__NREN__ 个）</h3>
 __RENBOX__
</section>
__SKIPSEC__
<div class=acts>
 __GOBTN__
 <a class=btn href="/admin?token=__TOKEN__&__BACK__">← 返回管理页</a>
</div>
</div>
</body></html>""")

def folder_options(cur: str) -> str:
    rows = ""
    for d in list_folders():
        sel = " selected" if cur == d else ""
        rows += f'    <option value="{html.escape(d)}"{sel}>{html.escape(d)}</option>\n'
    return rows.rstrip("\n")

@app.get("/admin")
def admin():
    if not is_admin():
        abort(401)
    folder = norm_folder(request.args.get("folder"))
    files = []
    for rel, fn, p, mt, sz in iter_all_files():
        if not match_folder(rel, folder):
            continue
        files.append((rel, fn, p, mt, sz))
    rows = ""
    for rel, fn, p, mt, sz in files:
        rel_path = (rel + "/" + fn) if rel else fn
        href_dl = "/admin/download/" + quote(rel_path)
        href_del = "/admin/delete/" + quote(rel_path)
        tag = (f'<span class=ftag>{html.escape(rel)}</span>' if rel
               else '<span class=ftag>未分类</span>')
        rows += (f"<div class=row>"
                 f"<div class=fmeta><div class=fname>{html.escape(fn)}{tag}</div>"
                 f"<div class=fsub>{fmt_sz(sz)} · 上传于 {time.strftime('%Y-%m-%d %H:%M', time.localtime(mt))}</div></div>"
                 f"<div class=facts>"
                 f"<a class=btn href='{href_dl}?token={ADMIN_TOKEN}'><svg class='svgico' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'><path d='M12 4.5v11'/><path d='m7.5 10.5 4.5 4.5 4.5-4.5'/><path d='M4.5 16v3a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2v-3'/></svg>下载</a>"
                 f"<a class=\"btn danger\" href='{href_del}?token={ADMIN_TOKEN}&folder={quote(folder)}' onclick=\"return confirm('确定删除该文件?')\">删除</a>"
                 f"</div></div>")
    if not files:
        if folder != "all":
            fdesc = {"__root__": "未分类"}.get(folder, folder)
            rows = ('<div class=empty>当前文件夹下暂无文件<br>'
                    '<small style="color:#a5b0c2">筛选条件：文件夹 = ' + html.escape(fdesc)
                    + '<br>文件并未丢失，在「文件夹」下拉里选「全部文件夹」即可查看所有文件</small></div>')
        else:
            rows = "<div class=empty>这里空空如也，还没有任何文件</div>"
    fp = {"all": ("", ""), "__root__": ("未分类", "pill folder")}.get(
        folder, (folder, "pill folder"))
    folder_pill = (f'<span class="pill {fp[1]}">{html.escape(fp[0])}</span>'
                   if fp[0] else "")
    sel_all = " selected" if folder == "all" else ""
    sel_root = " selected" if folder == "__root__" else ""
    # 批量下载"无文件"等提示: 回到本页后以 alert 弹窗呈现, 不跳独立页面
    warn = (request.args.get("warn") or "").strip()[:300]
    warn_js = ("<script>alert(" + json.dumps(warn) + ");"
               "history.replaceState(null,'',location.pathname+'?token="
               + ADMIN_TOKEN + "');</script>") if warn else ""
    roster = load_roster()
    roster_pairs = load_roster_pairs()
    id_cnt = sum(1 for _, s in roster_pairs if s)
    roster_status = (f"共 {len(roster)} 人（{id_cnt} 人有学号）" if roster
                     else "未设置（点「编辑名单」导入）")
    ann = load_announcement()
    ann_text = ann["text"] if ann else ""
    ann_status = (f"已发布（{ann['time']}，同学端可见）" if ann else "暂无公告")
    _cfg = load_config()
    _rec = _cfg.get("recognize", True)
    _norm = _cfg.get("normalize", True) and _rec
    norm_btn = (f'   <a class="btn" href="/admin/normalize?token={ADMIN_TOKEN}'
                f'&folder={quote(folder)}"><svg class=svgico viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3.5a9 9 0 0 1 1.8 4.7A9 9 0 0 1 18.5 10a9 9 0 0 1-4.7 1.8A9 9 0 0 1 12 16.5a9 9 0 0 1-1.8-4.7A9 9 0 0 1 5.5 10a9 9 0 0 1 4.7-1.8A9 9 0 0 1 12 3.5Z"/><path d="M18.5 16.5c.3.9.9 1.5 1.8 1.8-.9.3-1.5.9-1.8 1.8-.3-.9-.9-1.5-1.8-1.8.9-.3 1.5-.9 1.8-1.8Z"/></svg>规范文件名</a>' if _norm else '')
    _chk_svg = ('<svg class=svgico viewBox="0 0 24 24" fill="none" stroke="currentColor" '
                'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
                '<rect x="6" y="4.5" width="12" height="16" rx="2.2"/>'
                '<path d="M9.5 3.2h5a1.2 1.2 0 0 1 1.2 1.2V6H8.3V4.4a1.2 1.2 0 0 1 1.2-1.2Z"/>'
                '<path d="m9 13 2 2 4-4.5"/></svg>')
    check_icon = (f'<button type=button class=ticbtn id=btnCheck title="作业核对" '
                  f'onclick="location=\'/admin/check?token={ADMIN_TOKEN}'
                  f'&folder={quote(folder)}\'">{_chk_svg}</button>'
                  if _rec else '')
    if not _rec:
        check_hint = ('名单识别已在「设置」里关闭——开启后右上角出现核对入口，'
                      '同学上传时也才有重复提交提示。')
    elif not _norm:
        check_hint = ('按当前筛选的文件夹核对谁没交；自动规范化已关闭（上传的文件保持原名，'
                      '同名加 (1)），所以在「设置」里开启后才有「规范文件名」批量入口。')
    else:
        check_hint = ('点右上角的核对图标查看当前文件夹的核对报告（谁没交/已交明细）；'
                      '同学上传后若识别出是谁（且名单有其学号），文件会按命名模板自动改名，'
                      '「规范文件名」可批量处理历史文件')
    up_css, up_body, up_js = upload_pane_parts()
    page = (ADMIN_TPL
            .replace("__N__", str(len(files)))
            .replace("__ROSTER_STATUS__", roster_status)
            .replace("__RECON__", " checked" if _cfg.get("recognize") else "")
            .replace("__NORMON__", " checked" if _cfg.get("normalize") else "")
            .replace("__NORM_BTN__", norm_btn)
            .replace("__CHECK_ICON__", check_icon)
            .replace("__CHECK_HINT__", check_hint)
            .replace("__UP_CSS__", up_css)
            .replace("__UP_BODY__", up_body)
            .replace("__UP_JS__", up_js)
            .replace("__TPL__", html.escape(load_config()["template"]))
            .replace("__MAX_MB_CFG__", str(load_config()["max_mb"]))
            .replace("__MAX_FILES_CFG__", str(load_config()["max_files"]))
            .replace("__ANNOUNCE_TEXT__", html.escape(ann_text))
            .replace("__ANNOUNCE_STATUS__", ann_status)
            .replace("__WARN__", warn_js)
            .replace("__FOLDER_PILL__", folder_pill)
            .replace("__CURFOLDER__", folder)
            .replace("__SEL_ALL__", sel_all)
            .replace("__SEL_ROOT__", sel_root)
            .replace("__SEL_DL_ALL__", sel_all)
            .replace("__SEL_DL_ROOT__", sel_root)
            .replace("__FOLDER_OPTS__", folder_options(folder))
            .replace("__DL_FOLDER_OPTS__", folder_options(folder))
            .replace("__ROWS__", rows)
            .replace("__TOKEN__", ADMIN_TOKEN))
    return no_store(Response(page, mimetype="text/html"))

def _back_query():
    """构造带回筛选状态的跳转参数"""
    folder = norm_folder(request.args.get("folder"))
    return f"folder={quote(folder)}"

@app.post("/admin/announcement")
def admin_announcement():
    """发布/更新/撤下公告; 发布后立即显示在同学上传页顶部"""
    if not is_admin():
        abort(401)
    if (request.form.get("action") or "publish") == "clear":
        save_announcement("")   # 空内容 = 撤下
    else:
        save_announcement(request.form.get("text") or "")
    folder = norm_folder(request.form.get("folder"))
    return redirect(f"/admin?token={ADMIN_TOKEN}&folder={quote(folder)}")

@app.post("/admin/settings")
def admin_settings():
    """保存上传设置: 自动规范化开关 + 命名模板 + 客户端上限"""
    if not is_admin():
        abort(401)
    cfg = load_config()
    cfg["recognize"] = (request.form.get("recognize") == "1")
    cfg["normalize"] = (request.form.get("normalize") == "1") and cfg["recognize"]
    tpl = (request.form.get("template") or "").strip()[:120]
    if template_valid(tpl):
        cfg["template"] = tpl
    cfg["max_mb"] = _clamp(request.form.get("max_mb"), 10, 5000, cfg["max_mb"])
    cfg["max_files"] = _clamp(request.form.get("max_files"), 1, 999, cfg["max_files"])
    save_config(cfg)
    folder = norm_folder(request.form.get("folder"))
    return redirect(f"/admin?token={ADMIN_TOKEN}&folder={quote(folder)}")

@app.get("/admin/folder/add")
def folder_add():
    if not is_admin():
        abort(401)
    name = safe_name((request.args.get("name") or "").strip())[:32]
    if name and name not in list_folders():
        os.makedirs(os.path.join(UPLOAD_DIR, name), exist_ok=True)
    return redirect(f"/admin?token={ADMIN_TOKEN}&{_back_query()}")

@app.get("/admin/folder/delete")
def folder_delete():
    if not is_admin():
        abort(401)
    name = safe_name((request.args.get("name") or "").strip())[:32]
    if name and name in list_folders():
        # 删除文件夹时, 其中所有文件一并删除
        shutil.rmtree(os.path.join(UPLOAD_DIR, name), ignore_errors=True)
    return redirect(f"/admin?token={ADMIN_TOKEN}&{_back_query()}")

@app.get("/admin/folder/rename")
def folder_rename():
    """重命名文件夹: 只改目录名, 里面的原封不动"""
    if not is_admin():
        abort(401)
    old = safe_name((request.args.get("name") or "").strip())[:32]
    new = safe_name((request.args.get("new") or "").strip())[:32]
    if old and new and old != new and old in list_folders() \
       and new not in list_folders():
        os.rename(os.path.join(UPLOAD_DIR, old), os.path.join(UPLOAD_DIR, new))
        # 重命名成功后, 筛选自动切到新名字
        return redirect(f"/admin?token={ADMIN_TOKEN}&folder={quote(new)}")
    msg = "重命名失败：新名称不合法或已存在同名文件夹"
    return redirect(f"/admin?token={ADMIN_TOKEN}&folder={quote(old)}"
                    f"&warn={quote(msg)}")

@app.get("/admin/roster")
def admin_roster():
    """应交名单编辑页"""
    if not is_admin():
        abort(401)
    folder = norm_folder(request.args.get("folder"))
    pairs = load_roster_pairs()
    text = "\n".join(f"{n}\t{s}" if s else n for n, s in pairs)
    page = (ROSTER_TPL
            .replace("__CNT__", str(len(pairs)))
            .replace("__IDCNT__", str(sum(1 for _, s in pairs if s)))
            .replace("__NAMES__", html.escape(text))
            .replace("__TOKEN__", ADMIN_TOKEN)
            .replace("__FOLDER__", folder)
            .replace("__BACK__", f"folder={quote(folder)}"))
    return no_store(Response(page, mimetype="text/html"))

@app.post("/admin/roster")
def admin_roster_save():
    """保存应交名单(姓名+学号; 换行/逗号/分号分隔均可, 自动去重)"""
    if not is_admin():
        abort(401)
    pairs = parse_roster(request.form.get("names") or "")
    with open(ROSTER_FILE, "w") as f:
        f.write("\n".join(f"{n}\t{s}" if s else n for n, s in pairs))
    folder = norm_folder(request.form.get("folder"))
    return redirect(f"/admin?token={ADMIN_TOKEN}&folder={quote(folder)}")

@app.get("/admin/check")
def admin_check():
    """作业核对: 按当前文件夹比对名单, 输出已交/未交/无法识别"""
    if not is_admin():
        abort(401)
    folder = norm_folder(request.args.get("folder"))
    roster = load_roster()
    if not roster:
        return no_store(Response(
            "<!doctype html><meta charset=utf-8><script>alert("
            + json.dumps("还没有设置应交名单。请先回到管理页点「编辑名单」导入全班姓名。")
            + ");location='/admin?token=" + ADMIN_TOKEN + "';</script>",
            mimetype="text/html"))
    files = []
    for rel, fn, p, mt, sz in iter_all_files():
        if not match_folder(rel, folder):
            continue
        files.append((rel, fn, p, mt, sz))
    submitted, matched, unknown = match_roster(roster, files)
    n, ok, miss, unk = len(roster), len(submitted), 0, len(unknown)
    missing = [x for x in roster if x not in submitted]
    miss = len(missing)
    pct = round(ok / n * 100) if n else 0
    if missing:
        missbox = ('<div class=miss>' + "".join(
            f'<span class=mtag>🚫 {html.escape(x)}</span>' for x in missing) + '</div>')
    else:
        missbox = '<div class="allok g">全部交齐，无一遗漏！</div>'
    okrows = ""
    for name in roster:
        if name in submitted:
            fl = submitted[name]
            cls = "okrow multi" if len(fl) > 1 else "okrow"
            fl_html = "<br>".join(f"{html.escape(x)}" for x in fl)
            okrows += (f'<div class="{cls}"><span class=nm>{html.escape(name)}'
                       f'<span class=cnt>{len(fl)} 个文件</span></span>'
                       f'<span class=fl>{fl_html}</span></div>')
    okbox = okrows or '<div class="allok b">当前范围内还没有任何人交文件</div>'
    if unknown:
        unkbox = "".join(
            f'<div class=unkrow><span class=fn>{html.escape(fn)}</span>'
            f'<span class=ftag>{html.escape(rel) if rel else "未分类"}</span></div>'
            for rel, fn in unknown)
    else:
        unkbox = '<div class="allok g">所有文件都能对应到名单中的同学</div>'
    fdesc = {"all": "全部文件夹", "__root__": "未分类"}.get(folder, folder)
    scope = f"文件夹：{html.escape(fdesc)}"
    note = ('<br><span style="color:#b8860b;font-size:13px">⚠️ 当前文件夹下没有任何文件，因此所有人都计入未交</span>'
            if not files else "")
    page = (CHECK_TPL
            .replace("__SCOPE__", scope)
            .replace("__NOTE__", note)
            .replace("__N__", str(n))
            .replace("__OK__", str(ok))
            .replace("__MISS__", str(miss))
            .replace("__PCT__", str(pct))
            .replace("__UNK__", str(unk))
            .replace("__MISSBOX__", missbox)
            .replace("__OKBOX__", okbox)
            .replace("__UNKBOX__", unkbox)
            .replace("__TOKEN__", ADMIN_TOKEN)
            .replace("__BACK__", f"folder={quote(folder)}"))
    return no_store(Response(page, mimetype="text/html"))

def folder_files(folder: str):
    """收集某文件夹(或全部)下的所有文件 [(rel, fn, path)]; all 时含各子目录"""
    return [(rel, fn, p) for rel, fn, p, mt, sz in iter_all_files()
            if match_folder(rel, folder)]

@app.get("/admin/zipcheck")
def admin_zipcheck():
    """轻量预检: 前端点打包下载先 fetch 它, 无文件时就地 alert, 不发生任何跳转"""
    if not is_admin():
        abort(401)
    folder = norm_folder(request.args.get("folder"))
    fs = folder_files(folder)
    fdesc = {"all": "全部文件夹", "__root__": "未分类"}.get(folder, folder)
    msg = "" if fs else f"文件夹「{fdesc}」下没有任何文件，未执行下载。"
    return Response(json.dumps({"total": len(fs), "msg": msg}),
                    mimetype="application/json")

@app.get("/admin/range")
def admin_range():
    """按文件夹打包下载 ZIP; 选「全部文件夹」时保留目录结构防重名冲突"""
    if not is_admin():
        abort(401)
    folder = norm_folder(request.args.get("folder"))
    fs = folder_files(folder)
    fdesc = {"all": "全部文件夹", "__root__": "未分类"}.get(folder, folder)
    if not fs:
        # 兜底: 正常流程前端已用 zipcheck 就地弹窗, 不会走到这里
        msg = f"文件夹「{fdesc}」下没有任何文件，未执行下载。"
        return redirect(f"/admin?token={ADMIN_TOKEN}&folder={quote(folder)}"
                        f"&warn={quote(msg)}")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, fn, p in fs:
            arc = (rel + "/" + fn) if rel else fn
            z.write(p, arcname=arc)
    buf.seek(0)
    fname = "作业_全部文件夹.zip" if folder == "all" else f"作业_{fdesc}.zip"
    return send_file(buf, as_attachment=True,
                     download_name=fname, mimetype="application/zip")

REASON_TXT = {
    "canonical": "已是当前模板格式",
    "unknown": "没识别出是谁",
    "ambiguous": "命中多个姓名（如小组作业），不猜",
    "noid": "名单缺学号",
    "off": "自动规范化已关闭",
}

@app.get("/admin/normalize")
def admin_normalize():
    """文件名规范化预览: 列出「原名→标准名」对照, 不直接改, 需确认后执行"""
    if not is_admin():
        abort(401)
    folder = norm_folder(request.args.get("folder"))
    pairs = load_roster_pairs()
    fdesc = {"all": "全部文件夹", "__root__": "未分类"}.get(folder, folder)
    scope = f"范围：文件夹「{html.escape(fdesc)}」"
    has_ids = any(sid for _, sid in pairs)
    cfg_pre = load_config()
    enabled = cfg_pre.get("normalize") and cfg_pre.get("recognize")
    ren, skip = [], []
    if enabled and has_ids:
        for rel, fn, p in folder_files(folder):
            new, why = normalize_plan(fn, pairs, p)
            (ren if new else skip).append((fn, new or REASON_TXT.get(why, why), rel))
    renbox = ""
    for fn, new, rel in ren:
        tag = f'<span class=ftag>{html.escape(rel)}</span>' if rel else ""
        renbox += (f'<div class=nrow><span class=old>{html.escape(fn)}</span>'
                   f'<span class=arrow>➜</span>'
                   f'<span class=new>{html.escape(new)}</span>{tag}</div>')
    if not renbox:
        renbox = '<div class=allok>没有需要重命名的文件</div>'
    if skip:
        skipsec = ('<section class="glass panel"><h3><svg class=svgico viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="7" y="5" width="3.5" height="14" rx="1"/><rect x="13.5" y="5" width="3.5" height="14" rx="1"/></svg>保持不变（'
                   + str(len(skip)) + ' 个）</h3>'
                   + "".join(f'<div class=srow>{html.escape(fn)}'
                             f'<span class=rsn>{html.escape(why)}</span></div>'
                             for fn, why, rel in skip) + '</section>')
    else:
        skipsec = ""
    if not enabled:
        note = ('<br><span style="color:#b8860b">自动规范化已在管理页关闭</span>')
        warnbox = ('<div class=warnbox><b>「名单识别与自动规范化」当前是关闭状态。</b>'
                   '点管理页右上角的设置按钮打开开关即可——打开后上传的文件会按命名模板'
                   '自动改名，这里也能批量处理历史文件。'
                   '（关闭不影响「作业核对」功能。）</div>')
        gobtn = ""
    elif not has_ids:
        note = ('<br><span style="color:#b8860b">名单里还没有学号，自动重命名未启用</span>')
        warnbox = ('<div class=warnbox><b>名单里还没有学号。</b>自动重命名需要「姓名 + 学号」配对——'
                   '请先回管理页点「编辑名单」，把带学号的全员名单重新粘贴保存一次。'
                   '（只用姓名不带学号，核对功能不受影响，但文件不会自动改名。）</div>')
        gobtn = ""
    else:
        note = ""
        warnbox = ('<div class=warnbox><b>此操作会真正重命名文件，不可撤销。</b>'
                   '请核对上面的清单——按管理页右上角「设置」里的命名模板生成；'
                   '认不出是谁的、命中多个姓名的（如小组作业）会保持原样，列在下方「保持不变」里。</div>')
        gobtn = (f'<a class=cta href="/admin/normalize/run?token={ADMIN_TOKEN}'
                 f'&folder={quote(folder)}" onclick="return confirm(\'确认执行这 '
                 f'{len(ren)} 个重命名？\')">确认执行重命名</a>' if ren else "")
    page = (NORMALIZE_TPL
            .replace("__SCOPE__", scope)
            .replace("__NOTE__", note)
            .replace("__WARNBOX__", warnbox)
            .replace("__NREN__", str(len(ren)))
            .replace("__NSKIP__", str(len(skip)))
            .replace("__RENBOX__", renbox)
            .replace("__SKIPSEC__", skipsec)
            .replace("__GOBTN__", gobtn)
            .replace("__TOKEN__", ADMIN_TOKEN)
            .replace("__BACK__", f"folder={quote(folder)}"))
    return no_store(Response(page, mimetype="text/html"))

@app.get("/admin/normalize/run")
def admin_normalize_run():
    """执行批量规范化(预览页确认后触发); 完成后回管理页并弹窗摘要"""
    if not is_admin():
        abort(401)
    folder = norm_folder(request.args.get("folder"))
    pairs = load_roster_pairs()
    done, kept = 0, 0
    for rel, fn, p in folder_files(folder):
        new, why = normalize_plan(fn, pairs, p)
        if new:
            os.rename(p, os.path.join(os.path.dirname(p), new))
            done += 1
        else:
            kept += 1
    msg = f"文件名规范化完成：重命名 {done} 个，保持不变 {kept} 个。"
    return redirect(f"/admin?token={ADMIN_TOKEN}&folder={quote(folder)}"
                    f"&warn={quote(msg)}")

def resolve_rel(rel: str):
    """把 'folder/name' 或 'name' 解析为安全绝对路径; 非法返回 None"""
    parts = [s for s in (rel or "").split("/") if s]
    if len(parts) == 1:
        name = safe_name(parts[0])
        p = os.path.join(UPLOAD_DIR, name)
    elif len(parts) == 2:
        d = safe_name(parts[0])
        name = safe_name(parts[1])
        if d not in list_folders():
            return None
        p = os.path.join(UPLOAD_DIR, d, name)
    else:
        return None
    return p if os.path.isfile(p) else None

@app.get("/admin/download/<path:rel>")
def admin_download(rel):
    if not is_admin():
        abort(401)
    p = resolve_rel(rel)
    if not p:
        abort(404)
    return send_file(p, as_attachment=True)

@app.get("/admin/delete/<path:rel>")
def admin_delete(rel):
    if not is_admin():
        abort(401)
    p = resolve_rel(rel)
    if p:
        os.remove(p)
    # 删除后直接回到原筛选状态的管理页, 不停留
    folder = norm_folder(request.args.get("folder"))
    return redirect(f"/admin?token={ADMIN_TOKEN}&folder={quote(folder)}")

if __name__ == "__main__":
    import ssl
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain("/Users/zjk/nextcloud-drop/cert.pem",
                        "/Users/zjk/nextcloud-drop/key.pem")
    app.run(host="0.0.0.0", port=8080, debug=False, ssl_context=ctx)
