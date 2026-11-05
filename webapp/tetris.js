// Игра «Блоки» (падающие фигуры) для раздела «Другое»: бесконечный, управление кнопками под полем. Файл грузится только при открытии
// карточки, на скорость открытия расписания не влияет. Всё рисуется кодом, звука нет.
(() => {
  "use strict";
  const COLS = 10, ROWS = 20;
  const COLOR_STEP = 1000;  // каждые столько очков все блоки меняют цвет
  const LINE_POINTS = [0, 100, 300, 500, 800];
  const LOCK_DELAY = 0.5, LOCK_RESETS = 15;  // сколько фигура может лежать на опоре, пока её двигают
  const REPEAT_DELAY = 170, REPEAT_EVERY = 55;  // удержание кнопки: пауза перед повтором и шаг повтора, мс
  const tg = window.Telegram?.WebApp;

  // Цвета всех блоков по очереди: [основной, светлая грань, тёмная грань]
  const PALETTE = [
    ["#0a84ff", "#5eb0ff", "#0057b8"],
    ["#ff9f0a", "#ffc266", "#b86e00"],
    ["#30d158", "#7ee69a", "#1d8f3a"],
    ["#bf5af2", "#d999f7", "#8a2fb8"],
    ["#ff375f", "#ff8aa1", "#b81f3f"],
    ["#64d2ff", "#a6e6ff", "#2a94bf"],
    ["#ffd60a", "#ffe766", "#b89800"],
  ];
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
  let S = null, running = false, rafId = 0, last = 0, best = null;

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
    if (!fits(S.piece)) gameOver();
  }

  function cellsOf(p) {
    return rotated(SHAPES[p.type], SIZE[p.type], p.rot).map(([x, y]) => [p.x + x, p.y + y]);
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
        const wasGrounded = grounded();
        S.piece = moved;
        // фигуру на опоре можно ещё подвинуть: таймер фиксации начинается заново, но не бесконечно
        if (dy === 0 && wasGrounded && S.lockResets < LOCK_RESETS) {
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
    return Math.floor(score / COLOR_STEP) % PALETTE.length;
  }

  let shownColor = 0;
  function afterScore() {
    const c = colorIndex();
    if (c !== shownColor) {
      shownColor = c;
      S.flash = 0.35;  // короткая вспышка, чтобы смена цвета была заметна
    }
  }

  function fallInterval() {
    return Math.max(0.08, 0.8 * Math.pow(0.85, level()));
  }

  function update(dt) {
    if (S.flash > 0) S.flash = Math.max(0, S.flash - dt);
    if (grounded()) {
      S.lockTimer += dt;
      if (S.lockTimer >= LOCK_DELAY) lock();
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

  // --- Рисование ---
  function block(c, x, y, size, colors, alpha = 1) {
    const [main, light, dark] = colors;
    c.globalAlpha = alpha;
    c.fillStyle = main;
    c.fillRect(x, y, size, size);
    const b = Math.max(2, Math.round(size / 7));
    c.fillStyle = light;
    c.fillRect(x, y, size, b);
    c.fillRect(x, y, b, size);
    c.fillStyle = dark;
    c.fillRect(x, y + size - b, size, b);
    c.fillRect(x + size - b, y, b, size);
    c.globalAlpha = 1;
  }

  function draw() {
    if (!S || !ctx) return;
    const colors = PALETTE[colorIndex()];
    ctx.fillStyle = "#0b0b0f";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.strokeStyle = "rgba(255,255,255,0.05)";
    ctx.lineWidth = 1;
    for (let x = 1; x < COLS; x++) {
      ctx.beginPath(); ctx.moveTo(x * cell + 0.5, 0); ctx.lineTo(x * cell + 0.5, ROWS * cell); ctx.stroke();
    }
    for (let y = 1; y < ROWS; y++) {
      ctx.beginPath(); ctx.moveTo(0, y * cell + 0.5); ctx.lineTo(COLS * cell, y * cell + 0.5); ctx.stroke();
    }
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
      ctx.fillRect(0, 0, canvas.width, canvas.height);
    }
    drawNext(colors);
    hud.score.textContent = S.score;
    hud.lines.textContent = S.lines;
    hud.best.textContent = best != null ? Math.max(best, S.score) : S.score;
  }

  function drawNext(colors) {
    const n = nextCanvas.width, size = Math.floor(n / 4);
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
    cell = Math.max(12, Math.floor(Math.min((width - 80) / COLS, height / ROWS)));
    canvas.width = COLS * cell;
    canvas.height = ROWS * cell;
    draw();
  }

  // --- Цикл и экраны ---
  function loop(ts) {
    if (!running) return;
    if (!root.isConnected || root.offsetParent === null || document.hidden) { pause(); return; }
    const dt = last ? Math.min(0.05, (ts - last) / 1000) : 0;
    last = ts;
    update(dt);
    if (running) draw();
    if (running) rafId = requestAnimationFrame(loop);
  }

  function resume() {
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
    show("Пауза", "Игра остановлена.", [["Продолжить", resume]]);
  }

  function start() {
    newGame();
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
    draw();
    haptic("heavy");
    const score = S.score;
    const note = document.createElement("div");
    note.className = "tt-note";
    note.textContent = "Сохраняю результат...";
    lastBoard = null;
    show("Игра окончена", `Счёт: ${score}, линий: ${S.lines}.`,
      [["Ещё раз", start], ["Таблица лидеров", openBoard, true]], note);
    sendScore(score, note);
  }

  async function api(method, path, body) {
    const res = await fetch("/api/game" + path + "?game=tetris", {
      method,
      headers: { "Content-Type": "application/json", "X-Init-Data": tg?.initData || "" },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) throw new Error(String(res.status));
    return res.json();
  }

  async function sendScore(score, note) {
    try {
      lastBoard = await api("POST", "/score", { score });
      best = lastBoard.best;
      note.textContent = myPlace(lastBoard);
      draw();
    } catch (e) {
      note.textContent = "Не удалось сохранить результат.";
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
    title.textContent = "Таблица лидеров";
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
    show("Блоки", `Собирай полные линии, игра бесконечная и постепенно ускоряется. ` +
      `Каждые ${COLOR_STEP} очков блоки меняют цвет.`,
    [["Играть", start], ["Таблица лидеров", openBoard, true]]);
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
      const timer = { id: setTimeout(function again() { if (!running) return; ACTIONS[act](); timer.id = setTimeout(again, REPEAT_EVERY); }, REPEAT_DELAY) };
      held.set(btn, timer);
    }
  }

  function release(btn) {
    btn.classList.remove("on");
    const timer = held.get(btn);
    if (timer) clearTimeout(timer.id);
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

  const CSS = `
  .tt { user-select: none; -webkit-user-select: none; }
  .tt-play { display: flex; gap: 10px; justify-content: center; align-items: flex-start; }
  .tt-view { position: relative; border-radius: 12px; overflow: hidden; background: #0b0b0f; touch-action: none; line-height: 0; }
  .tt-view canvas { display: block; }
  .tt-side { display: flex; flex-direction: column; gap: 10px; width: 64px; font-size: 12px; color: var(--hint); }
  .tt-side b { display: block; color: var(--text); font-size: 17px; margin-top: 2px; }
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
    nextCtx = nextCanvas.getContext("2d");
    over = root.querySelector(".tt-over");
    hud = { score: root.querySelector(".tt-score"), lines: root.querySelector(".tt-lines"),
      best: root.querySelector(".tt-best") };
    for (const btn of root.querySelectorAll(".tt-arrow")) {
      btn.addEventListener("pointerdown", (e) => { e.preventDefault(); press(btn); });
      for (const ev of ["pointerup", "pointercancel", "pointerleave"]) {
        btn.addEventListener(ev, (e) => { e.preventDefault(); release(btn); });
      }
      btn.addEventListener("contextmenu", (e) => e.preventDefault());
    }
    document.addEventListener("keydown", onKey);
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
    },
  };
})();
