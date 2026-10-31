'use strict';

const elements = {
  form: document.querySelector('#task-form'),
  title: document.querySelector('#task-title'),
  titleCount: document.querySelector('#title-count'),
  create: document.querySelector('#create-button'),
  refresh: document.querySelector('#refresh-button'),
  panel: document.querySelector('#tasks-panel'),
  list: document.querySelector('#task-list'),
  empty: document.querySelector('#empty-state'),
  loading: document.querySelector('#loading-state'),
  error: document.querySelector('#error-message'),
  status: document.querySelector('#status-message'),
  total: document.querySelector('#total-count'),
  todo: document.querySelector('#todo-count'),
  done: document.querySelector('#done-count'),
};

let tasks = [];
let loading = false;
let writing = false;
let loaded = false;

function announce(message) {
  elements.status.textContent = message;
}

function showError(message = '') {
  elements.error.textContent = message;
  elements.error.hidden = !message;
}

function validTask(task) {
  return task && Number.isInteger(task.id) && typeof task.title === 'string'
    && ['todo', 'done'].includes(task.status);
}

async function request(path, options = {}) {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(path, {
      ...options,
      headers: { 'Content-Type': 'application/json', ...options.headers },
      signal: controller.signal,
    });
    let data;
    try {
      data = await response.json();
    } catch {
      throw new Error('服务器返回了无法识别的内容，请刷新后重试。');
    }
    if (!response.ok) {
      throw new Error(typeof data.error === 'string' ? data.error : `请求失败（${response.status}），请稍后重试。`);
    }
    return data;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('连接超时，请刷新列表确认结果后再试。');
    if (error instanceof TypeError) throw new Error('无法连接服务，请检查网络并刷新列表确认结果。');
    throw error;
  } finally {
    window.clearTimeout(timer);
  }
}

function setControls() {
  elements.create.disabled = loading || writing;
  elements.refresh.disabled = loading || writing;
  elements.title.disabled = writing;
  elements.panel.setAttribute('aria-busy', String(loading || writing));
  for (const button of elements.list.querySelectorAll('button')) button.disabled = loading || writing;
}

function render() {
  elements.list.replaceChildren();
  for (const task of tasks) {
    const item = document.createElement('li');
    item.className = `task-item${task.status === 'done' ? ' task-done' : ''}`;
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'task-toggle';
    toggle.dataset.taskId = String(task.id);
    toggle.setAttribute('aria-pressed', String(task.status === 'done'));
    toggle.setAttribute('aria-label', `${task.status === 'done' ? '重新打开' : '完成'}工单：${task.title}`);
    toggle.textContent = task.status === 'done' ? '✓' : '';
    toggle.addEventListener('click', () => toggleTask(task.id));
    const content = document.createElement('div');
    content.className = 'task-content';
    const title = document.createElement('span');
    title.className = 'task-title';
    title.textContent = task.title;
    const identity = document.createElement('span');
    identity.className = 'task-id';
    identity.textContent = `工单 #${task.id}`;
    content.append(title, identity);
    const badge = document.createElement('span');
    badge.className = `status-badge ${task.status}`;
    badge.textContent = task.status === 'done' ? '已完成' : '待完成';
    item.append(toggle, content, badge);
    elements.list.append(item);
  }
  elements.empty.hidden = !loaded || tasks.length !== 0;
  elements.total.textContent = loaded ? String(tasks.length) : '—';
  elements.todo.textContent = loaded ? String(tasks.filter(task => task.status === 'todo').length) : '—';
  elements.done.textContent = loaded ? String(tasks.filter(task => task.status === 'done').length) : '—';
  setControls();
}

async function loadTasks() {
  if (loading || writing) return;
  loading = true;
  showError();
  elements.loading.hidden = false;
  elements.loading.textContent = '正在读取工单…';
  elements.empty.hidden = true;
  setControls();
  try {
    const data = await request('/api/tasks');
    if (!Array.isArray(data.tasks) || !data.tasks.every(validTask)) throw new Error('工单数据格式异常，请联系服务维护人员。');
    tasks = data.tasks;
    loaded = true;
    announce(`已读取 ${tasks.length} 张工单。`);
  } catch (error) {
    showError(`${error.message}${loaded ? ' 当前显示上次成功读取的工单，状态可能已变化。' : ''}`);
  } finally {
    loading = false;
    elements.loading.hidden = true;
    render();
  }
}

async function createTask(event) {
  event.preventDefault();
  if (loading || writing) return;
  const title = elements.title.value.trim();
  if (title.length < 1 || title.length > 120) {
    showError('请填写 1–120 个字符的任务标题，不能只填写空格。');
    elements.title.focus();
    return;
  }
  writing = true;
  showError();
  elements.create.textContent = '正在创建…';
  setControls();
  try {
    const data = await request('/api/tasks', { method: 'POST', body: JSON.stringify({ title }) });
    if (!validTask(data.task)) throw new Error('服务器响应格式异常，请刷新列表确认工单是否已创建。');
    if (loaded) tasks.push(data.task);
    elements.title.value = '';
    elements.titleCount.textContent = '0 / 120';
    announce('工单已创建。');
  } catch (error) {
    showError(`${error.message} 如结果不确定，请先刷新，避免重复创建。`);
  } finally {
    writing = false;
    elements.create.textContent = '创建工单 ＋';
    render();
    elements.title.focus();
  }
  // A failed initial read must not turn one successful POST into a fake full list.
  if (!loaded && !elements.error.textContent) await loadTasks();
}

async function toggleTask(id) {
  if (loading || writing) return;
  const task = tasks.find(item => item.id === id);
  if (!task) return;
  writing = true;
  showError();
  setControls();
  const nextStatus = task.status === 'done' ? 'todo' : 'done';
  announce('正在保存工单状态。');
  try {
    const data = await request(`/api/tasks/${id}`, { method: 'PATCH', body: JSON.stringify({ status: nextStatus }) });
    if (!validTask(data.task) || data.task.id !== id) throw new Error('服务器响应格式异常，请刷新确认状态。');
    tasks = tasks.map(item => item.id === id ? data.task : item);
    announce(data.task.status === 'done' ? '工单已完成。' : '工单已重新打开。');
  } catch (error) {
    showError(`${error.message} 当前保留上次确认的状态，请刷新确认。`);
    announce('状态未能确认保存。');
  } finally {
    writing = false;
    render();
    const button = [...elements.list.querySelectorAll('button')].find(item => item.dataset.taskId === String(id));
    if (button) button.focus();
  }
}

elements.title.addEventListener('input', () => {
  elements.titleCount.textContent = `${elements.title.value.length} / 120`;
});
elements.form.addEventListener('submit', createTask);
elements.refresh.addEventListener('click', loadTasks);
loadTasks();
