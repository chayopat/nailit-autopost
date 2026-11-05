#!/usr/bin/env python3
"""โพสต์รูปจาก Google Drive (ซ้อนกรอบ) ลง Facebook Page + Instagram พร้อมขั้นตอนพรีวิว/อนุมัติ

คำสั่ง:
  python post.py run       # ตามเวลา: เตรียมรูป แล้วรออนุมัติ (หรือโพสต์เลยถ้าปิดโหมดอนุมัติ)
  python post.py approve   # อนุมัติรูปที่รออยู่ แล้วโพสต์
  python post.py skip      # ข้ามรูปที่รออยู่ (ไม่โพสต์ รูปถัดไปจะขึ้นแทน)
เพิ่ม --local DIR (รูปจากเครื่องแทน Drive) และ --dry-run (ไม่บันทึก/ไม่โพสต์) เพื่อทดสอบ
"""
import argparse, hashlib, io, json, os, re, subprocess, sys, tempfile, time, traceback
from datetime import datetime, timezone
from pathlib import Path

import requests
from PIL import Image
from compose import compose

ROOT = Path(__file__).parent
STATE, STATUS = ROOT / "state.json", ROOT / "docs" / "status.json"
FRAME, CAPTION = ROOT / "Review_-_2023.png", ROOT / "caption.txt"
PREVIEW = ROOT / "docs" / "preview"
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}
WARN_AT, HISTORY_MAX, HOSTED_W = {7, 3, 1, 0}, 30, 1600
CTX = {}          # ข้อมูลของรอบที่กำลังรัน (ชื่อรูป, แพลตฟอร์มที่กำลังโพสต์)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def flag(name, default=False):
    v = os.getenv(name, "").strip().lower()
    return default if v == "" else v in ("1", "true", "yes", "on")


def approval_mode():
    return flag("APPROVAL_MODE", default=True)     # ค่าเริ่มต้น: ต้องกดอนุมัติก่อนโพสต์


def platforms():
    env = os.getenv("PLATFORMS", "").strip()
    if env:
        return [p.strip().lower() for p in env.split(",") if p.strip()]
    return ["facebook"] + (["instagram"] if os.getenv("IG_USER_ID") else [])


def base_url():
    u = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if u:
        return u
    repo = os.getenv("GITHUB_REPOSITORY", "")
    if "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner}.github.io" + ("" if name.lower() == f"{owner}.github.io".lower() else f"/{name}")
    return ""


class Friendly(Exception):
    """ข้อผิดพลาดที่อธิบายเป็นภาษาคนได้ พร้อมบอกว่าต้องทำอะไร"""
    def __init__(self, title, detail, action):
        super().__init__(title)
        self.title, self.detail, self.action = title, detail, action


class FBError(Exception):
    def __init__(self, err):
        super().__init__(err.get("message", "Meta API error"))
        self.code, self.sub, self.msg = err.get("code"), err.get("error_subcode"), err.get("message", "")


def need(key):
    v = os.getenv(key, "").strip()
    if not v:
        raise Friendly("ตั้งค่าไม่ครบ", f"ยังไม่ได้ตั้งค่า {key}",
                       f"เพิ่ม {key} ที่ GitHub > Settings > Secrets and variables > Actions แล้วกด Run workflow")
    return v


