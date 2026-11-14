'use strict';

const $ = id => document.getElementById(id);
const COLORS = {red: '#bd3b30', blue: '#335b9a'};
const SIDES = ['red', 'blue'];
const SIDE_NAMES = {red: '红方', blue: '蓝方'};
const CARGO_NAMES = {box: '箱', cap: '顶', ball: '球'};
const CARGO_NAMES_FULL = {box: '箱体', cap: '塔顶', ball: '五色石'};
const REACH_COLOR = '#68541c';
const STATUS = {QUEUED: '排队中', RUNNING: '运行中', COMPLETED: '已完成', PARTIAL: '部分完成', VOID: '已作废'};
const KIND_NAMES = {manual: '手动', auto_round: '自动一轮', all_play: '全对全', official: '正式'};
const canvas = $('arena');
const ctx = canvas.getContext('2d');

let session = null;
let state = null;
let replay = null;
let replayTeams = SIDE_NAMES;
let frame = 0;
let playing = false;
let playtime = 0;
let lastTime = 0;
let frameRequest = 0;
let chunkCache = new Map();
let currentSeries = null;
let currentIndex = 0;
let pollTimer = null;
let signatures = {};

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function notice(message) {
  $('notice').textContent = message;
  $('notice').classList.add('visible');
  clearTimeout(notice.timer);
  notice.timer = setTimeout(() => $('notice').classList.remove('visible'), 6000);
}

async function api(path, body, method = 'GET') {
  const options = {method, headers: {}};
  if (session && session.token) options.headers['X-Robocon-Session'] = session.token;
  if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const value = await response.json().catch(() => ({}));
  if (!response.ok) throw Error(value.error || '请求失败');
  return value;
}

function teamName(id) {
  const team = (state?.teams || []).find(item => item.id === id);
  return team ? team.name : String(id || '').slice(0, 8);
}

