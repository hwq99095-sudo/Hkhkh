#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AV Controller — بوت ديسكورد يتحكم بموقع Always In Venous كامل.

الفكرة: تكتب أمر في ديسكورد  ->  الذكاء الاصطناعي يعدّل كود الموقع  ->  يُبنى الموقع (مضغوط ومشوَّش)
        ->  يُنشر  ->  الصفحات المفتوحة تتحدّث لوحدها. وكل تعديل ينحفظ للتراجع.

التشغيل:
  1) فك ضغط av-website.zip بجانب هذا الملف (يطلع مجلد av-website)
  2) pip install -U discord.py aiohttp
  3) اكتب إعداداتك في ملف اسمه .env بجانب هذا الملف (شوف القائمة تحت) ثم:  python av_controller.py

أمثلة ملف .env:
  DISCORD_TOKEN=توكن_البوت          # ← هنا يتحط التوكن (لا تحطه في الموقع أبداً)
  GUILD_ID=رقم_السيرفر
  OWNER_IDS=رقمك,رقم_ثاني            # اللي يقدرون يتحكمون
  ANTHROPIC_API_KEY=مفتاح_anthropic   # لأوامر التعديل
  PUBLISH=local                       # local | netlify | github
  NETLIFY_TOKEN= / NETLIFY_SITE_ID=   # لو PUBLISH=netlify
  GITHUB_TOKEN= / GITHUB_REPO=user/repo / GITHUB_BRANCH=main   # لو PUBLISH=github (GitHub Pages)
  EDIT_CHANNEL_ID=                    # اختياري: روم تكتب فيه الأوامر مباشرة
  SITE_URL=                           # اختياري: رابط الموقع العام (البوت يصير "متوقف" لو ما ردّ)
  GITHUB_PATH=                        # اختياري: لو صفحاتك من مجلد (مثل docs)
  PORT=3000   AUTO_PUBLISH=1   CLAUDE_MODEL=claude-sonnet-5-5
