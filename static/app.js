/**
 * static/app.js
 * Minimal chat client with tab switching (Chat & Docs).
 */

document.addEventListener("DOMContentLoaded", () => {
  const chatMessages = document.getElementById("chatMessages");
  const chatForm = document.getElementById("chatForm");
  const messageInput = document.getElementById("messageInput");
  const sendBtn = document.getElementById("sendBtn");
  const clearChatBtn = document.getElementById("clearChatBtn");
  const studentChip = document.getElementById("studentStatusChip");
  const chipStudentName = document.getElementById("chipStudentName");
  const chipRegNo = document.getElementById("chipRegNo");
  
  const tabChat = document.getElementById("tabChat");
  const tabDocs = document.getElementById("tabDocs");
  const chatSection = document.getElementById("chatSection");
  const docsSection = document.getElementById("docsSection");

  // Per-browser session id so each visitor gets their own VTOP connection.
  let USER_ID;
  try {
    USER_ID = localStorage.getItem("vitopia_user_id");
    if (!USER_ID) {
      USER_ID = "u_" + Math.random().toString(36).slice(2) + Date.now().toString(36);
      localStorage.setItem("vitopia_user_id", USER_ID);
    }
  } catch (e) {
    USER_ID = "u_" + Math.random().toString(36).slice(2);
  }

  // Tab switching
  if (tabChat && tabDocs) {
    tabChat.addEventListener("click", () => {
      tabChat.classList.add("active");
      tabDocs.classList.remove("active");
      chatSection.style.display = "flex";
      docsSection.classList.remove("active");
    });

    tabDocs.addEventListener("click", () => {
      tabDocs.classList.add("active");
      tabChat.classList.remove("active");
      chatSection.style.display = "none";
      docsSection.classList.add("active");
    });
  }

  // Auto-resize textarea
  messageInput.addEventListener("input", () => {
    messageInput.style.height = "auto";
    messageInput.style.height = Math.min(messageInput.scrollHeight, 120) + "px";
  });

  // Prompt chips
  document.querySelectorAll(".chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      const prompt = chip.getAttribute("data-prompt");
      if (prompt) {
        messageInput.value = prompt;
        sendMessage(prompt);
      }
    });
  });

  // Submit on Enter (without Shift)
  messageInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      const text = messageInput.value.trim();
      if (text) {
        sendMessage(text);
      }
    }
  });

  // Form submission
  chatForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = messageInput.value.trim();
    if (text) {
      sendMessage(text);
    }
  });

  // Clear chat
  clearChatBtn.addEventListener("click", async () => {
    if (confirm("Clear conversation history?")) {
      try {
        await fetch(`/api/chat/clear/${USER_ID}`, { method: "POST" });
        chatMessages.innerHTML = `
          <div class="message-wrapper assistant">
            <div class="message-content">
              <div class="message-body"><p>Conversation cleared.</p></div>
            </div>
          </div>
        `;
      } catch (err) {
        console.error("Error clearing chat:", err);
      }
    }
  });

  async function sendMessage(text) {
    appendUserMessage(text);
    messageInput.value = "";
    messageInput.style.height = "auto";

    const loadingElem = appendLoadingMessage();
    scrollToBottom();

    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          user_id: USER_ID,
          message: text
        })
      });

      const data = await res.json();
      loadingElem.remove();

      if (data.response) {
        appendAssistantMessage(data.response, data.captcha_image);
        updateStudentChip(data.student_profile);
      } else {
        appendAssistantMessage("⚠️ " + (data.detail || "Unable to process request."));
      }
    } catch (err) {
      loadingElem.remove();
      appendAssistantMessage("❌ Network error: Could not reach backend server.");
      console.error("Chat error:", err);
    }

    scrollToBottom();
  }

  function appendUserMessage(text) {
    const timeStr = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    const div = document.createElement("div");
    div.className = "message-wrapper user";
    div.innerHTML = `
      <div class="message-content">
        <div class="message-body">${escapeHTML(text)}</div>
        <span class="message-time">${timeStr}</span>
      </div>
    `;
    chatMessages.appendChild(div);
  }

  function appendAssistantMessage(rawMarkdown, captchaImage) {
    const timeStr = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    const div = document.createElement("div");
    div.className = "message-wrapper assistant";
    let bodyHTML = typeof marked !== "undefined" ? marked.parse(rawMarkdown) : escapeHTML(rawMarkdown);
    if (captchaImage) {
      bodyHTML += `<div style="background:#fff;padding:6px;display:inline-block;margin:8px 0;border:1px solid var(--border);border-radius:4px;">
        <img src="${captchaImage}" alt="VTOP CAPTCHA" style="height:44px;display:block;"></div>`;
    }

    div.innerHTML = `
      <div class="message-content">
        <div class="message-body">${bodyHTML}</div>
        <span class="message-time">${timeStr}</span>
      </div>
    `;
    chatMessages.appendChild(div);
  }

  function appendLoadingMessage() {
    const div = document.createElement("div");
    div.className = "message-wrapper assistant loading";
    div.innerHTML = `
      <div class="message-content">
        <div class="message-body">Thinking&hellip;</div>
      </div>
    `;
    chatMessages.appendChild(div);
    return div;
  }

  function updateStudentChip(profile) {
    if (profile && profile.register_no) {
      if (chipStudentName) chipStudentName.textContent = profile.student_name || "Connected";
      if (chipRegNo) chipRegNo.textContent = profile.register_no;
      if (studentChip) studentChip.classList.add("connected");
    } else {
      if (chipStudentName) chipStudentName.textContent = "Not connected";
      if (chipRegNo) chipRegNo.textContent = "";
      if (studentChip) studentChip.classList.remove("connected");
    }
  }

  function scrollToBottom() {
    chatMessages.scrollTop = chatMessages.scrollHeight;
  }

  function escapeHTML(str) {
    return str.replace(/[&<>'"]/g, 
      tag => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[tag] || tag)
    );
  }
});
