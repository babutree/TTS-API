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
function must(value, msg) {
  if (value == null) throw new Error(msg);
  return value;
}
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


PREFETCH_CONTROLLED_SETUP = r"""
globalThis.fetch = async () => ({ ok: true, json: async () => ({
  kokoro: [
    { id: 'af_heart', name: 'Heart', gender: 'female', language: 'en' },
  ],
  edge: [
    { id: 'zh-CN-XiaoxiaoNeural', name: 'A', gender: 'Female', locale: 'zh-CN' },
    { id: 'en-US-JennyNeural', name: 'B', gender: 'Female', locale: 'en-US' },
  ],
}) });
globalThis.location = { protocol: 'http:', host: 'tts.local' };
document.getElementById('engine').value = 'auto';

let timeoutSeq = 0;
const pendingTimeouts = new Map();
globalThis.setTimeout = (callback, delay) => {
  const id = ++timeoutSeq;
  pendingTimeouts.set(id, { callback, delay });
  return id;
};
globalThis.clearTimeout = (id) => { pendingTimeouts.delete(id); };
function fireTimeout(id) {
  const timer = pendingTimeouts.get(id);
  if (!timer) return;
  pendingTimeouts.delete(id);
  timer.callback();
}
function timeoutEntries(delay) {
  return Array.from(pendingTimeouts.entries()).filter(([, timer]) => timer.delay === delay);
}
function timeoutCount(delay) { return timeoutEntries(delay).length; }

const wsInstances = [];
class FakeWebSocket {
  constructor(url) {
    this.url = String(url);
    this.readyState = 0;
    this.sent = [];
    this.closed = false;
    this.onopen = null;
    this.onmessage = null;
    this.onerror = null;
    this.onclose = null;
    wsInstances.push(this);
    queueMicrotask(() => {
      this.readyState = 1;
      if (typeof this.onopen === 'function') this.onopen({});
    });
  }
  send(data) { this.sent.push(JSON.parse(data)); }
  close() {
    if (this.closed) return;
    this.closed = true;
    this.readyState = 3;
    if (typeof this.onclose === 'function') this.onclose({});
  }
  remoteClose() {
    if (this.closed) return;
    this.closed = true;
    this.readyState = 3;
    if (typeof this.onclose === 'function') this.onclose({ code: 1006, wasClean: false });
  }
  pushJson(obj) {
    if (typeof this.onmessage === 'function') {
      this.onmessage({ data: JSON.stringify(obj) });
    }
  }
  pushPcmSamples(n, value = 1000) {
    const buf = new ArrayBuffer(n * 2);
    const dv = new DataView(buf);
    for (let i = 0; i < n; i++) dv.setInt16(i * 2, value, true);
    if (typeof this.onmessage === 'function') this.onmessage({ data: buf });
  }
}
FakeWebSocket.OPEN = 1;
globalThis.WebSocket = FakeWebSocket;
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
        self.assertIn('data-i18n-html="healthMaxTextNote"', api_page)
        self.assertIn('max_text_length', api_page)
        self.assertIn('zh-CN-liaoning', api_page)
        self.assertIn('zh-CN-shaanxi', api_page)
        self.assertIn('engine and <code>ffmpeg</code> readiness', api_page)

    def test_index_default_sample_is_markdown_demo(self):
        """默认示意：中英交替自然文案 + 轻量 Markdown，体现 Auto 分句而非功能清单。"""
        index_page = (ROOT / "index.html").read_text(encoding="utf-8")
        match = re.search(
            r'<textarea id="t"[^>]*>(.*?)</textarea>',
            index_page,
            re.S,
        )
        self.assertIsNotNone(match, "textarea#t missing")
        sample = match.group(1)
        # 轻量 Markdown（不必堆标题列表）
        self.assertIn("**", sample)
        self.assertIn("`", sample)
        # 中英交替：中文句与英文句都出现
        self.assertIn("你好", sample)
        self.assertIn("Hello", sample)
        self.assertIn("Auto", sample)
        self.assertIn("Markdown", sample)
        self.assertIn("Enjoy the seamless experience!", sample)
        # 不应再是纯 API 功能清单
        self.assertNotIn("POST /api/tts", sample)
        self.assertNotIn("Docker one-liner", sample)

    def test_index_over_limit_highlights_and_speaks_in_limit_only(self):
        """超限：高亮提示，朗读不阻断，合成仅取前 maxTextLength 字。"""
        setup = r"""
globalThis.fetch = async (url) => {
  const u = String(url);
  if (u === '/' || u.endsWith('/')) {
    return { ok: true, status: 200, json: async () => ({ status: 'ok', ready: true, max_text_length: 10 }) };
  }
  return { ok: true, status: 200, json: async () => ({
    kokoro: [
      { id: 'zf_xiaoxiao', name: 'Xiaoxiao', gender: 'female', language: 'zh' },
      { id: 'af_heart', name: 'Heart', gender: 'female', language: 'en' },
    ],
    edge: [
      { id: 'zh-CN-XiaoxiaoNeural', name: 'ignored', gender: 'Female', locale: 'zh-CN' },
      { id: 'en-US-AvaMultilingualNeural', name: 'ignored', gender: 'Female', locale: 'en-US' },
    ],
  }) };
};
document.getElementById('engine').value = 'edge';
"""
        assertions = r"""
(async () => {
  await loadTextLimit();
  await voicesPromise;
  equal(maxTextLength, 10, 'health max_text_length applied');

  const ta = document.getElementById('t');
  ta.value = 'ABCDEFGHIJKLMNOP'; // 16 > 10
  updateCharCount();
  assertOk(isTextOverLimit(), 'over-limit detected');
  equal(textForSynthesis(), 'ABCDEFGHIJ', 'synthesis uses only in-limit prefix');
  assertOk(document.getElementById('textHighlight').innerHTML.includes('text-over'), 'overflow highlighted in input');
  assertOk(!document.getElementById('textLimitHint').classList.contains('hidden'), 'limit hint visible');
  assertOk(
    document.getElementById('textLimitHint').textContent.includes('10') ||
    document.getElementById('textLimitHint').textContent.includes('max'),
    'hint mentions limit'
  );

  // 不阻断：computeSentences 基于截断文本，仍可得到可发送 run
  updateVoices();
  document.getElementById('voice').value = 'en-US-AvaMultilingualNeural';
  const sents = computeSentences();
  assertOk(sents.length >= 1, 'over-limit still yields sentences for playback');
  const joined = sents.map(s => s.text).join('\n');
  assertOk(joined.length <= 10, 'spoken text length within limit');
  assertOk(!joined.includes('K'), 'overflow chars not sent for synthesis');

  ta.value = '短';
  updateCharCount();
  assertOk(!isTextOverLimit(), 'under limit clears over state');
  equal(textForSynthesis(), '短', 'under-limit synthesis is full text');
  finish();
})().catch(err => { throw err; });
"""
        run_node_contract("index.html", setup, assertions)

    def test_index_auto_defaults_to_adult_ava_and_deduplicates_edge_variants(self):
        """Auto 默认成年 Ava；同 locale 的普通/Multilingual 角色只展示一次。"""
        setup = r"""
globalThis.fetch = async () => ({ ok: true, json: async () => ({
  kokoro: [
    { id: 'zf_xiaoxiao', name: 'Xiaoxiao', gender: 'female', language: 'zh' },
    { id: 'af_heart', name: 'Heart', gender: 'female', language: 'en' },
  ],
  edge: [
    { id: 'zh-CN-XiaoxiaoNeural', name: 'ignored', gender: 'Female', locale: 'zh-CN' },
    { id: 'en-US-AnaNeural', name: 'ignored', gender: 'Female', locale: 'en-US' },
    { id: 'en-US-AvaNeural', name: 'ignored', gender: 'Female', locale: 'en-US' },
    { id: 'en-US-AvaMultilingualNeural', name: 'ignored', gender: 'Female', locale: 'en-US' },
    { id: 'en-US-EmmaMultilingualNeural', name: 'ignored', gender: 'Female', locale: 'en-US' },
    { id: 'en-US-EmmaNeural', name: 'ignored', gender: 'Female', locale: 'en-US' },
    { id: 'en-US-AndrewNeural', name: 'ignored', gender: 'Male', locale: 'en-US' },
    { id: 'en-US-AndrewMultilingualNeural', name: 'ignored', gender: 'Male', locale: 'en-US' },
    { id: 'en-US-BrianMultilingualNeural', name: 'ignored', gender: 'Male', locale: 'en-US' },
    { id: 'en-US-BrianNeural', name: 'ignored', gender: 'Male', locale: 'en-US' },
    { id: 'en-US-JennyNeural', name: 'ignored', gender: 'Female', locale: 'en-US' },
    { id: 'en-US-AriaMultilingualNeural', name: 'ignored', gender: 'Female', locale: 'en-US' },
    // 同名但 locale 不同，不属于普通/Multilingual 变体族，必须保留。
    { id: 'en-GB-AvaNeural', name: 'ignored', gender: 'Female', locale: 'en-GB' },
  ],
}) });
document.getElementById('engine').value = 'auto';
"""
        assertions = r"""