# ---------- อธิบายข้อผิดพลาดเป็นภาษาไทย ----------
def explain(exc):
    plat = {"facebook": "Facebook", "instagram": "Instagram"}.get(CTX.get("platform"), "")
    if isinstance(exc, FBError):
        c = exc.code
        if c == 190:
            return Friendly("โทเคน Facebook/Instagram ใช้ไม่ได้แล้ว",
                            "Meta แจ้งว่าโทเคนหมดอายุหรือถูกเพิกถอน (Error 190) มักเกิดหลังเปลี่ยนรหัสผ่านหรือถอนสิทธิ์แอป",
                            "สร้าง Page token ใหม่ตามคู่มือข้อ 2 แล้วอัปเดต FB_PAGE_TOKEN ใน GitHub Secrets จากนั้นกดอนุมัติ/Run workflow อีกครั้ง")
        if plat == "Instagram" and (c == 9004 or exc.sub in (2207052, 2207003, 2207020, 2207026)):
            return Friendly("Instagram ดึงรูปจากลิงก์ไม่ได้", f"Error {c}: {exc.msg[:150]}",
                            "ตรวจว่า repo เป็น Public และเปิด GitHub Pages (โฟลเดอร์ /docs) แล้ว จากนั้นกดอนุมัติอีกครั้ง ระบบจะโพสต์เฉพาะ Instagram ที่ยังค้างอยู่")
        if c in (10, 200, 299) or (isinstance(c, int) and 200 <= c <= 299):
            return Friendly(f"แอปไม่มีสิทธิ์โพสต์ลง {plat or 'เพจ'}", f"Meta ปฏิเสธเรื่องสิทธิ์ (Error {c}): {exc.msg[:150]}",
                            "ตรวจว่าโทเคนมีสิทธิ์ pages_manage_posts และ instagram_content_publish และคุณเป็นแอดมินของเพจ ถ้าไม่แน่ใจให้สร้างโทเคนใหม่ตามคู่มือข้อ 2")
        if c in (4, 17, 32, 613):
            return Friendly(f"โพสต์ถี่เกินที่ {plat or 'Meta'} อนุญาต", f"ถูกจำกัดจำนวนคำขอชั่วคราว (Error {c})",
                            "รอสักครู่แล้วกดอนุมัติ/Run workflow ใหม่ รูปเดิมจะไม่ถูกข้าม")
        return Friendly(f"{plat or 'Meta'} ปฏิเสธการโพสต์", f"Error {c}: {exc.msg[:200]}", "ส่งข้อความนี้ให้ Claude ช่วยดู")
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return Friendly("เชื่อมต่ออินเทอร์เน็ตไม่ได้ชั่วคราว", str(exc)[:200], "ไม่ต้องทำอะไร ลองกดอนุมัติ/Run workflow ใหม่ภายหลัง รูปเดิมจะไม่ถูกข้าม")
    resp = getattr(exc, "resp", None)
    if resp is not None and hasattr(resp, "status"):          # googleapiclient HttpError
        st = int(resp.status)
        if st == 404:
            return Friendly("ไม่พบโฟลเดอร์ใน Google Drive", "Drive ตอบ 404",
                            "ตรวจ DRIVE_FOLDER_ID และแชร์โฟลเดอร์ให้อีเมลของ Service Account แล้ว (สิทธิ์ผู้ดู)")
        if st in (401, 403):
            return Friendly("เข้าถึง Google Drive ไม่ได้", f"Drive ตอบ {st}", "ตรวจว่าเปิด Google Drive API แล้ว และโฟลเดอร์แชร์ให้ Service Account แล้ว")
    if type(exc).__name__ in ("RefreshError", "TransportError"):
        return Friendly("เชื่อมต่อบัญชี Google ไม่สำเร็จ", str(exc)[:200], "ตรวจ GOOGLE_SERVICE_ACCOUNT_JSON ว่าเป็นไฟล์ key ที่ถูกต้องและยังไม่ถูกลบ")
    return Friendly("เกิดข้อผิดพลาดที่ไม่คาดคิด", f"{type(exc).__name__}: {str(exc)[:250]}", "ส่งข้อความนี้ให้ Claude ช่วยดู")


# ---------- แจ้งเตือน ----------
def notify(text, photo=None):
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        print("[แจ้งเตือน]", text)
        return
    try:
        base = f"https://api.telegram.org/bot{tok}"
        if photo:
            requests.post(f"{base}/sendPhoto", data={"chat_id": chat, "caption": text[:1000]},
                          files={"photo": ("post.jpg", photo)}, timeout=60)
        else:
            requests.post(f"{base}/sendMessage", data={"chat_id": chat, "text": text}, timeout=30)
    except Exception as e:                      # แจ้งเตือนพัง ต้องไม่ทำให้งานหลักพัง
        print("ส่งแจ้งเตือนไม่สำเร็จ:", e)


def ping(ok=True):
    url = os.getenv("HEALTHCHECK_URL", "").strip()
    if url:
        try:
            requests.get(url.rstrip("/") + ("" if ok else "/fail"), timeout=20)
        except Exception as e:
            print("ping ไม่สำเร็จ:", e)


# ---------- Google Drive ----------
def drive_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    info = json.loads(need("GOOGLE_SERVICE_ACCOUNT_JSON"))
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive.readonly"])
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def list_drive(svc, folder):
    files, token = [], None
    while True:
        r = svc.files().list(q=f"'{folder}' in parents and trashed=false and mimeType contains 'image/'",
                             fields="nextPageToken, files(id,name)", pageSize=1000, pageToken=token,
                             supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
        files += [{"id": f["id"], "name": f["name"]} for f in r.get("files", [])]
        token = r.get("nextPageToken")
        if not token:
            return files


def download(svc, file_id):
    from googleapiclient.http import MediaIoBaseDownload
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, svc.files().get_media(fileId=file_id, supportsAllDrives=True))
    done = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue()


