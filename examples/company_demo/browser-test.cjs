'use strict';

// This launches a new, isolated Chromium against our generated local demo.
// It never attaches to a user's browser, profile, or authenticated session.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');

const outputIndex = process.argv.indexOf('--output');
const output = path.resolve(outputIndex < 0 ? path.join(__dirname, 'verification', 'browser')
  : process.argv[outputIndex + 1] || 'verification/browser');
fs.mkdirSync(output, { recursive: true });
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'cloudkit-browser-'));
const report = {
  schema_version: 1,
  timestamp_utc: new Date().toISOString(),
  passed: false,
  browser_launched: false,
  browser_execution_tested: false,
  model_execution_tested: false,
  prod_deployed: false,
  scope: 'Real isolated Chromium UI interactions with local Python HTTP and SQLite backend.',
  tests: [],
  limitations: ['Local demo only; no real model calls or production deployment.',
    'The failed POST is explicitly injected by browser request interception.',
    'Passing these scenarios is not a comprehensive security or accessibility audit.'],
};
let server;
let browser;
let page;
let logs = '';

async function check(name, action) {
  try {
    const evidence = await action();
    report.tests.push({ name, passed: true, evidence: evidence || {} });
  } catch (error) {
    report.tests.push({ name, passed: false, evidence: { error: error.message } });
    throw error;
  }
}

function startServer() {
  return new Promise((resolve, reject) => {
    server = spawn(process.env.PYTHON || 'python3', ['-u', path.join(__dirname, 'app.py'),
      '--host', '127.0.0.1', '--port', '0', '--db', path.join(temporary, 'tasks.sqlite3')],
    { cwd: __dirname, stdio: ['ignore', 'pipe', 'pipe'] });
    let pending = '';
    const timer = setTimeout(() => reject(new Error('Server startup timeout')), 10000);
    server.on('error', error => { clearTimeout(timer); reject(error); });
    server.on('exit', code => {
      clearTimeout(timer);
      reject(new Error(`Server exited before startup: ${code}; ${logs}`));
    });
    server.stderr.on('data', data => { logs += data.toString(); });
    server.stdout.on('data', data => {
      pending += data.toString();
      logs += data.toString();
      const lines = pending.split('\n');
      pending = lines.pop();
      for (const line of lines) {
        try {
          const address = JSON.parse(line);
          const port = address.port || (address.url && Number(new URL(address.url).port));
          if (Number.isInteger(port) && port > 0 && port < 65536) {
            clearTimeout(timer);
            resolve(`http://127.0.0.1:${port}`);
          }
        } catch { /* Non-JSON server diagnostics are retained in the log. */ }
      }
    });
  });
}

async function stopServer() {
  if (!server || server.exitCode !== null || server.signalCode !== null) return;
  await new Promise(resolve => {
    const timer = setTimeout(() => server.kill('SIGKILL'), 5000);
    server.once('exit', () => { clearTimeout(timer); resolve(); });
    server.kill('SIGTERM');
  });
}

async function waitCount(count) {
  await page.waitForFunction(expected =>
    document.querySelectorAll('#task-list .task-item').length === expected
      && document.querySelector('#tasks-panel').getAttribute('aria-busy') === 'false', count);
}

