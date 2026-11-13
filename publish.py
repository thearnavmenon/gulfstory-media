#!/usr/bin/env python3
"""The Gulf Story autopost: publishes approved reels from queue.json via the Instagram API.

  python3 publish.py status              show the queue
  python3 publish.py approve EP06 EP07   approve items (only approved items ever post)
  python3 publish.py unapprove EP07      take an item back out
  python3 publish.py dry-run EP06        upload + process on Meta, then STOP before publishing
  python3 publish.py run                 what the timer calls: publish the next due approved item

Safety rules built in:
- only items with "approved": true are published, one per run, and not within 60 min of the last post;
- media_publish is never retried blindly: a stuck item is resolved from the container's own status,
  so a reel can't go out twice;
- the token is read from .env and never printed or logged.
"""
import datetime as dt, fcntl, json, os, pathlib, subprocess, sys, time
import urllib.error, urllib.parse, urllib.request

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent                      # the-gulf-story/
QUEUE = pathlib.Path(os.environ.get('AUTOPOST_QUEUE', HERE / 'queue.json'))
CLOUD = os.environ.get('AUTOPOST_CLOUD') == '1'   # running on GitHub Actions
LOG = HERE / 'autopost.log'
POSTED = HERE / 'posted.log'
CACHE = HERE / 'cache'
GRAPH = 'https://graph.facebook.com/v27.0'
TZ = dt.timezone(dt.timedelta(hours=4))
MIN_GAP = dt.timedelta(minutes=60)
DONE = ('published',)


# ---------- small helpers ----------
def env():
    e = {}
    if (HERE / '.env').exists():
        for line in (HERE / '.env').read_text().splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                k, v = line.split('=', 1)
                e[k.strip()] = v.strip().strip('"').strip("'")
    for k in ('IG_ACCESS_TOKEN', 'IG_USER_ID', 'MEDIA_KEY', 'MEDIA_REPO'):
        if os.environ.get(k):
            e[k] = os.environ[k]
    if not e.get('IG_ACCESS_TOKEN'):
        raise SystemExit('IG_ACCESS_TOKEN missing in .env')
    e.setdefault('IG_USER_ID', '17841453759133381')
    return e


E = None


def now():
    return dt.datetime.now(TZ)


def log(msg):
    line = f'{now():%Y-%m-%d %H:%M:%S} {msg}'
    if E:
        line = line.replace(E['IG_ACCESS_TOKEN'], '[TOKEN]')
    print(line, flush=True)
    with open(LOG, 'a') as f:
        f.write(line + '\n')


def notify(title, msg):
    try:
        subprocess.run(['osascript', '-e', f'display notification {json.dumps(msg)} with title {json.dumps(title)}'],
                       timeout=10, capture_output=True)
    except Exception:
        pass


def load():
    return json.loads(QUEUE.read_text())


def save(q):
    tmp = QUEUE.with_suffix('.tmp')
    tmp.write_text(json.dumps(q, indent=2, ensure_ascii=False) + '\n')
    tmp.replace(QUEUE)


class ApiError(Exception):
    pass


def api(method, url, params=None, headers=None, data=None, timeout=120):
    h = {'Authorization': f"Bearer {E['IG_ACCESS_TOKEN']}"}
    h.update(headers or {})
    if not url.startswith('http'):
        url = f'{GRAPH}/{url}'
    body = data
    if params is not None:
        enc = urllib.parse.urlencode(params)
        if method == 'GET':
            url += ('&' if '?' in url else '?') + enc
        else:
            body = enc.encode()
            h['Content-Type'] = 'application/x-www-form-urlencoded'
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            err = json.load(e)
        except Exception:
            err = {'status': e.code}
        raise ApiError(json.dumps(err).replace(E['IG_ACCESS_TOKEN'], '[TOKEN]')) from None


# ---------- steps ----------
def prepare_video(item):
    """Meta's spec asks for 128 kbps AAC and the moov atom at the front. Copy the picture, re-encode only audio."""
    src = ROOT / item['video']
    CACHE.mkdir(exist_ok=True)
    out = CACHE / f"{item['id']}.mp4"
    if not out.exists() or out.stat().st_mtime < src.stat().st_mtime:
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(src), '-c:v', 'copy', '-c:a', 'aac', '-b:a', '128k',
                        '-ar', '48000', '-movflags', '+faststart', str(out)], check=True)
    return out


# Direct upload from the Mac stalls for full reels (Meta gives up after ~30 s; see PLAN.md), so Meta fetches
# the files from temporary GitHub release assets, which are deleted once the post is live.
def media_repo():
    return E.get('MEDIA_REPO', 'thearnavmenon/gulfstory-media')


def host_names(item):
    return f"{item['id']}.mp4", f"{item['id']}-cover.jpg"


def crypt(src, dst, decrypt):
    """AES-256 with the MEDIA_KEY; the key is passed through the environment, never on the command line."""
    args = ['openssl', 'enc', '-aes-256-cbc', '-pbkdf2', '-iter', '200000', '-in', str(src), '-out', str(dst),
            '-pass', 'env:MEDIA_KEY']
    if decrypt:
        args.insert(2, '-d')
    subprocess.run(args, check=True, env={**os.environ, 'MEDIA_KEY': E['MEDIA_KEY']}, capture_output=True)


