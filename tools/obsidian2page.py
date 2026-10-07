#!/usr/bin/env python3
"""Convert an Obsidian note into a standalone web page for GitHub Pages.

  python3 tools/obsidian2page.py --note "<vault>/Post title..md" --attachments "<vault>" \
      --out post --repo-root <BLE-bench checkout> [--repo-url https://github.com/teleop-bench/BLE-bench-pub] [--noindex]

What it does (and reports):
  - Obsidian image embeds  ![[file.png]] / ![[file.png|428]]  -> <figure><img>, on their own line (even if the embed
    was glued to the end of a sentence); the image files are copied into <out>/img/ with web-safe names.
  - Relative repo links  [text](./path)  -> full GitHub URLs, resolved against the local repo checkout (a bare app
    folder such as ./z54-lat-central/ is found under apps/...). Each link is checked: exists in the repo? publicly
    reachable yet (404 until the repo is public)?
  - Lists: normalizes indentation (lines indented 4+ spaces would otherwise render as code blocks) and adds the blank
    line Markdown needs before a list that follows a paragraph.
  - A line starting with several asterisks (e.g. '****duplex entries') is escaped so it renders as a single '*'.
  - Bare URLs become links; single line breaks are kept (as Obsidian shows them); headings are shifted so the
    page title is the only <h1>.
Nothing is published by this script; it only writes files into --out.
"""
import argparse, html, os, re, shutil, subprocess, sys, unicodedata, urllib.request

try:
    import markdown
except ImportError:
    sys.exit("needs python-markdown:  pip install markdown")

EMBED = re.compile(r'!\[\[([^\]|]+?)(?:\|(\d+))?\]\]')
LIST_ITEM = re.compile(r'^(\s*)([-*+]|\d+[.)])\s+')
REL_LINK = re.compile(r'\]\((\./[^)\s]+)\)')
BARE_URL = re.compile(r'(?<!\]\()(?<![<"\'=])\b(https?://[^\s<>()]+[^\s<>().,;:!?\'"])')   # not inside ](...) or <...>


def slug(name):
    base, ext = os.path.splitext(name)
    s = unicodedata.normalize('NFKD', base).encode('ascii', 'ignore').decode()
    s = re.sub(r'[^A-Za-z0-9]+', '-', s).strip('-').lower()
    return (s or 'image') + ext.lower()


PNG_KEEP = {b'IHDR', b'PLTE', b'IDAT', b'IEND', b'tRNS', b'iCCP', b'sRGB', b'gAMA', b'cHRM', b'pHYs', b'sBIT'}

def strip_png_metadata(path):
    """Drop metadata chunks (eXIf, XMP/iTXt, tEXt, zTXt, tIME, ...) from a published PNG; keep what rendering needs
    (including the colour profile). Returns the number of chunks removed, or None if the file is not a PNG."""
    import struct
    b = open(path, 'rb').read()
    if b[:8] != b'\x89PNG\r\n\x1a\n':
        return None
    out, i, dropped = [b[:8]], 8, 0
    while i + 8 <= len(b):
        n, t = struct.unpack('>I4s', b[i:i + 8])
        chunk = b[i:i + 12 + n]
        if t in PNG_KEEP: out.append(chunk)
        else: dropped += 1
        i += 12 + n
        if t == b'IEND': break
    if dropped:
        open(path, 'wb').write(b''.join(out))
    return dropped


def find_attachment(root, name):
    for dirpath, _, files in os.walk(root):
        if name in files:
            return os.path.join(dirpath, name)
    return None


def fenced_mask(lines):
    inside, mask = False, []
    for l in lines:
        if l.lstrip().startswith('```'):
            mask.append(True); inside = not inside
        else:
            mask.append(inside)
    return mask