function stamp(seconds) {
  if (!seconds) return '—';
  const date = new Date(seconds * 1000);
  return date.toLocaleString('zh-CN', {month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'});
}

/* ---------------------------------------------------------------- session */

$('login-form').onsubmit = async event => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  try {
    session = await api('/api/login', {name: $('login-name').value.trim(), password: $('login-password').value}, 'POST');
    sessionStorage.setItem('br-token', session.token);
    $('login-password').value = '';
    $('login-hint').textContent = '账号由管理员创建。首次启动时终端会打印管理员密码。';
    await enterApp();
  } catch (error) {
    notice(error.message);
    $('login-hint').textContent = error.message;
  } finally {
    button.disabled = false;
  }
};

async function enterApp() {
  $('login-view').hidden = true;
  $('app-view').hidden = false;
  const chip = $('user-chip');
  chip.hidden = false;
  chip.replaceChildren(
    element('b', session.user.name),
    element('small', session.user.team_name || '管理员'),
  );
  const quit = element('button', '退出', 'chip-button');
  quit.type = 'button';
  quit.onclick = logout;
  chip.append(quit);
  await refresh();
  if (!pollTimer) pollTimer = setInterval(() => refresh(true).catch(() => {}), 1500);
}

async function logout() {
  try { await api('/api/logout', {}, 'POST'); } catch { /* already gone */ }
  sessionStorage.removeItem('br-token');
  session = null;
  state = null;
  replay = null;
  chunkCache = new Map();
  currentSeries = null;
  clearInterval(pollTimer);
  pollTimer = null;
  $('app-view').hidden = true;
  $('login-view').hidden = false;
  $('user-chip').hidden = true;
  $('user-chip').replaceChildren();
  resetReplayView();
}

async function boot() {
  draw({});
  const stored = sessionStorage.getItem('br-token');
  if (stored) {
    session = {token: stored, user: null};
    try {
      const current = await api('/api/session');
      if (current.user) {
        session.user = current.user;
        await enterApp();
        return;
      }
    } catch { /* expired or server restarted */ }
    session = null;
    sessionStorage.removeItem('br-token');
  }
  showLogin();
}

function showLogin() {
  $('login-view').hidden = false;
  $('app-view').hidden = true;
  $('user-chip').hidden = true;
}

/* ------------------------------------------------------------- app state */

async function refresh(quiet = false) {
  if (!session) return;
  try {
    state = await api('/api/state');
  } catch (error) {
    if (!quiet) notice(error.message);
    if (String(error.message).includes('login')) logout();
    return;
  }
  $('queue-label').textContent = '串行调度 · 版本已冻结';
  renderTeam();
  renderStrategies();
  renderBattleSelects();
  renderRanking();
  renderBatches();
  renderAdmin();
}

function renderTeam() {
  const panel = $('team-panel');
  panel.replaceChildren();
  const user = state.user;
  if (!user.team_id) {
    $('team-meta').textContent = user.role === 'admin' ? '管理员' : '—';
    panel.append(element('p', '此账号不参与对战。用管理员账号创建队伍与队员。'));
    return;
  }
  const team = (state.teams || []).find(item => item.id === user.team_id) || {};
  $('team-meta').textContent = team.official_version ? '已有正式策略' : '尚无正式策略';
  const head = element('div', 'team-head');
  head.append(element('b', team.name || user.team_name || '—'));
  head.append(element('strong', String(Math.round(team.elo ?? 1000)), 'team-elo'));
  panel.append(head);
  panel.append(element('p', `胜 ${team.wins || 0} · 平 ${team.draws || 0} · 负 ${team.losses || 0} · 共 ${team.pairs || 0} 组`, 'hint'));
}

function renderStrategies() {
  const list = state.strategies || [];
  const mine = state.user.team_id;
  const signature = list.map(v => `${v.id}:${v.kind}`).join(',');
  const container = $('strategies');
  container.classList.toggle('empty', list.length === 0);
  if (signatures.strategies !== signature) {
    signatures.strategies = signature;
    container.replaceChildren();
    if (!list.length) container.textContent = '还没有策略版本';
    for (const version of list) {
      const row = element('div', 'strategy-row');
      const title = element('div', 'strategy-title');
      title.append(element('b', version.filename));
      title.append(element('span', version.kind === 'official' ? '正式' : '草稿',
        'badge ' + (version.kind === 'official' ? 'official' : 'draft')));
      const meta = element('small', `${String(version.sha256).slice(0, 8)} · ${stamp(version.created)}`);
      const actions = element('div', 'strategy-actions');
      const download = element('button', '下载', 'chip-button');
      download.type = 'button';
      download.onclick = () => downloadStrategy(version);
      actions.append(download);
      if (version.kind !== 'official' && version.team_id === mine) {
        const promote = element('button', '设为正式', 'chip-button primary');
        promote.type = 'button';
        promote.onclick = () => submitOfficial(version);
        actions.append(promote);
      }
      row.append(title, meta, actions);
      container.append(row);
    }
  }
  const select = $('battle-version');
  const selected = select.value;
  const options = list.filter(v => v.team_id === mine);
  if (signatures.versions !== options.map(v => v.id).join(',')) {
    signatures.versions = options.map(v => v.id).join(',');
    select.replaceChildren();
    for (const version of options) {
      const option = element('option', `${version.filename}${version.kind === 'official' ? ' · 正式' : ''}`);
      option.value = version.id;
      select.append(option);
    }
    const official = options.find(v => v.kind === 'official');
    select.value = (official || options[0] || {}).id || '';
  }
  if (selected && options.some(v => v.id === selected)) select.value = selected;
}

function renderBattleSelects() {
  const opponents = (state.teams || []).filter(t => t.official_version && t.id !== state.user.team_id);
  const signature = opponents.map(t => `${t.id}:${t.official_version}`).join(',');
  const select = $('battle-opponent');
  if (signatures.opponents !== signature) {
    signatures.opponents = signature;
    const keep = select.value;
    select.replaceChildren();
    for (const team of opponents) {
      const option = element('option', team.name);
      option.value = team.official_version;
      select.append(option);
    }
    if (opponents.some(t => t.official_version === keep)) select.value = keep;
  }
  const profiles = state.platform.profiles || [];
  const profileSignature = profiles.map(p => `${p.name}:${p.digest}`).join(',');
  if (signatures.profiles !== profileSignature) {
    signatures.profiles = profileSignature;
    for (const side of SIDES) {
      const picker = $(side + '-profile');
      picker.replaceChildren();
      for (const profile of profiles) {
        const option = element('option', profile.label);
        option.value = profile.name;
        option.title = profile.notes || '';
        picker.append(option);
      }
      picker.value = state.platform.default_profiles?.[side] || profiles[0]?.name || '';
    }
  }
  $('battle-meta').textContent = opponents.length
    ? `可挑战 ${opponents.length} 队`
    : '暂无对手正式策略';
}

function renderRanking() {
  const container = $('ranking');
  container.replaceChildren();
  const teams = state.teams || [];
  if (!teams.length) {
    container.append(element('p', '还没有队伍', 'hint'));
    return;
  }
  teams.forEach((team, index) => {
    const row = element('div', 'ranking-row');
    row.append(element('b', String(index + 1).padStart(2, '0')));
    const info = element('div', 'ranking-info');
    info.append(element('b', team.name));
    info.append(element('small', `${team.wins || 0}胜 ${team.draws || 0}平 ${team.losses || 0}负`));
    row.append(info, element('strong', String(Math.round(team.elo))));
    container.append(row);
  });
}

function renderBatches() {
  const container = $('batches');
  const batches = state.batches || [];
  const signature = batches.map(b => `${b.id}:${b.status}:${b.progress.done}:${b.progress.failed}`).join(',');
  if (signatures.batches === signature) return;
  signatures.batches = signature;
  container.replaceChildren();
  if (!batches.length) {
    container.append(element('p', '还没有对战记录', 'hint'));
    return;
  }
  for (const batch of batches) {
    const block = element('div', 'batch');
    const head = element('div', 'batch-head');
    head.append(element('span', KIND_NAMES[batch.kind] || batch.kind, 'badge kind-' + batch.kind));
    head.append(element('span', STATUS[batch.status] || batch.status, 'badge ' + batch.status));
    head.append(element('small', `${stamp(batch.created)} · ${batch.progress.done || 0}/${batch.progress.total || 0} 组`));
    if (batch.team_id === state.user.team_id) head.append(element('small', '由本队发起'));
    block.append(head);
    const seriesList = element('div', 'series-list');
    for (const series of batch.series || []) {
      seriesList.append(seriesRow(series));
    }
    block.append(seriesList);
    container.append(block);
  }
}

function seriesRow(series) {
  const row = element('div', 'series-row');
  const result = series.result || [];
  let aWins = 0;
  let bWins = 0;
  result.forEach((game, index) => {
    if (!game.winner) return;
    const aIsRed = index % 2 === 0;
    if ((aIsRed && game.winner === 'red') || (!aIsRed && game.winner === 'blue')) aWins += 1;
    else bWins += 1;
  });
  const label = element('div', 'series-teams');
  label.append(element('b', teamName(series.a)));
  label.append(element('span', series.count > 1 ? `${aWins} : ${bWins}` : (result.length ? '单局' : '—'), 'series-score'));
  label.append(element('b', teamName(series.b)));
  row.append(label);
  row.append(element('small', STATUS[series.status] || series.status, 'series-status'));
  if (result.length) {
    result.forEach((game, index) => {
      const open = element('button', result.length > 1 ? `第 ${index + 1} 局` : '回放', 'chip-button');
      open.type = 'button';
      open.onclick = () => openReplay(series.id, index);
      row.append(open);
    });
  }
  return row;
}

function renderAdmin() {
  const panel = $('admin-panel');
  panel.hidden = !state.is_admin;
  if (!state.is_admin) return;
  const teams = $('new-user-team');
  const signature = (state.teams || []).map(t => t.id + t.name).join(',');
  if (signatures.adminTeams !== signature) {
    signatures.adminTeams = signature;
    teams.replaceChildren();
    for (const team of state.teams || []) {
      const option = element('option', team.name);
      option.value = team.id;
      teams.append(option);
    }
  }
  const users = state.users || [];
  const container = $('user-list');
  container.classList.toggle('empty', users.length === 0);
  container.replaceChildren();
  for (const user of users) {
    const row = element('div', 'strategy-row');
    const title = element('div', 'strategy-title');
    title.append(element('b', user.name));
    title.append(element('span', user.role === 'admin' ? '管理员' : '队员',
      'badge ' + (user.role === 'admin' ? 'official' : 'draft')));
    row.append(title, element('small', user.team_name || '不参与对战'));
    container.append(row);
  }
}

/* --------------------------------------------------------------- actions */

$('upload-form').onsubmit = async event => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  try {
    const file = $('file').files[0];
    if (!file) return;
    const source = await file.text();
    const result = await api('/api/strategies', {filename: file.name, source}, 'POST');
    $('upload-status').textContent = '版本已冻结：' + result.sha256.slice(0, 12) + ' · 语法与入口检查通过';
    $('file').value = '';
    notice('策略版本已上传');
    await refresh();
  } catch (error) {
    notice(error.message);
  } finally {
    button.disabled = false;
  }
};

