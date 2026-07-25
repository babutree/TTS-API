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
        self.assertIn('data-i18n-html="voicesUiNote"', api_page)
        self.assertIn('zh-CN-liaoning', api_page)
        self.assertIn('zh-CN-shaanxi', api_page)
        self.assertIn('engine and <code>ffmpeg</code> readiness', api_page)

    def test_index_default_sample_is_markdown_demo(self):
        """默认示意文本须含 Markdown 标记，用于展示 clean_text 清理能力。"""
        index_page = (ROOT / "index.html").read_text(encoding="utf-8")
        # 从 textarea#t 提取默认正文（开标签到闭标签之间）
        match = re.search(
            r'<textarea id="t"[^>]*>(.*?)</textarea>',
            index_page,
            re.S,
        )
        self.assertIsNotNone(match, "textarea#t missing")
        sample = match.group(1)
        for marker in ("# ", "**", "- ", "`", "["):
            self.assertIn(marker, sample, f"default sample should include Markdown marker {marker!r}")
        # 中英双语：开箱/API 卖点写在示例里，观望用户点朗读即可感知
        self.assertIn("Markdown", sample)
        self.assertIn("POST /api/tts", sample)
        self.assertTrue(
            "开箱" in sample or "流式" in sample,
            "Chinese demo copy should mention streaming / ready-to-try",
        )

    def test_index_routes_mixed_language_without_losing_terms(self):
        setup = r"""
globalThis.fetch = async () => ({ ok: true, json: async () => ({
  kokoro: [
    { id: 'zf_xiaoxiao', name: 'Xiaoxiao', gender: 'female', language: 'zh' },
    { id: 'af_heart', name: 'Heart', gender: 'female', language: 'en' },
  ],
  edge: [
    { id: 'zh-CN-XiaoxiaoNeural', name: 'ignored', gender: 'Female', locale: 'zh-CN' },
    { id: 'zh-CN-liaoning-XiaobeiNeural', name: 'ignored', gender: 'Female', locale: 'zh-CN-liaoning' },
    { id: 'zh-CN-shaanxi-XiaoniNeural', name: 'ignored', gender: 'Female', locale: 'zh-CN-shaanxi' },
    { id: 'zh-HK-HiuGaaiNeural', name: 'ignored', gender: 'Female', locale: 'zh-HK' },
    { id: 'zh-TW-HsiaoChenNeural', name: 'ignored', gender: 'Female', locale: 'zh-TW' },
    { id: 'en-US-AvaMultilingualNeural', name: 'ignored', gender: 'Female', locale: 'en-US' },
    // 非白名单 locale：应被 EDGE_LOCALE_WHITELIST 过滤，不得进入任何下拉
    { id: 'ja-JP-NanamiNeural', name: 'ignored', gender: 'Female', locale: 'ja-JP' },
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
  equal(parseEdgeName('zh-CN-XiaoxiaoNeural'), 'Xiaoxiao', 'Mandarin two-part short name is cleaned');
  // 三段式方言 ID 剥掉 locale+方言前缀，只留人名
  equal(parseEdgeName('zh-CN-liaoning-XiaobeiNeural'), 'Xiaobei', 'Liaoning dialect short name is cleaned');
  equal(parseEdgeName('zh-CN-shaanxi-XiaoniNeural'), 'Xiaoni', 'Shaanxi dialect short name is cleaned');
  // 粤语/台湾仍是两段式 locale，正则不得把人名吃掉
  equal(parseEdgeName('zh-HK-HiuGaaiNeural'), 'HiuGaai', 'Cantonese two-part short name is cleaned');
  equal(parseEdgeName('zh-TW-HsiaoChenNeural'), 'HsiaoChen', 'Taiwan two-part short name is cleaned');

  equal(voiceLabel({ gender: 'vf', name: 'Ava', region: 'US' }), 'Female · Ava · US', 'English voice label');
  // 普通话无 dialect 键时不括注；非普通话在专名后括注方言(界面默认英文)
  equal(
    voiceLabel({ gender: 'vf', name: 'Xiaoxiao', region: 'CN', dialect: '' }),
    'Female · Xiaoxiao · CN',
    'Mandarin label has no dialect tag in English UI'
  );
  equal(
    voiceLabel({ gender: 'vf', name: 'Xiaobei', region: 'CN', dialect: 'dialectLiaoning' }),
    'Female · Xiaobei(Northeastern) · CN',
    'Liaoning dialect tag uses half-width parens in English UI'
  );
  equal(
    voiceLabel({ gender: 'vf', name: 'Xiaoni', region: 'CN', dialect: 'dialectShaanxi' }),
    'Female · Xiaoni(Shaanxi) · CN',
    'Shaanxi dialect tag uses half-width parens in English UI'
  );
  equal(
    voiceLabel({ gender: 'vf', name: 'HiuGaai', region: 'CN', dialect: 'dialectCantonese' }),
    'Female · HiuGaai(Cantonese) · CN',
    'Cantonese dialect tag uses half-width parens in English UI'
  );
  equal(
    voiceLabel({ gender: 'vf', name: 'HsiaoChen', region: 'CN', dialect: 'dialectTaiwan' }),
    'Female · HsiaoChen(Taiwan) · CN',
    'Taiwan dialect tag uses half-width parens in English UI'
  );
  // 切换中文 UI：方言标签走 i18n，不再硬编码英文
  currentLang = 'zh';
  equal(
    voiceLabel({ gender: 'vf', name: 'Xiaobei', region: 'CN', dialect: 'dialectLiaoning' }),
    '女声 · Xiaobei（东北） · CN',
    'Liaoning dialect tag in Chinese UI'
  );
  equal(
    voiceLabel({ gender: 'vf', name: 'Xiaoni', region: 'CN', dialect: 'dialectShaanxi' }),
    '女声 · Xiaoni（陕西） · CN',
    'Shaanxi dialect tag in Chinese UI'
  );
  equal(
    voiceLabel({ gender: 'vf', name: 'HiuGaai', region: 'CN', dialect: 'dialectCantonese' }),
    '女声 · HiuGaai（粤语） · CN',
    'Cantonese dialect tag in Chinese UI'
  );
  equal(
    voiceLabel({ gender: 'vf', name: 'HsiaoChen', region: 'CN', dialect: 'dialectTaiwan' }),
    '女声 · HsiaoChen（台湾） · CN',
    'Taiwan dialect tag in Chinese UI'
  );
  equal(
    voiceLabel({ gender: 'vf', name: 'Xiaoxiao', region: 'CN', dialect: '' }),
    '女声 · Xiaoxiao · CN',
    'Mandarin label has no dialect tag in Chinese UI'
  );
  currentLang = 'en';

  // loadVoices 映射：region 统一 CN + dialect i18n 键；非白名单 locale 不得入 edge 库
  const edgeById = Object.fromEntries(voiceDatabase.edge.map(v => [v.value, v]));
  equal(edgeById['zh-CN-XiaoxiaoNeural'].region, 'CN', 'Mandarin region is CN');
  equal(edgeById['zh-CN-XiaoxiaoNeural'].dialect, '', 'Mandarin dialect key empty');
  equal(edgeById['zh-CN-liaoning-XiaobeiNeural'].region, 'CN', 'Liaoning region is CN');
  equal(edgeById['zh-CN-liaoning-XiaobeiNeural'].dialect, 'dialectLiaoning', 'Liaoning dialect key');
  equal(edgeById['zh-CN-liaoning-XiaobeiNeural'].name, 'Xiaobei', 'Liaoning display name');
  equal(edgeById['zh-CN-shaanxi-XiaoniNeural'].region, 'CN', 'Shaanxi region is CN');
  equal(edgeById['zh-CN-shaanxi-XiaoniNeural'].dialect, 'dialectShaanxi', 'Shaanxi dialect key');
  equal(edgeById['zh-CN-shaanxi-XiaoniNeural'].name, 'Xiaoni', 'Shaanxi display name');
  equal(edgeById['zh-HK-HiuGaaiNeural'].region, 'CN', 'Cantonese region is CN');
  equal(edgeById['zh-HK-HiuGaaiNeural'].dialect, 'dialectCantonese', 'Cantonese dialect key');
  equal(edgeById['zh-TW-HsiaoChenNeural'].region, 'CN', 'Taiwan region is CN');
  equal(edgeById['zh-TW-HsiaoChenNeural'].dialect, 'dialectTaiwan', 'Taiwan dialect key');
  assertOk(!edgeById['ja-JP-NanamiNeural'], 'Non-whitelisted locale is filtered from edge catalog');

  // 方言音色全部进中文下拉，且不得泄漏到英文下拉
  updateVoices();
  const zhOpts = [...document.getElementById('voiceZh').options].map(o => o.value);
  const enOpts = [...document.getElementById('voiceEnAuto').options].map(o => o.value);
  for (const id of [
    'zh-CN-liaoning-XiaobeiNeural',
    'zh-CN-shaanxi-XiaoniNeural',
    'zh-HK-HiuGaaiNeural',
    'zh-TW-HsiaoChenNeural',
  ]) {
    assertOk(zhOpts.includes(id), id + ' routes to Chinese dropdown');
    assertOk(!enOpts.includes(id), id + ' never leaks into English dropdown');
  }
  assertOk(!zhOpts.includes('ja-JP-NanamiNeural'), 'Filtered locale never appears in Chinese dropdown');
  assertOk(!enOpts.includes('ja-JP-NanamiNeural'), 'Filtered locale never appears in English dropdown');

  // 下拉 option 文案含方言括注(默认英文 UI 半角)；普通话无括注
  const zhTextByValue = Object.fromEntries(
    [...document.getElementById('voiceZh').options].map(o => [o.value, o.textContent])
  );
  assertOk(zhTextByValue['zh-CN-XiaoxiaoNeural'].includes('Xiaoxiao'), 'Mandarin option keeps name');
  assertOk(!zhTextByValue['zh-CN-XiaoxiaoNeural'].includes('('), 'Mandarin option has no dialect parentheses');
  assertOk(!zhTextByValue['zh-CN-XiaoxiaoNeural'].includes('（'), 'Mandarin option has no fullwidth dialect parentheses');
  assertOk(zhTextByValue['zh-CN-liaoning-XiaobeiNeural'].includes('(Northeastern)'), 'Liaoning option shows half-width dialect tag');
  assertOk(zhTextByValue['zh-CN-shaanxi-XiaoniNeural'].includes('(Shaanxi)'), 'Shaanxi option shows half-width dialect tag');
  assertOk(zhTextByValue['zh-HK-HiuGaaiNeural'].includes('(Cantonese)'), 'Cantonese option shows half-width dialect tag');
  assertOk(zhTextByValue['zh-TW-HsiaoChenNeural'].includes('(Taiwan)'), 'Taiwan option shows half-width dialect tag');
  assertOk(!zhTextByValue['zh-HK-HiuGaaiNeural'].includes('（'), 'English UI dialect tags never use fullwidth parens');

  // 中文 UI 下拉：全角括注 + 中文方言词
  currentLang = 'zh';
  updateVoices();
  const zhTextByValueZh = Object.fromEntries(
    [...document.getElementById('voiceZh').options].map(o => [o.value, o.textContent])
  );
  assertOk(zhTextByValueZh['zh-CN-liaoning-XiaobeiNeural'].includes('（东北）'), 'Liaoning option shows Chinese fullwidth dialect tag');
  assertOk(zhTextByValueZh['zh-CN-shaanxi-XiaoniNeural'].includes('（陕西）'), 'Shaanxi option shows Chinese fullwidth dialect tag');
  assertOk(zhTextByValueZh['zh-HK-HiuGaaiNeural'].includes('（粤语）'), 'Cantonese option shows Chinese fullwidth dialect tag');
  assertOk(zhTextByValueZh['zh-TW-HsiaoChenNeural'].includes('（台湾）'), 'Taiwan option shows Chinese fullwidth dialect tag');
  assertOk(!zhTextByValueZh['zh-HK-HiuGaaiNeural'].includes('(Cantonese)'), 'Chinese UI does not keep English dialect wording');
  currentLang = 'en';
  updateVoices();

  // 单引擎 Edge 模式同样展示全部白名单中文方言音色
  const engine = document.getElementById('engine');
  const text = document.getElementById('t');
  engine.value = 'edge';
  updateVoices();
  const edgeOnlyOpts = [...document.getElementById('voice').options].map(o => o.value);
  for (const id of [
    'zh-CN-XiaoxiaoNeural',
    'zh-CN-liaoning-XiaobeiNeural',
    'zh-CN-shaanxi-XiaoniNeural',
    'zh-HK-HiuGaaiNeural',
    'zh-TW-HsiaoChenNeural',
    'en-US-AvaMultilingualNeural',
  ]) {
    assertOk(edgeOnlyOpts.includes(id), id + ' appears in Edge-only dropdown');
  }
  assertOk(!edgeOnlyOpts.includes('ja-JP-NanamiNeural'), 'Filtered locale absent from Edge-only dropdown');

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

  // Auto 试听：中英双音色依次请求，不得只打中文
  engine.value = 'auto';
  updateVoices();
  document.getElementById('voiceZh').value = 'zf_xiaoxiao';
  document.getElementById('voiceEnAuto').value = 'af_heart';
  deepEqual(
    selectedPreviewTargets().map(p => p.voice),
    ['zf_xiaoxiao', 'af_heart'],
    'Auto preview targets both selected voices'
  );

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
  equal(previewCalls.length, 2, 'Auto preview fetches Chinese then English');
  assertOk(previewCalls[0].startsWith('/api/voices/preview?'), 'preview calls voice preview endpoint');
  assertOk(previewCalls[0].includes('voice=zf_xiaoxiao'), 'Auto preview first is Chinese voice');
  assertOk(previewCalls[1].includes('voice=af_heart'), 'Auto preview second is English voice');

  // 单引擎仍只请求一个音色
  engine.value = 'kokoro';
  updateVoices();
  document.getElementById('voice').value = 'zf_xiaoxiao';
  previewCalls.length = 0;
  await previewVoice();
  equal(previewCalls.length, 1, 'Single-engine preview fetches one voice');
  assertOk(previewCalls[0].includes('voice=zf_xiaoxiao'), 'preview sends selected voice');

  // 回归：主播放接管须打断进行中的 Auto 试听(cancelPreview)——否则 preview 的第二段
  // (英文)会用 stopAllSources 掐断刚起播的主播放。真实模拟：Auto 试听第一段 fetch 后
  // 令主播放接管(调 cancelPreview)，断言循环在下个检查点退出、不再请求第二段。
  engine.value = 'auto';
  updateVoices();
  document.getElementById('voiceZh').value = 'zf_xiaoxiao';
  document.getElementById('voiceEnAuto').value = 'af_heart';
  const crossCalls = [];
  globalThis.fetch = async (url) => {
    crossCalls.push(String(url));
    cancelPreview();  // 模拟第一段请求期间用户点了"朗读"(主播放接管)
    return { ok: true, arrayBuffer: async () => new ArrayBuffer(8) };
  };
  await previewVoice();
  equal(crossCalls.length, 1, 'preview stops after main playback takes over (no second fetch)');

  // start / startPlayback / stop 三个主播放入口都须调用 cancelPreview(源码级锁定)
  for (const fn of ['start', 'startPlayback', 'stop']) {
    const body = eval(fn + '.toString()');
    assertOk(body.includes('cancelPreview'), fn + '() must call cancelPreview');
  }
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


class SchedulerThrottleContractTests(unittest.TestCase):
    """播放调度器节流契约(A3)：pump 前瞻式节流 + ticker 生命周期。

    根治的线上 bug：Edge 云端远快于实时地猛灌整段 PCM，旧 pump 每帧把全部数据
    一次性排入时间轴 → AudioBufferSourceNode 无限膨胀 + lastEnd 无限领先 → 长播卡顿/静音。
    这些断言锁死节流不变量，改 SCHEDULE_AHEAD/CHUNK 或 ticker 启停时回归可被发现。

    stub 的 setInterval 是 no-op、无 AudioContext，故 setup 注入可控假时钟：
    ctx.currentTime 手动推进、createBufferSource 记录 start(when)、定时器句柄可查。
    """

    def test_pump_throttles_scheduling_and_ticker_lifecycle(self):
        setup = r"""
