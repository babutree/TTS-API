# -*- coding: utf-8 -*-
"""前端契约测试：直接执行 HTML 内联脚本，避免 JS 逻辑漂移。"""
import pathlib
import re
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]

COMMON_JS_STUB = r"""
const failures = [];
function assertOk(value, msg) { if (!value) failures.push(msg); }
function equal(actual, expected, msg) {
  if (actual !== expected) failures.push(`${msg}: ${JSON.stringify(actual)} !== ${JSON.stringify(expected)}`);
}
function deepEqual(actual, expected, msg) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) failures.push(`${msg}: ${a} !== ${e}`);
}
function finish() { if (failures.length) throw new Error(failures.join('\n')); }

class ClassList {
  constructor() { this.items = new Set(); }
  add(...names) { names.forEach(n => this.items.add(n)); }
  remove(...names) { names.forEach(n => this.items.delete(n)); }
  contains(name) { return this.items.has(name); }
  toggle(name, force) {
    const on = force === undefined ? !this.items.has(name) : Boolean(force);
    if (on) this.items.add(name); else this.items.delete(name);
    return on;
  }
}

class Element {
  constructor(id, tagName = 'div') {
    this.id = id;
    this.tagName = tagName.toUpperCase();
    this.attributes = new Map();
    this.classList = new ClassList();
    this.children = [];
    this.options = [];
    this.dataset = {};
    this.style = {};
    this.textContent = '';
    this.className = '';
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.clientWidth = 320;
    this.clientHeight = 72;
    this._value = '';
    this._innerHTML = '';
  }
  get value() {
    if (this.tagName === 'SELECT' && !this._value && this.options.length) return this.options[0].value;
    return this._value;
  }
  set value(v) { this._value = String(v); }
  get innerHTML() { return this._innerHTML; }
  set innerHTML(v) {
    this._innerHTML = String(v);
    if (this.tagName === 'SELECT') { this.options = []; this.children = []; this._value = ''; }
  }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.get(name) || null; }
  appendChild(child) {
    this.children.push(child);
    if (this.tagName === 'SELECT' && child.tagName === 'OPTION') {
      this.options.push(child);
      if (!this._value) this._value = child.value;
    }
    return child;
  }
  addEventListener() {}
  getContext() {
    return {
      createLinearGradient() { return { addColorStop() {} }; },
      clearRect() {}, beginPath() {}, roundRect() {}, fill() {},
    };
  }
  play() { return Promise.resolve(); }
}

const elements = new Map();
const selectIds = new Set(['engine', 'voice', 'voiceZh', 'voiceEnAuto', 'ttsEngine', 'wsEngine']);
const canvasIds = new Set(['viz']);
function tagFor(id) { return selectIds.has(id) ? 'select' : (canvasIds.has(id) ? 'canvas' : 'div'); }
function ensure(id) {
  if (!elements.has(id)) elements.set(id, new Element(id, tagFor(id)));
  return elements.get(id);
}

const storage = new Map();
globalThis.localStorage = {
  getItem(k) { return storage.has(k) ? storage.get(k) : null; },
  setItem(k, v) { storage.set(k, String(v)); },
  removeItem(k) { storage.delete(k); },
};

globalThis.document = {
  documentElement: ensure('html'),
  title: '',
  getElementById: ensure,
  createElement(tag) { return new Element('', tag); },
  querySelectorAll(selector) {
    if (selector === '.out') return ['outAuth', 'outHealth', 'outVoices', 'outTts', 'outWs'].map(ensure);
    if (selector === '#speedGroup button') return ensure('speedGroup').children;
    return [];
  },
};

globalThis.window = globalThis;
globalThis.window.addEventListener = () => {};
globalThis.requestAnimationFrame = () => 1;
// setInterval/clearInterval 打桩为 no-op：pump ticker 用真实定时器会让 Node 事件循环
// 永不退出导致测试超时。契约测试只验证纯函数/状态机，不依赖定时器真正触发。
globalThis.setInterval = () => 0;
globalThis.clearInterval = () => {};
globalThis.console = { error() {}, log() {} };
"""


def inline_scripts(name):
    html = (ROOT / name).read_text(encoding="utf-8")
    return "\n".join(
        match.group(1)
        for match in re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S | re.I)
    )


