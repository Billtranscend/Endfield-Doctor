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
  bpaWriteState(running, kind);
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
    if (j.kind === 'bpa') {
      // 待确认开的任务（bpaFix 打的标记）只刷新设计分析，不整页 bpaRefresh（2026-11-19）：_bpa_fix 只重写 design.json，
      // 整页刷新会重载输入端表和准入口分组表，作者改了几行流速还没存、就去待确认里点了一项，跑完这些修改就没了。
      // 中途刷新过页面时标记是空的，退回整页刷新，和原来一样
      // 暗管来料（0.10.13）改的是 inputs.json：「输入端」页开着同一个据点时按新存的重新载入，免得拿旧数据一保存把新标记盖掉
      const only = bpa.afterJob === 'design', inAt = bpa.afterIn; bpa.afterJob = null; bpa.afterIn = null;
      if (only) { await bpaDesign(); if ($('#bpaWhich').value === 'design') bpaShowImage(); if (inAt) bpaInReload(inAt); } else await bpaRefresh();
    }
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
    feedbackProjects();
    await loadBlueprints();
    await loadRecent();
    refreshGame();
    const j = await api('job_status');
    if (j.state === 'running') startPolling(j.kind);
  } catch (e) { toast('后端未连接：' + e.message); $('#gameText').textContent = '后端未连接：' + e.message; $('#gameDot').className = 'dot off'; }
})();


