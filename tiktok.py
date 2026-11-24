#!/usr/bin/env python3
"""Publication de vidéos sur TikTok (Content Posting API).

Commandes :
  python3 tiktok.py auth <compte>      connecte un compte TikTok (ouvre le navigateur, une seule fois)
  python3 tiktok.py comptes            affiche chaque compte connecté et ce qu'il a le droit de publier
  python3 tiktok.py publier <compte> <video.mp4> "<légende>" [--visibilite NIVEAU] [--brouillon]

  --visibilite : SELF_ONLY (par défaut, seul choix possible tant que l'app n'est pas auditée),
                 PUBLIC_TO_EVERYONE, MUTUAL_FOLLOW_FRIENDS ou FOLLOWER_OF_CREATOR
  --brouillon  : envoie la vidéo dans la boîte de réception TikTok du compte au lieu de la publier
                 (il faut le scope video.upload) ; tu la finalises ensuite dans l'appli.

Configuration : secrets/tiktok_app.json
  {"client_key": "...", "client_secret": "...", "redirect_uri": "http://localhost:8765/callback/"}
Jetons : secrets/tiktok_token_<compte>.json (renouvelés automatiquement).
"""
import hashlib
import http.server
import json
import os
import secrets as rnd
import sys
import time
import urllib.parse
import webbrowser
from pathlib import Path

import requests

import uploader  # log(), SECRETS

SECRETS = uploader.SECRETS
APP = SECRETS / "tiktok_app.json"
AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
API = "https://open.tiktokapis.com/v2"
SCOPES = "user.info.basic,video.publish,video.upload"
VISIBILITES = {"SELF_ONLY", "PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR"}
MAX_CHUNK_UNIQUE = 64 * 1024 * 1024   # jusqu'à 64 Mo : un seul morceau
CHUNK = 10 * 1024 * 1024              # au-delà : morceaux de 10 Mo, le dernier absorbe le reste


class TikTokErreur(Exception):
    pass


def log(msg):
    uploader.log(f"TIKTOK {msg}")


# ---------------------------------------------------------------- configuration / jetons

def app():
    if not APP.exists():
        raise SystemExit(
            f"Fichier manquant : {APP}\n"
            'Crée-le avec : {"client_key": "...", "client_secret": "...", '
            '"redirect_uri": "http://localhost:8765/callback/"}'
        )
    cfg = json.loads(APP.read_text(encoding="utf-8"))
    for k in ("client_key", "client_secret", "redirect_uri"):
        if not cfg.get(k):
            raise SystemExit(f"{APP.name} : champ '{k}' manquant")
    return cfg


def token_path(compte):
    return SECRETS / f"tiktok_token_{compte}.json"


def sauver_jeton(compte, data):
    now = int(time.time())
    jeton = {
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "open_id": data.get("open_id"),
        "scope": data.get("scope"),
        "expire_le": now + int(data.get("expires_in", 0)),
        "refresh_expire_le": now + int(data.get("refresh_expires_in", 0)),
    }
    p = token_path(compte)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(jeton, indent=2), encoding="utf-8")
    tmp.chmod(0o600)
    os.replace(tmp, p)
    return jeton


def appel_token(params):
    cfg = app()
    params = {"client_key": cfg["client_key"], "client_secret": cfg["client_secret"], **params}
    r = requests.post(f"{API}/oauth/token/", data=params, timeout=30,
                      headers={"Content-Type": "application/x-www-form-urlencoded"})
    data = r.json()
    if "access_token" not in data:
        raise TikTokErreur(f"jeton refusé : {data.get('error')} {data.get('error_description')}")
    return data


def access_token(compte):
    p = token_path(compte)
    if not p.exists():
        raise SystemExit(f"Compte TikTok '{compte}' pas connecté. Lance : python3 tiktok.py auth {compte}")
    jeton = json.loads(p.read_text(encoding="utf-8"))
    if jeton["expire_le"] - 300 > time.time():
        return jeton["access_token"]
    if jeton["refresh_expire_le"] < time.time():
        raise SystemExit(f"Connexion TikTok de '{compte}' expirée. Relance : python3 tiktok.py auth {compte}")
    data = appel_token({"grant_type": "refresh_token", "refresh_token": jeton["refresh_token"]})
    return sauver_jeton(compte, data)["access_token"]


def api(compte, methode, chemin, **kw):
    """Appel à l'API TikTok ; renvoie le champ data ou lève TikTokErreur."""
    headers = {"Authorization": f"Bearer {access_token(compte)}",
               "Content-Type": "application/json; charset=UTF-8"}
    r = requests.request(methode, f"{API}{chemin}", headers=headers, timeout=60, **kw)
    try:
        body = r.json()
    except ValueError:
        raise TikTokErreur(f"HTTP {r.status_code} : {r.text[:300]}")
    err = body.get("error") or {}
    if err.get("code", "ok") != "ok":
        raise TikTokErreur(f"{err.get('code')} : {err.get('message')} (log_id {err.get('log_id')})")
    return body.get("data") or {}