$('team-form').onsubmit = async event => {
  event.preventDefault();
  try {
    await api('/api/admin/teams', {name: $('new-team').value.trim()}, 'POST');
    $('new-team').value = '';
    notice('队伍已创建');
    signatures.adminTeams = null;
    await refresh();
  } catch (error) {
    notice(error.message);
  }
};

$('user-form').onsubmit = async event => {
  event.preventDefault();
  try {
    await api('/api/admin/users', {
      name: $('new-user').value.trim(),
      password: $('new-password').value,
      role: $('new-user-role').value,
      team_id: $('new-user-team').value || null,
    }, 'POST');
    $('new-user').value = '';
    $('new-password').value = '';
    notice('账号已创建');
    await refresh();
  } catch (error) {
    notice(error.message);
  }
};

async function downloadStrategy(version) {
  try {
    const response = await fetch('/api/strategies/' + version.id + '/source',
      {headers: {'X-Robocon-Session': session.token}});
    if (!response.ok) throw Error('下载失败');
    const blob = await response.blob();
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = version.filename;
    link.click();
    URL.revokeObjectURL(link.href);
  } catch (error) {
    notice(error.message);
  }
}

async function submitOfficial(version) {
  try {
    const reply = await api('/api/strategies/' + version.id + '/official', {}, 'POST');
    notice(reply.waiting
      ? '已设为正式策略，等待其他队伍提交后可交手'
      : `正式循环赛已排队：${reply.series} 组换边双局`);
    await refresh();
  } catch (error) {
    notice(error.message);
  }
}

function battlePayload(kind) {
  const payload = {
    kind,
    version_id: $('battle-version').value,
    red_profile: $('red-profile').value,
    blue_profile: $('blue-profile').value,
  };
  if (kind === 'manual') {
    payload.opponent_version = $('battle-opponent').value;
    payload.count = Number(document.querySelector('[name=manual-count]:checked').value);
    if (!payload.opponent_version) throw Error('先选择一个对手');
  }
  return payload;
}

