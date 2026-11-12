"""Daily check for Fractal Audio updates that may affect the Field Guide.

Looks at three public sources (no sign-in, no browser):
  - the firmware version on the Axe-Fx III, FM9 and FM3 download pages
  - firmware/beta/release threads started by Fractal staff in the forum RSS feeds
  - edits to the wiki pages listed in pages.txt (via the wiki's API)

Usage: python watch.py STATE_JSON OUT_DIR
Reads and rewrites STATE_JSON. If something new turned up, writes OUT_DIR/title.txt
and OUT_DIR/body.md for the workflow to open as a GitHub issue.
Standard library only.
"""
import json, os, re, sys, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

UA = 'FractalFieldGuideWatcher/1.0 (+https://fractalfieldguide.github.io/fractal-field-guide/)'
HERE = os.path.dirname(os.path.abspath(__file__))
WIKI_API = 'https://wiki.fractalaudio.com/wiki/api.php'
WIKI = 'https://wiki.fractalaudio.com/wiki/index.php'
DOWNLOADS = {'Axe-Fx III': 'https://www.fractalaudio.com/axe-fx-iii-downloads/',
             'FM9': 'https://www.fractalaudio.com/fm9-downloads/',
             'FM3': 'https://www.fractalaudio.com/fm3-downloads/'}
FORUMS = {'Axe-Fx III': 'https://forum.fractalaudio.com/forums/axe-fx-iii-discussion.100/index.rss',
          'FM9': 'https://forum.fractalaudio.com/forums/fm9-discussion.139/index.rss',
          'FM3': 'https://forum.fractalaudio.com/forums/fm3-discussion.114/index.rss'}
STAFF = {'FractalAudio', 'Admin M@'}
FW_TITLE = re.compile(r'firmware|beta|release|public', re.I)
DC = '{http://purl.org/dc/elements/1.1/}'
FAIL_ALERT = 3  # consecutive failed runs before a source gets its own warning


def get(url):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode('utf-8', 'replace')


def check_firmware(state, found):
    seen = state.setdefault('firmware', {})
    for dev, url in DOWNLOADS.items():
        m = re.search(r'w-iconbox-title">\s*Firmware\s+v?(\d+(?:\.\d+)+)\s*<', get(url))
        if not m:
            raise RuntimeError(f'no firmware version found on the {dev} download page')
        ver = m.group(1)
        if dev in seen and seen[dev] != ver:
            found.append(f'**{dev} firmware {ver}** is on the [download page]({url}) (was {seen[dev]}).')
        seen[dev] = ver


def check_forum(state, found, first_run):
    seen = set(state.get('forum_seen', []))
    for dev, url in FORUMS.items():
        root = ET.fromstring(get(url))
        for it in root.iter('item'):
            link, title = it.findtext('link') or '', it.findtext('title') or ''
            who = it.findtext(DC + 'creator') or ''
            if who in STAFF and FW_TITLE.search(title) and link not in seen:
                seen.add(link)
                if not first_run:
                    found.append(f'Forum ({dev}): [{title}]({link}), posted by {who}.')
    state['forum_seen'] = sorted(seen)[-300:]


def check_wiki(state, found):
    watched = {l.strip() for l in open(os.path.join(HERE, 'pages.txt')) if l.strip() and not l.startswith('#')}
    now = datetime.now(timezone.utc).replace(microsecond=0)
    since = state.get('wiki_since') or (now - timedelta(days=1)).isoformat().replace('+00:00', 'Z')
    params = {'action': 'query', 'format': 'json', 'list': 'recentchanges', 'rcnamespace': '0',
              'rctype': 'edit|new', 'rclimit': '500', 'rcprop': 'title|ids|timestamp|user|comment|sizes',
              'rcend': since, 'rcstart': now.isoformat().replace('+00:00', 'Z')}
    pages = {}
    while True:
        d = json.loads(get(WIKI_API + '?' + urllib.parse.urlencode(params)))
        for c in d['query']['recentchanges']:
            if c['title'] not in watched or c['timestamp'] <= since:
                continue
            p = pages.setdefault(c['title'], {'old': c.get('old_revid', 0), 'new': c['revid'], 'n': 0, 'users': set(), 'notes': []})
            p['old'] = min(p['old'], c.get('old_revid', 0)) if p['n'] else c.get('old_revid', 0)
            p['new'] = max(p['new'], c['revid'])
            p['n'] += 1
            p['users'].add(c.get('user', '?'))
            if c.get('comment'):
                p['notes'].append(c['comment'][:120])
        if 'continue' not in d:
            break
        params.update(d['continue'])
        time.sleep(1)
    for t in sorted(pages):
        p = pages[t]
        q = {'title': t.replace(' ', '_'), 'diff': p['new']}
        if p['old']:
            q['oldid'] = p['old']
        notes = '; '.join(dict.fromkeys(p['notes']))[:300]
        found.append(f'Wiki: [{t}]({WIKI}?{urllib.parse.urlencode(q)}) edited {p["n"]}x by {", ".join(sorted(p["users"]))}'
                     + (f' ({notes})' if notes else '') + '.')
    state['wiki_since'] = now.isoformat().replace('+00:00', 'Z')


def main():
    state_path, out = sys.argv[1], sys.argv[2]
    state = json.load(open(state_path)) if os.path.exists(state_path) else {}
    first_run = not state
    fails = state.setdefault('fails', {})
    found, problems = [], []
    for name, fn in (('download pages', lambda: check_firmware(state, found)),
                     ('forum feeds', lambda: check_forum(state, found, first_run)),
                     ('wiki', lambda: check_wiki(state, found))):
        try:
            fn()
            fails[name] = 0
        except Exception as e:  # keep checking the other sources
            fails[name] = fails.get(name, 0) + 1
            print(f'{name}: {e}', file=sys.stderr)
            if fails[name] == FAIL_ALERT:
                problems.append(f'The watcher could not read the **{name}** {FAIL_ALERT} days in a row (last error: `{str(e)[:200]}`). '
                                'The site may be blocking automated requests; if so the watcher should not try to get around it.')
    json.dump(state, open(state_path, 'w'), indent=1, sort_keys=True)
    os.makedirs(out, exist_ok=True)
    if first_run:
        print('first run: saved current versions, nothing reported')
        return
    if not found and not problems:
        print('nothing new')
        return
    fw = [f for f in found if 'firmware' in f.lower() and not f.startswith('Wiki')]
    wiki = [f for f in found if f.startswith('Wiki')]
    bits = (['new firmware'] if fw else []) + ([f'{len(wiki)} wiki page{"s" if len(wiki) > 1 else ""} changed'] if wiki else []) + (['watcher problem'] if problems else [])
    title = f'Fractal updates {datetime.now(timezone.utc):%Y-%m-%d}: ' + ', '.join(bits)
    body = ['Found by the daily update check. Nothing on the site has been changed.', '']
    body += [f'- {x}' for x in found + problems]
    body += ['', 'To update the guide, ask Claude to "update the Field Guide from this issue" and paste its link. Close the issue when done.']
    open(os.path.join(out, 'title.txt'), 'w').write(title)
    open(os.path.join(out, 'body.md'), 'w').write('\n'.join(body) + '\n')
    print(title)


if __name__ == '__main__':
    main()