def vault_fetch(item):
    """Cloud: pull this reel's encrypted files from the 'vault' release and decrypt them."""
    CACHE.mkdir(exist_ok=True)
    vname, cname = host_names(item)
    pats = [vname + '.enc'] + ([cname + '.enc'] if item.get('cover') else [])
    args = ['gh', 'release', 'download', 'vault', '--repo', media_repo(), '-D', str(CACHE), '--clobber']
    for p in pats:
        args += ['-p', p]
    subprocess.run(args, check=True, capture_output=True, timeout=600)
    for p in pats:
        crypt(CACHE / p, CACHE / p[:-4], decrypt=True)
    return CACHE / vname, (CACHE / cname if item.get('cover') else None)


def push_media(item):
    """Mac: prepare, encrypt and upload a reel's files to the 'vault' release ahead of time."""
    video = prepare_video(item)
    vname, cname = host_names(item)
    files = [CACHE / (vname + '.enc')]
    crypt(video, files[0], decrypt=False)
    if item.get('cover'):
        files.append(CACHE / (cname + '.enc'))
        crypt(ROOT / item['cover'], files[1], decrypt=False)
    subprocess.run(['gh', 'release', 'upload', 'vault', *map(str, files), '--repo', media_repo(), '--clobber'],
                   check=True, capture_output=True, timeout=900)
    log(f"{item['id']}: encrypted files uploaded to the vault")


def host_upload(item):
    vname, cname = host_names(item)
    if CLOUD:
        video, cover = vault_fetch(item)
    else:
        video = prepare_video(item)
        cover = None
        if item.get('cover'):
            cover = CACHE / cname
            cover.write_bytes((ROOT / item['cover']).read_bytes())
    files = [str(video)] + ([str(cover)] if cover else [])
    subprocess.run(['gh', 'release', 'upload', 'media', *files, '--repo', media_repo(), '--clobber'],
                   check=True, capture_output=True, timeout=900)
    base = f'https://github.com/{media_repo()}/releases/download/media/'
    urls = {'video_url': base + vname}
    if item.get('cover'):
        urls['cover_url'] = base + cname
    for u in urls.values():
        req = urllib.request.Request(u, method='HEAD')
        with urllib.request.urlopen(req, timeout=60) as r:
            if r.status != 200:
                raise RuntimeError(f'{u} not reachable ({r.status})')
    log(f"{item['id']}: files online for Meta to fetch ({video.stat().st_size / 1e6:.1f} MB video{', cover' if 'cover_url' in urls else ''})")
    return urls


def host_delete(item, vault_too=False):
    for name in host_names(item):
        subprocess.run(['gh', 'release', 'delete-asset', 'media', name, '--repo', media_repo(), '-y'],
                       capture_output=True, timeout=120)
        if vault_too:
            subprocess.run(['gh', 'release', 'delete-asset', 'vault', name + '.enc', '--repo', media_repo(), '-y'],
                           capture_output=True, timeout=120)
    log(f"{item['id']}: temporary files removed from GitHub")


def make_container(item, dry):
    p = {'media_type': 'REELS', 'caption': item['caption'], 'share_to_feed': 'true'}
    if item.get('ai_label', True):
        p['is_ai_generated'] = 'true'
    p.update(host_upload(item))
    if 'cover_url' not in p:
        p['thumb_offset'] = '0'
    r = api('POST', f"{E['IG_USER_ID']}/media", p)
    log(f"{item['id']}: container {r['id']} created ({'designed cover' if 'cover_url' in p else 'first frame as cover'})")
    return r['id']


def container_status(cid):
    return api('GET', cid, {'fields': 'status_code,status'})


def wait_ready(item, cid, tries=15):
    for _ in range(tries):
        s = container_status(cid)
        code = s.get('status_code')
        if code == 'FINISHED':
            log(f"{item['id']}: container {cid} processed, ready to publish")
            return True
        if code in ('ERROR', 'EXPIRED'):
            raise RuntimeError(f"container {cid} {code}: {s.get('status')}")
        time.sleep(60)
    raise RuntimeError(f'container {cid} still processing after {tries} min')


def find_published(item):
    """Find our media by caption, for when publish went through but we didn't get the reply."""
    r = api('GET', f"{E['IG_USER_ID']}/media", {'fields': 'id,caption,timestamp', 'limit': '10'})
    for m in r.get('data', []):
        if (m.get('caption') or '').strip() == item['caption'].strip():
            return m['id']
    return None


def post_comments(q, item):
    done = item.get('comments_done', 0)
    for text in item['comments'][done:]:
        api('POST', f"{item['media_id']}/comments", {'message': text})
        item['comments_done'] = item.get('comments_done', 0) + 1
        save(q)
        log(f"{item['id']}: comment {item['comments_done']}/{len(item['comments'])} posted")
        time.sleep(5)