// ---------------------------------------------------------------- 蓝图分析
// 设置页：导出反馈包（每个 ≤ 24 MB，能直接传 GitHub）
async function feedbackProjects() {
  const sel = $('#fbProj'); if (!sel) return;
  const list = await api('bpa_projects');
  const keep = sel.value || localStorage.getItem('bpaProject') || '';
  sel.innerHTML = list.map(p => `<option ${p.name === keep ? 'selected' : ''}>${esc(p.name)}</option>`).join('') || '<option value="">（还没有蓝图分析项目）</option>';
}
async function feedbackExport() {
  const name = $('#fbProj').value; if (!name) return toast('还没有蓝图分析项目');
  const btn = $('#fbBtn'); btn.disabled = true; $('#fbResult').textContent = '正在打包…';
  const r = await api('feedback_export', name, $('#fbRaw').checked);
  btn.disabled = false;
  if (!r.ok) { $('#fbResult').textContent = ''; return toast(r.error); }
  $('#fbResult').innerHTML = `导出了 ${r.files.length} 个压缩包，共 ${r.total_mb} MB，在 <span class="mono">${esc(r.dir)}</span>：
    <ul class="plain">${r.files.map(f => `<li>${esc(f.name)}　${f.mb} MB${f.note ? `　<b style="color:var(--bad)">${esc(f.note)}</b>` : ''}</li>`).join('')}</ul>
    把这几个文件一起拖进 issue 的评论框就行。`;
}
// gt / gEdits / gFilter：设计分析页的准入口表（0.10.13）——载入的表、改了还没存的（按准入口键）、筛选
const bpa = { name: localStorage.getItem('bpaProject') || '', status: null, marks: [], scale: 0.6, base: '', gt: null, gEdits: {}, gEditsFor: '', gFilter: 'show' };
async function bpaLoad() {
  const list = await api('bpa_projects');
  if (!list.find(p => p.name === bpa.name)) bpa.name = list[0]?.name || '';
  $('#bpaProj').innerHTML = list.map(p => `<option ${p.name === bpa.name ? 'selected' : ''}>${esc(p.name)}</option>`).join('') || '<option value="">（还没有项目）</option>';
  const bps = await api('blueprints');
  $('#bpaArchive').innerHTML = bps.map(b => `<option>${esc(b.name)}</option>`).join('');
  if (!list.length) $('#bpaNewBox').open = true;
  await bpaRefresh();
}
function bpaSelect(n) { bpa.name = n; localStorage.setItem('bpaProject', n); bpaGateBtns(); bpaRefresh(); }  // 旧项目的准入口表先不能存
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
// 配方未知、没在用（2026-11-19）要排在没料前面：dsKey 按前缀找，找不到才归没料
const DS_COLORS = { '满速': '#1a8a3a', '间歇': '#d98200', '溢流位': '#888', '没接上': '#b000b0', '配方未知': '#5a78aa', '没在用': '#bbb', '没料': '#c22' };
const dsKey = st => Object.keys(DS_COLORS).find(k => (st || '').startsWith(k)) || '没料';
async function bpaDesign() {
  const base = $('#bpaDsBase').value, name = bpa.name; if (!name || !base) return;
  bpaGateBtns();  // 换了据点：旧的准入口表等新表载入前不能存（按钮变灰）
  const r = await api('bpa_design', name, base);
  if (!r.ok) {
    bpa.ds = null; bpa.gt = null; $('#bpaDsGates').hidden = true;
    // 待确认卡、行上打开的改配方卡也清掉：留着的是上一个据点的，点了没反应（bpa.ds 已经空了）
    $('#bpaDsPending').innerHTML = ''; bpaRowCardClose();
    $('#bpaDsSummary').innerHTML = `<div class="empty">${esc(r.error)}</div>`; $('#bpaDsTable').innerHTML = ''; $('#bpaDsInf').innerHTML = ''; return;
  }
  r.at = { name, base };  // 卡片、行上的键是这个项目、这个据点的：存修正时照它发，不照下拉框现在选的
  bpa.ds = r;
  const sm = r.summary || {}, st = sm.states || {};
  // 池子两项（审计第 3 项，2026-11-19）：summary 里没有，按行数——几条配方同时跑的池子；配方是邻居推断或照抄同类、没核实的池子
  const nPar = r.devices.filter(d => (d.channels || []).length).length, nGuess = r.devices.filter(d => (POOL_SRC[d.pool_src] || [])[1]).length;
  $('#bpaDsSummary').innerHTML = `<div class="bpa-dssum">${Object.keys(DS_COLORS).map(k => `<span style="color:${DS_COLORS[k]}">● ${k} ${st[k] || 0}</span>`).join('')}
    <span class="muted">共 ${sm.devices || 0} 台 · 推断连线 ${sm.inferred_links || 0} 条 · 来料经过推断连线 ${sm.via_inferred || 0} 台 · 推断配方 ${sm.recipe_inferred || 0} 台 · 要稳定环境 ${sm.env_required || 0} 台
      · 并行池子 ${nPar} 台 · 池子配方没核实 ${nGuess} 台</span></div>
    ${(sm.warnings || []).map(w => `<div class="hint" style="color:var(--warn)">${esc(w)}</div>`).join('')}`;
  $('#bpaDsInfN').textContent = (r.inferred_links || []).length;
  bpaPending();
  // 准入口表（0.10.13）：折叠标题用这里给的几个数；表带小图，展开着才载入，收着就等下次展开再载入
  const gOpen = $('#bpaDsGates').open;
  if (!gOpen) bpa.gt = null;
  $('#bpaDsGates').hidden = false; bpaGateHead(r.gate_summary);
  if (gOpen) bpaGateLoad();
  bpaRowCardClose();  // 重算过：行上打开的改配方卡是旧数据
  bpaLinksTable();
  bpaDesignTable();
  bpaProgress(r.progress);
}
// 待确认：程序自己定不下来的地方（一台设备连着带子却没有进料……），作者选一下，程序按选的重算。
// 程序看设备口格箭头自己改对的（reread）也列在这里，让作者知道动过什么
const FEED_WORDS = { none: '都不是（进料带没认出来）', unused: '这台设备没在用' };
function bpaPending() {
  const r = bpa.ds, box = $('#bpaDsPending'); if (!r || !box) return;
  const items = r.pending || [], auto = r.reread || [];
  if (!items.length && !auto.length) { box.innerHTML = ''; return; }
  // 「待确认 N 处」只数没回答的（0.10.13）：每种卡答没答由接口的 open 字段说（池子卡回答 [] = 这台没在用也算答了、
  // 准入口汇总卡还有没说的口才算没答……），界面不再各写一套
  const open = items.filter(p => p.open).length;
  const autoHtml = auto.length ? `<div class="pend-auto">程序按设备口格的箭头自己改过 ${auto.length} 处（已标成推断，在下面「推断出来的连线」里可以说对不对）：
    <ul>${auto.map(a => `<li>${esc(a.device)}：${esc(a.basis || a.kind)}</li>`).join('')}</ul></div>` : '';
  box.innerHTML = `<div class="pend-head">${open ? `<b>待确认 ${open} 处</b>：这些地方程序定不下来，需要你看一眼` : (items.length ? '待确认的都答过了' : '')}</div>${autoHtml}
    ${items.map((p, i) => {
      // 按卡的种类分开画；下面剩下的是「没有进料」卡
      if (p.kind === '池子配方') return bpaPoolCard(p, i);
      if (p.kind === '准入口限速') return bpaGateCard(p, i);
      if (p.kind === '暗管来料') return bpaTunCard(p, i);
      const ans = typeof p.answer === 'string' ? p.answer : '';
      const btn = (val, label) => `<button class="btn sm${ans === val ? '' : ' ghost'}" onclick="bpaFix('feeds',${i},'${val}')">${label}</button>`;
      const links = (p.links || []).map((l, k) => btn('to:' + l.key, `${k + 1} 号带子是进料（另一头是${esc(l.to)}）`)).join('');
      // 池子不出单条配方下拉（2026-11-19）：选一条会把同时跑的几条清成一条，和池子配方卡两个入口还会互相盖；改成去池子配方卡
      const rsel = /^(扩容)?反应池$/.test((p.device || '').split('[')[0]) ? `<div class="pend-rec">池子同时跑几条配方，去池子配方卡里选：
        <button class="btn sm ghost" onclick="bpaPendPoolCard(${i})">打开池子配方卡</button></div>`
        : (p.recipes || []).length > 1 ? `<div class="pend-rec">配方不是「${esc((p.recipe || '').split('（')[0])}」的话改成：
        <select onchange="bpaFix('recipes',${i},this.value)"><option value="">（不改）</option>${p.recipes.map(x => `<option${x === p.recipe_fix ? ' selected' : ''}>${esc(x)}</option>`).join('')}</select></div>` : '';
      return `<div class="pend-item${ans ? ' done' : ''}">${p.image ? `<img src="${p.image}" alt="">` : ''}
        <div class="pend-body"><div class="pend-msg">${esc(p.msg)}</div>
          <div class="pend-opts">${links}${btn('none', FEED_WORDS.none)}${btn('unused', FEED_WORDS.unused)}
            ${ans ? `<a class="undo" onclick="bpaFix('feeds',${i},'')">撤销</a>` : ''}</div>${rsel}
          ${ans ? `<div class="muted">已选：${esc(ans.startsWith('to:') ? '指定了进料带' : FEED_WORDS[ans] || ans)}${p.applied ? '' : '（这一项还会留在这里）'}</div>` : ''}</div></div>`;
    }).join('')}`;
  items.forEach((p, i) => { if (p.kind === '池子配方') bpaPoolCount(i); });
}
// 池子配方卡（审计第 3 项，2026-11-19）：池子机身没有产物图标，作者勾这台同时在跑的几条配方。
// 配方文字一律 esc 后才写进页面；按钮只带卡片序号，勾了哪几条由 bpaPoolFix 从页面上读（不把配方文字拼进 onclick）。
// 卡片序号 i：待确认里的卡是 pending 下标；设计分析表池子行「改配方」打开的那张是 'row'（bpa.rowCard，不算进待确认 N 处）
const bpaPoolItem = i => i === 'row' ? bpa.rowCard : ((bpa.ds || {}).pending || [])[i];
function bpaPoolCard(p, i) {
  const ans = Array.isArray(p.answer) ? p.answer : null;
  const ticked = ans || p.current || [];  // 没回答过：先勾上这台现在按哪几条算（行上打开的卡）
  const short = rc => (rc || '').split('（')[0];
  const ends = (lst, dir) => (lst || []).map(l => `<li>${esc((l.items || []).join('、') || '?')}${l.unclear ? '（物品不明）' : ''}
      <span class="muted">${l.layer === 'pipe' ? '管' : '带'} · ${dir === 'in' ? '来自' : '去'}${esc(dir === 'in' ? l.from : l.to)}</span></li>`).join('');
  const nIn = (p.ins || []).length, nOut = (p.outs || []).length;
  // 行上打开的卡没有进出连线清单（design.json 没存），不写「没有」免得当成真没接
  const io = p.from_row && !nIn && !nOut ? '<div class="pend-io muted">进出的连线这里没列，看图上红圈这台的带子和管子</div>' : `<div class="pend-io"><div>进来${nIn ? `（图上 1–${nIn} 号）` : ''}：<ol>${ends(p.ins, 'in') || '<li class="muted">没有</li>'}</ol></div>
    <div>出去${nOut ? `（图上 ${nIn + 1}–${nIn + nOut} 号）` : ''}：<ol start="${nIn + 1}">${ends(p.outs, 'out') || '<li class="muted">没有</li>'}</ol></div></div>`;
  // 得分 = 这组配方和池子进出的连线对得上的程度，越高越像（推断）；悬停看哪几根连线对不上
  const combos = (p.combos || []).map((c, k) => `<button class="btn sm ghost" title="${esc('分数是按进出的连线对得上的程度打的，越高越像。' + ((c.why || []).join('；') || '进出的连线都对得上'))}"
      onclick="bpaPoolPick('${i}',${k})">${esc(c.label)}（${c.items}/${p.slots} 格，${c.score} 分）</button>`).join('');
  const boxes = (p.options || []).map((o, k) => `<label class="pend-pool-opt"><input type="checkbox" data-k="${k}"${ticked.includes(o.recipe) ? ' checked' : ''}
      onchange="bpaPoolCount('${i}')"> ${esc(short(o.recipe))}</label>`).join('');
  const said = ans == null ? '' : `<div class="muted">已选：${ans.length ? esc(ans.map(short).join('；')) : '这台没在用'}${p.applied ? '' : '（没用上，看上面的说明）'}</div>`;
  // 出口回答（0.10.13 功能三）：存下的回答和按它算出来的转送量；没用上的说明红字（产物没出口那条不在这里写，出口一块里按勾的现算）
  const ow = bpaPoolOutWords(p.outlets), pw = (p.pass_out || []).map(po => bpaPoolPassWords(po)).join('；');
  const saidOut = ow ? `<div class="muted">出口：${esc(ow)}${pw ? `；${esc(pw)}` : ''}</div>` : '';
  const notes = (p.outlet_notes || []).filter(n => n.kind !== '产物没出口').map(n => `<div class="pend-rec po-bad">${esc(bpaPoolNoteText(p, n))}</div>`).join('');
  const undoTip = p.outlets ? '撤销这台的配方；出口送什么的回答也一起删' : '撤销这台的配方';
  return `<div class="pend-item${ans != null ? ' done' : ''}" id="pendPool${i}">${p.image ? `<img src="${p.image}" alt="">` : ''}
    <div class="pend-body"><div class="pend-msg">${esc(p.msg)}</div>${io}
      ${combos ? `<div class="pend-rec">按连线猜的（推断，点一下勾上，再点「按这样算」）：</div><div class="pend-opts">${combos}</div>` : ''}
      <div class="pend-pool-list">${boxes}</div>
      <div class="pend-rec pend-pool-count"></div>
      <div class="pend-outlets"></div>
      <div class="pend-opts"><button class="btn sm pend-pool-go" onclick="bpaPoolFix('${i}','set')">按这样算</button>
        <button class="btn sm${ans && !ans.length ? '' : ' ghost'}"${p.outlets ? ' title="出口送什么的回答也一起删"' : ''} onclick="bpaPoolFix('${i}','unused')">这台没在用</button>
        ${ans != null ? `<a class="undo" title="${undoTip}" onclick="bpaPoolFix('${i}','undo')">撤销</a>` : ''}${p.from_row ? ` <a class="undo" onclick="bpaRowCardClose()">关上</a>` : ''}</div>${said}${saidOut}${notes}</div></div>`;
}
function bpaPoolChecked(i) {
  const p = bpaPoolItem(i), el = $(`#pendPool${i}`);
  if (!p || !el) return null;
  return [...el.querySelectorAll('.pend-pool-list input:checked')].map(b => p.options[+b.dataset.k]).filter(Boolean);
}
// 勾选时现算：物品种类（中间产物也占一格）对格数；要送出去的液体 / 固体、要从外面进的流体对出口和管入口（推断，只提示不挡）
function bpaPoolCount(i) {
  const p = bpaPoolItem(i), el = $(`#pendPool${i}`), sel = bpaPoolChecked(i);
  if (!p || !el || !sel) return;
  const ins = new Set(sel.flatMap(o => o.ins)), outs = new Set(sel.flatMap(o => o.outs));
  const n = new Set([...ins, ...outs]).size, fluid = new Set(p.fluid || []);
  const term = [...outs].filter(k => !ins.has(k)), ext = [...ins].filter(k => !outs.has(k));
  const warn = [];
  if (term.filter(k => fluid.has(k)).length > 2) warn.push('要送出去的液体超过 2 种，管出口只有 2 个');
  if (term.filter(k => !fluid.has(k)).length > 1) warn.push('要送出去的固体超过 1 种，带出口只能选 1 种');
  if (ext.filter(k => fluid.has(k)).length > 2) warn.push('要从外面进的流体超过 2 种，管入口只有 2 个');
  const over = n > p.slots;
  el.querySelector('.pend-pool-count').innerHTML = sel.length ? `已勾 ${sel.length} 条，物品 <b${over ? ' style="color:#c22"' : ''}>${n}/${p.slots}</b> 格`
    + (over ? '：超格了，放不下' : '') + (warn.length ? `<div class="muted">${esc(warn.join('；'))}（推断，只提示）</div>` : '') : '一条都没勾';
  const go = el.querySelector('.pend-pool-go');
  go.disabled = over || !sel.length;
  go.title = over ? '物品种类超过格数，放不下' : (!sel.length ? '先勾上这台在跑的配方' : '配方和出口一起存，后台只重算这个据点');
  bpaPoolOutRender(i);  // 出口能选什么跟着勾的配方变
}
function bpaPoolPick(i, k) {
  const p = bpaPoolItem(i), el = $(`#pendPool${i}`);
  const c = p && (p.combos || [])[k]; if (!c || !el) return;
  el.querySelectorAll('.pend-pool-list input').forEach(b => { b.checked = c.recipes.includes((p.options[+b.dataset.k] || {}).recipe); });
  bpaPoolCount(i);
}
// mode：set = 按勾的配方和出口一起算；unused = 这台没在用（存 []）；undo = 撤销配方（存 null）；后两种后台会把这台的出口回答一起删。
// undoOut = 只撤销出口回答。都走修正任务 bpa_fix_batch（0.10.13），配方和出口一批存、只重算一次：
// set 时配方和现在按的一样（作者存的，或面板、推断来的）、只改了出口 → 只存出口，不把面板、推断来的配方固定成作者指定；
// 出口没改 → 照原来只存配方（点一下也就是把配方定成作者指定）
function bpaPoolFix(i, mode) {
  const p = bpaPoolItem(i); if (!p) return;
  const at = (bpa.ds && bpa.ds.at) || {};  // 卡片的键是载入设计分析那次的项目、据点的（见 bpaSendFix）
  const send = ch => bpaFixJob('bpa_fix_batch', at.base || $('#bpaDsBase').value, [ch], undefined, at.name || bpa.name);
  if (mode === 'undoOut') return send([{ kind: 'pool_outlets', key: p.key, value: null }]);
  if (mode !== 'set') return send([{ kind: 'pool_recipes', key: p.key, value: mode === 'unused' ? [] : null }]);
  const sel = bpaPoolChecked(i) || [];
  if (!sel.length) return toast('先勾上这台在跑的配方');
  const value = sel.map(o => o.recipe);
  const out = bpaPoolOutRead(i), outV = out && (out.belt || out.pipe) ? out : null;  // 两种出口都没说 = 没回答（存过的就是撤销）
  // 出口一块没画出来（读不到）就当没改，免得把存下的回答当成撤销
  const outMoved = !!out && JSON.stringify(bpaPoolOutNorm(outV)) !== JSON.stringify(bpaPoolOutNorm(p.outlets));
  const now = Array.isArray(p.answer) ? p.answer : p.current;  // 现在按哪几条算：作者存的；没存过的是行上的（面板、推断来的）
  const same = Array.isArray(now) && now.length === value.length && value.every(r => now.includes(r));
  const ch = [];
  if (!(same && outMoved)) ch.push({ kind: 'pool_recipes', key: p.key, value });
  if (outMoved) ch.push({ kind: 'pool_outlets', key: p.key, value: outV });
  send(ch);
}
// ---- 池子出口送什么（0.10.13 功能三）：作者照游戏说这台池子的带出口、管出口各选了什么。选的是原料时下游先拿、池子用剩下的
// （后台按这个算）；选池里自己做、自己用的只记下来。能选什么按勾的配方现算（原料 = 池内不产的，中间产物 = 自己做又自己用的，
// 其余是产物），「可能在送原料」的提示、产物没出口的红字也只在这里现算，不进 design.json。物品名只 esc 后写进 value，不拼进 onclick
const sig3 = v => String(+(+v || 0).toPrecision(3));  // 21.4286 → 21.4、8.5714 → 8.57、30 → 30（和依据里的写法一样）
const bpaPoolList = v => v == null ? undefined : Array.isArray(v) ? v : v ? [v] : [];  // 存下的回答里一种出口的选择；没说 = undefined
// 出口回答 → 「带出口送息壤，管出口送惰性壤晶废液、壤晶废液」（只写说了的那种出口）
const bpaPoolOutWords = o => [['belt', '带'], ['pipe', '管']].filter(([f]) => bpaPoolList((o || {})[f]))
  .map(([f, w]) => { const l = bpaPoolList(o[f]); return l.length ? `${w}出口送${l.join('、')}` : `${w}出口什么都不送`; }).join('，');
// 比较用：不比存的时间，管出口不比先后
const bpaPoolOutNorm = o => { const b = bpaPoolList((o || {}).belt), p = bpaPoolList((o || {}).pipe);
  return b || p ? { belt: b ? [...b].sort() : null, pipe: p ? [...p].sort() : null } : null; };
