// Мини-игра «Сессия» для раздела «Другое». Файл грузится только при открытии карточки игры,
// поэтому на скорость открытия расписания не влияет. Всё рисуется кодом, звука нет.
(() => {
  "use strict";
  const W = 200, H = 112, GROUND = 96, PLAYER_X = 26;
  const GRAVITY = 1100, JUMP_SPEED = 330, JUMP_CUT = 150;
  const LIVES = 3, NEED = 5, VED_TIME = 60, CHASE_TIME = 18, CATCH_TIME = 14, DEFENSE_TIME = 30;
  const tg = window.Telegram?.WebApp;

  // --- Пиксельные спрайты: строки символов, "." прозрачно ---
  function sprite(rows, colors) {
    const c = document.createElement("canvas");
    c.width = Math.max(...rows.map((r) => r.length));
    c.height = rows.length;
    const x = c.getContext("2d");
    rows.forEach((row, y) => [...row].forEach((ch, i) => {
      if (colors[ch]) { x.fillStyle = colors[ch]; x.fillRect(i, y, 1, 1); }
    }));
    return c;
  }

  const HEAD = ["....HHHH....", "...HHHHHH...", "...HHSSSS...", "...HSSSES...", "....SSSS...."];
  const RUN1 = [...HEAD, "....TTTT....", "...TTATTT...", "..STTATTTS..", "..S.TATT.S..", "....TTTT....",
    "....PPPP....", "...PP..PP...", "..PP....PP..", "..PP....PP..", ".BB......BB."];
  const RUN2 = [...HEAD, "....TTTT....", "...TTATTT...", "...STATTS...", "...STATTS...", "....TTTT....",
    "....PPPP....", ".....PP.....", ".....PP.....", ".....PP.....", ".....BBB...."];
  const JUMP = [...HEAD, "..S.TTTT.S..", "..STTATTTS..", "....TATT....", "....TATT....", "....TTTT....",
    "....PPPP....", "...PPPPPP...", "...PP..PP...", "...BB..BB..."];
  const DUCK = ["......HHHH..", ".....HHHSSS.", ".....HSSSES.", "..TTTTTTSS..", ".TTTAATTT...",
    ".STTTTTTTS..", "..PPPPPP....", ".PP....PP...", ".BB....BBB.."];
  const SKIN = { S: "#f1c27d", E: "#1b1b1b" };

  function person(colors) {
    const pal = { ...SKIN, ...colors };
    return { run: [sprite(RUN1, pal), sprite(RUN2, pal)], jump: sprite(JUMP, pal), duck: sprite(DUCK, pal) };
  }
  // Персонажи без фамилий: студент (игрок), халат, спортивный костюм, офисный костюм
  const LOOKS = {
    student: person({ H: "#4a2f1b", T: "#2f6fd6", A: "#2f6fd6", P: "#2b2d42", B: "#eeeeee" }),
    coat: person({ H: "#2b1d14", T: "#f4f4f4", A: "#58b9e8", P: "#58a79c", B: "#ffffff" }),
    sport: person({ H: "#1d1d1d", T: "#d7263d", A: "#ffffff", P: "#d7263d", B: "#f4f4f4" }),
    office: person({ H: "#7a5230", T: "#353a47", A: "#c0392b", P: "#353a47", B: "#111111" }),
    green: person({ H: "#222222", T: "#3fa34d", A: "#3fa34d", P: "#44475a", B: "#dddddd" }),
    orange: person({ H: "#8a5a2b", T: "#e67e22", A: "#e67e22", P: "#2b2d42", B: "#dddddd" }),
  };
  const CHASERS = { 1: ["coat"], 2: ["sport"], 3: ["office"], 4: ["coat", "sport", "office"] };

  const IMG = {
    chair: sprite(["........BB", "........BB", "........BB", "........BB", "BBBBBBBBBB", "DDDDDDDDDD",
      ".B......B.", ".B......B.", ".B......B.", ".B......B."], { B: "#8b5a2b", D: "#6b4423" }),
    puddle: sprite(["...LLLLLLLLLL...", ".WWLLLLLLLLLLWW.", "WWWWWWWWWWWWWWWW"], { W: "#3a7bd5", L: "#8fc1ff" }),
    ball: sprite(["..OOOO..", ".OODOOO.", "OOODOOOO", "ODDDDDDO", "OOODOOOO", "OOODOOOO", ".OODOOO.", "..OOOO.."],
      { O: "#f39c12", D: "#7a3e00" }),
    zachet: sprite(["WWWWWWW.", "WGGGGGWW", "WWWWWWWW", "WGGGGGGW", "WWWWWWWW", "WGGGGWWW", "WWWWWRRW",
      "WWWWRWWR", "WWWWWRRW", "WWWWWWWW"], { W: "#ffffff", G: "#9aa0a6", R: "#e53935" }),
    diploma: sprite(["BBBBBBB.", "BYYYYYBB", "BBBBBBBB", "BBYYYYBB", "BBYBBYBB", "BBYYYYBB", "BBBBBBBB",
      "BYYYYYYB", "BBBBBBBB", "BBBBBBBB"], { B: "#1e4fa8", Y: "#f5c518" }),
    question: sprite(["..RRR..", ".RR.RR.", "....RR.", "...RR..", "..RR...", "..RR...", ".......", "..RR...", "..RR..."],
      { R: "#e53935" }),
  };

  // --- Препятствия: hit - рамка столкновения внутри картинки ---
  const OBSTACLES = {
    chair: { img: IMG.chair, lift: 0, hit: [1, 1, 8, 9] },
    puddle: { img: IMG.puddle, lift: 0, hit: [2, 1, 12, 2] },
    ball: { img: IMG.ball, lift: 11, hit: [0, 1, 8, 7] },
    crowd: { img: null, w: 19, h: 15, lift: 0, hit: [3, 2, 14, 13] },
  };

  // --- Этапы игры ---
  const STAGES = {
    chase: { theme: "street", label: "Погоня" },
    ved2: { theme: "corridor", label: "Вторая ведомость", items: "zachet", speedK: 1.08, gap: [0.9, 1.5] },
    ved3: { theme: "corridor", label: "Третья ведомость", items: "zachet", speedK: 1.25, gap: [0.8, 1.2] },
    catch: { theme: "street", label: "Погоня" },
    diploma: { theme: "corridor", label: "Листы диплома", items: "diploma", speedK: 1.1, gap: [0.85, 1.4] },
    diploma2: { theme: "corridor", label: "Пересдача", items: "diploma", speedK: 1.25, gap: [0.8, 1.2] },
    defense: { theme: "hall", label: "Защита диплома" },
  };

  let root, canvas, ctx, over, boardEl, hud = {};
  let S = null; // состояние партии
  let running = false, rafId = 0, last = 0;
  const input = { duckHeld: false, jumpHeld: false, buffer: 0 };

  const rand = (a, b) => a + Math.random() * (b - a);
  const pick = (arr) => arr[Math.floor(Math.random() * arr.length)];

  function newGame() {
    S = { course: 1, lives: LIVES, score: 0, total: 0 };
    startStage("chase");
  }

  function startStage(type) {
    const st = STAGES[type];
    Object.assign(S, {
      type, st, t: 0, scroll: S.scroll || 0, objects: [], collected: 0, invuln: 0,
      nextObstacle: 1.2, nextItem: 1.5, itemsLeft: 8, nextQuestion: 1,
      speed: (82 + 10 * (S.course - 1)) * (st.speedK || 1),
      player: { y: 0, vy: 0, anim: 0 },
    });
    input.buffer = 0;
    updateHud();
  }

  function stageLabel() {
    return `${S.course} курс · ${S.st.label}`;
  }

  // --- Управление: тап - прыжок, свайп вниз - пригнуться ---
  function jump() {
    const p = S.player;
    if (p.y === 0) { p.vy = JUMP_SPEED; p.y = 0.01; } else input.buffer = 0.12;
  }
  function releaseJump() {
    if (S && S.player.vy > JUMP_CUT) S.player.vy = JUMP_CUT;
  }

  let touch = null;
  function onTouchStart(e) {
    if (!running) return;
    e.preventDefault();
    const t = e.changedTouches[0];
    // прыжок чуть откладывается: если палец сразу пошёл вниз, это свайп, а не тап
    touch = { id: t.identifier, y: t.clientY, swiped: false, timer: setTimeout(() => { if (touch && !touch.swiped) { touch.jumped = true; jump(); } }, 70) };
  }
  function onTouchMove(e) {
    if (!touch) return;
    e.preventDefault();
    const t = [...e.changedTouches].find((x) => x.identifier === touch.id);
    if (t && !touch.swiped && t.clientY - touch.y > 22) {
      touch.swiped = true;
      clearTimeout(touch.timer);
      input.duckHeld = true;
      input.duckUntil = performance.now() + 450;
    }
  }
  function onTouchEnd(e) {
    if (!touch) return;
    e.preventDefault();
    clearTimeout(touch.timer);
    if (!touch.swiped && !touch.jumped && running) jump();
    if (!touch.swiped) releaseJump();
    else if (performance.now() < input.duckUntil) { touch = null; return; } // короткий свайп: пригнуться ещё ненадолго
    touch = null;
    input.duckHeld = false;
  }
  function onKey(e) {
    if (!running || !root || root.offsetParent === null) return;
    const down = e.type === "keydown";
    if (["Space", "ArrowUp", "KeyW"].includes(e.code)) {
      e.preventDefault();
      if (down && !e.repeat) jump();
      if (!down) releaseJump();
    } else if (["ArrowDown", "KeyS"].includes(e.code)) {
      e.preventDefault();
      input.duckHeld = down;
      input.duckUntil = 0;
    }
  }

  // --- Обновление мира ---
  function spawnObstacle() {
    // и препятствие не ставим вплотную за зачётом
    if (S.objects.some((o) => o.item && o.x > W - 50)) { S.nextObstacle = 0.2; return; }
    const kinds = ["chair", "puddle", "crowd"];
    if (S.course >= 2 || S.type !== "chase") kinds.push("ball");
    if (S.type === "ved3" || S.type === "diploma2") kinds.push("ball", "chair");
    const kind = pick(kinds);
    const o = OBSTACLES[kind];
    const w = o.img ? o.img.width : o.w, h = o.img ? o.img.height : o.h;
    S.objects.push({ kind, x: W + 4, w, h, bottom: GROUND - o.lift, hit: o.hit,
      people: kind === "crowd" ? [pick(["green", "orange"]), pick(["green", "orange", "student"])] : null });
  }

  function spawnItem() {
    // не кладём зачёт прямо на препятствие
    if (S.objects.some((o) => !o.item && o.x > W - 40)) { S.nextItem = 0.25; return; }
    const high = Math.random() < 0.5;
    const img = IMG[S.st.items];
    S.objects.push({ item: true, img, x: W + 4, w: img.width, h: img.height, bottom: GROUND - (high ? 30 : 6),
      hit: [-2, -2, img.width + 4, img.height + 4], phase: Math.random() * 6 });
    S.itemsLeft--;
  }

  function spawnQuestion() {
    const high = Math.random() < 0.5;
    S.objects.push({ kind: "question", img: IMG.question, x: W - 56, w: 7, h: 9, bottom: high ? GROUND - 11 : GROUND - 1,
      hit: [1, 0, 5, 9], speed: rand(115, 150) });
  }

  function playerBox() {
    const p = S.player;
    const ducking = input.duckHeld && p.y === 0;
    return ducking ? { x: PLAYER_X + 1, y: GROUND - 8, w: 10, h: 8 }
      : { x: PLAYER_X + 3, y: GROUND - p.y - 14, w: 6, h: 14 };
  }

  function hits(a, o) {
    const x = o.x + o.hit[0], y = o.bottom - o.h + o.hit[1];
    return a.x < x + o.hit[2] && a.x + a.w > x && a.y < y + o.hit[3] && a.y + a.h > y;
  }

  function update(dt) {
    const p = S.player;
    S.t += dt;
    if (input.duckUntil && performance.now() > input.duckUntil && !touch) { input.duckHeld = false; input.duckUntil = 0; }

    const moving = S.type !== "defense";
    const speed = moving ? S.speed : 30;
    S.scroll += speed * dt;
    S.score += (moving ? S.speed * 0.1 : 12) * dt;
    p.anim += dt * speed / 9;

    // прыжок и приземление; свайп вниз в воздухе ускоряет падение
    if (p.y > 0 || p.vy > 0) {
      if (input.duckHeld && p.vy > -300) p.vy = -300;
      p.vy -= GRAVITY * dt;
      p.y += p.vy * dt;
      if (p.y <= 0) {
        p.y = 0; p.vy = 0;
        if (input.buffer > 0) { input.buffer = 0; jump(); }
      }
    }
    input.buffer = Math.max(0, input.buffer - dt);
    S.invuln = Math.max(0, S.invuln - dt);

    // появление препятствий, зачётов и вопросов
    const obstaclesOn = S.type === "chase" ? S.t < CHASE_TIME - 2 : S.type === "catch" ? S.t < CATCH_TIME * 0.6 : S.type !== "defense";
    if (obstaclesOn && (S.nextObstacle -= dt) <= 0) {
      spawnObstacle();
      const gap = S.st.gap || [1.0, 1.9];
      if (S.nextObstacle <= 0) S.nextObstacle = rand(gap[0], gap[1]);
    }
    if (S.st.items && S.itemsLeft > 0 && (S.nextItem -= dt) <= 0) {
      spawnItem();
      if (S.nextItem <= 0) S.nextItem = rand(5, 7);
    }
    if (S.type === "defense" && S.t < DEFENSE_TIME - 1.5 && (S.nextQuestion -= dt) <= 0) {
      spawnQuestion();
      S.nextQuestion = rand(0.8, 1.35) * (S.t > 15 ? 0.85 : 1);
    }

    const box = playerBox();
    for (const o of S.objects) {
      o.x -= (o.speed || speed) * dt;
      if (o.gone || !hits(box, o)) continue;
      if (o.item) {
        o.gone = true;
        S.collected++;
        S.score += 50;
        haptic("light");
      } else if (S.invuln <= 0) {
        o.gone = true;
        S.lastHit = o.kind;
        S.lives--;
        S.invuln = 1.4;
        haptic("heavy");
        if (S.lives <= 0) return finish(false, "Жизни кончились", "Слишком много падений по дороге к диплому.");
      }
    }
    S.objects = S.objects.filter((o) => o.x > -30 && !(o.gone && o.item));
    checkStageEnd();
  }

  function checkStageEnd() {
    const c = S.course;
    if (S.type === "chase" && S.t >= CHASE_TIME) {
      return between("Не догнал!", "Догнать преподавателя невозможно. Тебя отправляют на вторую ведомость: " +
        `собери ${NEED} зачётов за ${VED_TIME} секунд.`, "ved2");
    }
    if (S.type === "catch" && S.t >= CATCH_TIME) {
      S.score += 300;
      return between("Догнал всех!", `Остались листы диплома: собери ${NEED} за ${VED_TIME} секунд.`, "diploma");
    }
    if (S.type === "defense" && S.t >= DEFENSE_TIME) {
      S.score += 1000;
      return finish(true, "Диплом получен!", "Ты прошёл все четыре курса и защитился.");
    }
    if (!S.st.items) return;
    const done = S.collected >= NEED;
    const out = S.t >= VED_TIME || (S.itemsLeft === 0 && !S.objects.some((o) => o.item) && !done);
    if (done) {
      S.score += 300;
      if (S.type === "diploma" || S.type === "diploma2") {
        return between("Листы собраны!", `Защита: ${DEFENSE_TIME} секунд уворачивайся от вопросов комиссии. ` +
          "Низкий вопрос перепрыгни, высокий пропусти, пригнувшись.", "defense");
      }
      S.course = c + 1;
      return between(`Переводишься на ${c + 1} курс!`, c + 1 === 4
        ? "Последний курс: теперь ты догонишь всех, кто убегал." : "Новый курс, новая погоня.", c + 1 === 4 ? "catch" : "chase");
    }
    if (!out) return;
    if (S.type === "ved2") return between("Не успел", "Третья ведомость: всё то же самое, но быстрее и препятствий больше.", "ved3");
    if (S.type === "diploma") return between("Не успел", "Пересдача: всё то же самое, но быстрее и препятствий больше.", "diploma2");
    return finish(false, "Отчислен", S.type === "ved3" ? "Третья ведомость не закрыта." : "Пересдача не сдана.");
  }

  function haptic(kind) {
    try { tg?.HapticFeedback?.impactOccurred(kind); } catch (e) { /* не везде есть */ }
  }

  // --- Рисование ---
  function drawStreet(s) {
    ctx.fillStyle = "#7fbfff"; ctx.fillRect(0, 0, W, GROUND);
    ctx.fillStyle = "#a9d4ff"; ctx.fillRect(0, 36, W, GROUND - 36);
    ctx.fillStyle = "#ffffff";
    for (let i = 0; i < 4; i++) {
      const x = ((i * 90 - s * 0.08) % 360 + 360) % 360 - 50;
      ctx.fillRect(x, 10 + (i % 2) * 10, 26, 5); ctx.fillRect(x + 6, 6 + (i % 2) * 10, 14, 4);
    }
    // корпуса вуза
    for (let i = 0; i < 6; i++) {
      const x = ((i * 64 - s * 0.25) % 384 + 384) % 384 - 64;
      const h = [40, 54, 34, 48, 60, 38][i];
      ctx.fillStyle = i % 2 ? "#d9c7a6" : "#c9b48f";
      ctx.fillRect(x, GROUND - 6 - h, 56, h);
      ctx.fillStyle = "#7aa7d6";
      for (let wy = GROUND - h; wy < GROUND - 14; wy += 10) {
        for (let wx = x + 5; wx < x + 50; wx += 10) ctx.fillRect(wx, wy, 5, 6);
      }
    }
    // деревья
    for (let i = 0; i < 5; i++) {
      const x = ((i * 70 - s * 0.5) % 350 + 350) % 350 - 30;
      ctx.fillStyle = "#6b4423"; ctx.fillRect(x + 8, GROUND - 16, 3, 10);
      ctx.fillStyle = "#3f8f3a"; ctx.fillRect(x + 2, GROUND - 30, 15, 15); ctx.fillRect(x + 5, GROUND - 34, 9, 4);
    }
    ctx.fillStyle = "#5fae4e"; ctx.fillRect(0, GROUND - 6, W, 6);
    ctx.fillStyle = "#b8b2a7"; ctx.fillRect(0, GROUND, W, H - GROUND);
    ctx.fillStyle = "#9c968b";
    for (let x = -(s % 24); x < W; x += 24) ctx.fillRect(x, GROUND + 8, 12, 2);
  }

  function drawCorridor(s) {
    ctx.fillStyle = "#efe3c8"; ctx.fillRect(0, 0, W, GROUND);
    ctx.fillStyle = "#8fb3a3"; ctx.fillRect(0, GROUND - 26, W, 26);
    ctx.fillStyle = "#6f9686"; ctx.fillRect(0, GROUND - 27, W, 1);
    for (let x = -(s % 96); x < W + 96; x += 96) {
      ctx.fillStyle = "#8b5a2b"; ctx.fillRect(x + 10, GROUND - 46, 20, 46);
      ctx.fillStyle = "#6b4423"; ctx.fillRect(x + 12, GROUND - 44, 16, 20);
      ctx.fillStyle = "#f5c518"; ctx.fillRect(x + 26, GROUND - 24, 2, 3);
      ctx.fillStyle = "#ffffff"; ctx.fillRect(x + 14, GROUND - 54, 12, 5);
      ctx.fillStyle = "#ffffff"; ctx.fillRect(x + 50, GROUND - 76, 30, 32);
      ctx.fillStyle = "#bfe3ff"; ctx.fillRect(x + 52, GROUND - 74, 12, 28); ctx.fillRect(x + 66, GROUND - 74, 12, 28);
    }
    for (let x = -(s % 16); x < W; x += 16) {
      ctx.fillStyle = (Math.floor((x + s) / 16) % 2) ? "#c7c7c7" : "#b3b3b3";
      ctx.fillRect(x, GROUND, 16, H - GROUND);
    }
  }

  function drawHall(s) {
    ctx.fillStyle = "#e3d5b8"; ctx.fillRect(0, 0, W, GROUND);
    ctx.fillStyle = "#6b4423"; ctx.fillRect(40, 14, 90, 40);
    ctx.fillStyle = "#2f4f3f"; ctx.fillRect(44, 18, 82, 32);
    ctx.fillStyle = "#cfe8d8"; ctx.fillRect(52, 25, 36, 2); ctx.fillRect(52, 32, 54, 2); ctx.fillRect(52, 39, 26, 2);
    for (let x = -(s % 20); x < W; x += 20) {
      ctx.fillStyle = "#a87b4f"; ctx.fillRect(x, GROUND, 20, H - GROUND);
      ctx.fillStyle = "#8a6240"; ctx.fillRect(x, GROUND, 1, H - GROUND);
    }
  }

  function drawPerson(look, x, bottom, frame, mirror) {
    const img = frame === "jump" ? look.jump : frame === "duck" ? look.duck : look.run[frame];
    const y = Math.round(bottom - img.height);
    if (mirror) {
      ctx.save(); ctx.scale(-1, 1); ctx.drawImage(img, -Math.round(x) - img.width, y); ctx.restore();
    } else ctx.drawImage(img, Math.round(x), y);
  }

  function drawCommission() {
    ["office", "coat", "sport"].forEach((k, i) => drawPerson(LOOKS[k], W - 50 + i * 15, GROUND - 11, 1, true));
    ctx.fillStyle = "#6b4423"; ctx.fillRect(W - 56, GROUND - 14, 56, 14);
    ctx.fillStyle = "#8b5a2b"; ctx.fillRect(W - 56, GROUND - 15, 56, 2);
  }

  function draw() {
    const s = Math.floor(S.scroll);
    ({ street: drawStreet, corridor: drawCorridor, hall: drawHall })[S.st.theme](s);
    const frame = Math.floor(S.player.anim) % 2;

    // за кем бежим
    if (S.type === "chase") {
      drawPerson(LOOKS[CHASERS[S.course][0]], W - 46 + Math.sin(S.t * 2) * 6, GROUND, frame);
    } else if (S.type === "catch") {
      const k = Math.min(1, S.t / (CATCH_TIME * 0.9));
      CHASERS[4].forEach((name, i) => {
        const start = W - 80 + i * 24;
        const x = start + (PLAYER_X + 12 + i * 8 - start) * k;
        drawPerson(LOOKS[name], x, GROUND, k >= 1 ? 1 : (frame + i) % 2);
      });
    } else if (S.type === "defense") drawCommission();

    for (const o of S.objects) {
      const y = o.bottom - o.h + (o.item ? Math.round(Math.sin(S.t * 5 + o.phase)) : 0);
      if (o.kind === "crowd") {
        drawPerson(LOOKS[o.people[0]], o.x, o.bottom, 1);
        drawPerson(LOOKS[o.people[1]], o.x + 7, o.bottom, 1);
      } else ctx.drawImage(o.img || OBSTACLES[o.kind].img, Math.round(o.x), Math.round(y));
    }

    const p = S.player;
    if (S.invuln <= 0 || Math.floor(S.invuln * 10) % 2 === 0) {
      const pose = p.y > 0 ? "jump" : input.duckHeld ? "duck" : frame;
      drawPerson(LOOKS.student, PLAYER_X, GROUND - Math.round(p.y), pose);
    }
    updateHud();
  }

  function updateHud() {
    hud.stage.textContent = stageLabel();
    hud.lives.textContent = "❤".repeat(Math.max(0, S.lives)) + "♡".repeat(LIVES - Math.max(0, S.lives));
    hud.score.textContent = `Счёт: ${Math.floor(S.score)}`;
    if (S.st.items) {
      const left = Math.max(0, Math.ceil(VED_TIME - S.t));
      const what = S.st.items === "zachet" ? "Зачёты" : "Листы";
      hud.goal.textContent = `${what} ${S.collected}/${NEED} · ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
    } else if (S.type === "defense") {
      hud.goal.textContent = `Осталось ${Math.max(0, Math.ceil(DEFENSE_TIME - S.t))} с`;
    } else hud.goal.textContent = "";
  }

  // --- Цикл и экраны между этапами ---
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
    touch = null;
    input.duckHeld = false;
    try { tg?.enableVerticalSwipes?.(); } catch (e) { /* старый Telegram */ }
  }

  function pause() {
    if (!running) return;
    stop();
    show("Пауза", "Игра остановлена.", [["Продолжить", resume]]);
  }

  function show(title, text, buttons) {
    over.textContent = "";
    const box = document.createElement("div");
    box.className = "sg-box";
    const h = document.createElement("div");
    h.className = "sg-title";
    h.textContent = title;
    const p = document.createElement("div");
    p.className = "sg-text";
    p.textContent = text;
    box.append(h, p);
    const row = document.createElement("div");
    row.className = "sg-buttons";
    for (const [label, fn, secondary] of buttons) {
      const b = document.createElement("button");
      b.className = "sg-btn" + (secondary ? " sg-secondary" : "");
      b.textContent = label;
      b.disabled = true; // защита от случайного тапа, пока игрок ещё жмёт на экран
      setTimeout(() => { b.disabled = false; }, 450);
      b.addEventListener("click", fn);
      row.append(b);
    }
    box.append(row);
    over.append(box);
    over.style.display = "";
  }

  function between(title, text, next) {
    stop();
    draw();
    show(title, text, [["Вперёд", () => { startStage(next); resume(); }]]);
  }

  function finish(won, title, text) {
    stop();
    draw();
    const score = Math.floor(S.score);
    boardEl.textContent = "Сохраняю результат...";
    show(title, `${text} Счёт: ${score}.`, [["Ещё раз", () => { boardEl.textContent = ""; newGame(); resume(); }]]);
    sendScore(score, boardEl);
  }

  async function api(method, path, body) {
    const res = await fetch("/api/game" + path, {
      method,
      headers: { "Content-Type": "application/json", "X-Init-Data": tg?.initData || "" },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) throw new Error(String(res.status));
    return res.json();
  }

  async function sendScore(score, board) {
    try {
      renderBoard(board, await api("POST", "/score", { score }));
    } catch (e) {
      board.textContent = "Не удалось сохранить результат.";
    }
  }

  function renderBoard(board, data) {
    board.textContent = "";
    const head = document.createElement("div");
    head.className = "sg-board-head";
    head.textContent = data.best != null ? `Твой рекорд: ${data.best} (${data.rank} место)` : "Ты ещё не в таблице";
    board.append(head);
    const list = document.createElement("ol");
    for (const row of data.top || []) {
      const li = document.createElement("li");
      if (row.me) li.className = "sg-me";
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
    show("Сессия", "Беги за преподавателями, собирай зачёты на ведомостях и дойди до защиты диплома. " +
      "Жизней 3 на всю игру.",
    [["Играть", () => { boardEl.textContent = ""; newGame(); resume(); }], ["Таблица лидеров", async (e) => {
      e.target.disabled = true;
      boardEl.textContent = "Загружаю...";
      try { renderBoard(boardEl, await api("GET", "/top")); } catch (err) { boardEl.textContent = "Не удалось загрузить таблицу."; }
    }, true]]);
  }

  const CSS = `
  .sg { user-select: none; -webkit-user-select: none; }
  .sg-hud { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; font-size: 15px; font-weight: 600; margin-bottom: 6px; }
  .sg-lives { color: #ff453a; letter-spacing: 2px; }
  .sg-hud2 { display: flex; justify-content: space-between; color: var(--hint); font-size: 14px; margin-bottom: 8px; min-height: 18px; }
  .sg-view { position: relative; border-radius: 14px; overflow: hidden; background: #000; touch-action: none; }
  .sg-view canvas { display: block; width: 100%; aspect-ratio: ${W} / ${H}; image-rendering: pixelated; image-rendering: crisp-edges; }
  .sg-over { position: absolute; inset: 0; background: rgba(0,0,0,0.72); display: flex; align-items: center; justify-content: center; padding: 10px; }
  .sg-box { text-align: center; max-width: 320px; }
  .sg-title { font-size: 19px; font-weight: 700; margin-bottom: 4px; }
  .sg-text { font-size: 13px; line-height: 1.3; color: #e5e5ea; margin-bottom: 10px; }
  .sg-buttons { display: flex; gap: 8px; justify-content: center; flex-wrap: wrap; }
  .sg-btn { border: none; border-radius: 12px; padding: 10px 18px; font-size: 15px; font-weight: 600; background: var(--accent); color: #fff; font-family: inherit; }
  .sg-btn.sg-secondary { background: var(--card-pressed); }
  .sg-btn:disabled { opacity: 0.6; }
  .sg-help { color: var(--hint); font-size: 13px; text-align: center; margin-top: 10px; }
  .sg-board { margin-top: 14px; font-size: 14px; }
  .sg-board:empty { display: none; }
  .sg-board ol { list-style: none; padding: 0; margin: 6px 0 0; counter-reset: place; }
  .sg-board li { display: flex; gap: 8px; padding: 6px 10px; border-radius: 10px; background: var(--card); margin-bottom: 4px; counter-increment: place; }
  .sg-board li::before { content: counter(place) "."; color: var(--hint); min-width: 20px; text-align: left; }
  .sg-board li span { flex: 1; text-align: left; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .sg-board li.sg-me { background: rgba(10, 132, 255, 0.3); }
  .sg-board-head { color: #e5e5ea; }
  `;

  function build(container) {
    root = container;
    const style = document.createElement("style");
    style.textContent = CSS;
    document.head.append(style);
    root.innerHTML = `<div class="sg">
      <div class="sg-hud"><span class="sg-stage"></span><span class="sg-lives"></span></div>
      <div class="sg-hud2"><span class="sg-score"></span><span class="sg-goal"></span></div>
      <div class="sg-view"><canvas width="${W}" height="${H}"></canvas><div class="sg-over"></div></div>
      <div class="sg-help">Тап - прыжок · свайп вниз - пригнуться</div>
      <div class="sg-board"></div>
    </div>`;
    canvas = root.querySelector("canvas");
    ctx = canvas.getContext("2d");
    ctx.imageSmoothingEnabled = false;
    over = root.querySelector(".sg-over");
    boardEl = root.querySelector(".sg-board");
    hud = { stage: root.querySelector(".sg-stage"), lives: root.querySelector(".sg-lives"),
      score: root.querySelector(".sg-score"), goal: root.querySelector(".sg-goal") };
    const view = root.querySelector(".sg-view");
    view.addEventListener("touchstart", onTouchStart, { passive: false });
    view.addEventListener("touchmove", onTouchMove, { passive: false });
    view.addEventListener("touchend", onTouchEnd, { passive: false });
    view.addEventListener("touchcancel", onTouchEnd, { passive: false });
    canvas.addEventListener("mousedown", (e) => { if (running) { e.preventDefault(); jump(); } });
    canvas.addEventListener("mouseup", () => releaseJump());
    document.addEventListener("keydown", onKey);
    document.addEventListener("keyup", onKey);
    document.addEventListener("visibilitychange", () => { if (document.hidden) pause(); });
    newGame();
    intro();
  }

  window.SessionGame = {
    // вызывается при каждом открытии карточки; при повторном открытии игра стоит на паузе
    open(container) {
      if (!root) build(container);
    },
    // для проверки игры из консоли и автотестов
    debug: {
      state: () => S, input,
      stage: (type, course) => { if (course) S.course = course; startStage(type); resume(); },
      step: (dt) => { if (running) { update(dt); if (running) draw(); } return running; },
      jump, releaseJump,
    },
  };
})();
