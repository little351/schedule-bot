// Игра «Блоки» (падающие фигуры) для раздела «Другое»: бесконечный, управление кнопками под полем. Файл грузится только при открытии
// карточки, на скорость открытия расписания не влияет. Всё рисуется кодом, звука нет.
(() => {
  "use strict";
  const COLS = 12, ROWS = 18;
  const COLOR_STEP = 1000;  // каждые столько очков все блоки меняют цвет
  const LINE_POINTS = [0, 100, 300, 500, 800];
  // Сложность растёт ступенями каждые 25 000 очков до 100 000, дальше не меняется.
  // Секунды на одну клетку падения: поле (18 клеток) фигура проходит за 14, 12, 10, 8 и 5 с (решение Артёма)
  const DIFF_STEP = 25000, FALL_SECONDS = [14, 12, 10, 8, 5].map((s) => s / 18);
  // Сложный режим: сразу 5 с на поле, дальше каждые 25 000 очков на 10% быстрее, но не быстрее 1,5 с (решение Артёма).
  // У режимов разные таблицы рекордов, очки обоих идут на один счёт магазина
  const HARD_START = 5, HARD_FACTOR = 0.9, HARD_MIN = 1.5;
  const MODES = { normal: { name: "Обычный", game: "tetris" }, hard: { name: "Сложный", game: "tetris_hard" } };
  const MODE_KEY = "blocks_mode";
  // Счёт, который не дошёл до сервера (нет связи, устаревшая подпись Telegram), и партия, оставленная на паузе:
  // хранятся на телефоне и отправляются при следующем открытии игры, чтобы очки не пропадали
  const UNSAVED_KEY = "blocks_unsaved", RUN_KEY = "blocks_run", UNSAVED_MAX = 20;
  const FIELD_BG = "#1f2533";
  const LOCK_DELAY = 0.5, LOCK_RESETS = 15;  // сколько фигура может лежать на опоре, пока её двигают
  const REPEAT_DELAY = 170, REPEAT_EVERY = 55;  // удержание кнопки: пауза перед повтором и шаг повтора, мс
  const tg = window.Telegram?.WebApp;

  // Скины: свой набор цветов (они сменяются каждые COLOR_STEP очков) и свой рисунок блока.
  // Цены и список купленных хранит сервер, выбранный скин и своя картинка - только на этом телефоне.
  const SKINS = {
    classic: { name: "Классика", colors: ["#0a84ff", "#ff9f0a", "#30d158", "#bf5af2", "#ff375f", "#64d2ff", "#ffd60a"], paint: paintClassic },
    neon: { name: "Неон", colors: ["#00f0ff", "#ff2bd6", "#39ff14", "#ffe600", "#ff6b00", "#8a5cff"], paint: paintNeon },
    pixel: { name: "Пиксель", colors: ["#e43b44", "#3e8948", "#0099db", "#feae34", "#b55088", "#2ce8f5"], paint: paintPixel },
    candy: { name: "Конфеты", colors: ["#ff8fab", "#7fd8d4", "#9be8a8", "#f7c873", "#c3a3dc", "#ffadc9"], paint: paintCandy },
    wood: { name: "Дерево", colors: ["#b5793f", "#8b4a2b", "#d2a56d", "#6b4226", "#c68642", "#a0522d"], paint: paintWood },
    gold: { name: "Золото", colors: ["#f5c542", "#c0c0c8", "#e8a598", "#cd7f32", "#e5e4e2", "#ffd700"], paint: paintGold },
    custom: { name: "Свой скин", colors: [null], paint: paintCustom },  // своя картинка не перекрашивается
  };
  const SKIN_KEY = "blocks_skin", IMAGE_KEY = "blocks_custom_image", IMAGE_SIZE = 256;

  // Фигуры: клетки в рамке N×N, поворот по часовой стрелке вокруг центра рамки
  const SHAPES = {
    I: [[0, 1], [1, 1], [2, 1], [3, 1]],
    O: [[1, 0], [2, 0], [1, 1], [2, 1]],
    T: [[1, 0], [0, 1], [1, 1], [2, 1]],
    S: [[1, 0], [2, 0], [0, 1], [1, 1]],
    Z: [[0, 0], [1, 0], [1, 1], [2, 1]],
    J: [[0, 0], [0, 1], [1, 1], [2, 1]],
    L: [[2, 0], [0, 1], [1, 1], [2, 1]],
  };
  const SIZE = { I: 4, O: 4, T: 3, S: 3, Z: 3, J: 3, L: 3 };
  const KICKS = [[0, 0], [-1, 0], [1, 0], [0, -1], [-2, 0], [2, 0]];

  let root, canvas, ctx, nextCanvas, nextCtx, over, hud, cell = 24;
  // холсты рисуются в настоящих пикселях экрана (на телефонах их в 2-3 раза больше, чем CSS-пикселей), иначе всё мыльное
  const dpr = Math.min(3, Math.max(1, Math.round(window.devicePixelRatio || 1)));

  // холст размером w×h CSS-пикселей с плотностью экрана; рисовать в нём можно в CSS-пикселях
  function sizeCanvas(c, w, h) {
    c.width = w * dpr;
    c.height = h * dpr;
    c.style.width = w + "px";
    c.style.height = h + "px";
    const x = c.getContext("2d");
    x.setTransform(dpr, 0, 0, dpr, 0, 0);
    return x;
  }
  let S = null, running = false, rafId = 0, last = 0, best = null;
  let skin = loadSkin(), customImage = null, shop = null;
  let mode = MODES[storageGet(MODE_KEY)] ? storageGet(MODE_KEY) : "normal";

  function rotated(cells, size, times) {
    let out = cells;
    for (let i = 0; i < times; i++) out = out.map(([x, y]) => [size - 1 - y, x]);
    return out;
  }

  function newGame() {
    S = {
      board: Array.from({ length: ROWS }, () => Array(COLS).fill(0)),
      bag: [], piece: null, next: null,
      score: 0, lines: 0, fall: 0, lockTimer: 0, lockResets: 0, flash: 0,
    };
    S.next = takeFromBag();
    spawn();
  }

  // «Мешок»: все 7 фигур в случайном порядке, потом новый мешок, так длинных серий без нужной фигуры не бывает
  function takeFromBag() {
    if (!S.bag.length) {
      S.bag = Object.keys(SHAPES);
      for (let i = S.bag.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [S.bag[i], S.bag[j]] = [S.bag[j], S.bag[i]];
      }
    }
    return S.bag.pop();
  }

  function spawn() {
    const type = S.next;
    S.next = takeFromBag();
    S.piece = { type, rot: 0, x: Math.floor((COLS - SIZE[type]) / 2), y: type === "I" ? -1 : 0 };
    S.fall = 0;
    S.lockTimer = 0;
    S.lockResets = 0;
    S.lowest = S.piece.y;  // самая низкая строка, до которой фигура уже доходила
    S.landed = false;  // фигура уже касалась опоры на этой высоте
    if (!fits(S.piece)) gameOver();
  }

  function cellsOf(p) {
    // квадрат при повороте не меняется: в рамке 4×4 он бы сдвигался по кругу
    const rot = p.type === "O" ? 0 : p.rot;
    return rotated(SHAPES[p.type], SIZE[p.type], rot).map(([x, y]) => [p.x + x, p.y + y]);
  }

  function fits(p) {
    return cellsOf(p).every(([x, y]) => x >= 0 && x < COLS && y < ROWS && (y < 0 || !S.board[y][x]));
  }

  function grounded() {
    return !fits({ ...S.piece, y: S.piece.y + 1 });
  }

  function tryMove(dx, dy, drot = 0) {
    if (!running) return false;
    const p = S.piece;
    const kicks = drot ? KICKS : [[0, 0]];
    for (const [kx, ky] of kicks) {
      const moved = { ...p, x: p.x + dx + kx, y: p.y + dy + ky, rot: (p.rot + drot + 4) % 4 };
      if (fits(moved)) {
        S.piece = moved;
        if (moved.y > S.lowest) {
          // опустилась ниже, чем была: снова можно двигать её на опоре
          S.lowest = moved.y;
          S.lockResets = 0;
          S.landed = false;
          S.lockTimer = 0;
        } else if (dy === 0 && S.landed) {
          // фигуру, коснувшуюся опоры, можно ещё подвинуть или повернуть, но не больше LOCK_RESETS раз:
          // раньше поворот приподнимал её над опорой, таймер сбрасывался, и она не ложилась никогда
          S.lockTimer = 0;
          S.lockResets++;
        }
        draw();
        return true;
      }
    }
    return false;
  }

  function softDrop() {
    if (!running) return;
    if (tryMove(0, 1)) {
      S.score += 1;
      S.fall = 0;
      afterScore();
    } else {
      releaseAll();  // фигура легла: удержание «вниз» не должно сразу ронять следующую
      lock();
    }
    draw();
  }

  function lock() {
    for (const [x, y] of cellsOf(S.piece)) {
      if (y < 0) { gameOver(); return; }
      S.board[y][x] = 1;
    }
    S.boardVer = (S.boardVer || 0) + 1;  // поле изменилось: его надо перерисовать
    const full = S.board.filter((row) => row.every(Boolean)).length;
    if (full) {
      S.board = S.board.filter((row) => !row.every(Boolean));
      while (S.board.length < ROWS) S.board.unshift(Array(COLS).fill(0));
      S.lines += full;
      S.score += LINE_POINTS[full] * (level() + 1);
      afterScore();
      haptic(full >= 4 ? "success" : "light");
    }
    spawn();
  }

  function level() {
    return Math.floor(S.lines / 10);
  }

  function colorIndex(score = S.score) {
    return Math.floor(score / COLOR_STEP) % SKINS[skin].colors.length;
  }

  let shownColor = 0;
  function afterScore() {
    const c = colorIndex();
    if (c !== shownColor) {
      shownColor = c;
      S.flash = 0.35;  // короткая вспышка, чтобы смена цвета была заметна
    }
  }

  function difficulty() {
    return Math.min(FALL_SECONDS.length - 1, Math.floor(S.score / DIFF_STEP));
  }

  function fallInterval() {
    if (mode === "hard") {
      return Math.max(HARD_MIN, HARD_START * HARD_FACTOR ** Math.floor(S.score / DIFF_STEP)) / ROWS;
    }
    return FALL_SECONDS[difficulty()];
  }

  function update(dt) {
    if (S.flash > 0) S.flash = Math.max(0, S.flash - dt);
    if (grounded()) {
      S.landed = true;
      S.lockTimer += dt;
      // ходы на опоре кончились: фигура ложится сразу, как только снова на неё встанет
      if (S.lockTimer >= LOCK_DELAY || S.lockResets >= LOCK_RESETS) lock();
      return;
    }
    S.lockTimer = 0;
    S.fall += dt;
    while (S.fall >= fallInterval()) {
      S.fall -= fallInterval();
      if (!tryMove(0, 1)) break;
    }
  }

  function haptic(kind) {
    try {
      if (kind === "success") tg?.HapticFeedback?.notificationOccurred("success");
      else tg?.HapticFeedback?.impactOccurred(kind);
    } catch (e) { /* старый Telegram */ }
  }

  // --- Рисование: каждый вид блока рисуется один раз в маленький холст и потом только копируется ---
  function shade(hex, f) {
    const n = parseInt(hex.slice(1), 16), t = f < 0 ? 0 : 255, k = Math.abs(f);
    const mix = (v) => Math.round(v + (t - v) * k);
    return `rgb(${mix(n >> 16)}, ${mix((n >> 8) & 255)}, ${mix(n & 255)})`;
  }

  function paintClassic(c, s, col) {
    const b = Math.max(2, Math.round(s / 7));
    c.fillStyle = col; c.fillRect(0, 0, s, s);
    c.fillStyle = shade(col, 0.4); c.fillRect(0, 0, s, b); c.fillRect(0, 0, b, s);
    c.fillStyle = shade(col, -0.35); c.fillRect(0, s - b, s, b); c.fillRect(s - b, 0, b, s);
  }

  function paintNeon(c, s, col) {
    const w = Math.max(2, Math.round(s / 9)), pad = Math.max(2, Math.round(s / 8));
    c.fillStyle = col; c.globalAlpha = 0.16; c.fillRect(pad, pad, s - 2 * pad, s - 2 * pad); c.globalAlpha = 1;
    c.shadowColor = col; c.shadowBlur = s / 3;
    c.strokeStyle = col; c.lineWidth = w;
    c.strokeRect(pad, pad, s - 2 * pad, s - 2 * pad);
    c.shadowBlur = 0;
    c.strokeStyle = shade(col, 0.6); c.lineWidth = 1;
    c.strokeRect(pad + 0.5, pad + 0.5, s - 2 * pad - 1, s - 2 * pad - 1);
  }

  function paintPixel(c, s, col) {
    const u = Math.max(1, Math.floor(s / 8));
    c.fillStyle = shade(col, -0.5); c.fillRect(0, 0, s, s);
    c.fillStyle = col; c.fillRect(u, u, s - 2 * u, s - 2 * u);
    c.fillStyle = shade(col, -0.2); c.fillRect(u, s - 3 * u, s - 2 * u, 2 * u); c.fillRect(s - 3 * u, u, 2 * u, s - 2 * u);
    c.fillStyle = shade(col, 0.6); c.fillRect(2 * u, 2 * u, 2 * u, u); c.fillRect(2 * u, 2 * u, u, 2 * u);
  }

  function paintCandy(c, s, col) {
    const r = s * 0.3, p = 1;
    c.beginPath();
    c.roundRect ? c.roundRect(p, p, s - 2 * p, s - 2 * p, r) : c.rect(p, p, s - 2 * p, s - 2 * p);
    c.fillStyle = col; c.fill();
    c.save(); c.clip();
    c.strokeStyle = "rgba(255,255,255,0.28)"; c.lineWidth = s / 7;
    for (let k = -s; k < s * 2; k += s / 2.2) { c.beginPath(); c.moveTo(k, 0); c.lineTo(k - s, s); c.stroke(); }
    c.restore();
    c.fillStyle = "rgba(255,255,255,0.6)";
    c.beginPath(); c.ellipse(s * 0.33, s * 0.28, s * 0.17, s * 0.09, -0.5, 0, Math.PI * 2); c.fill();
  }

  function paintWood(c, s, col) {
    c.fillStyle = col; c.fillRect(0, 0, s, s);
    c.strokeStyle = shade(col, -0.25); c.lineWidth = Math.max(1, s / 22);
    for (let k = 1; k <= 4; k++) {
      const y = (s * k) / 5;
      c.beginPath(); c.moveTo(0, y);
      c.bezierCurveTo(s * 0.3, y - s * 0.08, s * 0.6, y + s * 0.08, s, y - s * 0.03);
      c.stroke();
    }
    c.fillStyle = shade(col, -0.35);
    c.beginPath(); c.ellipse(s * 0.68, s * 0.42, s * 0.08, s * 0.05, 0, 0, Math.PI * 2); c.fill();
    c.strokeStyle = shade(col, -0.45); c.lineWidth = Math.max(1, s / 14);
    c.strokeRect(c.lineWidth / 2, c.lineWidth / 2, s - c.lineWidth, s - c.lineWidth);
  }

  function paintGold(c, s, col) {
    const g = c.createLinearGradient(0, 0, s, s);
    g.addColorStop(0, shade(col, 0.6)); g.addColorStop(0.45, col);
    g.addColorStop(0.55, shade(col, -0.3)); g.addColorStop(1, shade(col, 0.25));
    c.fillStyle = g; c.fillRect(0, 0, s, s);
    const b = Math.max(2, Math.round(s / 6));
    c.strokeStyle = shade(col, -0.45); c.lineWidth = 1; c.strokeRect(0.5, 0.5, s - 1, s - 1);
    c.strokeStyle = "rgba(255,255,255,0.45)"; c.strokeRect(b + 0.5, b + 0.5, s - 2 * b - 1, s - 2 * b - 1);
  }

  function paintCustom(c, s, col) {
    if (!customImage) { paintClassic(c, s, col || "#8e8e93"); return; }
    c.drawImage(customImage, 0, 0, s, s);
    if (col) { c.globalAlpha = 0.32; c.fillStyle = col; c.fillRect(0, 0, s, s); c.globalAlpha = 1; }
    c.strokeStyle = "rgba(0,0,0,0.45)"; c.lineWidth = 1; c.strokeRect(0.5, 0.5, s - 1, s - 1);
  }

  const tiles = new Map();
  function tile(name, col, size) {
    const key = `${name}|${col}|${size}`;
    let t = tiles.get(key);
    if (!t) {
      t = document.createElement("canvas");
      t.width = t.height = size;
      const c = t.getContext("2d");
      c.imageSmoothingEnabled = name !== "pixel";
      SKINS[name].paint(c, size, col);
      tiles.set(key, t);
    }
    return t;
  }

  function block(c, x, y, size, col, alpha = 1, name = skin) {
    c.globalAlpha = alpha;
    c.drawImage(tile(name, col, size * dpr), x, y, size, size);
    c.globalAlpha = 1;
  }

  // Фон поля с сеткой рисуется один раз на размер клетки и потом только копируется
  let bg = null, bgCell = 0;
  function background() {
    if (bg && bgCell === cell) return bg;
    bg = document.createElement("canvas");
    bgCell = cell;
    const c = sizeCanvas(bg, COLS * cell, ROWS * cell);
    // поле светлее чёрного фона приложения, чтобы были видны его края (просьба Артёма)
    c.fillStyle = FIELD_BG;
    c.fillRect(0, 0, COLS * cell, ROWS * cell);
    c.strokeStyle = "rgba(255,255,255,0.08)";
    c.lineWidth = 1;
    c.beginPath();
    for (let x = 1; x < COLS; x++) { c.moveTo(x * cell + 0.5, 0); c.lineTo(x * cell + 0.5, ROWS * cell); }
    for (let y = 1; y < ROWS; y++) { c.moveTo(0, y * cell + 0.5); c.lineTo(COLS * cell, y * cell + 0.5); }
    c.stroke();
    return bg;
  }

  // Что видно на экране. Цикл игры перерисовывает поле, только если это изменилось: фигура сдвигается
  // несколько раз в секунду, а кадров 60, и перерисовка каждого кадра грела слабые телефоны
  let drawnState = null, drawnKey = "";
  function viewKey() {
    const p = S.piece;
    return `${p ? `${p.type}${p.rot},${p.x},${p.y}` : ""}|${S.boardVer || 0}|${S.flash}|${S.score}|${S.next}|${best}`;
  }
  function drawIfChanged() {
    if (S !== drawnState || viewKey() !== drawnKey) draw();
  }

  let hudText = ["", "", ""], nextKey = "";
  function setHud(i, el, value) {
    const text = String(value);
    if (hudText[i] !== text) { hudText[i] = text; el.textContent = text; }
  }

  function draw() {
    if (!S || !ctx) return;
    drawnState = S;
    drawnKey = viewKey();
    const colors = SKINS[skin].colors[colorIndex()];
    ctx.drawImage(background(), 0, 0, COLS * cell, ROWS * cell);
    S.board.forEach((row, y) => row.forEach((v, x) => { if (v) block(ctx, x * cell, y * cell, cell, colors); }));
    if (S.piece) {
      // тень: куда упадёт фигура
      let ghost = S.piece;
      while (fits({ ...ghost, y: ghost.y + 1 })) ghost = { ...ghost, y: ghost.y + 1 };
      for (const [x, y] of cellsOf(ghost)) if (y >= 0) block(ctx, x * cell, y * cell, cell, colors, 0.22);
      for (const [x, y] of cellsOf(S.piece)) if (y >= 0) block(ctx, x * cell, y * cell, cell, colors);
    }
    if (S.flash > 0) {
      ctx.fillStyle = `rgba(255,255,255,${(S.flash / 0.35) * 0.35})`;
      ctx.fillRect(0, 0, COLS * cell, ROWS * cell);
    }
    const key = `${S.next}|${skin}|${colors}|${tiles.size}`;
    if (key !== nextKey) { nextKey = key; drawNext(colors); }
    setHud(0, hud.score, S.score);
    setHud(1, hud.lines, S.lines);
    setHud(2, hud.best, best != null ? Math.max(best, S.score) : S.score);
  }

  function drawNext(colors) {
    const n = 64, size = Math.floor(n / 4);
    nextCtx.clearRect(0, 0, n, n);
    const cells = SHAPES[S.next];
    const xs = cells.map(([x]) => x), ys = cells.map(([, y]) => y);
    const w = Math.max(...xs) - Math.min(...xs) + 1, h = Math.max(...ys) - Math.min(...ys) + 1;
    const ox = (n - w * size) / 2 - Math.min(...xs) * size, oy = (n - h * size) / 2 - Math.min(...ys) * size;
    for (const [x, y] of cells) block(nextCtx, Math.round(ox + x * size), Math.round(oy + y * size), size, colors);
  }

  // Размер клетки под ширину экрана и высоту окна, чтобы поле и кнопки помещались без прокрутки
  function fit() {
    const width = root.clientWidth || 320;
    const height = (tg?.viewportStableHeight || window.innerHeight) - 260;
    cell = Math.max(12, Math.floor(Math.min((width - 74) / COLS, height / ROWS)));
    ctx = sizeCanvas(canvas, COLS * cell, ROWS * cell);
    draw();
  }

  // --- Цикл и экраны ---
  function loop(ts) {
    if (!running) return;
    if (!root.isConnected || root.offsetParent === null || document.hidden) { pause(); return; }
    const dt = last ? Math.min(0.05, (ts - last) / 1000) : 0;
    last = ts;
    update(dt);
    if (running) drawIfChanged();
    if (running) rafId = requestAnimationFrame(loop);
  }

  function resume() {
    storageSet(RUN_KEY, "");  // партия снова идёт: на паузе её больше нет
    over.style.display = "none";
    running = true;
    last = 0;
    try { tg?.disableVerticalSwipes?.(); } catch (e) { /* старый Telegram */ }
    rafId = requestAnimationFrame(loop);
  }

  function stop() {
    running = false;
    cancelAnimationFrame(rafId);
    releaseAll();
    try { tg?.enableVerticalSwipes?.(); } catch (e) { /* старый Telegram */ }
  }

  function pause() {
    if (!running) return;
    stop();
    // если мини-апп закроют на паузе, счёт этой партии отправится при следующем открытии игры
    if (S.score > 0) storageSet(RUN_KEY, JSON.stringify({ game: MODES[mode].game, score: S.score }));
    show("Пауза", "Игра остановлена. Если закрыть приложение, счёт сохранится при следующем открытии игры.",
      [["Продолжить", resume], ["Закончить и сохранить", gameOver, true]]);
  }

  function start() {
    newGame();
    hud.mode.textContent = MODES[mode].name;
    shownColor = 0;
    resume();
  }

  function show(title, text, buttons, note) {
    over.textContent = "";
    const box = document.createElement("div");
    box.className = "tt-box";
    const h = document.createElement("div");
    h.className = "tt-title";
    h.textContent = title;
    const p = document.createElement("div");
    p.className = "tt-text";
    p.textContent = text;
    box.append(h, p);
    if (note) box.append(note);
    const row = document.createElement("div");
    row.className = "tt-buttons";
    for (const [label, fn, secondary] of buttons) {
      const b = document.createElement("button");
      b.className = "tt-btn" + (secondary ? " tt-secondary" : "");
      b.textContent = label;
      b.disabled = true;  // защита от случайного тапа, пока игрок ещё жмёт на кнопки
      setTimeout(() => { b.disabled = false; }, 450);
      b.addEventListener("click", fn);
      row.append(b);
    }
    box.append(row);
    over.append(box);
    over.style.display = "";
  }

  let lastBoard = null;  // таблица из ответа на сохранение счёта: открывается без второго запроса

  function gameOver() {
    stop();
    storageSet(RUN_KEY, "");
    draw();
    haptic("heavy");
    const score = S.score;
    const note = document.createElement("div");
    note.className = "tt-note";
    note.textContent = "Сохраняю результат...";
    lastBoard = null;
    show("Игра окончена", `Счёт: ${score}, линий: ${S.lines}.`,
      [["Ещё раз", start], ["Режим", intro, true], ["Таблица лидеров", openBoard, true], ["Магазин", openShop, true]], note);
    sendScore(MODES[mode].game, score, note);
  }

  async function api(method, path, body, game = MODES[mode].game) {
    const res = await fetch("/api/game" + path + "?game=" + game, {
      method,
      headers: { "Content-Type": "application/json", "X-Init-Data": tg?.initData || "" },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) { const err = new Error(String(res.status)); err.status = res.status; throw err; }
    return res.json();
  }

  async function sendScore(game, score, note) {
    try {
      const data = await api("POST", "/score", { score }, game);
      if (game === MODES[mode].game) {
        lastBoard = data;
        best = data.best;
        draw();
      }
      note.textContent = myPlace(data);
    } catch (e) {
      if (e.status === 400) { note.textContent = "Не удалось сохранить результат."; return; }
      keepUnsaved({ game, score });
      note.textContent = "Сервер не ответил. Результат сохранён на телефоне и отправится при следующем открытии игры.";
    }
  }

  function readUnsaved() {
    try {
      const list = JSON.parse(storageGet(UNSAVED_KEY) || "[]");
      const games = Object.values(MODES).map((m) => m.game);
      return Array.isArray(list) ? list.filter((r) => r && games.includes(r.game) && r.score > 0) : [];
    } catch (e) { return []; }
  }

  function keepUnsaved(run) {
    storageSet(UNSAVED_KEY, JSON.stringify(readUnsaved().concat([run]).slice(-UNSAVED_MAX)));
  }

  // При открытии игры: партия, брошенная на паузе в прошлый раз, и всё неотправленное уходят на сервер по одному
  async function flushUnsaved() {
    try {
      const run = JSON.parse(storageGet(RUN_KEY) || "null");
      if (run && run.score > 0) keepUnsaved(run);
    } catch (e) { /* испорченная запись */ }
    storageSet(RUN_KEY, "");
    let list = readUnsaved();
    while (list.length) {
      const run = list[0];
      try {
        const data = await api("POST", "/score", { score: run.score }, run.game);
        if (run.game === MODES[mode].game) { best = data.best; lastBoard = null; if (!running) draw(); }
      } catch (e) {
        if (e.status !== 400) return;  // сервер спит или подпись старая: попробуем в следующий раз
      }
      list = readUnsaved().slice(1);
      storageSet(UNSAVED_KEY, JSON.stringify(list));
    }
  }

  function myPlace(data) {
    return data.best != null ? `Твой рекорд: ${data.best}, ${data.rank} место` : "Ты ещё не в таблице";
  }

  async function openBoard() {
    const modal = document.createElement("div");
    modal.className = "tt-modal";
    const sheet = document.createElement("div");
    sheet.className = "tt-sheet";
    const title = document.createElement("div");
    title.className = "tt-title";
    title.textContent = `Таблица лидеров · ${MODES[mode].name}`;
    const body = document.createElement("div");
    body.className = "tt-board";
    body.textContent = "Загружаю...";
    const close = document.createElement("button");
    close.className = "tt-btn";
    close.textContent = "Закрыть";
    sheet.append(title, body, close);
    modal.append(sheet);
    const remove = () => modal.remove();
    close.addEventListener("click", remove);
    modal.addEventListener("click", (e) => { if (e.target === modal) remove(); });
    document.body.append(modal);
    try {
      const data = lastBoard || await api("GET", "/top");
      if (data.best != null) best = data.best;
      renderBoard(body, data);
    } catch (e) {
      body.textContent = "Не удалось загрузить таблицу.";
    }
  }

  function renderBoard(board, data) {
    board.textContent = "";
    const head = document.createElement("div");
    head.className = "tt-board-head";
    head.textContent = myPlace(data);
    board.append(head);
    const list = document.createElement("ol");
    for (const row of data.top || []) {
      const li = document.createElement("li");
      if (row.me) li.className = "tt-me";
      const name = document.createElement("span");
      name.textContent = row.name;
      const sc = document.createElement("b");
      sc.textContent = row.score;
      li.append(name, sc);
      list.append(li);
    }
    if (!list.children.length) {
      const li = document.createElement("li");
      li.textContent = "Пока никто не играл";
      list.append(li);
    }
    board.append(list);
  }

  function intro() {
    stop();
    draw();
    show("Игра «Блоки»", `Собирай полные линии, игра бесконечная. Обычный: каждые 25 000 очков фигуры падают быстрее, после 100 000 скорость не растёт. ` +
      `Сложный: сразу быстро и дальше всё быстрее, рекорды отдельно. Каждые ${COLOR_STEP} очков блоки меняют цвет.`,
    [["Обычный", () => startMode("normal")], ["Сложный", () => startMode("hard")],
      ["Таблица лидеров", openBoard, true], ["Магазин", openShop, true]]);
  }

  function startMode(m) {
    if (m !== mode) {
      mode = m;
      best = null;  // рекорд и таблица у каждого режима свои
      lastBoard = null;
      storageSet(MODE_KEY, m);
    }
    start();
  }

  // --- Управление: кнопки под полем (удержание повторяет ход) и клавиатура ---
  const ACTIONS = {
    left: () => tryMove(-1, 0),
    right: () => tryMove(1, 0),
    rotate: () => tryMove(0, 0, 1),
    down: () => softDrop(),
  };
  const REPEATS = { left: true, right: true, down: true };
  const held = new Map();  // кнопка -> таймер повтора

  function press(btn) {
    const act = btn.dataset.act;
    btn.classList.add("on");
    if (!running) return;
    ACTIONS[act]();
    if (REPEATS[act]) {
      // stopped: повтор мог отпустить сам себя (фигура легла и releaseAll), тогда новый шаг не планируем,
      // иначе осиротевший таймер ронял бы фигуры вечно и с каждым разом быстрее
      const timer = { stopped: false };
      const again = () => {
        if (!running || timer.stopped) return;
        ACTIONS[act]();
        if (!timer.stopped) timer.id = setTimeout(again, REPEAT_EVERY);
      };
      timer.id = setTimeout(again, REPEAT_DELAY);
      held.set(btn, timer);
    }
  }

  function release(btn) {
    btn.classList.remove("on");
    const timer = held.get(btn);
    if (timer) { timer.stopped = true; clearTimeout(timer.id); }
    held.delete(btn);
  }

  function releaseAll() {
    for (const btn of [...held.keys()]) release(btn);
  }

  function onKey(e) {
    if (!running || !root.isConnected || root.offsetParent === null) return;
    const act = { ArrowLeft: "left", ArrowRight: "right", ArrowUp: "rotate", ArrowDown: "down" }[e.key];
    if (!act) return;
    e.preventDefault();
    ACTIONS[act]();
  }

  // --- Магазин скинов ---
  function storageGet(key) {
    try { return localStorage.getItem(key); } catch (e) { return null; }
  }
  function storageSet(key, value) {
    try { localStorage.setItem(key, value); return true; } catch (e) { return false; }
  }

  function loadSkin() {
    const saved = storageGet(SKIN_KEY);
    return SKINS[saved] ? saved : "classic";
  }

  function loadCustomImage() {
    const data = storageGet(IMAGE_KEY);
    if (!data) return;
    const img = new Image();
    img.onload = () => { customImage = img; tiles.clear(); draw(); };
    img.src = data;
  }

  function useSkin(name) {
    skin = name;
    storageSet(SKIN_KEY, name);
    shownColor = S ? colorIndex() : 0;
    draw();
  }

  // Своя картинка: квадрат из середины фото, уменьшенный до 256×256 (чётко даже на крупной клетке и плотном экране),
  // хранится только на этом телефоне
  function pickImage(done) {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = "image/*";
    input.addEventListener("change", () => {
      const file = input.files && input.files[0];
      if (!file) return;
      const url = URL.createObjectURL(file);
      const img = new Image();
      img.onload = () => {
        const side = Math.min(img.width, img.height), out = document.createElement("canvas");
        out.width = out.height = IMAGE_SIZE;
        const oc = out.getContext("2d");
        oc.imageSmoothingQuality = "high";
        oc.drawImage(img, (img.width - side) / 2, (img.height - side) / 2, side, side, 0, 0, IMAGE_SIZE, IMAGE_SIZE);
        URL.revokeObjectURL(url);
        const data = out.toDataURL("image/jpeg", 0.9);
        const small = new Image();
        small.onload = () => {
          customImage = small;
          tiles.clear();
          const saved = storageSet(IMAGE_KEY, data);
          done(saved ? "" : "Картинка выбрана, но телефон не дал её сохранить: после перезапуска выбери снова.");
        };
        small.src = data;
      };
      img.onerror = () => { URL.revokeObjectURL(url); done("Не получилось открыть эту картинку."); };
      img.src = url;
    });
    input.click();
  }

  async function shopApi(method, path, body) {
    const res = await fetch("/api/blocks" + path, {
      method,
      headers: { "Content-Type": "application/json", "X-Init-Data": tg?.initData || "" },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (res.status === 402) throw new Error("not_enough");
    if (!res.ok) throw new Error(String(res.status));
    return res.json();
  }

  function preview(name) {
    const c = document.createElement("canvas"), size = 16, cols = SKINS[name].colors;
    const x = sizeCanvas(c, size * 3, size * 2);
    c.className = "tt-skin-preview";
    [[0, 1], [1, 1], [2, 1], [1, 0]].forEach(([cx, cy], i) => block(x, cx * size, cy * size, size, cols[(i % 2) && cols.length > 1 ? 1 : 0], 1, name));
    return c;
  }

  async function openShop() {
    pause();
    const modal = document.createElement("div");
    modal.className = "tt-modal";
    const sheet = document.createElement("div");
    sheet.className = "tt-sheet";
    const title = document.createElement("div");
    title.className = "tt-title";
    title.textContent = "Магазин скинов";
    const head = document.createElement("div");
    head.className = "tt-board-head";
    head.textContent = "Загружаю...";
    const list = document.createElement("div");
    list.className = "tt-shop";
    const msg = document.createElement("div");
    msg.className = "tt-note tt-shop-msg";
    const close = document.createElement("button");
    close.className = "tt-btn";
    close.textContent = "Закрыть";
    sheet.append(title, head, list, msg, close);
    modal.append(sheet);
    const remove = () => modal.remove();
    close.addEventListener("click", remove);
    modal.addEventListener("click", (e) => { if (e.target === modal) remove(); });
    document.body.append(modal);

    const render = () => {
      head.textContent = shop.free ? "Тебе как владельцу все скины бесплатно" : `На счёте ${shop.balance} очков (копятся за все партии)`;
      list.textContent = "";
      for (const name of Object.keys(SKINS)) {
        const owned = shop.owned.includes(name), price = shop.prices[name] ?? 0;
        const row = document.createElement("div");
        row.className = "tt-skin" + (name === skin ? " tt-skin-on" : "");
        const info = document.createElement("div");
        info.className = "tt-skin-info";
        const nm = document.createElement("b");
        nm.textContent = SKINS[name].name;
        const sub = document.createElement("span");
        sub.textContent = (name === "custom" ? "Твоя картинка, " : "") + (owned ? "куплен" : `${price} очков`);
        info.append(nm, sub);
        const btn = document.createElement("button");
        btn.className = "tt-btn tt-small" + (owned ? " tt-secondary" : "");
        if (!owned) {
          btn.textContent = "Купить";
          btn.disabled = shop.balance < price;
          btn.addEventListener("click", () => buy(name, btn));
        } else if (name === "custom") {
          btn.textContent = customImage ? "Выбрать" : "Фото";
          btn.addEventListener("click", () => {
            if (customImage && name !== skin) { useSkin(name); render(); return; }
            pickImage((err) => { msg.textContent = err; useSkin("custom"); render(); });
          });
          if (customImage && name === skin) {
            btn.textContent = "Другое фото";
          }
        } else {
          btn.textContent = name === skin ? "Выбран" : "Выбрать";
          btn.disabled = name === skin;
          btn.addEventListener("click", () => { useSkin(name); render(); });
        }
        row.append(preview(name), info, btn);
        list.append(row);
      }
    };

    const buy = async (name, btn) => {
      btn.disabled = true;
      msg.textContent = "";
      try {
        shop = await shopApi("POST", "/buy", { skin: name });
        if (name === "custom") pickImage((err) => { msg.textContent = err; useSkin("custom"); render(); });
        else useSkin(name);
        haptic("success");
      } catch (e) {
        msg.textContent = e.message === "not_enough" ? "Не хватает очков." : "Не удалось купить, попробуй ещё раз.";
      }
      render();
    };

    try {
      shop = await shopApi("GET", "/shop");
      render();
    } catch (e) {
      head.textContent = "Не удалось открыть магазин, попробуй ещё раз.";
    }
  }

  const CSS = `
  .tt { user-select: none; -webkit-user-select: none; }
  .tt-play { display: flex; gap: 10px; justify-content: space-between; align-items: flex-start; }
  .tt-view { position: relative; border-radius: 12px; overflow: hidden; background: #1f2533; box-shadow: 0 0 0 2px #59627a; touch-action: none; line-height: 0; }
  .tt-view canvas { display: block; }
  .tt-side { display: flex; flex-direction: column; gap: 10px; width: 64px; font-size: 12px; color: var(--hint); }
  .tt-side b { display: block; color: var(--text); font-size: 17px; margin-top: 2px; }
  .tt-side b.tt-mode { font-size: 13px; }
  .tt-next { width: 64px; height: 64px; background: var(--card); border-radius: 10px; display: block; }
  .tt-over { position: absolute; inset: 0; background: rgba(0,0,0,0.72); display: flex; align-items: center; justify-content: center; padding: 10px; line-height: normal; }
  .tt-box { text-align: center; max-width: 300px; color: #fff; }
  .tt-title { font-size: 19px; font-weight: 700; margin-bottom: 4px; }
  .tt-text { font-size: 13px; line-height: 1.3; color: #e5e5ea; margin-bottom: 10px; }
  .tt-buttons { display: flex; gap: 8px; justify-content: center; flex-wrap: wrap; }
  .tt-btn { border: none; border-radius: 12px; padding: 10px 18px; font-size: 15px; font-weight: 600; background: var(--accent); color: #fff; font-family: inherit; }
  .tt-btn.tt-secondary { background: var(--card-pressed); }
  .tt-btn:disabled { opacity: 0.6; }
  .tt-pad { display: flex; gap: 10px; max-width: 420px; margin: 12px auto 0; }
  .tt-arrow { flex: 1; height: 60px; border: none; border-radius: 16px; background: var(--card); color: var(--text); font-size: 26px; touch-action: none; -webkit-tap-highlight-color: transparent; font-family: inherit; }
  .tt-arrow.on { background: var(--accent); color: #fff; }
  .tt-note { font-size: 13px; color: #ffd60a; margin: -4px 0 10px; }
  .tt-modal { position: fixed; inset: 0; z-index: 1000; background: rgba(0, 0, 0, 0.6); display: flex; align-items: center; justify-content: center; padding: 16px; }
  .tt-sheet { width: 100%; max-width: 420px; max-height: 85vh; overflow-y: auto; background: var(--bg); border: 1px solid var(--border); border-radius: 20px; padding: 18px 16px; text-align: center; }
  .tt-sheet .tt-btn { margin-top: 12px; width: 100%; }
  .tt-board { margin-top: 8px; font-size: 14px; }
  .tt-board ol { list-style: none; padding: 0; margin: 6px 0 0; counter-reset: place; }
  .tt-board li { display: flex; gap: 8px; padding: 6px 10px; border-radius: 10px; background: var(--card); margin-bottom: 4px; counter-increment: place; }
  .tt-board li::before { content: counter(place) "."; color: var(--hint); min-width: 20px; text-align: left; }
  .tt-board li span { flex: 1; text-align: left; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .tt-board li.tt-me { background: rgba(10, 132, 255, 0.3); }
  .tt-shop-btn { border: none; border-radius: 10px; background: var(--card); color: var(--text); padding: 6px 0; font-size: 20px; font-family: inherit; display: flex; flex-direction: column; align-items: center; gap: 2px; }
  .tt-shop-btn span { font-size: 11px; color: var(--hint); }
  .tt-shop { margin-top: 10px; display: flex; flex-direction: column; gap: 6px; }
  .tt-skin { display: flex; align-items: center; gap: 10px; padding: 8px 10px; border-radius: 12px; background: var(--card); text-align: left; }
  .tt-skin.tt-skin-on { box-shadow: inset 0 0 0 2px var(--accent); }
  .tt-skin-preview { width: 48px; height: 32px; flex: none; }
  .tt-skin-info { flex: 1; display: flex; flex-direction: column; font-size: 13px; color: var(--hint); min-width: 0; }
  .tt-skin-info b { color: var(--text); font-size: 15px; }
  .tt-sheet .tt-btn.tt-small { padding: 7px 12px; font-size: 14px; width: auto; margin-top: 0; flex: none; }
  .tt-shop-msg { margin: 8px 0 0; min-height: 0; }
  `;

  function build(container) {
    root = container;
    const style = document.createElement("style");
    style.textContent = CSS;
    document.head.append(style);
    root.innerHTML = `<div class="tt">
      <div class="tt-play">
        <div class="tt-view"><canvas></canvas><div class="tt-over"></div></div>
        <div class="tt-side">
          <div>Следующая<canvas class="tt-next" width="64" height="64"></canvas></div>
          <div>Счёт<b class="tt-score">0</b></div>
          <div>Линии<b class="tt-lines">0</b></div>
          <div>Рекорд<b class="tt-best">0</b></div>
          <div>Режим<b class="tt-mode"></b></div>
          <button class="tt-shop-btn" aria-label="Магазин скинов">🛍️<span>Магазин</span></button>
        </div>
      </div>
      <div class="tt-pad">
        <button class="tt-arrow" data-act="left" aria-label="Влево">◀</button>
        <button class="tt-arrow" data-act="rotate" aria-label="Повернуть">⟳</button>
        <button class="tt-arrow" data-act="down" aria-label="Вниз">▼</button>
        <button class="tt-arrow" data-act="right" aria-label="Вправо">▶</button>
      </div>
    </div>`;
    canvas = root.querySelector(".tt-view canvas");
    ctx = canvas.getContext("2d");
    nextCanvas = root.querySelector(".tt-next");
    nextCtx = sizeCanvas(nextCanvas, 64, 64);
    over = root.querySelector(".tt-over");
    hud = { score: root.querySelector(".tt-score"), lines: root.querySelector(".tt-lines"),
      best: root.querySelector(".tt-best"), mode: root.querySelector(".tt-mode") };
    hud.mode.textContent = MODES[mode].name;
    for (const btn of root.querySelectorAll(".tt-arrow")) {
      btn.addEventListener("pointerdown", (e) => { e.preventDefault(); press(btn); });
      for (const ev of ["pointerup", "pointercancel", "pointerleave"]) {
        btn.addEventListener(ev, (e) => { e.preventDefault(); release(btn); });
      }
      btn.addEventListener("contextmenu", (e) => e.preventDefault());
    }
    root.querySelector(".tt-shop-btn").addEventListener("click", openShop);
    document.addEventListener("keydown", onKey);
    loadCustomImage();
    flushUnsaved();
    document.addEventListener("visibilitychange", () => { if (document.hidden) pause(); });
    window.addEventListener("resize", () => { if (root.offsetParent !== null) fit(); });
    newGame();
    fit();
    intro();
  }

  window.TetrisGame = {
    // вызывается при каждом открытии карточки; при повторном открытии игра стоит на паузе
    open(container) {
      if (!root) build(container);
      else fit();
    },
    // для проверки игры из консоли и автотестов
    debug: {
      state: () => S,
      step: (dt) => { if (running) { update(dt); if (running) draw(); } return running; },
      act: (name) => ACTIONS[name](),
      start,
      startMode,
      mode: () => mode,
      fallInterval: () => fallInterval(),
    },
  };
})();
