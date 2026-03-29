marked.setOptions({ breaks: true, gfm: true });

document.getElementById("input").addEventListener("keydown", function(e) {
  if (e.key === "Enter") { e.preventDefault(); sendMessage(); }
});

function usePrompt(text) {
  document.getElementById("input").value = text;
  sendMessage();
}

function escHtml(t) {
  return String(t).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}

function processMermaid() {
  document.querySelectorAll(".bubble p, .bubble code, .bubble pre code").forEach(function(block) {
    if (block.dataset.mermaidProcessed) return;
    block.dataset.mermaidProcessed = "true";

    var text = block.innerHTML
      .replace(/<br\s*\/?>/gi, "\n")
      .replace(/&amp;/g, "&")
      .replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">")
      .replace(/&quot;/g, '"')
      .replace(/&#39;/g, "'")
      .trim();

    if (/^(graph\s+(TD|LR|BT|RL)|sequenceDiagram|classDiagram|flowchart)/i.test(text)) {
      var div = document.createElement("div");
      div.className = "mermaid";
      div.textContent = text;
      div.style.cssText = "background:#1a1a2e;padding:16px;border-radius:8px;margin:8px 0;overflow:auto;";
      var target = block.closest("pre") || block;
      target.replaceWith(div);
      if (typeof mermaid !== "undefined") {
        try { mermaid.init(undefined, [div]); }
        catch(e) { console.error("Mermaid:", e); }
      }
    }
  });
}
function processMermaid_OLD() {
  // Target both <code> blocks AND raw <p> tags (when LLM outputs raw graph syntax)
  var selectors = ["code", "pre code", "p"];
  selectors.forEach(function(sel) {
    document.querySelectorAll(sel).forEach(function(block) {
      if (block.dataset.mermaidProcessed) return;
      block.dataset.mermaidProcessed = "true";

      var text = block.innerHTML
        .replace(/<br\s*\/?>/gi, "\n")
        .replace(/&amp;/g, "&")
        .replace(/&lt;/g, "<")
        .replace(/&gt;/g, ">")
        .replace(/&quot;/g, '"')
        .replace(/&#39;/g, "'")
        .trim();

      if (/^(graph\s+(TD|LR|BT|RL)|sequenceDiagram|classDiagram|flowchart)/i.test(text)) {
        var div = document.createElement("div");
        div.className = "mermaid";
        div.textContent = text;
        div.style.cssText = "background:#1a1a2e;padding:16px;border-radius:8px;margin:8px 0;overflow:auto;";
        var target = block.closest("pre") || block;
        target.replaceWith(div);
        if (typeof mermaid !== "undefined") {
          try {
            mermaid.init(undefined, [div]);
          } catch(e) {
            console.error("Mermaid:", e);
          }
        }
      }
    });
  });
}

function sendMessage() {
  var input = document.getElementById("input");
  var question = input.value.trim();
  if (!question) return;

  var w = document.getElementById("welcome");
  if (w) w.remove();

  input.value = "";
  document.getElementById("btn-send").disabled = true;

  var chat = document.getElementById("chat");
  chat.innerHTML += '<div class="turn user"><div class="bubble">' + escHtml(question) + '</div></div>';

  var tid = "t" + Date.now();
  chat.innerHTML += '<div class="turn agent" id="' + tid + '"><div class="agent-icon">D</div><div class="bubble"><div class="thinking-dots"><span></span><span></span><span></span></div></div></div>';
  chat.scrollTop = chat.scrollHeight;

  var tokens = "";
  var cmdUsed = null;
  var cmdOutput = null;

  function renderStream() {
    var answerHtml = tokens ? marked.parse(tokens) : "";
    var cmdHtml = "";
    if (cmdUsed) {
      cmdHtml = '<div class="cmd-block"><div class="cmd-header"><div class="cmd-dot" style="background:#ff5f57"></div><div class="cmd-dot" style="background:#febc2e"></div><div class="cmd-dot" style="background:#28c840"></div><span class="cmd-label">Terminal</span></div><div class="cmd-body"><div class="cmd-line">' + escHtml(cmdUsed) + '</div><div class="cmd-output">' + escHtml(cmdOutput || "") + '</div></div></div>';
    }

    var el = document.getElementById(tid);
    if (el) {
      el.querySelector(".bubble").innerHTML = answerHtml + cmdHtml;
    }

    setTimeout(processMermaid, 100);
    setTimeout(processMermaid, 400);
  }

  fetch("/ask", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({query: question})
  }).then(function(response) {
    return response.json();
  }).then(function(data) {
    if (data.type === "command") {
      cmdUsed = data.result && data.result.output ? "Command executed" : "";
      cmdOutput = data.result ? (data.result.output || data.result.reason || "") : "";
      tokens = "";
    } else {
      tokens = data.response || data.analysis || "";
    }
    var sourcesCount = data.sources_used || 0;
    var timeStr = data.time_taken || "";
    document.getElementById("token-badge").textContent = sourcesCount + " sources • " + timeStr;
    renderStream();
    document.getElementById("btn-send").disabled = false;
    document.getElementById("input").focus();
    document.getElementById("chat").scrollTop = 99999;
  }).catch(function(e) {
    var el = document.getElementById(tid);
    if (el) el.querySelector(".bubble").innerHTML = "Error: " + e;
    document.getElementById("btn-send").disabled = false;
  });
}

function clearChat() {
  fetch("/clear", {method: "POST"});
  document.getElementById("chat").innerHTML = '<div id="welcome" style="max-width:600px;margin:40px auto 0;padding:0 24px;text-align:center"><div class="w-icon">D</div><h2 style="font-size:1.5rem;font-weight:500;margin-bottom:10px">What can I help with?</h2></div>';
  document.getElementById("token-badge").textContent = "0 tokens";
}
