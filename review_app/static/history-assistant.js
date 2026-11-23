const form = document.querySelector("#history-question-form");
const output = document.querySelector("#history-answer");
const question = form.elements.question;
const recordButton = document.querySelector("#record-question");
const readButton = document.querySelector("#read-answer");
const voiceStatus = document.querySelector("#history-voice-status");
const speech = window.speechSynthesis;
let briefing = "";
let recorder;
let recordTimer;

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function show(result) {
  const {facts, gaps, next_cursor: next} = result.result;
  output.replaceChildren(element("h2", facts.length ? "Evidence-backed answer" : "Answer"));
  if (!facts.length) output.append(element("p", result.answer));
  if (facts.length) {
    const rows = element("ol", "", "history-answer-facts");
    for (const fact of facts) {
      const item = element("li");
      item.append(element("p", fact.text));
      for (const cite of fact.citations) {
        const version = cite.locator.version ? " · version " + cite.locator.version : "";
        const link = element("a", cite.source + " · " + cite.locator.value + version, "history-citation");
        link.href = cite.original_url;
        item.append(link);
      }
      const identity = fact.citations[0]?.identity;
      if (fact.kind === "conflict" && identity) {
        const link = element("a", "Reviewed identity registry · version " + identity.registry_version);
        link.href = identity.registry_url;
        item.append(link);
      }
      rows.append(item);
    }
    output.append(rows);
  }
  if (gaps.length || next) {
    output.append(element("h3", "Coverage"));
    const notes = element("ul", "", "history-answer-gaps");
    for (const gap of gaps) notes.append(element("li", gap));
    if (next) notes.append(element("li", "More findings are available through the paginated history API."));
    output.append(notes);
  }
}

form.addEventListener("submit", async event => {
  event.preventDefault();
  const button = form.querySelector("button[type=submit]");
  const values = Object.fromEntries(new FormData(form));
  for (const key of ["as_of", "baseline_known_at"]) {
    if (!values[key].trim()) values[key] = null;
  }
  button.disabled = true;
  readButton.disabled = true;
  speech?.cancel();
  output.replaceChildren(element("p", "Checking original evidence…"));
  try {
    const response = await fetch(form.action, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(values),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.error?.message || "Check the timestamps and try again.");
    show(body);
    const {facts, gaps, next_cursor: more} = body.result;
    const details = facts.slice(0, 3).map(fact => fact.text);
    if (facts.length > 3 || more) details.push("See the screen for additional findings.");
    if (gaps.length) details.push("See coverage notes on screen for gaps in the evidence.");
    briefing = facts.length ? details.join(" ") : [body.answer, ...details].join(" ");
    readButton.disabled = !speech;
  } catch (error) {
    output.replaceChildren(element("p", error.message));
  } finally {
    button.disabled = false;
    question.focus();
  }
});

recordButton.addEventListener("click", async () => {
  if (recorder) {
    if (recorder.state === "recording") recorder.stop();
    return;
  }
  const mime = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/mp4"]
    .find(type => window.MediaRecorder?.isTypeSupported(type));
  if (!navigator.mediaDevices?.getUserMedia || !mime) {
    voiceStatus.textContent = "Recording is unavailable here. Open Roxi on localhost or type your question.";
    return;
  }
  let microphone;
  recordButton.disabled = true;
  try {
    microphone = await navigator.mediaDevices.getUserMedia({audio: true});
    const chunks = [];
    recorder = new MediaRecorder(microphone, {mimeType: mime});
    recorder.addEventListener("dataavailable", event => chunks.push(event.data));
    recorder.addEventListener("error", () => {
      chunks.length = 0;
      voiceStatus.textContent = "Recording failed. Try again or type your question.";
    });
    recorder.addEventListener("stop", async () => {
      clearTimeout(recordTimer);
      microphone.getTracks().forEach(track => track.stop());
      recordButton.textContent = "Record question";
      recordButton.disabled = true;
      recorder = null;
      try {
        if (!chunks.some(chunk => chunk.size)) return;
        voiceStatus.textContent = "Transcribing on this device…";
        const recording = new Blob(chunks, {type: mime});
        const response = await fetch(recordButton.dataset.url, {method: "POST", body: recording});
        const body = await response.json();
        if (!response.ok) throw new Error(body.error?.message || body.detail || "Unable to transcribe the recording.");
        question.value = body.question;
        question.focus();
        voiceStatus.textContent = "Review your transcript before asking.";
      } catch (error) {
        voiceStatus.textContent = error.message;
      } finally {
        recordButton.disabled = false;
      }
    }, {once: true});
    speech?.cancel();
    recorder.start();
    recordTimer = setTimeout(() => {
      if (recorder?.state === "recording") recorder.stop();
    }, 15_000);
    recordButton.textContent = "Stop recording";
    voiceStatus.textContent = "Recording for up to 15 seconds.";
  } catch {
    microphone?.getTracks().forEach(track => track.stop());
    recorder = null;
    voiceStatus.textContent = "Microphone access was unavailable. Try again or type your question.";
  } finally {
    recordButton.disabled = false;
  }
});

readButton.addEventListener("click", () => {
  const voice = speech?.getVoices().find(candidate => candidate.localService && /^en\b/i.test(candidate.lang));
  if (!voice) {
    voiceStatus.textContent = "No local English system voice is available. The full answer is on screen.";
    return;
  }
  speech.cancel();
  const utterance = new SpeechSynthesisUtterance(briefing);
  utterance.voice = voice;
  utterance.onerror = event => {
    if (event.error !== "canceled" && event.error !== "interrupted") {
      voiceStatus.textContent = "The system voice could not play. The full answer is on screen.";
    }
  };
  speech.speak(utterance);
});