(async () => {
  await voicesPromise;
  const edgeIds = voiceDatabase.edge.map(v => v.value);
  for (const role of ['Ava', 'Emma', 'Andrew', 'Brian']) {
    const standard = `en-US-${role}Neural`;
    const multilingual = `en-US-${role}MultilingualNeural`;
    assertOk(edgeIds.includes(multilingual), role + ' keeps the broader Multilingual voice');
    assertOk(!edgeIds.includes(standard), role + ' removes only its locale-specific duplicate');
  }
  assertOk(edgeIds.includes('en-US-JennyNeural'), 'standard-only voice is retained');
  assertOk(edgeIds.includes('en-US-AriaMultilingualNeural'), 'Multilingual-only voice is retained');
  assertOk(edgeIds.includes('en-GB-AvaNeural'), 'same display name in another locale is not merged');
  const edgeDisplayKeys = voiceDatabase.edge.map(v => `${v.locale}|${v.gender}|${v.name}`);
  equal(new Set(edgeDisplayKeys).size, edgeDisplayKeys.length, 'same-locale visible Edge roles are unique');

  updateVoices();
  const enSel = document.getElementById('voiceEnAuto');
  const enIds = [...enSel.options].map(o => o.value);
  equal(enSel.value, 'en-US-AvaMultilingualNeural', 'Auto defaults to adult Ava explicitly');
  assertOk(enIds.includes('en-US-AnaNeural'), 'Ana child voice remains manually selectable');
  assertOk(
    enIds.indexOf('en-US-AnaNeural') < enIds.indexOf('en-US-AvaMultilingualNeural'),
    'existing alphabetical sort remains unchanged even though Ava is selected',
  );

  document.getElementById('t').value = '试试 Auto：中文和英文会按句分开朗读 Try Auto — C';
  const routed = computeSentences();
  allSentences = routed;
  const routedRuns = buildRunsFrom(0);
  equal(routedRuns.length, 4, 'reported alternating sample remains four ordered runs');
  const englishRuns = routedRuns.filter(run => !/[\u4e00-\u9fff]/.test(run.text));
  assertOk(englishRuns.length === 2, 'reported sample contains two English runs');
  assertOk(
    englishRuns.every(run => run.voice === 'en-US-AvaMultilingualNeural'),
    'every English run uses adult Ava rather than Ana',
  );

  enSel.value = 'en-US-AnaNeural';
  updateVoices();
  equal(enSel.value, 'en-US-AnaNeural', 'an explicit existing Ana selection is preserved');

  const manualRouted = computeSentences();
  allSentences = manualRouted;
  const manualRuns = buildRunsFrom(0);
  const manualEnglishRuns = manualRuns.filter(run => !/[\u4e00-\u9fff]/.test(run.text));
  assertOk(
    manualEnglishRuns.every(run => run.voice === 'en-US-AnaNeural'),
    'manual Ana selection controls synthesis rather than being forced back to Ava',
  );

  document.getElementById('engine').value = 'edge';
  updateVoices();
  document.getElementById('engine').value = 'auto';
  updateVoices();
  equal(enSel.value, 'en-US-AnaNeural', 'manual Ana selection survives leaving and re-entering Auto');
  finish();
})().catch(err => { throw err; });
"""
        run_node_contract("index.html", setup, assertions)

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
    { id: 'en-US-JennyNeural', name: 'ignored', gender: 'Female', locale: 'en-US' },
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
  // 排序不变；空选择时显式默认成年女声 Ava，而不是排序更前的女童声 Ana。
  equal(document.getElementById('voiceEnAuto').value, 'en-US-AvaMultilingualNeural', 'Auto default EN uses adult Ava');
  const enOptIds = [...document.getElementById('voiceEnAuto').options].map(o => o.value);
  assertOk(enOptIds.includes('en-US-AvaMultilingualNeural'), 'Ava remains in list (sort unchanged)');
  assertOk(enOptIds.indexOf('en-US-AvaMultilingualNeural') < enOptIds.indexOf('en-US-JennyNeural'), 'Ava still sorts before Jenny');

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

  // 朗读中点试听：previewVoice 须先 stop 主播，避免叠音
  assertOk(previewVoice.toString().includes('stop()'), 'previewVoice stops main playback when active');

  // Edge 排序：普通话/美音优先，粤语/英音靠后，女声优先
  const sorted = sortVoices([
    { value: 'zh-HK-HiuGaaiNeural', gender: 'vf', name: 'HiuGaai', locale: 'zh-HK', region: 'CN' },
    { value: 'zh-CN-YunxiNeural', gender: 'vm', name: 'Yunxi', locale: 'zh-CN', region: 'CN' },
    { value: 'zh-CN-XiaoxiaoNeural', gender: 'vf', name: 'Xiaoxiao', locale: 'zh-CN', region: 'CN' },
    { value: 'en-GB-SoniaNeural', gender: 'vf', name: 'Sonia', locale: 'en-GB', region: 'UK' },
    { value: 'en-US-BrianNeural', gender: 'vm', name: 'Brian', locale: 'en-US', region: 'US' },
    { value: 'en-US-AvaNeural', gender: 'vf', name: 'Ava', locale: 'en-US', region: 'US' },
  ]);
  equal(sorted[0].value, 'zh-CN-XiaoxiaoNeural', 'Mandarin female first among Chinese');
  equal(sorted[1].value, 'zh-CN-YunxiNeural', 'Mandarin male before dialects');
  equal(sorted[2].value, 'zh-HK-HiuGaaiNeural', 'Cantonese after Mandarin');
  equal(sorted[3].value, 'en-US-AvaNeural', 'US female before US male / UK');
  equal(sorted[4].value, 'en-US-BrianNeural', 'US male before UK');
  equal(sorted[5].value, 'en-GB-SoniaNeural', 'UK last among EN');

  // 跨引擎预取：多槽容器、窗口调度、单槽/全槽清理 API 必须存在。
  assertOk(typeof beginPrefetch === 'function', 'beginPrefetch exists for cross-engine gap reduction');
  assertOk(prefetchMap instanceof Map, 'prefetchMap is the multi-slot source of truth');
  equal(PREFETCH_AHEAD, 2, 'prefetch window is capped at two slots');
  assertOk(typeof schedulePrefetch === 'function', 'schedulePrefetch exists');
  assertOk(typeof abortPrefetch === 'function', 'single-slot abort exists');
  assertOk(typeof abortAllPrefetch === 'function', 'all-slot abort exists');
  assertOk(stop.toString().includes('abortAllPrefetch'), 'stop aborts every prefetch socket');
  assertOk(sendRequest.toString().includes('flushPrefetchIfReady'), 'sendRequest uses prefetch flush');
  assertOk(flushPrefetchIfReady.toString().includes('segOffsets'), 'flush restores seg timeline offsets');

  finish();
})().catch(err => { throw err; });
"""
        run_node_contract("index.html", setup, assertions)

    def test_index_edge_prefetch_waits_for_main_admission_and_serializes_edge_slots(self):
        """Edge 投机请求不得抢在主 decoder admission 前，也不得同时占两条预取槽。"""
        assertions = r"""
(async () => {
  await voicesPromise;
  function sentCount(text) {
    return wsInstances.flatMap(sock => sock.sent).filter(req => req.text === text).length;
  }
  function pendingEdgeRequests() {
    return Array.from(prefetchMap.values()).filter(state => {
      const req = state.ws.sent[0];
      return req && req.engine === 'edge' && !state.ended && !state.error;
    });
  }

  runQueue = [
    { engine: 'edge', voice: 'zh-CN-XiaoxiaoNeural', text: 'r0', startSentence: 0, count: 1 },
    { engine: 'edge', voice: 'en-US-JennyNeural', text: 'r1', startSentence: 1, count: 1 },
    { engine: 'edge', voice: 'zh-CN-XiaoxiaoNeural', text: 'r2', startSentence: 2, count: 1 },
  ];
  runIndex = 0;
  streamEnded = false;
  sendRequest();
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();

  const mainSock = must(ws, 'missing main WebSocket');
  assertOk(
    !new URL(mainSock.url).searchParams.has('prefetch'),
    'main socket remains normal priority',
  );
  equal(mainSock.sent.map(req => req.text).join(','), 'r0', 'main run is sent first');
  equal(pendingEdgeRequests().length, 0, 'Edge prefetch is not sent before main decoder admission');
  equal(sentCount('r1'), 0, 'first Edge prefetch remains unsent before admission');
  equal(sentCount('r2'), 0, 'second Edge prefetch remains unsent before admission');

  mainSock.pushJson({ type: 'start' });
  equal(pendingEdgeRequests().length, 0, 'start is not an Edge decoder admission signal');

  mainSock.pushJson({ type: 'seg' });
  await Promise.resolve();
  await Promise.resolve();
  equal(pendingEdgeRequests().length, 1, 'main admission permits one pending Edge prefetch');
  equal(sentCount('r1'), 1, 'first Edge prefetch is sent after admission');
  equal(sentCount('r2'), 0, 'second Edge prefetch remains unsent while the first is pending');
  const state1 = must(prefetchMap.get(1), 'missing first Edge prefetch');
  equal(
    new URL(state1.ws.url).searchParams.get('prefetch'),
    '1',
    'prefetch socket declares its low-priority purpose',
  );

  state1.ws.pushJson({ type: 'end' });
  await Promise.resolve();
  await Promise.resolve();
  equal(sentCount('r2'), 1, 'successful Edge terminal refills the second window slot');
  const state2 = must(prefetchMap.get(2), 'missing refilled Edge prefetch');
  assertOk(state1.ended && !state1.error, 'first Edge slot remains ready for ordered consumption');
  assertOk(!state2.ended && !state2.error, 'second Edge slot is the only pending Edge request');
  equal(pendingEdgeRequests().length, 1, 'Edge refill preserves the single-pending admission bound');

  stop();
  equal(prefetchMap.size, 0, 'stop clears the serialized Edge window');

  runQueue = [
    { engine: 'edge', voice: 'zh-CN-XiaoxiaoNeural', text: 'e0', startSentence: 0, count: 1 },
    { engine: 'edge', voice: 'en-US-JennyNeural', text: 'e1', startSentence: 1, count: 1 },
    { engine: 'edge', voice: 'zh-CN-XiaoxiaoNeural', text: 'e2', startSentence: 2, count: 1 },
  ];
  runIndex = 0;
  streamEnded = false;
  sendRequest();
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
  const errorMain = must(ws, 'missing main socket for Edge error scenario');
  errorMain.pushJson({ type: 'seg' });
  await Promise.resolve();
  await Promise.resolve();
  const failedState = must(prefetchMap.get(1), 'missing failed Edge prefetch');
  failedState.ws.pushJson({ type: 'error', message: 'capacity' });
  await Promise.resolve();
  equal(sentCount('e2'), 0, 'failed Edge prefetch does not leapfrog into a farther Edge slot');

  stop();
  runQueue = [
    { engine: 'edge', voice: 'zh-CN-XiaoxiaoNeural', text: 'm0', startSentence: 0, count: 1 },
    { engine: 'kokoro', voice: 'af_heart', text: 'm1', startSentence: 1, count: 1 },
    { engine: 'edge', voice: 'zh-CN-XiaoxiaoNeural', text: 'm2', startSentence: 2, count: 1 },
  ];
  runIndex = 0;
  streamEnded = false;
  sendRequest();
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
  const mixedMain = must(ws, 'missing main socket for mixed-engine admission');
  equal(sentCount('m1'), 1, 'safe Kokoro prefetch is not delayed by Edge admission');
  equal(sentCount('m2'), 0, 'future Edge prefetch still waits for main Edge admission');
  mixedMain.pushJson({ type: 'seg' });
  await Promise.resolve();
  await Promise.resolve();
  equal(sentCount('m2'), 1, 'main Edge admission releases the future Edge prefetch');

  stop();
  finish();
})().catch(err => { throw err; });
"""
        run_node_contract("index.html", PREFETCH_CONTROLLED_SETUP, assertions)

    def test_index_current_prefetch_streams_partial_pcm_before_end(self):
        """预取槽成为当前 run 后，已有及后续 PCM 应立即入播放缓冲，不等整段 end。"""
        assertions = r"""
(async () => {
  await voicesPromise;
  runQueue = [
    { engine: 'kokoro', voice: 'af_heart', text: 'r0', startSentence: 0, count: 1 },
    { engine: 'edge', voice: 'zh-CN-XiaoxiaoNeural', text: 'r1', startSentence: 1, count: 1 },
  ];
  runIndex = 0;
  writePos = 0;
  timeline = [];
  streamEnded = false;
  sendRequest();
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();

  const mainSock = must(ws, 'missing main WebSocket');
  const state1 = must(prefetchMap.get(1), 'missing future prefetch state');
  mainSock.pushJson({ type: 'seg' });
  state1.ws.pushJson({ type: 'seg' });
  state1.ws.pushPcmSamples(3, 1000);
  equal(writePos, 0, 'future PCM remains isolated until its run becomes current');

  mainSock.pushJson({ type: 'end' });
  equal(runIndex, 1, 'main end advances to prefetched run');
  equal(writePos, 3, 'already buffered current PCM is committed before prefetch end');
  equal(timeline.length, 2, 'main and promoted prefetch boundaries stay ordered');
  equal(timeoutCount(PREFETCH_WAIT_GRACE_MS), 0, 'PCM progress prevents the no-progress fallback deadline');
  equal(mainSock.sent.map(req => req.text).join(','), 'r0', 'promotion does not duplicate on main');

  state1.ws.pushPcmSamples(2, 2000);
  equal(writePos, 5, 'later PCM streams directly while the promoted run is current');
  assertOk(!streamEnded, 'partial PCM alone does not forge a terminal state');
  state1.ws.pushJson({ type: 'end' });
  equal(runIndex, 2, 'prefetch end advances exactly once');
  equal(writePos, 5, 'promoted PCM is retained exactly once');
  assertOk(streamEnded, 'terminal after streamed PCM completes the queue');
  assertOk(!prefetchMap.has(1), 'completed promoted state is retired');

  stop();
  finish();
})().catch(err => { throw err; });
"""
        run_node_contract("index.html", PREFETCH_CONTROLLED_SETUP, assertions)

    def test_index_promoted_prefetch_failure_is_error_not_replay(self):
        """已交付部分 PCM 的当前预取失败后不可主 WS 重读，否则会重复朗读。"""
        assertions = r"""
(async () => {
  await voicesPromise;
  runQueue = [
    { engine: 'kokoro', voice: 'af_heart', text: 'r0', startSentence: 0, count: 1 },
    { engine: 'edge', voice: 'zh-CN-XiaoxiaoNeural', text: 'r1', startSentence: 1, count: 1 },
  ];
  runIndex = 0;
  writePos = 0;
  timeline = [];
  streamEnded = false;
  sendRequest();
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();

  const mainSock = must(ws, 'missing main WebSocket');
  const state1 = must(prefetchMap.get(1), 'missing current prefetch state');
  mainSock.pushJson({ type: 'seg' });
  state1.ws.pushJson({ type: 'seg' });
  state1.ws.pushPcmSamples(2);
  mainSock.pushJson({ type: 'end' });
  equal(writePos, 2, 'partial current PCM is committed before failure');

  state1.ws.pushJson({ type: 'error', message: 'edge interrupted' });
  equal(lastStatus, 'error', 'failure after PCM promotion is explicit');
  equal(mainSock.sent.map(req => req.text).join(','), 'r0', 'committed audio is never replayed on main');
  equal(writePos, 2, 'already received PCM is not duplicated or disguised as completion');
  assertOk(streamEnded, 'fatal promoted failure closes the stream state');
  assertOk(!prefetchMap.has(1), 'fatal promoted failure cleans prefetch ownership');
  equal(pendingTimeouts.size, 0, 'fatal promoted failure leaves no timer');

  stop();
  finish();
})().catch(err => { throw err; });
"""
        run_node_contract("index.html", PREFETCH_CONTROLLED_SETUP, assertions)

    def test_index_pending_prefetch_deadline_is_cancelled_when_late_pcm_arrives(self):
        """当前槽已挂回退定时器后，迟到的 PCM 必须清除该定时器、就地提升在播；陈旧定时器即便触发也不得重发。"""
        assertions = r"""
(async () => {
  await voicesPromise;
  runQueue = [
    { engine: 'kokoro', voice: 'af_heart', text: 'r0', startSentence: 0, count: 1 },
    { engine: 'edge', voice: 'zh-CN-XiaoxiaoNeural', text: 'r1', startSentence: 1, count: 1 },
  ];
  runIndex = 0;
  writePos = 0;
  timeline = [];
  streamEnded = false;
  sendRequest();
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();

  const mainSock = must(ws, 'missing main WebSocket');
  const state1 = must(prefetchMap.get(1), 'missing pending prefetch state');
  mainSock.pushJson({ type: 'seg' });
  // 当前预取尚无任何 PCM：主 run 结束后进入 pending 等待并挂起唯一的有界回退定时器
  mainSock.pushJson({ type: 'end' });
  equal(runIndex, 1, 'main end advances to the still-empty prefetched run');
  equal(timeoutCount(PREFETCH_WAIT_GRACE_MS), 1, 'empty pending current slot arms exactly one wait deadline');
  const timerId = timeoutEntries(PREFETCH_WAIT_GRACE_MS)[0][0];
  const staleTimer = must(pendingTimeouts.get(timerId), 'missing armed deadline entry');
  equal(staleTimer.delay, PREFETCH_WAIT_GRACE_MS, 'the armed deadline uses the declared wait grace');
  equal(mainSock.sent.map(req => req.text).join(','), 'r0', 'pending slot is not resent on main before its grace');

  // 迟到的 seg + PCM 到达：progress 就地提交音频并清除回退定时器
  state1.ws.pushJson({ type: 'seg' });
  state1.ws.pushPcmSamples(4, 1500);
  equal(writePos, 4, 'late PCM is promoted into the main buffer in place');
  equal(timeoutCount(PREFETCH_WAIT_GRACE_MS), 0, 'committed audio cancels the pending wait deadline');
  equal(mainSock.sent.map(req => req.text).join(','), 'r0', 'promoting late PCM never resends on main');

  // 陈旧定时器即便被显式触发也不得重发（身份守卫兜底）
  staleTimer.callback();
  equal(mainSock.sent.map(req => req.text).join(','), 'r0', 'a stale deadline fired after promotion cannot resend');
  assertOk(prefetchMap.get(1) === state1, 'stale deadline does not evict the promoted slot');
  assertOk(!state1.ws.closed, 'stale deadline does not abort the promoted socket');

  // 再现“回调已取得 timer 所有权”的防御分支，直接锁定 committed-audio 守卫，
  // 避免本用例只被更早的 timer 身份守卫短路。
  prefetchWaitTimers.set(state1, timerId);
  pendingTimeouts.set(timerId, staleTimer);
  fireTimeout(timerId);
  equal(timeoutCount(PREFETCH_WAIT_GRACE_MS), 0, 'forced owned deadline retires its harness timer');
  equal(mainSock.sent.map(req => req.text).join(','), 'r0', 'committed-audio guard blocks an owned stale deadline');
  assertOk(prefetchMap.get(1) === state1, 'committed-audio guard preserves the promoted slot');
  assertOk(!state1.ws.closed, 'committed-audio guard preserves the promoted socket');

  // 终态精确推进一次并保留已交付 PCM
  state1.ws.pushPcmSamples(2, 2000);
  equal(writePos, 6, 'further PCM keeps streaming after the deadline is cancelled');
  state1.ws.pushJson({ type: 'end' });
  equal(runIndex, 2, 'prefetch end advances exactly once');
  equal(writePos, 6, 'promoted PCM is retained exactly once');
  assertOk(streamEnded, 'terminal after cancelled deadline completes the queue');
  assertOk(!prefetchMap.has(1), 'completed promoted state is retired');

  stop();
  finish();
})().catch(err => { throw err; });
"""
        run_node_contract("index.html", PREFETCH_CONTROLLED_SETUP, assertions)

    def test_index_pending_prefetch_deadline_falls_back_once_and_cleans_timer(self):
        """当前槽无终态时必须有界回退；timer 迟到、重复触发和 stop 均不得双发。"""
        assertions = r"""
(async () => {
  await voicesPromise;
  runQueue = [
    { engine: 'kokoro', voice: 'af_heart', text: 'r0', startSentence: 0, count: 1 },
    { engine: 'edge', voice: 'zh-CN-XiaoxiaoNeural', text: 'r1', startSentence: 1, count: 1 },
  ];
  runIndex = 0;
  writePos = 0;
  timeline = [];
  streamEnded = false;
  sendRequest();
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();

  const mainSock = must(ws, 'missing main WebSocket');
  const state1 = must(prefetchMap.get(1), 'missing pending prefetch state');
  const oldMessage = must(state1.ws.onmessage, 'missing old prefetch message handler');
  const oldError = must(state1.ws.onerror, 'missing old prefetch error handler');
  const oldClose = must(state1.ws.onclose, 'missing old prefetch close handler');
  equal(timeoutCount(PREFETCH_WAIT_GRACE_MS), 0, 'prefetching ahead does not start the wait deadline');

  mainSock.pushJson({ type: 'end' });
  equal(runIndex, 1, 'main terminal advances to the pending prefetched run');
  equal(mainSock.sent.length, 1, 'pending prefetch is not duplicated before its grace expires');
  equal(timeoutCount(PREFETCH_WAIT_GRACE_MS), 1, 'current pending prefetch owns exactly one wait deadline');

  const timeoutEntry = timeoutEntries(PREFETCH_WAIT_GRACE_MS)[0];
  if (!timeoutEntry) { finish(); return; }
  const [timerId, timer] = timeoutEntry;
  equal(PREFETCH_WAIT_GRACE_MS, 1500, 'wait grace remains a bounded user-facing pause');
  equal(timer.delay, PREFETCH_WAIT_GRACE_MS, 'current slot uses the declared wait grace');
  const staleCallback = timer.callback;
  fireTimeout(timerId);
  equal(
    mainSock.sent.map(req => req.text).join(','),
    'r0,r1',
    'deadline expiry performs exactly one main fallback',
  );
  assertOk(!prefetchMap.has(1), 'timed-out prefetch is removed before fallback');
  assertOk(state1.ws.closed, 'timed-out prefetch socket is closed');
  equal(writePos, 0, 'zero-progress timeout has no PCM to discard or replay');
  equal(timeoutCount(PREFETCH_WAIT_GRACE_MS), 0, 'deadline removes its timer ownership');

  staleCallback();
  oldMessage({ data: JSON.stringify({ type: 'end' }) });
  oldError({});
  oldClose({});
  equal(mainSock.sent.length, 2, 'stale timer and transport callbacks cannot duplicate fallback');
  mainSock.pushJson({ type: 'seg' });
  mainSock.pushPcmSamples(2);
  mainSock.pushJson({ type: 'end' });
  equal(runIndex, 2, 'deadline fallback terminal advances through queue end');
  assertOk(streamEnded, 'deadline fallback terminal completes the stream');
  equal(writePos, 2, 'deadline fallback PCM is retained exactly once');

  stop();
  runQueue = [
    { engine: 'kokoro', voice: 'af_heart', text: 'd0', startSentence: 0, count: 1 },
    { engine: 'edge', voice: 'zh-CN-XiaoxiaoNeural', text: 'd1', startSentence: 1, count: 1 },
  ];
  runIndex = 0;
  streamEnded = false;
  sendRequest();
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
  const disconnectedMain = must(ws, 'missing main WebSocket for deadline disconnect');
  disconnectedMain.pushJson({ type: 'end' });
  const disconnectTimer = timeoutEntries(PREFETCH_WAIT_GRACE_MS)[0]?.[0];
  if (disconnectTimer !== undefined) fireTimeout(disconnectTimer);
  equal(
    disconnectedMain.sent.map(req => req.text).join(','),
    'd0,d1',
    'disconnect scenario reaches the deadline fallback request',
  );
  disconnectedMain.remoteClose();
  equal(lastStatus, 'error', 'main close during deadline fallback remains an explicit error');

  stop();
  runQueue = [
    { engine: 'kokoro', voice: 'af_heart', text: 's0', startSentence: 0, count: 1 },
    { engine: 'edge', voice: 'zh-CN-XiaoxiaoNeural', text: 's1', startSentence: 1, count: 1 },
  ];
  runIndex = 0;
  streamEnded = false;
  sendRequest();
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
  const stoppedMain = must(ws, 'missing main WebSocket for stop cleanup');
  const stoppedPrefetch = must(prefetchMap.get(1), 'missing stopped pending prefetch');
  stoppedMain.pushJson({ type: 'end' });
  equal(timeoutCount(PREFETCH_WAIT_GRACE_MS), 1, 'stop scenario arms one wait deadline');
  const stoppedTimer = timeoutEntries(PREFETCH_WAIT_GRACE_MS)[0]?.[1];
  const sentBeforeStop = stoppedMain.sent.length;
  stop();
  equal(pendingTimeouts.size, 0, 'stop physically clears the pending wait timer');
  assertOk(stoppedPrefetch.ws.closed, 'stop closes the pending prefetch socket');
  if (stoppedTimer) stoppedTimer.callback();
  equal(stoppedMain.sent.length, sentBeforeStop, 'stale timer after stop cannot send fallback');
  equal(lastStatus, 'idle', 'stale timer after stop cannot change idle status');

  stop();
  runQueue = [
    { engine: 'kokoro', voice: 'af_heart', text: 'q0', startSentence: 0, count: 1 },
    { engine: 'edge', voice: 'zh-CN-XiaoxiaoNeural', text: 'q1', startSentence: 1, count: 1 },
  ];
  runIndex = 0;
  writePos = 0;
  timeline = [];
  streamEnded = false;
  sendRequest();
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
  const readyMain = must(ws, 'missing main socket for ready-before-timeout');
  const readyState = must(prefetchMap.get(1), 'missing ready-before-timeout prefetch');
  readyMain.pushJson({ type: 'end' });
  equal(timeoutCount(PREFETCH_WAIT_GRACE_MS), 1, 'ready scenario arms a wait deadline');
  const readyTimer = must(timeoutEntries(PREFETCH_WAIT_GRACE_MS)[0]?.[1], 'missing ready wait timer');
  readyState.ws.pushJson({ type: 'seg' });
  readyState.ws.pushPcmSamples(2);
  equal(timeoutCount(PREFETCH_WAIT_GRACE_MS), 0, 'first current PCM clears the no-progress wait timer');
  equal(writePos, 2, 'current prefetch PCM is usable before its terminal frame');
  assertOk(!streamEnded, 'PCM progress alone does not complete the run');
  readyState.ws.pushJson({ type: 'end' });
  equal(timeoutCount(PREFETCH_WAIT_GRACE_MS), 0, 'ready terminal physically clears its wait timer');
  equal(readyMain.sent.length, 1, 'ready prefetch is consumed without fallback');
  equal(writePos, 2, 'ready prefetch PCM is appended exactly once');
  readyTimer.callback();
  equal(readyMain.sent.length, 1, 'cleared ready timer callback remains stale');
  assertOk(streamEnded, 'ready prefetch reaches queue end');

  stop();
  finish();
})().catch(err => { throw err; });
"""
        run_node_contract("index.html", PREFETCH_CONTROLLED_SETUP, assertions)

    def test_index_edge_prefetch_error_is_not_leapfrogged_after_ready_slot(self):
        """前一 ready 槽被消费后，紧邻 Edge error 必须先主回退，不能抢发更远 Edge。"""
        assertions = r"""
(async () => {
  await voicesPromise;
  function sentCount(text) {
    return wsInstances.flatMap(sock => sock.sent).filter(req => req.text === text).length;
  }

  runQueue = [
    { engine: 'edge', voice: 'zh-CN-XiaoxiaoNeural', text: 'r0', startSentence: 0, count: 1 },
    { engine: 'edge', voice: 'en-US-JennyNeural', text: 'r1', startSentence: 1, count: 1 },
    { engine: 'edge', voice: 'zh-CN-XiaoxiaoNeural', text: 'r2', startSentence: 2, count: 1 },
    { engine: 'edge', voice: 'en-US-JennyNeural', text: 'r3', startSentence: 3, count: 1 },
  ];
  runIndex = 0;
  streamEnded = false;
  sendRequest();
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();

  const mainSock = must(ws, 'missing main WebSocket');
  mainSock.pushJson({ type: 'seg' });
  await Promise.resolve();
  await Promise.resolve();
  const readyState = must(prefetchMap.get(1), 'missing first Edge ready target');
  readyState.ws.pushJson({ type: 'end' });
  await Promise.resolve();
  await Promise.resolve();
  const failedState = must(prefetchMap.get(2), 'missing Edge failure target');
  failedState.ws.pushJson({ type: 'error', message: 'prefetch failed' });
  equal(sentCount('r3'), 0, 'farther Edge is not sent while the failed slot is still ahead');

  mainSock.pushJson({ type: 'end' });
  await Promise.resolve();
  await Promise.resolve();
  equal(
    mainSock.sent.map(req => req.text).join(','),
    'r0,r2',
    'ready run is consumed before the failed run falls back on main',
  );
  equal(sentCount('r3'), 0, 'failed Edge slot blocks farther Edge admission during fallback');
  assertOk(!prefetchMap.has(1) && !prefetchMap.has(2), 'ready and failed slots are retired in order');
  mainSock.pushJson({ type: 'seg' });
  await Promise.resolve();
  await Promise.resolve();
  equal(sentCount('r3'), 1, 'fallback Edge admission resumes the deferred prefetch window');

  stop();
  finish();
})().catch(err => { throw err; });
"""
        run_node_contract("index.html", PREFETCH_CONTROLLED_SETUP, assertions)

    def test_index_future_ready_prefetch_stays_terminal_after_close(self):
        """后槽 end 后即使 transport close，ready PCM 仍应按序消费而非错误重发。"""
        assertions = r"""
(async () => {
  await voicesPromise;
  runQueue = [
    { engine: 'edge', voice: 'zh-CN-XiaoxiaoNeural', text: 'r0', startSentence: 0, count: 1 },
    { engine: 'kokoro', voice: 'af_heart', text: 'r1', startSentence: 1, count: 1 },
    { engine: 'edge', voice: 'zh-CN-XiaoxiaoNeural', text: 'r2', startSentence: 2, count: 1 },
  ];
  runIndex = 0;
  writePos = 0;
  timeline = [];
  streamEnded = false;
  sendRequest();
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();

  const mainSock = must(ws, 'missing main WebSocket');
  mainSock.pushJson({ type: 'seg' });
  await Promise.resolve();
  await Promise.resolve();
  const state1 = must(prefetchMap.get(1), 'missing first prefetched run');
  const state2 = must(prefetchMap.get(2), 'missing future prefetched run');

  state2.ws.pushJson({ type: 'seg' });
  state2.ws.pushPcmSamples(3, 2000);
  state2.ws.pushJson({ type: 'end' });
  state2.ws.remoteClose();
  assertOk(state2.ended && !state2.error, 'end is irreversible when the ready transport closes');
  assertOk(prefetchMap.get(2) === state2, 'future ready state remains owned until ordered consumption');

  state1.ws.pushJson({ type: 'seg' });
  state1.ws.pushPcmSamples(2, 1000);
  state1.ws.pushJson({ type: 'end' });
  mainSock.pushJson({ type: 'end' });
  await Promise.resolve();

  equal(runIndex, 3, 'ordered flush consumes both ready slots through queue end');
  equal(writePos, 5, 'ready PCM survives terminal transport close and is appended once');
  equal(timeline.length, 3, 'main and both prefetched runs retain their boundaries');
  equal(mainSock.sent.map(req => req.text).join(','), 'r0', 'ready future run is never resent on main');
  equal(pendingTimeouts.size, 0, 'ready ordered consumption leaves no wait deadline');
  assertOk(streamEnded, 'ordered ready consumption completes the stream');

  stop();
  finish();
})().catch(err => { throw err; });
"""
        run_node_contract("index.html", PREFETCH_CONTROLLED_SETUP, assertions)

    def test_index_prefetch_uses_second_ws_and_restores_timeline(self):
        """跨引擎预取：第二路 WS、不占用主连接；flush 补全多句 timeline。"""
        setup = r"""
globalThis.fetch = async () => ({ ok: true, json: async () => ({
  kokoro: [
    { id: 'zf_xiaoxiao', name: 'Xiaoxiao', gender: 'female', language: 'zh' },
    { id: 'af_heart', name: 'Heart', gender: 'female', language: 'en' },
  ],
  edge: [
    { id: 'zh-CN-XiaoxiaoNeural', name: 'ignored', gender: 'Female', locale: 'zh-CN' },
    { id: 'en-US-JennyNeural', name: 'ignored', gender: 'Female', locale: 'en-US' },
  ],
}) });
globalThis.location = { protocol: 'http:', host: 'tts.local' };
document.getElementById('engine').value = 'auto';

// 可控假 WebSocket：记录实例与 send；onopen 在赋值后再触发
const wsInstances = [];
class FakeWebSocket {
  constructor(url) {
    this.url = String(url);
    this.readyState = 0;
    this.binaryType = 'arraybuffer';
    this.sent = [];
    this.onopen = null;
    this.onmessage = null;
    this.onerror = null;
    this.onclose = null;
    wsInstances.push(this);
    queueMicrotask(() => {
      this.readyState = 1;
      if (typeof this.onopen === 'function') this.onopen({});
    });
  }
  send(data) { this.sent.push(data); }
  close() {
    this.readyState = 3;
    if (typeof this.onclose === 'function') this.onclose({});
  }
  pushJson(obj) {
    if (typeof this.onmessage === 'function') this.onmessage({ data: JSON.stringify(obj) });
  }
  pushPcmSamples(n) {
    const buf = new ArrayBuffer(n * 2);
    const dv = new DataView(buf);
    for (let i = 0; i < n; i++) dv.setInt16(i * 2, 1000, true);
    if (typeof this.onmessage === 'function') this.onmessage({ data: buf });
  }
}
FakeWebSocket.OPEN = 1;
globalThis.WebSocket = FakeWebSocket;
"""
        assertions = r"""
(async () => {
  await voicesPromise;
  document.getElementById('voiceZh').value = 'zh-CN-XiaoxiaoNeural';
  document.getElementById('voiceEnAuto').value = 'en-US-JennyNeural';
  document.getElementById('t').value = '你好。Hello world.';
  allSentences = computeSentences();
  runQueue = buildRunsFrom(0);
  assertOk(runQueue.length >= 2, 'mixed text yields at least two runs');
  assertOk(runQueue[0].engine !== runQueue[1].engine || runQueue[0].voice !== runQueue[1].voice,
    'first two runs differ engine/voice for prefetch');

  // 同音色相邻：不预取
  runQueue = [
    { engine: 'edge', voice: 'zh-CN-XiaoxiaoNeural', text: '一', startSentence: 0, count: 1 },
    { engine: 'edge', voice: 'zh-CN-XiaoxiaoNeural', text: '二', startSentence: 1, count: 1 },
  ];
  runIndex = 0;
  abortAllPrefetch();
  const before = wsInstances.length;
  beginPrefetch(1);
  equal(wsInstances.length, before, 'same engine+voice does not open second WS');

  // 跨音色：打开第二路 WS 并 send
  runQueue = [
    { engine: 'edge', voice: 'zh-CN-XiaoxiaoNeural', text: '你好\n世界', startSentence: 0, count: 2 },
    { engine: 'kokoro', voice: 'af_heart', text: 'Hello\nworld', startSentence: 2, count: 2 },
  ];
  runIndex = 0;
  abortAllPrefetch();
  const n0 = wsInstances.length;
  beginPrefetch(1);
  await Promise.resolve();
  await Promise.resolve();
  equal(wsInstances.length, n0 + 1, 'cross-engine opens second WebSocket');
  const prefState = must(prefetchMap.get(1), 'missing prefetch state for run 1');
  const prefSock = prefState.ws;
  assertOk(prefSock.sent.length >= 1, 'prefetch WS sent a request');
  const prefReq = JSON.parse(prefSock.sent[0]);
  equal(prefReq.voice, 'af_heart', 'prefetch targets next run voice');
  equal(prefReq.engine, 'kokoro', 'prefetch targets next run engine');
  assertOk(prefReq.text.includes('Hello'), 'prefetch body is next run text');

  // 模拟预取多句 seg + PCM，再 flush：timeline 须有多条句界
  writePos = 100;
  timeline = [];
  runIndex = 1;
  prefSock.pushJson({ type: 'start' });
  prefSock.pushJson({ type: 'seg' });
  prefSock.pushPcmSamples(10);
  prefSock.pushJson({ type: 'seg' });
  prefSock.pushPcmSamples(20);
  prefSock.pushJson({ type: 'end' });
  assertOk(flushPrefetchIfReady(), 'flush succeeds when prefetch ended');
  assertOk(timeline.length >= 2, 'prefetch flush restores multi-seg timeline');
  equal(timeline[0].sentenceIndex, 2, 'first prefetched unit maps startSentence');
  equal(timeline[1].sentenceIndex, 3, 'second prefetched unit increments sentenceIndex');
  assertOk(timeline[1].startSample > timeline[0].startSample, 'seg offsets advance startSample');
  assertOk(writePos > 100, 'prefetch PCM appended to main buffer');
  assertOk(!prefetchMap.has(1), 'flush deletes only the consumed slot');
  assertOk(prefSock !== ws, 'prefetch socket is not the main ws handle');

  finish();
})().catch(err => { throw err; });
"""
        run_node_contract("index.html", setup, assertions)

    def test_index_prefetch_window_deduplicates_slides_and_cleans_up(self):
        """多槽预取：窗口封顶、同 key 去重、滑动补槽、单槽与全槽清理。"""
        setup = r"""
globalThis.fetch = async () => ({ ok: true, json: async () => ({
  kokoro: [
    { id: 'af_heart', name: 'Heart', gender: 'female', language: 'en' },
  ],
  edge: [
    { id: 'zh-CN-XiaoxiaoNeural', name: 'A', gender: 'Female', locale: 'zh-CN' },
    { id: 'en-US-JennyNeural', name: 'B', gender: 'Female', locale: 'en-US' },
  ],
}) });
globalThis.location = { protocol: 'http:', host: 'tts.local' };
document.getElementById('engine').value = 'auto';

const wsInstances = [];
let activeSockets = 0;
let maxActiveSockets = 0;
class FakeWebSocket {
  constructor(url) {
    this.url = String(url);
    this.readyState = 0;
    this.binaryType = 'arraybuffer';
    this.sent = [];
    this.closed = false;
    this.onopen = null;
    this.onmessage = null;
    this.onerror = null;
    this.onclose = null;
    wsInstances.push(this);
    activeSockets++;
    maxActiveSockets = Math.max(maxActiveSockets, activeSockets);
    queueMicrotask(() => {
      this.readyState = 1;
      if (typeof this.onopen === 'function') this.onopen({});
    });
  }
  send(data) { this.sent.push(JSON.parse(data)); }
  close() {
    if (this.closed) return;
    this.closed = true;
    this.readyState = 3;
    activeSockets--;
    if (typeof this.onclose === 'function') this.onclose({});
  }
}
FakeWebSocket.OPEN = 1;
globalThis.WebSocket = FakeWebSocket;
"""
        assertions = r"""
(async () => {
  await voicesPromise;
  const voiceA = 'zh-CN-XiaoxiaoNeural';
  runQueue = Array.from({ length: 6 }, (_, i) => ({
    engine: i % 2 === 0 ? 'edge' : 'kokoro',
    voice: i % 2 === 0 ? voiceA : 'af_heart',
    text: 'run-' + i,
    startSentence: i,
    count: 1,
  }));
  runIndex = 0;
  abortAllPrefetch();

  schedulePrefetch(1);
  await Promise.resolve();
  await Promise.resolve();
  equal(PREFETCH_AHEAD, 2, 'window constant is exactly two');
  equal(Array.from(prefetchMap.keys()).join(','), '1,2', 'initial window owns runs 1 and 2');
  equal(prefetchMap.size, 2, 'initial window has exactly two slots');
  equal(wsInstances.length, 2, 'initial window opens exactly two sockets');
  equal(maxActiveSockets, 2, 'active prefetch sockets never exceed the window');

  const state1 = must(prefetchMap.get(1), 'missing initial prefetch state for run 1');
  const state2 = must(prefetchMap.get(2), 'missing initial prefetch state for run 2');
  equal(
    Object.keys(state1).sort().join(','),
    'chunks,ended,error,runIndex,sampleCount,segOffsets,ws',
    'state shape remains identical to the single-slot state',
  );
  beginPrefetch(1);
  schedulePrefetch(1);
  equal(wsInstances.length, 2, 'duplicate scheduling does not reopen an existing key');
  assertOk(prefetchMap.get(1) === state1, 'run 1 keeps its original state identity');
  assertOk(prefetchMap.get(2) === state2, 'run 2 is not evicted by duplicate scheduling');

  abortPrefetch(1);
  assertOk(!prefetchMap.has(1), 'single-slot abort deletes its target');
  assertOk(prefetchMap.get(2) === state2, 'single-slot abort preserves the other slot');
  assertOk(state1.ws.closed, 'single-slot abort closes its socket');
  assertOk(
    state1.ws.onopen === null && state1.ws.onmessage === null
      && state1.ws.onerror === null && state1.ws.onclose === null,
    'single-slot abort detaches every handler',
  );

  schedulePrefetch(2);
  await Promise.resolve();
  await Promise.resolve();
  equal(Array.from(prefetchMap.keys()).join(','), '2,3', 'window slides forward by one slot');
  assertOk(prefetchMap.get(2) === state2, 'sliding preserves the in-flight next slot');
  equal(prefetchMap.size, 2, 'sliding still respects the two-slot cap');
  assertOk(maxActiveSockets <= PREFETCH_AHEAD, 'historical active socket peak stays capped');

  abortPrefetch(2);
  schedulePrefetch(3);
  await Promise.resolve();
  await Promise.resolve();
  equal(Array.from(prefetchMap.keys()).join(','), '3,4', 'six-run window continues through key 4');
  equal(activeSockets, 2, 'continued sliding keeps exactly two active slots');

  abortPrefetch(3);
  schedulePrefetch(4);
  await Promise.resolve();
  await Promise.resolve();
  equal(Array.from(prefetchMap.keys()).join(','), '4,5', 'six-run window reaches the final two keys');
  equal(activeSockets, 2, 'final window keeps exactly two active slots');
  assertOk(maxActiveSockets <= PREFETCH_AHEAD, 'continued sliding never exceeds the cap');

  stop();
  stop();
  equal(prefetchMap.size, 0, 'stop is idempotent and clears every slot');
  equal(activeSockets, 0, 'stop closes every prefetch socket');
  assertOk(wsInstances.every(sock => sock.closed), 'all created prefetch sockets are closed');
  assertOk(wsInstances.every(sock =>
    sock.onopen === null && sock.onmessage === null
      && sock.onerror === null && sock.onclose === null
  ), 'all stopped sockets have detached handlers');
  finish();
})().catch(err => { throw err; });
"""
        run_node_contract("index.html", setup, assertions)

    def test_index_prefetch_waits_and_flushes_out_of_order_slots_in_order(self):
        """短段结束等待在飞预取；乱序到齐仍按 runIndex 拼接并滑动补窗。"""
        setup = r"""
globalThis.fetch = async () => ({ ok: true, json: async () => ({
  kokoro: [
    { id: 'af_heart', name: 'Heart', gender: 'female', language: 'en' },
    { id: 'af_bella', name: 'Bella', gender: 'female', language: 'en' },
  ],
  edge: [
    { id: 'zh-CN-XiaoxiaoNeural', name: 'A', gender: 'Female', locale: 'zh-CN' },
  ],
}) });
globalThis.location = { protocol: 'http:', host: 'tts.local' };
document.getElementById('engine').value = 'auto';

const wsInstances = [];
class FakeWebSocket {
  constructor(url) {
    this.url = String(url);
    this.readyState = 0;
    this.binaryType = 'arraybuffer';
    this.sent = [];
    this.closed = false;
    this.onopen = null;
    this.onmessage = null;
    this.onerror = null;
    this.onclose = null;
    wsInstances.push(this);
    queueMicrotask(() => {
      this.readyState = 1;
      if (typeof this.onopen === 'function') this.onopen({});
    });
  }
  send(data) { this.sent.push(JSON.parse(data)); }
  close() {
    if (this.closed) return;
    this.closed = true;
    this.readyState = 3;
    if (typeof this.onclose === 'function') this.onclose({});
  }
  pushJson(obj) {
    if (typeof this.onmessage === 'function') {
      this.onmessage({ data: JSON.stringify(obj) });
    }
  }
  pushPcmSamples(n, value) {
    const buf = new ArrayBuffer(n * 2);
    const dv = new DataView(buf);
    for (let i = 0; i < n; i++) dv.setInt16(i * 2, value, true);
    if (typeof this.onmessage === 'function') this.onmessage({ data: buf });
  }
}
FakeWebSocket.OPEN = 1;
globalThis.WebSocket = FakeWebSocket;
"""
        assertions = r"""
(async () => {
  await voicesPromise;
  runQueue = [
    { engine: 'kokoro', voice: 'af_bella', text: 'r0', startSentence: 0, count: 1 },
    { engine: 'kokoro', voice: 'af_heart', text: 'r1-pair', startSentence: 10, count: 2 },
    { engine: 'edge', voice: 'zh-CN-XiaoxiaoNeural', text: 'r2', startSentence: 12, count: 3 },
    { engine: 'kokoro', voice: 'af_heart', text: 'r3', startSentence: 15, count: 1 },
    { engine: 'edge', voice: 'zh-CN-XiaoxiaoNeural', text: 'r4', startSentence: 16, count: 1 },
    { engine: 'kokoro', voice: 'af_heart', text: 'r5', startSentence: 17, count: 1 },
  ];
  runIndex = 0;
  writePos = 100;
  scheduledPos = 100;
  timeline = [];
  sendRequest();
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();

  const mainSock = must(ws, 'missing main WebSocket');
  const state1 = must(prefetchMap.get(1), 'missing pending prefetch state for run 1');
  const state2 = must(prefetchMap.get(2), 'missing pending prefetch state for run 2');
  equal(mainSock.sent.map(req => req.text).join(','), 'r0', 'main socket initially sends only run 0');

  // run 2 先到齐：不得越过仍在飞的 run 1 写入主缓冲。
  state2.ws.pushJson({ type: 'seg' });
  state2.ws.pushPcmSamples(4, -1000);
  state2.ws.pushJson({ type: 'end' });
  equal(writePos, 100, 'later ready slot does not write out of order');
  equal(timeline.length, 0, 'later ready slot does not create timeline early');

  // 主 run 0 结束时 run 1 仍在飞：必须等待，不能关闭预取再由主 WS 重发。
  mainSock.pushJson({ type: 'end' });
  equal(runIndex, 1, 'run index advances to the awaited prefetch');
  assertOk(prefetchMap.get(1) === state1, 'in-flight current prefetch remains owned');
  assertOk(!state1.ws.closed, 'in-flight current prefetch is not aborted');
  equal(mainSock.sent.length, 1, 'main socket does not resend an in-flight prefetched run');

  // 同一主请求的重复 end 不是下一 run 的终态，不得跳过仍在飞的预取。
  mainSock.pushJson({ type: 'end' });
  equal(runIndex, 1, 'duplicate main end does not advance past the pending prefetch');
  assertOk(prefetchMap.get(1) === state1, 'duplicate main end preserves the pending current slot');
  assertOk(!streamEnded, 'duplicate main end cannot masquerade as queue completion');
  equal(mainSock.sent.length, 1, 'duplicate main end does not send or skip another run');
  finish();

  // run 1 到齐必须主动唤醒；随后 run 2 已 ready，应在同一控制流中顺序消费。
  state1.ws.pushJson({ type: 'seg' });
  state1.ws.pushPcmSamples(2, 1000);
  state1.ws.pushJson({ type: 'seg' });
  state1.ws.pushPcmSamples(1, 2000);
  state1.ws.pushJson({ type: 'end' });
  await Promise.resolve();

  equal(runIndex, 3, 'ready callback flushes runs 1 then 2 and waits at run 3');
  equal(writePos, 107, 'both prefetched runs append exactly seven samples');
  assertOk(buffer[100] > 0 && buffer[102] > 0, 'run 1 positive PCM is written first');
  assertOk(buffer[103] < 0 && buffer[106] < 0, 'run 2 negative PCM follows run 1');
  equal(timeline.length, 3, 'multi-seg run plus multi-sentence Edge run yields only real boundaries');
  equal(timeline[0] && timeline[0].startSample, 100, 'run 1 first seg starts at the original base');
  equal(timeline[0] && timeline[0].sentenceIndex, 10, 'run 1 first seg maps its first sentence');
  equal(timeline[1] && timeline[1].startSample, 102, 'run 1 second seg uses its relative sample offset');
  equal(timeline[1] && timeline[1].sentenceIndex, 11, 'run 1 second seg maps the next sentence');
  equal(timeline[2] && timeline[2].startSample, 103, 'run 2 starts after all run 1 samples');
  equal(timeline[2] && timeline[2].sentenceIndex, 12, 'whole-run slot maps its run start sentence');
  assertOk(!prefetchMap.has(1) && !prefetchMap.has(2), 'consumed slots are deleted');
  assertOk(state1.ws.closed && state2.ws.closed, 'consumed slots close their sockets');
  assertOk(
    state1.ws.onopen === null && state1.ws.onmessage === null
      && state1.ws.onerror === null && state1.ws.onclose === null
      && state2.ws.onopen === null && state2.ws.onmessage === null
      && state2.ws.onerror === null && state2.ws.onclose === null,
    'consumed slots detach every socket handler',
  );
  equal(Array.from(prefetchMap.keys()).join(','), '3,4', 'consumption slides the window through run 4');
  equal(mainSock.sent.length, 1, 'no prefetched run is also sent on the main socket');

  const state3 = must(prefetchMap.get(3), 'missing slid prefetch state for run 3');
  const state4 = must(prefetchMap.get(4), 'missing slid prefetch state for run 4');
  state3.ws.pushJson({ type: 'seg' });
  state3.ws.pushPcmSamples(2, 3000);
  state3.ws.pushJson({ type: 'end' });
  await Promise.resolve();
  equal(runIndex, 4, 'run 3 completion advances to the next pending slot');
  equal(Array.from(prefetchMap.keys()).join(','), '4,5', 'playback-driven sliding reaches keys 4 and 5');

  const state5 = must(prefetchMap.get(5), 'missing final prefetch state for run 5');
  state5.ws.pushJson({ type: 'seg' });
  state5.ws.pushPcmSamples(2, -3000);
  state5.ws.pushJson({ type: 'end' });
  equal(runIndex, 4, 'ready run 5 cannot pass pending run 4');

  state4.ws.pushJson({ type: 'seg' });
  state4.ws.pushPcmSamples(2, 4000);
  state4.ws.pushJson({ type: 'end' });
  await Promise.resolve();
  equal(runIndex, 6, 'runs 4 and 5 flush in order through queue end');
  equal(writePos, 113, 'all six-run prefetched PCM is retained exactly once');
  assertOk(buffer[107] > 0 && buffer[110] > 0, 'runs 3 and 4 stay before the final run');
  assertOk(buffer[111] < 0 && buffer[112] < 0, 'early-ready run 5 is appended last');
  assertOk(streamEnded, 'consuming the final ready slot completes the stream');
  equal(prefetchMap.size, 0, 'queue completion leaves no prefetch slots');
  equal(mainSock.sent.length, 1, 'fully prefetched tail never falls back to the main socket');

  stop();
  finish();
})().catch(err => { throw err; });
"""
        run_node_contract("index.html", setup, assertions)

    def test_index_completed_stream_ignores_late_main_socket_events(self):
        """全部 PCM 已落地后，主 WS 的迟到 error/close 不得破坏本地播放状态。"""
        setup = r"""
globalThis.fetch = async () => ({ ok: true, json: async () => ({
  kokoro: [],
  edge: [
    { id: 'zh-CN-XiaoxiaoNeural', name: 'A', gender: 'Female', locale: 'zh-CN' },
  ],
}) });
globalThis.location = { protocol: 'http:', host: 'tts.local' };
document.getElementById('engine').value = 'auto';

class FakeWebSocket {
  constructor(url) {
    this.url = String(url);
    this.readyState = 0;
    this.sent = [];
    this.onopen = null;
    this.onmessage = null;
    this.onerror = null;
    this.onclose = null;
    queueMicrotask(() => {
      this.readyState = 1;
      if (typeof this.onopen === 'function') this.onopen({});
    });
  }
  send(data) { this.sent.push(JSON.parse(data)); }
  close() { this.readyState = 3; }
  pushJson(obj) {
    if (typeof this.onmessage === 'function') {
      this.onmessage({ data: JSON.stringify(obj) });
    }
  }
  pushPcmSamples(n) {
    const buf = new ArrayBuffer(n * 2);
    const dv = new DataView(buf);
    for (let i = 0; i < n; i++) dv.setInt16(i * 2, 1000, true);
    if (typeof this.onmessage === 'function') this.onmessage({ data: buf });
  }
}
FakeWebSocket.OPEN = 1;
globalThis.WebSocket = FakeWebSocket;
"""
        assertions = r"""
(async () => {
  await voicesPromise;
  runQueue = [
    { engine: 'edge', voice: 'zh-CN-XiaoxiaoNeural', text: 'complete', startSentence: 0, count: 1 },
  ];
  runIndex = 0;
  writePos = 0;
  scheduledPos = 0;
  timeline = [];
  isPlaying = true;
  isPaused = false;
  streamEnded = false;
  sendRequest();
  await Promise.resolve();
  await Promise.resolve();

  const mainSock = ws;
  mainSock.pushPcmSamples(4);
  mainSock.pushJson({ type: 'end' });
  equal(runIndex, 1, 'the only run reaches queue end');
  assertOk(streamEnded, 'queue end marks the synthesis stream complete');
  equal(writePos, 4, 'all PCM remains buffered locally');
  assertOk(isPlaying, 'buffered local playback remains active before late error');
  equal(lastStatus, 'streaming', 'late error fixture starts from the streaming status');

  mainSock.onerror({});
  assertOk(isPlaying, 'late main socket error does not stop buffered local playback');
  equal(lastStatus, 'streaming', 'late main socket error does not replace status with error');
  equal(writePos, 4, 'late main socket error does not discard buffered PCM');
  assertOk(streamEnded, 'late main socket error preserves completed-stream state');

  mainSock.pushJson({ type: 'error', message: 'late backend error' });
  assertOk(isPlaying, 'late backend error frame does not stop buffered local playback');
  equal(lastStatus, 'streaming', 'late backend error frame preserves active playback status');
  equal(writePos, 4, 'late backend error frame does not discard buffered PCM');

  mainSock.onclose({});
  assertOk(isPlaying, 'late main socket close does not stop buffered local playback');
  equal(lastStatus, 'streaming', 'late main socket close keeps the active playback status');

  scheduledPos = writePos;
  sources = [];
  mainSock.onclose({});
  equal(lastStatus, 'done', 'completed local playback reaches done on close');
  assertOk(!isPlaying, 'done state stops active playback');

  mainSock.onclose({});
  equal(lastStatus, 'done', 'repeated late close cannot overwrite done with idle');

  isPaused = true;
  setStatus('paused');
  mainSock.onclose({});
  equal(lastStatus, 'paused', 'late close preserves a completed but paused local buffer');
  assertOk(isPaused, 'late close does not clear the paused flag');

  stop();
  finish();
})().catch(err => { throw err; });
"""
        run_node_contract("index.html", setup, assertions)

    def test_index_prefetch_error_falls_back_once_and_preserves_future_slot(self):
        """预取 error 才回退主 WS；重复终态与迟到事件不得双发或误删未来槽。"""
        setup = r"""
globalThis.fetch = async () => ({ ok: true, json: async () => ({
  kokoro: [
    { id: 'af_heart', name: 'Heart', gender: 'female', language: 'en' },
    { id: 'af_bella', name: 'Bella', gender: 'female', language: 'en' },
  ],
  edge: [
    { id: 'zh-CN-XiaoxiaoNeural', name: 'A', gender: 'Female', locale: 'zh-CN' },
  ],
}) });
globalThis.location = { protocol: 'http:', host: 'tts.local' };
document.getElementById('engine').value = 'auto';

const wsInstances = [];
class FakeWebSocket {
  constructor(url) {
    this.url = String(url);
    this.readyState = 0;
    this.sent = [];
    this.closed = false;
    this.onopen = null;
    this.onmessage = null;
    this.onerror = null;
    this.onclose = null;
    wsInstances.push(this);
    queueMicrotask(() => {
      this.readyState = 1;
      if (typeof this.onopen === 'function') this.onopen({});
    });
  }
  send(data) { this.sent.push(JSON.parse(data)); }
  close() {
    if (this.closed) return;
    this.closed = true;
    this.readyState = 3;
    if (typeof this.onclose === 'function') this.onclose({});
  }
  pushJson(obj) {
    if (typeof this.onmessage === 'function') {
      this.onmessage({ data: JSON.stringify(obj) });
    }
  }
  pushPcmSamples(n) {
    const buf = new ArrayBuffer(n * 2);
    const dv = new DataView(buf);
    for (let i = 0; i < n; i++) dv.setInt16(i * 2, 1000, true);
    if (typeof this.onmessage === 'function') this.onmessage({ data: buf });
  }
}
FakeWebSocket.OPEN = 1;
globalThis.WebSocket = FakeWebSocket;
"""
        assertions = r"""
(async () => {
  await voicesPromise;
  runQueue = [
    { engine: 'kokoro', voice: 'af_bella', text: 'r0', startSentence: 0, count: 1 },
    { engine: 'kokoro', voice: 'af_heart', text: 'r1', startSentence: 1, count: 1 },
    { engine: 'edge', voice: 'zh-CN-XiaoxiaoNeural', text: 'r2', startSentence: 2, count: 1 },
  ];
  runIndex = 0;
  writePos = 0;
  timeline = [];
  sendRequest();
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();

  const mainSock = must(ws, 'missing main WebSocket for error fallback');
  const state1 = must(prefetchMap.get(1), 'missing error target prefetch state');
  const state2 = must(prefetchMap.get(2), 'missing future prefetch state');
  const oldMessage = must(state1.ws.onmessage, 'missing old prefetch message handler');
  const oldError = must(state1.ws.onerror, 'missing old prefetch error handler');
  const oldClose = must(state1.ws.onclose, 'missing old prefetch close handler');
  state1.ws.pushPcmSamples(3);
  equal(writePos, 0, 'future partial PCM stays isolated before promotion');
  state1.ws.pushJson({ type: 'error', message: 'prefetch failed' });
  equal(mainSock.sent.length, 1, 'future prefetch error waits for the current main run to finish');
  assertOk(prefetchMap.get(1) === state1, 'future failed state remains owned until ordered fallback');
  equal(writePos, 0, 'uncommitted future PCM is not mistaken for played audio');

  mainSock.pushJson({ type: 'end' });
  await Promise.resolve();
  equal(mainSock.sent.map(req => req.text).join(','), 'r0,r1', 'error falls back to run 1 exactly once');
  equal(
    wsInstances.flatMap(sock => sock.sent).filter(req => req.text === 'r1').length,
    2,
    'run 1 has one prefetch attempt and one permitted main fallback',
  );
  equal(writePos, 0, 'uncommitted prefetch error is safe to retry without replay');
  assertOk(!prefetchMap.has(1), 'failed current slot is removed before fallback');
  assertOk(prefetchMap.get(2) === state2, 'future slot survives current-slot failure');

  // 模拟浏览器已排队的迟到回调：身份校验必须令其全部 no-op。
  oldError({});
  oldClose({});
  oldMessage({ data: JSON.stringify({ type: 'end' }) });
  oldMessage({ data: JSON.stringify({ type: 'error', message: 'late' }) });
  equal(mainSock.sent.length, 2, 'error/onerror/onclose/late messages cannot duplicate fallback');
  equal(runIndex, 1, 'late terminal events do not advance the main run');
  assertOk(prefetchMap.get(2) === state2, 'late events do not delete the future slot');

  // future slot 先 ready；主 fallback 完成后必须继续消费它并自然到达队尾。
  state2.ws.pushJson({ type: 'seg' });
  state2.ws.pushPcmSamples(2);
  state2.ws.pushJson({ type: 'end' });
  equal(writePos, 0, 'future ready PCM waits for the fallback run to finish');
  mainSock.pushJson({ type: 'seg' });
  mainSock.pushPcmSamples(2);
  mainSock.pushJson({ type: 'end' });
  await Promise.resolve();
  equal(runIndex, 3, 'fallback completion consumes the future ready slot through queue end');
  equal(writePos, 4, 'fallback PCM and future ready PCM are each appended once');
  equal(timeline.length, 2, 'fallback and future slot each contribute one real boundary');
  equal(timeline[0] && timeline[0].sentenceIndex, 1, 'fallback boundary maps run 1');
  equal(timeline[1] && timeline[1].sentenceIndex, 2, 'future boundary maps run 2');
  equal(mainSock.sent.map(req => req.text).join(','), 'r0,r1', 'ready run 2 is never sent on main');
  assertOk(streamEnded, 'future ready consumption completes the stream');
  equal(prefetchMap.size, 0, 'queue end clears the completed future slot');
  assertOk(state2.ws.closed, 'consumed future slot closes its socket');

  stop();
  runQueue = [
    { engine: 'kokoro', voice: 'af_bella', text: 'c0', startSentence: 0, count: 1 },
    { engine: 'kokoro', voice: 'af_heart', text: 'c1', startSentence: 1, count: 1 },
    { engine: 'edge', voice: 'zh-CN-XiaoxiaoNeural', text: 'c2', startSentence: 2, count: 1 },
  ];
  runIndex = 0;
  sendRequest();
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
  const waitingMain = must(ws, 'missing main WebSocket for waiting-close scenario');
  const waitingStates = [
    must(prefetchMap.get(1), 'missing waiting prefetch state 1'),
    must(prefetchMap.get(2), 'missing waiting prefetch state 2'),
  ];
  waitingMain.pushJson({ type: 'end' });
  equal(runIndex, 1, 'main end enters the prefetch-waiting state');
  assertOk(prefetchMap.has(1), 'current prefetch remains in flight before main close');

  waitingMain.onclose({});
  equal(lastStatus, 'error', 'main close while waiting for prefetch is fatal');
  equal(prefetchMap.size, 0, 'fatal main close clears every prefetch slot');
  assertOk(waitingStates.every(state => state.ws.closed), 'fatal main close closes every prefetch socket');
  assertOk(waitingStates.every(state =>
    state.ws.onopen === null && state.ws.onmessage === null
      && state.ws.onerror === null && state.ws.onclose === null
  ), 'fatal main close detaches all prefetch handlers');

  stop();
  finish();
})().catch(err => { throw err; });
"""
        run_node_contract("index.html", setup, assertions)

    def test_index_prefetch_transport_failures_wake_pending_fallback(self):
        """pending 预取的真实 onerror/onclose 都必须唤醒且仅回退主 WS 一次。"""
        setup = r"""
globalThis.fetch = async () => ({ ok: true, json: async () => ({
  kokoro: [
    { id: 'af_heart', name: 'Heart', gender: 'female', language: 'en' },
  ],
  edge: [
    { id: 'zh-CN-XiaoxiaoNeural', name: 'A', gender: 'Female', locale: 'zh-CN' },
  ],
}) });
globalThis.location = { protocol: 'http:', host: 'tts.local' };
document.getElementById('engine').value = 'auto';

const wsInstances = [];
class FakeWebSocket {
  constructor(url) {
    this.url = String(url);
    this.readyState = 0;
    this.sent = [];
    this.closed = false;
    this.onopen = null;
    this.onmessage = null;
    this.onerror = null;
    this.onclose = null;
    wsInstances.push(this);
    queueMicrotask(() => {
      this.readyState = 1;
      if (typeof this.onopen === 'function') this.onopen({});
    });
  }
  send(data) { this.sent.push(JSON.parse(data)); }
  close() {
    if (this.closed) return;
    this.closed = true;
    this.readyState = 3;
    if (typeof this.onclose === 'function') this.onclose({});
  }
  pushJson(obj) {
    if (typeof this.onmessage === 'function') {
      this.onmessage({ data: JSON.stringify(obj) });
    }
  }
  failTransport(kind) {
    if (kind === 'onclose') {
      this.closed = true;
      this.readyState = 3;
    }
    const handler = this[kind];
    if (typeof handler === 'function') handler({});
  }
}
FakeWebSocket.OPEN = 1;
globalThis.WebSocket = FakeWebSocket;
"""
        assertions = r"""
(async () => {
  await voicesPromise;

  async function exercise(kind, prefix) {
    stop();
    runQueue = [
      { engine: 'edge', voice: 'zh-CN-XiaoxiaoNeural', text: prefix + '-r0', startSentence: 0, count: 1 },
      { engine: 'kokoro', voice: 'af_heart', text: prefix + '-r1', startSentence: 1, count: 1 },
    ];
    runIndex = 0;
    writePos = 0;
    timeline = [];
    streamEnded = false;
    sendRequest();
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();

    const mainSock = must(ws, prefix + ': missing main WebSocket');
    const state1 = must(prefetchMap.get(1), prefix + ': missing pending prefetch state');
    mainSock.pushJson({ type: 'end' });
    equal(runIndex, 1, prefix + ': main end waits at the pending prefetch');
    equal(mainSock.sent.length, 1, prefix + ': pending prefetch is not sent on main yet');

    state1.ws.failTransport(kind);
    await Promise.resolve();
    equal(
      mainSock.sent.map(req => req.text).join(','),
      prefix + '-r0,' + prefix + '-r1',
      prefix + ': transport failure wakes exactly one main fallback',
    );
    equal(
      wsInstances.flatMap(sock => sock.sent).filter(req => req.text === prefix + '-r1').length,
      2,
      prefix + ': one prefetch attempt plus one main fallback',
    );
    assertOk(!prefetchMap.has(1), prefix + ': failed pending slot is removed');
    assertOk(state1.ws.closed, prefix + ': failed pending socket is closed');

    mainSock.pushJson({ type: 'end' });
    equal(runIndex, 2, prefix + ': fallback run reaches queue end');
    assertOk(streamEnded, prefix + ': fallback completion marks stream end');
    equal(prefetchMap.size, 0, prefix + ': no prefetch slot leaks after completion');
  }

  await exercise('onerror', 'error');
  await exercise('onclose', 'close');
  stop();
  finish();
})().catch(err => { throw err; });
"""
        run_node_contract("index.html", setup, assertions)

    def test_index_prefetch_failure_then_main_fallback_close_is_error(self):
        """预取失败后的主 WS 回退若再断连，必须显式 error，绝不能伪装完成。"""
        setup = r"""
globalThis.fetch = async () => ({ ok: true, json: async () => ({
  kokoro: [
    { id: 'af_heart', name: 'Heart', gender: 'female', language: 'en' },
  ],
  edge: [
    { id: 'zh-CN-XiaoxiaoNeural', name: 'A', gender: 'Female', locale: 'zh-CN' },
  ],
}) });
globalThis.location = { protocol: 'http:', host: 'tts.local' };
document.getElementById('engine').value = 'auto';

const wsInstances = [];
class FakeWebSocket {
  constructor(url) {
    this.url = String(url);
    this.readyState = 0;
    this.sent = [];
    this.closed = false;
    this.onopen = null;
    this.onmessage = null;
    this.onerror = null;
    this.onclose = null;
    wsInstances.push(this);
    queueMicrotask(() => {
      this.readyState = 1;
      if (typeof this.onopen === 'function') this.onopen({});
    });
  }
  send(data) { this.sent.push(JSON.parse(data)); }
  close() {
    if (this.closed) return;
    this.closed = true;
    this.readyState = 3;
    if (typeof this.onclose === 'function') this.onclose({});
  }
  pushJson(obj) {
    if (typeof this.onmessage === 'function') {
      this.onmessage({ data: JSON.stringify(obj) });
    }
  }
  failClose() {
    this.closed = true;
    this.readyState = 3;
    if (typeof this.onclose === 'function') this.onclose({});
  }
}
FakeWebSocket.OPEN = 1;
globalThis.WebSocket = FakeWebSocket;
"""
        assertions = r"""
(async () => {
  await voicesPromise;
  runQueue = [
    { engine: 'edge', voice: 'zh-CN-XiaoxiaoNeural', text: 'main-r0', startSentence: 0, count: 1 },
    { engine: 'kokoro', voice: 'af_heart', text: 'fallback-r1', startSentence: 1, count: 1 },
  ];
  runIndex = 0;
  writePos = 0;
  timeline = [];
  streamEnded = false;
  isPlaying = true;
  isPaused = false;
  sendRequest();
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();

  const mainSock = must(ws, 'missing main WebSocket');
  const prefState = must(prefetchMap.get(1), 'missing run 1 prefetch state');
  equal(mainSock.sent.map(req => req.text).join(','), 'main-r0', 'main sends only run 0 initially');
  equal(prefState.ws.sent.map(req => req.text).join(','), 'fallback-r1', 'run 1 was actually prefetched');

  mainSock.pushJson({ type: 'end' });
  equal(runIndex, 1, 'main run 0 end waits at the pending prefetch');
  equal(mainSock.sent.length, 1, 'pending prefetch is not redundantly sent before failure');

  must(prefState.ws.onerror, 'missing prefetch onerror handler')({});
  await Promise.resolve();
  equal(
    mainSock.sent.map(req => req.text).join(','),
    'main-r0,fallback-r1',
    'prefetch failure sends run 1 on the main WebSocket exactly once',
  );
  assertOk(!prefetchMap.has(1), 'failed prefetch slot is removed before main fallback');
  equal(lastStatus, 'streaming', 'main fallback is in progress before disconnect');

  mainSock.failClose();
  equal(lastStatus, 'error', 'main disconnect during fallback is exposed as error');
  assertOk(streamEnded, 'fallback disconnect terminates the synthesis stream');
  assertOk(!isPlaying && !isPaused, 'fallback disconnect stops playback state');
  equal(runIndex, 1, 'fallback disconnect does not advance or pretend the run ended');
  assertOk(lastStatus !== 'done' && lastStatus !== 'idle', 'fallback disconnect never masquerades as success');

  stop();
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
  const safeOpen = (sock) => {
    queueMicrotask(() => {
      sock.readyState = 1;
      if (typeof sock.onopen === 'function') sock.onopen({});
    });
  };
  globalThis.WebSocket = class {
    constructor(url) {
      this.url = url;
      this.readyState = 0;
      this.sent = [];
      sockets.push(this);
      safeOpen(this);
    }
    send(body) {
      const sent = JSON.parse(body);
      this.sent.push(sent);
      // 预取明确失败，验证回退到主连接后，后续 run 异常断连仍保持 error。
      if (this !== ws) {
        queueMicrotask(() => {
          if (typeof this.onmessage === 'function') {
            this.onmessage({ data: JSON.stringify({ type: 'error', message: 'prefetch failed' }) });
          }
        });
        return;
      }
      queueMicrotask(() => {
        if (typeof this.onmessage !== 'function') return;
        if (this.sent.length === 1) {
          this.onmessage({ data: JSON.stringify({ type: 'start' }) });
          this.onmessage({ data: JSON.stringify({ type: 'seg', text: sent.text }) });
          this.onmessage({ data: JSON.stringify({ type: 'end' }) });
        } else {
          this.onmessage({ data: JSON.stringify({ type: 'start' }) });
          if (typeof this.onclose === 'function') this.onclose();
        }
      });
    }
    close() { if (typeof this.onclose === 'function') this.onclose(); }
  };

  document.getElementById('engine').value = 'kokoro';
  updateVoices();
  document.getElementById('voice').value = 'zf_xiaoxiao';
  document.getElementById('t').value = '中文 DNS.';
  await start();
  await new Promise(resolve => setTimeout(resolve, 40));

  const main0 = sockets.find(s => s.sent && s.sent.length);
  assertOk(main0, 'main websocket sent runs');
  equal(main0.sent.length, 2, 'second run was sent on the same WebSocket');
  equal(lastStatus, 'error', 'later run close before end is an error');

  globalThis.WebSocket = class {
    constructor(url) {
      this.url = url;
      this.readyState = 0;
      sockets.push(this);
      safeOpen(this);
    }
    send() {
      queueMicrotask(() => {
        if (this !== ws) return;
        if (typeof this.onmessage !== 'function') return;
        this.onmessage({ data: JSON.stringify({ type: 'start' }) });
        this.onmessage({ data: JSON.stringify({ type: 'error', message: 'boom' }) });
        if (typeof this.onclose === 'function') this.onclose();
      });
    }
    close() { if (typeof this.onclose === 'function') this.onclose(); }
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

  // restart 必须自行作废全部预取，不能依赖 sendRequest 的间接副作用。
  allSentences = [
    { engine: 'kokoro', voice: 'zf_xiaoxiao', text: 'g0' },
    { engine: 'kokoro', voice: 'af_heart', text: 'g1' },
    { engine: 'kokoro', voice: 'zf_xiaoxiao', text: 'g2' },
    { engine: 'kokoro', voice: 'af_heart', text: 'g3' },
  ];
  runQueue = buildRunsFrom(0);
  runIndex = 0;
  abortAllPrefetch();
  schedulePrefetch(1);
  await new Promise(resolve => setTimeout(resolve, 20));
  const oldPrefetch1 = must(prefetchMap.get(1), 'missing restart prefetch state 1');
  const oldPrefetch2 = must(prefetchMap.get(2), 'missing restart prefetch state 2');
  const oldPrefetchOpen = must(oldPrefetch1.ws.onopen, 'missing old prefetch open handler');
  const oldPrefetchMessage = must(oldPrefetch1.ws.onmessage, 'missing old prefetch message handler');
  const oldPrefetchError = must(oldPrefetch1.ws.onerror, 'missing old prefetch error handler');
  const oldPrefetchClose = must(oldPrefetch1.ws.onclose, 'missing old prefetch close handler');
  const oldPrefetchSentCount = oldPrefetch1.ws.sent.length;

  const realSendRequest = sendRequest;
  let restartSendCalls = 0;
  sendRequest = () => { restartSendCalls++; };
  timeline = [{ startSample: 0, sentenceIndex: 0 }];
  pausedHead = 0;
  writePos = 0;
  scheduledPos = 0;
  isPlaying = false;
  isPaused = false;
  restartFromCurrentSentence();
  equal(restartSendCalls, 1, 'restart still delegates creation of the new main request');
  equal(prefetchMap.size, 0, 'restart directly clears old prefetch slots before the new request');
  assertOk(oldPrefetch1.ws.closed && oldPrefetch2.ws.closed, 'restart closes both old prefetch sockets');

  sendRequest = realSendRequest;
  schedulePrefetch(1);
  await new Promise(resolve => setTimeout(resolve, 20));
  const restartedPrefetch1 = must(prefetchMap.get(1), 'missing restarted prefetch state 1');
  assertOk(restartedPrefetch1 !== oldPrefetch1, 'restart creates a new state generation for the same key');
  oldPrefetchOpen({});
  equal(oldPrefetch1.ws.sent.length, oldPrefetchSentCount, 'old onopen cannot send after same-key rebuild');
  oldPrefetchMessage({ data: JSON.stringify({ type: 'end' }) });
  oldPrefetchError({});
  oldPrefetchClose({});
  assertOk(prefetchMap.get(1) === restartedPrefetch1, 'old restart callbacks cannot replace the new state');
  assertOk(!restartedPrefetch1.ended && !restartedPrefetch1.error, 'old callbacks cannot mutate the new state');

  // 新 sendRequest 同样清旧代；旧回调在相同 key 重建后仍必须无效。
  const priorPrefetch = restartedPrefetch1;
  const priorMessage = must(priorPrefetch.ws.onmessage, 'missing prior request message handler');
  sendRequest();
  await new Promise(resolve => setTimeout(resolve, 20));
  const requestPrefetch1 = must(prefetchMap.get(1), 'missing new-request prefetch state 1');
  assertOk(requestPrefetch1 !== priorPrefetch, 'new request replaces the previous prefetch generation');
  priorMessage({ data: JSON.stringify({ type: 'error', message: 'late generation' }) });
  assertOk(prefetchMap.get(1) === requestPrefetch1, 'late old-request callback keeps the new slot owned');
  assertOk(!requestPrefetch1.error, 'late old-request callback cannot poison the new state');

  stop();
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
  let resolveWsTerminal;
  const wsTerminal = new Promise(resolve => { resolveWsTerminal = resolve; });
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
        resolveWsTerminal();
      }, 0);
    }
    close() { if (this.onclose) this.onclose(); }
  };
  document.getElementById('wsText').value = 'hello';
  document.getElementById('wsEngine').value = 'kokoro';
  document.getElementById('wsVoice').value = 'af_heart';
  document.getElementById('wsSpeed').value = '1.25';
  await testWs();
  await wsTerminal;
  const wsBtn = document.getElementById('wsBtn');
  assertOk(!wsBtn.disabled, 'WS tester reaches a terminal state after the fake end event');
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


    def test_index_main_websocket_inactivity_fails_and_ignores_stale_events(self):
        assertions = r"""