# ---------- OneDrive (บัญชีส่วนตัว อ่านผ่านลิงก์แชร์แบบ 'ทุกคนที่มีลิงก์ดูได้') ----------
OD_API = "https://api.onedrive.com/v1.0/shares/"


def od_share_id(url):
    import base64
    return "u!" + base64.urlsafe_b64encode(url.strip().encode()).decode().rstrip("=")


def od_error(r):
    code = r.status_code
    if code in (401, 403):
        return Friendly("OneDrive ไม่อนุญาตให้อ่านโฟลเดอร์", f"OneDrive ตอบ {code}",
                        "เปิดโฟลเดอร์ใน OneDrive > แชร์ > ตั้งเป็น 'ทุกคนที่มีลิงก์' สิทธิ์ 'ดูได้' แล้วคัดลอกลิงก์ใหม่ไปใส่ ONEDRIVE_SHARE_URL")
    if code in (400, 404):
        return Friendly("เปิดลิงก์ OneDrive ไม่ได้", f"OneDrive ตอบ {code}",
                        "ตรวจ ONEDRIVE_SHARE_URL ว่าเป็นลิงก์แชร์ของโฟลเดอร์ ยังไม่ถูกยกเลิกการแชร์ และเป็นบัญชีส่วนตัว")
    return Friendly("OneDrive ตอบกลับผิดปกติ", f"HTTP {code}",
                    "ลองกดอนุมัติ/Run workflow ใหม่ภายหลัง ถ้ายังเป็นอีกให้ส่งข้อความนี้ให้ Claude ช่วยดู")


def list_onedrive(share_url):
    sid = od_share_id(share_url)
    last = None
    for tail in ("/root/children?$top=200", "/driveItem/children?$top=200"):
        url, files, ok = f"{OD_API}{sid}{tail}", [], True
        while url:
            r = requests.get(url, timeout=30)
            if r.status_code != 200:
                last, ok = r, False
                break
            j = r.json()
            for it in j.get("value", []):
                mime = (it.get("file") or {}).get("mimeType", "")
                if "file" in it and (mime.startswith("image/") or Path(it["name"]).suffix.lower() in IMG_EXT):
                    files.append({"id": it["id"], "name": it["name"], "sid": sid, "ts": it.get("createdDateTime", ""),
                                  "dl": it.get("@content.downloadUrl")})
            url = j.get("@odata.nextLink")
        if ok:
            return files
    raise od_error(last)


def download_onedrive(item):
    url = item.get("dl") or f"{OD_API}{item['sid']}/items/{item['id']}/content"
    r = requests.get(url, timeout=120)
    if r.status_code != 200:
        raise od_error(r)
    return r.content


# ---------- เลือกรูปถัดไป ----------
def sort_key(name, ts=""):
    """เรียงตามเลขตัวแรกในชื่อไฟล์ (2 มาก่อน 10) ไฟล์ไม่มีเลขเรียงตามเวลาที่ใส่เข้าโฟลเดอร์ต่อท้าย"""
    m = re.search(r"\d+", name)
    return (0, int(m.group()), name.lower()) if m else (1, ts or "", name.lower())


def pick(files, last_key, loop):
    files = sorted(files, key=lambda f: sort_key(f["name"], f.get("ts", "")))
    last = tuple(last_key) if last_key else None
    rest = [f for f in files if last is None or sort_key(f["name"], f.get("ts", "")) > last]
    if not rest and loop:
        rest = files
    return files, rest


# ---------- Meta (Facebook / Instagram) ----------
def graph_base():
    ver = os.getenv("FB_GRAPH_VERSION", "").strip("/ ")
    return f"https://graph.facebook.com/{ver + '/' if ver else ''}"


def gjson(r):
    try:
        j = r.json()
    except ValueError:
        raise RuntimeError(f"Meta ตอบกลับผิดรูปแบบ (HTTP {r.status_code})")
    if "error" in j:
        raise FBError(j["error"])
    return j


