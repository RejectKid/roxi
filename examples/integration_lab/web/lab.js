const $ = (id) => document.getElementById(id);
const delay = (ms) => new Promise(resolve => setTimeout(resolve, ms));
const time = (value) => value ? new Date(value).toLocaleTimeString() : "—";
const position = ([lon, lat]) => [(lon + 84.2) * 10000, (lat - 39.75) * 10000];
const crews = {blue: ["Blue Van", "#6298ff"], red: ["Red Van", "#fb897b"]};

document.body.classList.toggle("assistant-only", location.pathname === "/assistant");
let current = {step: 0, steps: 4, speech_enabled: false};
let tracks = {blue: [], red: []};
let playing = false;
let advancing = false;
let activeMic = null;
let pendingChat = null;
let revision = 0;

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(typeof data.detail === "string" ? data.detail : `Request failed (${response.status}).`);
  }
  return data;
}

function post(path, body) {
  return api(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
}

function element(tag, text) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  return node;
}

function error(message) {
  $("lab-error").hidden = !message;
  $("lab-error").textContent = message;
}

function drawTracks() {
  const canvas = $("tracks");
  const bounds = canvas.getBoundingClientRect();
  if (!bounds.width) return;
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.round(bounds.width * ratio);
  canvas.height = Math.round(bounds.height * ratio);
  const ctx = canvas.getContext("2d");
  ctx.scale(ratio, ratio);
  const {width, height} = bounds;
  const left = 34, top = 13, right = width - 16, bottom = height - 30;
  const x = (index) => left + (right - left) * index / 10;
  const y = (index) => bottom - (bottom - top) * index / 6;
  ctx.font = "12px Segoe UI, sans-serif";
  ctx.fillStyle = "#d1e3dd";
  ctx.strokeStyle = "#42675e";
  for (let column = 0; column <= 10; column += 1) {
    ctx.beginPath();
    ctx.moveTo(x(column), top);
    ctx.lineTo(x(column), bottom);
    ctx.stroke();
    if (column % 2 === 0) ctx.fillText(String(column), x(column) - 4, bottom + 19);
  }
  for (let row = 0; row <= 6; row += 1) {
    ctx.beginPath();
    ctx.moveTo(left, y(row));
    ctx.lineTo(right, y(row));
    ctx.stroke();
    ctx.fillText(String(row), left - 22, y(row) + 4);
  }
  ctx.fillStyle = "#edf7f3";
  ctx.fillText("West loading", x(.3), y(5.4));
  ctx.textAlign = "right";
  ctx.fillText("East receiving", x(9.7), y(5.4));
  ctx.textAlign = "left";
  for (const [crew, list] of Object.entries(tracks)) {
    ctx.strokeStyle = ctx.fillStyle = crews[crew][1];
    ctx.lineWidth = 3;
    ctx.beginPath();
    list.forEach(([horizontal, vertical], index) => {
      if (index === 0) ctx.moveTo(x(horizontal), y(vertical));
      else ctx.lineTo(x(horizontal), y(vertical));
    });
    ctx.stroke();
    list.forEach(([horizontal, vertical], index) => {
      ctx.beginPath();
      ctx.arc(x(horizontal), y(vertical), index === list.length - 1 ? 5 : 3, 0, Math.PI * 2);
      ctx.fill();
    });
  }
  if (!tracks.blue.length && !tracks.red.length) {
    ctx.fillStyle = "#edf7f3";
    ctx.font = "15px Segoe UI, sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("Play the replay to receive fleet positions.", (left + right) / 2, (top + bottom) / 2);
  }
  const locations = Object.entries(tracks).filter(([, list]) => list.length).map(([crew, list]) =>
    `${crews[crew][0]} at grid ${list.at(-1).map(value => value.toFixed(1)).join(", ")}`);
  canvas.setAttribute("aria-label", locations.join(". ") || "Local yard grid. No fleet positions received yet.");
}

function renderStatus(status) {
  current = status;
  $("phase").textContent = `${String(status.step).padStart(2, "0")} / ${String(status.steps).padStart(2, "0")}`;
  $("step").disabled = advancing || playing || status.step === status.steps;
  $("play").disabled = status.step === status.steps && !playing;
  $("play").textContent = playing ? "Pause replay" : "Play replay";
  $("record").disabled = !status.speech_enabled || Boolean(activeMic) || audioBusy;
}

function renderRecords(records) {
  const show = records.slice(0, 6);
  $("record-empty").hidden = show.length > 0;
  $("records").replaceChildren(...show.map(record => {
    const row = element("tr");
    const button = element("button", "View JSON");
    button.addEventListener("click", () => openDialog("Structured record", element("pre", JSON.stringify(record, null, 2))));
    const action = element("td");
    action.append(button);
    row.append(element("td", record.kind.replaceAll("_", " ")), element("td", record.source.replace("catalog:", "")),
               element("td", time(record.observed_at)), action);
    return row;
  }));
}

async function refresh() {
  const version = ++revision;
  const [status, page] = await Promise.all([api("/api/lab"), api("/api/snapshot")]);
  if (version !== revision) return;
  renderStatus(status);
  const records = page.facts.flatMap(fact => fact.records);
  for (const crew of Object.keys(crews)) {
    tracks[crew] = records.filter(record => record.entity_id === crew && record.source === "catalog:fleet" && record.position)
      .reverse().map(record => position(record.position));
  }
  drawTracks();
  renderRecords(records);
  $("channel-messages").replaceChildren(...records.filter(record => record.medium).slice(0, 8).map(record =>
    element("li", `${time(record.observed_at)} · ${record.source.replace("catalog:", "")} · ${record.text}`)));
}

async function advance() {
  if (advancing || current.step >= current.steps) return;
  advancing = true;
  renderStatus(current);
  try {
    const status = await api(`/api/replay/${current.step + 1}`, {method: "POST"});
    renderStatus(status);
    await refresh();
  } finally {
    advancing = false;
    renderStatus(current);
  }
}

$("step").addEventListener("click", () => advance().catch(err => error(err.message)));
$("play").addEventListener("click", async () => {
  if (playing) {
    playing = false;
    renderStatus(current);
    return;
  }
  playing = true;
  renderStatus(current);
  try {
    while (playing && current.step < current.steps) {
      await advance();
      if (current.step < current.steps) await delay(1400);
    }
  } catch (err) {
    error(err.message);
  } finally {
    playing = false;
    renderStatus(current);
  }
});

$("chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const crew = $("crew").value, text = $("chat").value.trim();
  if (!text) return;
  if (!pendingChat || pendingChat.crew !== crew || pendingChat.text !== text) {
    pendingChat = {crew, text, key: "lab-" + crypto.randomUUID(), observedAt: new Date().toISOString()};
  }
  const pending = pendingChat;
  $("send-chat").disabled = true;
  try {
    await post(`/api/chat/${crew}?observed_at=${encodeURIComponent(pending.observedAt)}`, {key: pending.key, text});
    pendingChat = null;
    if ($("chat").value.trim() === text) $("chat").value = "";
    $("capture-status").textContent = "Chat saved and classified by Roxi.";
    await refresh();
    $("channel-messages").closest("details").open = true;
  } catch (err) {
    $("capture-status").textContent = err.message;
  } finally {
    $("send-chat").disabled = false;
  }
});

function openDialog(title, ...content) {
  $("detail-title").textContent = title;
  $("detail-content").replaceChildren(...content);
  if (!$("details").open) $("details").showModal();
}
$("details").addEventListener("close", () => $("detail-content").replaceChildren());

async function openEvidence(citation) {
  const params = new URLSearchParams({known_at: citation.known_at});
  if (citation.as_of) params.set("as_of", citation.as_of);
  const path = `/api/evidence/${citation.key}`;
  openDialog("Original evidence", element("p", "Resolving the pinned source…"));
  try {
    const evidence = await api(`${path}?${params}`);
    if (!$("details").open) return;
    const original = element("a", "Open original file");
    original.href = `${path}/original?${params}`;
    original.target = "_blank";
    original.rel = "noopener";
    const blocks = [element("p", evidence.source), original];
    if (evidence.locator.kind === "audio_interval") {
      const start = evidence.locator.start_ms / 1000, end = evidence.locator.end_ms / 1000;
      const audio = element("audio");
      audio.src = original.href;
      audio.controls = true;
      audio.addEventListener("loadedmetadata", () => { audio.currentTime = start; });
      audio.addEventListener("play", () => {
        if (audio.currentTime < start || audio.currentTime >= end) audio.currentTime = start;
      });
      audio.addEventListener("timeupdate", () => { if (audio.currentTime >= end) audio.pause(); });
      blocks.push(element("p", `Cited audio interval ${start.toFixed(2)}–${end.toFixed(2)} s`), audio);
    }
    blocks.push(element("pre", JSON.stringify(evidence.original_record, null, 2)));
    openDialog("Original evidence", ...blocks);
  } catch (err) {
    if ($("details").open) openDialog("Original evidence", element("p", err.message));
  }
}

function renderAnswer(reply) {
  $("answer").textContent = reply.answer;
  $("gaps").replaceChildren(...reply.result.gaps.map(gap => element("li", gap)));
  $("gap-box").hidden = !reply.result.gaps.length;
  const entries = reply.result.facts.flatMap(fact =>
    fact.citations.map((cite, i) => ({cite, row: fact.records[i]})));
  $("evidence-count").textContent = `(${entries.length})`;
  $("citations").replaceChildren(...entries.map(({cite, row}) => {
    const item = element("li"), button = element("button");
    button.append(element("span", cite.source.replace("catalog:", "")), element("small", time(row?.observed_at)));
    button.addEventListener("click", () => openEvidence(cite));
    item.append(button);
    return item;
  }));
}

$("question-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const question = $("question").value.trim();
  if (!question) return;
  const button = event.currentTarget.querySelector("button");
  button.disabled = true;
  $("answer").textContent = "Querying Roxi…";
  try {
    renderAnswer(await post("/api/answer", {question}));
  } catch (err) {
    renderAnswer({answer: err.message, result: {facts: [], gaps: []}});
  } finally {
    button.disabled = false;
  }
});

document.querySelectorAll(".examples button").forEach(button => button.addEventListener("click", () => {
  $("question").value = button.textContent;
  $("question-form").requestSubmit();
}));

let audioBusy = false;

function encodeWav(samples, rate) {
  const view = new DataView(new ArrayBuffer(44 + samples.length * 2));
  const tag = (offset, text) => [...text].forEach((char, index) => view.setUint8(offset + index, char.charCodeAt(0)));
  const u16 = (offset, value) => view.setUint16(offset, value, true);
  const u32 = (offset, value) => view.setUint32(offset, value, true);
  tag(0, "RIFF"); u32(4, 36 + samples.length * 2); tag(8, "WAVE"); tag(12, "fmt ");
  u32(16, 16); u16(20, 1); u16(22, 1); u32(24, rate); u32(28, rate * 2); u16(32, 2); u16(34, 16);
  tag(36, "data"); u32(40, samples.length * 2);
  samples.forEach((sample, index) => {
    const bounded = Math.max(-1, Math.min(1, sample));
    view.setInt16(44 + index * 2, Math.round(bounded * (bounded < 0 ? 32768 : 32767)), true);
  });
  return new Blob([view.buffer], {type: "audio/wav"});
}

async function microphoneWav(chunks) {
  const decode = new AudioContext();
  try {
    const data = await decode.decodeAudioData(await new Blob(chunks).arrayBuffer());
    const rate = 16000;
    const context = new OfflineAudioContext(1, Math.min(rate * 12, Math.ceil(data.duration * rate)), rate);
    const source = context.createBufferSource();
    source.buffer = data;
    source.connect(context.destination);
    source.start();
    return encodeWav((await context.startRendering()).getChannelData(0), rate);
  } finally {
    await decode.close();
  }
}

async function finishMic(capture) {
  capture.stream.getTracks().forEach(track => track.stop());
  clearTimeout(capture.timeout);
  activeMic = null;
  audioBusy = true;
  renderStatus(current);
  $("stop").disabled = true;
  const status = $("capture-status");
  try {
    status.textContent = "Encoding and sending original WAV…";
    const body = await microphoneWav(capture.chunks);
    const params = new URLSearchParams({key: capture.key, observed_at: capture.started});
    const path = `/api/microphone/${capture.crew}`;
    const receipt = await api(`${path}?${params}`, {method: "POST", headers: {"Content-Type": "audio/wav"}, body});
    status.textContent = "Original WAV saved. Waiting for transcription…";
    await refresh();
    for (let waited = 0; waited < 90; waited += 1) {
      const state = await api(`${path}/${receipt.capture_id}`);
      if (state.state !== "pending") {
        status.textContent = state.state === "accepted" ? "Radio transcript ready in Roxi."
          : `The original is saved. Transcription status: ${state.state}.`;
        await refresh();
        return;
      }
      await delay(1000);
    }
    status.textContent = "The original is saved. The speech worker is still pending.";
  } catch (err) {
    status.textContent = err.message;
  } finally {
    audioBusy = false;
    renderStatus(current);
  }
}

$("record").addEventListener("click", async () => {
  const status = $("capture-status");
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
    status.textContent = "Microphone recording needs a browser with access to local audio capture.";
    return;
  }
  audioBusy = true;
  renderStatus(current);
  let stream;
  const crew = $("crew").value;
  try {
    stream = await navigator.mediaDevices.getUserMedia({audio: true});
    const recorder = new MediaRecorder(stream), chunks = [];
    const capture = {stream, recorder, chunks, crew, key: "lab-" + crypto.randomUUID(), started: new Date().toISOString()};
    activeMic = capture;
    recorder.addEventListener("dataavailable", event => { if (event.data.size) chunks.push(event.data); });
    recorder.addEventListener("stop", () => finishMic(capture), {once: true});
    recorder.start();
    capture.timeout = setTimeout(() => { if (recorder.state === "recording") recorder.stop(); }, 12000);
    status.textContent = `Recording ${crew} crew radio. Stop & send when finished (12 seconds max).`;
    $("stop").disabled = false;
  } catch (err) {
    stream?.getTracks().forEach(track => track.stop());
    activeMic = null;
    status.textContent = `Microphone unavailable. ${err.message}`;
  } finally {
    audioBusy = false;
    renderStatus(current);
  }
});
$("stop").addEventListener("click", () => {
  $("stop").disabled = true;
  if (activeMic?.recorder.state === "recording") activeMic.recorder.stop();
});
window.addEventListener("pagehide", () => activeMic?.stream.getTracks().forEach(track => track.stop()));
new ResizeObserver(drawTracks).observe($("tracks"));
refresh().catch(err => error(err.message));
setInterval(() => refresh().catch(err => error(err.message)), 2500);
