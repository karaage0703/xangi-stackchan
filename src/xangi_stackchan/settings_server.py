import errno
import html
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .dance import PRESETS as DANCE_PRESETS, run_demo as run_dance_demo
from .settings import RuntimeState


DEFAULT_SETTINGS_PORT = 7897


# 同時複数の demo リクエストを直列化する Lock (UI 連打でデモ多重起動を防ぐ)。
_DEMO_LOCK = threading.Lock()


def _sprite_wav_hooks(sprite_animator):
    if sprite_animator is None:
        return None, None
    keeps_running = getattr(sprite_animator, "keeps_running_during_wav", None)
    if callable(keeps_running) and keeps_running():
        return None, None
    before = sprite_animator.pause if hasattr(sprite_animator, "pause") else None
    after = sprite_animator.resume if hasattr(sprite_animator, "resume") else None
    return before, after


def _set_sprite_expression(sprite_animator, face: str) -> bool:
    if sprite_animator is None:
        return False
    set_expression = getattr(sprite_animator, "set_expression", None)
    if not callable(set_expression):
        return False
    set_expression(face)
    return True


def _field(name: str, label: str, value: object, input_type: str = "text") -> str:
    escaped = html.escape(str(value or ""), quote=True)
    return (
        f"<label><span>{html.escape(label)}</span>"
        f"<input name='{html.escape(name)}' type='{input_type}' value='{escaped}'></label>"
    )


def _select(name: str, label: str, value: str, options: list[str]) -> str:
    items = []
    for option in options:
        selected = " selected" if option == value else ""
        items.append(f"<option value='{html.escape(option)}'{selected}>{html.escape(option)}</option>")
    return f"<label><span>{html.escape(label)}</span><select name='{html.escape(name)}'>{''.join(items)}</select></label>"


