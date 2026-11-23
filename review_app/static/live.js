"use strict";

const panel = document.querySelector("[data-live-exercise]");
const status = panel.querySelector("[data-live-status]");
const items = panel.querySelector("[data-live-items]");
const empty = panel.querySelector("[data-live-empty]");
const review = panel.dataset.liveView === "review";
const subjects = JSON.parse(panel.dataset.liveSubjects);
const base = "/exercises/" + encodeURIComponent(panel.dataset.liveExercise) + "/live";
const classifications = {
  status_report: "Report",
  acknowledgment: "Acknowledgment",
  correction: "Correction",
  chatter: "Chatter",
};
const limit = 60;
const messages = new Map();

function node(tag, text, className) {
  const element = document.createElement(tag);
  element.textContent = text;
  if (className) element.className = className;
  return element;
}

function clock(milliseconds) {
  const seconds = Math.floor(milliseconds / 1000);
  const minutes = Math.floor(seconds / 60);
  return String(minutes).padStart(2, "0") + ":" + String(seconds % 60).padStart(2, "0")
    + "." + String(milliseconds % 1000).padStart(3, "0");
}

function row(message) {
  const item = node("li", "", "live-message");
  item.dataset.messageId = message.message_id;
  item.dataset.version = message.version;
  const header = node("div", "", "live-message-header");
  const heading = node("a", message.sender);
  heading.href = base + "/evidence/" + message.id;
  header.append(node("span", clock(message.sent_ms), "time"), heading,
    node("span", classifications[message.kind], "live-kind " + message.kind));
  if (message.version > 1) header.append(node("span", "Edited v" + message.version, "live-marker"));
  if (message.late) header.append(node("span", "Late arrival", "live-marker"));
  const detail = [subjects[message.subject_id], message.reported_status?.replaceAll("_", " ")].filter(Boolean);
  if (detail.length) header.append(node("span", detail.join(" · "), "live-marker"));
  item.append(header, node("p", message.body));
  return item;
}

function render() {
  const ordered = [...messages.values()].sort((a, b) => b.sent_ms - a.sent_ms
    || (a.message_id < b.message_id ? 1 : a.message_id > b.message_id ? -1 : 0));
  for (const message of ordered.slice(limit)) messages.delete(message.message_id);
  const visible = ordered.slice(0, limit).filter(message => !review || message.kind !== "chatter");
  const focused = items.contains(document.activeElement) ? document.activeElement.href : null;
  items.replaceChildren(...visible.map(row));
  if (focused) [...items.querySelectorAll("a")].find(link => link.href === focused)?.focus({preventScroll: true});
  empty.hidden = visible.length > 0;
}

function apply(event) {
  if (event.type === "snapshot") messages.clear();
  let changed = event.type === "snapshot";
  for (const item of JSON.parse(event.data)) {
    const previous = messages.get(item.message_id);
    if (!previous || item.version > previous.version) {
      messages.set(item.message_id, item);
      changed = true;
    }
  }
  if (changed) render();
}

const feed = new EventSource(base + "/feed");
feed.onopen = () => { status.textContent = "Connected"; };
feed.onerror = () => { status.textContent = "Reconnecting…"; };
feed.addEventListener("snapshot", apply);
feed.addEventListener("updates", apply);
window.addEventListener("pagehide", event => { if (!event.persisted) feed.close(); });
