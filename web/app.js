'use strict';

const $ = id => document.getElementById(id);
const COLORS = {red: '#bd3b30', blue: '#335b9a'};
const SIDES = ['red', 'blue'];
const SIDE_NAMES = {red: '红方', blue: '蓝方'};
const CARGO_NAMES = {box: '箱', cap: '顶', ball: '球'};
const CARGO_NAMES_FULL = {box: '箱体', cap: '塔顶', ball: '五色石'};
const REACH_COLOR = '#68541c';
const STATUS = {QUEUED: '排队中', RUNNING: '运行中', COMPLETED: '已完成', VOID: '已作废'};
const canvas = $('arena');
const ctx = canvas.getContext('2d');

let token = '';
let catalog = {teams: [], versions: [], jobs: []};
let replay = null;
let replayTeams = SIDE_NAMES;
let frame = 0;
let playing = false;
let playtime = 0;
let lastTime = 0;
let requestNumber = 0;

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

async function api(path, data) {
  const options = data ? {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-Robocon-Token': token},
    body: JSON.stringify(data),
  } : {};
  const response = await fetch(path, options);
  const value = await response.json();
  if (!response.ok) throw Error(value.error || '请求失败');
  return value;
}

function teamName(id, fallback = '队伍') {
  return catalog.teams.find(team => team.id === id)?.name || fallback;
}

function renderSelectors() {
  for (const id of ['strategy-a', 'strategy-b']) {
    const select = $(id);
    const oldValue = select.value;
    select.replaceChildren();
    for (const version of catalog.versions) {
      const title = teamName(version.team_id) + ' / ' + version.filename + ' · ' + version.sha256.slice(0, 7);
      select.append(new Option(title, version.id));
    }
    if ([...select.options].some(option => option.value === oldValue)) {
      select.value = oldValue;
    } else if (id === 'strategy-b' && select.options.length > 1) {
      const otherTeam = catalog.versions.findIndex(version => version.team_id !== catalog.versions[0].team_id);
      if (otherTeam >= 0) select.selectedIndex = otherTeam;
    }
  }
}

function renderRanking() {
  const ranking = $('ranking');
  ranking.replaceChildren();
  if (!catalog.teams.length) {
    ranking.append(element('p', '上传两份策略，开始第一组对战。', 'hint'));
  }
  catalog.teams.forEach((team, index) => {
    const row = element('div', undefined, 'ranking-row');
    row.append(
      element('small', String(index + 1).padStart(2, '0')),
      element('span', team.name),
      element('strong', team.elo.toFixed(1)),
    );
    ranking.append(row);
  });
}

function renderJobs() {
  const jobs = $('jobs');
  jobs.replaceChildren();
  if (!catalog.jobs.length) {
    jobs.append(element('div', '暂无任务。策略上传后即可创建对战。', 'empty'));
  }
  for (const job of catalog.jobs) {
    const row = element('div', undefined, 'job');
    const info = element('div', undefined, 'job-info');
    const description = job.count + ' 局 · 尝试 ' + job.attempt + ' · ' + job.id.slice(0, 8);
    info.append(
      element('b', teamName(job.a, 'A') + ' × ' + teamName(job.b, 'B')),
      element('small', description + (job.error ? ' · ' + job.error : '')),
    );
    row.append(info, element('span', STATUS[job.status] || job.status, 'badge ' + job.status));
    if (job.status === 'COMPLETED') {
      for (const [group, stats] of Object.entries(job.summary || {})) {
        const details = ['A', 'B'].map(side => {
          const team = stats.teams[side];
          return side + ' 均分 ' + team.average_score.toFixed(1) + ' · 胜 ' + (team.wins || 0) + '/' + team.games;
        });
        info.append(element('small', (group === 'fault' ? '故障组' : '常规组') + '：' + details.join(' / ')));
      }
      const actions = element('div', undefined, 'job-actions');
      job.result.forEach((result, index) => {
        const score = (result.scores?.red ?? '—') + ':' + (result.scores?.blue ?? '—');
        const button = element('button', '第 ' + (index + 1) + ' 局 ' + score);
        button.onclick = () => loadReplay(job, index);
        actions.append(button);
      });
      row.append(actions);
    }
    jobs.append(row);
  }
}

function renderCatalog(data) {
  if (JSON.stringify(catalog) === JSON.stringify(data)) return;
  catalog = data;
  renderSelectors();
  renderRanking();
  renderJobs();
}

$('upload-form').onsubmit = async event => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  try {
    const file = $('file').files[0];
    if (!file || !file.name.toLowerCase().endsWith('.py') || file.size > 262144) {
      throw Error('请选择不超过 256 KiB 的 .py 文件');
    }
    const source = new TextDecoder('utf-8', {fatal: true}).decode(await file.arrayBuffer());
    const result = await api('/api/upload', {
      team_name: $('team-name').value,
      filename: file.name,
      source,
    });
    $('upload-status').textContent = '版本已冻结：' + result.sha256.slice(0, 12) + ' · 语法与入口检查通过';
    notice('策略版本已上传');
    renderCatalog(await api('/api/state'));
  } catch (error) {
    notice(error.message);
  } finally {
    button.disabled = false;
  }
};