# ---------------------------------------------------------------- connexion (OAuth + PKCE)

def cmd_auth(compte):
    cfg = app()
    redirect = cfg["redirect_uri"]
    verifier = rnd.token_urlsafe(64)[:64]
    # TikTok (bureau) attend le SHA-256 du code_verifier en hexadécimal
    challenge = hashlib.sha256(verifier.encode()).hexdigest()
    state = rnd.token_urlsafe(16)
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_key": cfg["client_key"],
        "scope": SCOPES,
        "response_type": "code",
        "redirect_uri": redirect,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "disable_auto_auth": 1,  # force l'écran de choix du compte
    })

    print(f"Connexion du compte '{compte}' : connecte-toi dans le navigateur avec le BON compte TikTok.")
    print(f"Si le navigateur ne s'ouvre pas, ouvre ce lien :\n{url}\n")
    u = urllib.parse.urlparse(redirect)
    if u.hostname in ("localhost", "127.0.0.1"):
        retour = attendre_retour_local(u, url)
    else:
        webbrowser.open(url)
        retour = input("Après avoir accepté, colle ici l'adresse complète de la page sur laquelle tu arrives :\n> ").strip()
    q = urllib.parse.parse_qs(urllib.parse.urlparse(retour).query)
    if q.get("state", [""])[0] != state:
        raise SystemExit("Réponse invalide (state différent) : recommence.")
    if "code" not in q:
        raise SystemExit(f"Connexion refusée : {q.get('error', ['?'])[0]} {q.get('error_description', [''])[0]}")

    data = appel_token({
        "grant_type": "authorization_code",
        "code": q["code"][0],
        "redirect_uri": redirect,
        "code_verifier": verifier,
    })
    jeton = sauver_jeton(compte, data)
    info = api(compte, "GET", "/user/info/", params={"fields": "open_id,display_name"}).get("user", {})
    log(f"AUTH {compte} -> {info.get('display_name')} (open_id {jeton['open_id']}, scopes {jeton['scope']})")


def attendre_retour_local(u, url):
    """Petit serveur local qui récupère la redirection de TikTok."""
    recu = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if urllib.parse.urlparse(self.path).path.rstrip("/") != (u.path or "/").rstrip("/"):
                self.send_response(404)
                self.end_headers()
                return
            recu["url"] = self.path
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h2>C'est bon, tu peux fermer cet onglet.</h2>".encode())

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer((u.hostname, u.port or 80), H)
    webbrowser.open(url)
    while "url" not in recu:
        srv.handle_request()
    srv.server_close()
    return recu["url"]


# ---------------------------------------------------------------- infos comptes

def comptes_connectes():
    return sorted(p.stem[len("tiktok_token_"):] for p in SECRETS.glob("tiktok_token_*.json"))


def infos_createur(compte):
    return api(compte, "POST", "/post/publish/creator_info/query/", json={})


def cmd_comptes():
    liste = comptes_connectes()
    if not liste:
        print("Aucun compte TikTok connecté. Lance : python3 tiktok.py auth <compte>")
    for c in liste:
        try:
            i = infos_createur(c)
            print(f"{c:15} @{i.get('creator_username')} « {i.get('creator_nickname')} »  "
                  f"visibilités={','.join(i.get('privacy_level_options', []))}  "
                  f"durée max={i.get('max_video_post_duration_sec')} s")
        except (TikTokErreur, SystemExit) as e:
            print(f"{c:15} erreur : {e}")


# ---------------------------------------------------------------- publication

def morceaux(taille):
    if taille <= MAX_CHUNK_UNIQUE:
        return taille, 1
    return CHUNK, taille // CHUNK


def envoyer_fichier(upload_url, chemin, taille, chunk, nb):
    with open(chemin, "rb") as f:
        for i in range(nb):
            debut = i * chunk
            fin = taille - 1 if i == nb - 1 else debut + chunk - 1
            f.seek(debut)
            bloc = f.read(fin - debut + 1)
            r = requests.put(upload_url, data=bloc, timeout=600, headers={
                "Content-Type": "video/mp4",
                "Content-Length": str(len(bloc)),
                "Content-Range": f"bytes {debut}-{fin}/{taille}",
            })
            if r.status_code not in (200, 201, 206):
                raise TikTokErreur(f"envoi du morceau {i + 1}/{nb} refusé : HTTP {r.status_code} {r.text[:300]}")


def attendre_statut(compte, publish_id, delai_max=900):
    fin = time.time() + delai_max
    while time.time() < fin:
        d = api(compte, "POST", "/post/publish/status/fetch/", json={"publish_id": publish_id})
        s = d.get("status")
        if s in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"):
            return d
        if s == "FAILED":
            raise TikTokErreur(f"TikTok a refusé la vidéo : {d.get('fail_reason')}")
        time.sleep(5)
    raise TikTokErreur(f"toujours en traitement après {delai_max} s (publish_id {publish_id})")


