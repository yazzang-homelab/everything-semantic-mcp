"""Tiny local web UI (stdlib only): search box + results, bound to 127.0.0.1.

Started by double-clicking the executable (``findex`` with no arguments). Serves one HTML page
and a JSON API backed by the same functions as the MCP tools, so people and agents see the
same results.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__, semantic
from . import server as srv

PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>findex</title>
<style>
:root{--bg:#0b0f17;--p:#121826;--l:#1f2a3d;--i:#e7edf7;--m:#8b98ad;--a:#ffb020;--g:#3ddc97}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--i);font:15px "Segoe UI","Malgun Gothic","Apple SD Gothic Neo","Noto Sans KR",sans-serif}
header{position:sticky;top:0;background:var(--bg);padding:16px 18px 10px;border-bottom:1px solid var(--l)}
.row{display:flex;gap:8px;align-items:center}
input{flex:1;font:inherit;font-size:17px;padding:12px 14px;border-radius:10px;border:1px solid var(--l);background:var(--p);color:var(--i);outline:none}
input:focus{border-color:var(--a)}
select,button{font:inherit;padding:11px 12px;border-radius:10px;border:1px solid var(--l);background:var(--p);color:var(--i);cursor:pointer}
button.primary{border-color:#3a2e10;background:#1a1408;color:var(--a);font-weight:700}
.meta{color:var(--m);font-size:13px;margin-top:8px;display:flex;gap:14px;flex-wrap:wrap}
ul{list-style:none;margin:0;padding:6px 10px 60px}
li{padding:9px 10px;border-radius:8px;display:flex;gap:10px;align-items:baseline;cursor:pointer}
li:hover{background:var(--p)}
.k{font-size:12px;color:var(--m);width:34px;flex:none}.n{font-weight:600;word-break:break-all}
.d{color:var(--m);font-size:13px;word-break:break-all}.s{margin-left:auto;color:var(--m);font-size:12px;white-space:nowrap}
.tag{font-size:11px;padding:1px 6px;border-radius:6px;border:1px solid var(--l);color:var(--g)}
.warn{color:var(--a)}
</style></head><body>
<header>
 <div class="row">
  <input id="q" placeholder="파일·폴더 이름 또는 '찾고 싶은 내용'  (예: 신입 교육 자료, ext:pdf dm:last7days)" autofocus>
  <select id="mode"><option value="find">뜻까지 (find)</option><option value="search">이름 (search)</option></select>
  <button class="primary" id="go">검색</button>
 </div>
 <div class="meta"><span id="info">준비 중…</span><span id="st"></span>
  <a href="#" id="rescan" style="color:var(--m)">이름 색인 새로고침</a>
  <a href="#" id="reindex" style="color:var(--m)">의미 색인 만들기</a></div>
</header>
<ul id="out"></ul>
<script>
const $=s=>document.querySelector(s);let t=null,seq=0;
const fmt=n=>n==null?'':n>1e9?(n/1e9).toFixed(1)+' GB':n>1e6?(n/1e6).toFixed(1)+' MB':n>1e3?(n/1e3).toFixed(0)+' KB':n+' B';
function split(p){const i=Math.max(p.lastIndexOf('\\\\'),p.lastIndexOf('/'));return [p.slice(i+1)||p,p.slice(0,i)]}
async function run(){const q=$('#q').value.trim(),mode=$('#mode').value,my=++seq;if(!q&&mode==='find'){$('#out').innerHTML='';return}
 const r=await (await fetch('/api/'+mode+'?q='+encodeURIComponent(q))).json();if(my!==seq)return;
 const items=r.results||[];const mn=r.meaning_note?` · <span class="warn">${r.meaning_note.includes('loading')?'의미 모델 준비 중 — 이름 결과만 표시':'의미 색인 없음 — 이름 결과만'}</span>`:'';$('#info').innerHTML=(mode==='search'?`${(r.total||0).toLocaleString()}개 중 ${items.length}개`:`${items.length}개`)+` · ${r.elapsed_ms??''} ms`+(r.match==='relaxed'?' · <span class="warn">모든 단어가 들어간 항목이 없어 가장 많이 맞는 순</span>':'')+mn;
 $('#out').innerHTML=items.map(i=>{const p=i.full_path||i.path;const[n,d]=split(p);
  return `<li data-p="${encodeURIComponent(p)}"><span class="k">${i.type==='folder'?'📁':'📄'}</span><div><div class="n">${n.replace(/</g,'&lt;')} ${i.meaning_score?`<span class="tag">의미 ${i.meaning_score}</span>`:''}</div><div class="d">${d.replace(/</g,'&lt;')}</div></div><span class="s">${fmt(i.size)} ${(i.date_modified||'').slice(0,10)}</span></li>`}).join('')}
$('#q').addEventListener('input',()=>{clearTimeout(t);t=setTimeout(run,$('#mode').value==='search'?120:350)});
$('#q').addEventListener('keydown',e=>{if(e.key==='Enter')run()});$('#go').onclick=run;$('#mode').onchange=run;
$('#out').addEventListener('click',e=>{const li=e.target.closest('li');if(li)fetch('/api/reveal?p='+li.dataset.p)});
async function status(){const s=await (await fetch('/api/status')).json();const e=s.engine,sm=s.semantic.index;
 $('#st').textContent=`엔진: ${e.engine==='everything'?'Everything':'내장 색인'} · 이름 ${(e.entries??'').toLocaleString?.()||''}${e.scanning?' · 스캔 중…':''} · 의미 색인 ${sm?sm.count.toLocaleString()+'개':'없음'}${s.semantic.build.running?' (만드는 중 '+(s.semantic.build.embedded||0).toLocaleString()+')':''}`}
$('#rescan').onclick=async e=>{e.preventDefault();await fetch('/api/rescan',{method:'POST'});status()};
$('#reindex').onclick=async e=>{e.preventDefault();await fetch('/api/reindex',{method:'POST'});status()};
status();setInterval(status,4000);
</script></body></html>"""


def _reveal(path: str) -> None:
    """Show the file in the OS file manager."""
    if sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", path])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(path) or path])


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a) -> None:  # quiet
        pass

    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        q = parse_qs(u.query).get("q", [""])[0]
        try:
            if u.path == "/":
                body = PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif u.path == "/api/search":
                self._json(srv.everything_search(q, limit=200))
            elif u.path == "/api/find":
                self._json(srv.everything_find(q, limit=50, wait_for_model=False))
            elif u.path == "/api/status":
                self._json(srv.everything_status())
            elif u.path == "/api/reveal":
                _reveal(parse_qs(u.query).get("p", [""])[0])
                self._json({"ok": True})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:  # surface errors to the page instead of dropping the connection
            self._json({"error": f"{type(exc).__name__}: {exc}", "results": []}, 500)

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/api/rescan":
            eng = srv.engine()
            self._json(eng.rescan(background=True) if eng.name == "local" else {"ok": False})
        elif self.path == "/api/reindex":
            self._json(semantic.build_async(srv.engine()))
        else:
            self._json({"error": "not found"}, 404)


def serve(port: int = 0, open_browser: bool = True) -> None:
    os.environ.setdefault("FINDEX_EMBED_PROCESS", "1")  # keep the UI responsive while the model loads
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    threading.Thread(target=srv._background, daemon=True).start()
    print(f"findex {__version__}  {url}  (Ctrl+C to quit)", flush=True)
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    with contextlib.suppress(KeyboardInterrupt):  # Ctrl+C in the console window quits cleanly
        httpd.serve_forever()
