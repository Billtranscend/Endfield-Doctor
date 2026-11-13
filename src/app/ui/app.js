/* 界面逻辑：调后端 Api（pywebview），轮询任务，渲染报告 */
'use strict';

const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));

// ---------------------------------------------------------------- 后端调用
const hasPywebview = () => !!(window.pywebview && window.pywebview.api && typeof window.pywebview.api.version === 'function');
async function api(name, ...args) {
  if (hasPywebview()) return window.pywebview.api[name](...args);
  // 开发期：devserver.py 把 Api 挂在 /api/<name>
  const r = await fetch('/api/' + name, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(args) });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}
function ready() {
  // pywebview 先注入 window.pywebview（此时 api 还是空对象 {}），页面加载完才把方法填进 api 并触发 pywebviewready；
  // 它自己也用 http://127.0.0.1 供页，不能按协议判断，只能看方法有没有。开发期 devserver 给 <html> 打了 data-dev 标记，直接走 fetch。
  return new Promise(res => {
    if (hasPywebview()) return res();
    if (document.documentElement.dataset.dev) return res();
    window.addEventListener('pywebviewready', () => res(), { once: true });
    setTimeout(res, 8000);
  });
}

// ---------------------------------------------------------------- 状态
const state = { regionNames: ['武陵', '四号谷地'], blueprints: [], blueprint: localStorage.getItem('blueprint') || '', job: null, polling: null, lastReport: null, reports: [], reportId: '', reportFilter: 'all' };

function toast(msg) { const t = $('#toast'); t.textContent = msg; t.classList.add('show'); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), 2600); }

// ---------------------------------------------------------------- 弹窗（替代浏览器自带的 confirm，样式跟 OEA / MaaEnd 的对话框）
let _modalResolve = null;
// buttons：[{label, value, kind: 'primary'|'danger'|''}]；点遮罩、× 或 Esc 返回 null
function uiChoice(msg, buttons, title = '确认') {
  return new Promise(res => {
    if (_modalResolve) _modalResolve(null);
    _modalResolve = res;
    $('#modalTitle').textContent = title;
    $('#modalBody').innerHTML = esc(msg).replace(/\n/g, '<br>');
    $('#modalFoot').innerHTML = buttons.map((b, i) => `<button class="btn ${b.kind || ''}" data-i="${i}">${esc(b.label)}</button>`).join('');
    $('#modalFoot').querySelectorAll('button').forEach(el => el.onclick = () => modalClose(buttons[+el.dataset.i].value));
    $('#modal').hidden = false;
    const def = $('#modalFoot').querySelector('.primary, .danger'); if (def) def.focus();
  });
}
function uiConfirm(msg, { ok = '确定', cancel = '取消', danger = false, title = '确认' } = {}) {
  return uiChoice(msg, [{ label: cancel, value: false }, { label: ok, value: true, kind: danger ? 'danger solid' : 'primary' }], title).then(v => v === true);
}
function modalClose(v) { $('#modal').hidden = true; const r = _modalResolve; _modalResolve = null; if (r) r(v); }
function lightbox(src, cap) { $('#lightboxImg').src = src; $('#lightboxCap').textContent = cap || ''; $('#lightbox').hidden = false; }
function lightboxClose() { $('#lightbox').hidden = true; $('#lightboxImg').src = ''; }
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  if (!$('#lightbox').hidden) lightboxClose(); else if (!$('#modal').hidden) modalClose(null);
});
$('#modal').addEventListener('mousedown', e => { if (e.target.id === 'modal') modalClose(null); });
// 报告里的定位图 / 对照图：点一下看大图
document.addEventListener('click', e => {
  const img = e.target.closest('.imgs img, .zoomable');
  if (!img) return;
  const cap = img.closest('figure')?.querySelector('figcaption')?.textContent || img.alt || '';
  lightbox(img.src, cap);
});

// ---------------------------------------------------------------- 导航
const TITLES = { home: '总览', blueprint: '蓝图', bpa: '蓝图分析', quick: '快速诊断', patrol: '深入巡检', reports: '报告', settings: '设置', help: '帮助' };
function go(page) {
  document.querySelectorAll('.nav-item').forEach(b => b.classList.toggle('active', b.dataset.page === page));
  document.querySelectorAll('.page').forEach(p => p.classList.toggle('active', p.id === 'page-' + page));
  $('#crumb').textContent = TITLES[page];
  if (page === 'reports') loadReports();
  if (page === 'home') loadRecent();
  if (page === 'bpa') bpaLoad();
  if (page === 'help' && !$('#helpBody').innerHTML) renderHelpPage();
}
$('#nav').addEventListener('click', e => { const b = e.target.closest('.nav-item'); if (b) go(b.dataset.page); });

// ---------------------------------------------------------------- 游戏 / 蓝图
async function refreshGame() {
  const s = await api('game_status');
  $('#gameDot').className = 'dot ' + (s.connected ? 'on' : 'off');
  $('#gameText').textContent = s.connected ? `游戏：${s.scene}` : '游戏未连接';
  $('#homeWindow').textContent = s.connected ? s.window : '—';
  $('#homeScene').textContent = s.connected ? s.scene : '—';
  $('#homeHint').textContent = s.hint || (s.connected ? '快速诊断要求停在大世界；深入巡检会自动传送。' : '');
}

function currentBp() { return state.blueprints.find(b => b.name === state.blueprint) || state.blueprints[0]; }

async function loadBlueprints() {
  try { const rn = await api('known_regions'); if (Array.isArray(rn) && rn.length) state.regionNames = rn; } catch (e) { /* 用默认的两个地区 */ }
  state.blueprints = await api('blueprints');
  if (!state.blueprints.find(b => b.name === state.blueprint)) state.blueprint = state.blueprints[0]?.name || '';
  renderBlueprints();
}
function selectBlueprint(name) { state.blueprint = name; localStorage.setItem('blueprint', name); renderBlueprints(); toast('蓝图：' + name); }
function renderBlueprints() {
  const bp = currentBp();
  $('#sideBlueprint').textContent = bp ? bp.name : '—';
  $('#homeBlueprint').textContent = bp ? bp.name : '没有蓝图档案';
  $('#homeBlueprintNote').textContent = bp ? Object.entries(bp.regions).map(([r, as]) => `${r}：${as.map(a => a.name).join('、')}`).join('；') : '';
  $('#bpList').innerHTML = state.blueprints.map(b => `
    <div class="bp ${b.name === state.blueprint ? 'sel' : ''}" onclick="selectBlueprint('${esc(b.name)}')">
      <div class="name">${esc(b.name)}</div>
      <div class="sub">${Object.entries(b.regions).map(([r, as]) => `${esc(r)} / ${as.map(a => esc(a.name)).join('、')}`).join('<br>')}<br>
      ${b.has_reference ? '有参考简报（可判缺矿缺气液）' : '无参考简报'}</div>
    </div>`).join('') || '<div class="empty">data/blueprints 下没有蓝图档案</div>';
  if (bp) {
    $('#bpDetail').innerHTML = `<div class="card-h"><span>${esc(bp.name)}</span>
        <span class="btn-row inline">
          <button class="btn sm" onclick="exportBlueprint()">导出 zip</button>
          <button class="btn sm" onclick="updateReference()" ${state.job?.state === 'running' ? 'disabled' : ''}>更新基准</button>${helpIcon('reference')}
          <button class="btn sm danger" onclick="deleteBlueprint()">删除</button>
        </span></div>
      <div class="hint">${bp.has_reference ? '有参考简报（基准）：能判缺矿缺气液；没有备路规则的产物按基准比。' : '<span class="bad-text">没有参考简报</span>：判不了缺矿缺气液。基地跑正常时点「更新基准」抓一份。'}
        基地改动过、矿机/水泵/气口数量变了之后，趁基地正常时点「更新基准」。</div>
      <pre class="log sm" id="refLog" style="display:none"></pre>
      <div class="sub-h"><b>产能自检目标</b>（/分钟，${bp.targets.length} 项）</div>
      <table class="md"><tr><th>产物</th><th>目标</th><th>备注</th></tr>
      ${bp.targets.map(t => `<tr><td>${esc(t.product)}</td><td class="mono">${t.per_min}</td><td class="muted">${esc(t.note || '')}</td></tr>`).join('') || '<tr><td colspan="3" class="muted">没有目标：报告里不做产能自检</td></tr>'}</table>
      <div class="hint">${esc(bp.notes || '')}</div>
      <div class="sub-h"><b>核心取货口</b>（协议核心/次级核心东西两侧 6 个，俯瞰看不出设的物品；在「深入巡检」页点「读核心取货口」读）</div>
      ${Object.keys(bp.core_ports || {}).length ? Object.entries(bp.core_ports).map(([a, s]) => `<div class="hint"><b>${esc(a)}</b>：${esc(s)}</div>`).join('') : '<div class="hint muted">还没读过</div>'}
      <div class="sub-h"><b>两种植物轮换</b>（种植机/采种机在两种植物之间来回切，${(bp.rotating_plants || []).length} 组）</div>
      <div class="hint">登记后，巡检看到一台种植机/采种机空闲或阻塞、而旁边的搭档这会儿正在做这组里的另一种植物时，按设计内处理。
        植物写游戏里的显示名（锦草、芽针…）；分区不填 = 所有分区。巡检报告里提示「以前读到过几种植物」的设备，核对后在这里登记。</div>
      <table class="md" id="rotTable"><tr><th>植物 A</th><th>植物 B</th><th>分区</th><th>说明</th><th></th></tr>
      ${(bp.rotating_plants || []).map((x, i) => `<tr><td>${esc(x.plants[0])}</td><td>${esc(x.plants[1])}</td><td>${esc(x.area || '全部')}</td>
        <td class="muted">${esc(x.note || '')}</td><td><button class="btn sm ghost" onclick="delRotation(${i})">删</button></td></tr>`).join('')}
      <tr><td><input id="rotA" placeholder="锦草" size="6"></td><td><input id="rotB" placeholder="芽针" size="6"></td>
        <td><input id="rotArea" placeholder="武陵城" size="6"></td><td><input id="rotNote" placeholder="说明（可不填）" size="14"></td>
        <td><button class="btn sm" onclick="addRotation()">加</button></td></tr></table>
      <div class="sub-h"><b>跨地区传输</b>（可选，${(bp.transfers || []).length} 条）${helpIcon('transfers')}</div>
      <div class="hint">协议核心 → 协议管理里开着的跨地区传输。简报不把传进来的量算成产量，登记后终点地区这些物品「消耗大于产出」按正常处理，库存见底时提醒去看传输还开着没有。程序不读、也不会去点这个界面。</div>
      <table class="md" id="xferTable"><tr><th>物品</th><th>起点</th><th>终点</th><th>模式</th><th>说明</th><th></th></tr>
      ${(bp.transfers || []).map((x, i) => `<tr><td>${esc(x.item)}</td><td>${esc(x.from || '')}</td><td>${esc(x.to)}</td><td>${esc(x.mode || '超库存')}传输</td>
        <td class="muted">${esc(x.note || '')}</td><td><button class="btn sm ghost" onclick="delTransfer(${i})">删</button></td></tr>`).join('')}
      <tr><td><input id="xferItem" placeholder="蓝铁块" size="7"></td>
        <td><select id="xferFrom">${state.regionNames.map(r => `<option ${r === '四号谷地' ? 'selected' : ''}>${esc(r)}</option>`).join('')}</select></td>
        <td><select id="xferTo">${state.regionNames.map(r => `<option ${r === '武陵' ? 'selected' : ''}>${esc(r)}</option>`).join('')}</select></td>
        <td><select id="xferMode"><option value="超库存">超库存传输</option><option value="库存">库存传输</option></select></td>
        <td><input id="xferNote" placeholder="说明（可不填）" size="12"></td>
        <td><button class="btn sm" onclick="addTransfer()">加</button></td></tr></table>`;
    $('#quickRegions').innerHTML = Object.keys(bp.regions).map(r => `<label class="check"><input type="checkbox" value="${esc(r)}" checked><span>${esc(r)}</span></label>`).join('');
    $('#patrolAreas').innerHTML = Object.entries(bp.regions).flatMap(([r, as]) => as.map(a => `<label class="check"><input type="checkbox" value="${esc(a.name)}" checked><span>${esc(a.name)}</span>${a.main ? '<span class="tag">主分区</span>' : ''}</label>`)).join('');
    loadBaselines(Object.values(bp.regions).flat().map(a => a.name));
  }
}

