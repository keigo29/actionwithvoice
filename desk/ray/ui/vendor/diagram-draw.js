// Workstation（462d164 2026-11-26）の src/renderer/diagram-draw.js の写し。直すときは Workstation 側を直して、python -m ray --sync-workstation で写し直す
// 図解の「描く」部分（画面の操作とは別。テストからも使えるように、DOM がなくても動く）
// ・図形（nodes）と線（edges）から SVG の文字列を作る
// ・線のつなぎ方（どこから出て、どう曲がるか）・矢印の形
// ・ER図のテーブル・クラス図のクラスの中身（文字）を読む
(function (root) {
  'use strict';
  const FONT = "'Zen Kaku Gothic New', 'Hiragino Sans', 'Hiragino Kaku Gothic ProN', 'Noto Sans JP', 'Yu Gothic UI', sans-serif";
  const MONO = "ui-monospace, 'SF Mono', Menlo, Consolas, 'Noto Sans Mono CJK JP', monospace";
  const INK = '#1f2328', SUB = '#6a737d';

  // 色：塗り（f）と線（s）の組。どのテーマでも同じ色（書き出しても同じに見えるように）
  const COLORS = {
    white: { f: '#ffffff', s: '#1f2328', h: '#f1f3f5', name: '白' },
    gray: { f: '#f1f3f5', s: '#868e96', h: '#e2e6ea', name: 'グレー' },
    blue: { f: '#e8f0ff', s: '#3b6fe0', h: '#d3e2ff', name: '青' },
    green: { f: '#e4f6e9', s: '#2f9e57', h: '#c9ecd4', name: '緑' },
    yellow: { f: '#fff4cc', s: '#c99a06', h: '#ffe999', name: '黄' },
    orange: { f: '#ffecdc', s: '#e8590c', h: '#ffd8b8', name: 'オレンジ' },
    red: { f: '#ffe5e5', s: '#e03131', h: '#ffcccc', name: '赤' },
    purple: { f: '#efe8ff', s: '#7048e8', h: '#ddd0ff', name: '紫' },
  };
  const colorOf = n => COLORS[n && n.c] || COLORS.white;

  // ---------- 図形の種類 ----------
  // w・h：はじめの大きさ。auto：高さを中身に合わせる。edge：この図形どうしをつなぐときの線のはじめの形
  const SHAPES = {
    rect: { name: '四角', w: 140, h: 70 },
    round: { name: '角丸', w: 140, h: 70 },
    ellipse: { name: '丸', w: 140, h: 80, outline: 'ellipse' },
    diamond: { name: 'ひし形', w: 150, h: 90, outline: 'diamond' },
    hexagon: { name: '六角形', w: 150, h: 70 },
    parallelogram: { name: '平行四辺形', w: 150, h: 70 },
    terminal: { name: '開始・終了', w: 140, h: 54 },
    document: { name: '書類', w: 140, h: 80 },
    predefined: { name: '定義済み処理', w: 150, h: 70 },
    cylinder: { name: 'データベース', w: 120, h: 90 },
    circle: { name: '結合子', w: 44, h: 44, outline: 'ellipse' },
    text: { name: 'テキスト', w: 160, h: 40, c: 'white' },
    note: { name: '付箋', w: 170, h: 110, c: 'yellow' },
    frame: { name: '枠（グループ）', w: 380, h: 260, c: 'gray', back: true },
    entity: { name: 'テーブル', w: 220, h: 120, auto: true, edge: { s: 'only', e: 'zmany' } },
    class: { name: 'クラス', w: 230, h: 120, auto: true, edge: { e: 'triangle' } },
    lifeline: { name: '参加者', w: 130, h: 420, line: true, edge: { r: 'straight', e: 'arrow' } },
    actorline: { name: 'アクター', w: 110, h: 420, line: true, edge: { r: 'straight', e: 'arrow' } },
    activation: { name: '実行中', w: 16, h: 80 },
    icon: { name: 'アイコン', w: 120, h: 96 },
    image: { name: '画像', w: 240, h: 160, ratio: true },
  };
  const isLine = n => !!(n && SHAPES[n.t] && SHAPES[n.t].line);

  // ---------- 文字の幅（画面では canvas で測る。テストでは目安） ----------
  let measureCtx = null;
  function textW(s, fs, bold, mono) {
    if (!measureCtx && typeof document !== 'undefined') measureCtx = document.createElement('canvas').getContext('2d');
    if (measureCtx) {
      measureCtx.font = `${bold ? 700 : 500} ${fs}px ${mono ? MONO : FONT}`;
      return measureCtx.measureText(s).width;
    }
    let w = 0;
    for (const ch of s) w += /[　-鿿＀-￯]/.test(ch) ? fs : fs * (mono ? 0.62 : 0.56);
    return w;
  }
  // 幅に収まるように折り返す（英語は単語の切れ目で、日本語は文字で）
  function wrap(text, maxW, fs, bold) {
    const out = [];
    for (const para of String(text || '').split('\n')) {
      if (!para) { out.push(''); continue; }
      let line = '';
      for (const ch of para) {
        if (textW(line + ch, fs, bold) <= maxW || !line) { line += ch; continue; }
        const sp = line.lastIndexOf(' ');
        if (/[A-Za-z0-9]/.test(ch) && sp > 0) { out.push(line.slice(0, sp)); line = line.slice(sp + 1) + ch; }
        else { out.push(line); line = ch; }
      }
      out.push(line);
    }
    return out;
  }
  const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const r1 = v => Math.round(v * 10) / 10;

  // ---------- ER図のテーブル・クラス図のクラス（中身は文字で書く） ----------
  // テーブル：1行目が名前、2行目から列（例「PK id int」「FK user_id int」「name varchar(50)」）
  function parseEntity(text) {
    const lines = String(text || '').split('\n');
    const name = (lines.shift() || '').trim();
    const rows = lines.map(l => l.trim()).filter(Boolean).map(l => {
      const tk = l.split(/\s+/);
      let key = '';
      if (/^(PK|FK|UK|PK,FK|PK\/FK)$/i.test(tk[0])) key = tk.shift().toUpperCase();
      return { key, name: tk.shift() || '', type: tk.join(' ') };
    });
    return { name, rows };
  }
  // クラス：「--」の行で区切る（名前／属性／メソッド）。名前の段の «interface» や <<abstract>> は小さく出す
  function parseClass(text) {
    const secs = [[]];
    for (const l of String(text || '').split('\n')) {
      if (/^\s*-{2,}\s*$/.test(l)) secs.push([]);
      else secs[secs.length - 1].push(l);
    }
    const head = secs[0].map(l => l.trim()).filter(Boolean);
    const stereo = head.filter(l => /^(«.*»|<<.*>>)$/.test(l));
    const name = head.find(l => !stereo.includes(l)) || '';
    return { stereo, name, secs: secs.slice(1).map(s => s.map(l => l.replace(/\s+$/, '')).filter(l => l.trim())) };
  }
  const ROW = 24, HEAD = 34, CLINE = 20;
  // 中身に合わせた高さ（テーブル・クラス）
  function autoH(n) {
    if (n.t === 'entity') return HEAD + Math.max(1, parseEntity(n.text).rows.length) * ROW + 8;
    if (n.t === 'class') {
      const c = parseClass(n.text);
      const head = 14 + (c.stereo.length * 16) + 22;
      return head + (c.secs.length ? c.secs.reduce((a, s) => a + Math.max(1, s.length) * CLINE + 10, 0) : 0);
    }
    return n.h;
  }

  // ---------- 図形を描く ----------
  // 本体の形（塗りと線）。fill・stroke は呼ぶ側で決める
  function bodyPath(n, fill, stroke, sw, dash) {
    const { x, y, w, h } = n;
    const a = `fill="${fill}" stroke="${stroke}" stroke-width="${sw}"${dash ? ' stroke-dasharray="6 4"' : ''} stroke-linejoin="round"`;
    switch (n.t) {
      case 'round': return `<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="${Math.min(14, h / 2)}" ${a}/>`;
      case 'terminal': return `<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="${h / 2}" ${a}/>`;
      case 'ellipse': case 'circle': return `<ellipse cx="${x + w / 2}" cy="${y + h / 2}" rx="${w / 2}" ry="${h / 2}" ${a}/>`;
      case 'diamond': return `<path d="M${x + w / 2} ${y}L${x + w} ${y + h / 2}L${x + w / 2} ${y + h}L${x} ${y + h / 2}Z" ${a}/>`;
      case 'hexagon': { const k = Math.min(22, w / 4); return `<path d="M${x + k} ${y}H${x + w - k}L${x + w} ${y + h / 2}L${x + w - k} ${y + h}H${x + k}L${x} ${y + h / 2}Z" ${a}/>`; }
      case 'parallelogram': { const k = Math.min(20, w / 5); return `<path d="M${x + k} ${y}H${x + w}L${x + w - k} ${y + h}H${x}Z" ${a}/>`; }
      case 'document': { const k = Math.min(12, h / 6); return `<path d="M${x} ${y}H${x + w}V${y + h - k}C${x + w * 0.75} ${y + h - k * 3} ${x + w * 0.25} ${y + h + k} ${x} ${y + h - k}Z" ${a}/>`; }
      case 'predefined': return `<rect x="${x}" y="${y}" width="${w}" height="${h}" ${a}/><path d="M${x + 12} ${y}V${y + h}M${x + w - 12} ${y}V${y + h}" fill="none" stroke="${stroke}" stroke-width="${sw}"/>`;
      case 'cylinder': { const e = Math.min(14, h / 5); return `<path d="M${x} ${y + e}V${y + h - e}A${w / 2} ${e} 0 0 0 ${x + w} ${y + h - e}V${y + e}" ${a}/><ellipse cx="${x + w / 2}" cy="${y + e}" rx="${w / 2}" ry="${e}" ${a}/>`; }
      case 'note': { const k = 16; return `<path d="M${x} ${y}H${x + w}V${y + h - k}L${x + w - k} ${y + h}H${x}Z" ${a}/><path d="M${x + w} ${y + h - k}H${x + w - k}V${y + h}" fill="${stroke}" fill-opacity=".12" stroke="${stroke}" stroke-width="${sw}" stroke-linejoin="round"/>`; }
      default: return `<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="2" ${a}/>`;
    }
  }
  // 文字を置く（折り返して、縦も横もまん中。align='left' なら左から）
  function textBlock(lines, cx, cy, fs, { color = INK, bold = false, anchor = 'middle', lh = 1.35 } = {}) {
    if (!lines.length) return '';
    const step = fs * lh, top = cy - (lines.length - 1) * step / 2;
    return `<text font-family="${esc(FONT)}" font-size="${fs}" font-weight="${bold ? 700 : 500}" fill="${color}" text-anchor="${anchor}" dominant-baseline="central">` +
      lines.map((l, i) => `<tspan x="${r1(cx)}" y="${r1(top + i * step)}">${esc(l) || ' '}</tspan>`).join('') + '</text>';
  }
  // 文字を置ける場所（形の内側）
  function textBox(n) {
    const { x, y, w, h } = n;
    switch (n.t) {
      case 'diamond': return { x: x + w * 0.2, y: y + h * 0.2, w: w * 0.6, h: h * 0.6 };
      case 'ellipse': case 'circle': return { x: x + w * 0.14, y: y + h * 0.12, w: w * 0.72, h: h * 0.76 };
      case 'hexagon': case 'parallelogram': return { x: x + 22, y: y + 6, w: w - 44, h: h - 12 };
      case 'predefined': return { x: x + 18, y, w: w - 36, h };
      case 'cylinder': { const e = Math.min(14, h / 5); return { x: x + 8, y: y + e * 2, w: w - 16, h: h - e * 3 }; }
      case 'document': return { x: x + 8, y, w: w - 16, h: h - 10 };
      case 'note': return { x: x + 12, y: y + 10, w: w - 24, h: h - 20 };
      case 'frame': return { x: x + 12, y: y + 8, w: w - 24, h: 22 };
      case 'lifeline': return { x, y, w, h: 40 };
      case 'actorline': return { x: x - 20, y: y + 48, w: w + 40, h: 20 };
      case 'icon': return { x: x + 4, y: y + 58, w: w - 8, h: h - 60 };
      default: return { x: x + 8, y: y + 4, w: w - 16, h: h - 8 };
    }
  }

  // アイコン（Lucide の線の絵）を図の中に置く
  function iconSVG(name, cx, cy, size, color) {
    const ic = (typeof window !== 'undefined' && window.ICONS && window.ICONS[name]) || (iconSet && iconSet[name]) || '';
    const s = size / 24;
    return `<g transform="translate(${r1(cx - size / 2)} ${r1(cy - size / 2)}) scale(${r1(s * 100) / 100})" fill="none" stroke="${color}" stroke-width="${r1(1.8 / Math.max(s, .5) * 10) / 10}" stroke-linecap="round" stroke-linejoin="round">${ic}</g>`;
  }

  // 図形1つ分の SVG（g 要素）。hit：画面で押せるようにする（書き出しでは付けない）
  // assets：画像の中身（図のデータの assets に、画像ごとに1つ）。thumb：一覧の小さな絵では画像のかわりに灰色の四角
  function nodeSVG(n, { hit = true, hideText = false, assets = null, thumb = false } = {}) {
    const col = colorOf(n), fs = n.fs || 14, sw = n.sw || 1.5;
    const fill = n.fill === false ? 'none' : col.f, stroke = col.s;
    const attr = hit ? ` data-node="${esc(n.id)}"` : '';
    let s = '';
    if (n.t === 'image') {
      const src = assets && assets[n.src];
      if (src && !thumb) s += `<image href="${esc(src)}" x="${n.x}" y="${n.y}" width="${n.w}" height="${n.h}" preserveAspectRatio="none"/>`;
      else s += `<rect x="${n.x}" y="${n.y}" width="${n.w}" height="${n.h}" rx="6" fill="#e9ecef"/>` + iconSVG('image', n.x + n.w / 2, n.y + n.h / 2, Math.min(40, n.w / 3, n.h / 3), '#adb5bd');
      if (n.fill !== false) s += `<rect x="${n.x}" y="${n.y}" width="${n.w}" height="${n.h}" fill="none" stroke="${n.c && n.c !== 'white' ? stroke : '#dee2e6'}" stroke-width="${n.c && n.c !== 'white' ? 2 : 1}"/>`;
      if (hit) s += `<rect x="${n.x}" y="${n.y}" width="${n.w}" height="${n.h}" fill="transparent"/>`;
      return `<g${attr}>${s}</g>`;
    }
    const txt = (lines, box, o = {}) => (hideText ? '' : textBlock(lines, o.left ? box.x + 2 : box.x + box.w / 2, box.y + box.h / 2, o.fs || fs, { ...o, anchor: o.left ? 'start' : 'middle' }));
    if (n.t === 'entity') {
      const e = parseEntity(n.text), hasKey = e.rows.some(r => r.key);
      const H = n.h;
      s += `<rect x="${n.x}" y="${n.y}" width="${n.w}" height="${H}" rx="8" fill="#fff" stroke="${stroke}" stroke-width="${sw}"/>`;
      s += `<path d="M${n.x + 8} ${n.y}H${n.x + n.w - 8}A8 8 0 0 1 ${n.x + n.w} ${n.y + 8}V${n.y + HEAD}H${n.x}V${n.y + 8}A8 8 0 0 1 ${n.x + 8} ${n.y}Z" fill="${col.h}" stroke="${stroke}" stroke-width="${sw}"/>`;
      if (!hideText) {
        s += textBlock([e.name || 'テーブル'], n.x + n.w / 2, n.y + HEAD / 2, fs, { bold: true });
        e.rows.forEach((r, i) => {
          const cy = n.y + HEAD + 4 + i * ROW + ROW / 2;
          let x = n.x + 10;
          if (hasKey) { if (r.key) s += textBlock([r.key], x, cy, 10.5, { bold: true, anchor: 'start', color: r.key.startsWith('PK') ? '#c99a06' : '#3b6fe0' }); x += 34; }
          s += textBlock([r.name], x, cy, 13, { anchor: 'start', bold: r.key.startsWith('PK') });
          if (r.type) s += `<text font-family="${esc(MONO)}" font-size="11.5" fill="${SUB}" text-anchor="end" dominant-baseline="central" x="${n.x + n.w - 10}" y="${cy}">${esc(r.type)}</text>`;
        });
      }
    } else if (n.t === 'class') {
      const c = parseClass(n.text);
      const headH = 14 + c.stereo.length * 16 + 22;
      s += `<rect x="${n.x}" y="${n.y}" width="${n.w}" height="${n.h}" rx="4" fill="#fff" stroke="${stroke}" stroke-width="${sw}"/>`;
      s += `<rect x="${n.x}" y="${n.y}" width="${n.w}" height="${headH}" rx="4" fill="${col.h}" stroke="${stroke}" stroke-width="${sw}"/>`;
      if (!hideText) {
        let y = n.y + 7;
        for (const st of c.stereo) { s += textBlock([st.replace(/^<<(.*)>>$/, '«$1»')], n.x + n.w / 2, y + 8, 11, { color: SUB }); y += 16; }
        s += textBlock([c.name || 'クラス'], n.x + n.w / 2, y + 11, fs, { bold: true });
      }
      let y = n.y + headH;
      c.secs.forEach((sec, i) => {
        if (i > 0) s += `<path d="M${n.x} ${y}H${n.x + n.w}" stroke="${stroke}" stroke-width="${sw}"/>`;
        if (!hideText) sec.forEach((l, k) => { s += `<text font-family="${esc(MONO)}" font-size="12" fill="${INK}" dominant-baseline="central" x="${n.x + 10}" y="${r1(y + 5 + k * CLINE + CLINE / 2)}">${esc(l.trim())}</text>`; });
        y += Math.max(1, sec.length) * CLINE + 10;
      });
    } else if (n.t === 'lifeline' || n.t === 'actorline') {
      const cx = n.x + n.w / 2;
      if (n.t === 'lifeline') {
        s += `<path d="M${cx} ${n.y + 40}V${n.y + n.h}" stroke="${SUB}" stroke-width="1.3" stroke-dasharray="6 5"/>`;
        s += `<rect x="${n.x}" y="${n.y}" width="${n.w}" height="40" rx="8" fill="${fill === 'none' ? '#fff' : fill}" stroke="${stroke}" stroke-width="${sw}"/>`;
        s += txt(wrap(n.text, n.w - 14, fs, true).slice(0, 2), textBox(n), { bold: true });
      } else {
        s += `<path d="M${cx} ${n.y + 72}V${n.y + n.h}" stroke="${SUB}" stroke-width="1.3" stroke-dasharray="6 5"/>`;
        s += `<g fill="none" stroke="${stroke}" stroke-width="1.8" stroke-linecap="round"><circle cx="${cx}" cy="${n.y + 9}" r="8" fill="#fff"/><path d="M${cx} ${n.y + 17}V${n.y + 34}M${cx - 13} ${n.y + 23}H${cx + 13}M${cx} ${n.y + 34}L${cx - 11} ${n.y + 46}M${cx} ${n.y + 34}L${cx + 11} ${n.y + 46}"/></g>`;
        s += txt([n.text || ''], textBox(n), { bold: true });
      }
      if (hit) s += `<path d="M${cx} ${n.y}V${n.y + n.h}" stroke="transparent" stroke-width="14" data-line="1"/>`;
    } else if (n.t === 'icon') {
      s += `<rect x="${n.x}" y="${n.y}" width="${n.w}" height="${n.h}" rx="14" fill="${fill}" stroke="${n.fill === false ? 'none' : stroke}" stroke-width="${sw}"${n.fill === false && hit ? ' stroke-opacity="0"' : ''}/>`;
      s += `<circle cx="${n.x + n.w / 2}" cy="${n.y + 32}" r="22" fill="${col.h}"/>`;
      s += iconSVG(n.ic || 'box', n.x + n.w / 2, n.y + 32, 26, stroke);
      s += txt(wrap(n.text, n.w - 8, 12.5, true).slice(0, 2), textBox(n), { fs: 12.5, bold: true });
    } else if (n.t === 'frame') {
      s += `<rect x="${n.x}" y="${n.y}" width="${n.w}" height="${n.h}" rx="14" fill="${n.fill === false ? 'none' : col.f}" fill-opacity="${n.fill === false ? 0 : .45}" stroke="${stroke}" stroke-width="${sw}" stroke-dasharray="7 5"/>`;
      if (hit) s += `<rect x="${n.x}" y="${n.y}" width="${n.w}" height="${n.h}" rx="14" fill="none" stroke="transparent" stroke-width="12"/><rect x="${n.x}" y="${n.y}" width="${Math.min(n.w, 40 + textW(n.text || '', 13, true))}" height="34" fill="transparent"/>`;
      const b = textBox(n);
      if (!hideText && n.text) s += textBlock([n.text], b.x, b.y + b.h / 2, 13, { bold: true, anchor: 'start', color: stroke === INK ? INK : stroke });
    } else if (n.t === 'text') {
      if (hit) s += `<rect x="${n.x}" y="${n.y}" width="${n.w}" height="${n.h}" fill="transparent"/>`;
      if (n.fill === true) s += bodyPath({ ...n, t: 'round' }, col.f, col.s, sw, n.dash);
      const b = textBox(n);
      s += txt(wrap(n.text, b.w, fs, n.bold), b, { bold: n.bold, left: n.align === 'left' });
    } else if (n.t === 'activation') {
      s += `<rect x="${n.x}" y="${n.y}" width="${n.w}" height="${n.h}" fill="${fill === 'none' ? '#fff' : fill}" stroke="${stroke}" stroke-width="${sw}"/>`;
    } else {
      s += bodyPath(n, fill, stroke, sw, n.dash);
      if (n.fill === false && hit) s += `<rect x="${n.x}" y="${n.y}" width="${n.w}" height="${n.h}" fill="transparent"/>`;
      const b = textBox(n);
      s += txt(wrap(n.text, b.w, fs, n.bold), b, { bold: n.bold, left: n.align === 'left' });
    }
    return `<g${attr}>${s}</g>`;
  }

  // ---------- 線 ----------
  const center = n => ({ x: n.x + n.w / 2, y: n.y + n.h / 2 });
  // 形のふち：中心から (dx, dy) の向きに進んで、ふちに当たる点
  function boundary(n, tx, ty) {
    const c = center(n), dx = tx - c.x, dy = ty - c.y;
    if (!dx && !dy) return { x: c.x, y: n.y };
    const hw = n.w / 2, hh = n.h / 2, o = (SHAPES[n.t] || {}).outline;
    let k;
    if (o === 'ellipse') k = 1 / Math.sqrt((dx * dx) / (hw * hw) + (dy * dy) / (hh * hh));
    else if (o === 'diamond') k = 1 / (Math.abs(dx) / hw + Math.abs(dy) / hh);
    else k = Math.min(hw / Math.abs(dx || 1e-9), hh / Math.abs(dy || 1e-9));
    return { x: c.x + dx * k, y: c.y + dy * k };
  }
  const sidePoint = (n, side) => ({ l: { x: n.x, y: n.y + n.h / 2 }, r: { x: n.x + n.w, y: n.y + n.h / 2 }, t: { x: n.x + n.w / 2, y: n.y }, b: { x: n.x + n.w / 2, y: n.y + n.h } }[side]);
  // 線のはしの場所と、出ていく向き（l/r/t/b。決まらなければ null）
  function endInfo(end, other, route, map) {
    const n = end && end.id != null ? map.get(end.id) : null;
    if (!n) return { x: end ? end.x || 0 : 0, y: end ? end.y || 0 : 0, dir: null };
    if (isLine(n)) {
      const cx = n.x + n.w / 2, top = n.t === 'actorline' ? 76 : 44;
      const y = n.y + Math.max(top / n.h, Math.min(1, end.fy ?? 0.3)) * n.h;
      const ox = other ? other.x : cx + 1;
      if (n.t === 'activation') return { x: ox >= cx ? n.x + n.w : n.x, y, dir: ox >= cx ? 'r' : 'l' };
      return { x: cx, y, dir: ox >= cx ? 'r' : 'l' };
    }
    if (n.t === 'activation') {
      const y = n.y + Math.max(0, Math.min(1, end.fy ?? 0.1)) * n.h, ox = other ? other.x : n.x + n.w + 1;
      return { x: ox >= n.x + n.w / 2 ? n.x + n.w : n.x, y, dir: ox >= n.x + n.w / 2 ? 'r' : 'l' };
    }
    if (end.side) return { ...sidePoint(n, end.side), dir: end.side };
    const c = center(n), o = other || { x: c.x + 1, y: c.y };
    if (route === 'straight') { const p = boundary(n, o.x, o.y); return { ...p, dir: null }; }
    const dx = (o.x - c.x) / Math.max(n.w, 1), dy = (o.y - c.y) / Math.max(n.h, 1);
    const side = Math.abs(dx) >= Math.abs(dy) ? (dx >= 0 ? 'r' : 'l') : (dy >= 0 ? 'b' : 't');
    return { ...sidePoint(n, side), dir: side };
  }
  // 線の通り道（点の並び）と、曲線のときは制御点
  // used：ほかの線がもう使っている辺（同じ辺に線が重ならないように、なるべく避ける）
  function edgeGeom(e, map, used, obs) {
    const na = e.a && e.a.id != null ? map.get(e.a.id) : null, nb = e.b && e.b.id != null ? map.get(e.b.id) : null;
    const route = e.r || 'elbow';
    // 自分へ戻る線（シーケンス図の自分へのメッセージなど）
    if (na && na === nb) {
      const p = endInfo(e.a, null, route, map);
      const q = isLine(na) || na.t === 'activation' ? endInfo(e.b, null, route, map) : null;
      const x0 = isLine(na) ? p.x : na.x + na.w, y0 = isLine(na) ? p.y : na.y + na.h * 0.3;
      const y1 = q && q.y > y0 + 8 ? q.y : y0 + (isLine(na) ? 28 : na.h * 0.4);
      const ox = x0 + 44;
      return { pts: [{ x: x0, y: y0 }, { x: ox, y: y0 }, { x: ox, y: y1 }, { x: x0, y: y1 }], curve: null };
    }
    const ca = na ? (isLine(na) ? null : center(na)) : e.a, cb = nb ? (isLine(nb) ? null : center(nb)) : e.b;
    let pa = endInfo(e.a, cb || (nb ? { x: nb.x + nb.w / 2, y: 0 } : null), route, map);
    let pb = endInfo(e.b, ca || (na ? { x: na.x + na.w / 2, y: 0 } : null), route, map);
    // 参加者の線どうしは、まっすぐ横に（右と左を決め直す）
    if (na && (isLine(na) || na.t === 'activation')) pa = endInfo(e.a, pb, route, map);
    if (nb && (isLine(nb) || nb.t === 'activation')) pb = endInfo(e.b, pa, route, map);
    if (route === 'straight') return { pts: [pa, pb], curve: null };
    // かぎ線で、はしの向きが決まっていない（自動の）とき：ほかの図形を通らない向きを選ぶ
    if (route !== 'curve' && (autoEnd(e.a, na) || autoEnd(e.b, nb))) {
      const ca2 = sidesFor(e.a, na, cb || pb), cb2 = sidesFor(e.b, nb, ca || pa);
      let best = null;
      for (const A of ca2) for (const B of cb2) {
        const pts = elbow(A, B, e);
        // 出ていく線と入ってくる線が同じ辺で重なるのは避ける（同じ向きどうしの枝分かれはよい）
        const cost = routeCost(pts, obs || obstacles(map), na, nb) + axisPenalty(na, A, cb || pb) + axisPenalty(nb, B, ca || pa) + (used ? ((na && A.dir ? used.get(na.id + ':' + A.dir + ':in') || 0 : 0) + (nb && B.dir ? used.get(nb.id + ':' + B.dir + ':out') || 0 : 0)) * 90 : 0);
        if (!best || cost < best.cost) best = { cost, pts };
      }
      if (best) return { pts: best.pts, curve: null };
    }
    if (route === 'curve') {
      const d = Math.hypot(pb.x - pa.x, pb.y - pa.y), k = Math.max(30, d * 0.4);
      const push = (p, other) => {
        const dir = p.dir || (Math.abs(other.x - p.x) > Math.abs(other.y - p.y) ? (other.x > p.x ? 'r' : 'l') : (other.y > p.y ? 'b' : 't'));
        return { x: p.x + (dir === 'r' ? k : dir === 'l' ? -k : 0), y: p.y + (dir === 'b' ? k : dir === 't' ? -k : 0) };
      };
      return { pts: [pa, pb], curve: [push(pa, pb), push(pb, pa)] };
    }
    return { pts: elbow(pa, pb, e), curve: null };
  }
  // すべての線の通り道（前の線が使った辺を覚えながら、順番に決める）
  function geomAll(doc, map = new Map(doc.nodes.map(n => [n.id, n]))) {
    const used = new Map(), out = new Map(), obs = obstacles(map);
    const use = (id, d, role) => { if (id != null && d) { const k = id + ':' + d + ':' + role; used.set(k, (used.get(k) || 0) + 1); } };
    for (const e of doc.edges) {
      const g = edgeGeom(e, map, used, obs);
      out.set(e.id, g);
      use(e.a && e.a.id, g.pts[0].dir, 'out');
      use(e.b && e.b.id, g.pts[g.pts.length - 1].dir, 'in');
    }
    return out;
  }
  // かぎ線（縦と横だけ）の点の並び
  const hz = d => d === 'l' || d === 'r', vt = d => d === 't' || d === 'b';
  function elbow(pa, pb, e) {
    const da = pa.dir || (Math.abs(pb.x - pa.x) >= Math.abs(pb.y - pa.y) ? 'r' : 'b');
    const db = pb.dir || (hz(da) ? 'l' : 't');
    if (hz(da) && hz(db)) {
      const mx = e.mid != null ? (pa.x + pb.x) / 2 + e.mid : (pa.x + pb.x) / 2;
      return Math.abs(pa.y - pb.y) < 0.5 ? [pa, pb] : [pa, { x: mx, y: pa.y }, { x: mx, y: pb.y }, pb];
    }
    if (vt(da) && vt(db)) {
      const my = e.mid != null ? (pa.y + pb.y) / 2 + e.mid : (pa.y + pb.y) / 2;
      return Math.abs(pa.x - pb.x) < 0.5 ? [pa, pb] : [pa, { x: pa.x, y: my }, { x: pb.x, y: my }, pb];
    }
    if (hz(da)) return [pa, { x: pb.x, y: pa.y }, pb];
    return [pa, { x: pa.x, y: pb.y }, pb];
  }
  // 向きが自動のはし（ふつうの図形につないでいて、辺を決めていない）
  const autoEnd = (end, n) => !!(n && !isLine(n) && n.t !== 'activation' && !end.side);
  // はしの候補：相手のほうを向いている辺（自動でなければ1つだけ）
  function sidesFor(end, n, other) {
    if (!autoEnd(end, n)) return [endInfo(end, other, 'elbow', new Map(n ? [[n.id, n]] : []))];
    const c = center(n), out = [];
    if (other.x > c.x + 0.5) out.push('r');
    if (other.x < c.x - 0.5) out.push('l');
    if (other.y > c.y + 0.5) out.push('b');
    if (other.y < c.y - 0.5) out.push('t');
    if (!out.length) out.push('r');
    return out.map(sd => ({ ...sidePoint(n, sd), dir: sd }));
  }
  // 相手が主に横にいるのに上下の辺を使う（またはその逆）ときは少し悪くする
  function axisPenalty(n, end, other) {
    if (!n || !end.dir || !other) return 0;
    const c = center(n), rx = Math.abs(other.x - c.x) / Math.max(n.w, 1), ry = Math.abs(other.y - c.y) / Math.max(n.h, 1);
    if (hz(end.dir) && ry > rx * 1.2) return 100;
    if (vt(end.dir) && rx > ry * 1.2) return 100;
    return 0;
  }
  // 線が避ける図形を、場所ごとの箱に分けておく（近くの図形だけ調べれば済むように）
  const CELL = 240;
  function obstacles(map) {
    const cells = new Map();
    for (const n of map.values()) {
      if (n.t === 'frame' || isLine(n)) continue;
      const o = { n, mark: 0 };
      for (let cx = Math.floor(n.x / CELL); cx <= Math.floor((n.x + n.w) / CELL); cx++) {
        for (let cy = Math.floor(n.y / CELL); cy <= Math.floor((n.y + n.h) / CELL); cy++) {
          const k = cx + ',' + cy;
          if (!cells.has(k)) cells.set(k, []);
          cells.get(k).push(o);
        }
      }
    }
    return { cells, stamp: 0 };
  }
  function nearby(obs, x0, y0, x1, y1) {
    const out = [], st = ++obs.stamp;
    for (let cx = Math.floor(x0 / CELL); cx <= Math.floor(x1 / CELL); cx++) {
      for (let cy = Math.floor(y0 / CELL); cy <= Math.floor(y1 / CELL); cy++) {
        const arr = obs.cells.get(cx + ',' + cy);
        if (arr) for (const o of arr) if (o.mark !== st) { o.mark = st; out.push(o.n); }
      }
    }
    return out;
  }
  // 通り道のよしあし：短いほど・曲がりが少ないほどよく、図形を横切ると大きく悪くする
  function routeCost(pts, obs, na, nb) {
    let len = 0, bx0 = Infinity, by0 = Infinity, bx1 = -Infinity, by1 = -Infinity;
    for (let i = 0; i < pts.length; i++) {
      const p = pts[i];
      if (i) len += Math.abs(p.x - pts[i - 1].x) + Math.abs(p.y - pts[i - 1].y);
      if (p.x < bx0) bx0 = p.x; if (p.x > bx1) bx1 = p.x; if (p.y < by0) by0 = p.y; if (p.y > by1) by1 = p.y;
    }
    let hits = 0;
    for (const n of nearby(obs, bx0, by0, bx1, by1)) {
      const own = n === na || n === nb;
      const x0 = n.x + 2, y0 = n.y + 2, x1 = n.x + n.w - 2, y1 = n.y + n.h - 2;
      for (let i = 1; i < pts.length; i++) {
        const a = pts[i - 1], b = pts[i];
        const sx0 = Math.min(a.x, b.x), sx1 = Math.max(a.x, b.x), sy0 = Math.min(a.y, b.y), sy1 = Math.max(a.y, b.y);
        if (sx1 > x0 && sx0 < x1 && sy1 > y0 && sy0 < y1) { hits += own ? 2 : 1; break; }
      }
    }
    // 辺から出てすぐ逆向きに戻る形も悪くする
    const back = (p, q, d) => (d === 'r' && q.x < p.x - 0.5) || (d === 'l' && q.x > p.x + 0.5) || (d === 'b' && q.y < p.y - 0.5) || (d === 't' && q.y > p.y + 0.5);
    const L = pts.length - 1;
    let bad = 0;
    if (pts[0].dir && back(pts[0], pts[1], pts[0].dir)) bad++;
    if (pts[L].dir && back(pts[L], pts[L - 1], pts[L].dir)) bad++;
    return len + (pts.length - 2) * 24 + hits * 2000 + bad * 600;
  }

  // 矢印の形（はしの点 p、線が来た向き ang）。trim：線を手前で止める長さ
  const ENDS = {
    none: { name: 'なし', trim: 0 },
    arrow: { name: '矢印', trim: 9 },
    open: { name: '矢印（線だけ）', trim: 0 },
    triangle: { name: '白い三角（継承・実現）', trim: 13 },
    diamond: { name: '黒いひし形（コンポジション）', trim: 20 },
    odiamond: { name: '白いひし形（集約）', trim: 20 },
    circle: { name: '丸', trim: 9 },
    one: { name: 'ER：1', trim: 0 },
    only: { name: 'ER：1（必ず1つ）', trim: 0 },
    zone: { name: 'ER：0か1', trim: 0 },
    many: { name: 'ER：多', trim: 0 },
    omany: { name: 'ER：1以上', trim: 0 },
    zmany: { name: 'ER：0以上', trim: 0 },
  };
  function endShape(kind, p, ang, color, sw) {
    if (!kind || kind === 'none') return '';
    const c = Math.cos(ang), s = Math.sin(ang);
    // 線の向きにそった座標（u：後ろへ、v：横へ）→ 画面の座標
    const P = (u, v) => `${r1(p.x - c * u - s * v)} ${r1(p.y - s * u + c * v)}`;
    const st = `stroke="${color}" stroke-width="${sw}" stroke-linejoin="round" stroke-linecap="round"`;
    const bar = u => `M${P(u, -7)}L${P(u, 7)}`;
    const crow = () => `M${P(0, -8)}L${P(13, 0)}L${P(0, 8)}M${P(0, 0)}L${P(13, 0)}`;
    const ring = u => `<circle cx="${P(u, 0).split(' ')[0]}" cy="${P(u, 0).split(' ')[1]}" r="4.5" fill="#fff" ${st}/>`;
    switch (kind) {
      case 'arrow': return `<path d="M${P(0, 0)}L${P(11, -5.5)}L${P(11, 5.5)}Z" fill="${color}" ${st}/>`;
      case 'open': return `<path d="M${P(11, -6)}L${P(0, 0)}L${P(11, 6)}" fill="none" ${st}/>`;
      case 'triangle': return `<path d="M${P(0, 0)}L${P(14, -8)}L${P(14, 8)}Z" fill="#fff" ${st}/>`;
      case 'diamond': return `<path d="M${P(0, 0)}L${P(10, -6)}L${P(20, 0)}L${P(10, 6)}Z" fill="${color}" ${st}/>`;
      case 'odiamond': return `<path d="M${P(0, 0)}L${P(10, -6)}L${P(20, 0)}L${P(10, 6)}Z" fill="#fff" ${st}/>`;
      case 'circle': return `<circle cx="${P(4.5, 0).split(' ')[0]}" cy="${P(4.5, 0).split(' ')[1]}" r="4.5" fill="#fff" ${st}/>`;
      case 'one': return `<path d="${bar(10)}" fill="none" ${st}/>`;
      case 'only': return `<path d="${bar(8)}${bar(14)}" fill="none" ${st}/>`;
      case 'zone': return `<path d="${bar(8)}" fill="none" ${st}/>${ring(19)}`;
      case 'many': return `<path d="${crow()}" fill="none" ${st}/>`;
      case 'omany': return `<path d="${crow()}${bar(17)}" fill="none" ${st}/>`;
      case 'zmany': return `<path d="${crow()}" fill="none" ${st}/>${ring(22)}`;
      default: return '';
    }
  }
  // 点の並びの長さの半分の場所（ラベルを置く）
  function midOf(pts, curve) {
    if (curve) {
      const [a, b] = pts, [c1, c2] = curve, t = 0.5, m = 1 - t;
      return { x: m * m * m * a.x + 3 * m * m * t * c1.x + 3 * m * t * t * c2.x + t * t * t * b.x, y: m * m * m * a.y + 3 * m * m * t * c1.y + 3 * m * t * t * c2.y + t * t * t * b.y };
    }
    let total = 0;
    for (let i = 1; i < pts.length; i++) total += Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y);
    let half = total / 2;
    for (let i = 1; i < pts.length; i++) {
      const L = Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y);
      if (half <= L && L) { const k = half / L; return { x: pts[i - 1].x + (pts[i].x - pts[i - 1].x) * k, y: pts[i - 1].y + (pts[i].y - pts[i - 1].y) * k }; }
      half -= L;
    }
    return pts[0];
  }
  const EDGE_INK = '#495057';
  function edgeColor(e) { return e.c && COLORS[e.c] && e.c !== 'white' ? COLORS[e.c].s : EDGE_INK; }
  function edgeSVG(e, map, { hit = true, hideText = false, geom = null } = {}) {
    const g = geom || edgeGeom(e, map);
    const pts = g.pts.map(p => ({ x: p.x, y: p.y }));
    if (pts.length < 2) return '';
    const color = edgeColor(e), sw = e.w || 1.6;
    // はしの向き（矢印の向き）
    const last = pts.length - 1;
    const prevB = g.curve ? g.curve[1] : pts[last - 1], prevA = g.curve ? g.curve[0] : pts[1];
    const angB = Math.atan2(pts[last].y - prevB.y, pts[last].x - prevB.x);
    const angA = Math.atan2(pts[0].y - prevA.y, pts[0].x - prevA.x);
    const tipA = { ...pts[0] }, tipB = { ...pts[last] };
    const cut = (p, ang, len) => { p.x -= Math.cos(ang) * len; p.y -= Math.sin(ang) * len; };
    cut(pts[last], angB, (ENDS[e.e] || ENDS.none).trim);
    cut(pts[0], angA, (ENDS[e.s] || ENDS.none).trim);
    const d = g.curve
      ? `M${r1(pts[0].x)} ${r1(pts[0].y)}C${r1(g.curve[0].x)} ${r1(g.curve[0].y)} ${r1(g.curve[1].x)} ${r1(g.curve[1].y)} ${r1(pts[1].x)} ${r1(pts[1].y)}`
      : 'M' + pts.map(p => `${r1(p.x)} ${r1(p.y)}`).join('L');
    let s = '';
    if (hit) s += `<path d="${d}" fill="none" stroke="transparent" stroke-width="14"/>`;
    s += `<path d="${d}" fill="none" stroke="${color}" stroke-width="${sw}"${e.dash ? ' stroke-dasharray="7 5"' : ''} stroke-linejoin="round" stroke-linecap="round"/>`;
    s += endShape(e.e || 'none', tipB, angB, color, sw) + endShape(e.s || 'none', tipA, angA, color, sw);
    if (e.text && !hideText) {
      const m = midOf(g.pts, g.curve), fs = 12.5;
      const lines = String(e.text).split('\n');
      const w = Math.max(...lines.map(l => textW(l, fs))) + 12, h = lines.length * fs * 1.35 + 6;
      s += `<rect x="${r1(m.x - w / 2)}" y="${r1(m.y - h / 2)}" width="${r1(w)}" height="${r1(h)}" rx="5" fill="#fff" fill-opacity=".94"/>`;
      s += textBlock(lines, m.x, m.y, fs, { color: INK });
    }
    return `<g${hit ? ` data-edge="${esc(e.id)}"` : ''}>${s}</g>`;
  }

  // ---------- 全体 ----------
  // 枠（グループ）はいちばん後ろ、その上に線、その上に図形
  function contentSVG(doc, opt = {}) {
    const map = new Map(doc.nodes.map(n => [n.id, n]));
    const hide = opt.editing;
    const frames = doc.nodes.filter(n => n.t === 'frame'), others = doc.nodes.filter(n => n.t !== 'frame');
    const gs = opt.geoms || geomAll(doc, map);
    const o = { assets: doc.assets, thumb: opt.thumb };
    return frames.map(n => nodeSVG(n, { ...o, hit: opt.hit, hideText: hide === n.id })).join('') +
      doc.edges.map(e => edgeSVG(e, map, { hit: opt.hit, hideText: hide === e.id, geom: gs.get(e.id) })).join('') +
      others.map(n => nodeSVG(n, { ...o, hit: opt.hit, hideText: hide === n.id })).join('');
  }
  // 全部が入る四角
  function bounds(doc, ids) {
    const map = new Map(doc.nodes.map(n => [n.id, n]));
    const gs = doc.edges.length ? geomAll(doc, map) : new Map();
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    const add = (x, y) => { x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y); };
    for (const n of doc.nodes) if (!ids || ids.has(n.id)) { add(n.x, n.y); add(n.x + n.w, n.y + n.h); if (n.t === 'actorline') { add(n.x - 20, n.y); add(n.x + n.w + 20, n.y); } }
    for (const e of doc.edges) {
      if (ids && !ids.has(e.id)) continue;
      const g = gs.get(e.id);
      for (const p of g.pts.concat(g.curve || [])) add(p.x, p.y);
      if (e.text) { const m = midOf(g.pts, g.curve), w = textW(e.text, 12.5) / 2 + 8; add(m.x - w, m.y - 14); add(m.x + w, m.y + 14); }
    }
    if (x0 === Infinity) return { x: 0, y: 0, w: 0, h: 0 };
    return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
  }
  // 書き出し用の SVG（ほかのアプリやノートでも同じに見えるように、色や文字はすべて中に書く）
  function exportSVG(doc, { pad = 24, bg = '#ffffff', maxW = 0, thumb = false } = {}) {
    const b = bounds(doc);
    const W = Math.ceil(b.w + pad * 2) || 200, H = Math.ceil(b.h + pad * 2) || 120;
    const size = maxW && W > maxW ? `width="${maxW}" height="${Math.round(H * maxW / W)}"` : `width="${W}" height="${H}"`;
    return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${W} ${H}" ${size}>` +
      (bg ? `<rect width="${W}" height="${H}" fill="${bg}"/>` : '') +
      `<g transform="translate(${r1(pad - b.x)} ${r1(pad - b.y)})">${contentSVG(doc, { hit: false, thumb })}</g></svg>`;
  }
  // 手で書いたファイル（Claude Code など）でも読めるように、分かりやすい別名も受け付ける
  const NODE_ALIAS = { type: 't', label: 'text', color: 'c', width: 'w', height: 'h', icon: 'ic', fontSize: 'fs' };
  const EDGE_ALIAS = { from: 'a', to: 'b', label: 'text', route: 'r', start: 's', end: 'e', color: 'c', dashed: 'dash' };
  const ROUTE_ALIAS = { line: 'straight', straight: 'straight', elbow: 'elbow', orthogonal: 'elbow', curve: 'curve', curved: 'curve' };
  const alias = (o, A) => { const r = { ...o }; for (const [k, v] of Object.entries(A)) if (r[k] !== undefined && r[v] === undefined) { r[v] = r[k]; delete r[k]; } return r; };
  // 古いデータや手で書いたデータでも動くように、形をそろえる
  function normalize(doc) {
    const d = doc && typeof doc === 'object' ? doc : {};
    const raw = (Array.isArray(d.nodes) ? d.nodes : []).map(n => (n && typeof n === 'object' ? alias(n, NODE_ALIAS) : null));
    const placed = new Set();
    const nodes = raw.filter(n => n && SHAPES[n.t]).map((n, i) => {
      if (Number.isFinite(+n.x) && Number.isFinite(+n.y) && n.x !== null && n.y !== null && n.x !== '' && n.y !== '') placed.add(i);
      return { ...n, id: String(n.id ?? 'n' + (i + 1)), x: +n.x || 0, y: +n.y || 0, w: Math.max(8, +n.w || SHAPES[n.t].w), h: Math.max(8, +n.h || SHAPES[n.t].h), text: String(n.text ?? '') };
    });
    const ids = new Set(nodes.map(n => n.id));
    const toEnd = x => (typeof x === 'string' || typeof x === 'number' ? { id: String(x) } : x);
    const okEnd = x => x && (x.id != null ? ids.has(String(x.id)) : Number.isFinite(+x.x) && Number.isFinite(+x.y));
    const edges = (Array.isArray(d.edges) ? d.edges : []).map(e => (e && typeof e === 'object' ? alias(e, EDGE_ALIAS) : null)).filter(Boolean)
      .map(e => ({ ...e, a: toEnd(e.a), b: toEnd(e.b) })).filter(e => okEnd(e.a) && okEnd(e.b)).map((e, i) => ({
        ...e, id: String(e.id ?? 'e' + (i + 1)), a: e.a.id != null ? { ...e.a, id: String(e.a.id) } : { x: +e.a.x, y: +e.a.y }, b: e.b.id != null ? { ...e.b, id: String(e.b.id) } : { x: +e.b.x, y: +e.b.y },
        r: ROUTE_ALIAS[e.r] || (e.r ? 'elbow' : undefined), text: String(e.text ?? ''),
      }));
    for (const e of edges) { if (!e.r) delete e.r; if (e.s === undefined) e.s = 'none'; if (e.e === undefined) e.e = 'arrow'; }
    // 場所（x・y）が書いていない図形は、線のつながりにそって上から順に並べる
    if (placed.size < nodes.length) autoPlace(nodes, edges, placed);
    for (const n of nodes) if (SHAPES[n.t].auto) n.h = autoH(n);
    // 画像の中身：画像（data:image/…）で、使っているものだけ残す
    const assets = {}, src = d.assets && typeof d.assets === 'object' ? d.assets : {};
    for (const n of nodes) if (n.t === 'image' && typeof src[n.src] === 'string' && /^data:image\/(png|jpe?g|gif|webp);base64,/i.test(src[n.src])) assets[n.src] = src[n.src];
    return { v: 1, nodes, edges, assets };
  }

  // 場所のない図形を並べる：線でつながる順に段を決めて（上から下へ）、段の中は左から右へ
  function autoPlace(nodes, edges, placed) {
    let free = nodes.filter((n, i) => !placed.has(i));
    // シーケンス図の参加者は、横1列に並べる（線の向きでは段を変えない）
    const lines = free.filter(n => isLine(n));
    let top0 = 0;
    for (const n of nodes) if (!free.includes(n)) top0 = Math.max(top0, n.y + n.h + 80);
    if (lines.length) {
      let x = 0;
      for (const n of lines) { n.x = x; n.y = top0; x += n.w + 80; }
      top0 += Math.max(...lines.map(n => n.h)) + 80;
      free = free.filter(n => !isLine(n));
      if (!free.length) return;
    }
    const idx = new Map(free.map(n => [n.id, n]));
    // 段：はじまり（ほかから線が来ない図形）から順に、つながる先を1段下へ（戻る線は数えない）
    const next = new Map(free.map(n => [n.id, []])), incoming = new Set();
    for (const e of edges) {
      if (e.a.id == null || e.b.id == null || e.a.id === e.b.id || !idx.has(e.a.id) || !idx.has(e.b.id)) continue;
      next.get(e.a.id).push(e.b.id);
      incoming.add(e.b.id);
    }
    const level = new Map();
    const walk = start => {
      const q = [start];
      level.set(start, level.get(start) ?? 0);
      while (q.length) { const id = q.shift(); for (const t of next.get(id)) if (!level.has(t)) { level.set(t, level.get(id) + 1); q.push(t); } }
    };
    for (const n of free) if (!incoming.has(n.id) && !level.has(n.id)) walk(n.id);
    for (const n of free) if (!level.has(n.id)) walk(n.id);   // 輪になっているだけのもの
    // 置いてある図形があれば、その下に並べる
    let top = top0;
    const rows = new Map();
    for (const n of free) { const l = level.get(n.id); if (!rows.has(l)) rows.set(l, []); rows.get(l).push(n); }
    let y = top;
    for (const l of [...rows.keys()].sort((a, b) => a - b)) {
      const row = rows.get(l);
      const H = Math.max(...row.map(n => (SHAPES[n.t].auto ? autoH(n) : n.h)));
      let x = 0;
      const total = row.reduce((s, n) => s + n.w, 0) + (row.length - 1) * 60;
      x = -total / 2;
      for (const n of row) { n.x = Math.round(x); n.y = Math.round(y + (H - (SHAPES[n.t].auto ? autoH(n) : n.h)) / 2); x += n.w + 60; }
      y += H + 90;
    }
  }
  // 画面のないところ（アプリの裏側）で一覧の小さな絵を作るとき、アイコンの絵を渡す
  let iconSet = null;
  const setIcons = o => { iconSet = o; };

  const API = { FONT, setIcons, autoPlace, MONO, COLORS, SHAPES, ENDS, colorOf, isLine, textW, wrap, esc, parseEntity, parseClass, autoH, bodyPath, nodeSVG, edgeSVG, edgeGeom, geomAll, obstacles, endShape, contentSVG, bounds, exportSVG, normalize, center, boundary, textBox, iconSVG, midOf, EDGE_INK };
  if (typeof module !== 'undefined' && module.exports) module.exports = API;
  else root.DiagramDraw = API;
})(typeof window !== 'undefined' ? window : globalThis);