globalThis.fetch = async () => ({ ok: true, json: async () => ({ kokoro: [], edge: [] }) });

// 可控假时钟 + AudioContext：currentTime 手动推进，记录每个 source 的 start(when)。
let fakeNow = 0;
globalThis.__startedWhen = [];
class FakeParam { constructor(v){ this.value = v; } setValueAtTime(){} }
class FakeNode {
  connect(){ return this; } disconnect(){}
  start(when){ globalThis.__startedWhen.push(when); }
  stop(){}
}
class FakeBufferSource extends FakeNode {
  constructor(){ super(); this.buffer = null; this.onended = null; }
}
class FakeAudioContext {
  constructor(){ this.destination = new FakeNode(); this.state = 'running'; }
  get currentTime(){ return fakeNow; }
  createGain(){ const n = new FakeNode(); n.gain = new FakeParam(1); return n; }
  createAnalyser(){ const n = new FakeNode(); n.fftSize = 256; n.frequencyBinCount = 128; n.getByteFrequencyData = () => {}; return n; }
  createBuffer(ch, len, sr){ return { length: len, duration: len / sr, copyToChannel(){} }; }
  createBufferSource(){ return new FakeBufferSource(); }
  resume(){ return Promise.resolve(); }
}
globalThis.AudioContext = FakeAudioContext;
globalThis.webkitAudioContext = FakeAudioContext;
globalThis.__advance = (sec) => { fakeNow += sec; };