// ---------------------------------------------------------------- 蓝图档案：导入 / 导出 / 基准 / 新建向导
async function importBlueprint(kind) {
  let r = await api('import_blueprint', kind);
  if (!r.ok) return toast(r.error);
  if (r.choose) {  // 作者的蓝图分析 zip，且已有分区对得上的档案：合并进去（保留错题本、参考简报）还是新建
    const btns = [{ label: '取消', value: null }, { label: '新建档案', value: '' },
      ...r.candidates.map((c, i) => ({ label: `合并进「${c}」`, value: c, kind: i === 0 ? 'primary' : '' }))];
    const into = await uiChoice(`这是作者「${r.info.name}」导出的蓝图分析包（据点：${r.info.groups.join('、')}）。

合并进现有档案：补上底账、配方表、准入口限速和作者确认的设计内设备，档案原有的错题本、参考简报、备路规则都保留（推荐）。
新建档案：单独成一套，没有错题本和参考简报，要自己再抓基准。`, btns, '导入蓝图分析包');
    if (into === null) return;
    toast('导入中…（包较大，约半分钟）');
    r = await api('import_bpa', r.path, into);
    if (!r.ok) return toast(r.error);
  }
  await loadBlueprints(); selectBlueprint(r.name); toast((r.merged ? '已合并进：' : '已导入：') + r.name);
}
async function exportBlueprint() {
  const r = await api('export_blueprint', state.blueprint);
  toast(r.ok ? '已导出：' + r.path : r.error);
}
async function deleteBlueprint() {
  if (state.blueprints.length <= 1) return toast('这是最后一套档案，不能删');
  if (!(await uiConfirm(`删除蓝图档案「${state.blueprint}」？档案文件会被删掉，不可恢复。`, { ok: '删除', danger: true, title: '删除档案' }))) return;
  const r = await api('delete_blueprint', state.blueprint);
  if (!r.ok) return toast(r.error);
  state.blueprint = ''; await loadBlueprints(); toast('已删除');
}
async function updateReference() {
  const bp = currentBp();
  let merge = false;
  if (bp && bp.has_reference) {
    const how = await uiChoice(`「${state.blueprint}」已有基准。

合并：同一蓝图再抓一次，和现有基准取最大值（备路产物时断时续，多抓几次才全）。
替换：换过蓝图后用，旧基准整份丢掉。`, [{ label: '取消', value: null }, { label: '替换', value: 'replace' }, { label: '合并', value: 'merge', kind: 'primary' }], '更新基准');
    if (!how) return;
    merge = how === 'merge';
  }
  if (!(await uiConfirm(`现在抓一份简报${merge ? '合并进' : '存为'}「${state.blueprint}」的基准？
要求：基地此刻运转正常、游戏停在大世界。约 1.5 分钟。`, { ok: '开始', title: '更新基准' }))) return;
  const r = await api('start_update_reference', state.blueprint, merge);
  if (!r.ok) return toast(r.error);
  $('#refLog').style.display = ''; $('#refLog').textContent = '';
  startPolling('reference');
}

const wiz = { draft: null };
function openWizard() { $('#bpWizard').style.display = ''; renderWizardStart(); $('#bpWizard').scrollIntoView({ behavior: 'smooth' }); }
function closeWizard() { $('#bpWizard').style.display = 'none'; wiz.draft = null; }
function wizSteps(n) { return `<div class="wiz-steps">${['1 起名 · 选地区', '2 程序识别', '3 核对 · 保存'].map((t, i) => `<span class="wiz-step ${i + 1 === n ? 'active' : i + 1 < n ? 'done' : ''}">${t}</span>`).join('')}</div>`; }
async function renderWizardStart() {
  const regions = await api('known_regions');
  $('#bpWizard').innerHTML = `<div class="card-h"><span>新建蓝图档案</span><button class="btn ghost sm" onclick="closeWizard()">关闭</button></div>
    ${wizSteps(1)}
    <div class="form-row"><label>档案名</label><input class="input" id="wizName" placeholder="例如：1.7 某某产线" value="${esc(wiz.draft?.name || '')}"></div>
    <div class="form-row"><label>涉及地区</label><div class="checks" id="wizRegions">${regions.map(r => `<label class="check"><input type="checkbox" value="${esc(r)}" checked><span>${esc(r)}</span></label>`).join('')}</div></div>
    <div class="hint">开始前：基地按蓝图搭好并<b>正在正常运转</b>（这次读到的简报会成为「正常基准」），游戏停在大世界。
      程序会自动读简报（约 1.5 分钟），再打开地图逐个分区找基地（每个地区 1–3 分钟）。期间不要碰电脑。</div>
    <div class="btn-row"><button class="btn primary" id="wizStart" onclick="startWizardSurvey()">开始识别</button>
      <button class="btn danger" id="wizCancel" onclick="cancelJob()" disabled>取消</button><span class="muted" id="wizStep"></span></div>
    <pre class="log sm" id="wizLog" style="display:none"></pre>
    <div id="wizActions"></div>`;
}
async function startWizardSurvey() {
  const name = $('#wizName').value.trim();
  if (!name) return toast('先给档案起个名');
  if (state.blueprints.some(b => b.name === name)) return toast('已有同名档案，换个名字');
  const regions = checked('#wizRegions');
  if (!regions.length) return toast('至少选一个地区');
  wiz.draft = { name };
  const r = await api('start_blueprint_survey', name, regions);
  if (!r.ok) return toast(r.error);
  $('#bpWizard').querySelector('.wiz-steps').outerHTML = wizSteps(2);
  startPolling('survey');
}
function renderWizardConfirm(result) {
  wiz.draft = result;
  const regionsHtml = Object.entries(result.regions).map(([region, areas]) => `
    <div class="sub-h"><b>${esc(region)}</b> · 勾选属于这套蓝图的分区，并指定一个主分区（协议核心所在）</div>
    ${areas.length ? `<table class="md"><tr><th></th><th>分区</th><th>找到的基地</th><th>主分区</th></tr>
    ${areas.map((a, i) => `<tr class="${a.selected ? '' : 'off'}">
      <td><input type="checkbox" data-r="${esc(region)}" data-i="${i}" class="wizArea" ${a.selected ? 'checked' : ''}></td>
      <td>${esc(a.name)}</td>
      <td class="${a.has_base ? 'ok-text' : 'muted'}">${a.has_base ? esc(a.title) : '默认视野里没找到基地（基地在视野外或图标重叠时也会这样；确有基地可手动勾上）'}</td>
      <td><input type="radio" name="wizMain" data-r="${esc(region)}" data-i="${i}" ${a.main ? 'checked' : ''}></td></tr>`).join('')}</table>`
      : '<div class="empty">这个地区没识别到分区（地区总览没打开或没认到文字）。可以先保存，之后再识别。</div>'}`).join('');
  const targetsHtml = `<table class="md" id="wizTargets"><tr><th></th><th>产物</th><th>目标 /分钟</th><th>依据</th></tr>
    ${result.targets.map((t, i) => `<tr class="${t.selected ? '' : 'off'}"><td><input type="checkbox" class="wizTgt" data-i="${i}" ${t.selected ? 'checked' : ''}></td>
      <td>${esc(t.product)}</td><td><input class="input num" data-i="${i}" value="${t.per_min}"></td><td class="muted">${esc(t.note)}</td></tr>`).join('')}</table>
    <div class="form-row"><label>补一项</label><select class="input" id="wizAddSel">${result.products.map((p, i) => `<option value="${i}">${esc(p.product)}（${esc(p.region)}，当前 ${p.cur}/理论 ${p.theo}）</option>`).join('')}</select>
      <button class="btn sm" onclick="wizardAddTarget()">加入目标</button></div>`;
  $('#bpWizard').innerHTML = `<div class="card-h"><span>新建蓝图档案 · ${esc(result.name)}</span><button class="btn ghost sm" onclick="closeWizard()">关闭</button></div>
    ${wizSteps(3)}
    ${regionsHtml}
    <div class="sub-h"><b>最终产物与目标产量</b> · 报告里的「产能自检」拿实际产量和这里比。程序从基准简报推的：产大于耗的固体产物；标灰的请核对数字后再勾</div>
    ${targetsHtml}
    <div class="hint">基准简报：${esc(result.brief.split(/[\\/]/).pop())}（会存为档案的参考简报）。备路规则不用填：没有规则时，产物在基准里就没跑满的，现在不低于基准就算正常。</div>
    <div class="btn-row"><button class="btn primary" onclick="saveWizard()">保存档案</button><button class="btn" onclick="renderWizardStart()">重新识别</button></div>`;
  $('#bpWizard').scrollIntoView({ behavior: 'smooth' });
}
function wizardAddTarget() {
  const p = wiz.draft.products[+$('#wizAddSel').value];
  if (!p) return;
  if (wiz.draft.targets.some(t => t.product === p.product && t.region === p.region)) return toast('已在列表里');
  collectWizard();
  wiz.draft.targets.push({ product: p.product, per_min: p.cur || p.theo, note: `${p.region}；手动加入（当前 ${p.cur}/理论 ${p.theo}）`, region: p.region, selected: true });
  renderWizardConfirm(wiz.draft);
}
function collectWizard() {
  const d = wiz.draft;
  document.querySelectorAll('.wizArea').forEach(el => { d.regions[el.dataset.r][+el.dataset.i].selected = el.checked; });
  Object.values(d.regions).forEach(as => as.forEach(a => a.main = false));
  const m = document.querySelector('input[name=wizMain]:checked'); if (m) d.regions[m.dataset.r][+m.dataset.i].main = true;
  document.querySelectorAll('.wizTgt').forEach(el => { d.targets[+el.dataset.i].selected = el.checked; });
  document.querySelectorAll('#wizTargets .input.num').forEach(el => { const v = parseFloat(el.value); if (!isNaN(v)) d.targets[+el.dataset.i].per_min = v; });
  return d;
}
async function saveWizard() {
  const d = collectWizard();
  const nAreas = Object.values(d.regions).flat().filter(a => a.selected).length;
  if (!nAreas) return toast('至少勾一个分区');
  if (!Object.values(d.regions).flat().some(a => a.selected && a.main)) { if (!(await uiConfirm('没有指定主分区（协议核心所在）。地区级的简报问题会落不到分区上。仍要保存？', { ok: '仍要保存' }))) return; }
  const r = await api('save_blueprint', d);
  if (!r.ok) return toast(r.error);
  await loadBlueprints(); selectBlueprint(r.name); closeWizard(); toast('已保存档案：' + r.name);
}