def check_fb_token():
    aid, sec = os.getenv("FB_APP_ID", "").strip(), os.getenv("FB_APP_SECRET", "").strip()
    if not (aid and sec):
        return None
    r = requests.get("https://graph.facebook.com/debug_token", timeout=30,
                     params={"input_token": need("FB_PAGE_TOKEN"), "access_token": f"{aid}|{sec}"})
    d = r.json().get("data", {})
    if not d.get("is_valid"):
        raise FBError({"code": 190, "message": d.get("error", {}).get("message", "token invalid")})
    exp = d.get("expires_at", 0)
    return {"name": "Meta Page token (Facebook + Instagram)",
            "expires_at": datetime.fromtimestamp(exp, timezone.utc).isoformat(timespec="seconds") if exp else None}


def post_facebook(img, text):
    r = requests.post(f"{graph_base()}{need('FB_PAGE_ID')}/photos", timeout=120,
                      data={"caption": text, "access_token": need("FB_PAGE_TOKEN"), "published": "true"},
                      files={"source": ("photo.jpg", img, "image/jpeg")})
    j = gjson(r)
    return j.get("post_id") or j.get("id")


def post_instagram(image_url, text):
    """Instagram ต้องรับรูปจากลิงก์สาธารณะ: สร้าง container -> รอพร้อม -> publish"""
    ig, tok, g = need("IG_USER_ID"), need("FB_PAGE_TOKEN"), graph_base()
    cid = gjson(requests.post(f"{g}{ig}/media", timeout=60,
                              data={"image_url": image_url, "caption": text[:2200], "access_token": tok}))["id"]
    for _ in range(30):
        st = gjson(requests.get(f"{g}{cid}", params={"fields": "status_code", "access_token": tok}, timeout=30)).get("status_code")
        if st == "FINISHED":
            break
        if st in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram ประมวลผลรูปไม่สำเร็จ (status {st})")
        time.sleep(4)
    return gjson(requests.post(f"{g}{ig}/media_publish", data={"creation_id": cid, "access_token": tok}, timeout=60))["id"]


def caption():
    if os.getenv("CAPTION"):
        return os.environ["CAPTION"]
    return CAPTION.read_text("utf-8").strip() if CAPTION.exists() else ""


# ---------- git / ลิงก์สาธารณะสำหรับ Instagram ----------
def git_push(paths, msg):
    if os.getenv("GITHUB_ACTIONS") != "true":
        return
    def g(*a):
        return subprocess.run(["git", *a], cwd=ROOT, check=True, capture_output=True, text=True)
    g("config", "user.name", "autopost-bot"); g("config", "user.email", "autopost-bot@users.noreply.github.com")
    g("add", *[p for p in paths if (ROOT / p).exists()])
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode != 0:
        g("commit", "-m", msg); g("pull", "--rebase", "--autostash"); g("push")


def ensure_public(rel):
    base = base_url()
    if not base:
        raise Friendly("ไม่ทราบลิงก์ GitHub Pages", "Instagram ต้องใช้ลิงก์สาธารณะของรูป", "ตั้ง Variable ชื่อ PUBLIC_BASE_URL เป็นลิงก์แดชบอร์ด (ไม่มี / ท้าย)")
    git_push([f"docs/{rel}", "state.json", "docs/status.json"], "add preview image")
    url, deadline = f"{base}/{rel}", time.time() + 480
    while time.time() < deadline:
        try:
            r = requests.get(url, timeout=20)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/"):
                return url
        except requests.RequestException:
            pass
        time.sleep(15)
    raise Friendly("Instagram เข้าถึงรูปไม่ได้", "GitHub Pages ยังไม่แสดงรูปภายใน 8 นาที",
                   "ตรวจว่า repo เป็น Public และเปิด GitHub Pages (โฟลเดอร์ /docs) แล้ว จากนั้นกดอนุมัติอีกครั้ง")


# ---------- state / status ----------
def load_state():
    try:
        s = json.loads(STATE.read_text("utf-8"))
    except Exception:
        s = {}
    s.setdefault("last_key", None); s.setdefault("pending", None); s.setdefault("history", [])
    s.setdefault("snapshot", {}); s.setdefault("tokens", [])
    return s


def save_state(state):
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf-8")


def set_token(state, entry):
    state["tokens"] = [t for t in state["tokens"] if t["name"] != entry["name"]] + [entry]


def record(state, ok, image, message):
    state["history"].insert(0, {"at": now(), "ok": ok, "image": image or "-", "message": message})
    del state["history"][HISTORY_MAX:]