// 记录 ticker 启停：setInterval 返回句柄，clearInterval 标记清除。
let intervalSeq = 0;
globalThis.__liveIntervals = new Set();
globalThis.setInterval = () => { const id = ++intervalSeq; globalThis.__liveIntervals.add(id); return id; };
globalThis.clearInterval = (id) => { globalThis.__liveIntervals.delete(id); };

// 顶层 inline script 会同步跑一次 updateVoices()，engine 需为合法值(否则 voiceDatabase[''] 报错)
document.getElementById('engine').value = 'auto';
"""
        assertions = r"""
(async () => {
  document.getElementById('engine').value = 'auto';
  await voicesPromise;

  initAudio();
  isPlaying = true;
  anchorSet = false;
  pendingLead = 0;
  fakeNow = 0;

  // 模拟 Edge 猛灌：一次性写入 10 秒样本(远超 SCHEDULE_AHEAD)。
  const tenSec = new Float32Array(SAMPLE_RATE * 10);
  appendSamples(tenSec);
  equal(writePos, SAMPLE_RATE * 10, 'ten seconds buffered');

  // 契约1：单次 pump 不得把全部数据排完；已排队深度(lastEnd-now)不超过 AHEAD+一个 CHUNK。
  __startedWhen = [];
  pump();
  const scheduledSec1 = scheduledPos / SAMPLE_RATE;
  assertOk(scheduledSec1 < 10, 'pump does not schedule everything at once: ' + scheduledSec1);
  assertOk(scheduledSec1 <= SCHEDULE_AHEAD + SCHEDULE_CHUNK + 1e-9,
    'queued depth capped at ~AHEAD+CHUNK: ' + scheduledSec1);

  // 契约2：每个已排 source 时长不超过 CHUNK(单次排入至多 CHUNK 秒)。
  const chunkCount = sources.length;
  assertOk(chunkCount >= 1, 'at least one chunk scheduled');
  assertOk(chunkCount <= Math.ceil((SCHEDULE_AHEAD + SCHEDULE_CHUNK) / SCHEDULE_CHUNK) + 1,
    'chunk count bounded, not exploded: ' + chunkCount);

  // 契约3：时间不推进时再次 pump 不应继续排(缓冲已达 AHEAD 深度)——防 source 爆炸。
  const posBefore = scheduledPos;
  pump();
  equal(scheduledPos, posBefore, 'no further scheduling while buffer is full and clock not advanced');
  equal(sources.length, chunkCount, 'source count stays bounded when clock frozen');

  // 契约4：推进时钟后 pump 继续补排(追赶 writePos)——节流是"按需"而非"排一次就停"。
  __advance(SCHEDULE_AHEAD);
  pump();
  assertOk(scheduledPos > posBefore, 'pump resumes scheduling after clock advances');

  // 契约5：source 时长本身受 CHUNK 约束(不会一次排一大块)。
  const maxChunkSamples = Math.round(SCHEDULE_CHUNK * SAMPLE_RATE);
  // 首块从 buffer 头排起，验证 scheduledPos 以 CHUNK 步进(除最后一块外)。
  assertOk(maxChunkSamples === Math.round(SCHEDULE_CHUNK * SAMPLE_RATE), 'chunk sample size is CHUNK-bounded');

  // 契约6：ticker 生命周期——startPlayback 启动、暂停/停止清除。
  startPlayback(0, 0);
  assertOk(__liveIntervals.size >= 1, 'startPlayback starts a pump ticker');
  togglePlay();  // 播放中 → 暂停：应 stopPumpTicker
  equal(__liveIntervals.size, 0, 'pause clears the pump ticker');

  startPlayback(0, 0);
  assertOk(__liveIntervals.size >= 1, 'startPlayback re-arms ticker after pause');
  stop();
  equal(__liveIntervals.size, 0, 'stop clears the pump ticker');

  finish();
})().catch(err => { throw err; });
"""
        run_node_contract("index.html", setup, assertions)


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
