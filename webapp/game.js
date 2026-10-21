// Мини-игра «Сессия» для раздела «Другое». Файл грузится только при открытии карточки игры,
// поэтому на скорость открытия расписания не влияет. Всё рисуется кодом, звука нет.
(() => {
  "use strict";
  const W = 120, H = 120, GROUND = 98, PLAYER_X = 18;
  const GRAVITY = 1100, JUMP_SPEED = 330, JUMP_CUT = 150;
  const LIVES = 3, NEED = 5, VED_TIME = 60, CHASE_TIME = 18, CATCH_TIME = 14, DEFENSE_TIME = 30;
  const tg = window.Telegram?.WebApp;

  // --- Пиксельные спрайты: строки символов, "." прозрачно ---
  function canvasOf(w, h, paint) {
    const c = document.createElement("canvas");
    c.width = w;
    c.height = h;
    const x = c.getContext("2d");
    paint(x);
    return c;
  }
  function sprite(rows, colors) {
    return canvasOf(Math.max(...rows.map((r) => r.length)), rows.length, (x) => {
      rows.forEach((row, y) => [...row].forEach((ch, i) => {
        if (colors[ch]) { x.fillStyle = colors[ch]; x.fillRect(i, y, 1, 1); }
      }));
    });
  }
  // Обводка вокруг картинки: тёмный контур и (для опасностей) красное свечение, чтобы их было видно на любом фоне
  function outlined(src, rings) {
    const pad = rings.length;
    const silhouette = (color) => canvasOf(src.width, src.height, (x) => {
      x.drawImage(src, 0, 0);
      x.globalCompositeOperation = "source-in";
      x.fillStyle = color;
      x.fillRect(0, 0, src.width, src.height);
    });
    const c = canvasOf(src.width + pad * 2, src.height + pad * 2, (x) => {
      rings.slice().reverse().forEach((color, i) => {
        const r = pad - i;
        const sil = silhouette(color);
        for (let dy = -r; dy <= r; dy++) {
          for (let dx = -r; dx <= r; dx++) {
            if (Math.abs(dx) + Math.abs(dy) <= r + (r > 1 ? 1 : 0)) x.drawImage(sil, pad + dx, pad + dy);
          }
        }
      });
      x.drawImage(src, pad, pad);
    });
    c.pad = pad;
    return c;
  }
  const INK = "#1b1426";
  const hazard = (src) => outlined(src, [INK, "rgba(255, 45, 45, 0.75)"]);
  const shiny = (src) => outlined(src, [INK, "rgba(255, 214, 10, 0.85)"]);

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

  function person(colors, rings = [INK]) {
    const pal = { ...SKIN, ...colors };
    const make = (rows) => outlined(sprite(rows, pal), rings);
    return { run: [make(RUN1), make(RUN2)], jump: make(JUMP), duck: make(DUCK) };
  }
  // Персонажи без фамилий: студент (игрок), халат, спортивный костюм, офисный костюм
  const LOOKS = {
    student: person({ H: "#4a2f1b", T: "#2f6fd6", A: "#2f6fd6", P: "#2b2d42", B: "#eeeeee" }),
    coat: person({ H: "#2b1d14", T: "#f4f4f4", A: "#58b9e8", P: "#58a79c", B: "#ffffff" }),
    sport: person({ H: "#1d1d1d", T: "#d7263d", A: "#ffffff", P: "#d7263d", B: "#f4f4f4" }),
    office: person({ H: "#7a5230", T: "#353a47", A: "#c0392b", P: "#353a47", B: "#111111" }),
  };
  // толпа студентов на пути - тоже препятствие, поэтому с красным свечением
  const CROWD = [
    person({ H: "#222222", T: "#3fa34d", A: "#3fa34d", P: "#44475a", B: "#dddddd" }, [INK, "rgba(255, 45, 45, 0.75)"]),
    person({ H: "#8a5a2b", T: "#e67e22", A: "#e67e22", P: "#2b2d42", B: "#dddddd" }, [INK, "rgba(255, 45, 45, 0.75)"]),
    person({ H: "#3a2a1a", T: "#8e44ad", A: "#8e44ad", P: "#2b2d42", B: "#dddddd" }, [INK, "rgba(255, 45, 45, 0.75)"]),
  ];
  const CHASERS = { 1: ["coat"], 2: ["sport"], 3: ["office"], 4: ["coat", "sport", "office"] };

  // Буквы 4x5 для растяжки «СЕССИЯ»
  const LETTERS = {
    С: [".###", "#...", "#...", "#...", ".###"], Е: ["####", "#...", "###.", "#...", "####"],
    И: ["#..#", "#..#", "#.##", "##.#", "#..#"], Я: [".###", "#..#", ".###", ".#.#", "#..#"],
  };
  const BANNER_H = 48, BANNER_W = 14;
  const banner = canvasOf(BANNER_W, BANNER_H, (x) => {
    x.fillStyle = "#ffffff"; x.fillRect(0, 0, BANNER_W, BANNER_H);
    x.fillStyle = "#d7263d"; x.fillRect(1, 1, BANNER_W - 2, BANNER_H - 2);
    x.fillStyle = "#ffffff";
    [..."СЕССИЯ"].forEach((ch, i) => LETTERS[ch].forEach((row, y) => [...row].forEach((v, j) => {
      if (v === "#") x.fillRect(5 + j, 6 + i * 6 + y, 1, 1);
    })));
  });
  const stand = canvasOf(BANNER_W + 4, BANNER_H, (x) => {
    for (let y = 0; y < BANNER_H; y++) {
      for (let i = 0; i < BANNER_W + 4; i++) {
        x.fillStyle = Math.floor((i + y) / 3) % 2 ? "#1d1d1d" : "#ffcc00";
        x.fillRect(i, y, 1, 1);
      }
    }
    x.fillStyle = "#ffffff"; x.fillRect(3, 3, BANNER_W - 2, BANNER_H - 6);
    x.fillStyle = "#e53935"; x.fillRect(8, 12, 3, 18); x.fillRect(8, 34, 3, 3);
  });

  const IMG = {
    chair: hazard(sprite(["........BB", "........BB", "........BB", "........BB", "BBBBBBBBBB", "DDDDDDDDDD",
      ".B......B.", ".B......B.", ".B......B.", ".B......B."], { B: "#a0522d", D: "#6b3410" })),
    puddle: hazard(sprite(["...LLLLLLLLLL...", ".WWLLLLLLLLLLWW.", "WWWWWWWWWWWWWWWW"], { W: "#1f5fd6", L: "#5aa0ff" })),
    ball: hazard(sprite(["..OOOO..", ".OODOOO.", "OOODOOOO", "ODDDDDDO", "OOODOOOO", "OOODOOOO", ".OODOOO.", "..OOOO.."],
      { O: "#ff8c00", D: "#5a2a00" })),
    bin: hazard(sprite(["DDDDDDDD", ".GGGGGG.", ".GLGGGG.", ".GLGGGG.", ".GLGGGG.", ".GLGGGG.", ".GLGGGG.",
      ".GGGGGG.", ".DDDDDD."], { G: "#2e7d4f", L: "#6fcf97", D: "#14402a" })),
    cones: hazard(sprite(["...W.......W...", "..OOO.....OOO..", "..OOO.....OOO..", ".WWWWW...WWWWW.", ".OOOOO...OOOOO.",
      "OOOOOOO.OOOOOOO", "OOOOOOO.OOOOOOO", "DDDDDDDDDDDDDDD"], { O: "#ff6a00", W: "#ffffff", D: "#7a3200" })),
    hurdle: hazard(sprite(["RWRWRWRWRWRWRW", "RWRWRWRWRWRWRW", "K............K", "K............K", "K............K",
      "K............K", "K............K", "K............K", "K............K", "KK..........KK"],
    { R: "#e53935", W: "#ffffff", K: "#333333" })),
    books: hazard(sprite(["..RRRRRR..", "..RRRRRR..", ".BBBBBBBB.", ".BBBBBBBB.", "GGGGGGGG..", "GGGGGGGG..",
      ".YYYYYYYY.", ".YYYYYYYY.", "PPPPPPPPPP", "PPPPPPPPPP"],
    { R: "#e53935", B: "#1e63d6", G: "#2e9e4f", Y: "#f5c518", P: "#8e44ad" })),
    banner: hazard(banner),
    stand: hazard(stand),
    zachet: shiny(sprite(["WWWWWWW.", "WGGGGGWW", "WWWWWWWW", "WGGGGGGW", "WWWWWWWW", "WGGGGWWW", "WWWWWRRW",
      "WWWWRWWR", "WWWWWRRW", "WWWWWWWW"], { W: "#ffffff", G: "#9aa0a6", R: "#e53935" })),
    diploma: shiny(sprite(["BBBBBBB.", "BYYYYYBB", "BBBBBBBB", "BBYYYYBB", "BBYBBYBB", "BBYYYYBB", "BBBBBBBB",
      "BYYYYYYB", "BBBBBBBB", "BBBBBBBB"], { B: "#1e4fa8", Y: "#f5c518" })),
    question: hazard(sprite(["..RRR..", ".RR.RR.", "....RR.", "...RR..", "..RR...", "..RR...", ".......", "..RR...", "..RR..."],
      { R: "#ffffff" })),
  };

  // --- Препятствия: lift - высота над землёй, hit - рамка столкновения внутри картинки (без обводки).
  // duck: только пригнуться (висит высоко, перепрыгнуть нельзя); themes - где встречается
  const OBSTACLES = {
    puddle: { w: 16, h: 3, lift: 0, hit: [2, 1, 12, 2], themes: ["street", "corridor"] },
    crowd: { w: 19, h: 15, lift: 0, hit: [3, 2, 14, 13], themes: ["street", "corridor"] },
    bin: { w: 8, h: 9, lift: 0, hit: [1, 0, 6, 9], themes: ["street", "corridor"] },
    cones: { w: 15, h: 8, lift: 0, hit: [1, 1, 13, 7], themes: ["street"] },
    hurdle: { w: 14, h: 10, lift: 0, hit: [0, 0, 14, 10], themes: ["street"], from: 2 },
    chair: { w: 10, h: 10, lift: 0, hit: [1, 1, 8, 9], themes: ["corridor"] },
    books: { w: 10, h: 10, lift: 0, hit: [0, 0, 10, 10], themes: ["corridor"] },
    ball: { w: 8, h: 8, lift: 11, hit: [0, 1, 8, 7], themes: ["street", "corridor"], from: 2 },
    banner: { w: BANNER_W, h: BANNER_H, lift: 11, hit: [0, 0, BANNER_W, BANNER_H], themes: ["street"], duck: true },
    stand: { w: BANNER_W + 4, h: BANNER_H, lift: 11, hit: [0, 0, BANNER_W + 4, BANNER_H], themes: ["corridor"], duck: true },
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

  let root, canvas, ctx, over, hud = {};
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
    const kinds = Object.keys(OBSTACLES).filter((k) => {
      const o = OBSTACLES[k];
      return o.themes.includes(S.st.theme) && (!o.from || S.course >= o.from || S.type !== "chase");
    });
    // под висячие только пригнуться: примерно каждое пятое препятствие, на третьей ведомости и пересдаче чаще
    const hard = S.type === "ved3" || S.type === "diploma2";
    const hanging = kinds.filter((k) => OBSTACLES[k].duck), ground = kinds.filter((k) => !OBSTACLES[k].duck);
    const kind = Math.random() < (hard ? 0.3 : 0.2) ? pick(hanging) : pick(ground);
    const o = OBSTACLES[kind];
    S.objects.push({ kind, x: W + 4, w: o.w, h: o.h, bottom: GROUND - o.lift, hit: o.hit, duck: !!o.duck,
      people: kind === "crowd" ? [pick(CROWD), pick(CROWD)] : null });
  }

  function spawnItem() {
    // не кладём зачёт прямо на препятствие
    if (S.objects.some((o) => !o.item && o.x > W - 40)) { S.nextItem = 0.25; return; }
    const high = Math.random() < 0.5;
    const img = IMG[S.st.items];
    const w = img.width - img.pad * 2, h = img.height - img.pad * 2;
    S.objects.push({ item: true, img, x: W + 4, w, h, bottom: GROUND - (high ? 30 : 6),
      hit: [-2, -2, w + 4, h + 4], phase: Math.random() * 6 });
    S.itemsLeft--;
  }

  function spawnQuestion() {
    const high = Math.random() < 0.5;
    S.objects.push({ kind: "question", img: IMG.question, x: W - 50, w: 7, h: 9, bottom: high ? GROUND - 11 : GROUND - 1,
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

  // третья ведомость и пересдача - второй шанс, очков на них не дают
  const scoring = () => S.type !== "ved3" && S.type !== "diploma2";

  function update(dt) {
    const p = S.player;
    S.t += dt;
    if (input.duckUntil && performance.now() > input.duckUntil && !touch) { input.duckHeld = false; input.duckUntil = 0; }

    const moving = S.type !== "defense";
    const speed = moving ? S.speed : 30;
    S.scroll += speed * dt;
    if (scoring()) S.score += (moving ? S.speed * 0.1 : 12) * dt;
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
        if (scoring()) S.score += 50;
        haptic("light");
      } else if (S.invuln <= 0) {
        o.gone = true;
        S.lastHit = o.kind;
        S.lives--;
        S.invuln = 1.4;
        haptic("heavy");
        if (S.lives <= 0) return finish(false, pick(MSG.deadTitle), pick(MSG.deadText));
      }
    }
    S.objects = S.objects.filter((o) => o.x > -30 && !(o.gone && o.item));
    checkStageEnd();
  }

  // Несколько вариантов каждого сообщения, чтобы между этапами не было одного и того же текста
  const MSG = {
    chaseTitle: ["Не догнал!", "Упустил!", "Почти догнал!", "Слишком быстро!"],
    chaseText: ["Догнать преподавателя невозможно.", "Был совсем близко, но преподаватель скрылся в аудитории.",
      "Преподаватель свернул за угол и пропал.", "Расписались без тебя."],
    passTitle: (n) => [`Переводишься на ${n} курс!`, `Зачёт! Добро пожаловать на ${n} курс`, `Ведомость закрыта, впереди ${n} курс`],
    passText: ["Новый курс, новая погоня.", "Отдыхать некогда: снова кого-то догонять.", "Каникулы были короткими."],
    lastCourse: ["Последний курс: теперь ты догонишь всех, кто убегал.", "4 курс. Пора догнать всех, кто убегал все эти годы."],
    lateTitle: ["Не успел", "Время вышло", "Ведомость не закрыта"],
    catchTitle: ["Догнал всех!", "Попались!", "Все пойманы!"],
    sheetsTitle: ["Листы собраны!", "Диплом сшит!", "Всё подписано!"],
    deadTitle: ["Жизни кончились", "Без сил", "Сессия победила"],
    deadText: ["Слишком много падений по дороге к диплому.", "Споткнулся в третий раз и сошёл с дистанции.",
      "Синяков больше, чем зачётов."],
    outTitle: ["Отчислен", "Сессия не сдана", "До встречи в следующем году"],
    winTitle: ["Диплом получен!", "Выпускник!", "Защитился!"],
    winText: ["Ты прошёл все четыре курса и защитился.", "Четыре курса позади, комиссия довольна.",
      "Комиссия аплодирует стоя."],
  };

  function checkStageEnd() {
    const c = S.course;
    if (S.type === "chase" && S.t >= CHASE_TIME) {
      return between(pick(MSG.chaseTitle), `${pick(MSG.chaseText)} Тебя отправляют на вторую ведомость: ` +
        `собери ${NEED} зачётов за ${VED_TIME} секунд.`, "ved2");
    }
    if (S.type === "catch" && S.t >= CATCH_TIME) {
      S.score += 300;
      return between(pick(MSG.catchTitle), `Остались листы диплома: собери ${NEED} за ${VED_TIME} секунд.`, "diploma");
    }
    if (S.type === "defense" && S.t >= DEFENSE_TIME) {
      S.score += 1000;
      return finish(true, pick(MSG.winTitle), pick(MSG.winText));
    }
    if (!S.st.items) return;
    const done = S.collected >= NEED;
    const out = S.t >= VED_TIME || (S.itemsLeft === 0 && !S.objects.some((o) => o.item) && !done);
    if (done) {
      if (scoring()) S.score += 300;
      if (S.type === "diploma" || S.type === "diploma2") {
        return between(pick(MSG.sheetsTitle), `Защита: ${DEFENSE_TIME} секунд уворачивайся от вопросов комиссии. ` +
          "Низкий вопрос перепрыгни, под высоким пригнись.", "defense");
      }
      S.course = c + 1;
      return between(pick(MSG.passTitle(c + 1)), pick(c + 1 === 4 ? MSG.lastCourse : MSG.passText), c + 1 === 4 ? "catch" : "chase");
    }
    if (!out) return;
    const again = "всё то же самое, но быстрее, препятствий больше, а очки не начисляются.";
    if (S.type === "ved2") return between(pick(MSG.lateTitle), `Третья ведомость: ${again}`, "ved3");
    if (S.type === "diploma") return between(pick(MSG.lateTitle), `Пересдача: ${again}`, "diploma2");
    return finish(false, pick(MSG.outTitle), S.type === "ved3" ? "Третья ведомость не закрыта." : "Пересдача не сдана.");
  }

  function haptic(kind) {
    try { tg?.HapticFeedback?.impactOccurred(kind); } catch (e) { /* не везде есть */ }
  }

  // --- Рисование ---
  const wrap = (x, period) => ((x % period) + period) % period;
  function rect(color, x, y, w, h) { ctx.fillStyle = color; ctx.fillRect(Math.round(x), Math.round(y), w, h); }

  function drawStreet(s) {
    // небо полосами, солнце, облака
    ["#86c5f2", "#97cff5", "#a9d9f7", "#bce3f9"].forEach((c, i) => rect(c, 0, i * 12, W, 12));
    rect("#c9e9fa", 0, 48, W, GROUND - 48);
    rect("#fff4b0", W - 28, 10, 14, 14); rect("#ffe066", W - 26, 12, 10, 10);
    for (let i = 0; i < 3; i++) {
      const x = wrap(i * 70 - s * 0.06, 210) - 40;
      rect("#ffffff", x, 18 + i * 9, 24, 5); rect("#ffffff", x + 5, 14 + i * 9, 12, 4); rect("#e3f2fb", x, 22 + i * 9, 24, 1);
    }
    // дальние дома
    for (let i = 0; i < 6; i++) {
      const x = wrap(i * 40 - s * 0.12, 240) - 40;
      const h = [26, 34, 22, 30, 38, 24][i];
      rect("#b4cbdc", x, GROUND - 14 - h, 34, h + 6);
    }
    // корпуса вуза: стены, окна, крыша, колонны у входа
    for (let i = 0; i < 4; i++) {
      const x = wrap(i * 80 - s * 0.3, 320) - 80;
      const h = [44, 56, 40, 50][i];
      const top = GROUND - 8 - h;
      rect("#8f7a63", x - 1, top - 3, 70, 3);
      rect(i % 2 ? "#e2d2b4" : "#d6c19c", x, top, 68, h);
      for (let wy = top + 5; wy < GROUND - 22; wy += 9) {
        for (let wx = x + 5; wx < x + 64; wx += 9) { rect("#6f93b8", wx, wy, 5, 6); rect("#a8c6e2", wx, wy, 2, 2); }
      }
      if (i % 2 === 0) {
        rect("#f2ead8", x + 22, GROUND - 22, 24, 3);
        for (let c = 0; c < 4; c++) rect("#f2ead8", x + 23 + c * 6, GROUND - 19, 3, 11);
        rect("#5b4636", x + 30, GROUND - 17, 8, 9);
      }
    }
    // фонари и деревья
    for (let i = 0; i < 4; i++) {
      const x = wrap(i * 56 - s * 0.6, 224) - 30;
      if (i % 2) {
        rect("#4a4f5a", x + 6, GROUND - 40, 2, 32); rect("#4a4f5a", x + 4, GROUND - 42, 6, 2); rect("#fff3a0", x + 5, GROUND - 40, 4, 2);
      } else {
        rect("#6b4423", x + 9, GROUND - 22, 4, 14);
        rect("#2f7a35", x + 1, GROUND - 40, 20, 18); rect("#3f9a45", x + 4, GROUND - 44, 14, 6); rect("#58b55e", x + 5, GROUND - 38, 6, 5);
      }
    }
    // газон и тротуар
    rect("#4e9a46", 0, GROUND - 8, W, 8); rect("#6dbb5c", 0, GROUND - 8, W, 2);
    rect("#d5cfc3", 0, GROUND, W, H - GROUND);
    rect("#9e968a", 0, GROUND, W, 2);
    for (let x = -wrap(s, 18); x < W; x += 18) { rect("#c2bbae", x, GROUND + 2, 1, H - GROUND); }
    for (let x = -wrap(s, 36); x < W; x += 36) rect("#c2bbae", x, GROUND + 13, 18, 1);
  }

  function drawCorridor(s) {
    rect("#efe5cd", 0, 0, W, GROUND);
    rect("#d9ccae", 0, 0, W, 8);
    for (let x = -wrap(s, 48); x < W; x += 48) { rect("#fffbe6", x + 14, 8, 20, 2); rect("#c7b994", x + 14, 7, 20, 1); }
    rect("#93b8a8", 0, GROUND - 30, W, 30); rect("#7aa190", 0, GROUND - 31, W, 1); rect("#6b8f80", 0, GROUND - 3, W, 3);
    for (let x = -wrap(s, 96); x < W + 96; x += 96) {
      // дверь аудитории с табличкой
      rect("#6b3f1f", x + 8, GROUND - 52, 24, 52);
      rect("#8b5a2b", x + 10, GROUND - 50, 20, 50);
      rect("#74481f", x + 12, GROUND - 47, 16, 20); rect("#74481f", x + 12, GROUND - 24, 16, 20);
      rect("#f5c518", x + 26, GROUND - 27, 2, 4);
      rect("#ffffff", x + 13, GROUND - 60, 14, 6); rect("#5b6b7a", x + 15, GROUND - 58, 10, 2);
      // окно и доска объявлений
      rect("#ffffff", x + 44, GROUND - 92, 34, 40);
      rect("#9fd0f2", x + 46, GROUND - 90, 14, 36); rect("#9fd0f2", x + 62, GROUND - 90, 14, 36);
      rect("#d4ecfb", x + 47, GROUND - 89, 4, 8);
      rect("#b07a45", x + 54, GROUND - 46, 30, 16); rect("#d9a066", x + 55, GROUND - 45, 28, 14);
      rect("#ffffff", x + 57, GROUND - 43, 6, 8); rect("#fff3a0", x + 65, GROUND - 42, 7, 6); rect("#cde7ff", x + 74, GROUND - 43, 6, 9);
    }
    for (let x = -wrap(s, 16); x < W; x += 16) {
      const dark = Math.floor((x + s) / 16) % 2;
      rect(dark ? "#b9b9b9" : "#d0d0d0", x, GROUND, 16, 13);
      rect(dark ? "#d0d0d0" : "#b9b9b9", x, GROUND + 13, 16, H - GROUND - 13);
    }
    rect("#8d8d8d", 0, GROUND, W, 1);
  }

  function drawHall(s) {
    rect("#eadcbf", 0, 0, W, GROUND);
    rect("#d8c7a5", 0, 0, W, 6);
    // окно слева и доска
    rect("#ffffff", 4, 14, 18, 40); rect("#9fd0f2", 6, 16, 14, 36); rect("#ffffff", 12, 16, 2, 36);
    rect("#6b4423", 30, 12, 62, 36); rect("#2f4f3f", 33, 15, 56, 30);
    rect("#d8eadf", 38, 20, 26, 2); rect("#d8eadf", 38, 26, 40, 2); rect("#d8eadf", 38, 32, 18, 2); rect("#d8eadf", 60, 38, 22, 2);
    rect("#8a6240", 30, 48, 62, 3);
    rect("#c79a6b", 0, GROUND, W, H - GROUND);
    for (let x = -wrap(s, 12); x < W; x += 12) rect("#a97c51", x, GROUND, 1, H - GROUND);
    for (let y = GROUND + 6; y < H; y += 7) rect("#b88b5e", 0, y, W, 1);
  }

  function drawPerson(look, x, bottom, frame, mirror) {
    const img = frame === "jump" ? look.jump : frame === "duck" ? look.duck : look.run[frame];
    const y = Math.round(bottom - img.height + img.pad);
    const left = Math.round(x) - img.pad;
    if (mirror) {
      ctx.save(); ctx.scale(-1, 1); ctx.drawImage(img, -left - img.width, y); ctx.restore();
    } else ctx.drawImage(img, left, y);
  }

  function shadow(x, w) {
    ctx.fillStyle = "rgba(0, 0, 0, 0.18)";
    ctx.fillRect(Math.round(x) + 1, GROUND - 1, w - 2, 2);
  }

  function drawCommission() {
    ["office", "coat", "sport"].forEach((k, i) => drawPerson(LOOKS[k], W - 46 + i * 15, GROUND - 11, 1, true));
    rect("#2e6b45", W - 52, GROUND - 16, 52, 16); rect("#3d8a5a", W - 52, GROUND - 16, 52, 2);
    rect("#1d4a2f", W - 52, GROUND - 2, 52, 2);
  }

  function drawObject(o) {
    if (o.kind === "crowd") {
      shadow(o.x, o.w);
      drawPerson(o.people[0], o.x, o.bottom, 1);
      drawPerson(o.people[1], o.x + 7, o.bottom, 1);
      return;
    }
    const img = o.img || IMG[o.kind];
    const y = o.bottom - o.h + (o.item ? Math.round(Math.sin(S.t * 5 + o.phase)) : 0);
    if (o.duck) {
      // висит на верёвках с потолка: видно, что сверху не обойти
      const ropes = Math.round(y);
      rect("#3b3b44", o.x + 2, 0, 1, ropes); rect("#3b3b44", o.x + o.w - 3, 0, 1, ropes);
    } else if (!o.item && o.bottom === GROUND) shadow(o.x, o.w);
    ctx.drawImage(img, Math.round(o.x) - img.pad, Math.round(y) - img.pad);
  }

  function draw() {
    const s = Math.floor(S.scroll);
    ({ street: drawStreet, corridor: drawCorridor, hall: drawHall })[S.st.theme](s);
    const frame = Math.floor(S.player.anim) % 2;

    // за кем бежим
    if (S.type === "chase") {
      // отрыв постепенно сокращается, но до игрока остаётся пара шагов
      const k = Math.min(1, S.t / CHASE_TIME);
      const x = W - 34 - (W - 34 - (PLAYER_X + 24)) * (k * k * (3 - 2 * k)) + Math.sin(S.t * 2) * 3 * (1 - k);
      shadow(x, 12);
      drawPerson(LOOKS[CHASERS[S.course][0]], x, GROUND, frame);
    } else if (S.type === "catch") {
      const k = Math.min(1, S.t / (CATCH_TIME * 0.9));
      CHASERS[4].forEach((name, i) => {
        const start = W - 56 + i * 18;
        const x = start + (PLAYER_X + 10 + i * 7 - start) * k;
        shadow(x, 12);
        drawPerson(LOOKS[name], x, GROUND, k >= 1 ? 1 : (frame + i) % 2);
      });
    } else if (S.type === "defense") drawCommission();

    for (const o of S.objects) drawObject(o);

    const p = S.player;
    shadow(PLAYER_X + Math.min(4, p.y / 10), 12 - Math.min(8, p.y / 6));
    if (S.invuln <= 0 || Math.floor(S.invuln * 10) % 2 === 0) {
      const pose = p.y > 0 ? "jump" : input.duckHeld ? "duck" : frame;
      drawPerson(LOOKS.student, PLAYER_X, GROUND - Math.round(p.y), pose);
    }
    updateHud();
  }

  function updateHud() {
    hud.stage.textContent = stageLabel();
    hud.lives.textContent = "❤".repeat(Math.max(0, S.lives)) + "♡".repeat(LIVES - Math.max(0, S.lives));
    hud.score.textContent = `Счёт: ${Math.floor(S.score)}` + (scoring() ? "" : " · очки не идут");
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

  function show(title, text, buttons, note) {
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
    if (note) box.append(note);
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

  let lastBoard = null; // таблица из ответа на сохранение счёта: открывается без второго запроса

  function finish(won, title, text) {
    stop();
    draw();
    const score = Math.floor(S.score);
    const note = document.createElement("div");
    note.className = "sg-note";
    note.textContent = "Сохраняю результат...";
    lastBoard = null;
    show(title, `${text} Счёт: ${score}.`, [["Ещё раз", () => { newGame(); resume(); }], ["Таблица лидеров", openBoard, true]], note);
    sendScore(score, note);
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

  async function sendScore(score, note) {
    try {
      lastBoard = await api("POST", "/score", { score });
      note.textContent = myPlace(lastBoard);
    } catch (e) {
      note.textContent = "Не удалось сохранить результат.";
    }
  }

  function myPlace(data) {
    return data.best != null ? `Твой рекорд: ${data.best}, ${data.rank} место` : "Ты ещё не в таблице";
  }

  // Таблица лидеров - окно поверх страницы, листать ничего не надо
  async function openBoard() {
    const modal = document.createElement("div");
    modal.className = "sg-modal";
    const sheet = document.createElement("div");
    sheet.className = "sg-sheet";
    const title = document.createElement("div");
    title.className = "sg-title";
    title.textContent = "Таблица лидеров";
    const body = document.createElement("div");
    body.className = "sg-board";
    body.textContent = "Загружаю...";
    const close = document.createElement("button");
    close.className = "sg-btn";
    close.textContent = "Закрыть";
    sheet.append(title, body, close);
    modal.append(sheet);
    const remove = () => modal.remove();
    close.addEventListener("click", remove);
    modal.addEventListener("click", (e) => { if (e.target === modal) remove(); });
    document.body.append(modal);
    try {
      renderBoard(body, lastBoard || await api("GET", "/top"));
    } catch (e) {
      body.textContent = "Не удалось загрузить таблицу.";
    }
  }

  function renderBoard(board, data) {
    board.textContent = "";
    const head = document.createElement("div");
    head.className = "sg-board-head";
    head.textContent = myPlace(data);
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
    [["Играть", () => { newGame(); resume(); }], ["Таблица лидеров", openBoard, true]]);
  }

  const CSS = `
  .sg { user-select: none; -webkit-user-select: none; }
  .sg-hud { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; font-size: 15px; font-weight: 600; margin-bottom: 6px; }
  .sg-lives { color: #ff453a; letter-spacing: 2px; }
  .sg-hud2 { display: flex; justify-content: space-between; color: var(--hint); font-size: 14px; margin-bottom: 8px; min-height: 18px; }
  .sg-view { position: relative; border-radius: 14px; overflow: hidden; background: #000; touch-action: none; max-width: min(100%, 56vh); margin: 0 auto; }
  .sg-view canvas { display: block; width: 100%; aspect-ratio: ${W} / ${H}; image-rendering: pixelated; image-rendering: crisp-edges; }
  .sg-over { position: absolute; inset: 0; background: rgba(0,0,0,0.72); display: flex; align-items: center; justify-content: center; padding: 10px; }
  .sg-box { text-align: center; max-width: 320px; }
  .sg-title { font-size: 19px; font-weight: 700; margin-bottom: 4px; }
  .sg-text { font-size: 13px; line-height: 1.3; color: #e5e5ea; margin-bottom: 10px; }
  .sg-buttons { display: flex; gap: 8px; justify-content: center; flex-wrap: wrap; }
  .sg-btn { border: none; border-radius: 12px; padding: 10px 18px; font-size: 15px; font-weight: 600; background: var(--accent); color: #fff; font-family: inherit; }
  .sg-btn.sg-secondary { background: var(--card-pressed); }
  .sg-btn:disabled { opacity: 0.6; }
  .sg-pad { display: flex; gap: 12px; max-width: min(100%, 56vh); margin: 12px auto 0; }
  .sg-arrow { flex: 1; height: 64px; border: none; border-radius: 18px; background: var(--card); color: var(--text); font-size: 28px; touch-action: none; -webkit-tap-highlight-color: transparent; }
  .sg-arrow.on { background: var(--accent); }
  .sg-help { color: var(--hint); font-size: 13px; text-align: center; margin-top: 10px; }
  .sg-note { font-size: 13px; color: #ffd60a; margin: -4px 0 10px; }
  .sg-modal { position: fixed; inset: 0; z-index: 1000; background: rgba(0, 0, 0, 0.6); display: flex; align-items: center; justify-content: center; padding: 16px; }
  .sg-sheet { width: 100%; max-width: 420px; max-height: 85vh; overflow-y: auto; background: var(--bg); border: 1px solid var(--border); border-radius: 20px; padding: 18px 16px; text-align: center; }
  .sg-sheet .sg-btn { margin-top: 12px; width: 100%; }
  .sg-board { margin-top: 8px; font-size: 14px; }
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
      <div class="sg-pad">
        <button class="sg-arrow" data-act="jump" aria-label="Прыжок">▲</button>
        <button class="sg-arrow" data-act="duck" aria-label="Пригнуться">▼</button>
      </div>
      <div class="sg-help">Можно и по экрану: тап - прыжок, свайп вниз - пригнуться</div>
    </div>`;
    canvas = root.querySelector("canvas");
    ctx = canvas.getContext("2d");
    ctx.imageSmoothingEnabled = false;
    over = root.querySelector(".sg-over");
    hud = { stage: root.querySelector(".sg-stage"), lives: root.querySelector(".sg-lives"),
      score: root.querySelector(".sg-score"), goal: root.querySelector(".sg-goal") };
    const view = root.querySelector(".sg-view");
    view.addEventListener("touchstart", onTouchStart, { passive: false });
    view.addEventListener("touchmove", onTouchMove, { passive: false });
    view.addEventListener("touchend", onTouchEnd, { passive: false });
    view.addEventListener("touchcancel", onTouchEnd, { passive: false });
    for (const btn of root.querySelectorAll(".sg-arrow")) {
      const act = btn.dataset.act;
      const press = (e) => {
        e.preventDefault();
        btn.classList.add("on");
        if (!running) return;
        if (act === "jump") jump();
        else { input.duckHeld = true; input.duckUntil = 0; }
      };
      const release = (e) => {
        e.preventDefault();
        btn.classList.remove("on");
        if (act === "jump") releaseJump(); else input.duckHeld = false;
      };
      btn.addEventListener("pointerdown", press);
      btn.addEventListener("pointerup", release);
      btn.addEventListener("pointercancel", release);
      btn.addEventListener("pointerleave", release);
      btn.addEventListener("contextmenu", (e) => e.preventDefault());
    }
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
