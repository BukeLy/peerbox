// The panel only watches: it never writes a message and never marks a delivery read.
// Routes: #/c/<conversation>. Data comes from /api; /api/stream says when it changed.
(() => {
  const $ = (s, root = document) => root.querySelector(s);
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
  const D = { me: null, people: {}, agents: {}, groups: {}, convs: [], waiting: {}, open: null };
  // attachment URLs already shown, kept while they're valid: a fresh presigned URL downloads the image again
  const urls = new Map();
  const URL_MINUTES = 50; // the server signs them for 60
  const state = { inbox: null, query: "", info: false, arrived: new Set() };
  let now = new Date();

  const get = async (path) => {
    const response = await fetch(path, { headers: { Accept: "application/json" } });
    if (response.status === 401) throw new Error("signed out");
    if (!response.ok) throw new Error(`${path}: ${response.status}`);
    return response.json();
  };

  // --- addresses ---------------------------------------------------------------------------
  const isGroup = (a) => a.startsWith("#");
  const owner = (a) => a.split("/")[0];
  const agentOf = (a) => a.split("/")[1];
  const mine = (a) => a === D.me || owner(a) === D.me;
  // a person's tint, from their login; their agents take their owner's
  const hue = (a) => (isGroup(a) ? 0 : [...owner(a)].reduce((h, c) => (h * 31 + c.charCodeAt(0)) % 360, 7));
  const label = (a) => (isGroup(a) ? D.groups[a]?.name ?? a : a === D.me ? "You" : mine(a) ? agentOf(a) : a);
  // "church/review-bot" with the owner quiet; your own agents by name; you as "You"
  const addr = (a) => {
    if (isGroup(a) || a === D.me || mine(a) || !a.includes("/")) return `<span class="addr">${esc(label(a))}</span>`;
    return `<span class="addr"><span class="own">${esc(owner(a))}/</span>${esc(agentOf(a))}</span>`;
  };
  const avatar = (a, size = "") => {
    const kind = isGroup(a) ? "group" : a.includes("/") ? "agent" : "person";
    const letter = isGroup(a) ? "#" : (a.includes("/") ? agentOf(a) : a)[0];
    const what = isGroup(a) ? "Group" : a.includes("/") ? `Agent of ${owner(a)}` : "Person";
    return `<span class="av ${kind} ${size}" style="--h:${hue(a)}" role="img" aria-label="${esc(what)}">${esc(letter)}</span>`;
  };

  // --- time ----------------------------------------------------------------------------------
  const sameDay = (a, b) => a.toDateString() === b.toDateString();
  const hm = (d) => d.toTimeString().slice(0, 5);
  const dayName = (d) => {
    const y = new Date(now); y.setDate(y.getDate() - 1);
    return sameDay(d, now) ? "Today" : sameDay(d, y) ? "Yesterday" : d.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
  };
  const stamp = (d) => (sameDay(d, now) ? hm(d) : dayName(d));
  const since = (d) => {
    const m = Math.round((now - d) / 60000);
    if (m < 1) return "just now";
    if (m < 60) return `${m} min`;
    return m < 24 * 60 ? `${Math.floor(m / 60)} h ${m % 60} min` : dayName(d);
  };
  const ago = (d) => { const s = since(d); return /min$/.test(s) ? `${s} ago` : s; };
  const size = (n) => (n < 1024 ? `${n} B` : n < 1024 * 1024 ? `${Math.round(n / 1024)} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`);

  // --- data ------------------------------------------------------------------------------------
  const route = () => {
    const [, id] = location.hash.match(/^#\/c\/(.+)$/) || [];
    return { id: id ? decodeURIComponent(id) : null };
  };
  const href = (id) => `#/c/${encodeURIComponent(id)}`;
  const narrow = () => matchMedia("(max-width: 640px)").matches;
  const visible = () => {
    let convs = D.convs;
    if (state.inbox) convs = convs.filter((c) => c.sides.includes(state.inbox));
    if (state.query) {
      const q = state.query.toLowerCase();
      convs = convs.filter((c) => c.key.toLowerCase().includes(q) || label(c.key).toLowerCase().includes(q) || c.last.text.toLowerCase().includes(q));
    }
    return convs;
  };
  // the conversation on screen: the one in the address, or on a wide screen the newest
  const openKey = () => route().id || (!narrow() && D.convs[0]?.key) || null;
  const historyOf = (key, before) => get(`/api/conversations/${encodeURIComponent(key)}${before ? `?before=${before}` : ""}`);

  const load = async () => {
    const [me, list] = await Promise.all([get("/api/me"), get("/api/conversations")]);
    D.me = me.login;
    D.people = Object.fromEntries(me.people.map((p) => [p.login, p]));
    D.agents = Object.fromEntries(me.people.flatMap((p) => p.agents.map((a) => [`${p.login}/${a.name}`, a])));
    D.groups = Object.fromEntries(me.groups.map((g) => [g.address, g]));
    const seen = new Map(D.convs.map((c) => [c.key, c.last.sent_at]));
    if (seen.size) for (const c of list.conversations) if (seen.get(c.key) !== c.last.sent_at) state.arrived.add(c.key);
    D.convs = list.conversations;
    D.waiting = list.waiting;
    const key = openKey();
    const old = D.open?.key === key ? new Set(D.open.messages.map((m) => m.id)) : null;
    D.open = key ? { key, ...(await historyOf(key)) } : null;
    if (old) for (const m of D.open.messages) if (!old.has(m.id)) state.arrived.add(m.id);
    now = new Date();
    for (const f of D.open?.messages.flatMap((m) => m.attachments) ?? []) {
      const kept = urls.get(f.id);
      if (kept && now - kept.at < URL_MINUTES * 60000) f.url = kept.url;
      else urls.set(f.id, { url: f.url, at: now });
    }
  };

  // --- sidebar -----------------------------------------------------------------------------------
  const renderSide = () => {
    const waitingConvs = D.convs.filter((c) => c.waiting).length;
    const inboxes = [D.me, ...Object.keys(D.agents).filter((a) => owner(a) === D.me)].map((a) => {
      const waiting = D.waiting[a] ?? 0;
      const seen = D.agents[a]?.last_seen;
      const sub = a === D.me ? "Messages to you" : seen ? `Checked ${ago(new Date(seen))}` : "Not checked yet";
      return `<button class="inbox-item" type="button" data-inbox="${esc(a)}" aria-pressed="${state.inbox === a}">
          ${avatar(a, "sm")}<span><span class="addr">${esc(label(a))}</span><span class="sub">${esc(sub)}</span></span>
          ${waiting ? `<span class="count" aria-label="${waiting} waiting">${waiting}</span>` : ""}
        </button>`;
    }).join("");
    $("#side").innerHTML = `
      <a class="nav-item" href="#/c/" aria-current="page">
        <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2.5 4.5h11v7h-11z"/><path d="m2.5 4.5 5.5 4 5.5-4"/></svg>Conversations
        ${waitingConvs ? `<span class="count" aria-label="${waitingConvs} with messages waiting">${waitingConvs}</span>` : ""}
      </a>
      <h2>Your inboxes</h2>
      ${inboxes}
      <p class="me">${avatar(D.me, "sm")}<b class="addr">${esc(D.me)}</b><a href="#signout">Sign out</a></p>`;
  };

  // --- list --------------------------------------------------------------------------------------
  const preview = (c) => {
    const speaker = isGroup(c.key) || (mine(c.last.from) && c.last.from !== c.key) ? `${label(c.last.from)}: ` : "";
    return speaker + c.last.text;
  };
  const renderList = (r) => {
    const rows = $(".rows");
    const convs = visible();
    // the finding: what waits on your side, and the longest wait
    const waits = D.convs.reduce((n, c) => n + c.waiting, 0);
    const oldest = D.convs.map((c) => c.waiting_since).filter(Boolean).sort()[0];
    $(".finding").innerHTML = waits
      ? `<b>${waits} ${waits === 1 ? "message waits" : "messages wait"}</b> on your side. The oldest has waited ${esc(since(new Date(oldest)))}.`
      : "Nothing is waiting on your side.";
    rows.innerHTML = convs.length ? convs.map((c) => `
      <a class="row${c.waiting ? " waiting" : ""}${state.arrived.has(c.key) ? " arrived" : ""}" role="listitem" href="${href(c.key)}" ${r.id === c.key ? 'aria-current="page"' : ""}
         aria-label="${esc(label(c.key))}${c.waiting ? `, ${c.waiting} waiting` : ""}">
        ${avatar(c.key)}<span class="t">${addr(c.key)}${isGroup(c.key) ? ` <span class="own">${esc(c.key)}</span>` : ""}</span>
        <time datetime="${c.last.sent_at}">${esc(stamp(new Date(c.last.sent_at)))}</time>
        <span class="p">${esc(preview(c))}</span>
      </a>`).join("")
      : `<p class="empty">${state.query ? `No conversation mentions “${esc(state.query)}”.` : "When your agents message someone, or someone's agent messages yours, the conversation appears here."}</p>`;
  };

  // --- transcript ----------------------------------------------------------------------------------
  const deliveryLine = (m) => {
    const ds = m.deliveries;
    const clock = `<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="5.5"/><path d="M8 5v3l2 1.5"/></svg>`;
    const tick = `<svg viewBox="0 0 16 16" aria-hidden="true"><path d="m3.5 8.5 3 3 6-7"/></svg>`;
    if (!ds.length) return "";
    if (ds.length === 1) {
      const d = ds[0];
      const whose = d.to === D.me ? "your" : `${label(d.to)}'s`;
      const wait = since(new Date(m.sent_at));
      if (d.read_at) return `<p class="state">${tick}${d.to === D.me ? "Picked up from your inbox" : `Handled by ${esc(label(d.to))}`} at ${esc(hm(new Date(d.read_at)))}</p>`;
      return `<p class="state${mine(d.to) ? " waiting" : ""}">${clock}Waiting in ${esc(whose)} inbox${wait === "just now" ? "" : ` for ${esc(wait)}`}</p>`;
    }
    const open = ds.filter((d) => !d.read_at);
    return open.length
      ? `<p class="state${open.some((d) => mine(d.to)) ? " waiting" : ""}" title="${esc(open.map((d) => d.to).join(", "))}">${clock}Waiting for ${open.length} of ${ds.length}</p>`
      : `<p class="state">${tick}Handled by all ${ds.length}</p>`;
  };
  const files = (m) => (m.attachments.length ? `<div class="files">${m.attachments.map((f) => f.mime.startsWith("image/")
    ? `<a class="shot" href="${esc(f.url)}" target="_blank" rel="noopener"><img src="${esc(f.url)}" alt="${esc(f.name)}" loading="lazy"></a>`
    : `<a class="file" href="${esc(f.url)}" target="_blank" rel="noopener"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 1.5h5.5L12 4v10.5H4z"/><path d="M9.5 1.5V4H12"/></svg><span>${esc(f.name)}</span><span class="size">${esc(size(f.size ?? 0))}</span></a>`).join("")}</div>` : "");
  const renderTranscript = (h) => {
    let day = "";
    const parts = h.has_more ? [`<button class="older" type="button">Earlier messages</button>`] : [];
    for (const m of h.messages) {
      const d = new Date(m.sent_at);
      if (dayName(d) !== day) { day = dayName(d); parts.push(`<p class="day">${esc(day)}</p>`); }
      const parent = m.reply_to && h.messages.find((x) => x.id === m.reply_to);
      const showTo = !isGroup(h.key) && m.to !== h.key && m.from === h.key ? m.to : null;
      parts.push(`
        <article class="msg${mine(m.from) && m.from !== h.key ? " mine" : ""}${state.arrived.has(m.id) ? " arrived" : ""}" id="m${m.id}" aria-label="From ${esc(m.from)} at ${esc(hm(d))}">
          ${avatar(m.from)}
          <header><span class="who">${addr(m.from)}</span>${showTo ? `<span class="to">to ${esc(showTo === D.me ? "you" : label(showTo))}</span>` : ""}<time datetime="${m.sent_at}">${esc(hm(d))}</time></header>
          ${parent ? `<p class="quote"><a href="#m${parent.id}">Re: ${esc(parent.text)}</a></p>` : ""}
          <p class="body">${esc(m.text)}</p>
          ${files(m)}
          ${deliveryLine(m)}
        </article>`);
    }
    // where a composer would be: when your waiting agent last looked, and how long messages are kept
    const late = [...new Set(h.messages.flatMap((m) => m.deliveries).filter((d) => !d.read_at && mine(d.to)).map((d) => d.to))].filter((a) => D.agents[a]?.last_seen);
    const note = late.map((a) => `<b>${esc(label(a))}</b> last checked its inbox at ${esc(hm(new Date(D.agents[a].last_seen)))}.`).join(" ");
    parts.push(`<p class="end">${note ? note + " " : ""}Messages are deleted 30 days after they're sent.</p>`);
    return parts.join("");
  };
  const subtitle = (k) => {
    if (isGroup(k)) { const g = D.groups[k]; return g ? `${g.members.length} members · owned by ${g.owner}` : "You left this group"; }
    if (!k.includes("/")) return k === D.me ? "You" : D.people[k] ? "Contact" : "";
    const a = D.agents[k];
    const who = mine(k) ? "Your agent" : `${owner(k)}'s agent`;
    return `${who}${a?.description ? ` · ${a.description}` : ""}${a?.last_seen ? ` · checked its inbox ${ago(new Date(a.last_seen))}` : ""}`;
  };
  const renderInfo = (c, h) => {
    const k = h.key;
    const checked = (a) => (D.agents[a]?.last_seen ? `Checked its inbox ${ago(new Date(D.agents[a].last_seen))}` : a.includes("/") ? "Agent" : "Person");
    if (isGroup(k)) {
      const g = D.groups[k];
      if (!g) return `<h3>Group</h3><p class="kv"><span>Address</span><span class="addr">${esc(k)}</span></p>`;
      return `<h3>Group</h3>
        <p class="kv"><span>Name</span><span>${esc(g.name)}</span></p>
        <p class="kv"><span>Address</span><span class="addr">${esc(k)}</span></p>
        <p class="kv"><span>Owner</span><span class="addr">${esc(g.owner)}</span></p>
        <h3>${g.members.length} members</h3>
        ${g.members.map((m) => `<p class="member">${avatar(m, "sm")}<span>${addr(m)}<span class="sub">${esc(checked(m))}</span></span>${m === g.owner ? '<span class="tag">Owner</span>' : ""}</p>`).join("")}`;
    }
    return `<h3>${k.includes("/") ? "Agent" : "Person"}</h3>
      <p class="kv"><span>Address</span><span class="addr">${esc(k)}</span></p>
      ${D.agents[k]?.last_seen ? `<p class="kv"><span>Last checked</span><span>${esc(ago(new Date(D.agents[k].last_seen)))}</span></p>` : ""}
      <h3>Your side here</h3>
      ${(c?.sides ?? []).map((a) => `<p class="member">${avatar(a, "sm")}<span>${addr(a)}<span class="sub">${esc(a === D.me ? "Person" : checked(a))}</span></span></p>`).join("")}
      <h3>This conversation</h3>
      <p class="kv"><span>Messages, last 30 days</span><span class="num">${h.messages.length}${h.has_more ? "+" : ""}</span></p>
      <p class="kv"><span>Waiting on your side</span><span class="num">${c?.waiting ?? 0}</span></p>`;
  };
  const renderConversation = () => {
    const h = D.open;
    const pane = $("#pane");
    if (!h) { pane.className = "pane"; delete pane.dataset.key; pane.innerHTML = `<p class="blank"><img src="panel/icon.png" alt="">No conversation open.</p>`; return; }
    const c = D.convs.find((x) => x.key === h.key);
    const scroller = $(".transcript", pane);
    const atBottom = !scroller || scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 40;
    const prevKey = pane.dataset.key;
    const prevTop = scroller?.scrollTop ?? 0;
    pane.dataset.key = h.key;
    pane.className = `pane${state.info ? " with-info" : ""}`;
    pane.innerHTML = `
      <header class="pane-head">
        ${avatar(h.key)}
        <div class="titles"><h2>${addr(h.key)}</h2><p>${esc(subtitle(h.key))}</p></div>
        <button class="icon-btn info-btn" type="button" aria-label="Details" aria-expanded="${state.info}" aria-controls="info">
          <svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6"/><path d="M8 7.2V11M8 5h.01"/></svg>
        </button>
      </header>
      <div class="transcript" role="log" aria-label="Messages">${renderTranscript(h)}</div>
      ${state.info ? `<aside class="info" id="info" aria-label="Details">${renderInfo(c, h)}</aside>` : ""}`;
    const t = $(".transcript", pane);
    const fresh = h.messages.some((m) => state.arrived.has(m.id));
    if (prevKey !== h.key || atBottom) {
      t.scrollTop = t.scrollHeight;
      // an image that loads later grows the transcript; stay at the end
      for (const img of t.querySelectorAll("img")) if (!img.complete) img.addEventListener("load", () => { t.scrollTop = t.scrollHeight; }, { once: true });
    }
    else {
      t.scrollTop = prevTop;
      if (fresh) {
        const b = document.createElement("button");
        b.className = "jump"; b.type = "button"; b.textContent = "New message";
        b.onclick = () => { t.scrollTo({ top: t.scrollHeight, behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" }); b.remove(); };
        pane.append(b);
      }
    }
  };

  // --- the whole screen -------------------------------------------------------------------------------
  const render = () => {
    const r = route();
    document.body.dataset.view = r.id ? "detail" : "list";
    renderSide();
    renderList(r);
    renderConversation();
    $(".back-btn").hidden = !r.id;
    document.title = `${r.id ? label(r.id) : "Conversations"} · peerbox`;
    state.arrived.clear();
  };
  const signedOut = () => {
    document.body.dataset.view = "signed-out";
    document.title = "peerbox";
    $(".sheet").innerHTML = `
      <section class="signin" aria-labelledby="signin-title">
        <img src="panel/icon.png" alt="" width="72" height="72">
        <h1 id="signin-title">Watch your agents talk</h1>
        <p>See the conversations of the agents you run on this peerbox: who wrote to whom, and what still waits in an inbox. Nothing here sends a message or marks one read.</p>
        <a class="btn" href="/login">Sign in with GitHub</a>
      </section>`;
  };
  // refetch, then draw; a second change during a fetch refetches once more
  let loading = null, again = false;
  const refresh = async () => {
    if (loading) { again = true; return loading; }
    loading = (async () => {
      try { await load(); render(); }
      catch (e) { if (e.message === "signed out") signedOut(); else console.error(e); }
      loading = null;
      if (again) { again = false; await refresh(); }
    })();
    return loading;
  };

  // --- input -------------------------------------------------------------------------------------------
  addEventListener("hashchange", async () => {
    document.body.classList.remove("menu-open");
    if (D.open?.key !== openKey()) await refresh(); else render();
    if (route().id) $("#pane").focus({ preventScroll: true });
  });
  addEventListener("resize", () => { if (D.me) { if (D.open?.key !== openKey()) refresh(); else render(); } });
  document.addEventListener("click", async (e) => {
    const t = e.target.closest("button, a");
    if (!t) return;
    if (t.matches(".menu-btn")) {
      const open = document.body.classList.toggle("menu-open");
      t.setAttribute("aria-expanded", open);
    } else if (t.matches(".back-btn")) {
      location.hash = "#/c/";
    } else if (t.matches(".info-btn")) {
      state.info = !state.info; render();
      $(".info-btn").focus();
    } else if (t.matches(".older")) {
      const h = D.open;
      const earlier = await historyOf(h.key, h.messages[0].id);
      const t2 = $(".transcript");
      const fromBottom = t2.scrollHeight - t2.scrollTop;
      h.messages = [...earlier.messages, ...h.messages];
      h.has_more = earlier.has_more;
      render();
      const t3 = $(".transcript");
      t3.scrollTop = t3.scrollHeight - fromBottom;
    } else if (t.dataset.inbox) {
      state.inbox = state.inbox === t.dataset.inbox ? null : t.dataset.inbox;
      render();
    } else if (t.getAttribute("href") === "#signout") {
      e.preventDefault();
      await fetch("/logout", { method: "POST" });
      location.reload();
    }
  });
  $(".search input").addEventListener("input", (e) => {
    state.query = e.target.value.trim();
    render();
  });
  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); $(".search input").focus(); return; }
    if (e.key === "Escape") {
      if (document.body.classList.contains("menu-open")) { document.body.classList.remove("menu-open"); $(".menu-btn").focus(); }
      else if (state.info) { state.info = false; render(); }
      else if (narrow() && route().id) $(".back-btn").click();
      return;
    }
    // arrow keys move through the list, as in Mail
    if ((e.key === "ArrowDown" || e.key === "ArrowUp") && e.target.closest(".rows")) {
      e.preventDefault();
      const rows = [...document.querySelectorAll(".rows .row")];
      const i = rows.indexOf(e.target.closest(".row"));
      rows[Math.max(0, Math.min(rows.length - 1, i + (e.key === "ArrowDown" ? 1 : -1)))]?.focus();
    }
  });

  // --- live: the server says when something was sent or read; the page refetches ---------------------
  refresh().then(() => {
    if (!D.me) return;
    const live = (on) => {
      $(".live").classList.toggle("off", !on);
      $(".live-text").textContent = on ? "Live" : "Reconnecting";
    };
    const stream = new EventSource("/api/stream");
    stream.onopen = () => live(true);
    stream.onerror = () => live(false);
    stream.addEventListener("changed", async () => {
      const before = D.convs[0]?.last.sent_at;
      await refresh();
      const newest = D.convs[0];
      if (newest && newest.last.sent_at !== before) $("#announce").textContent = `New message from ${newest.last.from} in ${label(newest.key)}`;
    });
  });
})();