// 行上的 pass_out 一项 → 「息壤到手 30/分，从带出口转给下游 21.4，池子自己用 8.57」；brief 是表格悬停用的短说法。
// 出口经分流器绕回池子自己时有 back（到手里含这份）：转出的不全给了下游，分开写
function bpaPoolPassWords(po, brief) {
  const down = sig3(po.out - (po.back || 0)), w = po.layer === 'pipe' ? '管' : '带';
  if (po.back) {
    return brief ? `${po.item}到手 ${sig3(po.arrive)}/分，转给下游 ${down}（另有 ${sig3(po.back)} 绕回自己），自己用 ${sig3(po.own)}`
      : `${po.item}到手 ${sig3(po.arrive)}/分，从${w}出口转出 ${sig3(po.out)}（绕回这台自己 ${sig3(po.back)}、给下游 ${down}），池子自己用 ${sig3(po.own)}`;
  }
  return brief ? `${po.item}到手 ${sig3(po.arrive)}/分，转给下游 ${down}，自己用 ${sig3(po.own)}`
    : `${po.item}到手 ${sig3(po.arrive)}/分，从${w}出口转给下游 ${down}，池子自己用 ${sig3(po.own)}`;
}
// 出口说明是按底账坐标点名的（会进导入方的巡检说明），卡上已经有图、红圈就是这台：开头的「扩容反应池 (1798,454)」去掉，句中的换成「这台」
function bpaPoolNoteText(p, n) {
  const [name, xy] = (p.key || '').split('@'), me = `${name} (${xy})`;
  let m = n.msg || '';
  if (xy && m.startsWith(me)) m = m.slice(me.length).replace(/^\s*的?/, '');
  return xy ? m.split(me + ' ').join('这台').split(me).join('这台') : m;
}
// 现在页面上选的出口 → {belt?: [], pipe?: []}（没说的那种出口没有这一项）。出口一块还没画过 → null
function bpaPoolOutRead(i) {
  const box = $(`#pendPool${i} .pend-outlets`), o = {};
  if (!box || !box.hasAttribute('data-ready')) return null;
  const b = box.querySelector('.po-belt input:checked');
  if (b && !b.classList.contains('po-unsaid')) o.belt = b.classList.contains('po-empty') ? [] : [b.value];
  const ps = [...box.querySelectorAll('.po-pipe input.po-item:checked')].map(x => x.value);
  if (ps.length) o.pipe = ps;
  else if (box.querySelector('.po-pipe input.po-empty:checked')) o.pipe = [];
  return o;
}
// 管出口：「什么都不送」和物品互斥；带出口是单选，浏览器自己管。改完重画（灰掉、提示跟着变）
function bpaPoolOutPick(i, el) {
  const box = $(`#pendPool${i} .pend-outlets`); if (!box || !el) return;
  if (el.type === 'checkbox' && el.checked) {
    const empty = el.classList.contains('po-empty');
    box.querySelectorAll('.po-pipe input').forEach(b => { if (b !== el && (empty || b.classList.contains('po-empty'))) b.checked = false; });
  }
  bpaPoolOutRender(i);
}
// 画「出口送什么」一块（勾配方、改出口选择时都重画）：选择从页面上读，第一次画用存下的回答 p.outlets
function bpaPoolOutRender(i) {
  const p = bpaPoolItem(i), el = $(`#pendPool${i}`); if (!p || !el) return;
  const box = el.querySelector('.pend-outlets'); if (!box) return;
  const cur = box.hasAttribute('data-ready') ? bpaPoolOutRead(i) : { belt: bpaPoolList((p.outlets || {}).belt), pipe: bpaPoolList((p.outlets || {}).pipe) };
  const sel = bpaPoolChecked(i) || [];
  const ins = new Set(sel.flatMap(o => o.ins)), outs = new Set(sel.flatMap(o => o.outs)), fluid = new Set(p.fluid || []);
  // 0 原料、1 池里自己做又自己用、2 产物、3 选着但不在勾的配方里（不悄悄丢掉作者的选择，标出来让他自己取消）
  const rank = x => !ins.has(x) && !outs.has(x) ? 3 : ins.has(x) && outs.has(x) ? 1 : ins.has(x) ? 0 : 2;
  const TAG = ['原料', '池里自己做、自己用', '', '不在勾的配方里'];
  const all = [...new Set([...ins, ...outs])];
  const opts = (mine, picked) => [...new Set([...mine, ...(picked || [])])].sort((a, b) => rank(a) - rank(b));
  const beltOpts = opts(all.filter(x => !fluid.has(x)), cur.belt), pipeOpts = opts(all.filter(x => fluid.has(x)), cur.pipe);
  const name = x => `${esc(x)}${TAG[rank(x)] ? `<span class="muted">（${TAG[rank(x)]}）</span>` : ''}`;
  const on = `onchange="bpaPoolOutPick('${i}',this)"`;
  const bv = cur.belt === undefined ? null : cur.belt.length ? cur.belt[0] : '';
  const radio = (cls, label, chk, val = '') => `<label><input type="radio" name="poBelt${i}" class="${cls}"${val ? ` value="${esc(val)}"` : ''}${chk ? ' checked' : ''} ${on}> ${label}</label>`;
  const pv = cur.pipe || [], full = pv.length >= 2;
  const box2 = x => { const c = pv.includes(x), off = full && !c;
    return `<label${off ? ' class="off" title="管出口只有 2 个，先取消一个再选"' : ''}><input type="checkbox" class="po-item" value="${esc(x)}"${c ? ' checked' : ''}${off ? ' disabled' : ''} ${on}> ${name(x)}</label>`; };
  // 出口在图上的编号：进来的排 1–nIn，出去的接着排（和卡上「出去」的清单、小图一样）。行上打开的卡没有进出连线清单
  const nIn = (p.ins || []).length, ends = p.outs || [];
  const nos = layer => ends.map((l, k) => l.layer === layer ? nIn + k + 1 : 0).filter(Boolean);
  const noIo = p.from_row && !nIn && !ends.length;
  const where = (layer, w) => noIo ? '看图上红圈这台的带子和管子' : nos(layer).length ? `图上 ${nos(layer).join('、')} 号` : `图上没追到接出去的${w}`;
  // 「可能在送原料」（按连线现算，这种出口还没说时才提示）：出口通到的下游要的东西里，没有勾的配方的同形态产物、但有同形态原料 X
  const hints = [];
  for (const [layer, fl, form, w] of [['belt', false, '固体', '带子'], ['pipe', true, '液体', '管子']]) {
    if (cur[layer] !== undefined || !sel.length) continue;
    const prods = [...outs].filter(x => fluid.has(x) === fl), raws = [...ins].filter(x => !outs.has(x) && fluid.has(x) === fl);
    const by = new Map();
    ends.forEach((l, k) => {
      const want = l.items || [];
      if (l.layer !== layer || want.some(x => prods.includes(x))) return;
      want.filter(x => raws.includes(x)).forEach(x => { const h = by.get(x) || { nos: [], to: new Set() }; h.nos.push(nIn + k + 1); h.to.add(l.to); by.set(x, h); });
    });
    for (const [x, h] of by) {
      // 一个出口的去向本身可能是「A、B」（一个口接了几台），按拆开后的设备数定「它 / 它们」
      const tos = [...new Set([...h.to].flatMap(t => String(t || '').split('、')).filter(Boolean))];
      const it = tos.length > 1 ? '它们' : '它', many = h.nos.length > 1;
      hints.push(`${h.nos.join('、')} 号出口是${w}，通到${tos.join('、')}，${it}要${x}。勾的这 ${sel.length} 条配方`
        + (prods.length ? `做的${form}（${prods.join('、')}）${it}不要，${x}是池里的原料` : `没有${form}产物，池里的${form}${raws.length > 1 ? `原料有${raws.join('、')}` : `只有原料${x}`}`)
        + `——${many ? '这几个口' : '这个口'}可能在送${x}。去游戏里点开这台池子看一眼，是的话在上面选上。`);
    }
  }
  // 三个出口都说了、还有产物哪个出口都没选：游戏里会停（官方），程序没按这个算
  const chosen = new Set([...(cur.belt || []), ...pv]);
  const stuck = cur.belt !== undefined && cur.pipe !== undefined ? [...outs].filter(x => !ins.has(x) && !chosen.has(x)) : [];
  const it = stuck.length > 1 ? '它们' : '它';
  box.setAttribute('data-ready', '');
  box.innerHTML = `<div><b>出口送什么</b><span class="muted">（游戏里点开这台池子，看出口各选了什么；没看过可以不填）</span>
      ${p.outlets ? `<a class="undo" onclick="bpaPoolFix('${i}','undoOut')">撤销出口回答</a>` : ''}</div>
    <div class="po-row po-belt"><span class="po-l">带出口（${where('belt', '带子')}，几个带出口共用一个选择）：</span>
      ${radio('po-unsaid', '没说', bv === null)}${radio('po-empty', '什么都不送', bv === '')}${beltOpts.map(x => radio('po-item', name(x), bv === x, x)).join('')}</div>
    <div class="po-row po-pipe"><span class="po-l">管出口（${where('pipe', '管子')}，两个口各选一种液体，最多 2 种；都不勾 = 没说）：</span>
      <label><input type="checkbox" class="po-empty"${cur.pipe && !pv.length ? ' checked' : ''} ${on}> 什么都不送</label>${pipeOpts.map(box2).join('')}</div>
    <div class="muted po-note">${sel.length ? '' : '先勾上这台在跑的配方，这里才列得出池里有什么。'}出口选的是原料时，下游先拿，池子用剩下的。选的是池里自己做、自己用的东西，程序还不会算，只记下来。</div>
    ${hints.map(h => `<div class="po-hint">${esc(h)}</div>`).join('')}
    ${stuck.length ? `<div class="po-bad">${esc(stuck.join('、'))}三个出口都没选：游戏里${it}在池里攒满以后，做${it}的那条配方会停；几条配方都停住时，池子会清空缓存区（官方）。程序没按这个算，这台按${it}能送出去算，以实际为准。</div>` : ''}`;
}
// 暗管来料卡（0.10.13 功能二）：外接暗管出口（另一头不在这套蓝图里）送进来什么、每分钟多少，程序读不到，作者填。
// 存成 inputs.json 里标在这台暗管出口上的输入端标记，后台只重算这个据点。接口给的：outs 它接出去的几路（小图上按 1、2… 标在
// 每一路第一个物流件上）、fluids 物品下拉（下游要的排前面，只有液体、气体）、suggest 下游要的、cap 每分钟最多多少（120 × 口数）、
// input_no 记成输入端几号、answer 填了量才有（只填了物品的照没填算：还是按满管估，物品在 mark 里）。
// 按钮只带卡片序号，选的物品、填的数由 bpaTunFix 从页面上读（不把物品名拼进 onclick）
function bpaTunCard(p, i) {
  const ans = p.answer, mark = p.mark || {}, cap = +p.cap || 120, ports = +p.ports || 1;
  const fl = p.fluids || [], want = new Set(p.suggest || []);
  const cur = ans ? ans.item || '' : mark.item || '';  // 填过的、只在「输入端」页填了物品的：预选它
  const opt = n => `<option${n === cur ? ' selected' : ''}>${esc(n)}</option>`;
  const mine = fl.filter(n => want.has(n)), rest = fl.filter(n => !want.has(n));
  const sel = `<select class="tun-item"><option value=""${fl.includes(cur) ? '' : ' selected'}>选液体或气体</option>`
    + (mine.length ? `<optgroup label="下游要的">${mine.map(opt).join('')}</optgroup>` : '')
    + `<optgroup label="${mine.length ? '其他液体、气体' : '液体、气体'}">${rest.map(opt).join('')}</optgroup></select>`;
  const outs = p.outs || [];
  // 「去哪几台」后面跟括号说明：接口写的「（没追到设备）」去掉括号，免得两对括号挨着
  const to = o => `${esc(o.to.replace('（没追到设备）', '没追到设备'))}<span class="muted">（管${(o.items || []).length ? `，${o.to.includes('、') ? '这几台' : '这台'}要用${esc(o.items.join('、'))}` : ''}`
    + `${o.unclear ? '，下游有一路物品不明' : ''}）</span>`;
  const io = `<div class="pend-io"><div>它接到${outs.length > 1 ? `（图上 1–${outs.length} 号）` : outs.length ? '（图上 1 号）' : ''}：
    <ol>${outs.map(o => `<li>${to(o)}</li>`).join('') || '<li class="muted">没追到它接出去的管子</li>'}</ol></div></div>`;
  // 只填了物品（液体、气体）：上面的说明已经写了现在怎么估（每一路按满管），这里只说缺什么。填的不是液体、气体的，extra 里有提醒
  const half = !ans && mark.item && fl.includes(mark.item) ? `<div class="pend-rec tun-warn">输入端 ${esc(mark.no)} 号只填了物品「${esc(mark.item)}」，`
    + `没填每分钟多少：补上每分钟多少再点「按这个算」。</div>` : '';
  // 一台标了两个、几口合计、填的不是液体或气体（接口给的 extra）
  const extra = (p.extra || []).map(t => `<div class="pend-rec tun-warn">${esc(t)}</div>`).join('');
  const top = ports > 1 ? `（${ports} 个口合计，最多 ${gnum(cap)}）` : `（一根管子最多 ${gnum(cap)}）`;
  const said = ans ? `<div class="muted">已填：${ans.rate > 0 ? `${esc(ans.item || '物品没填（按下游缺什么当什么）')} ${gnum(ans.rate)}/分` : '这台平时不来料'}
    <a class="undo tun-undo" onclick="bpaTunFix(${i},'undo')">撤销</a></div>` : '';
  const img = p.image ? `<img class="zoomable" src="${p.image}" alt="红圈 = 这台暗管出口；1、2… = 它接出去的几路（标在每一路第一个物流件上）">` : '';
  return `<div class="pend-item${p.open ? '' : ' done'}" id="pendTun${i}">${img}
    <div class="pend-body"><div class="pend-msg">${esc(p.msg)}</div>${io}${extra}${half}
      <div class="pend-rec tun-in"><span class="tun-f">送进来的物品 ${sel}</span><span class="tun-f">每分钟 <input class="tun-rate" inputmode="decimal" size="5"
        placeholder="0–${gnum(cap)}" value="${ans && ans.rate > 0 ? gnum(ans.rate) : ''}"> <span class="muted">${top}</span></span></div>
      <div class="pend-opts"><button class="btn sm" onclick="bpaTunFix(${i},'set')">按这个算</button>
        <button class="btn sm${ans && !(ans.rate > 0) ? '' : ' ghost'}" onclick="bpaTunFix(${i},'none')">这台平时不来料</button></div>${said}
      <div class="muted tun-note">${ans ? `这是输入端 ${esc(p.input_no)} 号` : p.input_no != null ? `填的会记成输入端 ${esc(p.input_no)} 号`
        : '填的会新加一个输入端（这个据点有几张暗管来料卡没填，号码按先填的排）'}，在「输入端」页也能看到、能改；拼合图变了，它会跟着这台暗管走。</div></div></div>`;
}
// mode：set = 按选的物品、填的每分钟多少算；none = 这台平时不来料（存 rate 0）；undo = 撤销（删掉这台暗管上的输入端标记）。
// 走修正任务（bpa_fix_batch，类别 tunnel_feeds），接口当场校验；跑完「输入端」页要按新存的重新载入（pollJob 看 bpa.afterIn）
async function bpaTunFix(i, mode) {
  const p = ((bpa.ds || {}).pending || [])[i], el = $(`#pendTun${i}`); if (!p || !el) return;
  const cap = +p.cap || 120, many = (+p.ports || 1) > 1;
  let value = null, msg = '已撤销';
  if (mode === 'set') {
    const item = el.querySelector('.tun-item').value, raw = el.querySelector('.tun-rate').value.trim(), rate = Number(raw);
    if (!raw || !Number.isFinite(rate) || rate < 0 || rate > cap)
      return toast(`每分钟多少要填 0–${gnum(cap)} 的数${many ? `（${p.ports} 个口合计）` : ''}；不来料就点「这台平时不来料」`);
    if (!item && rate > 0) return toast('先选送进来的是哪种液体或气体');
    value = { item, rate }; msg = rate > 0 ? `已记下：${item} ${gnum(rate)}/分` : '已记下：这台平时不来料';
  } else if (mode === 'none') { value = { item: '', rate: 0 }; msg = '已记下：这台平时不来料'; }
  if (bpaJobBusy()) return;
  const at = (bpa.ds && bpa.ds.at) || { name: bpa.name, base: $('#bpaDsBase').value };
  // 「输入端」页开着同一个据点、又有没存的改动：跑完那一页要重新载入，没存的会丢，先问
  if (bpaInSame(at) && bpaInDirty() && !(await uiConfirm('「输入端」页还有没存的改动。暗管来料也记在输入端里，跑完以后那一页会按新存的重新载入，'
    + '没存的改动会丢掉。\n\n要留着的话先点「取消」，去「输入端」页点「保存」，再回来点。', { ok: '不要了，继续', title: '暗管来料' }))) return;
  bpa.afterIn = { name: at.name, base: at.base, sig: bpaInSame(at) ? bpaInSig() : '' };  // sig：发出去时那一页的样子，跑完比一下有没有又改
  const ok = await bpaFixJob('bpa_fix_batch', at.base, [[{ kind: 'tunnel_feeds', key: p.key, value }]],
    `${msg}，正在重算这个据点（进度看上面的任务日志；程序启动后第一次算较慢）`, at.name);
  if (!ok) bpa.afterIn = null;
}
// 待确认里选了以后走任务（2026-11-19）：进度在上面的任务日志里，跑完 pollJob 只刷新设计分析（bpa.afterJob 标记）。
// 原来同步等结果，和据点拼合任务、「保存并重算」能同时跑，两边一起写 design.json
async function bpaFix(kind, i, value) {
  const r = bpa.ds; if (!r) return;
  const p = (r.pending || [])[i]; if (!p) return;
  bpaSendFix(kind, p.key, value || null);
}
async function bpaSendFix(kind, key, value) {
  // 键来自 bpa.ds（载入设计分析那次的项目、据点）：换项目 / 据点、新结果还没回来时点了旧卡，也存回它自己的据点
  const at = (bpa.ds && bpa.ds.at) || {};
  return bpaFixJob('bpa_fix', at.base || $('#bpaDsBase').value, [kind, key, value], undefined, at.name || bpa.name);
}
// 作者修正的任务（待确认的选项、暗管来料卡、准入口表的保存 / 都没开 / 删 / 重算共用）：接口当场校验，不对的报错、不开任务；
// 开起来以后进度在上面的任务日志，跑完 pollJob 按 afterJob 只刷新设计分析（连带准入口表）。args 是接在项目名、据点后面的参数；
// name、base 由调用的一方给（准入口表给表载入时的项目、据点）
async function bpaFixJob(fn, base, args, msg, name = bpa.name) {
  if (bpaJobBusy()) return false;
  const boxes = [$('#bpaDsPending'), $('#bpaDsRowCard')].filter(Boolean);
  boxes.forEach(b => b.classList.add('busy'));
  let res;
  try { res = await api(fn, name, base, ...args); } catch (e) { res = { ok: false, error: String(e && e.message || e) }; }
  if (!res.ok) { boxes.forEach(b => b.classList.remove('busy')); bpaGateBtns(); toast(res.error); return false; }
  toast(msg || '已记下，正在按你的选择重算这个据点（进度看上面的任务日志；程序启动后第一次算较慢）');
  bpa.afterJob = 'design';
  $('#bpaLog').style.display = ''; startPolling('bpa');
  return true;
}
// 任务在跑时，蓝图分析页会写数据的操作（待确认的选项、保存并重算、保存输入端）先挡住：拼合任务也在写这些文件（2026-11-19）
function bpaWriteState(running, kind) {
  const box = $('#bpaDsPending'); if (box) box.classList.toggle('busy', running);
  const rc = $('#bpaDsRowCard'); if (rc) rc.classList.toggle('busy', running);
  const sg = $('#bpaSaveGates'); if (sg) { sg.disabled = running; sg.title = running ? '有任务在跑，跑完再保存' : ''; }
  const si = $('#bpaSaveIn'); if (si) { const b = running && kind === 'bpa'; si.disabled = b; si.title = b ? '蓝图分析任务在跑，跑完再保存' : ''; }
  bpaGateBtns(running);  // 准入口表的按钮（0.10.13）：表里照样能改，只是存不了
}
function bpaJobBusy(onlyBpa) {
  const j = state.job;
  if (j && j.state === 'running' && (!onlyBpa || j.kind === 'bpa')) { toast('有任务在跑，稍后再试'); return true; }
  return false;
}