"""
import asyncio, base64, hashlib, io, json, os, re, shutil, subprocess, sys, time, zipfile, zlib
from pathlib import Path

import aiohttp
from aiohttp import web
import discord
from discord import app_commands

BASE = Path(__file__).resolve().parent


def _load_env():
    f = BASE / ".env"
    if f.exists():
        for line in f.read_text("utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()


def cfg(k, d=""):
    return os.environ.get(k, d).strip()


DISCORD_TOKEN = cfg("DISCORD_TOKEN")
GUILD_ID = int(cfg("GUILD_ID", "0") or 0)
OWNER_IDS = {int(x) for x in re.split(r"[,\s]+", cfg("OWNER_IDS")) if x.isdigit()}
EDIT_CHANNEL_ID = int(cfg("EDIT_CHANNEL_ID", "0") or 0)
ANTHROPIC_API_KEY = cfg("ANTHROPIC_API_KEY")
CLAUDE_MODEL = cfg("CLAUDE_MODEL", "claude-sonnet-5-5")
PUBLISH = cfg("PUBLISH", "local").lower()
AUTO_PUBLISH = cfg("AUTO_PUBLISH", "1") not in ("0", "false", "no")
NETLIFY_TOKEN, NETLIFY_SITE_ID = cfg("NETLIFY_TOKEN"), cfg("NETLIFY_SITE_ID")
GITHUB_TOKEN, GITHUB_REPO, GITHUB_BRANCH = cfg("GITHUB_TOKEN"), cfg("GITHUB_REPO"), cfg("GITHUB_BRANCH", "main")
SITE_URL = cfg("SITE_URL").rstrip("/")
PORT = int(cfg("PORT", "3000") or 3000)
LOCAL_WEB = PUBLISH == "local" or cfg("LOCAL_WEB").lower() in ("1", "true", "yes")
GITHUB_PATH = cfg("GITHUB_PATH").strip("/")   # لو صفحاتك من مجلد مثل docs

SITE = Path(cfg("SITE_DIR", str(BASE / "av-website")))   # مجلد الموقع الجاهز (من الـ zip)
DATA = BASE / "data"
SRC = DATA / "src"                                          # الكود المقروء (للتعديل)
VER = DATA / "versions"
STATE_FILE = DATA / "state.json"
FILES = ["index.html", "staff.js"]

# ============================================================
# <<BUILD-START>>  بناء الموقع: ضغط + تشويش + حماية بسيطة
# ============================================================
ANTI = ('(function(){var b=function(e){e.preventDefault()};addEventListener("contextmenu",b);'
        'addEventListener("dragstart",b);addEventListener("keydown",function(e){var k=(e.key||"").toLowerCase();'
        'if(e.key==="F12"||(e.ctrlKey&&e.shiftKey&&"ijc".indexOf(k)>-1&&k.length===1)||(e.ctrlKey&&k==="u")||'
        '(e.metaKey&&e.altKey&&"iuj".indexOf(k)>-1&&k.length===1)){e.preventDefault()}})})();\n')


def _key(seed, n):
    out, s = [], seed
    for _ in range(n):
        s = (s * 1103515245 + 12345) & 0x7FFFFFFF
        out.append((s >> 16) & 255)
    return out


def _min_css(c):
    c = re.sub(r"/\*.*?\*/", "", c, flags=re.S)
    c = re.sub(r"\s+", " ", c)
    c = re.sub(r"\s*([{};,>])\s*", r"\1", c)
    c = re.sub(r":\s+", ":", c)
    return c.strip()


def pack_site(index_html, staff_js, build_id):
    """يحوّل المصدر المقروء إلى صفحة مضغوطة، وكود الجافاسكربت يُشفَّر (XOR + base64) ويُفكّ وقت التشغيل."""
    scripts = list(re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", index_html, re.S))
    main = scripts[-1]
    code = (staff_js.strip() + "\n;" + main.group(1).strip()).replace("%%BUILD%%", build_id)
    html = index_html[:main.start()] + "@@LOADER@@" + index_html[main.end():]
    html = re.sub(r'<script[^>]*\bsrc="staff\.js"[^>]*></script>\s*', "", html)
    parts = re.split(r"(<style.*?</style>|<script.*?</script>)", html, flags=re.S)
    out = []
    for i, p in enumerate(parts):
        if i % 2 == 0:
            p = re.sub(r"<!--.*?-->", "", p, flags=re.S)
            p = re.sub(r"\s*\n\s*", " ", p)
        elif p.startswith("<style"):
            m = re.match(r"(<style[^>]*>)(.*)(</style>)", p, re.S)
            p = m.group(1) + _min_css(m.group(2)) + m.group(3)
        out.append(p)
    html = "".join(out).replace("%%BUILD%%", build_id)
    seed = int(build_id, 16) & 0x7FFFFFFF
    raw = (ANTI + code).encode("utf-8")
    enc = base64.b64encode(bytes(b ^ k for b, k in zip(raw, _key(seed, len(raw))))).decode()
    loader = ('<script>(function(){var s=%d,d="%s",b=atob(d),u=new Uint8Array(b.length),i;'
              'for(i=0;i<b.length;i++){s=(Math.imul(s,1103515245)+12345)&2147483647;'
              'u[i]=b.charCodeAt(i)^((s>>16)&255)}(0,eval)(new TextDecoder().decode(u))})();</script>') % (seed, enc)
    return html.replace("@@LOADER@@", loader)


HEADERS_FILE = "/live.json\n  Cache-Control: no-store\n/index.html\n  Cache-Control: no-cache\n/\n  Cache-Control: no-cache\n"
# <<BUILD-END>>

# ---------- الحالة ----------
STATE = {"build": "", "avatars": {}, "published": {}, "deploys": []}
SESSION = None
client = None
ID_CACHE = {}
PUB_LOCK = asyncio.Lock()
EDIT_LOCK = asyncio.Lock()
EMBEDDED_SRC = "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"


def save_state():
    DATA.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(STATE, ensure_ascii=False), "utf-8")


def load_state():
    if STATE_FILE.exists():
        try:
            STATE.update(json.loads(STATE_FILE.read_text("utf-8")))
        except Exception:
            pass


def ensure_src():
    SRC.mkdir(parents=True, exist_ok=True)
    if not (SRC / "index.html").exists():
        blob = json.loads(zlib.decompress(base64.b64decode(EMBEDDED_SRC)).decode("utf-8"))
        for f in FILES:
            (SRC / f).write_text(blob[f], "utf-8")


def write_live():
    SITE.mkdir(parents=True, exist_ok=True)
    (SITE / "live.json").write_text(
        json.dumps({"v": STATE["build"], "avatars": STATE["avatars"]}, ensure_ascii=False, separators=(",", ":")), "utf-8")


def build():
    idx = (SRC / "index.html").read_text("utf-8")
    stf = (SRC / "staff.js").read_text("utf-8")
    bid = hashlib.sha1((idx + "\0" + stf).encode()).hexdigest()[:8]
    SITE.mkdir(parents=True, exist_ok=True)
    (SITE / "index.html").write_text(pack_site(idx, stf, bid), "utf-8")
    (SITE / "_headers").write_text(HEADERS_FILE, "utf-8")
    STATE["build"] = bid
    write_live()
    save_state()
    return bid


# ---------- النسخ والتراجع ----------
def list_versions():
    out = []
    if VER.exists():
        for d in VER.iterdir():
            m = d / "meta.json"
            if m.exists():
                out.append(json.loads(m.read_text("utf-8")))
    return sorted(out, key=lambda x: x["at"], reverse=True)


def snapshot(label, kind="edit"):
    vid = format(int(time.time() * 1000), "x")
    d = VER / vid
    d.mkdir(parents=True, exist_ok=True)
    for f in FILES:
        shutil.copy(SRC / f, d / f)
    (d / "meta.json").write_text(json.dumps(
        {"id": vid, "label": label, "kind": kind, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}, ensure_ascii=False), "utf-8")
    for old in [v for v in list_versions() if v["kind"] != "base"][60:]:
        shutil.rmtree(VER / old["id"], ignore_errors=True)
    return vid


def restore_files(vid):
    for f in FILES:
        shutil.copy(VER / vid / f, SRC / f)


# ---------- فحص الصياغة ----------
def js_check(code):
    node = shutil.which("node")
    if not node:
        return True, ""
    r = subprocess.run([node, "-e", "new (require('vm').Script)(require('fs').readFileSync(0,'utf8'))"],
                       input=code.encode("utf-8"), capture_output=True, timeout=30)
    return r.returncode == 0, r.stderr.decode("utf-8", "ignore")[:300]


def validate(files):
    h = files["index.html"]
    if not re.search(r"</html>\s*$", h.strip(), re.I):
        raise ValueError("ملف الصفحة ناقص (ما انتهى بـ </html>)")
    for need in ('id="track"', 'id="members"', 'name="build"', 'src="staff.js"'):
        if need not in h:
            raise ValueError(f"التعديل شال عنصر أساسي: {need}")
    if not re.search(r"const staff\s*=", files["staff.js"]):
        raise ValueError("ملف staff.js انكسر")
    scripts = [m.group(1) for m in re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", h, re.S)]
    for s in scripts[:-1] + [files["staff.js"], files["staff.js"] + "\n;" + scripts[-1]]:
        ok, err = js_check(s)
        if not ok:
            line = next((x for x in err.splitlines() if "Error" in x), "خطأ صياغة")
            raise ValueError("خطأ في كود الجافاسكربت الناتج: " + line.strip()[:160])


# ---------- الذكاء الاصطناعي ----------
SYSTEM = """You edit a premium Arabic (RTL) landing page for the Discord community "Always In Venous". You get the current source files and the owner's request (usually Arabic). Answer ONLY through the apply_edits tool with minimal, exact text edits.
Rules:
1. Every old_str must be copied exactly from the file and occur exactly once; add surrounding context to make it unique.
2. Keep the design language: CSS variables (--p, --surface, --line, --mu...), Cairo/Cinzel fonts, rounded cards, dark/light themes, RTL. Reuse existing classes (.sec, .sh, .card, .feat, .fg, .step, .btn...) for new sections. A new nav link goes inside <nav class="links"> and its section id must be added to the scrollspy array in the script.
3. Never remove or rename: #track, #members, #toast, #modal, <meta name="build">, <script src="staff.js">, the poll() live script, or the shape of the staff array in staff.js ({name, role, initials, image, discord, badge}).
4. Staff changes (add/remove/rename, Discord usernames) go in staff.js. "discord" is the Discord username only. For new people use image:"" (never invent image paths).
5. No external scripts or images. Only Google Fonts are allowed.
6. Write visible text in Arabic unless asked otherwise. Never put secrets in files.
7. If the request is impossible, unclear or unsafe, return edits: [] and explain in the summary.
Keep summary to 1-2 short Arabic sentences."""

TOOL = {
    "name": "apply_edits",
    "description": "Apply exact text edits to the website source files",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "ملخص قصير بالعربي لما تغيّر"},
            "edits": {"type": "array", "items": {"type": "object", "properties": {
                "file": {"type": "string", "enum": FILES},
                "old_str": {"type": "string"}, "new_str": {"type": "string"}},
                "required": ["file", "old_str", "new_str"]}},
        },
        "required": ["summary", "edits"],
    },
}


async def claude_edit(request):
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY ناقص في .env")
    files = {f: (SRC / f).read_text("utf-8") for f in FILES}
    body = {
        "model": CLAUDE_MODEL, "max_tokens": 8000, "system": SYSTEM, "tools": [TOOL],
        "tool_choice": {"type": "tool", "name": "apply_edits"},
        "messages": [{"role": "user", "content":
                      "\n".join(f'<file name="{f}">\n{files[f]}\n</file>' for f in FILES) + f"\n\nطلب المالك:\n{request}"}],
    }
    async with SESSION.post("https://api.anthropic.com/v1/messages", json=body, timeout=aiohttp.ClientTimeout(total=240),
                            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"}) as r:
        data = await r.json()
        if r.status != 200:
            raise RuntimeError("Anthropic: " + str(data.get("error", {}).get("message", r.status)))
    use = next((b for b in data.get("content", []) if b.get("type") == "tool_use"), None)
    if not use:
        raise RuntimeError("ما وصلني رد صالح من الذكاء الاصطناعي")
    return use["input"].get("summary", ""), use["input"].get("edits", []), files


async def edit_site(request):
    async with EDIT_LOCK:
        summary, edits, files = await claude_edit(request)
        if not edits:
            return f"{summary} (ما تغيّر شي)"
        out = dict(files)
        for e in edits:
            f, old = e.get("file"), e.get("old_str", "")
            if f not in FILES or not old:
                raise ValueError("تعديل غير صالح")
            n = out[f].count(old)
            if n != 1:
                raise ValueError(f"ما قدرت أطبّق التعديل بدقة (النص ظهر {n} مرة في {f}). جرّب صياغة ثانية.")
            out[f] = out[f].replace(old, e["new_str"], 1)
        await asyncio.to_thread(validate, out)
        snapshot("قبل: " + re.sub(r"\s+", " ", request)[:70])
        for f in FILES:
            (SRC / f).write_text(out[f], "utf-8")
        build()
        asyncio.create_task(sync_avatars())
        return summary


def undo_site():
    s = next((v for v in list_versions() if v["kind"] == "edit"), None)
    if not s:
        raise ValueError("ما في تعديل أرجع له. (تقدر تسترجع النسخة الأصلية من /site history)")
    snapshot("قبل التراجع", "undone")
    restore_files(s["id"])
    shutil.rmtree(VER / s["id"], ignore_errors=True)
    build()
    return s["label"].replace("قبل: ", "", 1)


def restore_site(vid):
    if not re.fullmatch(r"[a-z0-9]+", vid) or not (VER / vid / "meta.json").exists():
        raise ValueError("رقم النسخة غلط.")
    snapshot("قبل الاسترجاع", "undone")
    restore_files(vid)
    build()


# ---------- النشر ----------
def recent_deploys():
    cut = time.time() - 30 * 86400
    return [t for t in STATE["deploys"] if t > cut]


async def pub_netlify(force):
    if not (NETLIFY_TOKEN and NETLIFY_SITE_ID):
        raise RuntimeError("ناقص NETLIFY_TOKEN أو NETLIFY_SITE_ID في .env")
    n = len(recent_deploys())
    if n >= 15 and not force:
        return f"⚠️ ما نشرت تلقائياً: نشرت {n} مرة آخر 30 يوم، ونتلفاي المجاني حدّه تقريباً 20 نشر في الشهر. للنشر رغم ذلك: /site publish"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in SITE.rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(SITE).as_posix())
    async with SESSION.post(f"https://api.netlify.com/api/v1/sites/{NETLIFY_SITE_ID}/deploys", data=buf.getvalue(),
                            timeout=aiohttp.ClientTimeout(total=180),
                            headers={"Authorization": f"Bearer {NETLIFY_TOKEN}", "Content-Type": "application/zip"}) as r:
        if r.status >= 300:
            raise RuntimeError(f"Netlify {r.status}: {(await r.text())[:200]}")
    STATE["deploys"] = recent_deploys() + [time.time()]
    save_state()
    return f"🚀 تم النشر على Netlify (ثواني وتظهر). عدد النشر آخر 30 يوم: {len(STATE['deploys'])}"


async def pub_github():
    if not (GITHUB_TOKEN and GITHUB_REPO):
        raise RuntimeError("ناقص GITHUB_TOKEN أو GITHUB_REPO في .env")
    hdr = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json",
           "X-GitHub-Api-Version": "2022-11-28"}
    files = sorted([p for p in SITE.rglob("*") if p.is_file()], key=lambda p: p.name == "live.json")  # live.json آخر شي
    sent = 0
    for p in files:
        rel, data = p.relative_to(SITE).as_posix(), p.read_bytes()
        h = hashlib.sha1(data).hexdigest()
        if STATE["published"].get(rel) == h:
            continue
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_PATH + '/' if GITHUB_PATH else ''}{rel}"
        sha = None
        async with SESSION.get(url, headers=hdr, params={"ref": GITHUB_BRANCH}) as r:
            if r.status == 200:
                sha = (await r.json()).get("sha")
        body = {"message": f"AV bot: {rel}", "content": base64.b64encode(data).decode(), "branch": GITHUB_BRANCH}
        if sha:
            body["sha"] = sha
        async with SESSION.put(url, headers=hdr, json=body, timeout=aiohttp.ClientTimeout(total=120)) as r:
            if r.status not in (200, 201):
                raise RuntimeError(f"GitHub {r.status}: {(await r.text())[:200]}")
        STATE["published"][rel] = h
        sent += 1
        save_state()
    return f"🚀 تم الرفع على GitHub ({sent} ملف). صفحات GitHub تتحدّث خلال دقيقة تقريباً." if sent else "ما في جديد أرفعه."


async def publish(force=False):
    if PUBLISH == "local":
        return "الوضع local: الموقع يُقدَّم من هذا البرنامج مباشرة (ما فيه نشر خارجي)."
    async with PUB_LOCK:
        if PUBLISH == "netlify":
            return await pub_netlify(force)
        if PUBLISH == "github":
            return await pub_github()
    raise RuntimeError("قيمة PUBLISH غلط (local / netlify / github)")


async def auto_publish():
    if PUBLISH != "local" and AUTO_PUBLISH:
        try:
            return "\n" + await publish()
        except Exception as e:
            return f"\n⚠️ التعديل انحفظ لكن النشر فشل: {e}"
    return ""


# ---------- صور المشرفين ----------
def staff_names():
    try:
        s = (SRC / "staff.js").read_text("utf-8")
        return [n.lstrip("@").strip() for n in re.findall(r'discord\s*:\s*"([^"]+)"', s)]
    except Exception:
        return []


async def refresh_user(u):
    g = client.get_guild(GUILD_ID)
    if not g:
        return False
    uid = ID_CACHE.get(u)
    if not uid:
        found = await g.query_members(query=u, limit=10)
        m = next((x for x in found if x.name.lower() == u.lower()), None)
        if not m:
            print("ما لقيت العضو في السيرفر:", u)
            return False
        ID_CACHE[u] = uid = m.id
    user = await client.fetch_user(uid)
    nxt = {"id": str(user.id), "avatar": str((user.avatar or user.default_avatar).replace(size=256).url)}
    if STATE["avatars"].get(u) == nxt:
        return False
    STATE["avatars"][u] = nxt
    return True


async def push_avatars():
    write_live()
    save_state()
    return await auto_publish()


async def sync_avatars():
    names, changed = staff_names(), False
    for u in names:
        try:
            changed = (await refresh_user(u)) or changed
        except Exception as e:
            print("avatar", u, e)
    for u in [k for k in STATE["avatars"] if k not in names]:
        del STATE["avatars"][u]
        changed = True
    if changed:
        await push_avatars()
    return changed


async def on_avatar_event(uid):
    u = next((k for k, v in ID_CACHE.items() if v == uid), None)
    if u and await refresh_user(u):
        await push_avatars()


# ---------- ديسكورد ----------
intents = discord.Intents.default()
intents.members = True
intents.presences = True
intents.message_content = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
site = app_commands.Group(name="site", description="التحكم بالموقع", guild_only=True)


async def run_text(text):
    t = text.strip()
    if re.fullmatch(r"(تراجع|undo|ارجع)", t, re.I):
        msg = "↩️ تم التراجع عن: " + undo_site()
        return msg + await auto_publish()
    msg = "✅ " + await edit_site(t)
    if PUBLISH == "local":
        msg += f"\n🌐 الموقع المحلي يتحدّث فوراً (المنفذ {PORT})."
    return msg + await auto_publish() + "\n(للتراجع اكتب: تراجع)"


async def guard(i):
    if i.user.id in OWNER_IDS:
        return True
    await i.response.send_message("مو مصرّح لك.", ephemeral=True)
    return False


async def reply(i, coro):
    await i.response.defer(ephemeral=True)
    try:
        msg = await coro
    except Exception as e:
        msg = f"❌ {e}"
    await i.followup.send(str(msg)[:1900], ephemeral=True)


@site.command(name="edit", description="عدّل الموقع بأمر نصي")
@app_commands.describe(request="اكتب اللي تبغاه")
async def c_edit(i: discord.Interaction, request: str):
    if await guard(i):
        await reply(i, run_text(request))


@site.command(name="undo", description="تراجع عن آخر تعديل")
async def c_undo(i: discord.Interaction):
    if await guard(i):
        await reply(i, run_text("تراجع"))


@site.command(name="history", description="آخر النسخ المحفوظة")
async def c_hist(i: discord.Interaction):
    async def f():
        rows = [f"`{v['id']}` — {v['label']} ({v['at'][5:16].replace('T', ' ')})" for v in list_versions()[:10]]
        return "\n".join(rows) or "فاضي"
    if await guard(i):
        await reply(i, f())


@site.command(name="restore", description="استرجع نسخة معيّنة")
@app_commands.describe(id="رقم النسخة من /site history")
async def c_restore(i: discord.Interaction, id: str):
    async def f():
        restore_site(id.strip())
        return "✅ تم الاسترجاع" + await auto_publish()
    if await guard(i):
        await reply(i, f())


@site.command(name="refresh", description="حدّث صور المشرفين الحين")
async def c_refresh(i: discord.Interaction):
    async def f():
        ch = await sync_avatars()
        return "✅ تم تحديث الصور" if ch else "ما في تغيير."
    if await guard(i):
        await reply(i, f())


@site.command(name="publish", description="انشر الموقع الحين (يتجاوز حد Netlify التلقائي)")
async def c_pub(i: discord.Interaction):
    if await guard(i):
        await reply(i, publish(force=True))


@site.command(name="status", description="حالة الموقع والبوت")
async def c_status(i: discord.Interaction):
    async def f():
        return (f"البناء: `{STATE['build']}`\nالنشر: `{PUBLISH}`" +
                (f" ({len(recent_deploys())} نشر آخر 30 يوم)" if PUBLISH == "netlify" else "") +
                f"\nالمشرفين: {', '.join(staff_names()) or '-'}")
    if await guard(i):
        await reply(i, f())


tree.add_command(site, guild=discord.Object(id=GUILD_ID))


async def health_loop():
    while True:
        ok = True
        if SITE_URL:
            try:
                async with SESSION.get(SITE_URL, timeout=aiohttp.ClientTimeout(total=6)) as r:
                    ok = r.status < 500
            except Exception:
                ok = False
        await client.change_presence(
            status=discord.Status.online if ok else discord.Status.dnd,
            activity=discord.Activity(type=discord.ActivityType.watching,
                                      name="الموقع شغّال ✅" if ok else "الموقع متوقف ⚠️"))
        await asyncio.sleep(30)


async def avatar_loop():
    while True:
        await asyncio.sleep(60)
        try:
            await sync_avatars()
        except Exception as e:
            print("sync", e)


READY_ONCE = False


@client.event
async def on_ready():
    global READY_ONCE
    if READY_ONCE:
        return
    READY_ONCE = True
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    print("🤖 البوت شغّال:", client.user)
    await sync_avatars()
    asyncio.create_task(health_loop())
    asyncio.create_task(avatar_loop())


@client.event
async def on_user_update(before, after):
    if before.avatar != after.avatar:
        await on_avatar_event(after.id)


@client.event
async def on_member_update(before, after):
    if before.avatar != after.avatar:
        await on_avatar_event(after.id)


@client.event
async def on_message(m):
    if m.author.bot or not EDIT_CHANNEL_ID or m.channel.id != EDIT_CHANNEL_ID or m.author.id not in OWNER_IDS:
        return
    if not m.content.strip():
        return
    await m.add_reaction("⏳")
    try:
        await m.reply(str(await run_text(m.content))[:1900])
    except Exception as e:
        await m.reply(f"❌ {e}")
    finally:
        try:
            await m.remove_reaction("⏳", client.user)
        except Exception:
            pass


# ---------- الموقع المحلي (يُقدَّم من هذا البرنامج) ----------
@web.middleware
async def nocache(request, handler):
    resp = await handler(request)
    resp.headers["Cache-Control"] = "no-store"
    return resp


async def start_web():
    app = web.Application(middlewares=[nocache])

    async def index(_):
        return web.FileResponse(SITE / "index.html")
    app.router.add_get("/", index)
    app.router.add_static("/", SITE, show_index=False)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    print(f"🌐 الموقع المحلي: http://localhost:{PORT}")


async def amain():
    global SESSION
    if not DISCORD_TOKEN or not GUILD_ID:
        print("❌ ناقص DISCORD_TOKEN أو GUILD_ID. اكتبهم في ملف .env بجانب هذا الملف (شوف أول الملف).")
        return
    if not OWNER_IDS:
        print("⚠️ OWNER_IDS فاضي: ما أحد يقدر يستخدم أوامر التعديل.")
    load_state()
    ensure_src()
    if not list_versions():
        snapshot("النسخة الأصلية", "base")
    build()
    SESSION = aiohttp.ClientSession()
    try:
        if LOCAL_WEB:
            try:
                await start_web()
            except Exception as e:
                print("⚠️ ما قدرت أشغّل الموقع المحلي (عادي لو تستخدم GitHub):", e)
        await client.start(DISCORD_TOKEN)
    finally:
        try:
            await client.change_presence(status=discord.Status.invisible)
        except Exception:
            pass
        await client.close()
        await SESSION.close()


if __name__ == "__main__":
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass
