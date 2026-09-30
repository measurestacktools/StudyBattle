// StudyBattle frontend — vanilla JS. NEVER stores API keys (no localStorage/sessionStorage for keys).
let difficulty = "Medium";
let currentOptions = [];
let timerId = null;
let qStart = 0;
let timeLimit = 15000;

const $ = (id) => document.getElementById(id);

function show(id) {
  document.querySelectorAll(".screen").forEach((s) => s.classList.remove("active"));
  $(id).classList.add("active");
}
function setHud(d) {
  if (d.score !== undefined) $("hudScore").textContent = d.score;
  if (d.xp_total !== undefined) $("hudXp").textContent = d.xp_total;
  if (d.xp !== undefined) $("hudXp").textContent = d.xp;
  if (d.level !== undefined) $("hudLevel").textContent = d.level;
  if (d.streak !== undefined) $("hudStreak").textContent = d.streak;
  $("hudCombo").textContent = d.combo && d.combo > 1 ? `×${d.combo}` : "";
}
async function api(path, method = "GET", body) {
  const r = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || data.detail || `Request failed (${r.status})`);
  return data;
}

// Setup
$("inpTopic").addEventListener("input", (e) => { $("topicCount").textContent = `${e.target.value.length}/200`; });
document.querySelectorAll("#diffRow .diff").forEach((b) => {
  b.addEventListener("click", () => {
    document.querySelectorAll("#diffRow .diff").forEach((x) => x.classList.remove("sel"));
    b.classList.add("sel");
    difficulty = b.dataset.d;
  });
});
$("btnStart").addEventListener("click", async () => {
  $("setupErr").textContent = "";
  const topic = $("inpTopic").value.trim();
  if (!topic) { $("setupErr").textContent = "Enter a subject/topic to battle!"; return; }
  if (topic.length > 200) { $("setupErr").textContent = "Topic must be ≤ 200 characters."; return; }
  $("btnStart").textContent = "SUMMONING AI...";
  try {
    const d = await api("/api/start", "POST", { topic, difficulty });
    $("setupFlavor").textContent = d.flavor || "";
    setHud({ score: 0, xp: 0, level: 1, streak: 0, combo: 1 });
    await loadQuestion();
  } catch (e) { $("setupErr").textContent = e.message; }
  $("btnStart").textContent = "▶ START BATTLE";
});

function startTimer(ms) {
  clearInterval(timerId);
  timeLimit = ms; qStart = Date.now();
  const bar = $("timerBar");
  timerId = setInterval(() => {
    const el = Date.now() - qStart;
    const left = Math.max(0, 1 - el / ms);
    bar.style.width = `${left * 100}%`;
    if (el >= ms) { clearInterval(timerId); timeoutAnswer(); }
  }, 100);
}
async function loadQuestion() {
  show("screen-battle");
  $("battleErr").textContent = "";
  $("qText").textContent = "Summoning question...";
  $("qOpts").innerHTML = "";
  try {
    const q = await api("/api/question", "POST");
    currentOptions = q.options;
    $("roundLabel").textContent = `ROUND ${q.round}`;
    $("diffLabel").textContent = q.difficulty.toUpperCase();
    const boss = $("bossBadge");
    if (q.is_boss) boss.classList.remove("hidden"); else boss.classList.add("hidden");
    $("qText").textContent = (q.is_boss ? "👹 " : "") + q.question;
    const wrap = $("qOpts");
    q.options.forEach((opt) => {
      const b = document.createElement("button");
      b.className = "opt"; b.textContent = opt;
      b.addEventListener("click", () => answer(opt, b));
      wrap.appendChild(b);
    });
    startTimer(q.time_limit_ms);
  } catch (e) { $("battleErr").textContent = e.message; }
}
async function timeoutAnswer() {
  // auto-submit with empty-ish wrong choice on timeout
  await answer("__TIMEOUT__", null, true);
}
async function answer(choice, btn, isTimeout = false) {
  clearInterval(timerId);
  const elapsed = Date.now() - qStart;
  document.querySelectorAll("#qOpts .opt").forEach((b) => (b.disabled = true));
  try {
    const payloadChoice = isTimeout ? "__TIMEOUT__" : choice;
    const r = await api("/api/answer", "POST", { choice: payloadChoice, time_ms: elapsed });
    if (btn) btn.classList.add(r.correct ? "correct" : "wrong");
    // highlight right answer
    document.querySelectorAll("#qOpts .opt").forEach((b) => {
      if (b.textContent.trim().toLowerCase() === String(r.answer).trim().toLowerCase()) b.classList.add("correct");
    });
    setHud(r);
    setTimeout(() => showFeedback(r), 700);
  } catch (e) { $("battleErr").textContent = e.message; }
}
function showFeedback(r) {
  show("screen-feedback");
  $("fbTitle").textContent = r.correct ? (r.is_boss ? "👹 BOSS SLAIN!" : "⚡ CORRECT!") : "💥 HIT TAKEN!";
  $("fbTitle").style.color = r.correct ? "#39ff88" : "#ff4d5e";
  $("fbXp").textContent = r.correct ? `+${r.xp_earned} XP` : "+0 XP";
  $("fbExpl").textContent = `Answer: ${r.answer} — ${r.explanation}`;
  let extra = `Streak ${r.streak} · Combo ×${r.combo} · Level ${r.level}`;
  if (r.adaptation === "difficulty_eased") extra += " · AI eased off (2 misses)";
  if (r.adaptation === "difficulty_raised") extra += " · AI powers up (streak ≥ 5)!";
  if (r.hint) extra += ` · ${r.hint}`;
  $("fbHint").textContent = extra;
}
$("btnNext").addEventListener("click", loadQuestion);

// Finish flow: auto-finish after N rounds? Provide finish via keyboard? Add finish on feedback after round>=3 via long-press? Simpler: NEXT goes on; add FINISH button on battle via double-click round label.
$("roundLabel").addEventListener("dblclick", finishGame);
$("btnEndBattle").addEventListener("click", finishGame);
async function finishGame() {
  try {
    const f = await api("/api/finish", "POST");
    $("fScore").textContent = f.score;
    $("fXp").textContent = f.xp_total;
    $("fLevel").textContent = f.level;
    $("fAcc").textContent = `${f.accuracy}% (${f.correct}/${f.total})`;
    $("fStreak").textContent = f.best_streak;
    $("fWeak").innerHTML = f.weak_topics.length
      ? f.weak_topics.map((w) => `<span>${w}</span>`).join("")
      : "<span>None — flawless!</span>";
    show("screen-final");
  } catch (e) { $("battleErr").textContent = e.message; }
}
$("btnFinish").addEventListener("click", () => show("screen-setup"));
$("btnReplay").addEventListener("click", async () => {
  const topic = $("inpTopic").value.trim() || "General Knowledge";
  try {
    await api("/api/start", "POST", { topic, difficulty });
    setHud({ score: 0, xp: 0, level: 1, streak: 0, combo: 1 });
    await loadQuestion();
  } catch (e) { show("screen-setup"); $("setupErr").textContent = e.message; }
});
$("btnCopy").addEventListener("click", async () => {
  const t = `STUDY BATTLE RESULTS — Score ${$("fScore").textContent}, XP ${$("fXp").textContent}, Level ${$("fLevel").textContent}, Accuracy ${$("fAcc").textContent}, Best streak ${$("fStreak").textContent}, Weak: ${$("fWeak").textContent}`;
  try { await navigator.clipboard.writeText(t); $("copyMsg").textContent = "Copied!"; }
  catch { $("copyMsg").textContent = t; }
});

// Key modal (no browser storage of keys — key lives only in POST body, then discarded)
$("btnKey").addEventListener("click", async () => {
  $("keyModal").classList.remove("hidden");
  $("inpKey").value = "";
  try { const s = await api("/api/status"); $("keyStatus").textContent = `status: ${s.has_key ? "set (" + s.source + ")" : "not set"} · ${s.model}`; }
  catch { $("keyStatus").textContent = "status: ?"; }
});
$("btnCloseKey").addEventListener("click", () => $("keyModal").classList.add("hidden"));
$("btnSaveKey").addEventListener("click", async () => {
  $("keyMsg").textContent = "Verifying...";
  const k = $("inpKey").value;
  try {
    await api("/api/key", "POST", { key: k });
    $("keyMsg").textContent = "Key saved in server memory ✓";
    $("inpKey").value = "";
  } catch (e) { $("keyMsg").textContent = e.message; }
});
$("btnClearKey").addEventListener("click", async () => {
  try { await api("/api/key", "DELETE"); $("keyMsg").textContent = "Key cleared."; }
  catch (e) { $("keyMsg").textContent = e.message; }
});