def write_status(state, err=None):
    snap, p = state.get("snapshot", {}), state.get("pending")
    pub = None
    if p:
        pub = {k: p.get(k) for k in ("image", "preview", "caption", "platforms", "done", "created_at", "approved")}
    s = {"updated_at": now(), "last_run_at": now(), "platform": " + ".join(x.capitalize() for x in platforms()),
         "approval_mode": approval_mode(), "pending": pub,
         "next_image": snap.get("next_image", "-"), "images_total": snap.get("images_total", 0),
         "images_remaining": snap.get("images_remaining", 0), "tokens": state.get("tokens", []),
         "history": state["history"][:10],
         "error": {"title": err.title, "detail": err.detail, "action": err.action} if err else None}
    STATUS.parent.mkdir(exist_ok=True)
    STATUS.write_text(json.dumps(s, ensure_ascii=False, indent=1), "utf-8")


# ---------- ขั้นตอนหลัก ----------
def prepare(state, args):
    """ดึงรูปถัดไป -> ซ้อนกรอบ -> เก็บพรีวิวเป็น 'รอโพสต์'"""
    if args.local:
        svc = None
        files = [{"id": str(p), "name": p.name} for p in Path(args.local).iterdir() if p.suffix.lower() in IMG_EXT]
    elif os.getenv("ONEDRIVE_SHARE_URL", "").strip():
        svc = None
        files = list_onedrive(need("ONEDRIVE_SHARE_URL"))
        set_token(state, {"name": "OneDrive (ลิงก์แชร์)", "expires_at": None})
    else:
        svc = drive_service()
        files = list_drive(svc, need("DRIVE_FOLDER_ID"))
        set_token(state, {"name": "Google Drive (Service Account)", "expires_at": None})
    print(f"[1/3] อ่านโฟลเดอร์ได้ {len(files)} รูป")
    files, rest = pick(files, state.get("last_key"), flag("LOOP"))
    snap = state["snapshot"]
    snap.update(images_total=len(files), images_remaining=len(rest), next_image=rest[0]["name"] if rest else "-")
    if not rest:
        raise Friendly("รูปในโฟลเดอร์หมดแล้ว", "โพสต์ครบทุกรูปในโฟลเดอร์แล้ว",
                       "เพิ่มรูปใหม่ในโฟลเดอร์ Drive โดยตั้งชื่อเลขต่อจากรูปสุดท้าย หรือตั้ง LOOP=true ให้วนกลับรูปแรก")
    item = rest[0]; CTX["image"] = item["name"]
    print(f"[2/3] รูปถัดไปคือ {item['name']} (เหลือ {len(rest)} รูปรวมรูปนี้) กำลังดาวน์โหลดและซ้อนกรอบ")
    if args.local:
        data = Path(item["id"]).read_bytes()
    elif item.get("sid"):
        data = download_onedrive(item)
    else:
        data = download(svc, item["id"])
    PREVIEW.mkdir(parents=True, exist_ok=True)
    rel = "preview/img-" + hashlib.sha1(item["name"].encode()).hexdigest()[:10] + ".jpg"
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / ("src" + (Path(item["name"]).suffix or ".jpg")); src.write_bytes(data)
        out = Path(td) / "out.jpg"; info = compose(str(src), str(FRAME), str(out))
        im = Image.open(out).convert("RGB")
        if im.width > HOSTED_W:
            im = im.resize((HOSTED_W, round(im.height * HOSTED_W / im.width)), Image.LANCZOS)
        im.save(ROOT / "docs" / rel, quality=90)
    print(f"[3/3] ได้รูปพรีวิว docs/{rel} ({info})")
    if args.dry_run:
        return None
    state["pending"] = {"image": item["name"], "key": list(sort_key(item["name"], item.get("ts", ""))), "preview": rel,
                        "caption": caption(), "platforms": platforms(), "done": [],
                        "approved": False, "created_at": now(),
                        "next_image": rest[1]["name"] if len(rest) > 1 else "-"}
    save_state(state); write_status(state)
    return state["pending"]