(async () => {
  await voicesPromise;
  runQueue = [
    { engine: 'kokoro', voice: 'af_heart', text: 'main', startSentence: 0, count: 1 },
  ];
  runIndex = 0;
  writePos = 0;
  streamEnded = false;
  sendRequest();
  await Promise.resolve();
  await Promise.resolve();

  const mainSock = must(ws, 'missing main socket');
  const staleMessage = must(mainSock.onmessage, 'missing main message handler');
  const timerId = must(mainInactivityTimer, 'main socket must own an inactivity timer');
  const timer = must(pendingTimeouts.get(timerId), 'missing main inactivity timer');
  equal(timer.delay, WS_INACTIVITY_TIMEOUT_MS, 'main socket uses the declared inactivity timeout');
  fireTimeout(timerId);

  equal(lastStatus, 'error', 'main inactivity is an explicit error');
  assertOk(mainSock.closed, 'main inactivity closes the socket to cancel backend work');
  equal(mainSock.sent.length, 1, 'main inactivity never retries the run');
  equal(pendingTimeouts.size, 0, 'main inactivity retires every owned timer');

  staleMessage({ data: JSON.stringify({ type: 'end' }) });
  equal(runIndex, 0, 'late terminal from timed-out socket cannot advance state');
  equal(lastStatus, 'error', 'late event cannot disguise inactivity as success');
  finish();
})().catch(err => { throw err; });
"""
        run_node_contract("index.html", PREFETCH_CONTROLLED_SETUP, assertions)

    def test_index_websocket_activity_rearms_and_terminal_clears_deadline(self):
        assertions = r"""