for (const [id, kind] of [['do-manual', 'manual'], ['do-round', 'auto_round'], ['do-all', 'all_play']]) {
  $(id).onclick = async event => {
    const button = event.submitter || $(id);
    button.disabled = true;
    try {
      const reply = await api('/api/batches', battlePayload(kind), 'POST');
      notice(`已排队 ${reply.series} 组（${KIND_NAMES[kind]}），训练不计分`);
      await refresh();
    } catch (error) {
      notice(error.message);
    } finally {
      button.disabled = false;
    }
  };
}

for (const id of ['red-profile', 'blue-profile']) $(id).onchange = () => {
  const same = $('red-profile').value === $('blue-profile').value;
  $('battle-hint').textContent = same
    ? '双方同参数：训练不影响 Elo。'
    : '双方参数不同：仅作训练记录，不计入 Elo。';
};

/* ---------------------------------------------------------------- replay */

function resetReplayView() {
  replay = null;
  frame = 0;
  playtime = 0;
  setPlaying(false);
  $('timeline').max = 0;
  $('timeline').value = 0;
  $('clock').textContent = '000.0';
  $('frame-detail').textContent = '等待回放数据';
  $('replay-label').textContent = '选择一个已完成的系列';
  for (const side of SIDES) {
    $('team-' + side).textContent = SIDE_NAMES[side];
    $('score-' + side).textContent = '—';
  }
  $('events').classList.add('empty');
  $('events').textContent = '打开回放查看真实事件';
  $('ledger').classList.add('empty');
  $('ledger').textContent = '选择回放后查看分项';
  draw({});
}

async function openReplay(seriesId, index) {
  try {
    notice('正在载入比赛包…');
    replay = await api('/api/replay?series=' + encodeURIComponent(seriesId) + '&index=' + index);
    currentSeries = seriesId;
    currentIndex = index;
    chunkCache = new Map();
    replayTeams = {red: teamName(replay.series.a), blue: teamName(replay.series.b)};
    // Odd games swap sides: A drives blue in the second leg.
    if (index % 2 === 1) replayTeams = {red: teamName(replay.series.b), blue: teamName(replay.series.a)};
    for (const side of SIDES) $('team-' + side).textContent = replayTeams[side];
    frame = 0;
    playtime = 0;
    setPlaying(false);
    $('timeline').max = replay.frame_count - 1;
    $('replay-label').textContent = `${teamName(replay.series.a)} vs ${teamName(replay.series.b)} · 第 ${index + 1} 局`
      + (replay.series.ranked ? ' · 正式' : ' · 训练');
    renderEvents();
    renderLedger();
    renderMetrics();
    await showFrame(0);
    notice('比赛包已载入');
  } catch (error) {
    notice(error.message);
  }
}

async function loadChunk(start) {
  if (chunkCache.has(start)) return chunkCache.get(start);
  const block = await api('/api/replay/frames?series=' + encodeURIComponent(currentSeries)
    + '&index=' + currentIndex + '&from=' + start + '&count=' + replay.chunk);
  if (chunkCache.size >= 4) chunkCache.delete(chunkCache.keys().next().value);
  chunkCache.set(start, block);
  return block;
}

function chunkStart(index) {
  return Math.floor(index / replay.chunk) * replay.chunk;
}

async function showFrame(index) {
  if (!replay) return;
  const ticket = ++frameRequest;
  const target = Math.min(replay.frame_count - 1, Math.max(0, index));
  const start = chunkStart(target);
  let block;
  try {
    block = await loadChunk(start);
  } catch (error) {
    notice(error.message);
    return;
  }
  if (ticket !== frameRequest) return;
  const snap = block.frames[target - start];
  if (!snap) return;
  frame = target;
  const game = snap.state;
  $('timeline').value = frame;
  $('clock').textContent = (snap.sim_time_ms / 1000).toFixed(1).padStart(5, '0');
  for (const side of SIDES) $('score-' + side).textContent = game.score?.[side]?.total ?? '—';
  const eligibility = side => game.qualified?.[side] ? '✓' : '—';
  $('frame-detail').textContent = '帧 ' + snap.seq + ' · rev ' + game.world_revision
    + ' · 资格 R:' + eligibility('red') + ' B:' + eligibility('blue');
  draw(game);
}

function seekTime(time) {
  if (!replay) return;
  const times = replay.times_ms;
  let low = 0;
  let high = times.length - 1;
  while (low < high) {
    const mid = (low + high) >> 1;
    if (times[mid] < time) low = mid + 1;
    else high = mid;
  }
  showFrame(low);
  playtime = times[low];
}

function renderEvents() {
  const container = $('events');
  container.classList.remove('empty');
  container.replaceChildren();
  const events = replay.events || [];
  const list = document.createDocumentFragment();
  for (const event of events) {
    const time = event.occurred_at_ms || 0;
    const row = element('button', undefined, 'event');
    row.type = 'button';
    row.append(
      element('time', (time / 1000).toFixed(3) + 's'),
      element('span', event.type),
      element('small', JSON.stringify(event)),
    );
    row.onclick = () => seekTime(time);
    list.append(row);
  }
  container.append(list);
  if (!events.length) container.append(element('p', '本局无事件'));
}

