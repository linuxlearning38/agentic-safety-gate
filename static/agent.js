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

  var steps = [];
  var tokens = "";
  var cmdUsed = null;
  var cmdOutput = null;

  function renderStream() {
    var stepsHtml = steps.length ? '<div class="steps-block">' + steps.map(function(s){ return '<div class="step">' + escHtml(s) + '</div>'; }).join("") + '</div>' : "";
    var answerHtml = tokens ? marked.parse(tokens) : "";
    var cmdHtml = "";
    if (cmdUsed) {
      cmdHtml = '<div class="cmd-block"><div class="cmd-header"><div class="cmd-dot" style="background:#ff5f57"></div><div class="cmd-dot" style="background:#febc2e"></div><div class="cmd-dot" style="background:#28c840"></div><span class="cmd-label">Terminal</span></div><div class="cmd-body"><div class="cmd-line">' + escHtml(cmdUsed) + '</div><div class="cmd-output">' + escHtml(cmdOutput || "") + '</div></div></div>';
    }
    var el = document.getElementById(tid);
    if (el) el.outerHTML = '<div class="turn agent" id="' + tid + '"><div class="agent-icon">D</div><div class="bubble">' + stepsHtml + answerHtml + cmdHtml + '</div></div>';
  }

  fetch("/ask_stream", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({question: question})
  }).then(function(response) {
    var reader = response.body.getReader();
    var decoder = new TextDecoder();
    var buffer = "";

    function pump() {
      reader.read().then(function(result) {
        if (result.done) {
          renderStream();
          document.getElementById("btn-send").disabled = false;
          document.getElementById("input").focus();
          document.getElementById("chat").scrollTop = 99999;
          return;
        }
        buffer += decoder.decode(result.value, {stream: true});
        var lines = buffer.split("\n");
        buffer = lines.pop();
        lines.forEach(function(line) {
          if (line.startsWith("data: ")) {
            try {
              var msg = JSON.parse(line.slice(6));
              if (msg.type === "step") { steps.push(msg.content); }
              else if (msg.type === "token") { 
                // Strip COMMAND: lines from visible answer
                if (!msg.content.match(/^COMMAND:/)) { tokens += msg.content; }
              }
              else if (msg.type === "command") { cmdUsed = msg.command; cmdOutput = msg.output; tokens = tokens.replace(/COMMAND:.*$/m, "").trim(); }
              else if (msg.type === "done" && msg.tokens_total) {
                var t = msg.tokens_total;
                document.getElementById("token-badge").textContent = (t > 1000 ? (t/1000).toFixed(1)+"k" : t) + " tokens";
              }
              renderStream();
              document.getElementById("chat").scrollTop = 99999;
            } catch(e) {}
          }
        });
        pump();
      });
    }
    pump();
  }).catch(function(e) {
    var el = document.getElementById(tid);
    if (el) el.outerHTML = '<div class="turn agent"><div class="agent-icon">D</div><div class="bubble">Error: ' + e + '</div></div>';
    document.getElementById("btn-send").disabled = false;
  });
}

function clearChat() {
  fetch("/clear", {method: "POST"});
  document.getElementById("chat").innerHTML = '<div id="welcome" style="max-width:600px;margin:40px auto 0;padding:0 24px;text-align:center"><div class="w-icon">D</div><h2 style="font-size:1.5rem;font-weight:500;margin-bottom:10px">What can I help with?</h2></div>';
  document.getElementById("token-badge").textContent = "0 tokens";
}