// ---------------------------------------------------------------- 准入口限速表（0.10.13 功能一）
// 「设计分析」卡片里、待确认下面的可折叠块 #bpaDsGates：作者照游戏填准入口的限制速度、限制通过数量，存进项目 fixes.json，
// 后台只重算这个据点。表的行由 bpa_gate_table 排好（行号 no 和总览图上的编号一样）；改了还没存的按准入口键记在 bpa.gEdits，
// 重新载入表时还没存进去的照样留着（跑完任务、换筛选都不丢），和现在生效的值一样了就丢掉。按钮只带行号，不把物品名、键拼进 onclick
const gnum = v => String(Math.round((+v || 0) * 100) / 100);
const GATE_CNT_TIP = '1–5000 的整数，空着 = 不限量。一次性的：放满这个数就停，不是每分钟；设计分析算长期开工率，开了的口按不送料算';
const GATE_CNT_BAD = '限制通过数量要填 1–5000 的整数，空着 = 不限量';
// 筛选：[下拉里的字, 这一行显示不显示]。默认（有料经过的）用接口给的 show：有料经过的口和你填过的口
const GATE_FILTERS = { show: ['有料经过的', r => r.show], set: ['你填过或游戏里读到的', r => r.src != null], input: ['输入端', r => r.group === '输入端'],
  none: ['没追到有料经过的', r => r.group === '没追到'], all: ['全部', () => true] };