function renderLedger() {
  const container = $('ledger');
  container.classList.remove('empty');
  container.replaceChildren();
  const groups = Array.isArray(replay.ledger) ? {items: replay.ledger} : replay.ledger;
  for (const [team, items] of Object.entries(groups)) {
    container.append(element('h3', replayTeams[team] || team, team === 'red' ? 'red-text' : 'blue-text'));
    if (!Array.isArray(items)) {
      container.append(element('pre', JSON.stringify(items, null, 2)));
      continue;
    }
    if (!items.length) {
      container.append(element('p', '本队无计分项'));
      continue;
    }
    const table = element('table');
    const header = table.createTHead().insertRow();
    for (const heading of ['物品 / 落位', '分项', '分值']) header.append(element('th', heading));
    const body = table.createTBody();
    for (const item of items) {
      const row = body.insertRow();
      const id = element('td', item.item_id || item.id || '—');
      id.append(element('small', ' ' + (item.tower_id || '') + ' ' + (item.level || '')));
      row.title = item.reason || '';
      row.append(id, element('td', item.category || ''), element('td', String(item.points ?? '')));
    }
    container.append(table);
  }
}

function renderMetrics() {
  const metrics = replay.result.metrics;
  if (!metrics) return;
  const container = $('ledger');
  const panel = element('div', undefined, 'metrics');
  panel.append(element('b', '过程指标'));
  const time = value => value == null ? '未达成' : (value / 1000).toFixed(2) + ' 秒';
  for (const side of SIDES) {
    const record = metrics.teams[side];
    panel.append(element('p', replayTeams[side]
      + ' · 资格 ' + time(record.qualification_ms)
      + ' · 放球 ' + time(record.central_ball_placed_ms)
      + ' · 争用 ' + record.contested_receipts
      + ' · 目标失效 ' + record.target_changed_receipts));
  }
  panel.append(element('p', '故障触发：' + metrics.faults_triggered + ' / ' + metrics.faults_configured));
  container.prepend(panel);
}

function setPlaying(value) {
  playing = value;
  $('play').textContent = playing ? 'Ⅱ' : '▶';
  $('play').setAttribute('aria-label', playing ? '暂停回放' : '播放回放');
}

$('timeline').oninput = () => {
  if (!replay) return;
  const target = Number($('timeline').value);
  showFrame(target);
  playtime = replay.times_ms[target] ?? 0;
};

$('play').onclick = () => {
  if (!replay) return;
  if (frame >= replay.frame_count - 1) {
    showFrame(0);
    playtime = 0;
  }
  setPlaying(!playing);
};
/* Positions come from the engine. The projection only changes how we draw them. */
function project(x, y, z = 0) {
  return [500 + (x - y) * 0.039, 100 + (x + y) * 0.021 - z * 0.085];
}

function polygon(points, fill, stroke = '#b9bcad') {
  ctx.beginPath();
  points.forEach((point, i) => {
    const projected = project(...point);
    if (i) ctx.lineTo(...projected);
    else ctx.moveTo(...projected);
  });
  ctx.closePath();
  ctx.fillStyle = fill;
  ctx.fill();
  ctx.strokeStyle = stroke;
  ctx.lineWidth = 1;
  ctx.stroke();
}

function label(text, x, y, color = '#526046', font = '10px Consolas') {
  ctx.fillStyle = color;
  ctx.font = font;
  ctx.textAlign = 'center';
  ctx.fillText(text, x, y);
}

function plate(x, y, z, width, depth, fill, outline) {
  const dx = width / 2;
  const dy = depth / 2;
  polygon([
    [x - dx, y - dy, z], [x + dx, y - dy, z],
    [x + dx, y + dy, z], [x - dx, y + dy, z],
  ], fill, outline);
}

function cuboid(bounds, bottom, top, color, left, right) {
  const [x1, y1, x2, y2] = bounds;
  polygon([[x1,y2,bottom], [x2,y2,bottom], [x2,y2,top], [x1,y2,top]], left);
  polygon([[x2,y1,bottom], [x2,y2,bottom], [x2,y2,top], [x2,y1,top]], right);
  polygon([[x1,y1,top], [x2,y1,top], [x2,y2,top], [x1,y2,top]], color, '#f7f5ee');
}

function material(item, x, y, z, dims, color) {
  if (item.kind === 'ball') {
    // V3 §2.13: diameter 207–213 mm. Show the 210 mm nominal diameter.
    const radius = dims.ball_diameter_mm / 2;
    const p = project(x, y, z + radius);
    ctx.beginPath();
    ctx.ellipse(...p, radius * Math.hypot(0.039, 0.039), radius * 0.085, 0, 0, Math.PI * 2);
    ctx.fillStyle = '#d2a52c';
    ctx.fill();
    ctx.strokeStyle = '#a8781f';
    ctx.stroke();
    return radius * 2;
  }
  const [w, d, h] = item.kind === 'cap' ? dims.cap_mm : dims.box_mm;
  const paint = COLORS[color || item.owner_color] || '#a7ac87';
  cuboid([x - w / 2, y - d / 2, x + w / 2, y + d / 2], z, z + h, paint, paint, paint);
  return h;
}