def fix_lists(lines, report):
    """Normalize list indentation per block; insert a blank line before a list that follows a paragraph."""
    out, i, mask = [], 0, fenced_mask(lines)
    while i < len(lines):
        if mask[i] or not LIST_ITEM.match(lines[i]):
            out.append(lines[i]); i += 1; continue
        block = []                                  # consecutive list lines (blank lines allowed inside)
        while i < len(lines) and not mask[i] and (LIST_ITEM.match(lines[i]) or
                                                  (not lines[i].strip() and i + 1 < len(lines) and LIST_ITEM.match(lines[i + 1]))):
            block.append(lines[i]); i += 1
        items = [l for l in block if l.strip()]
        indents = sorted({len(LIST_ITEM.match(l).group(1)) for l in items})
        rank = {w: k for k, w in enumerate(indents)}
        first = rank[len(LIST_ITEM.match(items[0]).group(1))]
        prev_level, changed = -1, False
        if out and out[-1].strip() and not LIST_ITEM.match(out[-1]) and not out[-1].lstrip().startswith('#'):
            out.append(''); report['blank_before_list'] += 1
        for l in block:
            if not l.strip():
                out.append(l); continue
            m = LIST_ITEM.match(l)
            level = max(0, min(rank[len(m.group(1))] - first, prev_level + 1))
            new = '    ' * level + l[len(m.group(1)):]
            changed |= (new != l); out.append(new); prev_level = level
        report['lists_normalized'] += int(changed)
    return out


def resolve_repo_path(repo_root, rel, tracked):
    p = rel[2:].rstrip('/')
    if p in tracked['files']: return p, 'blob'
    if p in tracked['dirs']: return p, 'tree'
    cands = sorted(d for d in tracked['dirs'] if d == p or d.endswith('/' + p))
    if len(cands) == 1: return cands[0], 'tree'
    cands = sorted(f for f in tracked['files'] if f.endswith('/' + p))
    if len(cands) == 1: return cands[0], 'blob'
    return None, None


def http_status(url):
    try:
        req = urllib.request.Request(url, method='HEAD', headers={'User-Agent': 'obsidian2page'})
        return urllib.request.urlopen(req, timeout=10).status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return None