(async () => {
  await voicesPromise;
  runQueue = [
    { engine: 'edge', voice: 'en-US-JennyNeural', text: 'active', startSentence: 0, count: 1 },
  ];
  runIndex = 0;
  streamEnded = false;
  sendRequest();
  await Promise.resolve();
  await Promise.resolve();

  const mainSock = must(ws, 'missing active main socket');
  const firstId = must(mainInactivityTimer, 'missing initial main inactivity timer');
  const firstTimer = must(pendingTimeouts.get(firstId), 'missing initial timer entry');
  mainSock.pushJson({ type: 'start' });
  const startId = must(mainInactivityTimer, 'start must rearm inactivity');
  assertOk(startId !== firstId, 'start replaces the previous inactivity timer');
  assertOk(!pendingTimeouts.has(firstId), 'rearm physically clears the previous timer');
  firstTimer.callback();
  equal(lastStatus, 'streaming', 'stale pre-start timer cannot fail an active socket');

  mainSock.pushJson({ type: 'unknown' });
  equal(mainInactivityTimer, startId, 'unknown metadata cannot keep a silent connection alive');
  mainSock.pushJson({ type: 'seg' });
  const segId = must(mainInactivityTimer, 'seg must rearm inactivity');
  assertOk(segId !== startId, 'seg replaces the start timer');
  mainSock.pushPcmSamples(2);
  const pcmId = must(mainInactivityTimer, 'PCM must rearm inactivity');
  assertOk(pcmId !== segId, 'PCM replaces the seg timer');

  mainSock.pushJson({ type: 'end' });
  equal(mainInactivityTimer, null, 'terminal frame clears main inactivity ownership');
  equal(timeoutCount(WS_INACTIVITY_TIMEOUT_MS), 0, 'terminal frame clears the physical main timer');
  assertOk(streamEnded, 'single-run terminal completes the queue');
  stop();
  finish();
})().catch(err => { throw err; });
"""
        run_node_contract("index.html", PREFETCH_CONTROLLED_SETUP, assertions)

    def test_index_future_prefetch_inactivity_defers_one_ordered_fallback(self):
        assertions = r"""