def run_node_contract(name, setup_js, assertion_js):
    html_text = (ROOT / name).read_text(encoding="utf-8")
    script = "\n".join(
        [COMMON_JS_STUB, f"const SOURCE_HTML = {html_text!r};", setup_js, inline_scripts(name), assertion_js]
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tmp:
        tmp.write(script)
        tmp_path = pathlib.Path(tmp.name)
    try:
        result = subprocess.run(
            ["node", str(tmp_path)],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=20,
        )
    finally:
        tmp_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)


class FrontendContractTests(unittest.TestCase):
    def test_api_page_documents_current_supported_endpoints(self):
        api_page = (ROOT / "api.html").read_text(encoding="utf-8")

        self.assertIn('/api/voices/preview', api_page)
        self.assertIn('/api/logs', api_page)
        self.assertIn('ffmpeg missing', api_page)
        self.assertIn('data-i18n="tocLogs"', api_page)
        self.assertIn('data-i18n="tocPreview"', api_page)
        self.assertIn('data-i18n="healthRespFfmpeg"', api_page)
        self.assertIn('data-i18n-html="logsDesc"', api_page)
        self.assertIn('data-i18n-html="previewDesc"', api_page)
        self.assertIn('engine and <code>ffmpeg</code> readiness', api_page)

    def test_index_routes_mixed_language_without_losing_terms(self):
        setup = r"""
globalThis.fetch = async () => ({ ok: true, json: async () => ({
  kokoro: [
    { id: 'zf_xiaoxiao', name: 'Xiaoxiao', gender: 'female', language: 'zh' },
    { id: 'af_heart', name: 'Heart', gender: 'female', language: 'en' },
  ],
  edge: [
    { id: 'zh-CN-XiaoxiaoNeural', name: 'ignored', gender: 'Female', locale: 'zh-CN' },
    { id: 'en-US-AvaMultilingualNeural', name: 'ignored', gender: 'Female', locale: 'en-US' },
  ],
}) });
document.getElementById('engine').value = 'auto';
"""
        assertions = r"""
(async () => {
  await voicesPromise;
  equal(currentLang, 'en', 'index defaults to English');
  equal(document.documentElement.lang, 'en', 'index document lang');
    equal(parseEdgeName('en-US-AvaMultilingualNeural'), 'Ava', 'Edge short name is cleaned');
    equal(voiceLabel({ gender: 'vf', name: 'Ava', region: 'US' }), 'Female · Ava · US', 'English voice label');

  const engine = document.getElementById('engine');
  const text = document.getElementById('t');
  engine.value = 'auto';
  updateVoices();
  equal(document.getElementById('voiceZh').value, 'zh-CN-XiaoxiaoNeural', 'Auto prefers Edge zh voice');
  equal(document.getElementById('voiceEnAuto').value, 'en-US-AvaMultilingualNeural', 'Auto prefers Edge en voice');

  document.getElementById('voiceZh').value = 'zf_xiaoxiao';
  document.getElementById('voiceEnAuto').value = 'af_heart';
  text.value = '你好 DNS works. English only.';
  deepEqual(computeSentences(), [
    { engine: 'kokoro', voice: 'zf_xiaoxiao', text: '你好' },
    { engine: 'kokoro', voice: 'af_heart', text: 'DNS works.' },
    { engine: 'kokoro', voice: 'af_heart', text: 'English only.' },
  ], 'Auto routes Chinese and English segments separately');

  engine.value = 'kokoro';
  updateVoices();
  document.getElementById('voice').value = 'zf_xiaoxiao';
  text.value = '中文 OpenWrt DNS.';
  deepEqual(computeSentences(), [
    { engine: 'kokoro', voice: 'zf_xiaoxiao', text: '中文' },
    { engine: 'kokoro', voice: 'af_heart', text: 'OpenWrt DNS.' },
  ], 'Kokoro mode falls back to the matching language voice');

  const previewCalls = [];
  globalThis.fetch = async (url) => {
    previewCalls.push(String(url));
    return { ok: true, arrayBuffer: async () => new ArrayBuffer(8) };
  };
  globalThis.AudioContext = class {
    constructor() { this.destination = {}; this.state = 'running'; }
    createGain() { return { gain: { value: 1 }, connect() {} }; }
    createAnalyser() { return { fftSize: 0, connect() {}, getByteFrequencyData() {} }; }
    decodeAudioData() { return Promise.resolve({}); }
    createBufferSource() { return { connect() {}, start() { if (this.onended) this.onended(); } }; }
  };
  await previewVoice();
  assertOk(previewCalls[0].startsWith('/api/voices/preview?'), 'preview calls voice preview endpoint');
  assertOk(previewCalls[0].includes('voice=zf_xiaoxiao'), 'preview sends selected voice');
  finish();
})().catch(err => { throw err; });
"""
        run_node_contract("index.html", setup, assertions)

    def test_index_keeps_error_for_failed_websocket_runs(self):
        setup = r"""
globalThis.fetch = async () => ({ ok: true, json: async () => ({
  kokoro: [
    { id: 'zf_xiaoxiao', name: 'Xiaoxiao', gender: 'female', language: 'zh' },
    { id: 'af_heart', name: 'Heart', gender: 'female', language: 'en' },
  ],
  edge: [],
}) });
globalThis.location = { protocol: 'http:', host: 'tts.local' };
document.getElementById('engine').value = 'auto';
globalThis.AudioContext = class {
  constructor() { this.destination = {}; this.state = 'running'; this.currentTime = 0; }
  createGain() { return { gain: { value: 1 }, connect() {} }; }
  createAnalyser() { return { fftSize: 0, connect() {}, getByteFrequencyData() {} }; }
  createBuffer() { return { duration: 0.01, copyToChannel() {} }; }
  createBufferSource() { return { connect() {}, start() {}, stop() {}, disconnect() {} }; }
  resume() { return Promise.resolve(); }
};
"""
        assertions = r"""
(async () => {
  await voicesPromise;
  const sockets = [];
  globalThis.WebSocket = class {
    constructor(url) { this.url = url; sockets.push(this); setTimeout(() => this.onopen(), 0); }
    send(body) {
      const sent = JSON.parse(body);
      this.sent = this.sent || [];
      this.sent.push(sent);
      if (this.sent.length === 1) {
        setTimeout(() => {
          this.onmessage({ data: JSON.stringify({ type: 'start' }) });
          this.onmessage({ data: JSON.stringify({ type: 'seg', text: sent.text }) });
          this.onmessage({ data: JSON.stringify({ type: 'end' }) });
        }, 0);
      } else {
        setTimeout(() => {
          this.onmessage({ data: JSON.stringify({ type: 'start' }) });
          if (this.onclose) this.onclose();
        }, 0);
      }
    }
    close() { if (this.onclose) this.onclose(); }
  };

  document.getElementById('engine').value = 'kokoro';
  updateVoices();
  document.getElementById('voice').value = 'zf_xiaoxiao';
  document.getElementById('t').value = '中文 DNS.';
  await start();
  await new Promise(resolve => setTimeout(resolve, 40));

  equal(sockets[0].sent.length, 2, 'second run was sent on the same WebSocket');
  equal(lastStatus, 'error', 'later run close before end is an error');

  globalThis.WebSocket = class {
    constructor(url) { this.url = url; sockets.push(this); setTimeout(() => this.onopen(), 0); }
    send() {
      setTimeout(() => {
        this.onmessage({ data: JSON.stringify({ type: 'start' }) });
        this.onmessage({ data: JSON.stringify({ type: 'error', message: 'boom' }) });
        if (this.onclose) this.onclose();
      }, 0);
    }
    close() { if (this.onclose) this.onclose(); }
  };
  document.getElementById('t').value = '中文。';
  await start();
  await new Promise(resolve => setTimeout(resolve, 40));

  equal(lastStatus, 'error', 'close after backend error keeps error status');
  finish();
})().catch(err => { throw err; });
"""
        run_node_contract("index.html", setup, assertions)

    def test_index_stop_and_restart_ignore_stale_websocket_events(self):
        setup = r"""
globalThis.fetch = async () => ({ ok: true, json: async () => ({
  kokoro: [
    { id: 'zf_xiaoxiao', name: 'Xiaoxiao', gender: 'female', language: 'zh' },
    { id: 'af_heart', name: 'Heart', gender: 'female', language: 'en' },
  ],
  edge: [],
}) });
globalThis.location = { protocol: 'http:', host: 'tts.local' };
document.getElementById('engine').value = 'kokoro';
globalThis.AudioContext = class {
  constructor() { this.destination = {}; this.state = 'running'; this.currentTime = 0; }
  createGain() { return { gain: { value: 1 }, connect() {} }; }
  createAnalyser() { return { fftSize: 0, connect() {}, getByteFrequencyData() {} }; }
  createBuffer() { return { duration: 0.01, copyToChannel() {} }; }
  createBufferSource() { return { connect() {}, start() {}, stop() {}, disconnect() {} }; }
  resume() { return Promise.resolve(); }
};
"""
        assertions = r"""
(async () => {
  await voicesPromise;
  const sockets = [];
  globalThis.WebSocket = class {
    constructor(url) {
      this.url = url;
      this.sent = [];
      sockets.push(this);
      setTimeout(() => { if (this.onopen) this.onopen(); }, 0);
    }
    send(body) { this.sent.push(JSON.parse(body)); }
    close() { this.closed = true; }
  };

  updateVoices();
  document.getElementById('voice').value = 'zf_xiaoxiao';
  document.getElementById('t').value = '中文。';
  await start();
  await new Promise(resolve => setTimeout(resolve, 20));

  const stoppedSocket = sockets[0];
  equal(stoppedSocket.sent.length, 1, 'initial run was sent before stop');
  stop();
  equal(lastStatus, 'idle', 'stop returns UI to idle');
  stoppedSocket.onmessage({ data: JSON.stringify({ type: 'error', message: 'late failure' }) });
  stoppedSocket.onmessage({ data: JSON.stringify({ type: 'end' }) });
  if (stoppedSocket.onclose) stoppedSocket.onclose();
  if (stoppedSocket.onerror) stoppedSocket.onerror();
  equal(lastStatus, 'idle', 'stale events after stop are ignored');
  equal(isPlaying, false, 'stop leaves playback disabled after stale events');
  equal(runQueue.length, 0, 'stop clears pending runs after stale events');

  document.getElementById('t').value = '中文。';
  await start();
  await new Promise(resolve => setTimeout(resolve, 20));
  const oldSocket = sockets[1];
  oldSocket.onmessage({ data: JSON.stringify({ type: 'start' }) });
  oldSocket.onmessage({ data: JSON.stringify({ type: 'seg', text: '中文。' }) });
  const pcm = new ArrayBuffer(4);
  new Int16Array(pcm).set([1000, -1000]);
  oldSocket.onmessage({ data: pcm });
  equal(writePos, 2, 'old socket produced audio before restart');

  restartFromCurrentSentence();
  await new Promise(resolve => setTimeout(resolve, 20));
  const restartedSocket = sockets[2];
  equal(restartedSocket.sent.length, 1, 'restart sends a run on the new socket');
  equal(writePos, 0, 'restart truncates current sentence before stale events');
  equal(lastStatus, 'streaming', 'restart is streaming on the new socket');

  oldSocket.onmessage({ data: JSON.stringify({ type: 'error', message: 'late failure' }) });
  oldSocket.onmessage({ data: JSON.stringify({ type: 'end' }) });
  oldSocket.onmessage({ data: pcm });
  if (oldSocket.onclose) oldSocket.onclose();
  if (oldSocket.onerror) oldSocket.onerror();
  equal(lastStatus, 'streaming', 'stale events after restart do not change new run status');
  equal(writePos, 0, 'stale binary audio after restart is ignored');
  equal(runIndex, 0, 'stale terminal events after restart do not advance the new run');
  finish();
})().catch(err => { throw err; });
"""
        run_node_contract("index.html", setup, assertions)

    def test_api_tester_auth_and_ws_contracts(self):
        setup = r"""
globalThis.location = { protocol: 'http:', host: 'tts.local' };
globalThis.URL = { createObjectURL() { return 'blob:tts'; }, revokeObjectURL() {} };
globalThis.AudioContext = class {
  constructor() { this.destination = {}; this.closed = false; }
  createBuffer() { return { copyToChannel() {} }; }
  createBufferSource() { return { connect() {}, start() { if (this.onended) this.onended(); } }; }
  close() { this.closed = true; return Promise.resolve(); }
};
"""
        assertions = r"""
(async () => {
  keyInput.value = 'secret';
  saveKey();
  const headers = authHeaders({ 'Content-Type': 'application/json' });
  equal(headers['X-API-Key'], 'secret', 'REST tester sends X-API-Key');
  assertOk(!('Authorization' in headers), 'REST tester must not overwrite Authorization');

  globalThis.fetch = async () => ({ status: 401, ok: false, text: async () => '<html>login</html>' });
  await testAuth();
  equal(document.getElementById('outAuth').className, 'out err', 'auth HTML 401 is an error');
  assertOk(document.getElementById('outAuth').textContent.includes('Reverse proxy returned 401'), 'auth shows proxy 401 hint');
  await testVoices();
  assertOk(document.getElementById('outVoices').textContent.includes('Reverse proxy returned 401'), 'voices shows proxy 401 hint');

  const sockets = [];
  globalThis.WebSocket = class {
    constructor(url) { this.url = url; sockets.push(this); setTimeout(() => this.onopen(), 0); }
    send(body) {
      this.sent = JSON.parse(body);
      const bytes = new ArrayBuffer(4);
      new Int16Array(bytes).set([100, -100]);
      setTimeout(() => {
        this.onmessage({ data: JSON.stringify({ type: 'start' }) });
        this.onmessage({ data: bytes });
        this.onmessage({ data: JSON.stringify({ type: 'end' }) });
      }, 0);
    }
    close() { if (this.onclose) this.onclose(); }
  };
  document.getElementById('wsText').value = 'hello';
  document.getElementById('wsEngine').value = 'kokoro';
  document.getElementById('wsVoice').value = 'af_heart';
  document.getElementById('wsSpeed').value = '1.25';
  await testWs();
  await new Promise(resolve => setTimeout(resolve, 20));
  assertOk(!SOURCE_HTML.includes('process lifetime'), 'API docs describe TTL voice cache');
  equal(sockets[0].url, 'ws://tts.local/ws/tts?key=secret', 'WS tester sends key in query');
  assertOk(!document.getElementById('outWs').textContent.includes('secret'), 'WS output masks key');
  equal(document.getElementById('outWs').className, 'out ok', 'WS tester keeps ok terminal class');
  deepEqual(sockets[0].sent, { text: 'hello', engine: 'kokoro', voice: 'af_heart', speed: 1.25 }, 'WS payload');

  let ttsRequest;
  globalThis.fetch = async (url, opts) => {
    ttsRequest = { url, opts };
    return { ok: true, blob: async () => ({ size: 2048, type: 'audio/mpeg' }) };
  };
  document.getElementById('ttsText').value = '<speak>Hello</speak>';
  document.getElementById('ttsEngine').value = 'edge';
  document.getElementById('ttsVoice').value = 'en-US-AriaNeural';
  document.getElementById('ttsSpeed').value = '1.1';
  document.getElementById('ttsDownload').checked = true;
  await testTts();
  equal(ttsRequest.url, '/api/tts?download=true', 'REST tester sends download query');
  deepEqual(JSON.parse(ttsRequest.opts.body), {
    text: '<speak>Hello</speak>', engine: 'edge', voice: 'en-US-AriaNeural', speed: 1.1, ssml: false,
  }, 'REST tester keeps raw SSML disabled');
  finish();
})().catch(err => { throw err; });
"""
        run_node_contract("api.html", setup, assertions)


