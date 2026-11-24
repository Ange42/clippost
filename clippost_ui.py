#!/usr/bin/env python3
"""ClipPost : interface locale de publication TikTok (conforme aux Content Sharing Guidelines).

Usage : python3 clippost_ui.py <compte> <dossier_videos> [port]
Puis ouvrir http://localhost:<port>/ (8766 par défaut).

Pour chaque vidéo, l'utilisateur voit le compte connecté (pseudo et avatar), l'aperçu, écrit la légende,
choisit qui peut voir (aucune valeur par défaut), les interactions (décochées par défaut, grisées si le
créateur les a désactivées), la déclaration de contenu commercial, puis confirme. Rien n'est envoyé sans clic.
"""
import hashlib
import html
import secrets as rnd
import json
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path.home() / "YouTube-Shorts"))  # uploader.py (dossier secrets)
import tiktok

COMPTE = sys.argv[1] if len(sys.argv) > 1 else ""
DOSSIER = Path(sys.argv[2] if len(sys.argv) > 2 else ".").expanduser().resolve()
PORT = int(sys.argv[3]) if len(sys.argv) > 3 else 8765  # = port du redirect_uri déclaré chez TikTok
LIBELLES = {"PUBLIC_TO_EVERYONE": "Everyone", "MUTUAL_FOLLOW_FRIENDS": "Friends",
            "FOLLOWER_OF_CREATOR": "Followers", "SELF_ONLY": "Only me"}
TACHES = {}  # publish en cours : nom -> état
OAUTH = {}  # state -> code_verifier (connexion Login Kit en cours)

CSS = """
:root{--bg:#fafafa;--fg:#161616;--muted:#666;--card:#fff;--line:#e3e3e3;--accent:#fe2c55}
@media (prefers-color-scheme:dark){:root{--bg:#121212;--fg:#eee;--muted:#aaa;--card:#1c1c1c;--line:#333}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:980px;margin:0 auto;padding:24px 16px}
.top{display:flex;align-items:center;gap:12px;margin-bottom:20px}.top img{width:44px;height:44px;border-radius:50%}
.grid{display:grid;grid-template-columns:300px 1fr;gap:24px}@media(max-width:720px){.grid{grid-template-columns:1fr}}
video{width:100%;border-radius:12px;background:#000}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px}
label{display:block;margin:10px 0 4px;font-weight:600}textarea,select{width:100%;padding:8px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--fg);font:inherit;box-sizing:border-box}
.row{display:flex;gap:16px;flex-wrap:wrap}.row label{font-weight:400;display:flex;gap:6px;align-items:center}
.muted{color:var(--muted);font-size:13px}button{background:var(--accent);color:#fff;border:0;border-radius:8px;padding:12px 20px;font-weight:700;font-size:15px;cursor:pointer;margin-top:16px}
button:disabled{opacity:.4;cursor:not-allowed}a{color:var(--accent)}ul.list li{margin:6px 0}
"""


def page(titre, corps):
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(titre)}</title><style>{CSS}</style></head><body><main>{corps}</main></body></html>").encode()


def connecte():
    return tiktok.token_path(COMPTE).exists()


def accueil_deconnecte():
    return page("ClipPost", """<div class='top'><div><strong>ClipPost</strong><br><span class='muted'>No TikTok account connected</span></div></div>
<div class='card'><p>Connect the TikTok account you want to post to. You will be asked to log in on TikTok and to approve ClipPost.</p>
<p class='muted'>ClipPost reads your display name and avatar (user.info.basic) and posts only the videos you confirm (video.publish, video.upload).</p>
<a href='/connect'><button type='button'>Connect with TikTok</button></a></div>""")


def videos():
    return sorted(p.name for p in DOSSIER.glob("*.mp4"))


def entete(info):
    av = html.escape(info.get("creator_avatar_url", ""))
    nom = html.escape(info.get("creator_nickname") or info.get("creator_username") or COMPTE)
    return (f"<div class='top'><img src='{av}' alt=''><div><strong>ClipPost</strong><br>"
            f"<span class='muted'>Posting to TikTok as <strong>{nom}</strong> · <a href='/disconnect'>Disconnect</a></span></div></div>")


