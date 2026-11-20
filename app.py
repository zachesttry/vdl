#!/usr/bin/env python3
"""Local paste-and-download UI. python app.py -> http://127.0.0.1:8000"""
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from yt_dlp import YoutubeDL

OUT = Path("downloads").resolve()
OUT.mkdir(exist_ok=True)
POOL = ThreadPoolExecutor(max_workers=3)
JOBS: dict[str, dict] = {}
app = FastAPI(title="vdl")

QUALITY = {
    "best": "bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b",
    "1080": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/b",
    "720":  "bv*[height<=720][ext=mp4]+ba[ext=m4a]/bv*[height<=720]+ba/b[height<=720]/b",
}


class Req(BaseModel):
    url: str
    quality: str = "best"


def work(job_id: str, url: str, quality: str):
    job = JOBS[job_id]

    def hook(d):
        if d["status"] == "downloading":
            job["pct"] = d.get("_percent_str", "").strip()
            job["speed"] = d.get("_speed_str", "").strip()
            job["stage"] = "downloading"
        elif d["status"] == "finished":
            job["pct"] = "100%"
            job["stage"] = "processing"

    opts = {
        "outtmpl": str(OUT / "%(title).150B [%(id)s].%(ext)s"),
        "format": QUALITY.get(quality, QUALITY["best"]),
        "merge_output_format": "mp4",
        "remuxvideo": "mp4",
        "noplaylist": True,
        "concurrent_fragment_downloads": 16,
        "retries": 10,
        "socket_timeout": 25,
        "quiet": True,
        "noprogress": True,
        "progress_hooks": [hook],
        "postprocessors": [{"key": "FFmpegMetadata", "add_metadata": True}],
    }
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        job["title"] = info.get("title") or "file"
        rd = (info.get("requested_downloads") or [{}])[0].get("filepath")
        path = Path(rd) if rd else next(OUT.glob(f"*[{info['id']}]*"))
        job["file"] = path.name
        job["stage"] = "done"
    except Exception as e:
        job["stage"] = "error"
        job["error"] = str(e).split("\n")[0][:200]


@app.post("/api/download")
def start(req: Req):
    if not re.match(r"^https?://", req.url):
        raise HTTPException(400, "need an http(s) URL")
    jid = uuid.uuid4().hex[:12]
    JOBS[jid] = {"id": jid, "url": req.url, "stage": "queued", "pct": "", "speed": ""}
    POOL.submit(work, jid, req.url, req.quality)
    return {"id": jid}


@app.get("/api/job/{jid}")
def status(jid: str):
    if jid not in JOBS:
        raise HTTPException(404)
    return JOBS[jid]


@app.get("/files/{name}")
def get_file(name: str):
    p = (OUT / name).resolve()
    if not str(p).startswith(str(OUT)) or not p.is_file():
        raise HTTPException(404)
    return FileResponse(p, media_type="video/mp4", filename=p.name)


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML


HTML = """<!doctype html><meta charset=utf-8><title>vdl</title>
<style>
 body{background:#0d0f13;color:#e6e9ef;font:15px/1.5 system-ui;max-width:820px;margin:40px auto;padding:0 16px}
 h1{font-size:20px;letter-spacing:.5px} textarea{width:100%;height:64px;background:#151922;color:#e6e9ef;
 border:1px solid #262c38;border-radius:8px;padding:10px;font:inherit;resize:vertical}
 .row{display:flex;gap:10px;margin:10px 0} select,button{background:#151922;color:#e6e9ef;border:1px solid #262c38;
 border-radius:8px;padding:10px 14px;font:inherit} button{background:#2b6cf6;border-color:#2b6cf6;cursor:pointer}
 button:hover{background:#3b7cff} .job{background:#11141b;border:1px solid #222834;border-radius:8px;padding:10px;margin:8px 0}
 .bar{height:6px;background:#1d2230;border-radius:3px;overflow:hidden;margin-top:6px}
 .fill{height:100%;width:0;background:#2b6cf6;transition:width .3s} .u{color:#8a93a6;font-size:12px;word-break:break-all}
 a{color:#5b9bff}
</style>
<h1>vdl</h1>
<textarea id=u placeholder="paste video URL"></textarea>
<div class=row><select id=q><option value=best>best</option><option value=1080>1080p</option>
<option value=720>720p</option></select><button onclick=go()>Download MP4</button></div>
<div id=list></div>
<script>
async function go(){
 const url=u.value.trim(); if(!url) return; u.value='';
 const r=await fetch('/api/download',{method:'POST',headers:{'content-type':'application/json'},
  body:JSON.stringify({url,quality:q.value})});
 const {id}=await r.json();
 const el=document.createElement('div'); el.className='job';
 el.innerHTML=`<div class=u></div><div class=st>queued</div><div class=bar><div class=fill></div></div>`;
 el.querySelector('.u').textContent=url; list.prepend(el);
 const t=setInterval(async()=>{
  const j=await (await fetch('/api/job/'+id)).json();
  el.querySelector('.fill').style.width=parseInt(j.pct)||(j.stage==='done'?100:0)+'%';
  el.querySelector('.st').textContent = j.stage==='done'
    ? `<a href="/files/${encodeURIComponent(j.file)}" download>${j.title}.mp4</a>`
    : j.stage==='error' ? j.error : `${j.stage} ${j.pct} ${j.speed}`;
  if(j.stage==='done'||j.stage==='error') clearInterval(t);
 },600);
}
</script>"""

if __name__ == "__main__":
    print("http://127.0.0.1:8000")
    uvicorn.run(app, host="0.0.0.0")