$('job-form').onsubmit = async event => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  try {
    await api('/api/jobs', {
      a_version: $('strategy-a').value,
      b_version: $('strategy-b').value,
      count: Number(document.querySelector('[name=count]:checked').value),
    });
    notice('任务已创建，双方策略版本已冻结');
    renderCatalog(await api('/api/state'));
  } catch (error) {
    notice(error.message);
  } finally {
    button.disabled = false;
  }
};

async function loadReplay(job, index) {
  const request = ++requestNumber;
  try {
    notice('正在载入比赛包…');
    const bundle = await api('/api/replay?job=' + encodeURIComponent(job.id) + '&index=' + index);
    if (request !== requestNumber) return;
    replay = bundle;
    // The scheduler puts A on red in the first leg of each pair, then swaps.
    const [red, blue] = index % 2 === 0 ? [job.a, job.b] : [job.b, job.a];
    replayTeams = {red: teamName(red, SIDE_NAMES.red), blue: teamName(blue, SIDE_NAMES.blue)};
    for (const side of SIDES) $('team-' + side).textContent = replayTeams[side];
    frame = 0;
    playtime = 0;
    setPlaying(false);
    $('timeline').max = replay.snapshots.length - 1;
    $('replay-label').textContent = job.id.slice(0, 8) + ' / 第 ' + (index + 1) + ' 局 / ' + replay.result.status;
    renderEvents();
    renderLedger();
    renderMetrics();
    showFrame(0);
    notice('比赛包已载入');
  } catch (error) {
    if (request === requestNumber) notice(error.message);
  }
}

function showFrame(index) {
  if (!replay) return;
  frame = Math.min(replay.snapshots.length - 1, Math.max(0, index));
  const snap = replay.snapshots[frame];
  const state = snap.state;
  $('timeline').value = frame;
  $('clock').textContent = (snap.sim_time_ms / 1000).toFixed(1).padStart(5, '0');
  for (const side of SIDES) $('score-' + side).textContent = state.score?.[side]?.total ?? '—';
  const eligibility = side => state.qualified?.[side] ? '✓' : '—';
  $('frame-detail').textContent = '帧 ' + snap.seq + ' · ' + snap.hash.slice(0, 12)
    + ' · 资格 R:' + eligibility('red') + ' B:' + eligibility('blue');
  draw(state);
}

function seekTime(time) {
  const index = replay.snapshots.findIndex(snap => snap.sim_time_ms >= time);
  showFrame(index < 0 ? replay.snapshots.length - 1 : index);
  playtime = replay.snapshots[frame].sim_time_ms;
}

function renderEvents() {
  const container = $('events');
  container.classList.remove('empty');
  container.replaceChildren();
  const events = [...replay.events];
  const known = new Set(events.map(event => event.id));
  for (const command of replay.commands) {
    for (const team of SIDES) {
      for (const receipt of command.receipts?.[team] || []) {
        if (known.has(receipt.id)) continue;
        known.add(receipt.id);
        events.push({...receipt, type: 'RECEIPT ' + receipt.code, team});
      }
      const response = command.responses?.[team];
      if (response && !response.ok) {
        events.push({type: 'POLICY_ERROR', occurred_at_ms: command.sim_time_ms, team, reason: response.error});
      }
    }
  }
  events.sort((a, b) => (a.occurred_at_ms || 0) - (b.occurred_at_ms || 0));
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
    const title = replayTeams[team] || team;
    container.append(element('h3', title, team === 'red' ? 'red-text' : 'blue-text'));
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
    for (const label of ['物品 / 落位', '分项', '分值']) header.append(element('th', label));
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
  showFrame(Number($('timeline').value));
  playtime = replay.snapshots[frame].sim_time_ms;
};

$('play').onclick = () => {
  if (!replay) return;
  if (frame === replay.snapshots.length - 1) {
    showFrame(0);
    playtime = 0;
  }
  setPlaying(!playing);
};

function animate(time) {
  const delta = Math.min(100, time - lastTime);
  lastTime = time;
  if (playing && replay) {
    playtime += delta * Number($('speed').value);
    let next = frame;
    while (next < replay.snapshots.length - 1 && replay.snapshots[next + 1].sim_time_ms <= playtime) next++;
    if (next !== frame) showFrame(next);
    if (frame === replay.snapshots.length - 1) setPlaying(false);
  }
  requestAnimationFrame(animate);
}

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
  const map = state.map || replay?.manifest?.map || {};
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

boot();

async function boot() {
  try {
    const data = await api('/api/bootstrap');
    token = data.token;
    const {teams, versions, jobs} = data;
    renderCatalog({teams, versions, jobs});
    setInterval(async () => {
      try {
        renderCatalog(await api('/api/state'));
        $('queue-label').textContent = '串行调度 · 版本已冻结';
      } catch {
        $('queue-label').textContent = '连接中断，比赛仍由服务器运行';
      }
    }, 1800);
  } catch (error) {
    notice(error.message);
  }
  draw({});
  requestAnimationFrame(animate);
}