def formulaire(nom, info):
    opts = "".join(f"<option value='{o}'>{LIBELLES.get(o, o)}</option>" for o in info.get("privacy_level_options", []))
    maxd = info.get("max_video_post_duration_sec", 0)

    def case(cle, txt):
        off = info.get(f"{cle}_disabled")
        return (f"<label><input type='checkbox' name='allow_{cle}' {'disabled' if off else ''}> {txt}"
                f"{' <span class=muted>(disabled in your TikTok settings)</span>' if off else ''}</label>")

    q = urllib.parse.quote(nom)
    return entete(info) + f"""
<p><a href="/">← All videos</a></p>
<div class="grid">
 <div><video id="v" src="/video?f={q}" controls playsinline muted></video>
  <p class="muted" id="dur"></p></div>
 <form class="card" method="post" action="/publish" id="f">
  <input type="hidden" name="f" value="{html.escape(nom)}">
  <label for="t">Caption</label><textarea id="t" name="title" rows="4" maxlength="2200" required></textarea>
  <label for="p">Who can view this video</label>
  <select id="p" name="privacy" required><option value="" selected disabled>Select…</option>{opts}</select>
  <label>Allow users to</label>
  <div class="row">{case('comment', 'Comment')}{case('duet', 'Duet')}{case('stitch', 'Stitch')}</div>
  <label><input type="checkbox" id="cc" name="commercial"> Disclose video content</label>
  <div id="ccbox" style="display:none;margin-left:22px">
   <p class="muted">Turn on to disclose that this video promotes goods or services in exchange for something of value.</p>
   <label style="font-weight:400"><input type="checkbox" id="yb" name="your_brand"> Your brand — <span class="muted">labelled "Promotional content"</span></label>
   <label style="font-weight:400"><input type="checkbox" id="bc" name="branded"> Branded content — <span class="muted">labelled "Paid partnership"</span></label>
   <p class="muted" id="ccmsg"></p>
  </div>
  <p class="muted" id="consent">By posting, you agree to TikTok's <a href="https://www.tiktok.com/legal/page/global/music-usage-confirmation/en" target="_blank">Music Usage Confirmation</a>.</p>
  <button id="go" disabled>Post to TikTok</button>
  <p class="muted">After you post, it may take a few minutes for the video to appear on your profile.</p>
 </form>
</div>
<script>
const f=document.getElementById('f'),go=document.getElementById('go'),p=document.getElementById('p'),
cc=document.getElementById('cc'),yb=document.getElementById('yb'),bc=document.getElementById('bc'),v=document.getElementById('v'),maxd={maxd};
let tropLong=false;
v.addEventListener('loadedmetadata',()=>{{const d=Math.round(v.duration);tropLong=maxd&&d>maxd;
 document.getElementById('dur').textContent='Duration '+d+' s'+(tropLong?' — longer than the '+maxd+' s allowed for this account':'');maj();}});
function maj(){{
 document.getElementById('ccbox').style.display=cc.checked?'block':'none';
 const priv=p.querySelector('option[value=SELF_ONLY]');
 if(priv){{priv.disabled=cc.checked&&bc.checked; if(priv.disabled&&p.value==='SELF_ONLY')p.value='';}}
 let msg='';
 if(cc.checked&&bc.checked)msg='Your photo/video will be labeled as "Paid partnership". Branded content visibility cannot be set to private.';
 else if(cc.checked&&yb.checked)msg='Your photo/video will be labeled as "Promotional content".';
 else if(cc.checked)msg='You need to indicate if your content promotes yourself, a third party, or both.';
 document.getElementById('ccmsg').textContent=msg;
 const lien=bc.checked?"TikTok's <a href='https://www.tiktok.com/legal/page/global/bc-policy/en' target='_blank'>Branded Content Policy</a> and <a href='https://www.tiktok.com/legal/page/global/music-usage-confirmation/en' target='_blank'>Music Usage Confirmation</a>":"TikTok's <a href='https://www.tiktok.com/legal/page/global/music-usage-confirmation/en' target='_blank'>Music Usage Confirmation</a>";
 document.getElementById('consent').innerHTML='By posting, you agree to '+lien+'.';
 go.disabled=!p.value||!document.getElementById('t').value.trim()||tropLong||(cc.checked&&!yb.checked&&!bc.checked);
}}
f.addEventListener('input',maj);f.addEventListener('change',maj);
f.addEventListener('submit',()=>{{go.disabled=true;go.textContent='Posting…';}});
</script>"""


def publier_fond(nom, legende, priv, opts):
    try:
        r = tiktok.publier(COMPTE, DOSSIER / nom, legende, priv, options_post=opts)
        TACHES[nom] = {"etat": "ok", "detail": r}
        tiktok.log(f"OK (ClipPost) {COMPTE} {nom} : {r['statut']} ({priv})")
    except Exception as e:  # noqa: BLE001
        TACHES[nom] = {"etat": "erreur", "detail": str(e)}
        tiktok.log(f"ERREUR (ClipPost) {COMPTE} {nom} : {e}")


