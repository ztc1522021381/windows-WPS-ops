#!/usr/bin/env node
/**
 * edge_cdp.js —— 用 Edge + CDP 做网页自动化，零第三方依赖。
 *
 * 为什么需要它
 * ------------
 * 本机既没有 agent-browser，也没有 playwright / puppeteer，Python 侧同样没有
 * websocket 库（pip 走镜像可能不可达）。但：
 *   · Node 22 起内置全局 WebSocket（无需 npm install）
 *   · Edge 自带 CDP（--remote-debugging-port）
 * 两者组合即可完成导航、执行 JS、等待条件、截图，足以覆盖大部分网页自动化需求。
 *
 * 用法
 * ----
 *   # 自动启动 Edge（headless）→ 导航 → 等待 → 截图
 *   node edge_cdp.js --launch --url https://example.com --shot out.png
 *
 *   # 附加：等待页面出现某段文字后再截图（适合 AI 生成类页面）
 *   node edge_cdp.js --launch --url https://gitdiagram.com/o/r --wait "connections" --shot d.png
 *
 *   # 复用已启动的实例执行 JS
 *   node edge_cdp.js --eval "document.title"
 *
 *   # 抓取页面可点击元素，便于确定选择器
 *   node edge_cdp.js --probe
 *
 * 参数
 * ----
 *   --url <u>        导航到该地址
 *   --shot <path>    截图（captureBeyondViewport，含视口外内容）
 *   --eval <js>      执行 JS 并打印返回值
 *   --probe          打印页面标题、按钮、输入框、可见文字（排查选择器用）
 *   --click <text>   点击文字包含该串的按钮/链接（在 --wait 之前执行）。
 *                    用于「页面已有缓存内容，需先点『重新生成』再等新结果」的场景
 *   --wait <text>    轮询等待 document.body.innerText 含该文本
 *   --timeout <s>    等待超时秒数，默认 120
 *   --width/--height 视口尺寸，默认 1680x1500
 *   --port <n>       CDP 端口，默认 9222
 *   --launch         若实例未运行则自动启动 Edge
 *   --headed         有头模式启动（默认 headless=new）
 *   --proxy <url>    显式指定代理（如 http://127.0.0.1:2970）。不指定时自动采用
 *                    环境变量 http_proxy / HTTPS_PROXY；Edge 默认走**系统代理**，
 *                    本机系统代理指向不可用的 `[::1]:12334`，会导致 ERR_TIMED_OUT
 *   --no-proxy       完全不使用代理
 *   --close          执行完关闭浏览器
 *
 * 注意
 * ----
 * · 启动时务必使用独立的 --user-data-dir，避免干扰用户正在使用的浏览器。
 * · 页面若由 AI 后端流式生成（如 GitDiagram），要等到出现确定性文字再截图，
 *   否则截到的是 "Waiting for an update" 这类中间态。
 */

const http = require('http');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn } = require('child_process');

const argv = process.argv.slice(2);
const arg = (name, dflt) => {
  const i = argv.indexOf(name);
  return i >= 0 && argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[i + 1] : dflt;
};
const flag = (name) => argv.includes(name);

const PORT = parseInt(arg('--port', '9222'), 10);
const WIDTH = parseInt(arg('--width', '1680'), 10);
const HEIGHT = parseInt(arg('--height', '1500'), 10);
const TIMEOUT = parseInt(arg('--timeout', '120'), 10);
// 代理：Edge 启动时默认采用**系统代理**，而本机的系统代理是注册表里那个
// `http://[::1]:12334`（IPv6 回环），实测浏览器走它连不上外网（ERR_TIMED_OUT），
// 但命令行 curl 走环境变量代理却通畅——两者不一致就是这个坑的根源。
// 因此优先用 --proxy 显式指定，否则自动采用环境变量里的代理；--no-proxy 可关闭。
const PROXY = flag('--no-proxy')
  ? ''
  : (arg('--proxy')
     || process.env.http_proxy || process.env.HTTP_PROXY
     || process.env.https_proxy || process.env.HTTPS_PROXY
     || '');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function httpGet(url, timeoutMs = 3000) {
  return new Promise((resolve, reject) => {
    const req = http.get(url, (r) => {
      let d = '';
      r.on('data', (c) => (d += c));
      r.on('end', () => resolve(d));
    });
    req.setTimeout(timeoutMs, () => req.destroy(new Error('timeout')));
    req.on('error', reject);
  });
}

async function cdpAlive() {
  try {
    await httpGet(`http://127.0.0.1:${PORT}/json/version`, 2000);
    return true;
  } catch {
    return false;
  }
}

function findEdge() {
  const cands = [
    process.env['PROGRAMFILES(X86)'] && path.join(process.env['PROGRAMFILES(X86)'], 'Microsoft/Edge/Application/msedge.exe'),
    process.env.PROGRAMFILES && path.join(process.env.PROGRAMFILES, 'Microsoft/Edge/Application/msedge.exe'),
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
  ].filter(Boolean);
  for (const c of cands) if (fs.existsSync(c)) return c;
  throw new Error('未找到 msedge.exe，请用 --port 指向已有实例或改用 Chrome');
}

async function launch(initialUrl) {
  const exe = findEdge();
  const profile = path.join(os.tmpdir(), 'edge-cdp-' + PORT);
  const args = [
    flag('--headed') ? '--new-window' : '--headless=new',
    '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${profile}`,
    `--window-size=${WIDTH},${HEIGHT}`,
  ];
  if (PROXY) args.push(`--proxy-server=${PROXY}`);
  if (initialUrl) args.push(initialUrl);
  console.log('[launch] 代理: ' + (PROXY || '（不使用，走 Edge 系统代理设置）'));
  const child = spawn(exe, args, { detached: true, stdio: 'ignore' });
  child.unref();
  for (let i = 0; i < 30; i++) {
    await sleep(500);
    if (await cdpAlive()) return;
  }
  throw new Error('Edge 启动后 CDP 端口未就绪');
}