def publish_item(q, item):
    # Resume safely from whatever state a previous run left.
    cid = item.get('container')
    if cid and item['state'] in ('container_ready', 'publishing'):
        s = container_status(cid).get('status_code')
        if s == 'PUBLISHED':
            mid = find_published(item)
            if not mid:
                item['state'] = 'needs_check'
                save(q)
                raise RuntimeError(f'container {cid} says PUBLISHED but the post was not found; check the profile')
            item.update(media_id=mid, state='published')
            save(q)
        elif s in ('FINISHED', 'IN_PROGRESS'):
            item['state'] = 'container_ready'   # not published yet, per Meta's own status
            save(q)
        elif s in ('ERROR', 'EXPIRED', None):
            item.pop('container', None)
            item['state'] = 'queued'
            save(q)
            cid = None
    if item['state'] == 'queued':
        cid = make_container(item, dry=False)
        item.update(container=cid, state='container_ready', container_at=now().isoformat())
        save(q)
    if item['state'] == 'container_ready':
        wait_ready(item, cid)
        lim = api('GET', f"{E['IG_USER_ID']}/content_publishing_limit", {'fields': 'quota_usage,config'})['data'][0]
        if lim['quota_usage'] >= lim['config']['quota_total']:
            raise RuntimeError('publishing quota used up for the last 24 h')
        item['state'] = 'publishing'
        save(q)
        try:
            mid = api('POST', f"{E['IG_USER_ID']}/media_publish", {'creation_id': cid})['id']
        except Exception:
            # Outcome unknown: the next run resolves it from the container's status. Never re-publish blindly.
            raise
        item.update(media_id=mid, state='published', published_at=now().isoformat())
        save(q)
        log(f"{item['id']}: PUBLISHED media {mid}")
        host_delete(item, vault_too=True)
    if item['state'] == 'published':
        post_comments(q, item)
        try:
            item['permalink'] = api('GET', item['media_id'], {'fields': 'permalink'}).get('permalink')
        except Exception:
            pass
        save(q)
        with open(POSTED, 'a') as f:
            f.write(f"{now():%Y-%m-%d %H:%M} {item['id']} {item.get('permalink', item['media_id'])}\n")
        notify('The Gulf Story', f"{item['id']} is live, with {item.get('comments_done', 0)} comments.")


# ---------- commands ----------
def cmd_status(q):
    for i in q['items']:
        extra = i.get('permalink') or i.get('error') or ''
        print(f"{i['id']:5} {i['slot'][:16].replace('T', ' ')}  {'approved' if i['approved'] else 'NOT approved':12}  {i['state']:15} {extra}")


def cmd_approve(q, ids, val):
    for i in q['items']:
        if i['id'] in ids:
            i['approved'] = val
            log(f"{i['id']}: {'approved' if val else 'unapproved'}")
    save(q)


def cmd_dry(q, iid):
    item = next(i for i in q['items'] if i['id'] == iid)
    try:
        cid = make_container(item, dry=True)
        wait_ready(item, cid)
        log(f'{iid}: DRY RUN OK. Container {cid} is ready; not published (it expires unused in 24 h).')
    finally:
        host_delete(item)


def cmd_run(q):
    t = now()
    last = max((dt.datetime.fromisoformat(i['published_at']) for i in q['items'] if i.get('published_at')), default=None)
    unfinished = [i for i in q['items'] if i['state'] == 'published' and i.get('comments_done', 0) < len(i['comments'])]
    for i in unfinished:
        post_comments(q, i)
    due = [i for i in q['items'] if i['approved'] and i['state'] not in DONE + ('failed', 'needs_check')
           and dt.datetime.fromisoformat(i['slot']) <= t]
    if not due:
        log('run: nothing due')
        return
    if last and t - last < MIN_GAP:
        log(f"run: {due[0]['id']} is due but the last post was under 60 min ago; waiting for the next slot")
        return
    item = due[0]
    try:
        publish_item(q, item)
        item.pop('error', None)
    except Exception as e:
        item['attempts'] = item.get('attempts', 0) + 1
        item['error'] = str(e)[:300]
        if item['attempts'] >= 3 and item['state'] != 'publishing':
            item['state'] = 'failed'
        save(q)
        log(f"{item['id']}: FAILED ({item['attempts']}): {e}")
        notify('The Gulf Story: post failed', f"{item['id']}: {str(e)[:120]}")
        if CLOUD:
            sys.exit(1)   # fails the GitHub run, which emails the owner


def main():
    global E
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    E = env()
    with open(HERE / '.lock', 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('another autopost run is in progress')
        q = load()
        c = sys.argv[1]
        if c == 'status':
            cmd_status(q)
        elif c == 'approve':
            cmd_approve(q, sys.argv[2:], True)
        elif c == 'unapprove':
            cmd_approve(q, sys.argv[2:], False)
        elif c == 'dry-run':
            cmd_dry(q, sys.argv[2])
        elif c == 'push-media':
            for iid in sys.argv[2:]:
                push_media(next(i for i in q['items'] if i['id'] == iid))
        elif c == 'run':
            cmd_run(q)
        else:
            raise SystemExit(__doc__)


if __name__ == '__main__':
    main()
