"use strict";

const rows = [...document.querySelectorAll("[data-record]")];
const filters = [...document.querySelectorAll("[data-filter]")];
const markers = [...document.querySelectorAll("[data-marker]")];
const search = document.querySelector("#record-search");
const empty = document.querySelector("#no-results");
const recordList = document.querySelector(".records-scroll");
let filter = "all";

function filterRows() {
  const query = search.value.trim().toLocaleLowerCase();
  let count = 0;
  for (const row of rows) {
    row.hidden = (filter !== "all" && row.dataset.record !== filter)
      || !row.textContent.toLocaleLowerCase().includes(query);
    if (!row.hidden) count += 1;
  }
  for (const marker of markers) {
    marker.hidden = document.getElementById(marker.dataset.marker).hidden;
  }
  empty.hidden = count > 0;
}

for (const button of filters) {
  button.addEventListener("click", () => {
    filter = button.dataset.filter;
    for (const item of filters) item.setAttribute("aria-pressed", String(item === button));
    filterRows();
  });
}
search.addEventListener("input", filterRows);

const audio = document.querySelector("#recording");
const play = document.querySelector("#play-clip");
const scrub = document.querySelector("#clip-position");
const elapsed = document.querySelector("#clip-current");
const duration = document.querySelector("#clip-duration");
const speaker = document.querySelector("#clip-speaker");
const transcript = document.querySelector("#clip-text");
const clock = document.querySelector("#clip-clock");
const offset = document.querySelector("#clip-offset");
const error = document.querySelector("#audio-error");
const panel = document.querySelector("#audio-panel");

let selected;
let frameId = 0;

function formatTime(seconds, showFraction = false) {
  const milliseconds = Math.round(seconds * 1000);
  const whole = Math.floor(milliseconds / 1000);
  const minutes = String(Math.floor(whole / 60)).padStart(2, "0");
  const remainder = String(whole % 60).padStart(2, "0");
  const fractional = milliseconds % 1000;
  const decimal = showFraction && fractional
    ? "." + String(fractional).padStart(3, "0").replace(/0+$/, "")
    : "";
  return minutes + ":" + remainder + decimal;
}

function showPlaying(playing) {
  play.querySelector("span").textContent = playing ? "Pause clip" : "Play clip";
  play.querySelector(".play-icon").hidden = playing;
  play.querySelector(".pause-icon").hidden = !playing;
}

function selectClip(button, reveal = false) {
  audio.pause();
  if (selected) selected.button.setAttribute("aria-pressed", "false");
  const data = button.dataset;
  selected = { button, start: Number(data.start) / 1000, end: Number(data.end) / 1000 };
  button.setAttribute("aria-pressed", "true");
  for (const row of rows) row.classList.toggle("is-selected", row.id === data.key);
  for (const marker of markers) marker.classList.toggle("is-active", marker.dataset.marker === data.key);

  if (audio.getAttribute("src") !== data.src) audio.src = data.src;
  audio.currentTime = selected.start;
  speaker.textContent = data.speaker;
  transcript.textContent = data.transcript;
  clock.textContent = data.clock;
  offset.textContent = data.offset;
  scrub.max = String((selected.end - selected.start).toFixed(3));
  scrub.value = "0";
  elapsed.value = "00:00";
  duration.textContent = formatTime(Number(scrub.max), true);
  error.hidden = true;
  if (reveal && window.matchMedia("(max-width: 900px)").matches) {
    panel.scrollIntoView({ block: "nearest" });
  }
}

function updateProgress() {
  if (!selected) return;
  const relative = Math.min(Number(scrub.max), Math.max(0, audio.currentTime - selected.start));
  scrub.value = String(relative);
  elapsed.value = formatTime(relative, relative === Number(scrub.max));
  if (!audio.paused && audio.currentTime >= selected.end) {
    audio.pause();
    audio.currentTime = selected.end;
  }
}

function displayProgress() {
  updateProgress();
  if (!audio.paused) frameId = requestAnimationFrame(displayProgress);
}

if (audio) {
  const initial = document.querySelector('[data-clip][data-key="' + audio.dataset.initial + '"]');
  selectClip(initial);

  play.addEventListener("click", () => {
    if (!audio.paused) {
      audio.pause();
      return;
    }
    error.hidden = true;
    if (audio.currentTime < selected.start || audio.currentTime >= selected.end - 0.005) {
      audio.currentTime = selected.start;
    }
    audio.play().catch((failure) => {
      if (failure.name === "AbortError") return;
      error.textContent = "This recording could not be played.";
      error.hidden = false;
    });
  });
  scrub.addEventListener("input", () => {
    audio.currentTime = selected.start + Number(scrub.value);
    updateProgress();
  });
  audio.addEventListener("timeupdate", updateProgress);
  audio.addEventListener("play", () => {
    showPlaying(true);
    cancelAnimationFrame(frameId);
    frameId = requestAnimationFrame(displayProgress);
  });
  audio.addEventListener("pause", () => {
    cancelAnimationFrame(frameId);
    showPlaying(false);
  });
  audio.addEventListener("ended", () => showPlaying(false));
  audio.addEventListener("error", () => {
    error.textContent = "The audio file could not be loaded.";
    error.hidden = false;
  });
  for (const button of document.querySelectorAll("[data-clip]")) {
    button.addEventListener("click", () => selectClip(button, true));
  }
  for (const row of rows.filter((item) => item.dataset.record === "audio")) {
    row.addEventListener("click", (event) => {
      if (!event.target.closest("button")) selectClip(row.querySelector("[data-clip]"), true);
    });
  }
}

for (const marker of markers) {
  marker.addEventListener("click", () => {
    const row = document.getElementById(marker.dataset.marker);
    const button = row.querySelector("[data-clip]");
    if (button) {
      selectClip(button);
    } else {
      for (const item of rows) item.classList.toggle("is-selected", item === row);
      for (const item of markers) item.classList.toggle("is-active", item === marker);
    }
    const rowBox = row.getBoundingClientRect();
    const listBox = recordList.getBoundingClientRect();
    recordList.scrollTop += rowBox.top - listBox.top - (listBox.height - rowBox.height) / 2;
  });
}