// ---------------------------------------------------------------- 任务
function checked(sel) { return [...document.querySelectorAll(sel + ' input:checked')].map(i => i.value); }

async function startQuick() {
  const regions = checked('#quickRegions');
  if (!regions.length) return toast('至少选一个地区');
  $('#quickResult').innerHTML = '';
  const r = await api('start_quick', state.blueprint, regions);
  if (!r.ok) return toast(r.error);
  startPolling('quick');
}
// 近景巡检的结构基线（任务计划第 8 项）：看每个分区存了几台、什么时候存的；基地改过布局后「重设」
async function loadBaselines(areas) {
  if (!state.blueprint || !areas || !areas.length) return;
  const r = await api('near_baselines', state.blueprint, areas);
  if (!r.ok) { $('#baselineList').innerHTML = `<div class="empty">${esc(r.error)}</div>`; return; }
  $('#baselineList').innerHTML = `<table class="bpa-table"><tr><th>分区</th><th>基线</th><th>存的时间</th><th></th></tr>${r.areas.map(a =>
    `<tr><td>${esc(a.area)}</td><td>${a.count ? a.count + ' 台' : '<span class="muted">还没有（下次巡检自动存）</span>'}</td>
      <td class="muted">${a.count ? esc(a.oldest === a.newest ? a.oldest : a.oldest + ' ～ ' + a.newest) : ''}</td>
      <td>${a.count ? `<button class="btn sm ghost" onclick="resetBaseline('${esc(a.area)}')">重设</button>` : ''}</td></tr>`).join('')}</table>`;
  loadBaselines.areas = areas;
}
async function resetBaseline(area) {
  if (!(await uiConfirm(`重设「${area}」的结构基线？旧基线挪进备份目录（不删），下次近景巡检按当时的画面重新存——请确认基地现在是正常的。`, { ok: '重设', title: '重设基线' }))) return;
  const r = await api('reset_near_baseline', state.blueprint, area);
  if (!r.ok) return toast(r.error);
  toast(r.moved ? `已挪走 ${r.moved} 个基线文件到备份` : '这个分区还没有基线');
  loadBaselines(loadBaselines.areas);
}
// 两种植物轮换（任务计划第 13 项）：整张表覆盖保存
async function saveRotations(pairs) {
  const r = await api('save_rotating_plants', state.blueprint, pairs);
  if (!r.ok) return toast(r.error);
  toast('已保存'); await loadBlueprints();
}
function addRotation() {
  const a = $('#rotA').value.trim(), b = $('#rotB').value.trim();
  if (!a || !b) return toast('两种植物都要填');
  saveRotations([...(currentBp().rotating_plants || []), { plants: [a, b], area: $('#rotArea').value.trim(), note: $('#rotNote').value.trim() }]);
}
function delRotation(i) { const l = [...(currentBp().rotating_plants || [])]; l.splice(i, 1); saveRotations(l); }
// 跨地区传输（可选设置）：整张表覆盖保存
async function saveTransfers(rows) {
  const r = await api('save_transfers', state.blueprint, rows);
  if (!r.ok) return toast(r.error);
  toast('已保存'); await loadBlueprints();
}
function addTransfer() {
  const item = $('#xferItem').value.trim(), from = $('#xferFrom').value, to = $('#xferTo').value;
  if (!item) return toast('填物品名（游戏里的显示名）');
  if (from === to) return toast('起点和终点要是两个不同的地区');
  saveTransfers([...(currentBp().transfers || []), { item, from, to, mode: $('#xferMode').value, note: $('#xferNote').value.trim() }]);
}
function delTransfer(i) { const l = [...(currentBp().transfers || [])]; l.splice(i, 1); saveTransfers(l); }
async function startPatrol() {
  const areas = checked('#patrolAreas');
  if (!areas.length) return toast('至少选一个分区');
  $('#patrolResult').innerHTML = '';
  const r = await api('start_patrol', state.blueprint, areas, $('#patrolBrief').checked);
  if (!r.ok) return toast(r.error);
  startPolling('patrol');
}
async function startGateLimits() {
  const areas = checked('#patrolAreas');
  if (!areas.length) return toast('至少选一个分区');
  if (!(await uiConfirm(`读 ${areas.join('、')} 的准入口限速：进俯瞰拉近，逐个点开准入口面板读数后关掉（不点开关、±6、替换物品）。每个分区约 3–6 分钟，期间不要碰键鼠。`, { ok: '开始', title: '读准入口限速' }))) return;
  $('#patrolResult').innerHTML = '';
  const r = await api('start_gate_limits', state.blueprint, areas);
  if (!r.ok) return toast(r.error);
  startPolling('gates');
}
async function startCorePorts() {
  const areas = checked('#patrolAreas');
  if (!areas.length) return toast('至少选一个分区');
  if (!(await uiConfirm(`读 ${areas.join('、')} 的核心取货口：传送到基地、进俯瞰，逐个点开 6 个取货口面板读物品后关掉（不点添加/替换物品）。每个分区约 1 分钟，期间不要碰键鼠。`, { ok: '开始', title: '读核心取货口' }))) return;
  $('#patrolResult').innerHTML = '';
  const r = await api('start_core_ports', state.blueprint, areas);
  if (!r.ok) return toast(r.error);
  startPolling('cores');
}
function renderCorePorts(res) {
  return Object.entries(res || {}).map(([area, a]) => {
    const rows = a.rows || [];
    const body = rows.map(r => `<tr class="${r.error || !r.configured ? 'muted' : ''}"><td>${r.port ? r.port + ' 号' : '?'}</td>
      <td>${r.error ? '' : r.configured ? '<b>' + esc(r.item || '?') + '</b>' : '没设'}</td><td>${r.configured ? (r.stock ?? '?') : ''}</td>
      <td>${esc(r.error || r.status || '')}</td></tr>`).join('');
    return `<div class="card"><div class="card-h"><span>${esc(area)} · 核心取货口</span>${a.error ? `<span class="bad">${esc(a.error)}</span>` : ''}</div>
      <div class="hint">有没有接带子面板上看不出来（没接带子也显示「运行中」）；截图在 debug/shots 里。</div>
      <table class="bpa-st"><tr><th>取货口</th><th>物品</th><th>仓库存量</th><th>状态 / 说明</th></tr>${body}</table></div>`;
  }).join('');
}
function renderGateLimits(res) {
  const fmt = v => (v === null || v === undefined) ? '?' : v;
  return Object.entries(res || {}).map(([area, a]) => {
    const rows = a.rows || [];
    const ok = rows.filter(r => !r.error);
    const body = rows.map(r => `<tr class="${r.error ? 'muted' : ''}"><td>${r.gate_no ? '#' + r.gate_no : ''}</td><td>${esc(r.kind || '')}</td><td>${esc(r.item || '')}</td>
      <td>${r.error ? '' : (r.limit_on === true ? '开 ' + fmt(r.limit) : r.limit_on === false ? '关' : '? ' + fmt(r.limit))}</td><td>${r.error ? '' : fmt(r.speed)}</td><td>${r.error ? '' : r.count_on ? `<b class="bad" title="限制通过数量：过 ${fmt(r.count_limit)} 个后不再放行；累计已过 ${fmt(r.passed)}">限量 ${fmt(r.count_limit)}</b>${r.passed != null && r.count_limit != null && r.passed >= r.count_limit ? ' <small>已满</small>' : ''}` : r.count_on === false ? '关' : '?'}</td>
      <td>${r.rate ?? ''}</td><td>${esc(r.error || r.status || '')}</td><td><small>${r.p ? r.p.join(',') : ''}</small></td></tr>`).join('');
    return `<div class="card"><div class="card-h"><span>${esc(area)} · 准入口限速（读到 ${ok.length}/${rows.length}）</span>${a.error ? `<span class="bad">${esc(a.error)}</span>` : ''}</div>
      <table class="bpa-st"><tr><th>输入端</th><th>类型</th><th>物品（作者/自动）</th><th>限制速度</th><th>当前/分</th><th>限制通过数量</th><th>作者填的流速</th><th>状态 / 说明</th><th>拼合图位置</th></tr>${body}</table></div>`;
  }).join('');
}
async function cancelJob() { await api('cancel'); toast('已请求取消'); }

