"use strict";

const selectedEvent = document.getElementById(location.hash.slice(1));
if (selectedEvent instanceof HTMLDetailsElement) selectedEvent.open = true;

for (const form of document.querySelectorAll("[data-review-write]")) {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = form.querySelector("button[type='submit']");
    const error = form.querySelector(".form-error");
    const fields = new FormData(form);
    const action = form.dataset.reviewWrite;
    const version = fields.get("version");
    const payload = { version: version === "" ? null : Number(version) };

    if (action === "event") {
      const target = fields.get("target_id");
      Object.assign(payload, {
        subject_id: fields.get("subject_id") || null,
        reported_status: fields.get("reported_status") || null,
        state: fields.get("state"),
        target_id: target ? Number(target) : null,
      });
    } else if (action === "transcript") {
      payload.body = fields.get("body");
    }

    button.disabled = true;
    error.hidden = true;
    try {
      const response = await fetch(form.action, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await response.json();
      if (!response.ok) {
        const message = typeof data.detail === "string" ? data.detail : data.detail?.[0]?.msg;
        throw new Error(message || "Could not save the change.");
      }
      if (action === "publish") {
        location.assign(data.url);
        return;
      }
      if (action === "event") location.hash = "event-" + form.dataset.eventId;
      location.reload();
    } catch (failure) {
      error.textContent = failure.message || "Could not reach the local app.";
      error.hidden = false;
      button.disabled = false;
    }
  });
}
