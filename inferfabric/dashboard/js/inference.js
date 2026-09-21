/* InferFabric Console — Inference tab (v2, Task 6)
 * 模型卡片网格（独占 / 共享 / 空闲 三组）+ 网关控制卡（LRU 缓存开关迁移至此）。
 *
 * 数据源：
 *   - window.store  /api/snapshot 轮询结果
 *       models         — 全量模型清单（name/mode/type/active/quantization/context_window）
 *       sleep_states   — { name: sleep_label }（L2 休眠中的模型）
 *       services_info  — { name: { mode, type, port } }（活跃模型端口等）
 *       services_health— { name: health_str }
 *       local_models   — { cache_enabled, cache_stats: {hits,size,max}|null,
 *                          configured, discovered }（缓存开关 + LRU 生效计数）
 *   - POST /switch | /stop | /sleep | /wake  — 模型生命周期操作（admin header）
 *   - POST /admin/cache/toggle               — LRU 缓存开关（admin header）
 *
 * 设计纪律：
 *   - 事件委托（无 inline onclick）；数字等宽 tabular-nums；零 emoji（SVG 图标）
 *   - 破坏性操作（stop）UI.confirm danger；switch/sleep/wake UI.confirm 写明后果
 *   - switch 依赖 store 既有的 switch_target → #switchOverlay 机制（overlay 自动出现）
 *   - cacheHits 展示 LRU 生效（方案 A）：开 → 命中 N 次 · 在用 size/max（snapshot
 *     cache_stats = ResponseCache.stats() 真计数器）；关 → 状态 + 上限 500
 *     （R10 旧契约「后端不暴露命中数」已作废 —— 计数器由后端真实提供，
 *      且缓存命中不再落 RequestLog，命中数成为唯一可见通道）
 *   - rlMeta 静态标签 —— manager.status() 无 rate-limit 字段，配置见 iff.yaml（R11）
 *
 * 暴露：
 *   - window.tabRenderers['tab-inference'] = renderInference
 *   - window.doModelAction(name, action)   — action ∈ switch/stop/sleep/wake
 */