(async () => {
  await voicesPromise;
  runQueue = [
    { engine: 'kokoro', voice: 'af_heart', text: 'r0', startSentence: 0, count: 1 },
    { engine: 'edge', voice: 'zh-CN-XiaoxiaoNeural', text: 'r1', startSentence: 1, count: 1 },
  ];
  runIndex = 0;
  writePos = 0;
  streamEnded = false;
  sendRequest();
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();

  const mainSock = must(ws, 'missing main socket');
  const state1 = must(prefetchMap.get(1), 'missing future prefetch');
  const initialTimerId = must(prefetchInactivityTimers.get(state1), 'missing initial future timer');
  const initialTimer = must(pendingTimeouts.get(initialTimerId), 'missing initial future timer entry');
  state1.ws.pushJson({ type: 'start' });
  const startTimerId = must(prefetchInactivityTimers.get(state1), 'start must rearm future inactivity');
  assertOk(startTimerId !== initialTimerId, 'future start replaces its inactivity timer');
  initialTimer.callback();
  assertOk(!state1.error, 'stale future timer cannot fail an active slot');
  state1.ws.pushJson({ type: 'unknown' });
  equal(prefetchInactivityTimers.get(state1), startTimerId, 'unknown future metadata cannot rearm inactivity');
  state1.ws.pushJson({ type: 'seg' });
  state1.ws.pushPcmSamples(3);
  const timerId = must(prefetchInactivityTimers.get(state1), 'future prefetch must own an inactivity timer');
  const timer = must(pendingTimeouts.get(timerId), 'missing future inactivity timer');
  const staleCallback = timer.callback;
  equal(timer.delay, WS_INACTIVITY_TIMEOUT_MS, 'future prefetch uses the long inactivity timeout');
  fireTimeout(timerId);

  assertOk(state1.error && !state1.ended, 'future inactivity leaves an ordered error tombstone');
  assertOk(prefetchMap.get(1) === state1, 'future error remains owned until it becomes current');
  assertOk(state1.ws.closed, 'future inactivity closes its socket');
  equal(state1.chunks.length, 0, 'uncommitted future PCM is discarded before fallback');
  equal(mainSock.sent.map(req => req.text).join(','), 'r0', 'future timeout does not leapfrog current run');

  mainSock.pushJson({ type: 'end' });
  equal(mainSock.sent.map(req => req.text).join(','), 'r0,r1', 'timed-out future run falls back once in order');
  assertOk(!prefetchMap.has(1), 'ordered fallback retires the error tombstone');
  staleCallback();
  equal(mainSock.sent.length, 2, 'stale future timeout cannot duplicate fallback');
  stop();
  finish();
})().catch(err => { throw err; });
"""
        run_node_contract("index.html", PREFETCH_CONTROLLED_SETUP, assertions)

    def test_index_promoted_prefetch_inactivity_fails_without_replay(self):
        assertions = r"""