def render_page(state: RuntimeState) -> str:
    cfg = state.snapshot_dict()
    checked = " checked" if cfg.get("wifi") else ""
    simulator_checked = " checked" if cfg.get("simulator") else ""
    move_checked = " checked" if cfg.get("move_enabled") else ""
    puzzle_checked = " checked" if cfg.get("puzzle_light_enabled") else ""
    voice_checked = " checked" if cfg.get("voice_conversation") else ""
    head_pet_checked = " checked" if cfg.get("head_pet_reaction") else ""
    return f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>xangi-stackchan settings</title>
  <style>
    body {{ font-family: ui-sans-serif, system-ui, sans-serif; max-width: 880px; margin: 32px auto; padding: 0 16px; background: #f7f4ed; color: #222; }}
    h1 {{ font-size: 28px; margin-bottom: 8px; }}
    .hint {{ color: #5d574c; margin-bottom: 24px; }}
    .top-actions {{ display:flex; flex-wrap:wrap; gap:10px; margin: 0 0 24px; }}
    .top-actions a {{ display:inline-flex; align-items:center; min-height:34px; padding:0 12px; border:1px solid #c9c0b0; border-radius:8px; color:#233f5d; background:#ffffff; text-decoration:none; font-weight:700; }}
    form {{ display: grid; gap: 20px; }}
    fieldset {{ border: 1px solid #d8d0c1; border-radius: 14px; padding: 18px; background: #fffaf0; }}
    legend {{ font-weight: 700; padding: 0 8px; }}
    label {{ display: grid; grid-template-columns: 180px minmax(0, 1fr); gap: 12px; align-items: center; margin: 10px 0; }}
    input, select {{ font: inherit; padding: 9px 10px; border: 1px solid #c9c0b0; border-radius: 10px; background: white; }}
    .checkbox {{ display: flex; gap: 12px; align-items: center; margin: 10px 0; }}
    .checkbox input {{ width: 18px; height: 18px; }}
    button {{ width: fit-content; padding: 10px 18px; border: 0; border-radius: 999px; background: #2c5f2d; color: white; font-weight: 700; cursor: pointer; }}
    code {{ background: #eee4d2; padding: 2px 6px; border-radius: 6px; }}
  </style>
</head>
<body>
  <h1>xangi-stackchan settings</h1>
  <p class="hint">保存すると実行中デーモンに反映され、設定は <code>{html.escape(cfg["config_path"])}</code> に保存されます。</p>
  <div class="top-actions">
    <a href="/simulator">simulator</a>
    <a href="/api/config">config JSON</a>
  </div>
  <form method="post" action="/settings">
    <fieldset>
      <legend>xangi</legend>
      {_field("xangi_url", "xangi URL", cfg["xangi_url"])}
      {_field("thread_id", "thread filter", cfg["thread_id"])}
    </fieldset>
    <fieldset>
      <legend>device</legend>
      <label class="checkbox"><input name="wifi" type="checkbox"{checked}> WiFi HTTP API を使う</label>
      <label class="checkbox"><input name="simulator" type="checkbox"{simulator_checked}> ブラウザシミュレータを使う (USB/WiFi に接続しない)</label>
      {_field("host", "WiFi host", cfg["host"])}
      {_field("port", "USB port", cfg["port"])}
      {_field("baud", "baud", cfg["baud"], "number")}
      {_field("volume", "volume (0-255)", cfg["volume"], "number")}
      {_field("stackchan_retry_seconds", "retry seconds", cfg["stackchan_retry_seconds"], "number")}
    </fieldset>
    <fieldset>
      <legend>TTS</legend>
      {_select("tts", "TTS", cfg["tts"], ["piper", "voicevox", "none"])}
      {_field("piper_bin", "piper bin", cfg["piper_bin"])}
      {_field("piper_model", "piper model", cfg["piper_model"])}
      {_field("voicevox_url", "VOICEVOX URL", cfg["voicevox_url"])}
      {_field("voicevox_speaker", "VOICEVOX speaker", cfg["voicevox_speaker"], "number")}
    </fieldset>
    <fieldset>
      <legend>faces</legend>
      {_select("face_mode", "mode", cfg["face_mode"], ["avatar", "sprite"])}
      {_field("face_idle", "idle", cfg["face_idle"])}
      {_field("face_thinking", "thinking", cfg["face_thinking"])}
      {_field("face_talking", "talking", cfg["face_talking"])}
      {_field("face_error", "error", cfg["face_error"])}
      {_field("sprite_sheet", "sprite sheet", cfg["sprite_sheet"])}
      {_field("sprite_jpeg_quality", "sprite JPEG quality", cfg["sprite_jpeg_quality"], "number")}
    </fieldset>
    <fieldset>
      <legend>movement (MOVE:yaw,pitch / SAFE: yaw±100°, pitch±30°)</legend>
      <label class="checkbox"><input name="move_enabled" type="checkbox"{move_checked}> 首を動かす (MOVE 有効)</label>
      {_field("move_idle_yaw", "idle yaw (°)", cfg["move_idle_yaw"], "number")}
      {_field("move_idle_pitch", "idle pitch (°)", cfg["move_idle_pitch"], "number")}
      {_field("move_thinking_yaw", "thinking yaw (°)", cfg["move_thinking_yaw"], "number")}
      {_field("move_thinking_pitch", "thinking pitch (°)", cfg["move_thinking_pitch"], "number")}
      {_field("move_error_yaw", "error yaw (°)", cfg["move_error_yaw"], "number")}
      {_field("move_error_pitch", "error pitch (°)", cfg["move_error_pitch"], "number")}
      {_field("move_talking_sway_yaw", "talking sway yaw (±°)", cfg["move_talking_sway_yaw"], "number")}
      {_field("move_talking_sway_pitch", "talking sway pitch (±°)", cfg["move_talking_sway_pitch"], "number")}
      {_field("move_talking_sway_interval", "talking sway interval (s)", cfg["move_talking_sway_interval"], "number")}
    </fieldset>
    <fieldset>
      <legend>Puzzle Unit light (WS2812E)</legend>
      <p class="hint">CoreS3 Grove PORT.B の Puzzle Unit を、xangi の状態や demo 発話に合わせて点灯する。ファームの <code>STATUS</code> が <code>puzzle:true</code> の時だけ送信。</p>
      <label class="checkbox"><input name="puzzle_light_enabled" type="checkbox"{puzzle_checked}> Puzzle Unit を状態表示に使う</label>
      {_field("puzzle_idle", "idle pattern", cfg["puzzle_idle"])}
      {_field("puzzle_thinking", "thinking pattern", cfg["puzzle_thinking"])}
      {_field("puzzle_talking", "talking pattern", cfg["puzzle_talking"])}
      {_field("puzzle_error", "error pattern", cfg["puzzle_error"])}
    </fieldset>
    <fieldset>
      <legend>voice conversation (M5Stackchan K151 のアタマセンサ + 内蔵 PDM マイク経由)</legend>
      <p class="hint">tap で録音開始 → 無音 1.5 秒で自動停止 → faster-whisper STT → xangi <code>POST /api/chat</code> 投入。応答 TTS は既存経路で発話される。詳細 <code>docs/usage.md</code> の「音声対話モード」。</p>
      <label class="checkbox"><input name="voice_conversation" type="checkbox"{voice_checked}> 音声対話モードを有効化</label>
      {_field("voice_app_session_id", "appSessionId (空ならアプリ起動時に専用 web session 自動作成)", cfg["voice_app_session_id"])}
      {_field("voice_silence_dbfs", "silence threshold (dBFS、静か:-50 / 騒:-30)", cfg["voice_silence_dbfs"], "number")}
      {_field("voice_silence_seconds", "silence seconds (自動停止までの無音秒数)", cfg["voice_silence_seconds"], "number")}
      {_field("voice_max_seconds", "max record seconds (強制停止)", cfg["voice_max_seconds"], "number")}
      <div style="margin-top:12px;">
        <strong>直近の発話履歴 (上から新しい順、5 秒ごと自動更新):</strong>
        <pre id="vc-history" style="background:#eee4d2; padding:8px; border-radius:8px; max-height:240px; overflow-y:auto; white-space:pre-wrap; margin-top:6px;">(まだ録音なし)</pre>
      </div>
    </fieldset>
    <fieldset>
      <legend>なでなで反応 (アタマを触った瞬間にランダムなセリフを喋る / デモ向け)</legend>
      <p class="hint">話しかけ不要。アタマ (head_touch) を press / swipe すると即セリフ。voice conversation が有効な時はそちらが優先 (同じ press を消費するため反応しない)。</p>
      <label class="checkbox"><input name="head_pet_reaction" type="checkbox"{head_pet_checked}> なでなで反応モードを有効化</label>
      {_field("head_pet_phrases", "セリフ候補 (カンマ区切り、空ならデフォルト)", ",".join(cfg.get("head_pet_phrases") or []))}
      {_field("head_pet_cooldown_seconds", "クールダウン秒数 (発話完了後、次の反応まで)", cfg["head_pet_cooldown_seconds"], "number")}
    </fieldset>
    <button type="submit">保存して反映</button>
  </form>
  <form method="post" action="/demo" style="margin-top: 32px;">
    <fieldset>
      <legend>dance demo (現在の TTS + デバイスに直接喋らせて踊らせる)</legend>
      <label><span>text</span><input name="text" type="text" placeholder="踊るぞ、よろしくね！" required></label>
      {_select("preset", "preset", "happy", sorted(DANCE_PRESETS.keys()))}
      {_field("bpm", "BPM override", "", "number")}
    </fieldset>
    <button type="submit">ダンスデモを実行</button>
  </form>
  <fieldset style="margin-top: 32px;">
    <legend>camera (Phase 1A: snapshot + monitor)</legend>
    <p class="hint">CoreS3 内蔵 GC0308 カメラから JPEG 1 枚取得して表示する。撮影中はデバイスのアバターに「capturing」が出る。</p>
    <div style="display:flex; gap:16px; align-items:flex-start;">
      <img id="camera-preview" src="/api/camera/snapshot.jpg" alt="snapshot"
           style="max-width:320px; border:1px solid #c9c0b0; border-radius:10px; background:#000;"
           onerror="this.alt='no snapshot yet';">
      <div>
        <button type="button" id="camera-shutter" style="margin-bottom:8px;">スナップショット</button>
        <pre id="camera-status" style="background:#eee4d2; padding:8px; border-radius:8px; max-width:380px; white-space:pre-wrap;">(no capture yet)</pre>
      </div>
    </div>
  </fieldset>
  <script>
    (function() {{
      // 発話履歴 polling (voice_conversation 有効時のみ意味あり)。
      const vcHistory = document.getElementById('vc-history');
      if (vcHistory) {{
        const fmt = (ts) => {{
          if (!ts) return '?';
          const d = new Date(ts * 1000);
          const pad = (n) => String(n).padStart(2, '0');
          return `${{pad(d.getHours())}}:${{pad(d.getMinutes())}}:${{pad(d.getSeconds())}}`;
        }};
        const render = (data) => {{
          if (!data.history || data.history.length === 0) {{
            vcHistory.textContent = '(まだ録音なし)';
            return;
          }}
          const lines = data.history.slice().reverse().map((e) => {{
            const dur = e.duration_seconds != null ? `${{e.duration_seconds.toFixed(2)}}s` : '-';
            const ela = e.elapsed_seconds != null ? `${{e.elapsed_seconds.toFixed(2)}}s` : '-';
            const sent = e.sent_status != null ? `→${{e.sent_status}}` : '';
            return `[${{fmt(e.ts)}}] rec=${{dur}} stt=${{ela}} ${{sent}}\n  "${{e.text || '(empty)'}}"`;
          }});
          vcHistory.textContent = lines.join('\\n');
        }};
        const fetchHistory = async () => {{
          try {{
            const r = await fetch('/api/voice/history');
            if (r.ok) render(await r.json());
          }} catch (e) {{}}
        }};
        fetchHistory();
        setInterval(fetchHistory, 5000);
      }}

      const shutter = document.getElementById('camera-shutter');
      const preview = document.getElementById('camera-preview');
      const status = document.getElementById('camera-status');
      if (!shutter) return;
      shutter.addEventListener('click', async () => {{
        shutter.disabled = true;
        const t0 = Date.now();
        try {{
          const resp = await fetch('/api/camera/capture', {{ method: 'POST' }});
          const meta = await resp.json();
          status.textContent = JSON.stringify(meta, null, 2) + '\\n(client elapsed: ' + (Date.now() - t0) + 'ms)';
          if (meta.status === 'ok') {{
            preview.src = '/api/camera/snapshot.jpg?_=' + Date.now();
          }}
        }} catch (e) {{
          status.textContent = 'fetch error: ' + e;
        }} finally {{
          shutter.disabled = false;
        }}
      }});
    }})();
  </script>
</body>
</html>"""


def render_simulator_page() -> str:
    return """<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>xangi-stackchan simulator</title>
  <style>
    :root { color-scheme: light; --ink:#20252b; --muted:#62707d; --line:#d6dde3; --panel:#ffffff; --bg:#eef3f6; --accent:#2b6f9f; --warm:#f0b44d; --ok:#38a169; --err:#d45b5b; }
    * { box-sizing: border-box; }
    body { margin:0; font-family: ui-sans-serif, system-ui, sans-serif; color:var(--ink); background:var(--bg); }
    main { width:min(1180px, 100%); margin:0 auto; padding:24px; display:grid; grid-template-columns:minmax(320px, 520px) minmax(280px, 1fr); gap:20px; }
    header { grid-column:1 / -1; display:flex; align-items:flex-end; justify-content:space-between; gap:16px; border-bottom:1px solid var(--line); padding-bottom:14px; }
    h1 { margin:0; font-size:24px; letter-spacing:0; }
    a { color:var(--accent); font-weight:700; text-decoration:none; }
    section { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:16px; }
    .stage { min-height:580px; display:grid; place-items:center; background:linear-gradient(#f9fbfc, #e5edf2); position:relative; overflow:hidden; }
    .robot-wrap { width:min(410px, 86vw); aspect-ratio: 1 / 1.08; position:relative; display:grid; place-items:center; perspective:800px; }
    .robot { width:74%; height:82%; position:relative; transform:rotateY(calc(var(--yaw, 0) * 0.45deg)) rotateX(calc(var(--pitch, 0) * -0.55deg)); transform-style:preserve-3d; transition:transform 260ms ease; }
    .antenna { position:absolute; left:50%; top:0; width:5px; height:54px; background:#30363b; transform:translateX(-50%); border-radius:5px; }
    .antenna::before { content:""; position:absolute; left:50%; top:-14px; width:24px; height:24px; transform:translateX(-50%); border-radius:50%; background:var(--warm); border:4px solid #30363b; }
    .head { position:absolute; left:8%; top:12%; width:84%; height:58%; background:#f7f4e9; border:6px solid #30363b; border-radius:28px; box-shadow:0 16px 0 rgba(32,37,43,.08); }
    .screen { position:absolute; left:12%; top:14%; width:76%; height:62%; background:#222930; border-radius:18px; overflow:hidden; border:4px solid #30363b; }
    .eye { position:absolute; top:34%; width:15%; height:20%; background:#fbfbf7; border-radius:50%; transition:all 180ms ease; }
    .eye.left { left:27%; }
    .eye.right { right:27%; }
    .mouth { position:absolute; left:38%; top:62%; width:24%; height:12%; border-bottom:5px solid #fbfbf7; border-radius:0 0 40px 40px; transition:all 180ms ease; }
    .face-happy .eye { height:10%; top:38%; background:transparent; border-top:5px solid #fbfbf7; border-radius:50% 50% 0 0; }
    .face-happy .mouth { left:34%; top:58%; width:32%; height:18%; border-bottom-width:6px; }
    .face-sad .eye { height:12%; top:39%; }
    .face-sad .mouth { top:70%; transform:rotate(180deg); }
    .face-doubt .eye.left { transform:translateY(5px); }
    .face-doubt .eye.right { transform:translateY(-5px); }
    .face-angry .eye { border-radius:40% 40% 50% 50%; transform:skewY(-12deg); }
    .face-sprite .screen { background:linear-gradient(135deg,#3b4450,#161b20); }
    .face-sprite .screen::after { content:"SPRITE"; position:absolute; inset:auto 0 12px; text-align:center; color:#f4d06f; font-weight:800; font-size:18px; }
    .body { position:absolute; left:20%; bottom:0; width:60%; height:32%; background:#dfe7ec; border:6px solid #30363b; border-radius:24px 24px 18px 18px; }
    .neck { position:absolute; left:42%; top:66%; width:16%; height:12%; background:#30363b; border-radius:12px; }
    .lightbar { position:absolute; left:22%; right:22%; top:18%; height:14px; border-radius:20px; background:#7f8b94; box-shadow:0 0 18px rgba(0,0,0,.12); }
    .light-thinking { background:#f0b44d; box-shadow:0 0 22px rgba(240,180,77,.7); }
    .light-talking { background:#38a169; box-shadow:0 0 22px rgba(56,161,105,.7); }
    .light-error, .light-red { background:var(--err); box-shadow:0 0 22px rgba(212,91,91,.7); }
    .light-blue { background:#438bc4; box-shadow:0 0 22px rgba(67,139,196,.7); }
    .shadow { position:absolute; bottom:5%; width:58%; height:8%; border-radius:50%; background:rgba(32,37,43,.14); filter:blur(5px); }
    .metrics { display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:10px; }
    .metric { border:1px solid var(--line); border-radius:8px; padding:10px; background:#fbfcfd; min-height:64px; }
    .metric span { display:block; color:var(--muted); font-size:12px; }
    .metric strong { display:block; margin-top:4px; font-size:18px; overflow-wrap:anywhere; }
    .controls { display:grid; gap:12px; }
    .row { display:flex; flex-wrap:wrap; gap:8px; }
    button, input, select { font:inherit; min-height:36px; border-radius:8px; border:1px solid var(--line); background:#fff; color:var(--ink); }
    button { padding:0 12px; font-weight:700; cursor:pointer; }
    button.primary { background:var(--accent); color:#fff; border-color:var(--accent); }
    input { padding:0 10px; min-width:180px; flex:1; }
    pre { margin:0; background:#1f252b; color:#eef3f6; padding:12px; border-radius:8px; max-height:260px; overflow:auto; white-space:pre-wrap; font-size:12px; }
    @media (max-width: 820px) { main { grid-template-columns:1fr; padding:14px; } .stage { min-height:430px; } header { align-items:flex-start; flex-direction:column; } }
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>xangi-stackchan simulator</h1>
        <div id="connection" style="color:var(--muted); margin-top:6px;">connecting</div>
      </div>
      <a href="/">settings</a>
    </header>
    <section class="stage">
      <div class="robot-wrap">
        <div class="shadow"></div>
        <div id="robot" class="robot face-neutral" style="--yaw:0; --pitch:5;">
          <div class="antenna"></div>
          <div class="head">
            <div id="screen" class="screen">
              <div class="eye left"></div>
              <div class="eye right"></div>
              <div class="mouth"></div>
            </div>
          </div>
          <div class="neck"></div>
          <div class="body">
            <div id="lightbar" class="lightbar"></div>
          </div>
        </div>
      </div>
    </section>
    <section class="controls">
      <div class="metrics">
        <div class="metric"><span>state</span><strong id="m-state">-</strong></div>
        <div class="metric"><span>face</span><strong id="m-face">-</strong></div>
        <div class="metric"><span>move</span><strong id="m-move">-</strong></div>
        <div class="metric"><span>lights</span><strong id="m-lights">-</strong></div>
        <div class="metric"><span>audio</span><strong id="m-audio">off</strong></div>
      </div>
      <div class="row">
        <button data-cmd="FACE:neutral">neutral</button>
        <button data-cmd="FACE:happy">happy</button>
        <button data-cmd="FACE:doubt">doubt</button>
        <button data-cmd="FACE:sad">sad</button>
      </div>
      <div class="row">
        <button data-cmd="MOVE:0,5">home</button>
        <button data-cmd="MOVE:-20,8">left</button>
        <button data-cmd="MOVE:20,8">right</button>
        <button data-cmd="PUZZLE:thinking">thinking light</button>
        <button data-cmd="PUZZLE:talking">talking light</button>
        <button data-cmd="PUZZLE:off">light off</button>
      </div>
      <div class="row">
        <button id="enable-audio" type="button">enable audio</button>
      </div>
      <form id="command-form" class="row">
        <input id="command-input" value="STATUS" autocomplete="off">
        <button class="primary" type="submit">send</button>
      </form>
      <pre id="log">waiting</pre>
    </section>
  </main>
  <script>
    const robot = document.getElementById('robot');
    const lightbar = document.getElementById('lightbar');
    const log = document.getElementById('log');
    const conn = document.getElementById('connection');
    const audioMetric = document.getElementById('m-audio');
    let audioEnabled = false;
    let lastAudioId = 0;
    let latestState = null;
    let currentAudio = null;
    let audioStatus = 'off';
    const faceClass = (face) => {
      const f = String(face || 'neutral').toLowerCase();
      if (f.includes('happy')) return 'face-happy';
      if (f.includes('sad')) return 'face-sad';
      if (f.includes('doubt')) return 'face-doubt';
      if (f.includes('angry')) return 'face-angry';
      if (f.includes('sprite')) return 'face-sprite';
      return 'face-neutral';
    };
    const lightClass = (pattern) => {
      const p = String(pattern || 'off').toLowerCase();
      if (p.includes('thinking') || p.includes('rainbow')) return 'light-thinking';
      if (p.includes('talking') || p.includes('green')) return 'light-talking';
      if (p.includes('error') || p.includes('red')) return 'light-error';
      if (p.includes('blue')) return 'light-blue';
      return '';
    };
    const render = (state) => {
      latestState = state;
      robot.className = 'robot ' + faceClass(state.face);
      robot.style.setProperty('--yaw', Number(state.yaw || 0));
      robot.style.setProperty('--pitch', Number(state.pitch || 0));
      lightbar.className = 'lightbar ' + lightClass(state.puzzle || state.stack_led);
      document.getElementById('m-state').textContent = state.state || '-';
      document.getElementById('m-face').textContent = state.face || '-';
      document.getElementById('m-move').textContent = `${Number(state.yaw || 0).toFixed(1)}, ${Number(state.pitch || 0).toFixed(1)}`;
      document.getElementById('m-lights').textContent = `${state.puzzle || 'off'} / ${state.stack_led || 'off'}`;
      if (audioMetric) audioMetric.textContent = audioEnabled ? audioStatus : 'off';
      const history = (state.history || []).slice(-12).reverse().map((e) => `${new Date(e.ts * 1000).toLocaleTimeString()}  ${e.command}`);
      log.textContent = history.join('\\n') || 'no commands yet';
    };
    const playAudioIfNeeded = async (state, force = false) => {
      const id = Number(state.wav_id || 0);
      if (!audioEnabled || !id || (!force && id === lastAudioId) || !state.has_audio) return;
      try {
        const audio = new Audio(`/api/simulator/audio/latest.wav?wid=${id}`);
        audio.volume = Math.max(0, Math.min(1, Number(state.volume || 255) / 255));
        currentAudio = audio;
        await audio.play();
        lastAudioId = id;
        audioStatus = `playing #${id}`;
        if (audioMetric) audioMetric.textContent = audioStatus;
        audio.addEventListener('ended', () => {
          if (lastAudioId === id) {
            audioStatus = `played #${id}`;
            if (audioMetric) audioMetric.textContent = audioStatus;
          }
        }, {once: true});
      } catch (e) {
        audioEnabled = false;
        audioStatus = `blocked #${id}`;
        if (audioMetric) audioMetric.textContent = audioStatus;
      }
    };
    const poll = async () => {
      try {
        const r = await fetch('/api/simulator/state');
        const data = await r.json();
        if (!r.ok) throw new Error(data.error || r.statusText);
        conn.textContent = data.simulator ? 'simulator backend active' : 'runtime is not simulator';
        render(data);
        await playAudioIfNeeded(data);
      } catch (e) {
        conn.textContent = 'offline: ' + e.message;
      }
    };
    const sendCommand = async (command) => {
      const r = await fetch('/api/simulator/command', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({command})
      });
      const data = await r.json();
      if (!r.ok) log.textContent = JSON.stringify(data, null, 2);
      await poll();
    };
    document.querySelectorAll('button[data-cmd]').forEach((button) => {
      button.addEventListener('click', () => sendCommand(button.dataset.cmd));
    });
    document.getElementById('command-form').addEventListener('submit', (event) => {
      event.preventDefault();
      sendCommand(document.getElementById('command-input').value.trim());
    });
    document.getElementById('enable-audio').addEventListener('click', async () => {
      audioEnabled = true;
      audioStatus = latestState && latestState.has_audio ? `ready #${latestState.wav_id || 0}` : 'ready';
      if (audioMetric) audioMetric.textContent = audioStatus;
      try {
        const ctx = new (window.AudioContext || window.webkitAudioContext)();
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        gain.gain.value = 0.0001;
        osc.connect(gain).connect(ctx.destination);
        osc.start();
        osc.stop(ctx.currentTime + 0.03);
      } catch (e) {}
      if (latestState) await playAudioIfNeeded(latestState, true);
      await poll();
    });
    poll();
    setInterval(poll, 700);
  </script>
</body>
</html>"""


def _flatten_form(raw: bytes) -> dict[str, object]:
    parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
    data = {key: values[-1] for key, values in parsed.items()}
    data["wifi"] = "wifi" in parsed
    data["simulator"] = "simulator" in parsed
    data["move_enabled"] = "move_enabled" in parsed
    data["puzzle_light_enabled"] = "puzzle_light_enabled" in parsed
    data["voice_conversation"] = "voice_conversation" in parsed
    data["head_pet_reaction"] = "head_pet_reaction" in parsed
    return data


def _execute_capture(state: RuntimeState) -> dict[str, object]:
    """Phase 1A: backend.capture() を叩いて結果を RuntimeState にキャッシュ。

    Returns: capture() の結果 dict (image_jpeg を除いた snapshot メタデータ)。
             成功時は state.set_last_capture() で内部キャッシュも更新する。
    """
    backend, _ = state.get_runtime()
    if backend is None:
        return {"status": "error", "error": "runtime not ready"}
    if not hasattr(backend, "capture"):
        return {"status": "error", "error": "backend does not support capture"}
    try:
        result = backend.capture()
    except Exception as exc:
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}

    if result.get("status") == "ok" and isinstance(result.get("image_jpeg"), (bytes, bytearray)):
        # キャッシュ用に dict をコピー (image_jpeg は重いのでそのまま渡す = 同一参照)
        state.set_last_capture(dict(result))
    return result


def _execute_demo(state: RuntimeState, payload: dict[str, object]) -> dict[str, object]:
    """Validate the payload, acquire shared backend/piper, and run a dance demo.

    Returns the dict from `dance.run_demo` on success, or an error dict.
    Caller decides whether to surface the result synchronously (API) or
    discard it (form POST → 303 redirect after fire-and-forget).
    """
    text = str(payload.get("text") or "").strip()
    preset = str(payload.get("preset") or "happy").strip()
    bpm_raw = str(payload.get("bpm") or "").strip()
    try:
        bpm = float(bpm_raw) if bpm_raw else None
    except ValueError:
        return {"status": "error", "error": f"invalid bpm: {bpm_raw}"}
    if not text:
        return {"status": "error", "error": "text required"}
    if preset not in DANCE_PRESETS:
        return {"status": "error", "error": f"unknown preset: {preset}"}

    backend, piper = state.get_runtime()
    if backend is None:
        return {"status": "error", "error": "runtime not ready"}
    config, _ = state.snapshot()
    if config.tts == "none":
        return {"status": "error", "error": "TTS is disabled (config.tts=none)"}

    if not _DEMO_LOCK.acquire(blocking=False):
        return {"status": "error", "error": "another demo is running"}
    sprite_animator = state.get_sprite_animator()
    before_wav_send, after_wav_send = _sprite_wav_hooks(sprite_animator)
    should_restore_sprite_expression = False
    try:
        if config.face_mode == "sprite":
            should_restore_sprite_expression = _set_sprite_expression(
                sprite_animator, config.face_talking
            )
        return run_dance_demo(
            backend,
            piper,
            config,
            text,
            preset,
            bpm_override=bpm,
            before_wav_send=before_wav_send,
            after_wav_send=after_wav_send,
        )
    except Exception as exc:
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if should_restore_sprite_expression:
            _set_sprite_expression(sprite_animator, config.face_idle)
        _DEMO_LOCK.release()


def _get_simulator_backend(state: RuntimeState):
    backend, _ = state.get_runtime()
    if backend is None or not hasattr(backend, "snapshot"):
        return None
    snapshot = backend.snapshot()
    if not isinstance(snapshot, dict) or not snapshot.get("simulator"):
        return None
    return backend


def make_handler(state: RuntimeState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def _send(self, status: int, body: bytes, content_type: str):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/api/config":
                body = json.dumps(state.snapshot_dict(), ensure_ascii=False).encode()
                self._send(200, body, "application/json; charset=utf-8")
                return
            if path in {"/", "/settings"}:
                self._send(200, render_page(state).encode(), "text/html; charset=utf-8")
                return
            if path == "/simulator":
                self._send(200, render_simulator_page().encode(), "text/html; charset=utf-8")
                return
            if path == "/api/simulator/state":
                backend = _get_simulator_backend(state)
                if backend is None:
                    body = json.dumps(
                        {"status": "error", "error": "simulator backend is not active"},
                        ensure_ascii=False,
                    ).encode()
                    self._send(503, body, "application/json; charset=utf-8")
                    return
                body = json.dumps(backend.snapshot(), ensure_ascii=False).encode()
                self._send(200, body, "application/json; charset=utf-8")
                return
            if path == "/api/simulator/audio/latest.wav":
                backend = _get_simulator_backend(state)
                if backend is None or not hasattr(backend, "latest_wav"):
                    body = json.dumps(
                        {"status": "error", "error": "simulator backend is not active"},
                        ensure_ascii=False,
                    ).encode()
                    self._send(503, body, "application/json; charset=utf-8")
                    return
                wav = backend.latest_wav()
                if not isinstance(wav, (bytes, bytearray)) or not wav:
                    self._send(404, b"no simulator audio", "text/plain; charset=utf-8")
                    return
                self._send(200, bytes(wav), "audio/wav")
                return
            # /api/camera/snapshot.jpg は ?force=1 でデバイスから新規取得、
            # 省略時はキャッシュから返す (キャッシュ無しなら自動でデバイス取得)。
            if path == "/api/camera/snapshot.jpg":
                force = "force=1" in self.path
                cached = state.get_last_capture() if not force else None
                if cached is None:
                    result = _execute_capture(state)
                    if result.get("status") != "ok" or not isinstance(
                        result.get("image_jpeg"), (bytes, bytearray)
                    ):
                        body = json.dumps(result, ensure_ascii=False).encode()
                        self._send(
                            503 if result.get("error") in {"runtime not ready", "camera not ready"} else 502,
                            body,
                            "application/json; charset=utf-8",
                        )
                        return
                    cached = result
                self._send(200, bytes(cached["image_jpeg"]), "image/jpeg")
                return
            if path == "/api/voice/history":
                # voice_conversation.history (直近 N 件の STT + POST 結果) を返す。
                # 設定 UI の polling 表示 + デバッグ用。voice_conversation 無効起動時
                # や VoiceConversation 未生成 (WiFi backend 等) は status=ok + 空配列。
                vc = state.get_voice_conversation()
                history = []
                if vc is not None:
                    history = list(getattr(vc, "history", []))
                payload = {"status": "ok", "history": history, "count": len(history)}
                self._send(
                    200,
                    json.dumps(payload, ensure_ascii=False).encode(),
                    "application/json; charset=utf-8",
                )
                return
            if path == "/api/camera/status":
                cached = state.get_last_capture()
                if cached is None:
                    payload = {"status": "ok", "last_capture": None}
                else:
                    import time as _t
                    meta = {k: v for k, v in cached.items() if k != "image_jpeg"}
                    if isinstance(cached.get("captured_at"), (int, float)):
                        meta["age_ms"] = int((_t.time() - cached["captured_at"]) * 1000)
                    payload = {"status": "ok", "last_capture": meta}
                self._send(200, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                return
            self._send(404, b"not found", "text/plain; charset=utf-8")

        def do_POST(self):
            path = urlsplit(self.path).path
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            if path == "/api/config":
                payload = json.loads(raw.decode("utf-8") or "{}")
                try:
                    updated = state.update(payload)
                except ValueError as exc:
                    self._send(400, str(exc).encode(), "text/plain; charset=utf-8")
                    return
                body = json.dumps(updated, ensure_ascii=False).encode()
                self._send(200, body, "application/json; charset=utf-8")
                return
            if path == "/settings":
                try:
                    state.update(_flatten_form(raw))
                except ValueError as exc:
                    self._send(400, str(exc).encode(), "text/plain; charset=utf-8")
                    return
                self.send_response(303)
                self.send_header("Location", "/")
                self.end_headers()
                return
            if path == "/api/camera/capture":
                # Phase 1A: 同期 capture。成功時は image_jpeg を除いたメタを返す。
                # (画像は別途 GET /api/camera/snapshot.jpg で取りに来る前提)
                result = _execute_capture(state)
                meta = {k: v for k, v in result.items() if k != "image_jpeg"}
                if "size" in meta:
                    meta["has_image"] = True
                status = 200 if result.get("status") == "ok" else (
                    503 if result.get("error") in {"runtime not ready", "camera not ready"} else 502
                )
                body = json.dumps(meta, ensure_ascii=False).encode()
                self._send(status, body, "application/json; charset=utf-8")
                return
            if path == "/api/demo":
                # CLI / 自動化向け: 同期実行して結果 JSON を返す。
                payload = json.loads(raw.decode("utf-8") or "{}")
                result = _execute_demo(state, payload)
                status = 200 if result.get("status") == "ok" else (
                    503 if result.get("error") == "runtime not ready" else 400
                )
                body = json.dumps(result, ensure_ascii=False).encode()
                self._send(status, body, "application/json; charset=utf-8")
                return
            if path == "/api/simulator/command":
                backend = _get_simulator_backend(state)
                if backend is None:
                    body = json.dumps(
                        {"status": "error", "error": "simulator backend is not active"},
                        ensure_ascii=False,
                    ).encode()
                    self._send(503, body, "application/json; charset=utf-8")
                    return
                payload = json.loads(raw.decode("utf-8") or "{}")
                command = str(payload.get("command") or "").strip()
                if not command:
                    self._send(400, b'{"status":"error","error":"command required"}', "application/json; charset=utf-8")
                    return
                try:
                    result = backend.send_command(command)
                except Exception as exc:
                    result = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
                status = 200 if result.get("status") != "error" else 400
                body = json.dumps(result, ensure_ascii=False).encode()
                self._send(status, body, "application/json; charset=utf-8")
                return
            if path == "/demo":
                # ブラウザ UI 向け: 別スレッドで fire-and-forget、即 303 で画面を戻す。
                parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
                payload: dict[str, object] = {
                    key: values[-1] for key, values in parsed.items()
                }
                threading.Thread(
                    target=_execute_demo, args=(state, payload), daemon=True
                ).start()
                self.send_response(303)
                self.send_header("Location", "/")
                self.end_headers()
                return
            self._send(404, b"not found", "text/plain; charset=utf-8")

    return Handler


def start_settings_server(
    state: RuntimeState,
    bind: str,
    port: int,
    autoshift_tries: int = 1,
) -> tuple[ThreadingHTTPServer, int]:
    """Start the settings UI HTTP server, auto-shifting the port on conflict.

    ``autoshift_tries=1`` keeps the previous fail-fast behaviour and binds to
    ``port`` exactly. With ``autoshift_tries>1`` the server tries ``port``,
    ``port+1`` ... up to ``autoshift_tries`` candidates and binds the first
    free one.

    Returns ``(server, bound_port)``. The caller is expected to log the
    bound port so concurrent instances on the same host can be told apart.
    """
    tries = max(1, autoshift_tries)
    last_error: OSError | None = None
    for offset in range(tries):
        candidate = port + offset
        try:
            server = ThreadingHTTPServer((bind, candidate), make_handler(state))
        except OSError as exc:
            if exc.errno in {errno.EADDRINUSE, errno.EACCES}:
                last_error = exc
                continue
            raise
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, candidate
    raise OSError(
        f"settings UI port {port}..{port + tries - 1} all busy"
    ) from last_error