class H(BaseHTTPRequestHandler):
    def envoyer(self, corps, code=200, type_="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", type_)
        self.send_header("Content-Length", str(len(corps)))
        self.end_headers()
        self.wfile.write(corps)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        nom = Path(q.get("f", [""])[0]).name
        if u.path == "/connect":
            cfg = tiktok.app()
            verifier, state = rnd.token_urlsafe(64)[:64], rnd.token_urlsafe(16)
            OAUTH[state] = verifier
            url = tiktok.AUTH_URL + "?" + urllib.parse.urlencode({
                "client_key": cfg["client_key"], "scope": tiktok.SCOPES, "response_type": "code",
                "redirect_uri": cfg["redirect_uri"], "state": state,
                "code_challenge": hashlib.sha256(verifier.encode()).hexdigest(), "code_challenge_method": "S256",
                "disable_auto_auth": 1})
            self.send_response(302)
            self.send_header("Location", url)
            return self.end_headers()
        if u.path.rstrip("/") == "/callback":
            state = q.get("state", [""])[0]
            if state not in OAUTH or "code" not in q:
                return self.envoyer(page("ClipPost", f"<p>Connection cancelled: {html.escape(q.get('error_description', q.get('error', ['?']))[0])}</p><p><a href='/'>Back</a></p>"), 400)
            data = tiktok.appel_token({"grant_type": "authorization_code", "code": q["code"][0],
                                       "redirect_uri": tiktok.app()["redirect_uri"], "code_verifier": OAUTH.pop(state)})
            jeton = tiktok.sauver_jeton(COMPTE, data)
            tiktok.log(f"AUTH (ClipPost) {COMPTE} (open_id {jeton['open_id']}, scopes {jeton['scope']})")
            self.send_response(303)
            self.send_header("Location", "/")
            return self.end_headers()
        if u.path == "/disconnect":
            tiktok.token_path(COMPTE).unlink(missing_ok=True)
            self.send_response(303)
            self.send_header("Location", "/")
            return self.end_headers()
        if not connecte():
            return self.envoyer(accueil_deconnecte())
        if u.path == "/":
            info = tiktok.infos_createur(COMPTE)
            items = "".join(f"<li><a href='/post?f={urllib.parse.quote(n)}'>{html.escape(n)}</a>"
                            f"{' — ' + TACHES[n]['etat'] if n in TACHES else ''}</li>" for n in videos())
            return self.envoyer(page("ClipPost", entete(info) + f"<div class='card'><strong>Videos ready to post</strong><ul class='list'>{items or '<li>None</li>'}</ul></div>"))
        if u.path == "/post" and nom in videos():
            return self.envoyer(page("ClipPost", formulaire(nom, tiktok.infos_createur(COMPTE))))
        if u.path == "/video" and nom in videos():
            return self.envoyer((DOSSIER / nom).read_bytes(), type_="video/mp4")
        if u.path == "/status" and nom:
            t = TACHES.get(nom, {"etat": "en cours"})
            if t["etat"] == "en cours":
                corps = "<p>Uploading and processing… this page refreshes automatically.</p><meta http-equiv='refresh' content='4'>"
            elif t["etat"] == "ok":
                corps = f"<p>✅ Posted. Status: <strong>{html.escape(str(t['detail'].get('statut')))}</strong>. It may take a few minutes to appear on your profile.</p>"
            else:
                corps = f"<p>❌ TikTok returned an error: {html.escape(t['detail'])}</p>"
            return self.envoyer(page("ClipPost", corps + "<p><a href='/'>← All videos</a></p>"))
        self.envoyer(b"Not found", 404, "text/plain")

    def do_POST(self):
        if self.path != "/publish":
            return self.envoyer(b"Not found", 404, "text/plain")
        n = int(self.headers.get("Content-Length", 0))
        d = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode()).items()}
        nom = Path(d.get("f", "")).name
        if nom not in videos() or not d.get("privacy") or not d.get("title", "").strip():
            return self.envoyer(page("ClipPost", "<p>Missing information.</p><p><a href='/'>Back</a></p>"), 400)
        com = "commercial" in d
        opts = {"disable_comment": "allow_comment" not in d, "disable_duet": "allow_duet" not in d,
                "disable_stitch": "allow_stitch" not in d,
                "brand_organic_toggle": com and "your_brand" in d, "brand_content_toggle": com and "branded" in d}
        TACHES[nom] = {"etat": "en cours"}
        threading.Thread(target=publier_fond, args=(nom, d["title"].strip(), d["privacy"], opts), daemon=True).start()
        self.send_response(303)
        self.send_header("Location", f"/status?f={urllib.parse.quote(nom)}")
        self.end_headers()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    if not COMPTE:
        raise SystemExit(__doc__)
    print(f"ClipPost : http://localhost:{PORT}/  (compte {COMPTE}, dossier {DOSSIER})")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