// 载入的表是不是现在选的项目、据点的（换了项目 / 据点、新表还没回来时，旧表照样显示着，但不能存）
const bpaGateMatch = t => !!(t && t.name === bpa.name && t.base === $('#bpaDsBase').value);
const GATE_SWITCH = '正在换成现在选的项目 / 据点，等准入口表载入完再点';
// 折叠时的标题：有料经过的口几个、你填了几个（说都没开的另算）、游戏里读到几个（bpa_design 的 gate_summary，和待确认汇总卡同一个口径）
function bpaGateHead(s) {
  const el = $('#bpaDsGatesSum'); if (!el) return;
  s = s || {};
  const t = bpaGateMatch(bpa.gt) ? bpa.gt : null;  // 载入过这个据点的表才知道对不上的有几条
  const st = t && (t.stale || []).length ? ` · <b style="color:var(--warn)">对不上的旧设置 ${t.stale.length} 条</b>` : '';
  const off = s['说没开'] || 0, cant = s['填不了'] || 0;
  el.innerHTML = `<b>准入口限速 / 限制通过数量</b>${helpIcon('bpagatelimit')}：` + (s.need_rerun
    ? '这个据点的设计分析是旧版本算的，点开以后点「重算」生成准入口表'
    : `有料经过的口 ${s['有料'] || 0} 个${cant ? `（其中 ${cant} 个没有位置数据、填不了）` : ''} · 你填了 ${(s['已填'] || 0) - off} 个`
      + `${off ? ` · 你说都没开 ${off} 个` : ''} · 游戏里读到 ${s['游戏读到'] || 0} 个${st}<span class="muted">（点开看、填）</span>`);
}
function bpaGateToggle() {
  const d = $('#bpaDsGates'); if (!d || !d.open) return;
  if (!bpaGateMatch(bpa.gt)) bpaGateLoad();
}
async function bpaGateLoad() {
  const base = $('#bpaDsBase').value, box = $('#bpaDsGatesBody'); if (!bpa.name || !base || !box) return;
  const name = bpa.name, tok = bpa.gTok = (bpa.gTok || 0) + 1;
  if (!bpa.gt) box.innerHTML = '<div class="empty">正在载入准入口表…</div>';
  let t;
  try { t = await api('bpa_gate_table', name, base); } catch (e) { t = { ok: false, error: String(e && e.message || e) }; }
  if (tok !== bpa.gTok) return;  // 等的时候又换了据点 / 又载入了一次：用后面那次的
  const id = name + '|' + base;
  if (bpa.gEditsFor !== id) {
    const lost = Object.keys(bpa.gEdits).length;
    if (lost) toast(`换了项目或据点：「准入口限速」表里还没存的 ${lost} 处改动没保留`);
    bpa.gEdits = {}; bpa.gEditsFor = id;
  }
  bpa.gt = t.ok ? Object.assign(t, { name, base }) : { error: t.error, need_rerun: !!t.need_rerun, name, base, rows: [], stale: [] };
  for (const k of Object.keys(bpa.gEdits)) { const r = bpa.gt.rows.find(x => x.key === k); if (!r || !bpaGateDirty(r)) delete bpa.gEdits[k]; }
  bpaGateRender();
  bpaGateHead(bpa.ds && bpa.ds.gate_summary);
}
function bpaGateRender() {
  const box = $('#bpaDsGatesBody'), t = bpa.gt; if (!box || !t) return;
  if (t.error) {
    box.innerHTML = `<div class="empty">${esc(t.error)}</div>`
      + (t.need_rerun ? '<div class="btn-row"><button class="btn sm primary gate-act" onclick="bpaGateRerun()">重算</button><span class="muted">只重算这个据点的设计分析（一两分钟），不重新拼合</span></div>' : '');
    return bpaGateBtns();
  }
  const n = k => t.rows.filter(GATE_FILTERS[k][1]).length;
  const read = t.has_read ? `<div class="hint">这个据点有游戏里读到的限速${t.read_date ? `（${esc(t.read_date)} 读的）` : ''}，表里标「游戏里读的」、图上是绿圈：照样按读到的算。
    你改了以你填的为准（标「你改的」），撤销就回到读到的。</div>` : '';
  box.innerHTML = `<div class="hint">蓝图预览里看不出准入口开没开限速，设计分析默认按不限速算。下面默认列的是设计分析里有料经过的口；输入端和没追到料的口在筛选里。
      游戏里开了「限制速度」或「限制通过数量」的，照游戏里填；没开的可以不填，也可以点最下面的「上面没说的这些都没开」。
      改完点「保存并重算」，后台只重算这个据点（一两分钟），跑完这张表和下面的设备表自己刷新。</div>${read}
    <details class="hint-d"><summary>限制通过数量怎么算</summary>限制通过数量是一次性的：放满这个数就不再放行，不是每分钟。
      设计分析算的是长期开工率，开了它的口一律按不送料算（线内的口、输入端都一样，你填的和游戏里读的都一样，累计放了多少不管）；
      上游生产设备的产物只能走这个口的，出不去，开工率跟着算 0，依据写「出料受限」：原料有、只是产物出不去的写间歇，不算没料。原料有没有是估的：先假设产物出不去的设备照常收料，再假设上游也不会因为产物出不去而停，各算一遍；两遍里开了限制通过数量、限速 0 的口都照样不往下送料。每种原料有一遍送得到就算有，有一种两遍都送不到的照旧写没料。估算可能判错，比如很长的断料链、绕圈供料、备路上的设备、另一路来料的上游自己也出不去。同时跑几条配方的池子例外：每条都出不去时一律写间歇，原料有没有看不出来（已知限制）。
      口下游第一台缺这种料的设备（穿过物流件、储存箱找，不穿过别的设备），依据里会写是哪个口挡的；
      口上没认出物品、也没追到上游要送什么料的写不出（上游设备自己没料的口不算有料经过）。下游另有来路的（比如种植机、采种机自己循环供料），那部分照常算。
      游戏里读到开着、但没读到数量的口不按它挡。你填的导出 zip 时一起带上。</details>
    ${t.overview ? `<div class="hint">总览：表里的 # 号标在图上，画的是你填过的、游戏里读到开着的和有料经过的口（其余输入端、没追到料的不画）。
      红圈 = 还没说，蓝圈 = 你填的，绿圈 = 游戏里读的。按住拖动、滚轮缩放，右上「复位」回到整图。</div>
      <div id="bpaGateOver" class="bpa-img ds-gover"><img src="${t.overview}" draggable="false" alt=""></div>` : ''}
    <div class="btn-row ds-gbar">筛选 <select id="bpaGateFilter" onchange="bpa.gFilter=this.value;bpaGateRows()">${Object.entries(GATE_FILTERS).map(([k, [l]]) =>
        `<option value="${k}"${k === bpa.gFilter ? ' selected' : ''}>${l}（${n(k)}）</option>`).join('')}</select>
      <span class="muted">排序：先放你填过的和游戏里读到开着的，再放有料经过、还没说的，最后是输入端和没追到料的；同一种物品按位置从上到下、从左到右，和总览图的编号一样。每行的小图点开看大图。</span></div>
    <div id="bpaGateRows" class="ds-gwrap"></div>
    <div class="btn-row"><button class="btn sm primary" id="bpaGateSaveBtn" onclick="bpaGateSave()">保存并重算（改了 0 个口）</button>
      <button class="btn sm ghost" id="bpaGateOffBtn" onclick="bpaGateAllOff()">上面没说的这些都没开</button></div>
    ${bpaGateStale(t)}`;
  if (t.overview) panZoom($('#bpaGateOver'));
  bpaGateRows();
}
function bpaGateRows() {
  const t = bpa.gt, el = $('#bpaGateRows'); if (!t || !el) return;
  const f = (GATE_FILTERS[bpa.gFilter] || GATE_FILTERS.show)[1];
  const body = t.rows.map((r, i) => f(r) ? bpaGateRow(r, i) : '').join('');
  el.innerHTML = `<table class="bpa-table ds-gtable"><tr><th>#</th><th>位置</th><th>物品</th><th>从哪来 → 到哪去</th><th>设计分析里</th><th>限制速度（每分钟）</th>
      <th>限制通过数量<div class="muted gnote">一次性，放满就停：设计分析按不送料算</div></th><th>来源</th></tr>
    ${body || '<tr><td colspan="8" class="muted">这个筛选下没有口</td></tr>'}</table>`;
  bpaGateBtns();
}
function bpaGateRow(r, i) {
  const e = bpa.gEdits[r.key], undo = !!(e && e.undo && r.src === '作者'), off = !r.key || undo;
  const bad = !!(e && !e.undo && Number.isNaN(e.count));
  const cnt = e && !e.undo ? (e.raw ?? '') : (r.count ?? '');
  const cls = r.src === '作者' ? 'mine' : r.src === '游戏' ? 'read' : '';
  const pic = r.thumb ? `<img class="ds-gthumb" src="${r.thumb}" alt=""${r.key ? ` title="点开看大图" onclick="bpaGateBig(${i})"` : ''}>` : '';
  return `<tr id="gtr${i}" class="${bpaGateDirty(r) ? 'dirty' : ''}"><td class="gno ${cls}">${r.no}</td>
    <td>${pic}<div class="muted">(${Math.round(r.c[0])},${Math.round(r.c[1])})</div></td>
    <td>${bpaGateItem(r)}</td><td class="gends">${bpaGateEnds(r)}</td><td class="gflow">${bpaGateFlow(r)}</td>
    <td>${bpaGateLimitSel(r, i, e, off)}</td>
    <td class="gcnt-c"><input class="gcnt${bad ? ' bad' : ''}" inputmode="numeric" size="6" placeholder="不限量" value="${esc(cnt)}" title="${bad ? GATE_CNT_BAD : GATE_CNT_TIP}"
      oninput="bpaGateEdit(${i})"${off ? ' disabled' : ''}> <span class="muted" title="${GATE_CNT_TIP}">一次性</span></td>
    <td class="gsrc">${bpaGateSrc(r, i)}</td></tr>`;
}
// 物品：口上认出来的；没认出来、只过了一种料的用那种料（接口的 item_name），另外标一下
function bpaGateItem(r) {
  const it = r.item_name, ic = it && (bpa.gt.icons || {})[it];
  const name = it ? `${ic ? `<img class="ds-gicon" src="${ic}" alt="">` : ''}${esc(it)}`
    + (r.item ? '' : ' <span class="muted" title="口上的图标没认出来，按设计分析里过这个口（或被这个口挡下）的料写的">（按过口的料）</span>') : '<span class="muted">看不出</span>';
  return `${name}<div class="muted">${r.kind ? esc(r.kind) + '准入口' : '类型没定的准入口'}</div>`;
}
// 上下游最近的设备（穿过物流件找），坐标是拼合图坐标。一边有几台时标「进1」「进2」…，和小图、大图上的标签一样（接口 _gate_ends）
function bpaGateEnds(r) {
  const devs = (ds, tag) => ds.map((d, k) => `${ds.length > 1 ? `<b>${tag}${k + 1}</b> ` : ''}${esc(d.name)} (${Math.round(d.c[0])},${Math.round(d.c[1])})`).join('、');
  const from = r.group === '输入端' ? '基地外面' : (r.from || []).length ? devs(r.from, '进') : '<span class="muted">上游没追到</span>';
  const to = (r.to || []).length ? devs(r.to, '出') : '<span class="muted">下游没追到</span>';
  return `从 ${from} → 到 ${to}`;
}
// 设计分析里这个口过了多少、被挡下多少、下游满速要多少。输入端不写算出来的流量（外接暗管会重复算，出现 180/分），写作者在输入端页填的
// 开了限制通过数量（count 有数）的口设计分析按不放行算：上游是生产设备时被压成 0，没有「通过 / 被挡下」的量，写清是它挡的、挡的什么料（shut）
function bpaGateFlow(r) {
  const once = r.count != null;
  if (r.group === '输入端') return `输入端 ${esc(r.input_no)} 号，` + (r.input_rate != null ? `填的 ${gnum(r.input_rate)}/分` : '输入端页没填流速')
    + (once ? '；<b class="gheld">开了限制通过数量，设计分析按不来料算</b>' : '');
  const f = r.flow || {}, h = r.held || {}, its = [...new Set([...Object.keys(f), ...Object.keys(h)])];
  // 下游一路去仓库、绕回上游、没追到头：接口给 null；是 0：下游那台不吃这种料，或者连接没追到
  const dn = r.downstream == null ? '下游用多少看不出（有一路去仓库、绕回上游或没追到头）'
    : +r.downstream === 0 ? ((r.to || []).length ? '下游的设备不用这种料（或后面还有口挡住、连接没追到）' : '下游没追到设备')
    : `下游满速要 ${gnum(r.downstream)}/分`;
  if (!its.length) {
    if (!once) return '<span class="muted">没有料经过</span>';
    const sh = r.shut || [];
    // shut 空的分开说（复审 2026-11-21）：上游没追到设备；出边没追到下游设备（下游那句 dn 已经说了，上游追到了不能说成没追到）；
    // 上下游都追到了，是上游这一路没有料（上游设备自己没料、分料时分不到这一路，或前面还有一个口已经挡住：两个限量口串着，A2 最后一轮复审）
    const why = sh.length ? `：上游要往这里送${esc(sh.join('、'))}`
      : !(r.from || []).length ? '；没追到上游设备'
      : !(r.to || []).length ? ''
      : '；分析里上游没有料要往这里送（上游设备自己没料、分料时分不到这一路，或前面还有口已经挡住）';
    return '<b class="gheld">开了限制通过数量，设计分析按不放行算（长期 0）</b>' + why + `；${dn}`;
  }
  // 开了限制通过数量的口，shut 里还有推料账上没有的料（上游是生产设备、被背压压成 0）：一并写出来，料名也写（物品一栏这时是「看不出」，A2 最后一轮复审）
  const extra = once ? (r.shut || []).filter(k => !its.includes(k)) : [];
  const one = its.length === 1 && !extra.length;
  const part = its.map(k => (one ? '' : esc(k) + ' ') + `通过 ${gnum(f[k])}/分` + (h[k] ? `，<b class="gheld">被挡下 ${gnum(h[k])}/分</b>` : '')).join('；')
    + (extra.length ? `；<b class="gheld">上游还要往这里送${esc(extra.join('、'))}</b>（按不放行算）` : '');
  // 已知问题：几路来料在口前汇到一起时，设计分析按每一路各限一次速，合计会超过限速。照实写出来，不当成正常数
  const tot = Object.values(f).reduce((a, v) => a + (+v || 0), 0);
  const over = r.limit != null && tot > +r.limit + 0.01
    ? `<div class="gheld">几路来料各按限速 ${gnum(r.limit)}/分算了一次，合计超过了限速：设计分析这个口算多了（已知问题，以后修）</div>` : '';
  return `${part}；${dn}${over}`;
}
// 限制速度只给下拉（游戏里每档 6）：物品准入口 0–30，管道准入口、类型没定的口 0–60
function bpaGateLimitSel(r, i, e, off) {
  const top = r.kind === '物品' ? 30 : 60, cur = e && !e.undo ? e.limit : (r.limit ?? null);
  const vals = []; for (let v = 0; v <= top; v += 6) vals.push(v);
  if (cur != null && !vals.includes(cur)) vals.push(cur);  // 游戏里读到的不在档上：照样显示出来
  const tip = r.kind === '物品' ? '游戏里每档 6，物品准入口最多 30/分' : '游戏里每档 6，管道准入口最多 60/分；想超过 60 只能关掉限速';
  return `<select title="${tip}" onchange="bpaGateEdit(${i})"${off ? ' disabled' : ''}><option value=""${cur == null ? ' selected' : ''}>不限速</option>${vals.map(v =>
    `<option value="${v}"${v === cur ? ' selected' : ''}>${gnum(v)}</option>`).join('')}</select>`;
}
function bpaGateReadWords(rd) {
  const full = rd.passed != null && rd.count_limit != null && +rd.passed >= +rd.count_limit;  // 读到已经放满：标出来（开着就按不放行算，放没放满不影响结果）
  // 读到开着、数量没读出来（count_limit 是 null）：不能写成「限制通过数量 0」。这段只用在「你改的（游戏里读到…）」的括号里：
  // 不再套一层括号，也不写「设计分析不按它挡」（这时按你填的算，容易看成现在不挡。复审 2026-11-21）
  const cnt = rd.count_limit == null ? '，限制通过数量开着、数量没读出来'
    : `，限制通过数量 ${gnum(rd.count_limit)}${full ? '（已放满）' : ''}`;
  return (rd.limit_on ? ` ${gnum(rd.limit)}` : '：不限速') + (rd.count_on ? cnt : '');
}
function bpaGateSrc(r, i) {
  if (!r.key) return '<span class="muted">这个口在蓝图里没有位置数据，填不了</span>';
  const e = bpa.gEdits[r.key];
  if (e && e.undo && r.src === '作者') return `<span class="gtag edit">撤销，还没存</span> <a class="undo" onclick="bpaGateRevert(${i})">不撤了</a>
    <div class="muted">存了以后${r.read ? '回到游戏里读的' : '回到没说（按不限速算）'}</div>`;
  if (bpaGateDirty(r)) return `<span class="gtag edit">改了，还没存</span> <a class="undo" onclick="bpaGateRevert(${i})">不改了</a>`;
  if (r.src === '作者') {
    const w = r.read ? `你改的（游戏里读到${bpaGateReadWords(r.read)}）` : r.limit == null && r.count == null ? '你说都没开' : '你填的';
    const at = r.fix && r.fix.at ? ` title="${esc('填的时间：' + r.fix.at)}"` : '';
    return `<span class="gtag mine"${at}>${w}</span> <a class="undo" onclick="bpaGateUndo(${i})">撤销</a>`;
  }
  if (r.src === '游戏') return `<span class="gtag read">游戏里读的${bpa.gt.read_date ? `（${esc(bpa.gt.read_date)}）` : ''}</span>`
    // 读到限制通过数量开着、数量没读出来：输入框只能显示「不限量」，在这里说一声
    + (r.read && r.read.count_on && r.read.count_limit == null ? '<div class="muted">限制通过数量开着，数量没读出来：设计分析不按它挡，可以照游戏里填上数</div>' : '');
  // 档案里这个口读失败了（画面里没找到图标、点开的不是准入口…）：不当读数，和没说一样按不限速算，可以照游戏里填
  if (r.read_error) return `<span class="muted" title="${esc('读的时候：' + r.read_error)}">游戏里没读出来（按不限速算）</span>`;
  return '<span class="muted">没说（按不限速算）</span>';
}
// 改没改：撤销只对作者填过的口算数；其余和现在生效的值（作者填的 / 游戏里读的 / 没说 = 都没开）比，限制通过数量填错了也算改了（存的时候拦）
function bpaGateDirty(r) {
  const e = r && r.key && bpa.gEdits[r.key]; if (!e) return false;
  if (e.undo) return r.src === '作者';
  return Number.isNaN(e.count) || e.limit !== (r.limit ?? null) || e.count !== (r.count ?? null);
}
function bpaGateCnt(raw) {
  const s = String(raw || '').trim();
  if (!s) return null;
  return /^\d+$/.test(s) && +s >= 1 && +s <= 5000 ? +s : NaN;
}
function bpaGateEdit(i) {
  const t = bpa.gt, r = t && t.rows[i], tr = $(`#gtr${i}`); if (!r || !tr || !r.key) return;
  const sv = tr.querySelector('select').value, raw = tr.querySelector('.gcnt').value;
  bpa.gEdits[r.key] = { limit: sv === '' ? null : +sv, count: bpaGateCnt(raw), raw };
  if (!bpaGateDirty(r)) delete bpa.gEdits[r.key];
  const bad = Number.isNaN(bpaGateCnt(raw)), inp = tr.querySelector('.gcnt');
  inp.classList.toggle('bad', bad); inp.title = bad ? GATE_CNT_BAD : GATE_CNT_TIP;
  tr.classList.toggle('dirty', bpaGateDirty(r));
  tr.querySelector('.gsrc').innerHTML = bpaGateSrc(r, i);
  bpaGateBtns();
}
function bpaGateRedraw(i) {
  const tr = $(`#gtr${i}`); if (tr) tr.outerHTML = bpaGateRow(bpa.gt.rows[i], i);
  bpaGateBtns();
}
// 撤销先记着（行变黄），和别的改动一起「保存并重算」，只重算一次
function bpaGateUndo(i) {
  const r = bpa.gt && bpa.gt.rows[i]; if (!r || !r.key) return;
  bpa.gEdits[r.key] = { undo: true };
  bpaGateRedraw(i);
  toast('已标成撤销，点下面「保存并重算」才生效');
}
function bpaGateRevert(i) {
  const r = bpa.gt && bpa.gt.rows[i]; if (!r) return;
  delete bpa.gEdits[r.key];
  bpaGateRedraw(i);
}
// 改了还没存的 → 存的时候给接口的 {准入口键: {limit, count} 或 null（撤销）}；限制通过数量填错了 → 报出来、返回 null
function bpaGateEntries() {
  const ent = {}, bad = [];
  (bpa.gt ? bpa.gt.rows : []).forEach(r => {
    if (!bpaGateDirty(r)) return;
    const e = bpa.gEdits[r.key];
    if (e.undo) ent[r.key] = null;
    else if (Number.isNaN(e.count)) bad.push('#' + r.no);
    else ent[r.key] = { limit: e.limit, count: e.count };
  });
  if (bad.length) { toast(`${bad.join('、')} 的限制通过数量要填 1–5000 的整数，空着 = 不限量`); return null; }
  return ent;
}
const bpaGateDirtyN = () => (bpa.gt ? bpa.gt.rows.filter(bpaGateDirty).length : 0);
// 当前筛选里还没说的口（作者没填、游戏里也没读到，也没在改）
const bpaGateUnsaid = () => (bpa.gt ? bpa.gt.rows.filter(r => r.key && r.src == null && !bpaGateDirty(r) && (GATE_FILTERS[bpa.gFilter] || GATE_FILTERS.show)[1](r)) : []);
// 保存、都没开：改了 0 个 / 有任务在跑 / 表还是换之前那个项目、据点的时变灰（bpaWriteState 每次轮询都调，换项目、据点时也调）。
// 删、重算在任务跑时、表没换过来时也变灰
function bpaGateBtns(running = state.job?.state === 'running') {
  const mism = !!bpa.gt && !bpaGateMatch(bpa.gt), stop = running || mism;
  const why = running ? '有任务在跑，跑完再点' : mism ? GATE_SWITCH : '';
  const s = $('#bpaGateSaveBtn');
  if (s) { const n = bpaGateDirtyN(); s.disabled = stop || !n; s.textContent = `保存并重算（改了 ${n} 个口）`; s.title = running ? '有任务在跑，跑完再保存' : why || (!n ? '还没改' : ''); }
  const o = $('#bpaGateOffBtn');
  if (o) { const k = bpaGateUnsaid().length; o.disabled = stop || !k; o.title = why || (!k ? '当前筛选里没有还没说的口' : `把当前筛选里还没说的 ${k} 个口记成都没开`); }
  document.querySelectorAll('#bpaDsGatesBody .gate-act').forEach(b => { b.disabled = stop; b.title = why; });
}
// 准入口表的写操作只认表载入时的项目、据点（bpa.gt.name / base）：换了项目或据点、新表还没回来时不存，免得存进别的项目
function bpaGateCur() {
  const t = bpa.gt;
  if (!bpaGateMatch(t)) { toast(GATE_SWITCH); return null; }
  return t;
}
async function bpaGateSave() {
  const t = bpaGateCur(); if (!t || bpaJobBusy()) return;
  const ent = bpaGateEntries(); if (!ent) return;
  const n = Object.keys(ent).length; if (!n) return toast('还没改');
  const b = $('#bpaGateSaveBtn'); if (b) b.disabled = true;  // 防连点：任务开起来以后由 bpaWriteState 管，开不起来 bpaFixJob 会放开
  bpaFixJob('bpa_fix_gates', t.base, [ent], `已记下 ${n} 个口，正在重算这个据点（进度看上面的任务日志；程序启动后第一次算较慢）`, t.name);
}
// 「上面没说的这些都没开」（card 为空）/ 待确认汇总卡的「这 N 个口都没开」（card = 卡片序号）：给还没说的口存两项都没开的记录，
// 表里改了还没存的一起存，只重算一次。卡上的键来自 bpa.ds（载入设计分析那次的项目、据点），表上的来自 bpa.gt：都要是现在选的
async function bpaGateAllOff(card) {
  if (bpaJobBusy()) return;
  const fromCard = card != null, at = fromCard ? ((bpa.ds || {}).at || {}) : (bpa.gt || {});
  if (!bpaGateMatch(at)) return toast(GATE_SWITCH);
  const keys = fromCard ? ((((bpa.ds || {}).pending || [])[card] || {}).open_keys || []) : bpaGateUnsaid().map(r => r.key);
  const ent = bpa.gt && bpa.gt.base === at.base && bpa.gt.name === at.name ? bpaGateEntries() : {};
  if (!ent) return;
  const n = Object.keys(ent).length, add = keys.filter(k => !(k in ent));
  if (!add.length && !n) return toast('没有还没说的口');
  const msg = `把${fromCard ? '' : '当前筛选里'}还没说的 ${add.length} 个口记成「限制速度、限制通过数量都没开」`
    + (n ? `，表里改了还没存的 ${n} 个口一起存` : '') + '，然后重算这个据点（一两分钟）？\n\n游戏里开了限速的口别点这个，去表里照着填。';
  if (!(await uiConfirm(msg, { ok: '都没开', title: '准入口限速' }))) return;
  add.forEach(k => { ent[k] = { limit: null, count: null }; });
  bpaFixJob('bpa_fix_gates', at.base, [ent], `已记下 ${Object.keys(ent).length} 个口，正在重算这个据点（进度看上面的任务日志；程序启动后第一次算较慢）`, at.name);
}
// 旧版本算的据点没有准入口表：不带改动的修正任务 = 只重算一次
function bpaGateRerun() {
  const t = bpaGateCur(); if (!t) return;
  bpaFixJob('bpa_fix_gates', t.base, [{}], '正在重算这个据点、生成准入口表（进度看上面的任务日志；程序启动后第一次算较慢）', t.name);
}
// 小图点开：480 px 大图，写「进：…」「出：…」；stale = 「对不上的旧设置」那一行（虚线圈 = 当时存的位置）
async function bpaGateBig(i, stale) {
  const t = bpa.gt; if (!t) return;
  const r = stale ? (t.stale || [])[i] : t.rows[i]; if (!r || !r.key) return;
  let res;
  try { res = await api('bpa_gate_image', t.name, t.base, r.key); } catch (e) { res = { ok: false, error: String(e && e.message || e) }; }
  if (!res.ok) return toast(res.error);
  lightbox(res.image, stale ? `对不上的旧设置（蓝图「${r.bp || '?'}」）：虚线圈 = 当时存的位置换到现在拼合图上的地方。${r.why || ''}`
    : `#${r.no} ${r.item_name || '物品看不出'} · ${r.kind ? r.kind + '准入口' : '类型没定的准入口'} (${Math.round(r.c[0])},${Math.round(r.c[1])})：`
      + '红圈 = 这个口，绿底「进」= 上游的设备，蓝底「出」= 下游的设备（一边有几台时按表里的 1、2、3 标）');
}
// 「对不上的旧设置」：重新拼合、换了蓝图以后找不到原来那个口的记录，不参加计算。蓝图还在据点里的配虚线圈小图，不在了只写字
function bpaGateStale(t) {
  const st = t.stale || []; if (!st.length) return '';
  const rows = st.map((s, i) => `<tr><td>${s.thumb ? `<img class="ds-gthumb" src="${s.thumb}" alt="" title="点开看大图" onclick="bpaGateBig(${i},1)">` : ''}
      <div class="muted">${s.c ? `旧位置 (${Math.round(s.c[0])},${Math.round(s.c[1])})` : '蓝图不在这个据点里了，标不出位置'}</div></td>
    <td>${esc(s.item || '看不出')}<div class="muted">${s.kind ? esc(s.kind) + '准入口' : '准入口'}</div></td>
    <td>${s.limit != null ? `限制速度 ${gnum(s.limit)}/分` : '不限速'}<br>${s.count != null ? `限制通过数量 ${gnum(s.count)}` : '不限量'}</td>
    <td>蓝图「${esc(s.bp || '?')}」<div class="muted">${esc(s.at || '')}</div></td><td class="muted">${esc(s.why || '')}</td>
    <td><button class="btn sm ghost gate-act" onclick="bpaGateStaleDel(${i})">删</button></td></tr>`).join('');
  return `<div class="sub-h"><b>对不上的旧设置</b>（${st.length} 条）</div>
    <div class="hint">拼合图变过，或那张蓝图不在这个据点里了，下面这些不参加计算。请在上面表里找到对应的口重新填，核对完点「删」（删了也要重算一次这个据点，这里才更新）。
      <button class="btn sm ghost gate-act" onclick="bpaGateStaleDel(-1)">全部删掉</button></div>
    <table class="bpa-table ds-gtable">${rows}</table>`;
}
async function bpaGateStaleDel(i) {
  const t = bpaGateCur(); if (!t || bpaJobBusy()) return;
  const st = i < 0 ? (t.stale || []) : [(t.stale || [])[i]].filter(Boolean);
  if (!st.length) return;
  const s = st[0], what = `${s.item || '物品看不出'}，${s.limit != null ? `限制速度 ${gnum(s.limit)}` : '不限速'}，蓝图「${s.bp || '?'}」`;
  const msg = (i < 0 ? `删掉这 ${st.length} 条对不上的旧设置？` : `删掉这条对不上的旧设置（${what}）？`) + '\n\n它们本来就不参加计算；删了以后重算一次这个据点，这里才更新。';
  if (!(await uiConfirm(msg, { ok: '删掉', danger: true, title: '对不上的旧设置' }))) return;
  const ent = {}; st.forEach(x => { ent[x.key] = null; });
  bpaFixJob('bpa_fix_gates', t.base, [ent], '正在删掉并重算这个据点（进度看上面的任务日志）', t.name);
}
// 待确认里的准入口汇总卡（别人的蓝图、没有游戏读数时才有）：小图是只标这几个口的总览图，编号和表里的 # 一样
function bpaGateCard(p, i) {
  const a = p.answered || {}, said = (a.filled || 0) + (a.off || 0);
  const ans = said ? `<div class="muted">已答：填了 ${a.filled || 0} 个，说没开的 ${a.off || 0} 个${p.n_open ? `，还有 ${p.n_open} 个没说` : ''}</div>` : '';
  const img = p.image ? `<img class="zoomable" src="${p.image}" alt="准入口总览：编号和下面「准入口限速」表里的 # 一样；红圈 = 还没说，蓝圈 = 你填的">` : '';
  return `<div class="pend-item${p.open ? '' : ' done'}">${img}
    <div class="pend-body"><div class="pend-msg">${esc(p.msg)}</div>
      <div class="pend-opts"><button class="btn sm${p.open ? '' : ' ghost'}" onclick="bpaGateGo()">去表里填</button>
        ${p.n_open ? `<button class="btn sm ghost" onclick="bpaGateAllOff(${i})">这 ${p.n_open} 个口都没开</button>` : ''}</div>${ans}</div></div>`;
}
// 「去表里填」：展开准入口表（展开时载入）、筛选回到有料经过的、滚过去
function bpaGateGo() {
  const d = $('#bpaDsGates'); if (!d) return;
  bpa.gFilter = 'show';
  if ($('#bpaGateFilter')) { $('#bpaGateFilter').value = 'show'; bpaGateRows(); }
  d.open = true;
  d.scrollIntoView({ behavior: 'smooth', block: 'start' });
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
  const rows = (bpa.dsRows || []).filter(d => !d.confirm && !d.recipe_unknown);  // 配方未知的行没有可确认的结果（2026-11-19）
  if (!rows.length) return toast('这一页没有未确认的设备');
  if (!(await uiConfirm(`把这一页 ${rows.length} 台还没确认的设备都确认为「对」？请先对着游戏核对过。`, { ok: '全部确认' }))) return;
  const entries = {}; rows.forEach(d => { entries[d.key] = { verdict: 'ok', actual: '', note: '' }; });
  const res = await api('bpa_confirm', bpa.name, $('#bpaDsBase').value, 'devices', entries);
  if (!res.ok) return toast(res.error);
  rows.forEach(d => { d.confirm = entries[d.key]; });
  bpaProgress(res.progress); bpaDesignTable();
}
// 池子配方从哪来（行上的 pool_src）→ [标签文字, 醒目不醒目]：推断、照抄同类没核实，标醒目。不明 = 配方未知，那一行另写
const POOL_SRC = { '面板': ['面板', false], '作者': ['作者指定', false], '邻居推断': ['推断', true], '照抄同类': ['照抄同类', true] };
// 配方列（审计第 3 项，2026-11-19）：并行池子每条一行带开工率；几条首尾相接并成的净配方悬停看组成；配方未知的行指向待确认。
// 配方、物品文字都 esc；按钮只带行号
function dsRecipeCell(d, i) {
  const pct = v => `${Math.round(v * 100)}%`;
  const made = m => (m || []).length > 1 ? { attr: ` title="${esc('由这几条首尾相接并成：' + m.join('；'))}"`, note: ` <span class="muted">（${m.length} 条并成）</span>` } : { attr: '', note: '' };
  if (d.recipe_unknown) {
    const go = 'pool_src' in d ? ` <a class="undo" onclick="bpaGoPending(${i})">去待确认选</a>` : '';
    return `<b style="color:${DS_COLORS['配方未知']}">配方未知</b>${go}`;  // 这种机器能做什么写在依据第二条，这里不重复
  }
  const body = (d.channels || []).length ? d.channels.map(c => { const m = made(c.members);
    return `<div class="ds-ch"${m.attr}>${esc(c.recipe)} · <b>${pct(c.rate)}</b>${m.note}</div>`; }).join('')
    : (m => `<span${m.attr}>${esc(d.recipe || '')}</span>${m.note}`)(made(d.members));
  const ps = POOL_SRC[d.pool_src];
  const src = ps ? `<span class="tag${ps[1] ? ' guess' : ''}" title="池子配方从哪来">${ps[0]}</span>` : '';
  // 作者说了出口送原料的池子（0.10.13 功能三）：到手多少、转给下游多少、自己用多少放在悬停里
  const po = d.pass_out || [];
  const pass = po.length ? `<span class="tag pass" title="${esc(po.map(x => bpaPoolPassWords(x, true)).join('；') + '（作者说的）')}">出口送原料</span>` : '';
  const fix = 'pool_src' in d ? ` <button class="btn sm ghost" onclick="bpaRowCard(${i})">改配方</button>` : '';
  return body + src + pass + fix;
}
// 行上的「改配方」：打开和待确认同样的池子卡（bpa_pool_card），不进待确认列表、不算进「待确认 N 处」
async function bpaRowCard(i) {
  const d = (bpa.dsRows || [])[i]; if (!d) return;
  bpaKeyCard(d.key, d.device, d.c, '从表格行打开');
}
// 「没有进料」卡上的池子：待确认里已有这台的池子卡就滚过去，没有就在表格上方打开同一张卡
function bpaPendPoolCard(i) {
  const p = ((bpa.ds || {}).pending || [])[i]; if (!p) return;
  const k = bpa.ds.pending.findIndex(q => q.kind === '池子配方' && q.key === p.key);
  const el = k >= 0 ? $(`#pendPool${k}`) : null;
  if (el) return el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  bpaKeyCard(p.key, p.device.split('[')[0], p.c, '从进料卡打开');
}
async function bpaKeyCard(key, device, c, from) {
  const r = await api('bpa_pool_card', bpa.name, $('#bpaDsBase').value, key);
  if (!r.ok) return toast(r.error);
  bpa.rowCard = r.card;
  const box = $('#bpaDsRowCard');
  box.innerHTML = `<div class="pend-head"><b>改配方</b>：${esc(device)} (${c[0]},${c[1]})<span class="muted">（${from}，不算进待确认）</span></div>${bpaPoolCard(r.card, 'row')}`;
  bpaPoolCount('row');
  box.scrollIntoView({ block: 'nearest' });
}
function bpaRowCardClose() {
  bpa.rowCard = null;
  const box = $('#bpaDsRowCard'); if (box) box.innerHTML = '';
}
function bpaGoPending(i) {
  const d = (bpa.dsRows || [])[i]; if (!d) return;
  const k = ((bpa.ds || {}).pending || []).findIndex(p => p.kind === '池子配方' && p.key === d.key);
  const el = k >= 0 ? $(`#pendPool${k}`) : null;
  if (!el) return toast('待确认里没有这台的卡片（重算一下再看）');
  el.scrollIntoView({ behavior: 'smooth', block: 'center' });
}
function bpaDesignTable() {
  const r = bpa.ds; if (!r) return;
  const f = $('#bpaDsFilter').value;
  const rows = r.devices.filter(d => f === 'all' || (f === 'bad' && d.rate < 0.995) || (f === 'inferred' && (d.via_inferred || d.recipe_inferred)) || (f === 'env' && d.env_required)
      || (f === 'unknown' && d.recipe_unknown)
      || (f === 'unconfirmed' && !d.confirm && !d.recipe_unknown) || (f === 'wrong' && d.confirm && d.confirm.verdict === 'wrong'))
    .sort((a, b) => a.rate - b.rate || a.device.localeCompare(b.device));
  bpa.dsRows = rows;
  const pct = v => `${Math.round(v * 100)}%`;
  $('#bpaDsTable').innerHTML = `<table class="bpa-table"><tr><th>设备</th><th>位置</th><th>配方</th><th>开工率</th><th>状态</th><th>依据</th><th>作者确认</th></tr>${rows.map((d, i) => {
    const tags = [d.recipe_inferred ? '<span class="tag">推断配方</span>' : '', d.env_required ? '<span class="tag">稳定环境</span>' : '', d.via_inferred ? '<span class="tag inf">经推断连线</span>' : ''].join('');
    const basis = (d.basis || []).map(b => esc(b).replace(/〔推断连线〕/g, '<b class="inf">〔推断连线〕</b>')).join('<br>');
    const noRate = d.recipe_unknown || dsKey(d.state) === '没在用';  // 开工率没算（rate 记 0.0），写「?」不写 0%，和设计分析图一样
    const conf = d.recipe_unknown ? '<span class="muted">配方定了再确认</span>' : confirmCell('devices', i, d.confirm, r.actuals);
    return `<tr><td>${esc(d.device)}</td><td class="muted">(${d.c[0]},${d.c[1]})</td><td>${dsRecipeCell(d, i)}${tags}</td>
      <td style="color:${DS_COLORS[dsKey(d.state)]};font-weight:700">${noRate ? '?' : pct(d.rate)}</td><td style="color:${DS_COLORS[dsKey(d.state)]}">${esc(d.state)}</td>
      <td class="muted ds-basis">${basis}</td><td class="ds-conf">${conf}</td></tr>`; }).join('')}</table>`;
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
  if (bpaJobBusy()) return;
  const btn = $('#bpaSaveGates');
  if (btn) { if (btn.disabled) return; btn.disabled = true; }  // 防连点：任务开起来以后由 setRunningUI 管，开不起来再放开
  const labels = {};
  document.querySelectorAll('#bpaGateTable tr[data-k]').forEach(tr => {
    const v = tr.querySelector('.gst').value;
    if (v) labels[tr.dataset.k] = { state: v, item: tr.querySelector('.gnm').value };
  });
  const r = await api('bpa_label_gates', bpa.name, labels);
  if (!r.ok) { if (btn) btn.disabled = false; return toast(r.error); }
  toast('已保存，正在重新拼合（进度看上面的任务日志）');
  $('#bpaLog').style.display = ''; startPolling('bpa');
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
async function bpaRevealImage() {
  // 在资源管理器里选中当前这张图的原图（界面里的是缩小过的）
  const base = $('#bpaBase').value; if (!bpa.name || !base) { toast('先选一个项目和据点'); return; }
  const r = await api('bpa_reveal_image', bpa.name, base, $('#bpaWhich').value);
  toast(r.ok ? '已在文件夹里选中这张图的原图' : r.error);
}
async function bpaInputs() {
  const base = $('#bpaInBase').value; if (!bpa.name || !base) return;
  const r = await api('bpa_inputs', bpa.name, base);
  if (!r.ok) { $('#bpaInImage').innerHTML = `<div class="empty">${esc(r.error)}</div>`; $('#bpaInTable').innerHTML = ''; return; }
  bpa.base = base; bpa.scale = r.scale;
  bpa.marks = r.marks.map(m => Object.assign({ item: '', rate: '', merged: false, via: '', note: '' }, m));
  bpa.dropped = r.dropped || [];
  bpa.stale = r.stale || []; bpa.items = new Set(r.items || []);
  bpa.inSig = bpaInSig();  // 载入时的样子：改了没存要和它比（bpaInDirty）
  $('#bpaInImage').innerHTML = `<img src="${r.image}" draggable="false" onclick="bpaAddMark(event)">`;
  panZoom($('#bpaInImage'));
  bpaRenderTable();
}
// 「输入端」页有没有改了还没存的（表里的改动直接写进 bpa.marks，和载入时比）。暗管来料卡（0.10.13）也写 inputs.json，用它决定要不要先问
const bpaInSig = () => JSON.stringify([bpa.marks, bpa.stale]);
const bpaInDirty = () => !!bpa.inSig && bpaInSig() !== bpa.inSig;
const bpaInSame = at => !!(at && bpa.base && bpa.name === at.name && bpa.base === at.base);
// 暗管来料的任务跑完：「输入端」页开着同一个项目、据点就重新载入。发出去之前没存的改动作者已经说了不要（bpaTunFix 问过）；
// 跑的时候又在那一页改了的也会丢，说一声
function bpaInReload(at) {
  if (!bpaInSame(at)) return;
  // pollJob 接着还要报「任务完成」：这句排在它后面，不被盖掉
  if (at.sig && bpaInSig() !== at.sig) setTimeout(() => toast('「输入端」页按刚存的暗管来料重新载入了，任务跑的时候在那一页改的没保留'), 0);
  bpaInputs();
}
function bpaRenderTable() {
  const vias = ['', '水泵', '暗管', '多口暗管', '其他'];
  // 物品名原来随手打字：打错了（「水」「重积酸」）分析里这个口送的东西没有设备吃，也没有任何提示（2026-11-14 issue #1）。
  // 现在带下拉（分析认得的全部物品名），不在列表里的标红
  const rows = bpa.marks.map((m, i) => `<tr><td class="no ${m.source === 'manual' ? 'manual' : ''}">${m.no}</td>
      <td><input list="bpaItemList" class="${bpaBadItem(m.item) ? 'bad' : ''}" title="${bpaBadItem(m.item) ? BPA_BAD_ITEM : ''}" value="${esc(m.item)}" onchange="bpaSetItem(${i}, this)"></td>
      <td><input value="${esc(m.rate ?? '')}" size="4" onchange="bpa.marks[${i}].rate=this.value"></td>
      <td><input type="checkbox" ${m.merged ? 'checked' : ''} onchange="bpa.marks[${i}].merged=this.checked"></td>
      <td><select onchange="bpa.marks[${i}].via=this.value">${vias.map(v => `<option ${v === m.via ? 'selected' : ''}>${v}</option>`).join('')}</select></td>
      <td><button class="btn sm ghost" onclick="bpaDelMark(${i})">删</button></td></tr>`).join('');
  const drops = (bpa.dropped || []).map((d, i) => `<tr><td class="no" style="color:#888">${d.tag}</td><td>${esc(d.item || '')}</td>
      <td colspan="3" class="muted">${esc(d.why)}</td><td><button class="btn sm ghost" onclick="bpaAddDropped(${i})">加回</button></td></tr>`).join('');
  const stale = (bpa.stale || []).map((s, i) => `<tr><td class="no" style="color:#888">${esc(s.no ?? '')}</td><td>${esc(s.item || '')}</td>
      <td>${esc(s.rate ?? '')}</td><td colspan="2" class="muted">${esc(s.why || '')}</td><td><button class="btn sm ghost" onclick="bpaDelStale(${i})">删</button></td></tr>`).join('');
  $('#bpaInTable').innerHTML = `<datalist id="bpaItemList">${[...(bpa.items || [])].map(n => `<option value="${esc(n)}">`).join('')}</datalist>`
    + `<table class="bpa-table"><tr><th>#</th><th>物品</th><th>每分钟</th><th>汇流口</th><th>接口</th><th></th></tr>${rows}</table>`
    + (drops ? `<div class="hint" style="margin-top:8px">贴边但程序判断不是输入端的准入口（图上灰色虚圈），判断错了就点「加回」：</div><table class="bpa-table">${drops}</table>` : '')
    + (stale ? `<div class="hint" style="margin-top:8px">对不上的旧标记：拼合图变过，它们存的位置现在不在准入口上。不参加分析、图上也不画，只留着给你看当时填的物品和流速；核对完上面的表就可以删。
      <button class="btn sm ghost" onclick="bpaDelStale(-1)">全部删掉</button></div><table class="bpa-table">${stale}</table>` : '');
}
const BPA_BAD_ITEM = '物品库里没有这个名字，分析里用不上。请从下拉里选一个';
function bpaBadItem(v) { return !!(v && bpa.items && bpa.items.size && !bpa.items.has(v)); }
function bpaSetItem(i, el) {
  const v = el.value.trim(); el.value = v; bpa.marks[i].item = v;
  el.classList.toggle('bad', bpaBadItem(v)); el.title = bpaBadItem(v) ? BPA_BAD_ITEM : '';
}
function bpaDelStale(i) { if (i < 0) bpa.stale = []; else bpa.stale.splice(i, 1); bpaSaveInputs(true); }
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
  if (bpaJobBusy(true)) return;  // 拼合任务会按新拼合图改写 inputs.json，这时存会互相盖掉
  const marks = bpa.marks.map(m => Object.assign({}, m, { rate: (m.rate === '' || m.rate === null || m.rate === undefined) ? null : Number(m.rate) }));
  const r = await api('bpa_save_inputs', bpa.name, bpa.base, marks, bpa.stale || []);
  if (!r.ok) return toast(r.error);
  const bad = (r.bad_items || []).map(b => `${b.no} 号「${b.item}」` + (b.suggest && b.suggest.length ? `（是不是${b.suggest.join('、')}？）` : ''));
  if (reload === true) bpaInputs(); else { toast(bad.length ? '已保存。有物品名认不出来，分析里用不上：' + bad.join('；') : '输入端已保存'); bpaRefresh(); }
}
