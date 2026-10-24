const id = new URLSearchParams(location.search).get("id");
const title = document.getElementById("recording-title");
const meta = document.getElementById("recording-meta");
const status = document.getElementById("recording-status");
const video = document.getElementById("recording-video");
const messages = document.getElementById("chat-messages");
const form = document.getElementById("chat-form");
const input = document.getElementById("chat-input");
const submit = document.getElementById("chat-submit");
const notice = document.getElementById("investigation-message");

function setNotice(text, kind = "") {
  notice.textContent = text;
  notice.className = `form-message${kind ? ` form-message--${kind}` : ""}`;
}

function timestamp(ms) {
  return new Date(ms).toLocaleString([], {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}

function renderMessage(message) {
  const article = document.createElement("article");
  article.className = `investigation__message investigation__message--${message.role}`;
  const body = document.createElement("p");
  body.textContent = message.content;
  article.append(body);
  (message.evidence || []).forEach((evidence) => {
    const link = document.createElement("button");
    link.type = "button";
    link.className = "investigation__evidence";
    link.textContent = `${timestamp(evidence.timestamp_ms)} · ${evidence.class_name} · ${(evidence.confidence * 100).toFixed(1)}%`;
    link.addEventListener("click", () => {
      const seconds = evidence.timestamp_ms / 1000;
      video.currentTime = seconds;
      video.play().catch(() => {});
      video.scrollIntoView({ behavior: "smooth", block: "center" });
    });
    article.append(link);
  });
  messages.append(article);
}

async function load() {
  if (!id) throw new Error("recording id is missing");
  const response = await fetch(`/api/uploads/${encodeURIComponent(id)}/investigation`, { cache: "no-store" });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || "investigation could not be loaded");
  const recording = payload.recording;
  title.textContent = recording.original_name;
  meta.textContent = `${recording.camera_name} · ${new Date(recording.recording_at).toLocaleString()}`;
  status.textContent = recording.processing_status === "completed"
    ? `Analysis completed · ${recording.detection_count || 0} detections`
    : recording.processing_status;
  video.src = recording.file_url;
  if (recording.processing_status !== "completed") {
    submit.disabled = true;
    setNotice("Run video analysis before asking questions about this recording.", "warning");
  }
  messages.replaceChildren();
  payload.messages.forEach(renderMessage);
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = input.value.trim();
  if (!message || submit.disabled) return;
  submit.disabled = true;
  setNotice("Checking persisted evidence…");
  try {
    const response = await fetch(`/api/uploads/${encodeURIComponent(id)}/investigation`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "investigation request failed");
    renderMessage({ role: "user", content: message, evidence: [] });
    renderMessage(payload);
    input.value = "";
    setNotice("Answer grounded in this recording's persisted evidence.", "success");
  } catch (error) {
    setNotice(error.message, "error");
  } finally {
    submit.disabled = false;
  }
});

load().catch((error) => setNotice(error.message, "error"));