function route(a, b, color) {
  ctx.beginPath();
  ctx.moveTo(...project(a.x_mm, a.y_mm, a.z_mm));
  ctx.lineTo(...project(b.x_mm, b.y_mm, b.z_mm));
  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  ctx.setLineDash([3, 4]);
  ctx.stroke();
  ctx.setLineDash([]);
}

function drawTransfer(team, supply, map, state, dims) {
  const point = map.points?.[team.toUpperCase() + '_TRANSFER'];
  if (!point) return;
  const {x_mm: x, y_mm: y, z_mm: z} = point;
  const [w, d] = dims.transfer_mm;
  plate(x, y, z, w, d, COLORS[team] + '18', COLORS[team]);
  // The upper and lower slot share one footprint; the cap has its own bay.
  const boxX = x - w * 0.22;
  const capX = x + w * 0.22;
  plate(boxX, y, z + 1, 380, 380, '#ffffffa0');
  plate(capX, y, z + 1, 250, 250, '#ffffffa0');
  const slot = key => state.items?.[supply.slots?.[key]];
  const lower = slot('BOX_LOWER');
  const upper = slot('BOX_UPPER');
  const cap = slot('CAP_BAY');
  if (lower) material(lower, boxX, y, z, dims);
  if (upper) material(upper, boxX, y, z + dims.box_mm[2], dims);
  if (cap) material(cap, capX, y, z, dims);
  const [sx, sy] = project(x, y + d / 2, z);
  const status = '下' + (lower ? '■' : '□') + ' 上' + (upper ? '■' : '□') + ' 顶' + (cap ? '■' : '□');
  label(team.toUpperCase() + ' TR / ' + status, sx, sy + 20, COLORS[team]);
}

function carriedItems(robot, items) {
  const cargo = (robot.cargo || []).map(item => items?.[item.id] || item);
  const ids = new Set(cargo.map(item => item.id));
  for (const item of Object.values(items || {})) {
    if (item.holder_id === robot.id && item.location?.startsWith('held_flip:') && !ids.has(item.id)) {
      cargo.push(item);
    }
  }
  return cargo;
}

function cargoText(robot, cargo, names = CARGO_NAMES) {
  const counts = {box: 0, cap: 0, ball: 0};
  let flipCaps = 0;
  let other = 0;
  for (const item of cargo) {
    if (item.location?.startsWith('held_flip:')) flipCaps++;
    else if (Object.hasOwn(counts, item.kind)) counts[item.kind]++;
    else other++;
  }
  const parts = [];
  if (robot.carried_stack_id && counts.box >= 2) {
    parts.push('整摞 ×2');
    counts.box -= 2;
  }
  for (const [kind, count] of Object.entries(counts)) {
    if (count) parts.push(names[kind] + (count > 1 ? ' ×' + count : ''));
  }
  if (flipCaps) parts.push('翻顶持有');
  if (other) parts.push('物品 ×' + other);
  return parts.join(' · ') || '空载';
}

function drawRobot(robot, dims, cargo, stack) {
  const pose = robot.pose;
  if (!pose) return;
  const {x_mm: x, y_mm: y, z_mm: z = 0} = pose;
  const [sx, sy] = project(x, y, z);
  const color = COLORS[robot.team] || '#333';
  ctx.beginPath();
  ctx.ellipse(sx, sy + 8, 18, 8, 0, 0, Math.PI * 2);
  ctx.fillStyle = '#25282022';
  ctx.fill();
  ctx.beginPath();
  ctx.arc(sx, sy - 3, 12, 0, Math.PI * 2);
  ctx.fillStyle = color;
  ctx.strokeStyle = '#fff9e8';
  ctx.lineWidth = 3;
  ctx.fill();
  ctx.stroke();
  label(robot.team === 'red' ? 'R' : 'B', sx, sy, '#fff', 'bold 9px Consolas');
  const base = z + 220;
  let top = base;
  const stackIds = [stack?.bottom_item_id, stack?.top_item_id];
  const stacked = robot.carried_stack_id
    ? (stack ? stackIds.map(id => cargo.find(item => item.id === id)).filter(Boolean)
      : cargo.filter(item => item.kind === 'box').slice(0, 2))
    : [];
  for (const item of stacked) top += material(item, x, y, top, dims);
  const loose = cargo.filter(item => !stacked.includes(item));
  let labelY = project(x, y, top)[1];
  loose.forEach((item, i) => {
    const offset = stacked.length ? (i + 1) * 360 : (i - (loose.length - 1) / 2) * 360;
    const height = material(item, x + offset, y, base, dims);
    labelY = Math.min(labelY, project(x + offset, y, base + height)[1]);
  });
  const action = robot.action?.kind || robot.control_state || '';
  label(action, sx, sy + 23, color);
  return {x: sx, y: labelY - 7, color, name: replayTeams[robot.team] || 'BR', status: cargoText(robot, cargo)};
}