function startPolling(kind) {
  clearInterval(state.polling);
  state.polling = setInterval(pollJob, 1000);
  pollJob();
}
function setRunningUI(running, kind) {
  $('#quickStart').disabled = running; $('#patrolStart').disabled = running;
  const ws = $('#wizStart'); if (ws) ws.disabled = running;
  const wc = $('#wizCancel'); if (wc) wc.disabled = !(running && kind === 'survey');
  $('#quickCancel').disabled = !(running && kind === 'quick'); $('#patrolCancel').disabled = !(running && (kind === 'patrol' || kind === 'gates' || kind === 'cores'));
  if ($('#gateLimitStart')) $('#gateLimitStart').disabled = running;
  if ($('#coreStart')) $('#coreStart').disabled = running;
  $('#bpaRead').disabled = running; $('#bpaFit').disabled = running; bpaLedgerState(); $('#bpaCancel').disabled = !(running && kind === 'bpa');
}
async function pollJob() {
  const j = await api('job_status');
  state.job = j;
  const running = j.state === 'running';
  setRunningUI(running, j.kind);
  const KIND = { quick: '快速诊断', patrol: '深入巡检', survey: '识别蓝图', reference: '更新基准', bpa: '蓝图分析', gates: '读准入口限速', cores: '读核心取货口' };
  const label = running ? `${KIND[j.kind] || j.kind} · ${j.step} · ${fmtSec(j.elapsed)}` : (j.state === 'idle' ? '空闲' : `${j.state === 'done' ? '完成' : j.state === 'error' ? '出错' : '已取消'} · ${fmtSec(j.elapsed)}`);
  $('#jobChip').textContent = label; $('#jobChip').className = 'chip' + (running ? ' running' : '');
  $('#sideJob').textContent = running ? j.step : (j.state === 'idle' ? '空闲' : label);
  const logEl = { quick: $('#quickLog'), patrol: $('#patrolLog'), gates: $('#patrolLog'), cores: $('#patrolLog'), survey: $('#wizLog'), reference: $('#refLog'), bpa: $('#bpaLog') }[j.kind];
  if (logEl) { logEl.style.display = ''; const atEnd = logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 20; logEl.textContent = j.log.join('\n'); if (atEnd) logEl.scrollTop = logEl.scrollHeight; }
  const stepEl = { quick: $('#quickStep'), patrol: $('#patrolStep'), gates: $('#patrolStep'), cores: $('#patrolStep'), survey: $('#wizStep'), bpa: $('#bpaStep') }[j.kind];
  if (stepEl) stepEl.textContent = running ? j.step : '';
  if (j.kind === 'patrol') {
    $('#patrolAreaState').innerHTML = Object.entries(j.areas).map(([a, s]) => `<span class="area-pill ${s === '进行中' ? 'run' : s === '完成' ? 'ok' : s.startsWith('失败') ? 'bad' : ''}" title="${esc(s)}">${esc(a)} · ${esc(s.split('（')[0])}</span>`).join('');
  }
  if (!running && state.polling) {
    clearInterval(state.polling); state.polling = null;
    if (j.state === 'error') toast('任务出错：' + j.error);
    if (j.report_id) { const rep = await api('report', j.report_id); if (rep) { state.lastReport = rep; (rep.kind === 'quick' ? $('#quickResult') : $('#patrolResult')).innerHTML = renderReport(rep); } }
    if (j.kind === 'survey' && j.state === 'done' && j.result) renderWizardConfirm(j.result);
    if (j.kind === 'survey' && j.state !== 'done' && $('#wizActions')) $('#wizActions').innerHTML = `<button class="btn" onclick="renderWizardStart()">返回重试</button>`;
    if (j.kind === 'reference' && j.state === 'done') { await loadBlueprints(); toast('基准已更新'); }
    if (j.kind === 'bpa') { await bpaRefresh(); }
    if (j.kind === 'gates' && j.result) $('#patrolResult').innerHTML = renderGateLimits(j.result);
    if (j.kind === 'cores' && j.result) $('#patrolResult').innerHTML = renderCorePorts(j.result);
    if (j.state !== 'idle') toast(j.state === 'done' ? '任务完成' : j.state === 'cancelled' ? '已取消' : '任务出错');
  }
}
function fmtSec(s) { s = Math.round(s || 0); return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m${String(s % 60).padStart(2, '0')}s`; }

// ---------------------------------------------------------------- 报告列表
async function loadReports() {
  state.reports = await api('reports');
  renderReportList();
  if (!state.reportId && state.reports.length) openReport(state.reports[0].id, true);  // 进报告页默认打开最新一份
}
function setReportFilter(f) {
  state.reportFilter = f;
  document.querySelectorAll('#reportFilter button').forEach(b => b.classList.toggle('on', b.dataset.f === f));
  renderReportList();
}
function renderReportList() {
  const f = state.reportFilter || 'all';
  const rs = state.reports.filter(r => f === 'all' || r.kind === f);
  $('#reportList').innerHTML = rs.map(reportItem).join('') || '<div class="empty">还没有报告。先跑一次快速诊断或深入巡检。</div>';
}
// 左栏紧凑条目（MaaEnd 任务卡式）：类型 + 时间一行，范围 + 计数一行
function reportItem(r) {
  const when = r.stamp ? `${r.stamp.slice(4, 6)}-${r.stamp.slice(6, 8)} ${r.stamp.slice(9, 11)}:${r.stamp.slice(11, 13)}` : '';
  const scope = r.kind === 'patrol' ? (r.areas || []).map(a => a.area).join('、') : (r.blueprint || '');
  return `<div class="ritem ${r.id === state.reportId ? 'sel' : ''}" onclick="openReport('${r.id}')">
    <div class="ritem-1"><span class="kind ${r.kind}">${r.kind === 'patrol' ? '深入巡检' : '快速诊断'}</span><span class="when">${when}</span></div>
    <div class="ritem-2"><span class="scope">${esc(scope)}</span>${reportCounts(r)}</div></div>`;
}
// 列表里的计数：深入巡检数「问题」；快速诊断数「要看」= 排查树 ✗ + 缺矿缺气液。没有就写「正常」，待确认是 0 就不显示
function reportCounts(r) {
  const patrol = r.kind === 'patrol';
  const bad = patrol ? (r.problems || 0) : (r.problems || 0) + (r.tree_bad || 0);
  const pend = patrol ? (r.areas || []).reduce((s, a) => s + (a.counts?.['待确认'] || 0), 0) : (r.pending || 0);
  const failed = patrol ? (r.areas || []).filter(a => a.ok === false).length : 0;
  const head = bad ? `<span class="p">${patrol ? '问题' : '要看'} ${bad}</span>` : failed ? `<span class="w">${failed} 区没巡成</span>` : `<span class="n">正常</span>`;
  return `<span class="cnt">${head}${pend ? `<span class="w">待确认 ${pend}</span>` : ''}</span>`;
}
async function loadRecent() {
  const rs = await api('reports');
  $('#homeRecent').innerHTML = rs.slice(0, 5).map(reportRow).join('') || '<div class="empty">还没有报告</div>';
}
function reportRow(r) {
  const when = r.stamp ? `${r.stamp.slice(4, 6)}-${r.stamp.slice(6, 8)} ${r.stamp.slice(9, 11)}:${r.stamp.slice(11, 13)}` : '';
  const what = r.kind === 'patrol' ? `深入巡检 <small>${(r.areas || []).map(a => a.area).join('、')}</small>` : `快速诊断 <small>${esc(r.blueprint || '')}</small>`;
  return `<div class="row" onclick="openReport('${r.id}')"><span class="when">${when}</span><span class="what">${what}</span>${reportCounts(r)}</div>`;
}
async function openReport(id, quiet) {
  const rep = await api('report', id);
  if (!rep) return toast('报告不存在');
  state.reportId = id;
  if (!$('#page-reports').classList.contains('active')) go('reports');
  if (state.reports.length) renderReportList();
  const when = rep.stamp ? `${rep.stamp.slice(0, 4)}-${rep.stamp.slice(4, 6)}-${rep.stamp.slice(6, 8)} ${rep.stamp.slice(9, 11)}:${rep.stamp.slice(11, 13)}` : '';
  const scope = rep.kind === 'patrol' ? (rep.areas || []).map(a => a.area).join('、') : (rep.blueprint || '');
  $('#reportView').innerHTML = `<div class="report-head"><span class="kind ${rep.kind}">${rep.kind === 'patrol' ? '深入巡检' : '快速诊断'}</span><b>${esc(scope)}</b><span class="muted mono">${when}</span></div>` + renderReport(rep);
  if (!quiet) $('#page-reports').scrollTop = 0;
}

// ---------------------------------------------------------------- 渲染
function renderReport(rep) {
  return rep.kind === 'quick' ? renderQuick(rep) : renderPatrol(rep);
}
const LEVEL_CLS = { '问题': 'bad', '待确认': 'warn', '正常': 'ok', '提醒': 'info', '设计内': 'ok', '闲置': 'ok' };

function stat(n, label, cls) { return `<div class="stat ${cls}"><div class="n">${n}</div><div class="l">${label}</div></div>`; }
// 数字是 0 的格子不用红 / 黄（2026-11-12：「缺矿 / 缺气液 0」也是红的，群友一眼看过去以为有事）
function statN(n, label, cls) { return stat(n, label, n ? cls : 'ok'); }
function headline(cls, title, lines) {
  return `<div class="headline ${cls}"><div class="h">${esc(title)}</div>${lines.filter(Boolean).map(l => `<div class="d">${esc(l)}</div>`).join('')}</div>`;
}

function renderTree(tree, canTarget) {
  if (!tree || !tree.length) return '';
  const mark = { ok: '✓', bad: '✗', skip: '→', info: '?' };
  const bad = tree.filter(s => s.status === 'bad').length;
  let html = `<div class="card tree"><div class="card-h"><span>排查树${helpIcon('tree')}</span><span class="muted">按人工检修顺序，只用简报：${bad ? `<b class="bad-text">${bad} 项要去看</b>` : '全部正常'}</span></div>`;
  html += tree.map((s, i) => {
    const w = s.where || {};
    const where = s.status === 'bad' ? `<div class="tree-where">去哪：<b>${esc((w.areas || []).join('、') || '—')}</b>　看什么：<b>${esc((w.machines || []).join('、') || '按错题本')}</b>${w.checks ? '　+ 错题本 7.1 核查' : ''}
      ${canTarget && (w.areas || []).length ? `<button class="btn sm primary" onclick='startTargeted(${JSON.stringify(w).replace(/'/g, '&#39;')})'>定向巡检这几处</button>` : ''}</div>` : '';
    return `<details class="tree-step ${s.status}" ${s.status === 'bad' ? 'open' : ''}><summary><span class="m">${mark[s.status] || '?'}</span><span class="t">${esc(s.title)}</span><span class="c">${esc(s.cause)}</span></summary>
      <div class="body">${(s.facts || []).map(f => `<p>${esc(f)}</p>`).join('')}${where}${s.advice ? `<p class="advice"><span class="k">建议</span>${esc(s.advice)}</p>` : ''}</div></details>`;
  }).join('');
  return html + `</div>`;
}
async function startTargeted(where) {
  if (state.job && state.job.state === 'running') return toast('已有任务在跑');
  const targets = (where.areas || []).map(a => ({ area: a, machines: where.machines || [], checks: !!where.checks }));
  const how = (where.machines || []).length
    ? `有底账的分区：近景直接看 ${(where.machines || []).join('、')}，每处看 12 秒，不点任何东西（每个分区约 2–4 分钟）；没有底账的分区：只扫一遍、点这些设备的面板${where.checks ? '，并跑 7.1 核查' : ''}。`
    : `只扫一遍、点标记设备的面板${where.checks ? '，并跑 7.1 核查' : ''}（每个分区约 3–6 分钟）。`;
  if (!(await uiConfirm(`定向巡检 ${targets.map(t => t.area).join('、')}：${how}\n期间不要碰键鼠。`, { ok: '开始巡检', title: '定向巡检' }))) return;
  const r = await api('start_targeted', state.blueprint, targets, true);
  if (!r.ok) return toast(r.error);
  go('patrol'); $('#patrolResult').innerHTML = '';
  startPolling('patrol');
}
function renderQuick(rep) {
  const src = rep.sources || [];
  const kinds = ['缺地区', '缺物品', '缺设备', '来源供不上', '库存在降'];
  const regions = Object.entries(rep.regions || {});
  const pending = regions.reduce((s, [, r]) => s + r.findings.filter(f => f.level === '待确认').length, 0);
  const tree = rep.tree || [];
  const badSteps = tree.filter(s => s.status === 'bad');
  const treeBad = badSteps.length;
  const srcBad = src.filter(s => s.kind !== '库存在降').length;
  // 最上面先给一句总结论：排查树看的是最终产物，全 ✓ 就是产线在正常运转，待确认只是疑点
  // 排查树有 ✗ 时，待确认多半是同一件事的另一面，别说成「不用管」
  const pendNote = !pending ? ''
    : treeBad ? `另有 ${pending} 条待确认：可能和上面这几项是同一件事，先把排查树的 ✗ 处理掉，再诊断一次看还剩哪些。`
    : `另有 ${pending} 条待确认：只是简报上的疑点，多半是一时的波动或设计内，不用急着处理；连着几次诊断都在，再跑一次深入巡检确认。`;
  const again = rep.reread
    ? (rep.reread.cleared?.length ? `第一次读简报时 ${rep.reread.cleared.join('、')} 偏低，复读一次后正常，是一时的波动。` : '排查树有 ✗，所以复读了一次简报，两次结果一样。')
    : '';
  let html;
  if (treeBad) html = headline('bad', `有 ${treeBad} 项要去看：${badSteps.map(s => s.title).join('、')}`, ['在下面的排查树里点「定向巡检这几处」，程序会去对应分区找出是哪台设备。', again, pendNote]);
  else if (srcBad) html = headline('warn', `产物都在正常出，但有 ${srcBad} 种源头物品比蓝图少`, ['见下面「缺矿 / 缺气液」：多半是少开了矿机、水泵或气口。', again, pendNote]);
  else html = headline('ok', '产线在正常运转', [tree.length ? '排查树里的产物都达标，源头物品够用。' : '源头物品够用。', again, pendNote]);
  html += `<div class="summary">${statN(treeBad, '排查树 ✗', 'bad')}${statN(srcBad, '缺矿 / 缺气液', 'bad')}${statN(pending, '待确认', 'warn')}${stat(fmtSec(rep.elapsed), '用时', '')}</div>`;
  html += renderTree(rep.tree, true);
  html += `<div class="hint">快速诊断只看简报。「待确认」是简报层面的疑点，不等于故障：排查树全 ✓ 时可以不管；想确认有没有设备真的停了，跑一次深入巡检。</div>`;
  html += `<div class="card"><div class="card-h"><span>缺矿 / 缺气液（对照蓝图参考）</span><span class="muted">简报 ${esc(rep.brief)}</span></div>`;
  if (!src.length) html += `<div class="empty">源头物品全部够用：没有缺开矿、缺水口、缺气口。</div>`;
  for (const k of kinds) {
    const items = src.filter(s => s.kind === k);
    if (!items.length) continue;
    html += `<div class="sec-title ${k === '库存在降' ? 'warn' : 'bad'}">${k}（${items.length}）</div>`;
    html += items.map(s => `<details class="verdict ${k === '库存在降' ? 'warn' : 'bad'}" open><summary><span class="t">${esc(s.region)} · ${esc(s.item)}</span><span class="s">理论 ${s.theo ?? '?'} / 参考 ${s.reference ?? '?'}${s.cur != null ? ` · 当前 ${s.cur}` : ''}</span></summary>
      <div class="body"><p>${esc(s.text)}</p>${s.advice ? `<p class="advice"><span class="k">建议</span>${esc(s.advice)}</p>` : ''}</div></details>`).join('');
  }
  html += `</div>`;
  for (const [name, r] of regions) {
    const groups = {};
    for (const f of r.findings) (groups[f.level] ||= []).push(f);
    html += `<div class="card"><div class="card-h"><span>${esc(name)}</span><span class="muted">仓库上限 ${r.cap} · 爆仓 ${r.full.length} 种</span></div>`;
    for (const lv of ['待确认', '提醒', '设计内', '闲置', '正常']) {
      const g = groups[lv]; if (!g) continue;
      html += `<div class="sec-title ${LEVEL_CLS[lv]}">${lv}（${g.length}）</div>` + g.map(f => `<details class="verdict ${LEVEL_CLS[lv]}" ${lv === '待确认' && treeBad ? 'open' : ''}><summary><span class="t">${esc(f.text)}</span><span class="s">${esc((f.affected || []).slice(0, 3).join('、'))}</span></summary>
        <div class="body">${f.advice ? `<p class="advice"><span class="k">建议</span>${esc(f.advice)}</p>` : ''}<p><span class="k">影响</span>${esc((f.affected || []).join('、'))}</p>${f.chain?.length ? `<pre class="chain">${esc(f.chain.join('\n'))}</pre>` : ''}</div></details>`).join('');
    }
    if (r.full.length) html += `<p class="hint">爆仓：${esc(r.full.join('、'))}</p>`;
    html += `</div>`;
  }
  html += rawDetails(rep.sources_text + '\n\n' + rep.text);
  return html;
}

function renderPatrol(rep) {
  const areas = rep.areas || [];
  const tot = (k) => areas.reduce((s, a) => s + (a.counts?.[k] || 0), 0);
  const nBad = tot('问题'), nPend = tot('待确认'), nFail = areas.filter(a => !a.ok).length;
  const nDev = areas.reduce((s, a) => s + (a.devices || 0), 0);
  const pendNote = nPend ? `另有 ${nPend} 条待确认：是疑点，不一定是故障，有空时照着每条的建议看一眼。` : '';
  // 总结论要和下面的排查树对得上：设备上没问题、排查树却有 ✗ 时不能写「没发现问题」
  const badSteps = (rep.tree || []).filter(s => s.status === 'bad');
  const treeNote = badSteps.length ? `排查树有 ${badSteps.length} 项没达标：${badSteps.map(s => s.title).join('、')}，见下面排查树每一项的「去哪 / 看什么」。` : '';
  const scanned = `巡了 ${areas.length - nFail} 个分区，看了 ${nDev} 台带标记的设备。`;
  const failNote = nFail ? `有 ${nFail} 个分区没巡成，原因写在下面的页签里，可以单独再巡一次。` : '';
  let html;
  if (nBad) html = headline('bad', `有 ${nBad} 个问题要去修`, ['每条问题都写了依据和建议，带定位图；从第一条看起，修好后再巡一次。', treeNote, failNote, pendNote]);
  else if (badSteps.length) html = headline('warn', `巡过的设备没发现要修的问题，但排查树有 ${badSteps.length} 项没达标`,
    [treeNote, scanned + '原因可能在没巡的分区，或在协议区域外（矿机、水泵）。', failNote, pendNote]);
  else html = headline(nFail ? 'warn' : 'ok', nFail ? `巡完的分区没发现要修的问题，但有 ${nFail} 个分区没巡成` : '没发现要修的问题', [scanned, failNote, pendNote]);
  html += `<div class="summary">${statN(nBad, '问题', 'bad')}${statN(nPend, '待确认', 'warn')}${stat(tot('正常'), '正常', 'ok')}${stat(areas.reduce((s, a) => s + (a.devices || 0), 0), '设备', 'info')}${stat(fmtSec(rep.elapsed), '用时', '')}</div>`;
  const uid = 'tabs' + Math.random().toString(36).slice(2, 7);
  // 默认打开第一个有问题的分区（没有就第一个）：打开报告先看到要修的东西
  const first = Math.max(0, areas.findIndex(a => a.ok && (a.counts?.['问题'] || 0) > 0));
  html += renderTree(rep.tree, false);
  html += `<div class="card"><div class="tabs" id="${uid}">${areas.map((a, i) => `<button class="tab ${i === first ? 'active' : ''}" onclick="switchTab('${uid}',${i})">${esc(a.area)}<span class="b ${a.counts?.['问题'] ? 'bad' : ''}">${a.ok ? (a.counts['问题'] || 0) : '!'}</span></button>`).join('')}</div>`;
  html += areas.map((a, i) => `<div class="tabpane" data-tabs="${uid}" style="${i === first ? '' : 'display:none'}">${renderArea(a)}</div>`).join('');
  html += `</div>`;
  if (rep.cancelled) html += `<p class="hint">本次巡检被取消，只合并了已扫完的分区。</p>`;
  return html;
}
function switchTab(uid, i) {
  document.querySelectorAll(`#${uid} .tab`).forEach((t, k) => t.classList.toggle('active', k === i));
  document.querySelectorAll(`.tabpane[data-tabs="${uid}"]`).forEach((p, k) => p.style.display = k === i ? '' : 'none');
}
function renderArea(a) {
  if (!a.ok || !a.data) return `<div class="empty">${esc(a.area)} 失败：${esc(a.error || '未知')}</div>`;
  const d = a.data;
  let html = `<div class="hint">${esc(a.region)} / ${esc(a.area)} · ${a.devices} 台设备 · 简报 ${esc(d.brief)} · 巡检 ${esc(d.patrol)}</div>`;
  const cap = capacityLines(a.text);
  if (cap.length) html += `<div class="sec-title">产能自检</div><ul class="plain">${cap.map(l => `<li>${inlineMd(l)}</li>`).join('')}</ul>`;
  for (const lv of ['问题', '待确认']) {
    const vs = d.verdicts[lv] || [];
    html += `<div class="sec-title ${LEVEL_CLS[lv]}">${lv}（${vs.length}）${helpIcon('levels')}</div>`;
    html += vs.length ? vs.map(v => verdictCard(v, lv, lv === '问题')).join('') : `<div class="empty">无</div>`;
  }
  if (a.near) html += renderNear(a.near);
  const ok = d.verdicts['正常'] || [];
  html += `<details class="raw"><summary>正常 / 设计内（${ok.length}）</summary>${ok.map(v => verdictCard(v, '正常', false)).join('')}</details>`;
  if (d.logistics?.length) html += `<details class="raw"><summary>物流件（${d.logistics.length}）</summary><ul class="plain">${d.logistics.map(l => `<li>${inlineMd(typeof l === 'string' ? l : JSON.stringify(l))}</li>`).join('')}</ul></details>`;
  html += rawDetails(a.text);
  return html;
}
// 近景定向巡检：每台目标设备看 12 秒的观察。缺气、没认出在这里下结论；阻塞/空闲/关闭的结论在上面的合并诊断里
function renderNear(n) {
  const rows = n.rows || [];
  const c = n.counts || {};
  const cls = { '问题': 'bad', '待确认': 'warn', '观察': 'info', '设计内': 'ok', '正常': 'ok' };
  const head = `近景定向巡检：看了 ${rows.length} 台（${rows.filter(r => r.id_ok).length} 台认出）`
    + Object.entries(c).filter(([, v]) => v).map(([k, v]) => `，${k} ${v}`).join('');
  const card = r => {
    const imgs = (r.image ? `<figure><img src="${r.image}" alt="${esc(r.name)}"><figcaption>${esc(r.name)} · 对照 / 定位</figcaption></figure>` : '')
      + (r.impact_image ? `<figure><img src="${r.impact_image}" alt="影响范围"><figcaption>影响范围：紫圈 = 断开的输入端，蓝圈 = 同网输入端，红线 = 管网，绿框 = 下游设备</figcaption></figure>` : '');
    const open = r.level === '问题' || r.level === '待确认';
    return `<details class="verdict ${cls[r.level] || ''}" ${open ? 'open' : ''}><summary><span class="lv ${cls[r.level] || ''}">${esc(r.level)}</span><span class="t">${esc(r.name)}</span>`
      + `<span class="chips"><span class="chip-kv">位置 ${r.far[0]}, ${r.far[1]}</span>${r.product ? `<span class="chip-kv">${esc(r.product)}</span>` : ''}</span></summary>`
      + `<div class="body"><p>${esc(r.text)}</p>${imgs ? `<div class="imgs">${imgs}</div>` : ''}</div></details>`;
  };
  const top = rows.filter(r => ['问题', '待确认', '观察'].includes(r.level));
  const rest = rows.filter(r => !['问题', '待确认', '观察'].includes(r.level));
  return `<div class="sec-title info">${esc(head)}${helpIcon('near')}</div>`
    + (top.length ? top.map(card).join('') : `<div class="empty">目标设备都没有持续的异常标记</div>`)
    + (rest.length ? `<details class="raw"><summary>正常 / 设计内（${rest.length}）</summary>${rest.map(card).join('')}</details>` : '');
}
function verdictCard(v, lv, open) {
  const dev = v.device || {};
  const title = v.title || `${dev.name || ''} ${dev.name && !dev.name.startsWith('（') ? '' : ''}`.trim() || '（合并条目）';
  const status = v.title && (v.members || []).length ? `${v.members.length} 台` : dev.near ? `标记「${dev.badge || '?'}」（${dev.seen || '近景'}）` : `面板「${dev.status || '?'}」${dev.badge ? ` / 标记「${dev.badge}」` : ''}`;
  const devs = v.members?.length ? v.members : [dev, v.partner].filter(x => x && x.name);
  const imgs = devs.filter(x => x.image).map(x => `<figure><img src="${x.image}" alt=""><figcaption>${esc(x.name)} · ${esc(x.status || '')}</figcaption></figure>`).join('')
    + (v.impact_image ? `<figure><img src="${v.impact_image}" alt=""><figcaption>影响范围：紫圈 = 断开的输入端，蓝圈 = 同网输入端，红线 = 管网，绿框 = 下游设备</figcaption></figure>` : '');
  const names = devs.filter(x => x.name && !x.name.startsWith('（无')).map(x => `${esc(x.name)}${x.status ? `（${esc(x.status)}）` : ''}`).join('、');
  return `<details class="verdict ${LEVEL_CLS[lv] || ''}" ${open ? 'open' : ''}><summary><span class="lv ${LEVEL_CLS[lv] || ''}">${esc(lv)}</span><span class="t">${esc(title)}</span><span class="chips"><span class="chip-kv">${esc(status)}</span></span></summary>
    <div class="body">
      ${dev.recipe && !v.title ? `<p><span class="k">配方</span>${esc(dev.recipe)}</p>` : ''}
      ${v.title && names ? `<p><span class="k">涉及</span>${names}</p>` : ''}
      <p><span class="k">依据</span>${esc(v.reason)}</p>
      ${v.advice ? `<p class="advice"><span class="k">建议</span>${esc(v.advice)}</p>` : ''}
      ${v.chain?.length ? `<pre class="chain">${esc(v.chain.join('\n'))}</pre>` : ''}
      ${imgs ? `<div class="imgs">${imgs}</div>` : (lv !== '正常' ? '<p class="hint">这条没有定位图（设备不在截图里或是地区级结论）。</p>' : '')}
    </div></details>`;
}
function capacityLines(text) {
  const lines = (text || '').split('\n'); const out = []; let on = false;
  for (const l of lines) {
    if (l.startsWith('## ')) { on = l.includes('产能自检'); continue; }
    if (on && l.startsWith('- ')) out.push(l.slice(2));
    else if (on && l.startsWith('  - ')) out[out.length - 1] += ' — ' + l.trim().slice(2);
  }
  return out;
}
function inlineMd(s) { return esc(s).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>'); }
function rawDetails(text) { return `<details class="raw"><summary>原始报告（文字）</summary><div class="md">${mdToHtml(text || '')}</div></details>`; }
function mdToHtml(md) {
  const out = []; let inList = 0, inTable = false;
  const closeList = () => { while (inList) { out.push('</ul>'); inList--; } };
  for (const raw of md.split('\n')) {
    const line = raw.replace(/\s+$/, '');
    if (/^\|/.test(line)) { if (!inTable) { closeList(); out.push('<table>'); inTable = true; } if (/^\|[-| ]+\|$/.test(line)) continue; out.push('<tr>' + line.slice(1, -1).split('|').map(c => `<td>${inlineMd(c.trim())}</td>`).join('') + '</tr>'); continue; }
    if (inTable) { out.push('</table>'); inTable = false; }
    const h = /^(#{1,3}) (.*)/.exec(line);
    if (h) { closeList(); out.push(`<h${h[1].length}>${inlineMd(h[2])}</h${h[1].length}>`); continue; }
    const li = /^(\s*)- (.*)/.exec(line);
    if (li) { const depth = Math.floor(li[1].length / 2) + 1; while (inList < depth) { out.push('<ul>'); inList++; } while (inList > depth) { out.push('</ul>'); inList--; } out.push(`<li>${inlineMd(li[2])}</li>`); continue; }
    if (!line.trim()) { closeList(); continue; }
    if (/^\s{4,}/.test(raw)) { out.push(`<div class="pre">${inlineMd(line.trim())}</div>`); continue; }
    closeList(); out.push(`<p>${inlineMd(line)}</p>`);
  }
  closeList(); if (inTable) out.push('</table>');
  return out.join('');
}

// ---------------------------------------------------------------- 启动
(async function init() {
  await ready();
  setInterval(() => { $('#clock').textContent = new Date().toLocaleTimeString('zh-CN', { hour12: false }); }, 1000);
  try {
    const v = await api('version');
    $('#setVersion').textContent = v.version; $('#setRoot').textContent = v.root; $('#setDebug').textContent = v.debug;
    await loadBlueprints();
    await loadRecent();
    refreshGame();
    const j = await api('job_status');
    if (j.state === 'running') startPolling(j.kind);
  } catch (e) { toast('后端未连接：' + e.message); $('#gameText').textContent = '后端未连接：' + e.message; $('#gameDot').className = 'dot off'; }
})();


// ---------------------------------------------------------------- 蓝图分析
const bpa = { name: localStorage.getItem('bpaProject') || '', status: null, marks: [], scale: 0.6, base: '' };
async function bpaLoad() {
  const list = await api('bpa_projects');
  if (!list.find(p => p.name === bpa.name)) bpa.name = list[0]?.name || '';
  $('#bpaProj').innerHTML = list.map(p => `<option ${p.name === bpa.name ? 'selected' : ''}>${esc(p.name)}</option>`).join('') || '<option value="">（还没有项目）</option>';
  const bps = await api('blueprints');
  $('#bpaArchive').innerHTML = bps.map(b => `<option>${esc(b.name)}</option>`).join('');
  if (!list.length) $('#bpaNewBox').open = true;
  await bpaRefresh();
}
function bpaSelect(n) { bpa.name = n; localStorage.setItem('bpaProject', n); bpaRefresh(); }
// 删除项目（用户 2026-11-10）：先确认；挪进备份目录不真删；有档案的近景巡检要用它的拼合结果时先说清楚
async function bpaDelete() {
  if (!bpa.name) { toast('没有选中的项目'); return; }
  const info = await api('bpa_delete_info', bpa.name);
  if (!info.ok) { toast(info.error); return; }
  let msg = `删除蓝图分析项目「${info.name}」？\n\n整个项目目录挪进 debug\\bpa_备份（不真删，挪回 debug\\bpa 就恢复）。`;
  if (info.archive && info.lost.length) {
    msg += `\n\n注意：蓝图档案「${info.archive}」的近景定向巡检在用这个项目的拼合结果（${info.lost.join('、')}）。删了以后这些据点的管网追踪、断水影响就算不了，直到重新做一个项目或导入作者的 zip。`;
  } else if (info.archive && info.bases.length) {
    const alt = [info.other_projects.length ? `同档案的其他项目（${info.other_projects.join('、')}）` : '', info.imported.length ? `档案里导入的作者 zip（${info.imported.join('、')}）` : ''].filter(Boolean).join('、');
    msg += `\n\n蓝图档案「${info.archive}」的近景巡检改用${alt}的拼合结果，不受影响。`;
  }
  if (!(await uiConfirm(msg, { ok: '删除', danger: true, title: '删除项目' }))) return;
  const r = await api('bpa_delete', bpa.name);
  if (!r.ok) { toast(r.error); return; }
  toast(`已挪到 ${r.backup}`);
  bpa.name = ''; localStorage.removeItem('bpaProject');
  bpaLoad();
}
// 扫底账的据点下拉框：没有项目 / 项目没填分组时里面是空的，点开什么也选不了（用户 2026-11-10）——放一条提示、「开始扫」变灰
function bpaNoBases(msg) {
  ['#bpaLedgerBase', '#bpaBase', '#bpaInBase', '#bpaDsBase'].forEach(id => { if ($(id)) $(id).innerHTML = `<option value="">${esc(msg)}</option>`; });
  bpa.noBases = msg; bpaLedgerState();
}
function bpaLedgerState() {
  const b = $('#bpaLedger'); if (!b) return;
  const running = state.job?.state === 'running';
  b.disabled = running || !!bpa.noBases;
  b.title = bpa.noBases ? bpa.noBases : '';
}
async function bpaRefresh() {
  if (!bpa.name) { $('#bpaStatus').innerHTML = '<div class="empty">先新建一个项目</div>'; bpaNoBases('先新建项目，填据点分组'); if ($('#bpaNewBox')) $('#bpaNewBox').open = true; return; }
  const r = await api('bpa_status', bpa.name);
  if (!r.ok) { toast(r.error); return; }
  const s = bpa.status = r.status;
  $('#bpaName').value = s.name;
  if (s.archive) $('#bpaArchive').value = s.archive;
  $('#bpaGroups').value = Object.entries(s.groups).map(([b, l]) => `${b}：${l.join('，')}`).join('\n');
  const yes = (v, t = '✓') => v === true ? `<span class="ok">${t}</span>` : v === false ? '<span class="bad">✗</span>' : '<span class="muted">—</span>';
  let h = '<table class="bpa-st"><tr><th>据点</th><th>蓝图</th><th>设备一览</th><th>拼图尺寸</th><th>暗管数量</th><th>暗管连接</th><th>物流件</th><th>拼到基地</th></tr>';
  for (const [base, names] of Object.entries(s.groups)) {
    const bs = s.bases[base] || {};
    for (const n of names) {
      const b = s.blueprints[n] || {};
      const f = (bs.fit || {})[n];
      // 外接的暗管（预览里带红标、没有放大镜：对面的口不在这张蓝图里）不去点，单独写出来
      const openNote = b.pipes_open ? ` / 外接 ${b.pipes_open}` : '';
      const lk = b.links ? `双向 ${b.links['双向']} / 单向 ${b.links['单向']} / 未配 ${b.links['未配'] + b.links['矛盾']}${openNote}`
        : (b.pipes && b.pipes === b.pipes_open ? `<span class="muted">外接 ${b.pipes_open}（不用点）</span>`
          : b.pipes ? '<span class="muted">未点</span>' : '<span class="muted">无暗管</span>');
      const fit = f ? (f.ok ? `<span class="ok">✓ 旋转 ${f.rot}° 锚点 ${f.inliers}/${f.total}</span>` : `<span class="bad">不确定（锚点 ${f.inliers}/${f.total}，次优 ${f.second}）</span>`)
                    : (bs.error ? `<span class="bad">${esc(bs.error)}</span>` : '<span class="muted">—</span>');
      const lgc = b.logistics ? Object.values(b.logistics).reduce((a, c) => a + c, 0) : null;
      const lgs = b.logistics ? (b.logistics_bad && b.logistics_bad.length
          ? `<span class="bad" title="${esc(b.logistics_bad.join('；'))}">✗ 和一览不一致</span>`
          : `<span class="ok" title="${esc(Object.entries(b.logistics).filter(([k, v]) => v).map(([k, v]) => k + ' ' + v).join('，'))}">✓ ${lgc}（准入口 ${b.logistics['准入口']}）</span>`)
        : '<span class="muted">—</span>';
      h += `<tr><td>${esc(base)}</td><td>${esc(n)}</td><td>${yes(b.devices)}</td><td>${yes(b.size_check)}</td><td>${yes(b.pipe_check, '✓ ' + (b.pipes || 0))}</td><td>${lk}</td><td>${lgs}</td><td>${fit}</td></tr>`;
    }
  }
  h += '</table>';
  h += '<div class="hint">' + Object.entries(s.bases).map(([b, x]) => `${esc(b)}：底账 ${x.ledger ? x.ledger.devices + ' 台（' + x.ledger.source + '）' : '<span class="bad">没有，先扫底账</span>'}，输入端候选 ${x.inputs_auto} 个，已填 ${x.inputs_filled} 个${x.placement ? '，放置检查：' + (x.placement === '全部到位' ? '<span class="ok">全部到位</span>' : '<span class="bad">' + esc(x.placement) + '（看「放置核对图」）</span>') : ''}`).join('；') + '</div>';
  $('#bpaStatus').innerHTML = h;
  const opts = Object.keys(s.groups).map(b => `<option>${esc(b)}</option>`).join('');
  if (!opts) { bpaNoBases('项目还没填据点分组'); return; }
  bpa.noBases = '';
  const keep = $('#bpaBase').value, keep2 = $('#bpaInBase').value, keep3 = $('#bpaLedgerBase').value, keep4 = $('#bpaDsBase').value;
  $('#bpaBase').innerHTML = opts; $('#bpaInBase').innerHTML = opts; $('#bpaLedgerBase').innerHTML = opts; $('#bpaDsBase').innerHTML = opts;
  if (keep4 && s.groups[keep4]) $('#bpaDsBase').value = keep4;
  if (keep3 && s.groups[keep3]) $('#bpaLedgerBase').value = keep3;
  if (keep && s.groups[keep]) $('#bpaBase').value = keep;
  if (keep2 && s.groups[keep2]) $('#bpaInBase').value = keep2;
  bpaLedgerState();
  bpaShowImage(); bpaInputs(); bpaGates(); bpaDesign();
}
// 设计分析（任务计划第 3 项）：每台设备的设计开工率；推断出来的连线/配方单独标出来
const DS_COLORS = { '满速': '#1a8a3a', '间歇': '#d98200', '溢流位': '#888', '没接上': '#b000b0', '没料': '#c22' };
const dsKey = st => Object.keys(DS_COLORS).find(k => (st || '').startsWith(k)) || '没料';
async function bpaDesign() {
  const base = $('#bpaDsBase').value; if (!bpa.name || !base) return;
  const r = await api('bpa_design', bpa.name, base);
  if (!r.ok) { bpa.ds = null; $('#bpaDsSummary').innerHTML = `<div class="empty">${esc(r.error)}</div>`; $('#bpaDsTable').innerHTML = ''; $('#bpaDsInf').innerHTML = ''; return; }
  bpa.ds = r;
  const sm = r.summary || {}, st = sm.states || {};
  $('#bpaDsSummary').innerHTML = `<div class="bpa-dssum">${Object.keys(DS_COLORS).map(k => `<span style="color:${DS_COLORS[k]}">● ${k} ${st[k] || 0}</span>`).join('')}
    <span class="muted">共 ${sm.devices || 0} 台 · 推断连线 ${sm.inferred_links || 0} 条 · 来料经过推断连线 ${sm.via_inferred || 0} 台 · 推断配方 ${sm.recipe_inferred || 0} 台 · 要稳定环境 ${sm.env_required || 0} 台</span></div>`;
  $('#bpaDsInfN').textContent = (r.inferred_links || []).length;
  bpaLinksTable();
  bpaDesignTable();
  bpaProgress(r.progress);
}
function bpaProgress(p) {
  if (!p) return;
  $('#bpaDsProgress').innerHTML = `作者确认：设备 ${p.devices[0]}/${p.devices[1]} 台，推断连线 ${p.links[0]}/${p.links[1]} 条` + (p.wrong ? `，其中 <b style="color:#c22">${p.wrong} 处说不对</b>` : '');
}
function bpaLinksTable() {
  const r = bpa.ds; if (!r) return;
  $('#bpaDsInf').innerHTML = `<table class="bpa-table"><tr><th>从</th><th>到</th><th>层</th><th>依据</th><th>作者确认</th></tr>${(r.inferred_links || []).map((l, i) =>
    `<tr><td>${esc(l.from)}</td><td>${esc(l.to)}</td><td>${l.layer === 'pipe' ? '管道' : '传送带'}</td><td class="muted">${esc((l.basis || []).join('；'))}</td>
      <td class="ds-conf">${confirmCell('links', i, l.confirm, null)}</td></tr>`).join('')}</table>`;
}
// 确认单元格：未确认 → 「对」「不对」两个按钮；确认过 → 显示结果 + 撤销。设备选「不对」时要选实际情况
function confirmCell(kind, i, c, actuals) {
  if (c && c.verdict === 'ok') return `<span class="conf ok">✓ 对</span>${c.note ? ` <span class="muted">${esc(c.note)}</span>` : ''} <a class="undo" onclick="bpaConfirmOne('${kind}',${i},null)">撤销</a>`;
  if (c && c.verdict === 'wrong') return `<span class="conf bad">✗ 不对${c.actual ? '：实际' + esc(c.actual) : ''}</span>${c.note ? ` <span class="muted">${esc(c.note)}</span>` : ''} <a class="undo" onclick="bpaConfirmOne('${kind}',${i},null)">撤销</a>`;
  const sel = actuals ? `<select id="cf_${kind}_${i}"><option value="">不对，实际…</option>${actuals.map(a => `<option>${esc(a)}</option>`).join('')}</select>` : '';
  return `<button class="btn sm" onclick="bpaConfirmOne('${kind}',${i},'ok')">对</button>
    ${sel}<input id="cfn_${kind}_${i}" placeholder="备注（可不填）" size="10">
    <button class="btn sm ghost" onclick="bpaConfirmOne('${kind}',${i},'wrong')">不对</button>`;
}
async function bpaConfirmOne(kind, i, verdict) {
  const r = bpa.ds; if (!r) return;
  const item = kind === 'devices' ? bpa.dsRows[i] : r.inferred_links[i];
  let entry = null;
  if (verdict) {
    const note = ($(`#cfn_${kind}_${i}`) || {}).value || '';
    const actual = kind === 'devices' && verdict === 'wrong' ? (($(`#cf_${kind}_${i}`) || {}).value || '') : '';
    if (kind === 'devices' && verdict === 'wrong' && !actual) return toast('选「不对」时先在下拉框里选实际情况');
    entry = { verdict, actual, note };
  }
  const res = await api('bpa_confirm', bpa.name, $('#bpaDsBase').value, kind, { [item.key]: entry });
  if (!res.ok) return toast(res.error);
  item.confirm = entry;
  bpaProgress(res.progress);
  kind === 'devices' ? bpaDesignTable() : bpaLinksTable();
}
async function bpaConfirmPage() {
  const rows = (bpa.dsRows || []).filter(d => !d.confirm);
  if (!rows.length) return toast('这一页没有未确认的设备');
  if (!(await uiConfirm(`把这一页 ${rows.length} 台还没确认的设备都确认为「对」？请先对着游戏核对过。`, { ok: '全部确认' }))) return;
  const entries = {}; rows.forEach(d => { entries[d.key] = { verdict: 'ok', actual: '', note: '' }; });
  const res = await api('bpa_confirm', bpa.name, $('#bpaDsBase').value, 'devices', entries);
  if (!res.ok) return toast(res.error);
  rows.forEach(d => { d.confirm = entries[d.key]; });
  bpaProgress(res.progress); bpaDesignTable();
}
function bpaDesignTable() {
  const r = bpa.ds; if (!r) return;
  const f = $('#bpaDsFilter').value;
  const rows = r.devices.filter(d => f === 'all' || (f === 'bad' && d.rate < 0.995) || (f === 'inferred' && (d.via_inferred || d.recipe_inferred)) || (f === 'env' && d.env_required)
      || (f === 'unconfirmed' && !d.confirm) || (f === 'wrong' && d.confirm && d.confirm.verdict === 'wrong'))
    .sort((a, b) => a.rate - b.rate || a.device.localeCompare(b.device));
  bpa.dsRows = rows;
  const pct = v => `${Math.round(v * 100)}%`;
  $('#bpaDsTable').innerHTML = `<table class="bpa-table"><tr><th>设备</th><th>位置</th><th>配方</th><th>开工率</th><th>状态</th><th>依据</th><th>作者确认</th></tr>${rows.map((d, i) => {
    const tags = [d.recipe_inferred ? '<span class="tag">推断配方</span>' : '', d.env_required ? '<span class="tag">稳定环境</span>' : '', d.via_inferred ? '<span class="tag inf">经推断连线</span>' : ''].join('');
    const basis = (d.basis || []).map(b => esc(b).replace(/〔推断连线〕/g, '<b class="inf">〔推断连线〕</b>')).join('<br>');
    return `<tr><td>${esc(d.device)}</td><td class="muted">(${d.c[0]},${d.c[1]})</td><td>${esc(d.recipe || '')}${tags}</td>
      <td style="color:${DS_COLORS[dsKey(d.state)]};font-weight:700">${pct(d.rate)}</td><td style="color:${DS_COLORS[dsKey(d.state)]}">${esc(d.state)}</td>
      <td class="muted ds-basis">${basis}</td><td class="ds-conf">${confirmCell('devices', i, d.confirm, r.actuals)}</td></tr>`; }).join('')}</table>`;
}
async function bpaGates() {
  if (!bpa.name) return;
  const r = await api('bpa_gate_groups', bpa.name);
  if (!r.ok) { $('#bpaGateTable').innerHTML = `<div class="empty">${esc(r.error)}</div>`; return; }
  const st = { '液': '液体', '气': '气体', '固': '固体' };
  const rows = r.groups.map(g => `<tr data-k="${g.k}"><td><img src="${g.icon}"></td><td>G${g.k}</td><td>${g.n}</td>
      <td><select class="gst">${['', '液', '气', '固'].map(v => `<option value="${v}" ${v === (g.state || '') ? 'selected' : ''}>${v ? st[v] : '不知道'}</option>`).join('')}</select></td>
      <td><input class="gnm" value="${esc(g.item || '')}" size="8"><div class="muted" style="font-size:11px">${esc(g.source || '')}</div></td>
      <td class="gcand">${(g.candidates || []).map(c => `<span class="chip" title="AKEData 比对分 ${c.score}" onclick="bpaPickCand(this,'${esc(c.name)}','${c.state}')"><img src="${c.icon}">${esc(c.name)}</span>`).join('')}</td>
      <td>${g.kind === '?' ? '<span class="muted">未定</span>' : esc(g.kind) + '准入口'}${g.state ? '' : ' <span class="muted">（推断）</span>'}</td>
      <td>${g.maybe_item_below ? `<span class="bad">${g.maybe_item_below} 个</span>` : ''}</td></tr>`).join('');
  $('#bpaGateTable').innerHTML = `<table class="bpa-table"><tr><th>图标</th><th>组</th><th>个数</th><th>形态</th><th>名字</th><th>AKEData 最像的（点一下采用）</th><th>结果</th><th>底下可能叠物品准入口</th></tr>${rows}</table>`;
}
function bpaPickCand(el, name, state) {
  const tr = el.closest('tr'); tr.querySelector('.gnm').value = name; tr.querySelector('.gst').value = state;
}
async function bpaSaveGates() {
  const labels = {};
  document.querySelectorAll('#bpaGateTable tr[data-k]').forEach(tr => {
    const v = tr.querySelector('.gst').value;
    if (v) labels[tr.dataset.k] = { state: v, item: tr.querySelector('.gnm').value };
  });
  const r = await api('bpa_label_gates', bpa.name, labels);
  if (!r.ok) return toast(r.error);
  toast('已保存'); bpaRefresh();
}
async function bpaCreate() {
  const r = await api('bpa_create', $('#bpaName').value, $('#bpaGroups').value, $('#bpaArchive').value);
  if (!r.ok) { toast(r.error); return; }
  bpa.name = r.status.name; localStorage.setItem('bpaProject', bpa.name); toast('项目已保存'); bpaLoad();
}
async function bpaRead() {
  if (!bpa.name) return toast('先新建项目');
  const r = await api('start_bpa_read', bpa.name, null); if (!r.ok) return toast(r.error);
  $('#bpaLog').style.display = ''; startPolling('bpa');
}
async function bpaLedger() {
  if (!bpa.name) return toast('先新建项目');
  const base = $('#bpaLedgerBase').value; if (!base) return toast('先选据点');
  const r = await api('start_bpa_ledger', bpa.name, base); if (!r.ok) return toast(r.error);
  $('#bpaLog').style.display = ''; startPolling('bpa');
}
async function bpaFit() {
  if (!bpa.name) return toast('先新建项目');
  const r = await api('start_bpa_fit', bpa.name); if (!r.ok) return toast(r.error);
  $('#bpaLog').style.display = ''; startPolling('bpa');
}
async function bpaExport() {
  if (!bpa.name) return toast('先新建项目');
  const kind = await uiChoice(`导出「${bpa.name}」的蓝图分析包。

给玩家：精简包（约 30 MB），只带导入后巡检要用的底账、配方表、准入口限速、拼合结果和近景大图。
给开发核对：完整包（200 MB 以上），另带建底账的逐屏截图、读蓝图的原始帧和标注图。`,
    [{ label: '取消', value: null }, { label: '完整包', value: 'full' }, { label: '精简包（给玩家）', value: 'lite', kind: 'primary' }], '导出 zip');
  if (!kind) return;
  toast('导出中…');
  const r = await api('bpa_export', bpa.name, kind === 'lite'); toast(r.ok ? '已导出：' + r.path : r.error);
}
async function bpaShowImage() {
  const base = $('#bpaBase').value; if (!bpa.name || !base) return;
  const r = await api('bpa_image', bpa.name, base, $('#bpaWhich').value, 0.6);
  $('#bpaImage').innerHTML = r.ok ? `<img src="${r.image}" draggable="false">` : `<div class="empty">${esc(r.error)}</div>`;
  panZoom($('#bpaImage'));
}
async function bpaInputs() {
  const base = $('#bpaInBase').value; if (!bpa.name || !base) return;
  const r = await api('bpa_inputs', bpa.name, base);
  if (!r.ok) { $('#bpaInImage').innerHTML = `<div class="empty">${esc(r.error)}</div>`; $('#bpaInTable').innerHTML = ''; return; }
  bpa.base = base; bpa.scale = r.scale;
  bpa.marks = r.marks.map(m => Object.assign({ item: '', rate: '', merged: false, via: '', note: '' }, m));
  bpa.dropped = r.dropped || [];
  $('#bpaInImage').innerHTML = `<img src="${r.image}" draggable="false" onclick="bpaAddMark(event)">`;
  panZoom($('#bpaInImage'));
  bpaRenderTable();
}
function bpaRenderTable() {
  const vias = ['', '水泵', '暗管', '多口暗管', '其他'];
  const rows = bpa.marks.map((m, i) => `<tr><td class="no ${m.source === 'manual' ? 'manual' : ''}">${m.no}</td>
      <td><input value="${esc(m.item)}" onchange="bpa.marks[${i}].item=this.value"></td>
      <td><input value="${esc(m.rate ?? '')}" size="4" onchange="bpa.marks[${i}].rate=this.value"></td>
      <td><input type="checkbox" ${m.merged ? 'checked' : ''} onchange="bpa.marks[${i}].merged=this.checked"></td>
      <td><select onchange="bpa.marks[${i}].via=this.value">${vias.map(v => `<option ${v === m.via ? 'selected' : ''}>${v}</option>`).join('')}</select></td>
      <td><button class="btn sm ghost" onclick="bpaDelMark(${i})">删</button></td></tr>`).join('');
  const drops = (bpa.dropped || []).map((d, i) => `<tr><td class="no" style="color:#888">${d.tag}</td><td>${esc(d.item || '')}</td>
      <td colspan="3" class="muted">${esc(d.why)}</td><td><button class="btn sm ghost" onclick="bpaAddDropped(${i})">加回</button></td></tr>`).join('');
  $('#bpaInTable').innerHTML = `<table class="bpa-table"><tr><th>#</th><th>物品</th><th>每分钟</th><th>汇流口</th><th>接口</th><th></th></tr>${rows}</table>`
    + (drops ? `<div class="hint" style="margin-top:8px">贴边但程序判断不是输入端的准入口（图上灰色虚圈），判断错了就点「加回」：</div><table class="bpa-table">${drops}</table>` : '');
}
// 大图查看：按住左键拖动、滚轮以鼠标处为中心缩放、右上「复位」回到适应宽度（2026-11-09 用户：不要靠横竖滑条）。
// 拖动过就不算点击（输入端图上点击是补标），缩放后点击补标的坐标照样对（bpaAddMark 用变换后的外框换算）。
function panZoom(box) {
  const img = box && box.querySelector('img'); if (!img) return;
  const st = { s: 1, x: 0, y: 0 };
  img.style.transformOrigin = '0 0';
  const apply = () => { img.style.transform = `translate(${st.x}px, ${st.y}px) scale(${st.s})`; };
  const fit = () => { const nw = img.naturalWidth || 1, nh = img.naturalHeight || 1; st.s = Math.min(box.clientWidth / nw, box.clientHeight / nh, 1); st.x = (box.clientWidth - nw * st.s) / 2; st.y = 0; apply(); };
  if (img.complete && img.naturalWidth) fit(); else img.addEventListener('load', fit, { once: true });
  let reset = box.querySelector('.pz-reset');
  if (!reset) { reset = document.createElement('button'); reset.className = 'btn sm ghost pz-reset'; reset.textContent = '复位'; box.appendChild(reset); }
  reset.onclick = e => { e.stopPropagation(); fit(); };
  box.onwheel = e => {
    e.preventDefault();
    const r = box.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
    const ns = Math.min(8, Math.max(0.05, st.s * (e.deltaY < 0 ? 1.15 : 1 / 1.15)));
    st.x = mx - (mx - st.x) * ns / st.s; st.y = my - (my - st.y) * ns / st.s; st.s = ns; apply();
  };
  let drag = null;
  box.onmousedown = e => {
    if (e.button !== 0 || e.target === reset) return;
    e.preventDefault();
    drag = { x: e.clientX, y: e.clientY, ox: st.x, oy: st.y, moved: false }; box.classList.add('dragging');
    const move = ev => { const dx = ev.clientX - drag.x, dy = ev.clientY - drag.y; if (Math.abs(dx) + Math.abs(dy) > 4) drag.moved = true; st.x = drag.ox + dx; st.y = drag.oy + dy; apply(); };
    const up = () => { window.removeEventListener('mousemove', move); window.removeEventListener('mouseup', up); box.classList.remove('dragging'); box._dragged = drag.moved; drag = null; setTimeout(() => { box._dragged = false; }, 0); };
    window.addEventListener('mousemove', move); window.addEventListener('mouseup', up);
  };
  if (!box._pzClick) { box.addEventListener('click', e => { if (box._dragged) { e.stopPropagation(); e.preventDefault(); } }, true); box._pzClick = true; }
}
function bpaRenumber() { bpa.marks.forEach((m, i) => { m.no = i + 1; }); }
function bpaAddMark(ev) {
  const img = ev.target, r = img.getBoundingClientRect();
  const x = (ev.clientX - r.left) * (img.naturalWidth / r.width) / bpa.scale;
  const y = (ev.clientY - r.top) * (img.naturalHeight / r.height) / bpa.scale;
  bpa.marks.push({ no: bpa.marks.length + 1, p: [Math.round(x), Math.round(y)], source: 'manual', item: '', rate: '', merged: false, via: '', note: '' });
  bpaSaveInputs(true);
}
function bpaAddDropped(i) {
  const d = bpa.dropped[i];
  bpa.marks.push({ no: bpa.marks.length + 1, p: d.p, source: 'manual', item: d.item || '', rate: d.rate ?? '', merged: false, via: d.via || '', note: '作者加回：' + d.why, icon_group: d.icon_group });
  bpaSaveInputs(true);
}
function bpaDelMark(i) { bpa.marks.splice(i, 1); bpaRenumber(); bpaSaveInputs(true); }
function bpaBatch() {
  const rng = $('#bpaBRange').value.trim(), item = $('#bpaBItem').value.trim(), rate = $('#bpaBRate').value.trim(), via = $('#bpaBVia').value;
  const nos = new Set();
  rng.split(/[,，\s]+/).filter(Boolean).forEach(p => {
    const m = p.match(/^(\d+)(?:-(\d+))?$/);
    if (m) for (let k = +m[1]; k <= +(m[2] || m[1]); k++) nos.add(k);
  });
  bpa.marks.forEach(m => { if (nos.has(m.no)) { if (item) m.item = item; if (rate) m.rate = rate; m.via = via; } });
  bpaRenderTable();
}
async function bpaSaveInputs(reload) {
  const marks = bpa.marks.map(m => Object.assign({}, m, { rate: (m.rate === '' || m.rate === null || m.rate === undefined) ? null : Number(m.rate) }));
  const r = await api('bpa_save_inputs', bpa.name, bpa.base, marks);
  if (!r.ok) return toast(r.error);
  if (reload === true) bpaInputs(); else { toast('输入端已保存'); bpaRefresh(); }
}
