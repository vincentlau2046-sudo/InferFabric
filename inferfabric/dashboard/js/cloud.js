/* InferFabric Console — Cloud TAB (v2, Task 8)
 * 预设厂商网格（9 项，点击展开内联 API Key 表单）+ Provider 表（行内 测试/发现/删除）
 * + 手动配置表单 + 发现模型紧凑列表。
 *
 * 数据源（全部 /admin/cloud/*，UI.adminHeaders()）：
 *   - GET    /admin/cloud/presets    — {presets:[{id,display_name,openai_base,anthropic_base,env_var,discovery,model_count}]}
 *   - GET    /admin/cloud/providers  — {providers:[{name,enabled,openai_base,anthropic_base,discovery_enabled,discovery_interval,model_count,key_env_var,key_env_set,preset_id}], models:[…], total_cloud_models, last_discovery}
 *   - POST   /admin/cloud/providers  — 预设路径 {preset, api_key}；手动路径 {name, api_key, openai_base, anthropic_base}
 *   - DELETE /admin/cloud/providers  — {name}
 *   - POST   /admin/cloud/test       — {url: openai_base + '/models', api_key}
 *   - POST   /admin/cloud/discover   — 全部 {}；单个 {provider: <name>}
 *   - POST   /admin/cloud/reload     — {}
 *
 * 设计纪律（R13 / spec §4.5 §7）：
 *   - 零 emoji：preset `icon` 字段（emoji）一律不渲染，改用文本 monogram 徽章；
 *     状态全部 SVG 图标 + 文本标签双编码（已启用/已禁用、已设置/未设置、OpenAI/Anthropic 可用/不可用），
 *     绝不单独用颜色表达状态。
 *   - 数字（model_count / discovery_interval）等宽 + tabular-nums。
 *   - 删除走 UI.confirm danger（不 use 原生 confirm）。
 *   - 无 inline onclick：#tab-cloud 上事件委托（[data-action] + [data-provider]），
 *     预设卡为 role=button，Enter/Space 可触发。
 *   - 不新增 API（R10）：仅既有 /admin/cloud/* 端点。
 *
 * 暴露：
 *   - window.tabRenderers['tab-cloud'] = renderCloud；window.renderCloud
 *   - window.doCloudAdd() / window.doCloudAddPreset() / window.doCloudTest()
 *   - window.doCloudDiscover(name?) / window.doCloudDelete(name) / window.doCloudReload()
 */