async function connect() {
  const raw = await httpGet(`http://127.0.0.1:${PORT}/json`);
  const targets = JSON.parse(raw);
  const page = targets.find((t) => t.type === 'page' && !t.url.startsWith('chrome-extension://'));
  if (!page) throw new Error('未找到可用的 page target');
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();
  ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  });
  await new Promise((res, rej) => {
    ws.addEventListener('open', res);
    ws.addEventListener('error', rej);
  });
  const send = (method, params) => new Promise((res) => {
    const _id = ++id;
    pending.set(_id, res);
    ws.send(JSON.stringify({ id: _id, method, params: params || {} }));
  });
  const evalJS = async (expression) => {
    const r = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
    if (r.result && r.result.exceptionDetails) return { __error: r.result.exceptionDetails.text };
    return r.result && r.result.result ? r.result.result.value : undefined;
  };
  return { ws, send, evalJS, port: PORT };
}

(async () => {
  const url = arg('--url');
  const shot = arg('--shot');
  const waitText = arg('--wait');
  const evalExpr = arg('--eval');
  const clickText = arg('--click');

  if (!(await cdpAlive())) {
    if (flag('--launch')) {
      console.log('[launch] 启动 Edge（headless）...');
      await launch(url);
      console.log('[launch] CDP 就绪于端口 ' + PORT);
    } else {
      throw new Error(`端口 ${PORT} 上没有运行中的浏览器，加 --launch 自动启动`);
    }
  }

  const c = await connect();
  await c.send('Page.enable');
  await c.send('Runtime.enable');
  await c.send('Emulation.setDeviceMetricsOverride', {
    width: WIDTH, height: HEIGHT, deviceScaleFactor: 1, mobile: false,
  });

  if (url) {
    console.log('[nav] ' + url);
    await c.send('Page.navigate', { url });
    await sleep(4000);
  }

  if (!flag('--no-wait-ready')) {
    for (let i = 0; i < 40; i++) {
      const s = await c.evalJS('document.readyState');
      if (s === 'complete') break;
      await sleep(500);
    }
  }

  // 点击：在等待之前先点一次按钮（典型场景——页面上已有缓存内容，
  // 需要点「重新生成」触发新结果，随后再用 --wait 等新结果出现）。
  if (clickText) {
    const r = await c.evalJS(`(() => {
      const t = ${JSON.stringify(clickText)};
      const els = [...document.querySelectorAll('button,a,[role="button"]')];
      const el = els.find((e) => (e.textContent || '').includes(t));
      if (!el) return 'NOT_FOUND';
      el.click();
      return 'CLICKED: ' + (el.textContent || '').trim().slice(0, 40);
    })()`);
    console.log('[click] ' + r);
    await sleep(2500);
  }

  if (waitText) {
    let hit = false;
    const deadline = Date.now() + TIMEOUT * 1000;
    while (Date.now() < deadline) {
      const t = await c.evalJS('document.body.innerText');
      if (typeof t === 'string' && t.includes(waitText)) { hit = true; break; }
      await sleep(2000);
    }
    console.log(hit ? `[wait] 已出现目标文本: ${waitText}` : `[wait] 超时未出现: ${waitText}`);
    await sleep(2500);
  }

  if (flag('--probe')) {
    const p = await c.evalJS(`JSON.stringify({
      url: location.href, title: document.title, ready: document.readyState,
      buttons: [...document.querySelectorAll('button')].map((b,i)=>({i,
        text:(b.textContent||'').trim().slice(0,50), aria:b.getAttribute('aria-label'),
        cls:String(b.className||'').slice(0,70), disabled:!!b.disabled})),
      inputs: [...document.querySelectorAll('input,textarea')].map(e=>({tag:e.tagName,
        value:String(e.value||'').slice(0,60), ph:e.placeholder})),
      bodyText: document.body.innerText.slice(0,800)
    })`);
    console.log('=== 页面探测 ===');
    console.log(typeof p === 'string' ? JSON.stringify(JSON.parse(p), null, 2) : p);
  }

  if (evalExpr) {
    const r = await c.evalJS(evalExpr);
    console.log('=== eval 结果 ===');
    console.log(typeof r === 'string' ? r : JSON.stringify(r, null, 2));
  }

  if (shot) {
    const dims = await c.evalJS('JSON.stringify({w:document.documentElement.scrollWidth,h:document.documentElement.scrollHeight})');
    console.log('[shot] 页面尺寸 ' + dims);
    const r = await c.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true });
    if (r.result && r.result.data) {
      fs.writeFileSync(shot, Buffer.from(r.result.data, 'base64'));
      console.log('[shot] 已保存 ' + shot + ' (' + fs.statSync(shot).size + ' bytes)');
    } else {
      console.log('[shot] 失败: ' + JSON.stringify(r).slice(0, 400));
    }
  }

  c.ws.close();

  if (flag('--close')) {
    try {
      const v = JSON.parse(await httpGet(`http://127.0.0.1:${PORT}/json/version`));
      const bws = new WebSocket(v.webSocketDebuggerUrl);
      await new Promise((r) => bws.addEventListener('open', r));
      bws.send(JSON.stringify({ id: 1, method: 'Browser.close' }));
      console.log('[close] 已请求关闭浏览器');
      await sleep(1500);
    } catch (e) {
      console.log('[close] 关闭失败: ' + e.message);
    }
  }
})().catch((e) => {
  console.error('ERROR: ' + e.message);
  process.exit(1);
});
