// Service worker: shows push notifications and handles their buttons.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data.json();
  } catch {
    /* empty or non-JSON push */
  }
  event.waitUntil(
    self.registration.showNotification(data.title || "Assistant", {
      body: data.body || "",
      tag: data.ref || `n${data.id}`,
      icon: "/icon.svg",
      data,
      requireInteraction: data.urgency === "high",
      actions: (data.actions || []).map((a) => ({ action: a.id, title: a.label })),
    }),
  );
});

async function openApp(url) {
  const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  for (const client of windows) {
    if ("focus" in client) {
      await client.navigate(url).catch(() => {});
      return client.focus();
    }
  }
  return self.clients.openWindow(url);
}

async function runAction(ref, action) {
  try {
    const res = await fetch("/api/notifications/action", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ref, action }),
    });
    if (res.ok) return;
  } catch {
    /* fall through */
  }
  // Signed out or offline: say so instead of failing silently.
  await self.registration.showNotification("That didn't go through", {
    body: "Open the assistant to finish that.",
    tag: "action-failed",
    icon: "/icon.svg",
    data: {},
  });
}

self.addEventListener("notificationclick", (event) => {
  const data = event.notification.data || {};
  event.notification.close();
  if (event.action && data.ref) {
    event.waitUntil(runAction(data.ref, event.action));
  } else {
    event.waitUntil(openApp(data.url || "/?tab=reminders"));
  }
});