async function main() {
  try {
    const { chromium } = require('playwright');
    const base = await startServer();
    browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
    report.browser_launched = true;
    report.browser_version = browser.version();
    report.playwright_version = require('playwright/package.json').version;
    page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    page.setDefaultTimeout(10000);
    const dialogs = [];
    const pageErrors = [];
    page.on('dialog', async dialog => { dialogs.push(dialog.message()); await dialog.dismiss(); });
    page.on('pageerror', error => pageErrors.push(error.message));

    await check('initial_empty_ui', async () => {
      await page.goto(base, { waitUntil: 'networkidle' });
      await waitCount(0);
      assert.equal(await page.locator('#empty-state').isVisible(), true);
      assert.equal(await page.locator('#total-count').textContent(), '0');
      return { real_browser: true, empty_state_visible: true };
    });

    const ordinary = 'Browser acceptance task';
    await check('create_task_through_ui', async () => {
      await page.locator('#task-title').fill(ordinary);
      await page.locator('#create-button').click();
      await waitCount(1);
      assert.equal(await page.locator('.task-title').textContent(), ordinary);
      assert.equal(await page.locator('#task-title').inputValue(), '');
      assert.equal(await page.locator('#todo-count').textContent(), '1');
      return { title: ordinary, tasks: 1 };
    });

    await check('toggle_task_through_ui', async () => {
      await page.locator('.task-toggle').click();
      await page.waitForFunction(() => document.querySelector('.task-toggle')
        .getAttribute('aria-pressed') === 'true');
      assert.equal(await page.locator('#done-count').textContent(), '1');
      assert.equal(await page.locator('.status-badge').textContent(), '已完成');
      return { status: 'done', aria_pressed: true };
    });

    await check('reload_preserves_task_and_status', async () => {
      await page.reload({ waitUntil: 'networkidle' });
      await waitCount(1);
      assert.equal(await page.locator('.task-title').textContent(), ordinary);
      assert.equal(await page.locator('.task-toggle').getAttribute('aria-pressed'), 'true');
      return { reloaded: true, title: ordinary, status: 'done' };
    });

    const malicious = '<img src=x onerror="window.__xss=1;alert(\'xss\')">';
    await check('malicious_title_is_visible_literal_text', async () => {
      await page.locator('#task-title').fill(malicious);
      await page.locator('#create-button').click();
      await waitCount(2);
      assert.deepEqual(await page.locator('.task-title').allTextContents(), [ordinary, malicious]);
      assert.equal(await page.locator('#task-list img').count(), 0);
      assert.equal(await page.evaluate(() => typeof window.__xss), 'undefined');
      assert.deepEqual(dialogs, []);
      await page.reload({ waitUntil: 'networkidle' });
      await waitCount(2);
      assert.equal(await page.locator('.task-title').nth(1).textContent(), malicious);
      assert.equal(await page.locator('#task-list img').count(), 0);
      assert.deepEqual(dialogs, []);
      return { literal_title: malicious, injected_images: 0, dialogs: 0, survives_reload: true };
    });

    await check('failed_post_shows_error_without_phantom_task', async () => {
      let intercepted = 0;
      await page.route('**/api/tasks', async route => {
        if (route.request().method() === 'POST') {
          intercepted += 1;
          await route.fulfill({ status: 503, contentType: 'application/json',
            body: JSON.stringify({ error: 'Acceptance injected save failure' }) });
        } else await route.continue();
      });
      await page.locator('#task-title').fill('This task must not exist');
      await page.locator('#create-button').click();
      await page.locator('#error-message').waitFor({ state: 'visible' });
      await waitCount(2);
      assert.match(await page.locator('#error-message').textContent(), /Acceptance injected save failure/);
      assert.equal(await page.locator('#task-title').inputValue(), 'This task must not exist');
      assert.equal(intercepted, 1);
      const response = await page.request.get(base + '/api/tasks');
      assert.equal(response.status(), 200);
      assert.equal((await response.json()).tasks.length, 2);
      await page.unroute('**/api/tasks');
      await page.reload({ waitUntil: 'networkidle' });
      await waitCount(2);
      assert.deepEqual(await page.locator('.task-title').allTextContents(), [ordinary, malicious]);
      return { injected_http_status: 503, ui_tasks: 2, database_tasks: 2,
        visible_error: true, entered_title_preserved: true };
    });

    await check('no_uncaught_javascript_errors', async () => {
      assert.deepEqual(pageErrors, []);
      return { uncaught_errors: pageErrors };
    });
    await page.screenshot({ path: path.join(output, 'desktop.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: path.join(output, 'mobile.png'), fullPage: true });
    report.screenshots = ['desktop.png', 'mobile.png'];
    report.passed = report.tests.length > 0 && report.tests.every(test => test.passed);
    report.browser_execution_tested = report.passed;
  } catch (error) {
    report.error = error.stack || String(error);
    if (page) {
      try { await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }); }
      catch { /* Keep the primary failure. */ }
    }
  } finally {
    try { if (browser) await browser.close(); }
    finally {
      await stopServer();
      fs.writeFileSync(path.join(output, 'server.log'), logs);
      fs.rmSync(temporary, { recursive: true, force: true });
      report.test_count = report.tests.length;
      report.passed_count = report.tests.filter(test => test.passed).length;
      fs.writeFileSync(path.join(output, 'browser-report.json'), JSON.stringify(report, null, 2) + '\n');
      console.log(JSON.stringify({ passed: report.passed, tests: report.test_count,
        passed_count: report.passed_count, output }));
      process.exitCode = report.passed ? 0 : 1;
    }
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