(function () {
  'use strict';

  var UI = window.UI;
  var store = window.store;
  if (!UI || !store) {
    console.warn('[cloud] UI/store not ready — deferring');
    return;
  }

  var $ = function (id) { return document.getElementById(id); };

  var _presets = [];          // 预设缓存（GET /admin/cloud/presets）
  var _selectedPreset = null; // 当前选中预设
  var _providers = [];        // provider 缓存（行内 测试 需查 openai_base）

  /* ── XSS-safe 转义（与 inference.js escHtml 一致） ── */
  function esc(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  /* ── 预设 monogram（R13：icon 字段是 emoji 不渲染；
   *    拉丁名取前 2 字符大写，CJK 名取首字符） ── */
  function monogram(displayName) {
    var n = String(displayName || '').trim();
    if (!n) return '·';
    if (n.charCodeAt(0) < 128) return n.slice(0, 2).toUpperCase();
    return n.charAt(0);
  }

  function icon(name) { return UI.icon(name, 12); }

  /* ── 按钮 busy 态（provider 行按钮按 data-provider 精确定位） ──
   *   provider 为用户输入（手动 provider 名），不得拼进 CSS 选择器——
   *   名字含 " / [ / \ 会使 querySelectorAll 抛错（review fix#1）。
   *   改为按 data-action 取候选（action 为代码内固定字面量，安全），
   *   再 getAttribute 比较 data-provider，彻底避免注入。 */
  function setBtnBusy(action, busy, provider) {
    var els = document.querySelectorAll('[data-action="' + action + '"]');
    for (var i = 0; i < els.length; i++) {
      if (provider && els[i].getAttribute('data-provider') !== provider) continue;
      els[i].disabled = busy;
    }
  }

  /* ══ 预设：GET /admin/cloud/presets → #presetGrid ══ */
  async function cloudLoadPresets() {
    var grid = $('presetGrid');
    if (!grid) return;
    // 首次加载（无缓存）→ skeleton（spec §5 全 TAB 骨架；重取不闪）
    if (!_presets.length) UI.skeleton(grid, 3);
    try {
      var res = await fetch('/admin/cloud/presets', { headers: UI.adminHeaders() });
      var d = null;
      try { d = await res.json(); } catch (_) {}
      if (!res.ok || !d || d.error) {
        grid.innerHTML = '<div class="if-empty">加载失败 · ' +
          esc((d && d.error) || ('HTTP ' + res.status)) + '</div>';
        return;
      }
      _presets = d.presets || [];
      if (!_presets.length) {
        grid.innerHTML = '<div class="if-empty">无预设 — 用下方手动配置添加 Provider</div>';
        return;
      }
      var html = '';
      _presets.forEach(function (p) {
        // R13：预设的 icon 字段是 emoji，不渲染 → 文本 monogram 徽章
        var proto = [];
        if (p.openai_base) proto.push('OpenAI');
        if (p.anthropic_base) proto.push('Anthropic');
        var meta = proto.join(' · ');
        if (p.model_count != null) meta += (meta ? ' · ' : '') + p.model_count + ' 模型';
        var sel = _selectedPreset && _selectedPreset.id === p.id;
        html +=
          '<div class="cp-preset' + (sel ? ' selected' : '') + '" role="button" tabindex="0" ' +
            'aria-pressed="' + (sel ? 'true' : 'false') + '" ' +
            'data-action="preset-select" data-preset="' + esc(p.id) + '">' +
            '<span class="cp-mono" aria-hidden="true">' + esc(monogram(p.display_name)) + '</span>' +
            '<span class="cp-preset-body">' +
              '<span class="cp-preset-name">' + esc(p.display_name) + '</span>' +
              '<span class="cp-preset-meta">' + esc(meta) + '</span>' +
            '</span>' +
          '</div>';
      });
      grid.innerHTML = html;
    } catch (e) {
      grid.innerHTML = '<div class="if-empty">加载失败 · ' + esc(e.message || e) + '</div>';
    }
  }

  function cloudSelectPreset(id) {
    var p = null;
    for (var i = 0; i < _presets.length; i++) {
      if (_presets[i].id === id) { p = _presets[i]; break; }
    }
    if (!p) return;
    _selectedPreset = p;
    var grid = $('presetGrid');
    if (grid) {
      grid.querySelectorAll('.cp-preset').forEach(function (c) {
        var isSel = c.getAttribute('data-preset') === id;
        c.classList.toggle('selected', isSel);
        c.setAttribute('aria-pressed', isSel ? 'true' : 'false');
      });
    }
    var form = $('presetForm');
    if (form) form.style.display = '';
    var label = $('presetSelected');
    if (label) label.textContent = p.display_name || p.id;
    var key = $('cpPresetApiKey');
    if (key) { key.value = ''; key.focus(); }
    var hint = $('presetEnvHint');
    if (hint) {
      var env = p.env_var || ('IFF_' + String(p.id).toUpperCase().replace(/-/g, '_') + '_KEY');
      hint.textContent = 'Key 将存为 ' + env + '，写入 ~/.inferfabric/secrets.env';
    }
  }

  function cloudDeselectPreset() {
    _selectedPreset = null;
    var grid = $('presetGrid');
    if (grid) {
      grid.querySelectorAll('.cp-preset').forEach(function (c) {
        c.classList.remove('selected');
        c.setAttribute('aria-pressed', 'false');
      });
    }
    var form = $('presetForm');
    if (form) form.style.display = 'none';
  }

  /* ══ 预设添加：POST /admin/cloud/providers {preset, api_key} ══ */
  window.doCloudAddPreset = async function () {
    if (!_selectedPreset) { UI.toast('请先选择预设', 'error'); return; }
    var keyEl = $('cpPresetApiKey');
    var apiKey = keyEl ? keyEl.value.trim() : '';
    if (!apiKey) {
      UI.toast('请填写 API Key', 'error');
      if (keyEl) keyEl.focus();
      return;
    }
    var preset = _selectedPreset;
    try {
      var res = await fetch('/admin/cloud/providers', {
        method: 'POST',
        headers: UI.adminHeaders(),
        body: JSON.stringify({ preset: preset.id, api_key: apiKey }),
      });
      var d = null;
      try { d = await res.json(); } catch (_) {}
      if (!res.ok || !d || d.error) {
        UI.toast((d && d.error) || ('添加失败 · HTTP ' + res.status), 'error');
        return;
      }
      UI.toast('Provider ' + (preset.display_name || preset.id) + ' 已添加', 'success');
      if (preset.discovery) {
        // doCloudDiscover 内部 finally 会刷新 Provider 表（无论发现成败，review fix#2）
        await window.doCloudDiscover(preset.id);
      } else {
        await cloudLoadProviders();
      }
      cloudDeselectPreset();
    } catch (e) {
      UI.toast('添加失败: ' + (e.message || e), 'error');
    }
  };

  /* ── Provider 表加载骨架（UI.skeleton 同款 .skeleton/.skeleton-row，逐行占位；
   *    与 anomaly.js showSkeleton 同模式：detached div 生成后取 innerHTML 入 <tr>） ── */
  function providerSkeleton() {
    var tbody = $('provTbody');
    if (!tbody) return;
    var html = '';
    for (var i = 0; i < 3; i++) {
      var cell = document.createElement('div');
      UI.skeleton(cell, 1);
      html += '<tr class="cp-skel"><td colspan="6">' + cell.innerHTML + '</td></tr>';
    }
    tbody.innerHTML = html;
  }

  /* ══ Provider 表：GET /admin/cloud/providers → #provTable + #cloudModels ══ */
  function providerRow(p) {
    var stOn = !!p.enabled;
    // 状态：SVG 图标 + 文本标签双编码（spec §7：不得仅用颜色）
    var stHtml = stOn
      ? '<span class="cp-st on">' + icon('check') + '已启用</span>'
      : '<span class="cp-st off">' + icon('xmark') + '已禁用</span>';
    var oi = !!p.openai_base;
    var ai = !!p.anthropic_base;
    var protoHtml =
      '<span class="cp-chips">' +
        '<span class="cp-chip ' + (oi ? 'on' : 'off') + '">' + icon(oi ? 'check' : 'xmark') + 'OpenAI</span>' +
        '<span class="cp-chip ' + (ai ? 'on' : 'off') + '">' + icon(ai ? 'check' : 'xmark') + 'Anthropic</span>' +
      '</span>';
    // 模型数 + 发现间隔（数字等宽 tabular-nums）
    var intervalSub = (p.discovery_enabled && p.discovery_interval != null)
      ? '<span class="cp-sub">' + UI.fmtNum(p.discovery_interval) + 's 间隔</span>'
      : '';
    var countHtml =
      '<div class="cp-cell"><span class="cp-num mono">' + UI.fmtNum(p.model_count) + '</span>' +
      intervalSub + '</div>';
    // Key 状态：icon + 已设置/未设置 + 变量名
    var keyOn = !!p.key_env_set;
    var keyHtml =
      '<div class="cp-cell">' +
        '<span class="cp-key ' + (keyOn ? 'on' : 'off') + '">' + icon('key') + (keyOn ? '已设置' : '未设置') + '</span>' +
        (p.key_env_var ? '<span class="cp-sub">' + esc(p.key_env_var) + '</span>' : '') +
      '</div>';
    var nameAttr = esc(p.name);
    var canTest = !!p.openai_base;
    var opsHtml =
      '<div class="cp-ops">' +
        '<button type="button" class="btn btn-sec btn-sm" data-action="provider-test" data-provider="' + nameAttr + '"' +
          (canTest ? '' : ' disabled title="该 Provider 无 OpenAI Base URL"') + '>测试</button>' +
        '<button type="button" class="btn btn-sec btn-sm" data-action="provider-discover" data-provider="' + nameAttr + '">发现</button>' +
        '<button type="button" class="btn btn-danger btn-sm" data-action="provider-delete" data-provider="' + nameAttr + '">删除</button>' +
      '</div>';
    return '<tr>' +
      '<td><div class="cp-cell">' +
        '<span class="cp-name-txt">' + esc(p.name) + '</span>' +
        (p.preset_id ? '<span class="cp-sub">预设 ' + esc(p.preset_id) + '</span>' : '') +
      '</div></td>' +
      '<td>' + stHtml + '</td>' +
      '<td>' + protoHtml + '</td>' +
      '<td class="num">' + countHtml + '</td>' +
      '<td>' + keyHtml + '</td>' +
      '<td>' + opsHtml + '</td>' +
      '</tr>';
  }

  async function cloudLoadProviders() {
    var tbody = $('provTbody');
    if (!tbody) return;
    function fail(msg) {
      tbody.innerHTML = '<tr class="cp-row-loading"><td colspan="6">' +
        '<div class="if-empty">加载失败 · ' + esc(msg) + '</div></td></tr>';
    }
    // 首次加载（无缓存）→ skeleton（spec §5 全 TAB 骨架；重取不闪）
    if (!_providers.length) providerSkeleton();
    try {
      var res = await fetch('/admin/cloud/providers', { headers: UI.adminHeaders() });
      var d = null;
      try { d = await res.json(); } catch (_) {}
      if (!res.ok || !d || d.error) {
        fail((d && d.error) || ('HTTP ' + res.status));
        return;
      }
      _providers = d.providers || [];
      if (!_providers.length) {
        tbody.innerHTML = '<tr class="cp-row-loading"><td colspan="6">' +
          '<div class="if-empty">暂无 Provider — 使用预设或手动配置添加</div></td></tr>';
      } else {
        var rows = '';
        _providers.forEach(function (p) { rows += providerRow(p); });
        tbody.innerHTML = rows;
      }
      cloudRenderModels(d);
    } catch (e) {
      fail(e.message || e);
    }
  }

  /* ── 发现模型紧凑列表：providers.models → #cloudModels ── */
  function cloudRenderModels(data) {
    var models = (data && data.models) || [];
    var count = $('cloudModelCount');
    if (count) count.textContent = UI.fmtNum(models.length) + ' 个模型';
    var list = $('cloudModelsList');
    if (!list) return;
    if (!models.length) {
      list.innerHTML = '<div class="if-empty">暂无模型 — 在 Provider 行点击「发现」或「发现全部」</div>';
      return;
    }
    var sorted = models.slice().sort(function (a, b) {
      return (b.discovered_at || 0) - (a.discovered_at || 0);
    });
    var html = '';
    sorted.forEach(function (m) {
      var chips = '';
      if (m.openai_available) chips += '<span class="cp-chip on">' + icon('check') + 'OpenAI</span>';
      if (m.anthropic_available) chips += '<span class="cp-chip on">' + icon('check') + 'Anthropic</span>';
      html +=
        '<div class="cp-model-row">' +
          '<span class="cp-model-id">' + esc(m.model_id || m.id) + '</span>' +
          chips +
          '<span class="cp-model-prov">' + esc(m.provider) + '</span>' +
        '</div>';
    });
    list.innerHTML = html;
  }

  /* ══ 手动添加：POST /admin/cloud/providers {name, api_key, openai_base, anthropic_base} ══ */
  window.doCloudAdd = async function () {
    var name = ($('cpName') || {}).value ? $('cpName').value.trim() : '';
    var apiKey = ($('cpApiKey') || {}).value ? $('cpApiKey').value.trim() : '';
    var openaiBase = ($('cpOpenaiBase') || {}).value ? $('cpOpenaiBase').value.trim() : '';
    var anthropicBase = ($('cpAnthropicBase') || {}).value ? $('cpAnthropicBase').value.trim() : '';
    if (!name) { UI.toast('请填写 Provider 名称', 'error'); return; }
    if (!openaiBase && !anthropicBase) { UI.toast('至少填写一个 Base URL', 'error'); return; }
    setBtnBusy('manual-add', true);
    try {
      var res = await fetch('/admin/cloud/providers', {
        method: 'POST',
        headers: UI.adminHeaders(),
        body: JSON.stringify({
          name: name,
          api_key: apiKey,
          openai_base: openaiBase,
          anthropic_base: anthropicBase,
        }),
      });
      var d = null;
      try { d = await res.json(); } catch (_) {}
      if (!res.ok || !d || d.error) {
        UI.toast((d && d.error) || ('添加失败 · HTTP ' + res.status), 'error');
        return;
      }
      UI.toast('Provider ' + name + ' 已添加', 'success');
      await window.doCloudDiscover(name);  // 发现单个 + 刷新 Provider 表
      ['cpName', 'cpApiKey', 'cpOpenaiBase', 'cpAnthropicBase'].forEach(function (id) {
        var el = $(id);
        if (el) el.value = '';
      });
    } catch (e) {
      UI.toast('添加失败: ' + (e.message || e), 'error');
    } finally {
      setBtnBusy('manual-add', false);
    }
  };

  /* ══ 测试连接：POST /admin/cloud/test {url, api_key}
   * 不传 baseUrl 时读手动表单 #cpOpenaiBase；行内测试传 provider.openai_base。
   * key 留空时后端不带 Authorization 头（公开端点可测）。 */
  window.doCloudTest = async function (baseUrl, apiKey) {
    var fromForm = baseUrl == null;
    var base = fromForm
      ? (($('cpOpenaiBase') ? $('cpOpenaiBase').value.trim() : ''))
      : String(baseUrl || '');
    var key = apiKey != null
      ? String(apiKey)
      : ($('cpApiKey') ? $('cpApiKey').value.trim() : '');
    if (!base) { UI.toast('请填写 OpenAI Base URL', 'error'); return; }
    UI.toast('正在测试连接...', 'info');
    try {
      var res = await fetch('/admin/cloud/test', {
        method: 'POST',
        headers: UI.adminHeaders(),
        body: JSON.stringify({ url: base.replace(/\/+$/, '') + '/models', api_key: key }),
      });
      var d = null;
      try { d = await res.json(); } catch (_) {}
      if (!res.ok || !d || d.error) {
        UI.toast('连接失败: ' + ((d && d.error) || ('HTTP ' + res.status)), 'error');
        return;
      }
      UI.toast('连接成功 · 发现 ' + (d.model_count || 0) + ' 个模型', 'success');
    } catch (e) {
      UI.toast('测试失败: ' + (e.message || e), 'error');
    }
  };

  /* ══ 模型发现：POST /admin/cloud/discover {} | {provider} ══ */
  window.doCloudDiscover = async function (name) {
    var provider = name && String(name).trim() ? String(name).trim() : null;
    var opts = { method: 'POST', headers: UI.adminHeaders() };
    if (provider) opts.body = JSON.stringify({ provider: provider });
    setBtnBusy('provider-discover', true, provider);
    try {
      var res = await fetch('/admin/cloud/discover', opts);
      var d = null;
      try { d = await res.json(); } catch (_) {}
      if (!res.ok || !d || d.error) {
        UI.toast((d && d.error) || ('发现失败 · HTTP ' + res.status), 'error');
        return;
      }
      if (provider) UI.toast('Provider ' + provider + ': 发现 ' + (d.cloud_models || 0) + ' 个模型', 'success');
      else UI.toast('发现 ' + (d.cloud_models || 0) + ' 个云端模型', 'success');
    } catch (e) {
      UI.toast('发现失败: ' + (e.message || e), 'error');
    } finally {
      setBtnBusy('provider-discover', false, provider);
      await cloudLoadProviders();  // review fix#2：无论发现成败都刷新（provider 可能已添加但发现失败）
    }
  };

  /* ══ 删除：DELETE /admin/cloud/providers {name}（R13：UI.confirm danger） ══ */
  window.doCloudDelete = function (name) {
    if (!name) { UI.toast('缺少 Provider 名称', 'error'); return; }
    UI.confirm({
      title: '删除 Provider',
      body: '确定删除 Provider "' + name + '"？其发现的模型将一并移除。',
      danger: true,
      onOk: async function () {
        setBtnBusy('provider-delete', true, name);
        try {
          var res = await fetch('/admin/cloud/providers', {
            method: 'DELETE',
            headers: UI.adminHeaders(),
            body: JSON.stringify({ name: name }),
          });
          var d = null;
          try { d = await res.json(); } catch (_) {}
          if (!res.ok || !d || d.error) {
            UI.toast((d && d.error) || ('删除失败 · HTTP ' + res.status), 'error');
            return;
          }
          UI.toast('Provider ' + name + ' 已删除', 'success');
          await cloudLoadProviders();
        } catch (e) {
          UI.toast('删除失败: ' + (e.message || e), 'error');
        } finally {
          setBtnBusy('provider-delete', false, name);
        }
      },
    });
  };

  /* ══ 重载配置：POST /admin/cloud/reload ══ */
  window.doCloudReload = async function () {
    setBtnBusy('reload', true);
    try {
      var res = await fetch('/admin/cloud/reload', {
        method: 'POST',
        headers: UI.adminHeaders(),
      });
      var d = null;
      try { d = await res.json(); } catch (_) {}
      if (!res.ok || !d || d.error) {
        UI.toast((d && d.error) || ('重载失败 · HTTP ' + res.status), 'error');
        return;
      }
      UI.toast('已重新加载: ' + (d.providers || 0) + ' 个 provider, ' + (d.cloud_models || 0) + ' 个模型', 'success');
      await cloudLoadProviders();
    } catch (e) {
      UI.toast('重载失败: ' + (e.message || e), 'error');
    } finally {
      setBtnBusy('reload', false);
    }
  };

  /* ── 渲染入口：tab_active 刷新（tabRenderers 契约，forceRefresh 亦复用） ── */
  function renderCloud() {
    cloudLoadPresets();
    cloudLoadProviders();
  }
  window.renderCloud = renderCloud;
  window.tabRenderers['tab-cloud'] = renderCloud;

  store.on('tab_active', function (tab) {
    if (tab === 'tab-cloud') renderCloud();
  });

  /* ── 事件委托（无 inline onclick） ──
   * #tab-cloud 点击：[data-action] + [data-provider]。
   * 预设卡为 div[role=button] → 另挂 keydown，Enter/Space 触发（BUTTON 原生处理）。 */
  function handleAction(el) {
    var act = el.getAttribute('data-action');
    if (!act || el.disabled) return;
    var provider = el.getAttribute('data-provider');
    switch (act) {
      case 'preset-select': {
        var pid = el.getAttribute('data-preset');
        if (!pid) return;
        // 再次点击已选卡 → 取消（spec §4.5）
        if (_selectedPreset && _selectedPreset.id === pid) cloudDeselectPreset();
        else cloudSelectPreset(pid);
        break;
      }
      case 'preset-cancel':
        cloudDeselectPreset();
        break;
      case 'preset-add':
        window.doCloudAddPreset();
        break;
      case 'manual-test':
        window.doCloudTest();
        break;
      case 'manual-add':
        window.doCloudAdd();
        break;
      case 'reload':
        window.doCloudReload();
        break;
      case 'provider-discover':
        window.doCloudDiscover(provider || '');
        break;
      case 'provider-test': {
        if (!provider) return;
        var p = null;
        for (var i = 0; i < _providers.length; i++) {
          if (_providers[i].name === provider) { p = _providers[i]; break; }
        }
        if (!p || !p.openai_base) {
          UI.toast('该 Provider 未配置 OpenAI Base URL', 'error');
          return;
        }
        window.doCloudTest(p.openai_base, '');
        break;
      }
      case 'provider-delete':
        if (provider) window.doCloudDelete(provider);
        break;
    }
  }

  var root = $('tab-cloud');
  if (root) {
    root.addEventListener('click', function (ev) {
      var el = ev.target.closest ? ev.target.closest('[data-action]') : null;
      if (!el || !root.contains(el)) return;
      handleAction(el);
    });
    root.addEventListener('keydown', function (ev) {
      if (ev.key !== 'Enter' && ev.key !== ' ') return;
      var el = ev.target.closest ? ev.target.closest('[data-action]') : null;
      if (!el || !root.contains(el) || el.tagName === 'BUTTON') return;
      ev.preventDefault();
      handleAction(el);
    });
  }
})();
