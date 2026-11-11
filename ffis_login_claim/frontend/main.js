// Redeems a one-time nonce so the FFIS server can set the session cookie.
// The nonce is not the session token. This script does not read or write
// cookies; only the server response can mark the cookie HttpOnly.

const LOGIN_COOKIE_PATH = "/ffis/login-cookie";
const sent = Object.create(null);

function send(type, data) {
  const payload = { isStreamlitMessage: true, type: type };
  if (data) {
    Object.keys(data).forEach(function (key) {
      payload[key] = data[key];
    });
  }
  window.parent.postMessage(payload, "*");
}

function redeem(nonce) {
  if (!nonce || sent[nonce]) {
    return;
  }
  sent[nonce] = true;
  fetch(LOGIN_COOKIE_PATH, {
    method: "POST",
    credentials: "same-origin",
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ nonce: nonce }),
  }).catch(function () {});
}

window.addEventListener("message", function (event) {
  if (!event.data || event.data.type !== "streamlit:render") {
    return;
  }
  const args = event.data.args || {};
  redeem(args.nonce);
  send("streamlit:setFrameHeight", { height: 0 });
});

send("streamlit:componentReady", { apiVersion: 1 });
send("streamlit:setFrameHeight", { height: 0 });