function drawRobotLabels(labels) {
  const drawn = [];
  for (const robot of labels.sort((a, b) => a.y - b.y)) {
    const nameFont = 'bold 16px "Microsoft YaHei", sans-serif';
    const statusFont = 'bold 13px "Microsoft YaHei", sans-serif';
    ctx.font = nameFont;
    const name = fitText(robot.name, 172);
    const nameWidth = ctx.measureText(name).width;
    ctx.font = statusFont;
    const status = fitText(robot.status, 172);
    const width = Math.max(nameWidth, ctx.measureText(status).width) + 20;
    const x = Math.max(width / 2 + 3, Math.min(canvas.width - width / 2 - 3, robot.x));
    let y = Math.max(4, robot.y - 49);
    const collides = top => drawn.some(box => Math.abs(x - box.x) < (width + box.width) / 2 + 6
      && Math.abs(top - box.y) < 45);
    if (collides(y)) {
      let above = y - 45;
      while (above >= 4 && collides(above)) above -= 45;
      if (above >= 4) y = above;
      else while (collides(y)) y += 45;
    }
    drawn.push({x, y, width});
    ctx.strokeStyle = robot.color;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x, y + 41);
    ctx.lineTo(robot.x, robot.y - 3);
    ctx.stroke();
    ctx.fillStyle = '#fcfaf0';
    ctx.fillRect(x - width / 2, y, width, 41);
    ctx.strokeRect(x - width / 2, y, width, 41);
    label(name, x, y + 17, robot.color, nameFont);
    label(status, x, y + 33, '#252820', statusFont);
  }
}

function fitText(text, width) {
  if (ctx.measureText(text).width <= width) return text;
  const chars = Array.from(text);
  while (ctx.measureText(chars.join('') + '…').width > width) chars.pop();
  return chars.join('') + '…';
}

function reachPoints(map, state) {
  const points = map.points || map.workpoints || {};
  const towers = state.towers || map.towers || {};
  const robots = Object.values(state.robots || {});
  return Object.values(towers).flatMap(tower => {
    const pointId = tower.l1_approach_point_id || map.towers?.[tower.id]?.l1_approach_point_id;
    const point = points[pointId];
    if (!point) return [];
    const atPoint = robots.find(robot => robot.pose?.layer === 'L1' && robot.pose.workpoint_id === pointId);
    const action = atPoint?.action;
    const building = action?.params?.site_id === tower.id && (action.kind === 'build_stack'
      || (action.kind === 'build' && ['base', 'middle'].includes(action.params.level)));
    return [{tower, point, atPoint, building}];
  });
}

function drawReachPoint({tower, point, atPoint}) {
  const [x, y] = project(point.x_mm, point.y_mm, point.z_mm);
  const size = atPoint ? 420 : 320;
  plate(point.x_mm, point.y_mm, point.z_mm + 2, size, size,
    atPoint ? '#e8cd76' : '#f5e4a6', REACH_COLOR);
  const n = tower.id.split('_').at(-1);
  label(n, x, y + 3, REACH_COLOR, 'bold 10px Consolas');
}

function renderFrameDetails(robots, cargoByTeam, reaches) {
  $('br-summary').hidden = !replay;
  for (const side of SIDES) {
    const robot = robots.find(robot => robot.team === side);
    const status = robot ? cargoText(robot, cargoByTeam[side], CARGO_NAMES_FULL) : '—';
    $('br-' + side + '-status').textContent = replayTeams[side] + ' BR：' + status;
  }
  $('reach-legend').hidden = !reaches.length;
  const activity = reaches.filter(reach => reach.atPoint).map(({tower, atPoint, building}) =>
    replayTeams[atPoint.team] + ' · ' + (building ? '跨层放箱 ' : '已到指定点 ') + tower.id);
  $('reach-status').textContent = activity.join(' / ');
  $('reach-status').hidden = !activity.length;
}

function drawCentral(map, state, dims, z) {
  const [x1, y1, x2, y2] = map.layers?.L2?.bounds || [4000, 4000, 7000, 7000];
  const x = (x1 + x2) / 2;
  const y = (y1 + y2) / 2;
  const top = z + dims.central_pedestal_height_above_l2_mm;
  const radius = dims.central_pedestal_diameter_mm / 2;
  const [sx, bottom] = project(x, y, z);
  const [, sy] = project(x, y, top);
  const rx = radius * Math.hypot(0.039, 0.039);
  const ry = radius * Math.hypot(0.021, 0.021);
  ctx.fillStyle = '#7d826b';
  ctx.fillRect(sx - rx, sy, rx * 2, bottom - sy);
  ctx.beginPath();
  ctx.ellipse(sx, sy, rx, ry, 0, 0, Math.PI * 2);
  ctx.fillStyle = '#abb192';
  ctx.fill();
  if (state.central_ball?.placed || state.central_ball?.placed_by) {
    material({kind: 'ball'}, x, y, top - 60, dims);
  }
}