def publish(state):
    p = state.get("pending")
    if not p:
        raise Friendly("ไม่มีรูปที่รอโพสต์", "ยังไม่มีรูปที่เตรียมไว้", "กด Run workflow แบบ run เพื่อเตรียมรูปใหม่")
    CTX["image"] = p["image"]
    f = ROOT / "docs" / p["preview"]
    if not f.exists():
        raise Friendly("ไม่พบไฟล์รูปที่เตรียมไว้", f"หาไฟล์ {p['preview']} ไม่เจอ", "กด Run workflow แบบ skip แล้วรันใหม่เพื่อเตรียมรูปอีกครั้ง")
    need("FB_PAGE_TOKEN")
    tk = check_fb_token()
    if tk:
        set_token(state, tk)
    img = f.read_bytes()
    for name in [x for x in p["platforms"] if x not in p["done"]]:
        CTX["platform"] = name
        print(f"กำลังโพสต์ลง {name} ...")
        if name == "facebook":
            pid = post_facebook(img, p["caption"])
        elif name == "instagram":
            pid = post_instagram(ensure_public(p["preview"]), p["caption"])
        else:
            continue
        p["done"].append(name); save_state(state)          # จำไว้ทันที กันโพสต์ซ้ำถ้าแพลตฟอร์มถัดไปพลาด
        print(f"  สำเร็จ {name}: {pid}")
    CTX.pop("platform", None)
    snap, left = state["snapshot"], max(state["snapshot"].get("images_remaining", 1) - 1, 0)
    state["last_key"], state["pending"] = p["key"], None
    snap.update(images_remaining=left, next_image=p.get("next_image", "-"))
    record(state, True, p["image"], "โพสต์สำเร็จ: " + ", ".join(p["platforms"]))
    f.unlink(missing_ok=True)
    write_status(state); save_state(state)
    if left in WARN_AT:
        notify(f"⚠️ รูปในโฟลเดอร์เหลือ {left} รูป" + (" (หมดแล้ว รอบหน้าจะไม่มีรูปโพสต์)" if left == 0 else " ควรเพิ่มรูปใหม่"))
    if flag("NOTIFY_ON_SUCCESS"):
        notify(f"✅ โพสต์แล้ว: {p['image']} ({', '.join(p['platforms'])})", photo=img)
    ping(True)


def skip(state):
    p = state.get("pending")
    if not p:
        raise Friendly("ไม่มีรูปที่รอโพสต์", "ไม่มีอะไรให้ข้าม", "ไม่ต้องทำอะไร")
    state["last_key"], state["pending"] = p["key"], None
    (ROOT / "docs" / p["preview"]).unlink(missing_ok=True)
    record(state, True, p["image"], "ข้ามรูปนี้ (ไม่ได้โพสต์)")
    write_status(state); save_state(state)
    print("ข้ามรูป", p["image"])


def cmd_run(state, args):
    p = state.get("pending")
    if p:
        if approval_mode() and not p.get("approved"):
            url = base_url()
            notify(f"⏳ รูป {p['image']} ยังรออนุมัติอยู่" + (f"\nดูพรีวิวและกดอนุมัติ: {url}/" if url else ""),
                   photo=(ROOT / "docs" / p["preview"]).read_bytes())
            write_status(state); ping(True)
            return
        return publish(state)
    p = prepare(state, args)
    if args.dry_run or p is None:
        return
    if approval_mode():
        url = base_url()
        notify(f"🖼️ พรีวิวรูปวันนี้: {p['image']}\nรออนุมัติก่อนโพสต์" + (f"\nกดอนุมัติที่: {url}/" if url else ""),
               photo=(ROOT / "docs" / p["preview"]).read_bytes())
        ping(True)
        return
    p["approved"] = True
    publish(state)


def cmd_approve(state, args):
    p = state.get("pending")
    if not p:
        raise Friendly("ไม่มีรูปที่รออนุมัติ", "ยังไม่มีรูปที่เตรียมไว้ หรือโพสต์ไปแล้ว", "ไม่ต้องทำอะไร")
    p["approved"] = True; save_state(state)
    publish(state)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command", nargs="?", default="run", choices=["run", "approve", "skip"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--local")
    args = ap.parse_args()
    state = load_state()
    try:
        {"run": cmd_run, "approve": cmd_approve, "skip": lambda s, a: skip(s)}[args.command](state, args)
    except Exception as exc:
        traceback.print_exc()
        err = exc if isinstance(exc, Friendly) else explain(exc)
        print(f"\nผิดพลาด: {err.title}\n{err.detail}\nต้องทำ: {err.action}")
        if not args.dry_run:
            done = (state.get("pending") or {}).get("done") or []
            msg = err.title + (f" (โพสต์ {', '.join(done)} สำเร็จแล้ว)" if done else "")
            record(state, False, CTX.get("image"), msg)
            write_status(state, err); save_state(state)
            notify(f"❌ โพสต์ไม่สำเร็จ\n{msg}\n{err.detail}\n\nต้องทำ: {err.action}")
            ping(False)
        sys.exit(1)


if __name__ == "__main__":
    main()
