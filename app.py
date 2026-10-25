"""Paradise Tunes VU v1: experimental, read-only live audio spectrum feed."""
import os
import time
import threading
import subprocess
import math
from collections import deque

import numpy as np
from flask import Flask, jsonify, Response

STREAM_URL = os.getenv('PARADISE_STREAM_URL', 'https://s2.free-shoutcast.com/stream/18102')
RATE = 11025
FRAME = 1024
BANDS = 24
MIN_HZ = 45.0
MAX_HZ = 5000.0
app = Flask(__name__)
_lock = threading.Lock()
_state = {'bands': [0] * BANDS, 'updated': 0.0, 'status': 'starting', 'error': None}
_worker_started = False


def frequency_bands(audio):
    """Convert real mono PCM samples into 24 log-spaced energy bands."""
    if len(audio) != FRAME:
        return [0] * BANDS
    windowed = audio * np.hanning(FRAME)
    spectrum = np.abs(np.fft.rfft(windowed))
    freqs = np.fft.rfftfreq(FRAME, d=1.0 / RATE)
    edges = np.geomspace(MIN_HZ, MAX_HZ, BANDS + 1)
    vals = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        use = (freqs >= lo) & (freqs < hi)
        magnitude = float(np.sqrt(np.mean(spectrum[use] ** 2))) if np.any(use) else 0.0
        # Rough perceptual scaling, adjusted after listening and measured data.
        db = 20.0 * math.log10(max(magnitude, 1e-9))
        vals.append(int(round(max(0, min(100, (db + 22) * 1.6)))))
    return vals


def consume_stream():
    delays = (2, 4, 8, 15, 30)
    attempt = 0
    while True:
        proc = None
        try:
            with _lock:
                _state['status'] = 'connecting'
                _state['error'] = None
            # Audio ONLY. No microphone or Toshiba connection required.
            proc = subprocess.Popen([
                'ffmpeg', '-hide_banner', '-nostdin', '-loglevel', 'error',
                '-rw_timeout', '12000000', '-i', STREAM_URL,
                '-vn', '-ac', '1', '-ar', str(RATE), '-f', 'f32le', 'pipe:1'
            ], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
            smooth = np.zeros(BANDS, dtype=np.float64)
            attempt = 0
            bytes_per_frame = FRAME * 4
            while True:
                chunks = bytearray()
                while len(chunks) < bytes_per_frame:
                    part = proc.stdout.read(bytes_per_frame - len(chunks))
                    if not part:
                        raise ConnectionError('Audio stream ended or decoder stopped')
                    chunks.extend(part)
                audio = np.frombuffer(chunks, dtype='<f4').astype(np.float64)
                raw = np.asarray(frequency_bands(audio), dtype=np.float64)
                smooth = 0.52 * raw + 0.48 * smooth
                with _lock:
                    _state['bands'] = [int(round(x)) for x in smooth]
                    _state['updated'] = time.time()
                    _state['status'] = 'live'
                    _state['error'] = None
        except Exception as exc:
            with _lock:
                _state['status'] = 'reconnecting'
                _state['error'] = str(exc)[:160]
                _state['bands'] = [0] * BANDS
                _state['updated'] = 0
        finally:
            if proc:
                try:
                    proc.kill()
                    proc.wait(timeout=3)
                except Exception:
                    pass
        time.sleep(delays[min(attempt, len(delays) - 1)])
        attempt += 1


def start_worker():
    global _worker_started
    with _lock:
        if _worker_started:
            return
        _worker_started = True
    threading.Thread(target=consume_stream, daemon=True, name='spectrum-reader').start()


@app.get('/health')
def health():
    return jsonify({'service': 'paradise-tunes-vu-v1', 'ok': True})


@app.get('/api/vu')
def vu():
    with _lock:
        data = dict(_state)
        data['bands'] = list(_state['bands'])
    age = time.time() - data['updated'] if data['updated'] else None
    valid = age is not None and age < 3
    return jsonify({
        'version': 1, 'source': 'real-audio-fft', 'band_count': BANDS,
        'live': valid, 'status': data['status'] if valid else ('waiting' if data['status'] == 'live' else data['status']),
        'age_seconds': round(age, 2) if age is not None else None,
        'bands': data['bands'] if valid else [0] * BANDS,
        'error': data['error'] if not valid else None,
    }), 200, {'Cache-Control': 'no-store', 'Access-Control-Allow-Origin': '*'}


@app.get('/')
def home():
    return Response('''<!doctype html><html lang="en"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Paradise Tunes VU - Isolated Test</title><style>
body{background:#071522;color:#f1f5f9;font:16px system-ui;margin:24px auto;max-width:850px;padding:0 16px}
#bars{height:220px;display:flex;align-items:end;gap:4px;background:#13273a;padding:12px;border-radius:12px}
.bar{flex:1;min-width:0;height:0;background:linear-gradient(#ec594b,#f9c64d,#20c76a);border-radius:3px 3px 0 0;transition:height .1s linear}
pre{white-space:pre-wrap}small{color:#a3b4c5}</style>
<h1>Paradise Tunes - 24-band REAL VU test</h1><p>This is a <b>separate test</b>. Bars are driven ONLY by measured FFT data from the station stream. No fallback animation.</p>
<div id="bars"></div><p id="state">Connecting…</p><small>If the host sleeps, allow it to wake. Inactive or unavailable audio shows flat bars.</small>
<pre id="diagnostics"></pre><script>
const wrap=document.querySelector('#bars'), bars=Array.from({length:24},()=>{const b=document.createElement('div');b.className='bar';wrap.appendChild(b);return b;});
let busy=false;async function tick(){if(busy)return;busy=true;try{const r=await fetch('/api/vu',{cache:'no-store'});const d=await r.json();bars.forEach((b,i)=>b.style.height=(d.live?d.bands[i]:0)+'%');document.querySelector('#state').textContent=d.live?'REAL spectrum data received':('Waiting: '+d.status);document.querySelector('#diagnostics').textContent=JSON.stringify({live:d.live,age_seconds:d.age_seconds,peak:Math.max(...d.bands),status:d.status,error:d.error},null,2);}catch(e){bars.forEach(b=>b.style.height='0%');document.querySelector('#state').textContent='Connection unavailable';document.querySelector('#diagnostics').textContent=String(e)}finally{busy=false}}
tick();setInterval(tick,150);
</script></html>''', mimetype='text/html')

# Gunicorn uses a single worker intentionally to avoid duplicate upstream connections.
start_worker()