function draw(state) {
  const map = state.map || replay?.static?.map || replay?.manifest?.map || {};
  const dims = {
    field_mm: [11000, 11000], l1_height_mm: 600, l2_height_mm: 900,
    transfer_mm: [1000, 1000], green_pad_mm: [500, 500],
    box_mm: [350, 350, 350], cap_mm: [200, 200, 200],
    ball_diameter_mm: 210, central_pedestal_diameter_mm: 270,
    central_pedestal_height_above_l2_mm: 800,
    ...map.dimensions,
  };
  const [width, depth] = dims.field_mm;
  const l1 = map.layers?.L1 || {z_mm: dims.l1_height_mm, bounds: [2500, 2500, 8500, 8500]};
  const l2 = map.layers?.L2 || {z_mm: dims.l2_height_mm, bounds: [4000, 4000, 7000, 7000]};
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  plate(width / 2, depth / 2, 0, width, depth, '#e7e7d9', '#959c87');
  ctx.beginPath();
  for (let x = 1000; x < width; x += 1000) {
    ctx.moveTo(...project(x, 0));
    ctx.lineTo(...project(x, depth));
  }
  for (let y = 1000; y < depth; y += 1000) {
    ctx.moveTo(...project(0, y));
    ctx.lineTo(...project(width, y));
  }
  ctx.strokeStyle = '#d4d8c8';
  ctx.lineWidth = 0.5;
  ctx.stroke();
  cuboid(l1.bounds, 0, l1.z_mm, '#dfe3ca', '#d2d3c0', '#b9bea7');
  cuboid(l2.bounds, l1.z_mm, l2.z_mm, '#d1d9b6', '#c8cdb4', '#adb691');

  for (const edge of map.edges || []) {
    const from = map.points[edge.a];
    const to = map.points[edge.b];
    if (from && to) route(from, to, edge.owner ? COLORS[edge.owner] + '35' : '#7c886b55');
  }
  for (const connection of Object.values(map.connections || {})) {
    const from = map.points[connection.from_point_id];
    const to = map.points[connection.to_point_id];
    if (from && to) route(from, to, '#876c4f75');
  }

  const reaches = reachPoints(map, state);
  reaches.forEach(drawReachPoint);
  const objects = [];
  const robotLabels = [];
  const add = (x, y, render) => objects.push({depth: x + y, render});
  add(width / 2, depth / 2, () => drawCentral(map, state, dims, l2.z_mm));
  for (const tower of Object.values(state.towers || map.towers || {})) {
    const {x_mm: x, y_mm: y} = tower.center;
    const z = tower.layer === 'L2' ? l2.z_mm : l1.z_mm;
    add(x, y, () => {
      plate(x, y, z, ...dims.green_pad_mm, '#79905c');
      let height = z;
      for (const level of ['base', 'middle', 'cap']) {
        if (!tower[level]) continue;
        const item = state.items?.[tower[level]] || {kind: level === 'cap' ? 'cap' : 'box'};
        if (item.location?.startsWith('held_flip:')) continue;
        height += material(item, x, y, height, dims, level === 'cap' ? tower.top_color : undefined);
      }
      const [sx, sy] = project(x, y, z);
      const name = tower.id.replace('PUBLIC', 'P').replace('BLUE', 'B').replace('RED', 'R');
      label(name, sx, sy + 19);
    });
  }
  for (const [team, supply] of Object.entries(state.supply || {})) {
    const point = map.points?.[team.toUpperCase() + '_TRANSFER'];
    if (point) add(point.x_mm, point.y_mm, () => drawTransfer(team, supply, map, state, dims));
  }
  const robots = Object.values(state.robots || {});
  const cargoByTeam = Object.fromEntries(robots.map(robot => [robot.team, carriedItems(robot, state.items)]));
  for (const robot of robots) {
    if (robot.pose) add(robot.pose.x_mm, robot.pose.y_mm, () => {
      robotLabels.push(drawRobot(robot, dims, cargoByTeam[robot.team], state.stacks?.[robot.carried_stack_id]));
    });
  }
  objects.sort((a, b) => a.depth - b.depth).forEach(object => object.render());
  for (const {tower, point, building} of reaches) {
    if (building) route(point, {...tower.center, z_mm: l2.z_mm}, REACH_COLOR);
  }
  label(width.toLocaleString('zh-CN') + ' mm', 500, 602);
  label('L1 +' + l1.z_mm, 775, 310);
  label('L2 +' + l2.z_mm, 630, 258);
  drawRobotLabels(robotLabels);
  renderFrameDetails(robots, cargoByTeam, reaches);
}

function animate(time) {
  const delta = Math.min(100, time - lastTime);
  lastTime = time;
  if (playing && replay) {
    playtime += delta * Number($('speed').value);
    let next = frame;
    while (next < replay.frame_count - 1 && replay.times_ms[next + 1] <= playtime) next++;
    if (next !== frame) showFrame(next);
    const ahead = next + replay.chunk;
    if (ahead < replay.frame_count) loadChunk(chunkStart(ahead)).catch(() => {});
    if (frame >= replay.frame_count - 1) setPlaying(false);
  }
  requestAnimationFrame(animate);
}

requestAnimationFrame(animate);
boot();
