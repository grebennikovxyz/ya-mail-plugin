"use strict";
const get = id => document.getElementById(id);
const utf8 = new TextEncoder();
if (location.origin === "https://grebennikovxyz.github.io" && ["/ya-mail-plugin/", "/ya-mail-plugin/index.html"].includes(location.pathname)) {
  const opening = new Image(1, 1);
  opening.crossOrigin = "anonymous";
  opening.referrerPolicy = "no-referrer";
  opening.src = "https://ya-ma-count.grebennikov.xyz/ya-ma/open.gif";
}
function decode(text) {
  return Uint8Array.from(atob(text.replace(/-/g, "+").replace(/_/g, "/")), c => c.charCodeAt(0));
}
function encode(bytes) {
  return btoa(String.fromCharCode(...new Uint8Array(bytes))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
async function start() {
  const fragment = location.hash.slice(1);
  if (!fragment) return;
  history.replaceState(null, "", location.pathname);
  if (fragment.length > 4096) throw Error();
  const {id,email,key,linux} = JSON.parse(new TextDecoder().decode(decode(fragment)));
  if (!/^[a-f0-9]{32}$/.test(id) || typeof email !== "string" || email.length > 254) throw Error();
  const publicKey = await crypto.subtle.importKey("spki", decode(key), {name:"RSA-OAEP",hash:"SHA-256"}, false, ["encrypt"]);
  if (publicKey.algorithm.modulusLength !== 3072) throw Error();
  get("status").textContent = "Вы подключаете " + email;
  get("wizard").hidden = false;
  get("storage").hidden = linux !== true;
  get("encrypt").onclick = async () => {
    const password = get("password").value.trim().replace(/ /g, "");
    if (!/^[A-Za-z0-9]{1,128}$/.test(password)) {
      get("status").textContent = "Вставьте пароль приложения типа «Почта» для " + email;
      return;
    }
    try {
      const payload = JSON.stringify([get("mode").value, password, linux === true && get("local").checked]);
      const sealed = await crypto.subtle.encrypt({name:"RSA-OAEP",label:utf8.encode("Ya-Ma/1:" + id + ":" + email)},publicKey,utf8.encode(payload));
      get("password").value = "";
      get("code").value = "YM1." + id + "." + encode(sealed);
      get("wizard").hidden = true;
      get("result").hidden = false;
      get("status").textContent = "Верните код в тот же личный чат в течение 10 минут с момента запроса.";
    } catch { get("status").textContent = "Браузеру не удалось зашифровать пароль. Откройте ссылку в современном браузере."; }
  };
  get("copy").onclick = async () => {
    try { await navigator.clipboard.writeText(get("code").value); get("copy").textContent = "Скопировано"; }
    catch { get("code").focus(); get("code").select(); }
  };
}
start().catch(() => { get("status").textContent = "Ссылка неполная или браузер не поддерживает шифрование. Запросите новую ссылку в личном чате."; });
