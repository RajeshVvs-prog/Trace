const selector = document.getElementById("recording-investigation");
const status = document.getElementById("recording-investigation-status");
const playback = document.getElementById("recording-investigation-playback");
const compose = document.getElementById("compose");
const input = document.getElementById("input");
const thread = document.getElementById("thread");
let recording = null;

function show(text, kind = "") {
  status.textContent = text;
  status.dataset.kind = kind;
}

function addMessage(role, text, evidence = []) {
  const item = document.createElement("article");
  item.className = `inv-recording-message inv-recording-message--${role}`;
  const body = document.createElement("p");
  body.textContent = text;
  item.appendChild(body);
  evidence.forEach((entry) => {
    const link = document.createElement("a");
    link.href = `/api/uploads/${encodeURIComponent(recording.id)}/file#t=${entry.timestamp_ms / 1000}`;
    link.target = "_blank";
    link.textContent = `${new Date(entry.timestamp_ms).toLocaleString()} · ${entry.class_name}`;
    item.appendChild(link);
  });
  thread.appendChild(item);
  item.scrollIntoView({ block: "nearest" });
}

async function loadRecordings() {
  const response = await fetch("/api/uploads", { cache: "no-store" });
  if (!response.ok) throw new Error("uploaded recordings unavailable");
  const recordings = await response.json();
  recordings.forEach((item) => selector.appendChild(new Option(
    `${item.original_name} · ${item.processing_status}`, item.id,
  )));
}

selector.addEventListener("change", async () => {
  recording = null;
  const id = selector.value;
  if (!id) {
    show("Select an analyzed upload to scope this investigation.");
    playback.hidden = true;
    return;
  }
  const response = await fetch(`/api/uploads/${encodeURIComponent(id)}/investigation`, { cache: "no-store" });
  const payload = await response.json();
  if (!response.ok) {
    show(payload.detail || "recording investigation unavailable", "error");
    return;
  }
  recording = payload.recording;
  playback.href = `playback.html?recording=${encodeURIComponent(recording.id)}`;
  playback.hidden = false;
  show(`${recording.original_name} · ${recording.processing_status} · ${recording.detection_count || 0} detections`);
  if (recording.processing_status === "completed") {
    payload.messages.forEach((message) => addMessage(message.role, message.content, message.evidence));
  }
});

compose.addEventListener("submit", async (event) => {
  if (!recording) return;
  event.preventDefault();
  event.stopImmediatePropagation();
  const message = input.value.trim();
  if (!message) return;
  addMessage("user", message);
  input.value = "";
  try {
    const response = await fetch(`/api/uploads/${encodeURIComponent(recording.id)}/investigation`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
    }, true);
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "investigation request failed");
    addMessage("assistant", payload.content, payload.evidence);
  } catch (error) {
    addMessage("assistant", error.message);
  }
});

loadRecordings().catch((error) => show(error.message, "error"));