class SentenceSplitRegexParityTests(unittest.TestCase):
    """切句正则前后端一致性(B2)：把"靠注释对齐"升级为"靠测试锁死"。

    后端 split_text 与前端 splitSentences 是句边界的单一事实源，二者必须
    逐字符一致——否则 Kokoro over WS 的句级时间线会漂移(变速续播错位)。
    此测试不跑 Node，直接从两份源码抽出切句正则做等值比对，无运行时副作用。
    """

    def test_backend_and_frontend_split_regex_are_identical(self):
        app_src = (ROOT / "app.py").read_text(encoding="utf-8")
        html_src = (ROOT / "index.html").read_text(encoding="utf-8")

        # 后端：re.split(r'...') —— 抽出单引号原始字符串内的 pattern
        back = re.search(r"re\.split\(r'([^']*)', text\)", app_src)
        self.assertIsNotNone(back, "未能在 app.py 定位 split_text 的切句正则")

        # 前端：.split(/.../) —— 抽出正则字面量(去掉首尾斜杠)
        front = re.search(r"\.split\(/(.+?)/\)", html_src)
        self.assertIsNotNone(front, "未能在 index.html 定位 splitSentences 的切句正则")

        self.assertEqual(
            back.group(1),
            front.group(1),
            "前后端切句正则已漂移：app.py 的 split_text 与 index.html 的 "
            "splitSentences 必须逐字符一致(句级时间线对齐依赖此)",
        )


if __name__ == "__main__":
    unittest.main()