CSS = """
:root{--ink:#1d1d1b;--muted:#5f5e59;--paper:#fbfaf6;--rule:#dcd9cf;--link:#7a3e2c;--code:#f1efe7}
@media (prefers-color-scheme:dark){:root{--ink:#e9e7e1;--muted:#a3a19a;--paper:#161614;--rule:#3a3934;--link:#e0a184;--code:#22221f}}
html{background:var(--paper)}body{margin:0;background:var(--paper);color:var(--ink);font:1.08rem/1.65 Charter,"Iowan Old Style",Georgia,serif;-webkit-text-size-adjust:100%}
main{max-width:44rem;margin:0 auto;padding:2.5rem 16px 4rem}
h1{font-size:2.1rem;line-height:1.2;font-weight:600;margin:0 0 1.6rem}
h2{font-size:1.45rem;margin:2.6rem 0 .8rem;line-height:1.3}h3{font-size:1.15rem;margin:2rem 0 .6rem}h4{font-size:1rem;margin:1.6rem 0 .5rem}
p,li{hyphens:auto}a{color:var(--link);text-underline-offset:2px}
figure{margin:1.6rem 0;text-align:center}figure img{max-width:100%;height:auto;border-radius:4px}
table{border-collapse:collapse;width:100%;margin:1.4rem 0;font-size:.92rem;display:block;overflow-x:auto}
th,td{border-bottom:1px solid var(--rule);padding:.45rem .6rem;text-align:left;white-space:nowrap}th{font-weight:600}
pre,code{font:.86rem/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--code);border-radius:4px}
pre{padding:.9rem 1rem;overflow-x:auto;white-space:pre-wrap}code{padding:.1rem .3rem}pre code{padding:0;background:none}
hr{border:0;border-top:1px solid var(--rule);margin:2.5rem 0}
.note-repo{font-size:.85rem;color:var(--muted)}
footer{max-width:44rem;margin:0 auto;padding:1.4rem 16px 3rem;border-top:1px solid var(--rule);font-size:.9rem;color:var(--muted)}
footer a{color:var(--muted)}
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--note', required=True); ap.add_argument('--attachments', required=True)
    ap.add_argument('--out', required=True); ap.add_argument('--repo-root', required=True)
    ap.add_argument('--repo-url', default='https://github.com/teleop-bench/BLE-bench-pub')
    ap.add_argument('--branch', default='main'); ap.add_argument('--title')
    ap.add_argument('--noindex', action='store_true', help='ask search engines not to index (for previews)')
    ap.add_argument('--home', help='also write the site home page (e.g. index.html at the site root) linking to this post')
    ap.add_argument('--site-name', default='teleopbench'); ap.add_argument('--date', help='post date shown on the home page (YYYY-MM-DD)')
    a = ap.parse_args()
    report = {'blank_before_list': 0, 'lists_normalized': 0, 'asterisks_escaped': 0, 'embeds': 0, 'meta_stripped': 0}
    title = a.title or re.sub(r'\.+$', '', os.path.basename(a.note)[:-3] if a.note.endswith('.md') else os.path.basename(a.note))
    src = open(a.note, encoding='utf-8').read()
    lines = src.splitlines()

    # a leading asterisk run that never closes on its line (e.g. '****duplex entries ...', a footnote marker, not
    # bold) is escaped so it renders as a single '*'; '**bold**' at the start of a line is left alone
    mask = fenced_mask(lines)
    for k, l in enumerate(lines):
        m = re.match(r'^(\*{2,})(?=[^\s*])', l)
        if not mask[k] and m and m.group(1) not in l[len(m.group(1)):]:
            lines[k] = '\\* ' + l[len(m.group(1)):]; report['asterisks_escaped'] += 1

    # a table must be followed by a blank line, or the next line becomes a table row
    out, report['blank_after_table'] = [], 0
    for k, l in enumerate(lines):
        out.append(l)
        nxt = lines[k + 1] if k + 1 < len(lines) else ''
        if not mask[k] and l.lstrip().startswith('|') and nxt.strip() and not nxt.lstrip().startswith('|'):
            out.append(''); report['blank_after_table'] += 1
    lines = out

    # embeds onto their own line
    imgdir = os.path.join(a.out, 'img'); os.makedirs(imgdir, exist_ok=True)
    missing, copied = [], []
    def embed(m):
        name, width = m.group(1).strip(), m.group(2)
        report['embeds'] += 1
        path = find_attachment(a.attachments, name)
        dst = slug(name)
        if path:
            shutil.copy2(path, os.path.join(imgdir, dst)); copied.append(name)
            report['meta_stripped'] += strip_png_metadata(os.path.join(imgdir, dst)) or 0
        else:
            missing.append(name)
        w = f' width="{width}"' if width else ''
        return f'\n\n<figure><img src="img/{html.escape(dst)}" alt="" loading="lazy"{w}></figure>\n\n'
    text = EMBED.sub(embed, '\n'.join(lines))
    lines = text.splitlines()

    lines = fix_lists(lines, report)
    text = '\n'.join(lines)

    # relative repo links -> GitHub URLs
    ls = subprocess.run(['git', '-C', a.repo_root, 'ls-files'], capture_output=True, text=True).stdout.split()
    tracked = {'files': set(ls), 'dirs': {os.path.dirname(f) for f in ls} | {'/'.join(f.split('/')[:k]) for f in ls for k in range(1, f.count('/') + 1)}}
    links = []
    def rel(m):
        r = m.group(1)
        path, kind = resolve_repo_path(a.repo_root, r, tracked)
        if not path:
            links.append((r, None, 'NOT FOUND in repo')); return m.group(0)
        url = f'{a.repo_url}/{kind}/{a.branch}/{path}'
        links.append((r, url, None)); return f']({url})'
    text = REL_LINK.sub(rel, text)

    # link targets that look like a domain but lack a scheme ('nordicsemi.com/...') would resolve relative to the
    # site and 404; prepend https://
    schemeless = []
    def add_scheme(m):
        tgt = m.group(1)
        schemeless.append(tgt)
        return f'](https://{tgt})'
    text = re.sub(r'\]\(((?:www\.)?[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?:/[^)\s]*)?)\)', add_scheme, text)

    # bare URLs -> links (outside fenced code and existing links)
    mask = fenced_mask(text.splitlines()); out = []
    for k, l in enumerate(text.splitlines()):
        out.append(l if mask[k] or l.lstrip().startswith('<figure') else BARE_URL.sub(r'<\1>', l))
    text = '\n'.join(out)

    # headings: shallowest heading in the note becomes <h2> (the title is the page's only <h1>)
    levels = [len(m.group(1)) for m in re.finditer(r'^(#{1,6})\s', text, re.M)]
    shift = (min(levels) - 2) if levels else 0
    if shift:
        text = re.sub(r'^(#{1,6})(\s)', lambda m: '#' * max(1, len(m.group(1)) - shift) + m.group(2), text, flags=re.M)

    body = markdown.markdown(text, extensions=['tables', 'fenced_code', 'nl2br', 'sane_lists'], output_format='html5')
    first_p = re.search(r'<p>(.*?)</p>', body, re.S)
    desc = re.sub(r'<[^>]+>', '', first_p.group(1)).strip() if first_p else title
    desc = html.escape(re.sub(r'\s+', ' ', desc)[:200])
    robots = '<meta name="robots" content="noindex">\n' if a.noindex else ''
    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<meta name="description" content="{desc}">
<meta property="og:title" content="{html.escape(title)}">
<meta property="og:description" content="{desc}">
<meta property="og:type" content="article">
{robots}<style>{CSS}</style>
</head>
<body>
<main>
<h1>{html.escape(title)}</h1>
{body}
</main>
<footer><a href="{html.escape(a.repo_url)}">GitHub: {html.escape(a.repo_url.split('github.com/')[-1])}</a></footer>
</body>
</html>
"""
    open(os.path.join(a.out, 'index.html'), 'w', encoding='utf-8').write(page)
    if a.home:
        rel = os.path.relpath(a.out, os.path.dirname(os.path.abspath(a.home)) or '.').strip('./') + '/'
        date = f'<span class="date">{html.escape(a.date)}</span> ' if a.date else ''
        home = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(a.site_name)}</title>