(function () {
  'use strict';

  var UI = window.UI;
  var store = window.store;
  if (!UI || !store) {
    console.warn('[inference] UI/store not ready — deferring');
    return;
  }

  var $ = function (id) { return document.getElementById(id); };

  /* ── 客户端会话级 uptime（同 overview.js：后端未暴露进程启动时间，
   *    此值为"本次页面会话观察到的运行时长"，刷新后归零）。── */
  var _startedAt = {};

  var ENGINE_LABEL = {
    vllm: 'vLLM', sglang: 'SGLang', ninfer: 'NInfer',
    ollama: 'Ollama', ollama_cpp: 'OllamaCpp', comfyui: 'ComfyUI',
    tts: 'TTS', asr: 'ASR',
  };

  function engineLabel(type) {
    return ENGINE_LABEL[type] || (type ? type.charAt(0).toUpperCase() + type.slice(1) : '—');
  }

  function escHtml(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function escAttr(s) {
    return escHtml(s);
  }

  function isVllm(type) { return type === 'vllm'; }

  /* 缓存上限（response_cache.py:24 默认 maxsize=500）—— 常量，后端不暴露实时值 */
  var CACHE_MAXSIZE = 500;

  /* ── 分组 ──
   * mode: 'exclusive' → 独占 / 'shared' → 共享 / 'none' → 空闲（CPU-only） */
  function groupModels(models) {
    var excl = [], shrd = [], free = [];
    if (!models) return { exclusive: excl, shared: shrd, none: free };
    for (var i = 0; i < models.length; i++) {
      var m = models[i];
      if (!m) continue;
      var mode = m.mode || 'exclusive';
      if (mode === 'shared') shrd.push(m);
      else if (mode === 'none') free.push(m);
      else excl.push(m);
    }
    return { exclusive: excl, shared: shrd, none: free };
  }

  /* ── 单张模型卡 ── */
  function modelCard(m, info, sleepState) {
    var name = m.name || '';
    var type = (info && info.type) || m.type || '';
    var port = info && info.port ? info.port : null;
    var sleeping = !!sleepState;
    var active = !!m.active && !sleeping;

    // 客户端 uptime
    var now = Date.now();
    if (!sleeping && active) {
      if (!_startedAt[name]) _startedAt[name] = now;
    } else {
      delete _startedAt[name];
    }
    var upSecs = _startedAt[name] ? Math.max(0, Math.floor((now - _startedAt[name]) / 1000)) : 0;

    // 状态点 + 文本
    var dotCls, stateTxt;
    if (active) { dotCls = 'ok'; stateTxt = 'running'; }
    else if (sleeping) { dotCls = 'warn'; stateTxt = 'sleeping ' + String(sleepState).toUpperCase(); }
    else { dotCls = ''; stateTxt = 'stopped'; }

    // 操作按钮（按状态可用性显隐）
    var actions = '';
    var nm = escAttr(name);
    if (active) {
      actions += '<button type="button" class="btn btn-danger btn-sm" data-action="stop" data-model="' + nm + '">' +
        UI.icon('stop', 14) + 'Stop</button>';
      if (isVllm(type)) {
        actions += '<button type="button" class="btn btn-sec btn-sm" data-action="sleep" data-model="' + nm + '">' +
          UI.icon('sleep', 14) + 'Sleep</button>';
      }
    } else if (sleeping) {
      actions += '<button type="button" class="btn btn-sec btn-sm" data-action="wake" data-model="' + nm + '">' +
        UI.icon('wake', 14) + 'Wake</button>';
      actions += '<button type="button" class="btn btn-danger btn-sm" data-action="stop" data-model="' + nm + '">' +
        UI.icon('stop', 14) + 'Stop</button>';
    } else {
      actions += '<button type="button" class="btn btn-pri btn-sm" data-action="switch" data-model="' + nm + '">' +
        UI.icon('play', 14) + '启动</button>';
    }

    var quant = m.quantization ? escHtml(m.quantization) : '';
    var portHtml = (port != null && active)
      ? '<div class="inf-stat"><dt>端口</dt><dd class="mono">:' + escHtml(port) + '</dd></div>'
      : '';

    return '' +
      '<div class="if-card inf-card">' +
        '<div class="if-card-hdr">' +
          '<span class="status-dot ' + dotCls + '" title="' + escHtml(stateTxt) + '"></span>' +
          '<span class="if-card-title">' + escHtml(name) + '</span>' +
          '<span class="badge">' + escHtml(engineLabel(type)) + '</span>' +
        '</div>' +
        '<div class="if-card-body">' +
          '<dl class="inf-stats">' +
            portHtml +
            '<div class="inf-stat"><dt>uptime</dt><dd class="mono">' + (active ? UI.fmtDur(upSecs) : '—') + '</dd></div>' +
            (quant ? '<div class="inf-stat"><dt>精度</dt><dd class="mono">' + quant + '</dd></div>' : '') +
          '</dl>' +
        '</div>' +
        '<div class="if-card-actions">' + actions + '</div>' +
      '</div>';
  }

  /* ── 渲染一个分组 ── */
  function renderGroup(groupEl, countEl, listEl, models, sleepStates, servicesInfo) {
    if (!groupEl || !listEl || !countEl) return;

    if (models === undefined) {
      // 快照未到达 → skeleton
      countEl.textContent = '—';
      listEl.innerHTML = '<div class="skeleton"><div class="skeleton-row"></div><div class="skeleton-row"></div></div>';
      groupEl.style.display = '';
      return;
    }

    countEl.textContent = String(models.length);

    if (!models.length) {
      // 空态：隐藏整组（DOM 保留供测试 / 后续填充）
      groupEl.style.display = 'none';
      listEl.innerHTML = '';
      return;
    }

    groupEl.style.display = '';
    var html = '';
    for (var i = 0; i < models.length; i++) {
      var m = models[i];
      var info = (servicesInfo && servicesInfo[m.name]) || null;
      var sleep = (sleepStates && sleepStates[m.name]) || null;
      html += modelCard(m, info, sleep);
    }
    listEl.innerHTML = html;
  }

  /* ── 网关控制卡：缓存状态 + 速率限制 ── */
  function renderGateway() {
    var lm = store.get('local_models') || {};
    var cacheOn = !!lm.cache_enabled;

    // #cacheHits — LRU 生效可见性（方案 A）：真计数器来自 snapshot
    // local_models.cache_stats（ResponseCache.stats()：命中次数 / 在用条目
    // / 上限）。缓存关闭或字段缺失时回退「状态 + 上限」信息性标签。
    var stats = (lm.cache_stats && typeof lm.cache_stats === 'object') ? lm.cache_stats : null;
    var cacheLabel = $('cacheHits');
    if (cacheLabel) {
      if (cacheOn && stats) {
        cacheLabel.textContent = 'LRU 缓存 · 开 · 命中 ' + UI.fmtNum(stats.hits || 0) +
          ' 次 · 在用 ' + (stats.size || 0) + '/' + (stats.max || CACHE_MAXSIZE) + ' 条';
      } else {
        cacheLabel.textContent = 'LRU 缓存 · ' + (cacheOn ? '开' : '关') + ' · 上限 ' + CACHE_MAXSIZE + ' 条';
      }
    }

    // #cacheToggle — 按钮文本随状态翻转
    var tog = $('cacheToggle');
    if (tog) {
      tog.textContent = cacheOn ? '关闭' : '开启';
      tog.setAttribute('aria-pressed', String(cacheOn));
      tog.classList.toggle('btn-pri', !cacheOn);
      tog.classList.toggle('btn-sec', cacheOn);
    }

    // #rlMeta — R11：静态标签（manager.status 无 rate-limit 字段）
    var rl = $('rlMeta');
    if (rl) {
      rl.textContent = '配置见 iff.yaml';
    }
  }

  /* ── 清理已不活跃模型的 uptime 记录 ── */
  function pruneUptime(models) {
    if (!models) return;
    var live = {};
    for (var i = 0; i < models.length; i++) {
      if (models[i]) live[models[i].name] = true;
    }
    for (var k in _startedAt) {
      if (!live[k]) delete _startedAt[k];
    }
  }

  /* ── 渲染入口 ── */
  function renderInference() {
    var models = store.get('models');
    var sleepStates = store.get('sleep_states') || {};
    var servicesInfo = store.get('services_info') || {};

    pruneUptime(models);

    var g = groupModels(models);

    renderGroup($('infExclGroup'), $('infExclCount'), $('infExclList'),
                g.exclusive, sleepStates, servicesInfo);
    renderGroup($('infShrdGroup'), $('infShrdCount'), $('infShrdList'),
                g.shared, sleepStates, servicesInfo);
    renderGroup($('infFreeGroup'), $('infFreeCount'), $('infFreeList'),
                g.none, sleepStates, servicesInfo);

    renderGateway();
  }

  window.renderInference = renderInference;
  window.tabRenderers['tab-inference'] = renderInference;

  /* ── 订阅：snapshot 到达时刷新（仅推理页可见时） ── */
  function isInferenceActive() {
    var el = $('tab-inference');
    return !!(el && el.classList.contains('active'));
  }

  store.on('sync_meta', function () { if (isInferenceActive()) renderInference(); });
  store.on('tab_active', function (tab) {
    if (tab === 'tab-inference') renderInference();
  });

  /* ── 模型操作：switch / stop / sleep / wake ── */
  window.doModelAction = async function (name, action) {
    if (!name) { UI.toast('缺少模型名', 'error'); return; }

    var OPS = {
      switch: {
        url: '/switch',
        confirm: {
          title: '切换模型',
          body: '将切换到 ' + name + '。这是一项重型操作，可能需要数十秒加载权重。确定？',
        },
        ok: name + ' 切换中',
      },
      stop: {
        url: '/stop',
        confirm: {
          title: '停止模型',
          body: '将停止 ' + name + '，进程立即终止。确定？',
          danger: true,
        },
        ok: name + ' 已停止',
      },
      sleep: {
        url: '/sleep',
        confirm: {
          title: '休眠模型',
          body: '将休眠 ' + name + '（L2：丢弃 vLLM 权重释放显存，唤醒需 3-6s 重新加载）。确定？',
        },
        ok: name + ' 休眠中',
      },
      wake: {
        url: '/wake',
        confirm: {
          title: '唤醒模型',
          body: '将唤醒 ' + name + '（需 3-6s 重新加载权重）。确定？',
        },
        ok: name + ' 唤醒中',
      },
    };

    var op = OPS[action];
    if (!op) { UI.toast('未知操作: ' + action, 'error'); return; }

    var run = async function () {
      try {
        var res = await fetch(op.url, {
          method: 'POST',
          headers: UI.adminHeaders(),
          body: JSON.stringify({ model: name }),
        });
        if (!res.ok) {
          var msg = 'HTTP ' + res.status;
          try {
            var j = await res.json();
            if (j) msg = j.error || j.message || msg;
          } catch (_) {}
          throw new Error(msg);
        }
        // switch 依赖后端设置 switch_target → store 既有 overlay 机制自动出现
        UI.toast(op.ok, 'ok');
        store.forceRefresh();
      } catch (e) {
        UI.toast('操作失败: ' + (e && e.message ? e.message : e), 'error');
      }
    };

    if (op.confirm) {
      UI.confirm({
        title: op.confirm.title,
        body: op.confirm.body,
        danger: !!op.confirm.danger,
        onOk: run,
      });
    } else {
      run();
    }
  };

  /* ── 缓存开关切换 ── */
  async function toggleCache() {
    try {
      var res = await fetch('/admin/cache/toggle', {
        method: 'POST',
        headers: UI.adminHeaders(),
      });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      var j = await res.json();
      UI.toast(j && j.cache_enabled ? '缓存已开启' : '缓存已关闭', 'ok');
      store.forceRefresh();
    } catch (e) {
      UI.toast('缓存切换失败: ' + (e && e.message ? e.message : e), 'error');
    }
  }

  /* ── 事件委托（无 inline onclick） ── */
  var root = $('tab-inference');
  if (root) {
    root.addEventListener('click', function (ev) {
      var btn = ev.target.closest('[data-action]');
      if (!btn || !root.contains(btn) || btn.disabled) return;
      var act = btn.getAttribute('data-action');

      if (act === 'cache-toggle') {
        toggleCache();
        return;
      }
      if (act === 'goto-deploy') {
        store.switchTab('tab-deploy');
        return;
      }
      // 模型操作：switch / stop / sleep / wake
      var model = btn.getAttribute('data-model');
      if (model && window.doModelAction) {
        window.doModelAction(model, act);
      }
    });
  }
})();