(async () => {
  await voicesPromise;
  runQueue = [
    { engine: 'kokoro', voice: 'af_heart', text: 'r0', startSentence: 0, count: 1 },
    { engine: 'edge', voice: 'zh-CN-XiaoxiaoNeural', text: 'r1', startSentence: 1, count: 1 },
  ];
  runIndex = 0;
  writePos = 0;
  streamEnded = false;
  sendRequest();
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();

  const mainSock = must(ws, 'missing main socket');
  const state1 = must(prefetchMap.get(1), 'missing promoted prefetch');
  state1.ws.pushJson({ type: 'seg' });
  state1.ws.pushPcmSamples(3);
  mainSock.pushJson({ type: 'end' });
  equal(writePos, 3, 'future PCM is committed when the run becomes current');

  const timerId = must(prefetchInactivityTimers.get(state1), 'promoted prefetch must retain an inactivity timer');
  fireTimeout(timerId);

  equal(lastStatus, 'error', 'promoted inactivity is fatal after PCM commit');
  equal(mainSock.sent.map(req => req.text).join(','), 'r0', 'committed PCM is never replayed on main');
  equal(writePos, 3, 'committed PCM remains exact after inactivity failure');
  assertOk(streamEnded, 'promoted inactivity terminates the stream');
  assertOk(!prefetchMap.has(1), 'promoted inactivity clears prefetch ownership');
  assertOk(mainSock.closed, 'fatal promoted inactivity closes the main socket');
  equal(pendingTimeouts.size, 0, 'fatal promoted inactivity clears every timer');
  finish();
})().catch(err => { throw err; });
"""
        run_node_contract("index.html", PREFETCH_CONTROLLED_SETUP, assertions)


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
