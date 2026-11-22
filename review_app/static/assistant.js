"use strict";

const transcript = document.querySelector("#messages");
const form = document.querySelector("#question-form");
const field = document.querySelector("#question");
const submit = form.querySelector("button");

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function showAnswer(container, result) {
  container.replaceChildren(element("p", result.answer));
  const status = result.verification.status;
  const verification = {
    supported: "Evidence verified",
    contradicted: "Evidence disagrees",
    insufficient_evidence: "Evidence insufficient",
  }[status];
  container.append(element("p", verification, "verdict verdict-" + status));
  container.append(element("p", result.verification.reason, "verifier-detail"));

  if (result.citations.length) {
    const sources = element("ul", "", "citation-list");
    for (const citation of result.citations) {
      const item = element("li");
      const loc = citation.locator;
      const isAudio = loc.kind === "audio_interval";
      const locationLabel = isAudio
        ? loc.cue_id || (loc.start_ms / 1000).toFixed(3) + " s"
        : "line " + loc.line_start;
      const link = element("a", citation.original_name + " · " + locationLabel);
      if (isAudio) {
        link.href = citation.playback_path;
      } else {
        link.href = "/exercises/" + result.claim.exercise_id + "/evidence/"
          + citation.source_file_id + "/" + loc.line_start + "?revision=" + result.claim.revision_id;
      }
      link.target = "_blank";
      link.rel = "noopener";
      item.append(link);
      if (result.claim.coverage_evidence_ids.includes(citation.evidence_id)) {
        item.append(element("span", "Exercise time unknown", "coverage-label"));
      }
      item.append(element("span", citation.excerpt, "citation-excerpt"));
      sources.append(item);
    }
    container.append(sources);
  }
  container.append(element("p", result.parser, "parser-note"));
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const question = field.value.trim();
  if (!question || submit.disabled) return;
  const message = element("article", question, "question-message");
  const reply = element("article", "Checking the published record…", "answer-message");
  transcript.append(message, reply);
  field.value = "";
  submit.disabled = true;
  transcript.scrollTop = transcript.scrollHeight;
  try {
    const response = await fetch(form.action, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({revision_id: Number(form.dataset.revision), question}),
    });
    const result = await response.json();
    if (!response.ok) {
      throw new Error(result.error?.message || (typeof result.detail === "string" && result.detail)
        || "The local assistant could not respond.");
    }
    showAnswer(reply, result);
  } catch (error) {
    reply.textContent = error.message;
    reply.classList.add("assistant-error");
  } finally {
    submit.disabled = false;
    transcript.scrollTop = transcript.scrollHeight;
    field.focus();
  }
});