def attendre_brouillon(compte, publish_id, taille, delai_max=120):
    fin = time.time() + delai_max
    while time.time() < fin:
        d = api(compte, "POST", "/post/publish/status/fetch/", json={"publish_id": publish_id})
        s = d.get("status")
        if s == "FAILED":
            raise TikTokErreur(f"TikTok a refusé la vidéo : {d.get('fail_reason')}")
        if s in ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE") or int(d.get("uploaded_bytes") or 0) >= taille:
            return d
        time.sleep(5)
    raise TikTokErreur(f"vidéo pas entièrement reçue par TikTok après {delai_max} s (publish_id {publish_id})")


def publier(compte, chemin, legende, visibilite="SELF_ONLY", brouillon=False, options_post=None):
    """Publie (ou envoie en brouillon) une vidéo. Renvoie un dict avec publish_id, statut et post_id éventuel."""
    chemin = Path(chemin)
    taille = chemin.stat().st_size
    chunk, nb = morceaux(taille)
    source = {"source": "FILE_UPLOAD", "video_size": taille, "chunk_size": chunk, "total_chunk_count": nb}

    if brouillon:
        init = api(compte, "POST", "/post/publish/inbox/video/init/", json={"source_info": source})
    else:
        if visibilite not in VISIBILITES:
            raise TikTokErreur(f"visibilité inconnue '{visibilite}'")
        info = infos_createur(compte)  # obligatoire avant chaque publication directe
        options = info.get("privacy_level_options", [])
        if visibilite not in options:
            raise TikTokErreur(f"visibilité {visibilite} pas autorisée pour ce compte (possibles : {', '.join(options)})")
        if len(legende) > 2200:
            raise TikTokErreur(f"légende trop longue ({len(legende)} caractères, 2200 max)")
        post_info = {
            "title": legende,
            "privacy_level": visibilite,
            "disable_comment": bool(info.get("comment_disabled")),
            "disable_duet": bool(info.get("duet_disabled")),
            "disable_stitch": bool(info.get("stitch_disabled")),
            "video_cover_timestamp_ms": 1000,
            "brand_content_toggle": False,
            "brand_organic_toggle": False,
        }
        # Choix faits par l'utilisateur dans l'interface (clippost_ui.py) ; un réglage
        # désactivé par le créateur reste toujours désactivé.
        for k, v in (options_post or {}).items():
            if k in ("disable_comment", "disable_duet", "disable_stitch"):
                post_info[k] = post_info[k] or bool(v)
            elif k in ("brand_content_toggle", "brand_organic_toggle"):
                post_info[k] = bool(v)
        if post_info["brand_content_toggle"] and visibilite == "SELF_ONLY":
            raise TikTokErreur("un contenu de marque ne peut pas être privé")
        init = api(compte, "POST", "/post/publish/video/init/", json={
            "post_info": post_info,
            "source_info": source,
        })

    publish_id = init["publish_id"]
    envoyer_fichier(init["upload_url"], chemin, taille, chunk, nb)
    if brouillon:
        # En brouillon, TikTok peut rester sur PROCESSING_UPLOAD jusqu'à ce que l'utilisateur
        # ouvre la notification : on considère l'envoi réussi dès que tous les octets sont reçus.
        d = attendre_brouillon(compte, publish_id, taille)
        return {"publish_id": publish_id, "statut": d.get("status"), "post_id": None}
    d = attendre_statut(compte, publish_id)
    ids = d.get("publicaly_available_post_id") or []
    return {"publish_id": publish_id, "statut": d.get("status"), "post_id": str(ids[0]) if ids else None}


def cmd_publier(args):
    visibilite, brouillon, reste = "SELF_ONLY", False, []
    it = iter(args)
    for a in it:
        if a == "--visibilite":
            visibilite = next(it, "").upper()
        elif a == "--brouillon":
            brouillon = True
        else:
            reste.append(a)
    if len(reste) != 3:
        raise SystemExit(__doc__)
    compte, video, legende = reste
    if not Path(video).is_file():
        raise SystemExit(f"Fichier introuvable : {video}")
    try:
        r = publier(compte, video, legende, visibilite, brouillon)
    except TikTokErreur as e:
        log(f"ERREUR {compte} {Path(video).name} : {e}")
        sys.exit(1)
    lien = f" -> post {r['post_id']}" if r["post_id"] else ""
    log(f"OK {compte} {Path(video).name} : {r['statut']} ({visibilite if not brouillon else 'brouillon'}){lien}")


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) == 2 and a[0] == "auth":
        cmd_auth(a[1])
    elif a == ["comptes"]:
        cmd_comptes()
    elif a and a[0] == "publier":
        cmd_publier(a[1:])
    else:
        print(__doc__)
