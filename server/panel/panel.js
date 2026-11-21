// The panel only watches: it never writes a message and never marks a delivery read.
// Routes: #/ the list, #/c/<conversation> one conversation, #/i/<conversation> it with its details.
// Data comes from /api; /api/stream says when something was sent or read, and the page refetches.
(() => {
  const $ = (s, root = document) => root.querySelector(s);
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

  // --- message text: agents write GitHub markdown. Strangers' agents write it too and this page holds the session,
  // so raw HTML is shown as text and marked's output is sanitised to the tags it makes
  marked.use({
    gfm: true, breaks: true,
    renderer: {
      html: ({ text, block }) => (block ? `<p>${esc(text.trim())}</p>` : esc(text)),
      // a message's headings sit under the conversation's title, so they start at h4
      heading({ tokens, depth }) { const n = Math.min(6, depth + 3); return `<h${n}>${this.parser.parseInline(tokens)}</h${n}>`; },
      // a remote image would load from its host as soon as it is shown, telling a stranger who reads the
      // message and when, so it becomes a link that names the host; attachments come from our bucket and show inline
      image({ href, text }) {
        let host = "";
        try { host = new URL(href, location.href).host; } catch { return esc(text); }
        return `<a href="${esc(href)}">${esc(text || "Image")} (${esc(host)})</a>`;
      },
    },
  });
  const ALLOWED_MARKUP = {
    ALLOWED_TAGS: ["p", "br", "strong", "em", "del", "code", "pre", "a", "ul", "ol", "li", "input", "blockquote", "h4", "h5", "h6", "hr", "table", "thead", "tbody", "tr", "th", "td"],
    ALLOWED_ATTR: ["href", "title", "start", "type", "checked", "disabled", "align"],
  };
  DOMPurify.addHook("afterSanitizeAttributes", (n) => {
    if (n.localName === "a") { n.setAttribute("target", "_blank"); n.setAttribute("rel", "noopener noreferrer nofollow"); }
  });
  const md = (text) => DOMPurify.sanitize(marked.parse(text), ALLOWED_MARKUP);
  // a preview or a quote is the message's words without its markup
  const plain = (text) => {
    const out = [];
    const walk = (ts) => {
      for (const t of ts ?? []) {
        if (t.type === "table") for (const cell of [...t.header, ...t.rows.flat()]) { walk(cell.tokens); out.push(" "); }
        else if (t.items) { out.push(" "); walk(t.items); }
        else if (t.tokens) walk(t.tokens);
        else out.push(t.text ?? "");
        if (/^(paragraph|heading|list_item|code|blockquote|space|hr|br)$/.test(t.type) || (t.type === "html" && t.block)) out.push(" ");
      }
    };
    walk(marked.lexer(text));
    return out.join("").replace(/\s+/g, " ").trim();
  };
  const data = { me: null, name: null, people: {}, agents: {}, groups: {}, convs: null, waiting: {}, open: null };
  const state = { inbox: null, query: "", arrived: new Set(), failed: null, updated: null };
  // attachment URLs already shown, kept until shortly before they expire: a fresh presigned URL downloads the image again
  const urls = new Map();
  const URL_MARGIN = 10 * 60000;
  let now = new Date();

  const get = async (path) => {
    const response = await fetch(path, { headers: { Accept: "application/json" } });
    if (response.status === 401) throw new Error("signed out");
    if (!response.ok) throw new Error(`${path}: ${response.status}`);
    return response.json();
  };

  // --- addresses ---------------------------------------------------------------------------------
  const kind = (a) => (a.startsWith("#") ? "group" : a.includes("/") ? "agent" : "person");
  const owner = (a) => a.split("/")[0];
  const agentOf = (a) => a.split("/")[1];
  const mine = (a) => kind(a) !== "group" && owner(a) === data.me;
  // label: you as "You", your agents by name, a group by its name, anyone else by their address.
  // addr: the label as an address; another person's agent keeps its owner, quiet. detail: the line under a member's address.
  // noun: the line under the details pane's title. facts: the details pane's lines about it.
  // subtitle: the line under the conversation's title. where: how a sentence names the conversation, around a link to it.
  // in: what an announcement of a new message adds after its sender.
  const named = (a) => `<span class="addr">${esc(label(a))}</span>`;
  const direct = { where: (link) => `your conversation with ${link}`, in: () => "" };
  const KIND = {
    person: {
      glyph: '<circle cx="8" cy="5.5" r="2.5"/><path d="M3.2 13.5c.6-2.5 2.5-3.9 4.8-3.9s4.2 1.4 4.8 3.9"/>',
      label: (a) => (a === data.me ? "You" : a),
      addr: named,
      detail: (a) => data.people[a]?.name ?? "Person",
      noun: () => "Person",
      facts: (a, c) => `<p class="kv"><span>Name</span><span>${esc(data.people[a]?.name ?? "—")}</span></p>${members("Your side", c?.sides ?? [], a)}`,
      subtitle: (a) => (a === data.me ? "You" : data.people[a]?.name ?? "Person"),
      ...direct,
    },
    agent: {
      glyph: '<rect x="2.5" y="4.5" width="11" height="9" rx="3"/><path d="M6 8.3v1.4M10 8.3v1.4M8 4.5V2.3"/>',
      label: (a) => (mine(a) ? agentOf(a) : a),
      addr: (a) => (mine(a) ? named(a) : `<span class="addr"><span class="owner">${esc(owner(a))}/</span>${esc(agentOf(a))}</span>`),
      detail: (a) => checked(a),
      noun: (a) => (mine(a) ? "Your agent" : "Agent"),
      facts: (a, c) => {
        const does = data.agents[a]?.description;
        const person = data.people[owner(a)];
        return `${does ? `<p class="kv"><span>Does</span><span>${esc(does)}</span></p>` : ""}
          <p class="kv"><span>Acts for</span><span>${esc(person?.name ? `${person.name} (${owner(a)})` : owner(a))}</span></p>
          <p class="kv"><span>Inbox</span><span>${esc(checked(a))}</span></p>${members("Your side", c?.sides ?? [], a)}`;
      },
      subtitle: (a) => [mine(a) ? "Your agent" : `${owner(a)}'s agent`, data.agents[a]?.description, data.agents[a] && checked(a).toLowerCase()].filter(Boolean).join(" · "),
      ...direct,
    },
    group: {
      glyph: '<path d="M6.3 2.5 5 13.5M11 2.5 9.7 13.5M2.8 6h10.7M2.5 10h10.7"/>',
      label: (a) => data.groups[a]?.name ?? a,
      addr: named,
      noun: (a) => (data.groups[a] ? a : "Group"),
      facts: (a) => {
        const g = data.groups[a];
        return g
          ? `<p class="kv"><span>Owner</span><span class="addr">${esc(g.owner)}</span></p>${members(`${g.members.length} members`, g.members, a)}`
          : `<p class="kv"><span>Membership</span><span>You left this group</span></p>`;
      },
      subtitle: (a) => { const g = data.groups[a]; return g ? `${g.members.length} members · owner ${g.owner}` : "You are no longer in this group"; },
      where: (link) => link,
      in: (a) => ` in ${label(a)}`,
    },
  };
  const about = (a) => KIND[kind(a)];
  const label = (a) => about(a).label(a);
  const addr = (a) => about(a).addr(a);
  const glyph = (a) => `<svg class="kind" viewBox="0 0 16 16" aria-hidden="true">${about(a).glyph}</svg>`;
  const disc = (a) => `<span class="disc" aria-hidden="true">${glyph(a)}</span>`;

  // --- time ----------------------------------------------------------------------------------------
  const sameDay = (a, b) => a.toDateString() === b.toDateString();
  const hm = (d) => d.toTimeString().slice(0, 5);
  const dayName = (d) => {
    const y = new Date(now); y.setDate(y.getDate() - 1);
    return sameDay(d, now) ? "Today" : sameDay(d, y) ? "Yesterday" : d.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
  };
  const stamp = (d) => (sameDay(d, now) ? hm(d) : dayName(d));
  const since = (d) => {
    const m = Math.floor((now - d) / 60000);
    if (m < 1) return "under a minute";
    if (m < 60) return `${m} min`;
    return m < 24 * 60 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${Math.floor(m / 1440)} d`;
  };
  const ago = (d) => (now - d < 60000 ? "just now" : `${since(d)} ago`);
  const checked = (a) => { const seen = data.agents[a]?.last_seen; return seen ? `Checked ${ago(new Date(seen))}` : "Not checked yet"; };
  const size = (n) => (n < 1024 ? `${n} B` : n < 1024 * 1024 ? `${Math.round(n / 1024)} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`);

  // --- route and data --------------------------------------------------------------------------------
  const narrow = () => matchMedia("(max-width: 720px)").matches;
  // on a phone a pushed screen stands alone, so its title is the page's heading
  const titleTag = () => (narrow() ? "h1" : "h2");
  const route = () => {
    const [, pane, key] = location.hash.match(/^#\/([ci])\/(.+)$/) || [];
    return { key: key ? decodeURIComponent(key) : null, info: pane === "i" };
  };
  const href = (key, info) => `#/${info ? "i" : "c"}/${encodeURIComponent(key)}`;
  // the conversation on screen: the one in the address, or on a wide screen the newest
  const openKey = () => route().key || (!narrow() && data.convs?.[0]?.key) || null;
  const historyOf = (key, before) => get(`/api/conversations/${encodeURIComponent(key)}${before ? `?before=${before}` : ""}`);
  const visible = () => {
    let convs = data.convs;
    if (state.inbox) convs = convs.filter((c) => c.sides.includes(state.inbox));
    if (state.query) {
      const q = state.query.toLowerCase();
      convs = convs.filter((c) => [c.key, label(c.key), c.last.from, c.last.text].some((s) => s.toLowerCase().includes(q)));
    }
    return convs;
  };

  const load = async () => {
    const [me, list] = await Promise.all([get("/api/me"), get("/api/conversations")]);
    data.me = me.login;
    data.people = Object.fromEntries(me.people.map((p) => [p.login, p]));
    data.agents = Object.fromEntries(me.people.flatMap((p) => p.agents.map((a) => [`${p.login}/${a.name}`, a])));
    data.groups = Object.fromEntries(me.groups.map((g) => [g.address, g]));
    data.name = data.people[data.me]?.name;
    const seen = data.convs && new Map(data.convs.map((c) => [c.key, c.last.sent_at]));
    if (seen) for (const c of list.conversations) if (seen.get(c.key) !== c.last.sent_at) state.arrived.add(c.key);
    data.convs = list.conversations;
    data.waiting = list.waiting;
    const key = openKey();
    const old = data.open?.key === key ? new Set(data.open.messages.map((m) => m.id)) : null;
    try {
      data.open = key ? { key, ...(await historyOf(key)) } : null;
      state.failed = null;
    } catch (e) {
      if (e.message === "signed out") throw e;
      data.open = null;
      state.failed = { key, at: now };
    }
    if (old) for (const m of data.open?.messages ?? []) if (!old.has(m.id)) state.arrived.add(m.id);
    now = new Date();
    state.updated = now;
    for (const f of data.open?.messages.flatMap((m) => m.attachments) ?? []) {
      const kept = urls.get(f.id);
      if (kept && new Date(kept.expires) - now > URL_MARGIN) f.url = kept.url;
      else urls.set(f.id, { url: f.url, expires: f.url_expires_at });
    }
  };

  // --- the title row ------------------------------------------------------------------------------------
  const renderHead = () => {
    const waits = Object.values(data.waiting).reduce((n, c) => n + c, 0);
    // the count leads to its cause: a link to the conversation that holds the oldest waiting message
    const oldest = data.convs.filter((c) => c.waiting_since).sort((a, b) => new Date(a.waiting_since) - new Date(b.waiting_since))[0];
    const link = oldest && `<a href="${href(oldest.key)}">${esc(label(oldest.key))}</a>`;
    $(".finding").innerHTML = waits && oldest
      ? `<b>${waits === 1 ? "1 message waits" : `${waits} messages wait`}</b> in your inboxes. ${waits === 1 ? "It" : "The oldest"} has waited ${esc(since(new Date(oldest.waiting_since)))}, in ${about(oldest.key).where(link)}.`
      : data.convs.length ? "Nothing waits in your inboxes." : "No conversations in the last 30 days.";
    const inboxes = [data.me, ...Object.keys(data.agents).filter(mine)];
    $(".inboxes").innerHTML = inboxes.map((a) => {
      const n = data.waiting[a] ?? 0;
      return `<button class="inbox" type="button" data-inbox="${esc(a)}" aria-pressed="${state.inbox === a}">
          <span class="label addr">${esc(label(a))}</span>
          <span class="fig">${n ? `<b>${n}</b>waiting` : "Nothing waiting"}</span>
          <span class="sub">${esc(a === data.me ? `Sent to ${data.me}` : checked(a))}</span>
        </button>`;
    }).join("");
  };

  // --- the list ------------------------------------------------------------------------------------------
  const preview = (c) => {
    const speaker = c.last.from !== c.key && (kind(c.key) === "group" || mine(c.last.from)) ? `${label(c.last.from)}: ` : "";
    return speaker + plain(c.last.text);
  };
  const renderList = () => {
    const key = data.open?.key ?? state.failed?.key;
    const convs = visible();
    const why = state.query ? `No conversation's name or latest message mentions “${esc(state.query)}”. <button class="clear-search" type="button">Clear search</button>`
      : state.inbox ? `Nothing in the last 30 days involves ${esc(state.inbox === data.me ? "you directly" : label(state.inbox))}. <button type="button" data-inbox="${esc(state.inbox)}">Show all</button>`
      : "When one of your agents writes to someone, or someone writes to you or your agents, the conversation shows here.";
    // the pressed inbox can be out of view (a phone scrolls it away), so the list says it is filtered
    const scope = state.inbox && convs.length ? `<p class="scope">Showing conversations that involve ${esc(state.inbox === data.me ? "you directly" : label(state.inbox))}. <button type="button" data-inbox="${esc(state.inbox)}">Show all</button></p>` : "";
    $(".rows").innerHTML = scope + (convs.length ? convs.map((c) => `
      <a class="row${c.waiting ? " waiting" : ""}${state.arrived.has(c.key) ? " arrived" : ""}" href="${href(c.key)}" data-key="${esc(c.key)}"${c.key === key ? ' aria-current="page"' : ""}>
        <span class="name">${glyph(c.key)}${addr(c.key)}</span>
        <time datetime="${c.last.sent_at}">${esc(stamp(new Date(c.last.sent_at)))}</time>
        <span class="preview">${esc(preview(c))}</span>
        ${c.waiting ? `<span class="count"><span class="sr">, waiting: </span>${c.waiting}</span>` : ""}
      </a>`).join("") : `<p class="empty">${why}</p>`);
  };

  // --- the conversation ------------------------------------------------------------------------------------
  const tick = `<svg viewBox="0 0 16 16" aria-hidden="true"><path d="m3.5 8.5 3 3 6-7"/></svg>`;
  const chev = `<svg class="chev" viewBox="0 0 16 16" aria-hidden="true"><path d="m4.5 6.5 3.5 3.5 3.5-3.5"/></svg>`;
  const whose = (a) => (a === data.me ? "your" : `${esc(label(a))}'s`);
  const deliveryLine = (m) => {
    const ds = m.deliveries;
    if (!ds.length) return "";
    if (ds.length === 1) {
      const [d] = ds;
      return d.read_at
        ? `<p class="state">${tick}Handled by ${esc(d.to === data.me ? "you" : label(d.to))} at ${esc(hm(new Date(d.read_at)))}</p>`
        : `<p class="state"><span class="slot" aria-hidden="true"></span><span class="waits">Waiting in ${whose(d.to)} inbox for ${esc(since(new Date(m.sent_at)))}</span></p>`;
    }
    const open = ds.filter((d) => !d.read_at);
    const summary = open.length
      ? `<span class="slot" aria-hidden="true"></span><span class="waits">Waiting in ${open.length} of ${ds.length} inboxes</span>`
      : `${tick}Handled by all ${ds.length}`;
    return `<details class="deliveries"><summary>${summary}${chev}</summary><ul>${ds.map((d) => `
        <li class="${d.read_at ? "" : "waits"}"><span>${addr(d.to)}</span><span>${d.read_at ? `Handled ${esc(hm(new Date(d.read_at)))}` : `Waiting ${esc(since(new Date(m.sent_at)))}`}</span></li>`).join("")}</ul></details>`;
  };
  const files = (m) => (m.attachments.length ? `<div class="files">${m.attachments.map((f) => f.mime.startsWith("image/")
    ? `<a class="shot" href="${esc(f.url)}" target="_blank" rel="noopener"><img src="${esc(f.url)}" alt="${esc(f.name)}"></a>`
    : `<a class="file" href="${esc(f.url)}" target="_blank" rel="noopener"><svg viewBox="0 0 20 20" aria-hidden="true"><path d="M5 2.5h6.5L15 6v11.5H5z"/><path d="M11.5 2.5V6H15"/></svg><span class="fname">${esc(f.name)}</span><span class="size">${esc(size(f.size ?? 0))}</span></a>`).join("")}</div>` : "");
  const renderTranscript = (conversation) => {
    let day = "", prev = null;
    const parts = [conversation.has_more ? `<button class="older" type="button">Show earlier messages</button>` : `<p class="kept">Messages are kept for 30 days.</p>`];
    for (const m of conversation.messages) {
      const d = new Date(m.sent_at);
      if (dayName(d) !== day) { day = dayName(d); prev = null; parts.push(`<p class="day">${esc(day)}</p>`); }
      const cont = prev && prev.from === m.from && prev.to === m.to && d - new Date(prev.sent_at) < 10 * 60000;
      const parent = m.reply_to && conversation.messages.find((x) => x.id === m.reply_to);
      const to = kind(conversation.key) !== "group" && !mine(m.from) ? m.to : null;
      parts.push(`
        <article class="msg${mine(m.from) ? " mine" : ""}${cont ? " cont" : ""}${state.arrived.has(m.id) ? " arrived" : ""}" id="m${m.id}" aria-label="${esc(label(m.from))}, ${esc(hm(d))}">
          <p class="meta"><span class="who">${addr(m.from)}</span>${to ? `<span class="to">to ${esc(to === data.me ? "you" : label(to))}</span>` : ""}<time datetime="${m.sent_at}">${esc(hm(d))}</time></p>
          <div class="message glass">
            ${parent ? `<a class="quote" href="#m${parent.id}" data-jump="m${parent.id}">${esc(plain(parent.text))}</a>` : ""}
            <div class="body">${md(m.text)}</div>
            ${files(m)}
          </div>
          ${deliveryLine(m)}
        </article>`);
      prev = m;
    }
    return parts.join("");
  };
  const renderConvo = () => {
    const pane = $("#convo");
    const conversation = data.open;
    if (!conversation) {
      delete pane.dataset.key;
      pane.innerHTML = state.failed
        ? `<div class="blank"><p>Couldn't load this conversation.</p><button class="retry" type="button">Try again</button></div>`
        : `<div class="blank"><img src="panel/icon.png" alt="">${data.convs.length ? "<p>Choose a conversation.</p>" : ""}</div>`;
      return;
    }
    const r = route();
    const scroller = $(".transcript", pane);
    const atEnd = !scroller || scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 40;
    const same = pane.dataset.key === conversation.key;
    const top = scroller?.scrollTop ?? 0;
    pane.dataset.key = conversation.key;
    pane.innerHTML = `
      <header class="convo-head">
        <a class="icon-btn back" href="#/" aria-label="Back to conversations"><svg viewBox="0 0 20 20" aria-hidden="true"><path d="M12.5 4 6.5 10l6 6"/></svg></a>
        ${disc(conversation.key)}
        <div class="titles"><${titleTag()} class="title" tabindex="-1">${addr(conversation.key)}</${titleTag()}><p>${esc(about(conversation.key).subtitle(conversation.key))}</p></div>
        <button class="icon-btn info-btn" type="button" data-href="${href(conversation.key, !r.info)}" aria-label="Details" aria-expanded="${r.info}" aria-controls="info">
          <svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="7.5"/><path d="M10 9v5M10 6.5h.01"/></svg></button>
      </header>
      <div class="transcript" role="log" aria-label="Messages">${renderTranscript(conversation)}</div>`;
    const t = $(".transcript", pane);
    if (!same || atEnd) {
      t.scrollTop = t.scrollHeight;
      // an image that loads later grows the transcript; stay at the end
      for (const img of t.querySelectorAll("img")) if (!img.complete) img.addEventListener("load", () => { t.scrollTop = t.scrollHeight; }, { once: true });
    } else {
      t.scrollTop = top;
      if (conversation.messages.some((m) => state.arrived.has(m.id))) {
        pane.insertAdjacentHTML("beforeend", `<button class="jump" type="button">New message</button>`);
      }
    }
  };

  // --- the inspector -----------------------------------------------------------------------------------------
  // the addresses in conversation k, under a heading; a group's owner is tagged
  const members = (heading, list, k) => `<h3>${heading}</h3>${list.map((a) => `
    <p class="member">${glyph(a)}<span>${addr(a)}<span class="sub">${esc(about(a).detail(a))}</span></span>${a === data.groups[k]?.owner ? '<span class="tag">Owner</span>' : ""}</p>`).join("")}`;
  const renderInfo = () => {
    const info = $("#info");
    const conversation = data.open;
    const show = Boolean(conversation && route().info);
    info.hidden = !show;
    $(".sheet").classList.toggle("with-info", show);
    if (!show) return;
    const k = conversation.key;
    const c = data.convs.find((x) => x.key === k);
    info.innerHTML = `
      <div class="info-head">
        <a class="icon-btn back" href="${href(k)}" aria-label="Back to the conversation"><svg viewBox="0 0 20 20" aria-hidden="true"><path d="M12.5 4 6.5 10l6 6"/></svg></a>
        ${disc(k)}<div><${titleTag()} class="title" id="info-title" tabindex="-1">${addr(k)}</${titleTag()}><p>${esc(about(k).noun(k))}</p></div>
        <a class="icon-btn info-close" href="${href(k)}" aria-label="Close details"><svg viewBox="0 0 20 20" aria-hidden="true"><path d="m5.5 5.5 9 9m0-9-9 9"/></svg></a>
      </div>
      ${about(k).facts(k, c)}
      <h3>Last 30 days</h3>
      <p class="kv"><span>Messages</span><span>${conversation.messages.length}${conversation.has_more ? "+" : ""}</span></p>
      <p class="kv"><span>Waiting in your inboxes</span><span>${c?.waiting ?? 0}</span></p>`;
  };

  // --- the whole screen ------------------------------------------------------------------------------------------
  const view = () => (!route().key ? "list" : route().info ? "info" : "detail");
  // one h1 a screen: on a phone the pushed conversation's title is it, and the list's title steps down
  const pageTitle = (tag) => {
    const old = $("#title");
    if (old.localName !== tag) old.outerHTML = `<${tag} id="title">Conversations</${tag}>`;
  };
  const render = () => {
    $(".signin")?.remove();
    document.body.dataset.view = view();
    pageTitle(narrow() && route().key ? "p" : "h1");
    $(".who").textContent = data.me;
    $(".account-btn .initial").textContent = data.me[0];
    $(".menu-who").innerHTML = `<b>${esc(data.name || data.me)}</b>Signed in as ${esc(data.me)}`;
    renderHead();
    renderList();
    renderInfo(); // first: it narrows the transcript, which then scrolls to its end
    renderConvo();
    const key = route().key;
    document.title = `${key ? label(key) : "Conversations"} · peerbox`;
    state.arrived.clear();
  };
  const loading = () => {
    const bar = (w) => `<span class="bar" style="width:${w}"></span>`;
    $(".inboxes").innerHTML = ["4em", "6em"].map((w) => `
      <div class="inbox" aria-hidden="true"><span class="label">${bar(w)}</span><span class="fig"><b>${bar("1.4em")}</b></span><span class="sub">${bar("7em")}</span></div>`).join("");
    $(".rows").innerHTML = Array.from({ length: 6 }, (_, i) => `
      <div class="row" aria-hidden="true"><span class="name">${bar(`${8 + (i % 3) * 3}em`)}</span><time>${bar("2.5em")}</time><span class="preview">${bar(`${12 + (i % 2) * 4}em`)}</span></div>`).join("");
    $("#convo").innerHTML = `<div class="blank" aria-busy="true"><p class="sr">Loading</p></div>`;
  };
  const failed = () => {
    document.body.dataset.view = "failed";
    $(".finding").textContent = "";
    $(".inboxes").innerHTML = "";
    $(".rows").innerHTML = `<p class="empty">Couldn't load your conversations. <button class="retry" type="button">Try again</button></p>`;
  };
  const signedOut = () => {
    document.body.dataset.view = "signed-out";
    document.title = "Sign in · peerbox";
    $(".head")?.remove();
    $(".signin").hidden = false;
  };
  // refetch, then draw; a second change during a fetch refetches once more
  let busy = null, again = false;
  const refresh = async () => {
    if (busy) { again = true; return busy; }
    busy = (async () => {
      try { await load(); render(); }
      catch (e) {
        if (e.message === "signed out") signedOut();
        else if (!data.convs) failed();
        else live(false, `Couldn't refresh · updated ${hm(state.updated)}`);
        console.error(e);
      }
      busy = null;
      if (again) { again = false; await refresh(); }
    })();
    return busy;
  };
  const live = (on, text) => {
    $(".live").classList.toggle("on", on);
    $(".live").classList.toggle("off", !on);
    $(".live-text").textContent = text ?? (on ? "Live" : "Reconnecting");
  };

  // --- input -----------------------------------------------------------------------------------------------------
  let from = null;
  addEventListener("hashchange", async () => {
    const before = from;
    from = route().key;
    if (data.open?.key !== openKey()) await refresh(); else render();
    const v = view();
    if (v === "detail" && before !== from) $(".convo-head .title")?.focus({ preventScroll: true });
    else if (v === "info") $("#info-title")?.focus({ preventScroll: true });
    else if (v === "list" && before) $(`.row[data-key="${CSS.escape(before)}"]`)?.focus();
  });
  let wasNarrow = narrow();
  addEventListener("resize", () => {
    if (!data.me) return;
    if (data.open?.key !== openKey()) refresh();
    else if (wasNarrow !== narrow()) render();
    wasNarrow = narrow();
  });
  const menu = (open) => {
    $("#account-menu").hidden = !open;
    $(".account-btn").setAttribute("aria-expanded", open);
  };
  document.addEventListener("click", async (e) => {
    if (!e.target.closest(".account")) menu(false);
    const t = e.target.closest("button, a");
    if (!t) return;
    if (t.matches(".account-btn")) menu($("#account-menu").hidden);
    else if (t.matches(".signout")) { await fetch("/logout", { method: "POST" }); location.replace("/"); }
    else if (t.dataset.inbox) {
      const inList = t.closest(".rows"); // Show all goes away with the line it is on, so focus moves to the first row
      state.inbox = state.inbox === t.dataset.inbox ? null : t.dataset.inbox;
      render();
      if (inList) $(".rows a.row")?.focus();
    }
    else if (t.matches(".retry")) { loading(); refresh(); }
    else if (t.matches(".clear-search")) { const q = $(".search input"); q.value = ""; state.query = ""; renderList(); q.focus(); }
    else if (t.dataset.jump) {
      e.preventDefault(); // the hash holds the route
      const m = document.getElementById(t.dataset.jump);
      m?.scrollIntoView({ block: "center", behavior: reduced() ? "auto" : "smooth" });
      m?.classList.remove("lit"); void m?.offsetWidth; m?.classList.add("lit");
    }
    else if (t.dataset.href) location.hash = t.dataset.href;
    else if (t.matches(".jump")) { const s = $(".transcript"); s.scrollTo({ top: s.scrollHeight, behavior: reduced() ? "auto" : "smooth" }); t.remove(); }
    else if (t.matches(".older")) {
      const conversation = data.open;
      t.disabled = true;
      try {
        const earlier = await historyOf(conversation.key, conversation.messages[0].id);
        const s = $(".transcript");
        const fromEnd = s.scrollHeight - s.scrollTop;
        conversation.messages = [...earlier.messages, ...conversation.messages];
        conversation.has_more = earlier.has_more;
        render();
        const s2 = $(".transcript");
        s2.scrollTop = s2.scrollHeight - fromEnd;
      } catch { t.disabled = false; t.textContent = "Couldn't load earlier messages. Try again"; }
    }
  });
  const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
  $(".search input").addEventListener("input", (e) => { state.query = e.target.value.trim(); renderList(); });
  document.addEventListener("keydown", (e) => {
    const typing = e.target.matches("input");
    if (((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") || (e.key === "/" && !typing)) {
      e.preventDefault(); $(".search input").focus(); return;
    }
    if (e.key === "Escape") {
      if (!$("#account-menu").hidden) { menu(false); $(".account-btn").focus(); }
      else if (typing && state.query) { e.target.value = ""; state.query = ""; renderList(); }
      else if (route().info) location.hash = href(route().key);
      else if (narrow() && route().key) location.hash = "#/";
      return;
    }
    // arrow keys move through the list, as in Mail
    if ((e.key === "ArrowDown" || e.key === "ArrowUp") && e.target.closest(".rows, .search")) {
      e.preventDefault();
      const rows = [...document.querySelectorAll(".rows a.row")];
      const i = rows.indexOf(e.target.closest(".row"));
      rows[Math.max(0, Math.min(rows.length - 1, i + (e.key === "ArrowDown" ? 1 : -1)))]?.focus();
    }
  });

  // --- live: the server says when something was sent or read --------------------------------------------------------
  loading();
  from = route().key;
  refresh().then(() => {
    if (!data.me) return;
    const stream = new EventSource("/api/stream");
    stream.onopen = () => { live(true); if (state.failed || $(".live").dataset.lost) refresh(); delete $(".live").dataset.lost; };
    stream.onerror = () => { live(false); $(".live").dataset.lost = "1"; };
    stream.addEventListener("changed", async () => {
      const before = data.convs[0]?.last.sent_at;
      await refresh();
      const newest = data.convs[0];
      if (newest && newest.last.sent_at !== before) $("#announce").textContent = `New message from ${label(newest.last.from)}${about(newest.key).in(newest.key)}`;
    });
  });
})();