<meta name="description" content="{html.escape(title)}">
{robots}<style>{CSS}
.posts{{list-style:none;padding:0}}.posts li{{margin:.8rem 0}}.date{{color:var(--muted);font-size:.9rem;margin-right:.5rem}}</style>
</head>
<body>
<main>
<h1>{html.escape(a.site_name)}</h1>
<ul class="posts"><li>{date}<a href="{html.escape(rel)}">{html.escape(title)}</a></li></ul>
</main>
<footer><a href="{html.escape(a.repo_url)}">GitHub: {html.escape(a.repo_url.split('github.com/')[-1])}</a></footer>
</body>
</html>
"""
        open(a.home, 'w', encoding='utf-8').write(home)
        print(f'wrote home page {a.home} -> links to {rel}')

    print(f'wrote {os.path.join(a.out, "index.html")}  ({len(page)//1024} KB)')
    print(f'fixes: {report["asterisks_escaped"]} asterisk run(s) escaped; {report["blank_after_table"]} blank line(s) added after tables; {report["lists_normalized"]} list block(s) re-indented; '
          f'{report["blank_before_list"]} blank line(s) added before lists; {report["embeds"]} image embed(s) put on their own line')
    print(f'images: {len(copied)} copied ({report["meta_stripped"]} metadata chunk(s) stripped: EXIF/XMP/text), {len(missing)} missing' + ''.join(f'\n  MISSING: {n}' for n in missing))
    print(f'images without alt text: {report["embeds"]} (Obsidian embeds carry none; consider adding descriptions)')
    for t in schemeless:
        print(f'link missing https:// fixed: {t} -> https://{t}')
    print('repo links:')
    for r, url, err in links:
        if err: print(f'  {r:55s} -> {err}'); continue
        st = http_status(url)
        tag = 'OK' if st == 200 else ('404: not public yet' if st == 404 else f'status {st}')
        print(f'  {r:55s} -> {url}  [{tag}]')
    return 1 if missing or any(e for _, _, e in links) else 0


if __name__ == '__main__':
    sys.exit(main())
